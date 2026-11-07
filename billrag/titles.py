"""Title index and ranker: which bill or Act does a question name?

A side table `title_index` (like `bill_meta` and `glossary`: `chunks`, `fingerprint()` and `vectors.npz` are untouched, old code
ignores it, code without it falls back to `Pipeline.titled_bills`). One row per bill family (its GS + Bill versions) and one per
Act, built deterministically from `bills` + `bill_meta`, no model call.

The ranker is a small hand-weighted scorer made for titles, not a general text ranker:
  cov_q  how much of what the user SAID is in the title (idf-weighted; words that are in no title, like "summarise", are ignored)
  cov_t  how much of the title the user said (idf-weighted: "Sri Lanka" or "Amendment" cost little, a name costs a lot)
  order  the matched title words come in the same order
  year   a year in the question equals / differs from the bill's year
  kind   the question says "act" or "bill" and the row is one
Words match exactly or by a close spelling ("commision"). The decision is `one` (a clear winner), `many` (several titles fit
equally: ask the user which) or `none`. Weights were set with `evaluate()` (synthetic title questions, see its docstring).
"""
import difflib
import json
import math
import re
from collections import Counter

SCHEMA = """
CREATE TABLE IF NOT EXISTS title_index(
  family TEXT PRIMARY KEY, key TEXT NOT NULL, keys TEXT NOT NULL, title TEXT NOT NULL, words TEXT NOT NULL,
  kind TEXT NOT NULL, year INT, date TEXT);
"""
SUFFIX = re.compile(r",?\s*No\.?\s*\d+\s+of\s+\d{4}\s*$|\s-\s(?:Bill|GS)\s*$", re.I)
KIND_WORDS = {"act", "bill", "gs"}  # said by the user about the kind of text, not part of what it is called
STOP = KIND_WORDS | {"the", "of", "and", "for", "to", "in", "no", "a", "an"}
# Tuned on synthetic title questions (see evaluate): weights, the score a winner needs, and how close a rival may be.
W = {"cov_q": 0.55, "cov_t": 0.35, "order": 0.04, "year": 0.06, "kind": 0.10}
MIN_SCORE = 0.62
MARGIN = 0.05
MIN_COV_Q = 0.99  # the title must contain everything the user said about it
CLOSE = 0.85
MAX_ASK = 30  # more equally good titles than this means the question names nothing in particular


# words of a gist question that are not about the title ("key provisions of ..."); "provisions" and "summary" do occur in titles
FILLER = set("""key main major important principal provisions provision points point features feature clauses clause changes tell me about
summarise summarize summary overview outline explain describe what does do give details detail please can you could is are whats
whats new latest recent proposed this that these those one any which more say""".split())


def plain_title(title):
    return SUFFIX.sub("", title or "").strip()


def words_of(text):
    return [w for w in re.findall(r"[a-z]{2,}", (text or "").lower()) if w not in STOP]


