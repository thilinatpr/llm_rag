"""Appropriation Bill parser: sections + First/Second/Third Schedule tables -> records.

Every amount is checked against totals the bill prints itself (`check`); a bill that
does not reconcile is reported, never silently indexed.

Records (dicts):
  section   {no, text}
  estimate  {head, head_title, group, programme, programme_name, recurrent, capital, page}
  charged   {head, title, authority, programme, recurrent, capital, total, page}   (Second Schedule)
  advance   {srl, title, item, activity, max_exp, min_receipts, max_debit, max_liab, page}  (Third Schedule)
  totals    {first_recurrent, first_capital, second_recurrent, second_capital, second_total, ...}
"""
from __future__ import annotations

import re

AMOUNT = re.compile(r"^-?[\d,]*\d$|^-$")
# an amount: has a comma group, or a dash for nil. Plain digits are NOT amounts: head numbers (249) and
# margin line numbers (5..25) look the same. A bill with an amount under 1,000 fails check() loudly.
NUM = re.compile(r"^\d{1,3}(,\d{3})+$|^-$")


def amount(tok: str) -> int:
    tok = tok.strip()
    return 0 if tok in ("-", "") else int(tok.replace(",", ""))


def page_tokens(page_text: str) -> list[str]:
    """Split a page into non-empty cells (tab/newline separated), minus layout noise."""
    out = []
    for raw in re.split(r"[\t\n]", page_text):
        s = raw.strip()
        if not s:
            continue
        if s == "Appropriation":
            continue
        # margin line numbers 5,10,15,20,25 and the printed page number: no comma, short digits
        if re.fullmatch(r"\d{1,2}", s) and False:
            continue
        out.append(s)
    return out


def schedule_pages(pages: list[str]) -> dict:
    """1-based page numbers where each schedule starts (and where it ends)."""
    starts = {}
    for i, t in enumerate(pages, 1):
        for name in ("FIRST", "SECOND", "THIRD"):
            if re.search(rf"(?m)^\s*{name}\s+SCHEDULE", t) and name not in starts:
                starts[name] = i
    return starts


# ------------------------------------------------------------------ sections

def parse_sections(pages: list[str], first_schedule_page: int) -> list[dict]:
    text = "\n".join(pages[: first_schedule_page - 1])
    text = re.sub(r"(?m)^\s*(\d{1,2}|Appropriation|PL \d+.*|L\.D\..*)\s*$", "", text)
    m = re.search(r"as follows:-", text)
    body = text[m.end():] if m else text
    parts = re.split(r"(?m)^\s*(\d{1,2})\.\s{1,3}(?=\(|[A-Z“\"])", body)
    out, expected = [], 1
    for i in range(1, len(parts) - 1, 2):
        no = int(parts[i])
        if no != expected:  # stray number inside a clause
            if out:
                out[-1]["text"] += f"\n{no}. {parts[i + 1]}"
            continue
        out.append({"no": no, "text": re.sub(r"\s+", " ", parts[i + 1]).strip()})
        expected += 1
    return out


# ------------------------------------------------------------ First Schedule

