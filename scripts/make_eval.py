#!/usr/bin/env python3
"""Generate a retrieval eval set from the index (LLM-written questions, gold = source clause).

Needs a chat model (BILLRAG_CHAT_* env vars, see billrag/config.py). Output is a *draft*: review it
(`--review` prints question/evidence side by side) and delete bad items before trusting the numbers.

  python scripts/make_eval.py --out evals/retrieval_eval.jsonl --seed 7
"""
import argparse
import json
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from billrag.config import load_settings  # noqa: E402
from billrag.llm import make_llm  # noqa: E402
from billrag.store import Store  # noqa: E402

# type -> (regex the clause must match, how to ask, quota)
TYPES = {
    "detail": (r"\b(days?|months?|years?|rupees|per centum|percent|not (less|more) than|not exceeding)\b",
               "asks for a specific figure, time limit, threshold or amount stated in the clause", 12),
    "definition": (r"\bmeans\b|\bincludes\b|\"[^\"]{3,40}\"\s+(means|includes)",
                   "asks what a term or concept is defined as or what it covers", 8),
    "procedure": (r"\bshall\b.*\b(apply|appoint|submit|notify|issue|refer|report|inform|publish|summon|hold)\b",
                  "asks who must do what, or how a process works step by step", 10),
    "penalty": (r"\b(offence|liable|fine|imprisonment|convicted|penalty)\b",
                "asks what offence, punishment, or consequence the clause creates", 8),
    "amendment": (r"\b(amended|substitut|repealed|inserted|omitted)\b",
                  "asks what change this clause makes to an existing law, in plain terms", 10),
}
N_BILL_LEVEL = 12

CLAUSE_PROMPT = """You write evaluation questions for a search engine over Sri Lankan bills.
Given ONE clause of a bill, write ONE question that a curious citizen, journalist or lawyer might ask and that this
clause answers. The question {how}.

Rules:
- Paraphrase. Do NOT reuse any run of 4 or more consecutive words from the clause; use everyday wording.
- The question must stand alone and identify the subject matter (the topic, body or Act concerned, e.g. "under the
  proposed Sri Lanka Cricket law"), but must NOT mention a section number or bill number.
- It must be fully answerable from this clause alone.
Return ONLY JSON: {{"question": "...", "answer": "<one or two sentences, from the clause>"}}

BILL: {title} ({year}). Long title: {long_title}
CLAUSE (section {no}{part}):
{text}"""

BILL_PROMPT = """You write evaluation questions for a search engine over Sri Lankan bills.
Given a bill's title and long title, write ONE question a citizen might ask to find this bill or learn what it is
for, WITHOUT using the bill's exact title words more than necessary (describe the subject in other words).
The question must carry enough identifying detail (the year, the Act being amended, or the named body or subject)
that only THIS bill fits; many bills amend the same Acts in different years, so a vague question is wrong.
Return ONLY JSON: {{"question": "...", "answer": "<one sentence on the bill's purpose>"}}

TITLE: {title} ({year})
LONG TITLE: {long_title}"""


def norm(t):
    return re.sub(r"\s+", " ", t).strip()


def gold_equivalents(store, key, no):
    """The source clause plus identical clauses in other versions (GS vs Bill) of the same title."""
    b = store.bill(key)
    text = norm(store.section_map(key).get(no, ""))
    gold = [{"bill": key, "section": no}]
    for v in store.versions(b["title_norm"], b["date"]):
        if v["key"] != key and norm(store.section_map(v["key"]).get(no, "")) == text:
            gold.append({"bill": v["key"], "section": no})
    return gold


def pick(store, rng, n, regex, used_titles, since_year=0):
    rows = store.db.execute("SELECT key, year, title_norm FROM bills WHERE kind IN ('Bill','Other') AND n_sections>=4 AND year>=?", (since_year,)).fetchall()
    weights = [2.0 if r["year"] >= 2021 else 1.0 for r in rows]
    out, tries = [], 0
    while len(out) < n and tries < 4000:
        tries += 1
        r = rng.choices(rows, weights)[0]
        if r["title_norm"] in used_titles:
            continue
        secs = {k: v for k, v in store.section_map(r["key"]).items() if k >= 2 and 300 <= len(v) <= 1800
                and re.search(regex, v, re.I | re.S)}
        if not secs:
            continue
        no = rng.choice(sorted(secs))
        used_titles.add(r["title_norm"])
        out.append((r["key"], no, secs[no]))
    return out