def build(store):
    """(Re)compute title_index. Families come from bill_meta when it exists (else title + year)."""
    db = store.db
    db.executescript(SCHEMA)
    has_meta = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='bill_meta'").fetchone())
    fam = {r[0]: r[1] for r in db.execute("SELECT bill_key, family FROM bill_meta")} if has_meta else {}
    groups = {}
    for r in db.execute("SELECT key, title, title_norm, kind, date, year FROM bills ORDER BY date, key"):
        is_act = (r["kind"] or "") == "Act"
        f = ("act:" if is_act else "") + (fam.get(r["key"]) or f"{r['title_norm']}|{r['year']}")
        groups.setdefault(f, []).append(dict(r))
    rows = []
    for f, bs in groups.items():
        top = bs[-1]  # newest
        title = plain_title(top["title"])
        w = words_of(title)
        if w:
            rows.append((f, top["key"], json.dumps([b["key"] for b in bs]), title, " ".join(w),
                         "act" if (top["kind"] or "") == "Act" else "bill", top["year"], top["date"]))
    db.execute("DELETE FROM title_index")
    db.executemany("INSERT INTO title_index VALUES(?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return {"titles": len(rows), "acts": sum(1 for r in rows if r[5] == "act")}


def has_index(store):
    return bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='title_index'").fetchone()) and \
        bool(store.db.execute("SELECT 1 FROM title_index LIMIT 1").fetchone())


class Ranker:
    """Loads the index once; `rank(question)` scores every row."""

    def __init__(self, store):
        self.rows = [dict(r) for r in store.db.execute("SELECT * FROM title_index")]
        df = Counter()
        for r in self.rows:
            r["w"] = r["words"].split()
            df.update(set(r["w"]))
        n = len(self.rows)
        self.idf = {w: math.log((n + 1) / (c + 0.5)) for w, c in df.items()}
        self.vocab = list(df)

    def canon(self, qwords):
        """Question word -> the title word it means (exact or a close spelling), or None when it is in no title."""
        out = []
        for w in qwords:
            if w in self.idf:
                out.append(w)
                continue
            m = difflib.get_close_matches(w, self.vocab, 1, CLOSE) if len(w) >= 5 else []
            out.append(m[0] if m else None)
        return out

    def score(self, row, q, qc, years, said):
        tw = row["w"]
        tset = set(tw)
        used = {c for c in qc if c}
        tot_t = sum(self.idf[w] for w in tw) or 1.0
        hit_t = sum(self.idf[w] for w in tset & used)
        known = [c for c in qc if c]
        tot_q = sum(self.idf[c] for c in set(known)) or 1.0
        cov_q = sum(self.idf[c] for c in set(known) if c in tset) / tot_q
        cov_t = hit_t / tot_t
        pos = [qc.index(w) for w in tw if w in qc]
        order = 1.0 if pos == sorted(pos) and len(pos) > 1 else 0.0
        year = 0.0
        if years and row["year"]:
            year = 1.0 if row["year"] in years else -1.0
        kind = 0.0
        if said:
            kind = 1.0 if row["kind"] in said else -1.0
        s = W["cov_q"] * cov_q + W["cov_t"] * cov_t + W["order"] * order + W["year"] * year + W["kind"] * kind
        return s, cov_q

    def rank(self, question):
        """-> (decision, candidates): decision "one" | "many" | "none"; candidates are row dicts, best first (with `score`)."""
        q = (question or "").lower()
        qw = [w for w in re.findall(r"[a-z]{2,}", q) if w not in STOP and w not in FILLER]
        qc = self.canon(qw)
        if not any(qc):
            return "none", []
        years = {int(y) for y in re.findall(r"\b(20\d\d|19\d\d)\b", q)}
        said = set()
        if re.search(r"\bacts?\b", q):
            said.add("act")
        if re.search(r"\bbills?\b|\bdraft\b", q):
            said.add("bill")
        if said == {"act", "bill"}:
            said = set()
        scored = []
        for r in self.rows:
            s, cq = self.score(r, q, qc, years, said)
            if cq >= MIN_COV_Q:
                scored.append({**r, "score": s})
        if not scored:
            return "none", []
        scored.sort(key=lambda r: (-r["score"], r["date"] or ""), reverse=False)
        scored.sort(key=lambda r: (round(r["score"], 3), r["date"] or ""), reverse=True)
        # one row per title: an Act beats its own bill unless the user said "bill"
        seen, uniq = {}, []
        for r in scored:
            ident = r["words"]
            if ident in seen:
                continue
            seen[ident] = r
            uniq.append(r)
        top = uniq[0]
        if top["score"] < MIN_SCORE:  # what the user said fits many titles and names none ("the Foundation incorporation bill")
            close = [r for r in uniq if top["score"] - r["score"] <= MARGIN]
            if said and 1 < len(close) <= MAX_ASK:
                return "many", sorted(close, key=lambda r: r["date"] or "", reverse=True)
            return "none", []
        close = [r for r in uniq if top["score"] - r["score"] <= MARGIN]
        if len(close) == 1:
            return "one", [top]
        return "many", sorted(close, key=lambda r: r["date"] or "", reverse=True)  # newest first: users ask about recent bills


# ------------------------------------------------------------------ evaluation
def _typo(word, rng):
    if len(word) < 6:
        return word
    i = rng.randrange(1, len(word) - 2)
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


def make_cases(store, n=300, seed=7):
    """Synthetic title questions with gold = the row's family: full title, without bracketed words, a typo, with a year.
    Gold also accepts any row with the same words (the same title in another year is the question's own ambiguity)."""
    import random
    rng = random.Random(seed)
    rows = [dict(r) for r in store.db.execute("SELECT * FROM title_index")]
    rng.shuffle(rows)
    cases = []
    for r in rows[:n]:
        t = r["title"]
        short = re.sub(r"\s*\([^)]*\)", "", t).strip() or t
        w = short.split()
        noun = "Act" if r["kind"] == "act" else "bill"
        variant = rng.choice(["full", "short", "typo", "year", "lower"])
        if variant == "full":
            q = f"Summarise the {t} {noun}"
        elif variant == "short":
            q = f"key provisions of the {short} {noun}"
        elif variant == "typo":
            i = rng.randrange(len(w))
            w[i] = _typo(w[i], rng)
            q = f"tell me about {' '.join(w)} {noun}"
        elif variant == "year":
            q = f"what does the {short} {noun} {r['year']} do"
        else:
            q = f"explain {t.lower()}"
        cases.append({"q": q, "gold": r["words"], "variant": variant, "key": r["key"]})
    return cases


def evaluate(store, n=300, seed=7):
    """Accuracy of the decision on synthetic questions. `right` = decision one with the gold title, or many with the gold title
    among the candidates; `wrong_one` = a confident wrong winner (the costly error); `none` = no decision (falls back to search)."""
    rk = Ranker(store)
    out = Counter()
    for c in make_cases(store, n, seed):
        d, cands = rk.rank(c["q"])
        gold = {r["words"] for r in cands}
        ok = c["gold"] in gold
        if d == "one":
            out["right_one" if ok else "wrong_one"] += 1
        elif d == "many":
            out["right_many" if ok else "wrong_many"] += 1
        else:
            out["none"] += 1
        out["n"] += 1
    return dict(out)