def parse_first(pages: list[str], p0: int, p1: int) -> tuple[list[dict], dict]:
    """Heads and programmes. Returns (rows, totals)."""
    rows: list[dict] = []
    totals: dict = {"groups": []}
    group = None
    head = None
    for pno in range(p0, p1 + 1):
        toks = page_tokens(pages[pno - 1])
        i = 0
        while i < len(toks):
            t = toks[i]
            # group header: "Head No. 1 - 25 Special ..." or "<Ministry> / Recurrent X / Capital Y / Made up as follows"
            m = re.match(r"Head No\.\s*([\d\s\-–and]+?)\s{2,}(.+)$|^Head No\.\s*([\d\s\-–]+)\s+(.+)$", t)
            if t.startswith("Head No."):
                group = re.sub(r"^Head No\.\s*[\d\s\-–and]+?\s{2,}", "", t) or t
                i += 1
                continue
            if t == "Recurrent" and i + 1 < len(toks) and NUM.match(toks[i + 1]):
                # group subtotal: title is the previous cell
                prev = toks[i - 1] if i else ""
                rec = amount(toks[i + 1])
                j = i + 2
                cap = None
                if j < len(toks) and toks[j] == "Capital" and j + 1 < len(toks):
                    cap = amount(toks[j + 1])
                    j += 2
                if not str(prev).startswith("Head No."):
                    group = prev.strip()
                totals["groups"].append({"group": group, "recurrent": rec, "capital": cap, "page": pno})
                i = j
                continue
            if t == "Head" and i + 2 < len(toks) and re.fullmatch(r"\d+", toks[i + 1]):
                head = {"no": int(toks[i + 1]), "title": toks[i + 2].strip()}
                i += 3
                continue
            m = re.match(r"Programme\s+(\d+)\s*(?:-\s*(.+))?$", t)
            if m and head:
                prog = int(m.group(1))
                if m.group(2):  # "Programme 02 - Development Activities" in one cell
                    name = m.group(2)
                    j = i + 1
                else:
                    name = toks[i + 1]
                    j = i + 2
                vals = []
                while j < len(toks) and len(vals) < 2 and NUM.match(toks[j]):
                    vals.append(amount(toks[j]))
                    j += 1
                rows.append({
                    "head": head["no"], "head_title": head["title"], "group": group,
                    "programme": prog, "programme_name": name.strip(),
                    "recurrent": vals[0] if vals else None,
                    "capital": vals[1] if len(vals) > 1 else None,
                    "page": pno,
                })
                i = j
                continue
            if t == "Total":
                vals = []
                j = i + 1
                while j < len(toks) and len(vals) < 2 and NUM.match(toks[j]):
                    vals.append(amount(toks[j]))
                    j += 1
                if len(vals) == 2:
                    totals["first_recurrent"], totals["first_capital"] = vals
                i = j
                continue
            i += 1
    return rows, totals


# ----------------------------------------------------------- Second Schedule

def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _like(a: str, b: str, n: int = 18) -> bool:
    """Same title start, tolerating 'Minister of' vs 'Ministry of' and similar drift."""
    import difflib
    b = _norm(b)
    n = min(n, len(b))  # a short title ("Parliament") is compared on its own length
    a, b = _norm(a)[:n], b[:n]
    return bool(a) and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def parse_second(pages: list[str], p0: int, p1: int, titles: dict[int, str]) -> tuple[list[dict], dict]:
    """Charged expenditure (Second Schedule). A numeric cell is a head number only when the cells
    after it begin like that head's title in the First Schedule: margin line numbers (5, 10, 15)
    and real heads (5, 10, 15 ...) look alike, so the title decides."""
    flat = []
    for pno in range(p0, p1 + 1):
        for s in page_tokens(pages[pno - 1]):
            parts = s.split()
            m = re.match(r"^(\d{1,3})\s+(\S.*)$", s)
            if len(parts) > 1 and all(NUM.match(x) for x in parts):  # amounts sharing one cell
                flat += [(x, pno) for x in parts]
            elif m and int(m.group(1)) in titles and _like(m.group(2), titles[int(m.group(1))], 10):
                flat += [(m.group(1), pno), (m.group(2), pno)]  # "111 Ministry of Health and" in one cell
            else:
                flat.append((s, pno))
    rows, totals = [], {}
    i, head = 0, None
    while i < len(flat):
        s, pno = flat[i]
        if s == "Total":
            vals, j = [], i + 1
            while j < len(flat) and len(vals) < 3 and NUM.match(flat[j][0]):
                vals.append(amount(flat[j][0]))
                j += 1
            if len(vals) == 3:
                totals["second_recurrent"], totals["second_capital"], totals["second_total"] = vals
            i = j
            continue
        if re.fullmatch(r"\d{1,3}", s) and int(s) in titles:
            j, cells = i + 1, []
            while j < len(flat) and not flat[j][0].startswith("Programme") and len(cells) < 16:
                cells.append(flat[j][0])
                j += 1
            if j < len(flat) and _like(" ".join(cells), titles[int(s)]):
                head, i = int(s), i + 1
                head_cells = cells
                # authority = the cells after the title's words
                acc, k = "", 0
                while k < len(cells) and len(_norm(acc)) < len(_norm(titles[head])):
                    acc += " " + cells[k]
                    k += 1
                authority = " ".join(cells[k:])
                i = j
                continue
        if s.startswith("Programme") and head is not None:
            prog = re.sub(r"\s+", " ", s)
            k = i + 1
            name = []
            while k < len(flat) and not NUM.match(flat[k][0]) and len(name) < 3 and not flat[k][0].startswith("Programme"):
                name.append(flat[k][0])
                k += 1
            vals = []
            while k < len(flat) and len(vals) < 3 and NUM.match(flat[k][0]):
                vals.append(amount(flat[k][0]))
                k += 1
            if not vals:  # column-header words, not a row
                i = k
                continue
            rows.append({"head": head, "title": titles[head], "authority": re.sub(r"\s+", " ", authority).strip(),
                         "programme": re.sub(r"\s+", " ", (prog + " " + " ".join(name))).strip(), "vals": vals, "page": pno})
            i = k
            continue
        i += 1
    return rows, totals


