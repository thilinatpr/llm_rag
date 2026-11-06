"""Agentic RAG loop: the model plans, calls retrieval tools (possibly many times), then answers with citations."""
import difflib
import json
import re

from . import meta
from .store import normalize_ref

SYSTEM = """You are an expert assistant that helps people scrutinize and understand bills of the Parliament of Sri Lanka.
You can ONLY rely on what your tools return from the indexed bills (English text of bills and Gazette-supplement versions).

How to work:
- For "which bill/law is about X?" questions use find_bills first (it searches bill-level text), then
  get_bill_overview / search_bills to confirm in the clauses.
- Orient with list_bills / get_bill_overview; find passages with search_bills. For multi-part or comparative questions,
  run several focused searches (rephrase, use the bill's own wording, filter by bill or year).
- Before stating precise obligations, powers, offences, penalties, definitions or amounts, read the full clause with
  get_section; search snippets can be cut mid-clause.
- Use diff_bills to compare a Gazette (GS) version with the later Bill version, or two related bills.
- Users mostly care about recently published bills. When several bills match and the user gives no year, prefer the
  newest, state the dates you relied on, and mention older bills only if they are clearly relevant or asked about.
- "GS" = bill published in the Gazette before presentation; "Bill" = version presented to Parliament. Amendments made
  later in committee are NOT in this corpus; say so when relevant.

Answer rules:
- Cite every substantive claim as [bill-key §section], e.g. [077/2026 §12(2)]; cite schedules as [key Schedule].
- Distinguish clearly between what the text says (quote short exact phrases) and your interpretation or analysis.
- If the tools do not contain the answer, say so plainly. Never invent clauses, numbers, or section references.
- Be concise and structured: a short answer first, then supporting detail. You explain; you do not give legal advice."""

def _meta_line(store, key):
    m = meta.meta(store, key)
    if not m:
        return []
    tags = ", ".join(json.loads(m["tags"])) if m.get("tags") else "none"
    amends = f"; amends: {m['amends']}" if m.get("amends") else ""
    return [f"Type: {m['bill_type']} ({m['stage']}){amends}; newest instance of its title: {'yes' if m['is_latest'] else 'no'}; tags: {tags}"]


TOOLS = [
    {"name": "find_bills",
     "description": "Find WHICH bills are about a topic, purpose, body or Act (searches bill-level text: titles, long titles, "
                    "auto-generated summaries, outlines). Returns one entry per bill. Use for 'which law ...' questions.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "year": {"type": "integer", "description": "Only bills from this year"},
         "bill_type": {"type": "string", "enum": ["appropriation", "incorporation", "amendment", "new"], "description": "Only this kind of bill"},
         "tag": {"type": "string", "description": "Only bills with this topic tag (e.g. 'taxation', 'criminal law')"},
         "latest_only": {"type": "boolean", "description": "Only the newest instance of a recurring title (e.g. this year's Appropriation Bill)"},
"top_k": {"type": "integer", "description": "Bills to return (default 6, max 12)"}},
         "required": ["query"]}},
    {"name": "search_bills",
     "description": "Hybrid (keyword + semantic) search over bill CLAUSES and schedules. Returns cited passages. Use specific wording; "
                    "call several times for different aspects.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"},
         "year": {"type": "integer", "description": "Only bills from this year"},
         "bill": {"type": "string", "description": "Restrict to one bill, e.g. '077/2026'"},
         "bill_type": {"type": "string", "enum": ["appropriation", "incorporation", "amendment", "new"], "description": "Only this kind of bill"},
         "tag": {"type": "string", "description": "Only bills with this topic tag (e.g. 'taxation', 'criminal law')"},
         "latest_only": {"type": "boolean", "description": "Only the newest instance of a recurring title (e.g. this year's Appropriation Bill)"},
         "top_k": {"type": "integer", "description": "Passages to return (default 8, max 15)"}},
         "required": ["query"]}},
    {"name": "list_bills",
     "description": "List indexed bills (newest first), optionally filtered by year, title words, or kind (Bill|GS).",
     "parameters": {"type": "object", "properties": {
         "year": {"type": "integer"}, "title_contains": {"type": "string"},
         "kind": {"type": "string", "enum": ["Bill", "GS"]}, "limit": {"type": "integer"}}}},
    {"name": "get_bill_overview",
     "description": "Metadata, long title, summary (if available), related versions and the section outline of one bill.",
     "parameters": {"type": "object", "properties": {"bill": {"type": "string", "description": "e.g. '077/2026'"}},
                    "required": ["bill"]}},
    {"name": "get_section",
     "description": "Full text of one numbered section of a bill.",
     "parameters": {"type": "object", "properties": {"bill": {"type": "string"}, "section": {"type": "integer"}},
                    "required": ["bill", "section"]}},
    {"name": "diff_bills",
     "description": "Section-by-section comparison of two bills/versions (added, removed, changed sections with diffs).",
     "parameters": {"type": "object", "properties": {
         "bill_a": {"type": "string"}, "bill_b": {"type": "string"},
         "section": {"type": "integer", "description": "Only diff this section number"}},
         "required": ["bill_a", "bill_b"]}},
]