def ask(llm, prompt):
    for _ in range(3):
        txt = llm.chat("You output only valid JSON.", [{"role": "user", "content": prompt}], max_tokens=1500).text
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                if d.get("question") and d.get("answer"):
                    return d
            except json.JSONDecodeError:
                pass
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="evals/retrieval_eval.jsonl")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--append", action="store_true", help="add to the existing file instead of overwriting")
    ap.add_argument("--only-bill", type=int, default=0, help="generate only N bill-level items (skips clause items)")
    ap.add_argument("--since-year", type=int, default=0, help="only sample bills from this year on")
    ap.add_argument("--clause-scale", type=float, default=1.0, help="multiply the per-type clause quotas")
    ap.add_argument("--bill-n", type=int, default=0, help="number of bill-level items (default 12)")
    ap.add_argument("--review", action="store_true", help="print the existing file for manual review and exit")
    a = ap.parse_args()
    if a.review:
        for it in map(json.loads, open(a.out, encoding="utf-8")):
            print(f"\n[{it['id']}] ({it['type']}, {it['source']['bill']} §{it['source'].get('section')}) {it['question']}"
                  f"\n   A: {it['answer']}\n   EVIDENCE: {it['evidence'][:350]}")
        return
    s = load_settings()
    store, llm, rng = Store(s.db_path), make_llm(s), random.Random(a.seed)
    jobs, used, existing = [], set(), []
    if a.append and Path(a.out).exists():
        existing = [json.loads(l) for l in open(a.out, encoding="utf-8") if l.strip()]
        used = {store.bill(i["source"]["bill"])["title_norm"] for i in existing if store.bill(i["source"]["bill"])}
    for t, (regex, how, quota) in ([] if a.only_bill else TYPES.items()):
        for key, no, text in pick(store, rng, max(1, round(quota * a.clause_scale)), regex, used, a.since_year):
            b = store.bill(key)
            part = (store.section_text(key, no) or {}).get("part")
            jobs.append((t, "clause", key, no, text, CLAUSE_PROMPT.format(
                how=how, title=b["title"], year=b["year"], long_title=b["long_title"] or "n/a", no=no,
                part=f", {part}" if part else "", text=text)))
    rows = store.db.execute("SELECT key FROM bills WHERE kind IN ('Bill','Other') AND long_title IS NOT NULL AND year>=?", (a.since_year,)).fetchall()
    n_bill = a.only_bill or a.bill_n or N_BILL_LEVEL
    pool = [r["key"] for r in rows if store.bill(r["key"])["title_norm"] not in used]
    for r in rng.sample(pool, n_bill):
        b = store.bill(r)
        jobs.append(("bill-purpose", "bill", r, None, b["long_title"], BILL_PROMPT.format(
            title=b["title"], year=b["year"], long_title=b["long_title"])))

    with ThreadPoolExecutor(4) as ex:
        outs = list(ex.map(lambda j: ask(llm, j[5]), jobs))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    n = max([int(i["id"][1:]) for i in existing], default=0)
    start = n
    with open(a.out, "a" if a.append else "w", encoding="utf-8") as f:
        for (t, level, key, no, text, _), d in zip(jobs, outs):
            if not d:
                print(f"  ! no usable output for {key} {no}", file=sys.stderr)
                continue
            b = store.bill(key)
            gold = (gold_equivalents(store, key, no) if level == "clause" else
                    [{"bill": v["key"]} for v in store.versions(b["title_norm"], b["date"])])
            n += 1
            f.write(json.dumps({"id": f"q{n:03d}", "type": t, "level": level, "question": norm(d["question"]),
                                "answer": norm(d["answer"]), "gold": gold,
                                "source": {"bill": key, "section": no, "year": b["year"]},
                                "evidence": norm(text)[:900], "reviewed": False}, ensure_ascii=False) + "\n")
    print(f"wrote {n - start}/{len(jobs)} items to {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