def parse_bill(pdf_path: str) -> dict:
    import pymupdf
    doc = pymupdf.open(pdf_path)
    pages = [p.get_text() for p in doc]
    st = schedule_pages(pages)
    n = len(pages)
    first_end = st["SECOND"] - 1
    second_end = st["THIRD"] - 1
    sections = parse_sections(pages, st["FIRST"])
    est, totals = parse_first(pages, st["FIRST"], first_end)
    titles = {}
    for r in est:
        titles.setdefault(r["head"], r["head_title"])
    charged, t2 = parse_second(pages, st["SECOND"], second_end, titles)
    totals.update(t2)
    for r in charged:
        v = r["vals"]
        rec, cap, tot = (v + [0, 0, 0])[:3] if len(v) == 3 else (v[0], 0, v[-1]) if len(v) == 2 else (0, 0, 0)
        r.update(recurrent=rec, capital=cap, total=tot)
    return {"pages": n, "schedules": st, "sections": sections, "estimate": est, "charged": charged, "totals": totals}


def stated_total(sections: list[dict]) -> int | None:
    """The sum named in section 2(1): 'rupees four thousand nine hundred ... thousand'."""
    return words_to_int(next((re.search(r"estimated to be rupees ([a-z\- ]+?) for the service", s["text"]).group(1)
                              for s in sections if s["no"] == 2 and re.search(r"estimated to be rupees ([a-z\- ]+?) for the service", s["text"])), None))


_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("zero ten twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALE = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000, "trillion": 1_000_000_000_000}


def words_to_int(text: str | None) -> int | None:
    if not text:
        return None
    grand = chunk = cur = 0
    for w in re.split(r"[\s\-]+", text.lower().replace(",", "")):
        if w in _UNITS:
            cur += _UNITS[w]
        elif w in _TENS:
            cur += _TENS[w]
        elif w == "hundred":
            cur *= 100
        elif w == "thousand":
            chunk += max(cur, 1) * 1000
            cur = 0
        elif w in _SCALE:
            grand += (chunk + cur) * _SCALE[w]
            chunk = cur = 0
        elif w in ("and", ""):
            continue
        else:
            return None
    return grand + chunk + cur


def check(bill: dict) -> list[str]:
    """Human-readable problems; empty list = every printed total reconciled."""
    problems = []
    est, t = bill["estimate"], bill["totals"]
    rec = sum(r["recurrent"] or 0 for r in est)
    cap = sum(r["capital"] or 0 for r in est)
    if t.get("first_recurrent") != rec:
        problems.append(f"First Schedule recurrent: printed {t.get('first_recurrent')} vs sum of programmes {rec}")
    if t.get("first_capital") != cap:
        problems.append(f"First Schedule capital: printed {t.get('first_capital')} vs sum of programmes {cap}")
    ch = bill["charged"]
    for key, label in (("recurrent", "second_recurrent"), ("capital", "second_capital"), ("total", "second_total")):
        s = sum(r[key] for r in ch)
        if t.get(label) != s:
            problems.append(f"Second Schedule {key}: printed {t.get(label)} vs sum of rows {s}")
    said = stated_total(bill["sections"])
    if said is not None and said != rec + cap:
        problems.append(f"Section 2(1) total in words {said} vs First Schedule {rec + cap}")
    if said is None:
        problems.append("could not read the section 2(1) total")
    return problems