MAX_TOOL_CHARS = 12000
# Some models (gpt-oss via Workers AI) occasionally emit their raw tool-call syntax as plain text instead of a
# structured call; treat that as a failed turn and ask again rather than showing it as the answer.
LEAK = re.compile(r"<\|(?:call|start|end|channel|message|return|constrain)\|>|\bto=functions\b")


def fmt_passage(c, limit=1100):
    where = f"§{c['section_no']}" if c.get("section_no") is not None else c["kind"]
    part = f" — {c['part']}" if c.get("part") else ""
    text = c["text"] if len(c["text"]) <= limit else c["text"][:limit] + " …[truncated; use get_section]"
    also = f"\n(identical text also in: {', '.join(c['also_in'])})" if c.get("also_in") else ""
    return (f"[{c['bill_key']} {where}, p.{c['page']}] {c['title']} ({c['bill_kind']}, {c['date'][:10]}){part}\n{text}{also}")


class Agent:
    def __init__(self, store, retriever, llm, max_steps=8, on_event=None):
        self.store, self.retriever, self.llm = store, retriever, llm
        self.max_steps, self.on_event = max_steps, on_event or (lambda *_: None)
        self.messages = []

    # ------------------------------------------------------------ tools
    def resolve(self, ref):
        key = normalize_ref(str(ref))
        if key and self.store.bill(key):
            return key
        if key:
            raise ValueError(f"No indexed bill {key}")
        hits = self.store.find_bills(title_contains=str(ref), limit=6)
        if len(hits) == 1:
            return hits[0]["key"]
        if not hits:
            raise ValueError(f"No bill matches {ref!r}; use 'NNN/YYYY' or list_bills")
        raise ValueError("Ambiguous; specify one of: " + ", ".join(f"{h['key']} ({h['title']})" for h in hits))

    def tool_find_bills(self, query, year=None, top_k=6, bill_type=None, tag=None, latest_only=False):
        keys = meta.keys_where(self.store, bill_type=bill_type, tag=tag, latest_only=latest_only)
        if keys is not None and not keys:
            return "No bills match those filters."
        res = self.retriever.search(query, k=min(int(top_k or 6), 12), year=year, keys=keys, kinds=("overview",))
        out = []
        for c in res:
            b = self.store.bill(c["bill_key"])
            vers = [v["key"] for v in self.store.versions(b["title_norm"], b["date"]) if v["key"] != b["key"]]
            summ = (b.get("summary") or "").split("Keywords:")[0].strip()
            out.append(f"{b['key']} | {b['date'][:10]} | {b['kind']} | {b['title']}\n  Long title: {(b['long_title'] or 'n/a')[:260]}"
                       + (f"\n  Summary (auto-generated): {summ[:420]}" if summ else "")
                       + (f"\n  Other versions: {', '.join(vers)}" if vers else ""))
        return "\n\n".join(out) or "No matching bills."

    def tool_search_bills(self, query, year=None, bill=None, top_k=8, bill_type=None, tag=None, latest_only=False):
        keys = [self.resolve(bill)] if bill else meta.keys_where(self.store, bill_type=bill_type, tag=tag, latest_only=latest_only)
        if keys is not None and not keys:
            return "No bills match those filters."
        res = self.retriever.search(query, k=min(int(top_k or 8), 15), year=year, keys=keys, kinds=("section", "schedule"))
        return "\n\n".join(fmt_passage(c) for c in res) or "No matching passages."

    def tool_list_bills(self, year=None, title_contains=None, kind=None, limit=30):
        rows = self.store.find_bills(year, title_contains, kind, min(int(limit or 30), 100))
        return "\n".join(f"{b['key']} | {b['date'][:10]} | {b['kind']} | {b['title']} | {b['n_sections']} sections"
                         for b in rows) or "No bills found."

    def tool_get_bill_overview(self, bill):
        key = self.resolve(bill)
        b = self.store.bill(key)
        vers = [v for v in self.store.versions(b["title_norm"], b["date"]) if v["key"] != key]
        lines = [f"{key} | {b['title']} | {b['kind']} | {b['date'][:10]} | {b['n_pages']} pages, {b['n_sections']} sections",
                 f"Long title: {b['long_title']}",
                 *_meta_line(self.store, key),
                 f"Presented by: {b['presented_by']} on {b['presented_on']}; Gazette: {b['gazette_date']}"]
        if b.get("summary"):
            lines.append(f"Summary (auto-generated; verify against the clauses): {b['summary']}")
        if vers:
            lines.append("Related versions: " + "; ".join(f"{v['key']} ({v['kind']}, {v['date'][:10]})" for v in vers))
        lines.append("Outline:")
        for o in self.store.outline(key)[:150]:
            ref = f"§{o['section_no']}" if o["section_no"] is not None else "Sched."
            lines.append(f"  {ref} p.{o['page']} {o['heading'] or o['head']}")
        return "\n".join(lines)

    def tool_get_section(self, bill, section):
        key = self.resolve(bill)
        s = self.store.section_text(key, section)
        if not s:
            raise ValueError(f"{key} has no section {section}")
        refs = meta.refs_from(self.store, key, section)
        note = f"\n(This section refers to: {', '.join('§' + str(n) for n in refs)}; use get_section to read them.)" if refs else ""
        return f"[{key} §{section}, p.{s['page']}]{' ' + s['part'] if s['part'] else ''}\n{s['text']}{note}"

    def tool_diff_bills(self, bill_a, bill_b, section=None):
        a, b = self.resolve(bill_a), self.resolve(bill_b)
        ma, mb = self.store.section_map(a), self.store.section_map(b)
        nz = lambda t: re.sub(r"\s+", " ", t).strip()
        nums = [section] if section else sorted(set(ma) | set(mb))
        added = [n for n in nums if n in mb and n not in ma]
        removed = [n for n in nums if n in ma and n not in mb]
        changed = [n for n in nums if n in ma and n in mb and nz(ma[n]) != nz(mb[n])]
        same = sum(1 for n in nums if n in ma and n in mb and nz(ma[n]) == nz(mb[n]))
        out = [f"{a} -> {b}: {same} identical, {len(changed)} changed {changed[:40]}, "
               f"added in {b} {added[:40]}, removed from {a} {removed[:40]}"]
        for n in changed[:6]:
            sa, sb = re.split(r"(?<=[.;:])\s+", nz(ma[n])), re.split(r"(?<=[.;:])\s+", nz(mb[n]))
            d = [l for l in difflib.unified_diff(sa, sb, lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
            out.append(f"§{n} diff ('-' = {a}, '+' = {b}):\n" + "\n".join(d)[:1500])
        return "\n\n".join(out)

    def run_tool(self, name, args):
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return f"ERROR: unknown tool {name}"
        try:
            return fn(**args)[:MAX_TOOL_CHARS]
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    # ------------------------------------------------------------ loop
    def ask(self, question: str) -> str:
        self.messages.append({"role": "user", "content": question})
        leaks = 0
        for _ in range(self.max_steps + 2):
            reply = self.llm.chat(SYSTEM, self.messages, TOOLS)
            self.messages.append({"role": "assistant", "content": reply.text,
                                  "tool_calls": [{"id": t.id, "name": t.name, "args": t.args} for t in reply.tool_calls]})
            if not reply.tool_calls and LEAK.search(reply.text) and leaks < 2:
                leaks += 1
                self.messages[-1]["content"] = ""  # do not keep the malformed text in the history
                self.messages.append({"role": "user", "content": "Your last message contained malformed tool-call text "
                                      "instead of a real tool call or an answer. Use the tool-calling interface, or "
                                      "give your final answer in plain prose."})
                continue
            if not reply.tool_calls:
                return LEAK.split(reply.text)[0].strip() or reply.text
            for t in reply.tool_calls:
                self.on_event(t.name, t.args)
                self.messages.append({"role": "tool", "tool_call_id": t.id, "name": t.name,
                                      "content": self.run_tool(t.name, t.args)})
        self.messages.append({"role": "user", "content": "Step limit reached. Answer now using only what you retrieved."})
        reply = self.llm.chat(SYSTEM, self.messages, [])
        self.messages.append({"role": "assistant", "content": reply.text, "tool_calls": []})
        return reply.text
