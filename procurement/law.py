"""Law passages from Bill Assistant for Procurement Rules (owner, 2026-11-09).

Procurement Rules holds procurement rules, not tax law. A question like "the contractor asks for SSCL on 2024 payments of a 2021 contract:
can we pay?" needs both: the contract rules (here) and who owes the levy (the SSCL Act, in Bill Assistant). When a question names a tax,
a levy or an Act, this module asks Bill Assistant for law passages (no model call there) through the Worker (/internal/law -> service
binding -> Bill Assistant's /internal/evidence), in parallel with our own search, and the answer is written from both.

- Bill Assistant may be asleep: starting it takes 1-3 min, and we WAIT for it (owner: wait for cold starts, do not keep it warm).
- Its passages keep their own citations ([Act 25/2022 §3], [Cons 25/2022 §4], bills [017/2026 §2]) and STATUS lines; `verify` checks
  them against what was received. Its fixed notes (IRD consolidated text, as enacted, scanned copy) go under the answer by code.
- If Bill Assistant cannot be reached, the answer says, by code, where the law can be asked.
"""
import json
import re
import time
import urllib.request

from billrag import calltrace

# A question that turns on a tax, levy or duty, or names an Act or an Ordinance. Procurement rules mention taxes in passing (bids "inclusive
# of taxes"), so only the question decides, not the passages.
NEEDS_LAW = re.compile(
    r"\b(?:tax(?:es|ation)?|levy|levies|cess|duty|duties|VAT|SSCL|social\s+security\s+contribution|income\s+tax|PAYE|APIT|withholding|"
    r"excise|customs\s+duty|stamp\s+duty|NBT|nation\s+building\s+tax|inland\s+revenue|IRD)\b"
    r"|\bAct\s*,?\s*No\.?\s*\d{1,3}\s*(?:of|/)\s*\d{4}|\bOrdinance\b", re.I)

CITE = re.compile(r"\[((?:Act|Cons) \d{1,3}/\d{4}|\d{3}/\d{4})(?:\s*§\s*(\d+))?")
FALLBACK_NOTE = ("For what the tax law itself says (who owes a levy, its rate, from when), ask Bill Assistant (bills.citizenlk.com): "
                 "these procurement sources do not hold Acts of Parliament.")
SYSTEM_NOTE = """
- Passages labelled [Act N/YYYY §s], [Cons N/YYYY §s] or a bill [NNN/YYYY §s] come from Bill Assistant: Acts of Parliament (certified text),
  Inland Revenue Department consolidated texts of tax Acts (a convenient reference, not statutory) and bills (proposals, not law). Each has
  a STATUS line: follow it. They say what the LAW is (who owes a tax, its rate, from when); the procurement passages say what the RULES for
  public procurement are. Keep the two apart in the answer and cite each claim with its own label. A tax law says who owes the tax; whether
  a contract price changes because of a new tax is a matter for the contract's conditions, unless a passage given says otherwise."""
USER_AGENT = "citizenlk-procurement/1.0"


def needs_law(question):
    return bool(NEEDS_LAW.search(question or ""))


class LawClient:
    """POST {query, k} to the Worker's /internal/law. Waits for a sleeping Bill Assistant (timeout covers a cold start)."""

    def __init__(self, url, secret, timeout=240):
        self.url, self.secret, self.timeout = url, secret, timeout

    def evidence(self, query, k=4):
        body = json.dumps({"query": query, "k": k}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Proxy-Secret": self.secret, "User-Agent": USER_AGENT})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.load(r)
        except Exception as e:
            calltrace.record("law", time.time() - t, model="bill-assistant", error=type(e).__name__)
            raise
        calltrace.record("law", time.time() - t, model="bill-assistant")
        return out


def to_units(reply):
    """Bill Assistant's passages -> units that render() and the answer's Sources understand (doc "LAW")."""
    units = []
    for p in (reply or {}).get("passages", []):
        text = (f"STATUS: {p['status']}\n" if p.get("status") else "") + (p.get("text") or "")
        units.append({"doc": "LAW", "ref": str(p["section"]) if p.get("section") is not None else "", "key": p["key"],
                      "label": p["cite"], "heading": f"{p.get('title', '')}, from Bill Assistant", "chapter": "", "page": "-",
                      "text": text, "note": ""})
    return units


def verify(answer, units):
    """Law citations in the answer that point at no passage received from Bill Assistant."""
    have = {(u["key"], u["ref"]) for u in units if u["doc"] == "LAW"}
    keys = {u["key"] for u in units if u["doc"] == "LAW"}
    bad = []
    for m in CITE.finditer(answer or ""):
        key, sec = m.group(1), m.group(2) or ""
        if (key, sec) not in have and not (not sec and key in keys):
            bad.append(f"{key} §{sec}" if sec else key)
    return list(dict.fromkeys(bad))


def notes(answer, reply):
    """The fixed notes of the law documents the answer cites (IRD consolidated text, as enacted, scanned copy)."""
    cited = {m.group(1) for m in CITE.finditer(answer or "")}
    out = []
    for key, note in ((reply or {}).get("notes") or {}).items():
        if key in cited and note not in out:
            out.append(note)
    return out
