#!/usr/bin/env python
"""Build the public, crawlable pages for citizenlk.com from the published index (SEO phase 2, see TODO.md).

One page per Act and per bill family, two list pages, and a sitemap, all static HTML written into site/ (served by the
`citizenlk-home` Worker). Nothing here calls a model or the network: it reads index.sqlite (and bills.csv for the official PDF links).

  python scripts/build_site.py --index <dir with index.sqlite [bills.csv]> [--out site] [--bills-since 2026] [--acts-since 2024]

Generated paths are git-ignored (site/acts/, site/bills/, site/sitemap-docs.xml). Run this before every `wrangler deploy -c workers/home/wrangler.toml`,
otherwise the homepage's links to /acts/ and /bills/ lead nowhere.

What a page may say is deliberately narrow (same rules as billrag/acts.py): no Act held means "not shown to have passed", never "failed";
a "dropped" clause means its WORDING is not in the Act, not that the Act is silent; the summary is labelled as not official; the text of
a bill or Act is linked, not copied. Page text follows DESIGN.md: English only, no technology words, the honesty footer.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import quote

SITE = "https://citizenlk.com"
CHAT = "https://bills.citizenlk.com"
DOCS = "https://documents.gov.lk"
KIND_LABEL = {"Bill": "Bill as presented to Parliament", "GS": "Gazette text"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


# ---------------------------------------------------------------- small helpers

def e(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def slug(key: str) -> str:
    """'Act 27/2026' -> '27-2026'; '085/2026' -> '085-2026'."""
    return re.sub(r"[^0-9A-Za-z]+", "-", key.replace("Act ", "")).strip("-")


def nice_date(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
    return f"{d} {MONTHS[m - 1]} {y}"


def fix_case(text: str) -> str:
    """The PDF text of some long titles has stray capitals inside words (OrdinAnce, DAngerous, ChApter). A capital in the middle of a word that
    is followed by a lowercase letter is that artefact and is lowercased. Acronyms (all capitals) and names that end in a capital (UnV) are left alone."""
    def word(m):
        w = m.group(0)
        return re.sub(r"(?<=[A-Za-z])[A-Z](?=[a-z])", lambda x: x.group(0).lower(), w)
    return re.sub(r"[A-Za-z]{2,}", word, re.sub(r"\s+", " ", text or "").strip())


def plain_title(title: str) -> str:
    return re.sub(r"\s+-\s+(Bill|GS)\s*$", "", re.sub(r"\s+", " ", title or "")).strip()


def clean_summary(text: str | None) -> str:
    """Stored summaries end with a "Keywords: ..." line used for search; it is not for readers."""
    return re.sub(r"\s*Keywords?:.*$", "", (text or "").strip(), flags=re.S | re.I).strip().replace("\u2011", "-")


def sentence_cut(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: stop + 1] if stop > 60 else cut.rsplit(" ", 1)[0] + "...").strip()


# ---------------------------------------------------------------- data

def load(index_dir: Path, bills_since: int, acts_since: int, csv_path: Path | None):
    db = sqlite3.connect(f"file:{index_dir / 'index.sqlite'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    acts = [dict(r) for r in db.execute("SELECT * FROM bills WHERE kind='Act' AND CAST(year AS INT)>=? ORDER BY date DESC, CAST(bill_no AS INT) DESC", (acts_since,))]
    links = {r["bill_key"]: dict(r) for r in db.execute("SELECT bill_key, act_key, direct FROM act_links")}
    stats = {}
    for r in db.execute("SELECT act_key, bill_key, status, COUNT(*) n FROM act_sections GROUP BY act_key, bill_key, status"):
        stats.setdefault((r["bill_key"], r["act_key"]), {})[r["status"]] = r["n"]
    meta = {r["bill_key"]: dict(r) for r in db.execute("SELECT bill_key, family, amends FROM bill_meta")}

    # bill families: a Gazette text and the presented Bill of one title are one page
    fam: dict[str, list[dict]] = {}
    for r in db.execute("SELECT * FROM bills WHERE kind IN ('Bill','GS') AND CAST(year AS INT)>=?", (bills_since,)):
        d = dict(r)
        fam.setdefault(meta.get(d["key"], {}).get("family") or d["key"], []).append(d)
    families = []
    for f, docs in fam.items():
        docs.sort(key=lambda d: (d["date"] or "", d["key"]), reverse=True)
        rep = next((d for d in docs if d.get("summary")), docs[0])
        families.append({"family": f, "docs": docs, "rep": rep, "title": plain_title(rep["title"]), "date": docs[0]["date"]})
    families.sort(key=lambda x: (x["date"] or "", x["family"]), reverse=True)

    pdf = {}
    if csv_path and csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                rf = row.get("remote_file") or ""
                if row.get("language") == "ENGLISH" and rf and rf.isascii() and row.get("status") != "failed":
                    pdf[row["bill_no"]] = rf
    return {"acts": acts, "links": links, "stats": stats, "families": families, "pdf": pdf, "meta": meta}


def pdf_url(remote_file: str) -> str:
    # The same file address documents.gov.lk's own pages use to open a document.
    return f"{DOCS}/api/content-file-proxy?file=" + quote("/" + remote_file.lstrip("/"), safe="")


# ---------------------------------------------------------------- page shell

CSS = """
:root{--lime:#F4F2EE;--ink:#1C1511;--muted:#6E6157;--line:#DAD3CA;--box:#FFFFFF;--sindoor:#A3241B;--ochre:#E0A93A;
--f-display:"Abhaya Libre","Noto Serif Sinhala",Georgia,serif;--f-body:"Yaldevi","Noto Sans Sinhala","Segoe UI",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--lime:#15100D;--ink:#F1E6D6;--muted:#B3A391;--line:#3A2D24;--box:#211913;--sindoor:#E0675A;--ochre:#E8B550;color-scheme:dark}}
:root[data-theme="dark"]{--lime:#15100D;--ink:#F1E6D6;--muted:#B3A391;--line:#3A2D24;--box:#211913;--sindoor:#E0675A;--ochre:#E8B550;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--lime);color:var(--ink);font:400 1rem/1.65 var(--f-body);min-height:100vh;display:flex;flex-direction:column}
a{color:inherit;text-decoration-color:var(--line);text-underline-offset:3px}a:hover{text-decoration-color:var(--ochre)}
a:focus-visible{outline:2px solid var(--ochre);outline-offset:3px;border-radius:4px}
.petals{height:14px;flex:none;background:radial-gradient(circle at 50% 0,var(--sindoor) 0 7px,transparent 7.5px) 0 0/20px 14px repeat-x}
header,main,footer div{width:min(100%,720px);margin-inline:auto;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
header{padding-top:22px;padding-bottom:6px;display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px 18px}
.mark{font:800 1.7rem/1 var(--f-display);text-decoration:none}.mark b{color:var(--sindoor);font-weight:800}
nav.crumbs{font-size:.88rem;color:var(--muted)}nav.crumbs a{color:var(--muted)}
main{flex:1;padding-top:18px;padding-bottom:40px}
h1{font:800 clamp(1.7rem,5vw,2.5rem)/1.15 var(--f-display);margin:.2em 0 .5em}
h2{font:700 1.3rem/1.3 var(--f-display);margin:1.7em 0 .4em}
p{margin:.6em 0}.muted{color:var(--muted)}.status{padding:12px 16px;border:1px solid var(--line);border-radius:8px;background:var(--box)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:.6em 0}dt{color:var(--muted)}dd{margin:0}
ul.plain{list-style:none;padding:0;margin:.4em 0}ul.plain li{padding:9px 0;border-bottom:1px solid var(--line)}ul.plain li:last-child{border:0}
.btn{display:inline-block;margin-top:8px;padding:10px 18px;border-radius:6px;background:color-mix(in srgb,var(--ink) 6%,var(--lime));text-decoration:none}
.btn:hover{outline:1px solid var(--line)}
.note{font-size:.88rem;color:var(--muted)}
footer{border-top:1px solid var(--line);font-size:.84rem;color:var(--muted)}footer div{padding-block:16px}
"""

FOOTER = ("Answers and summaries cite the official text but can be incomplete or wrong: check the document · Not legal advice · "
          "Not a government service · Source: documents.gov.lk")


def page(*, title, description, path, body, crumbs, ld=None, lastmod=None):
    url = f"{SITE}{path}"
    crumb_html = " / ".join(f'<a href="{e(h)}">{e(t)}</a>' if h else e(t) for t, h in crumbs)
    graph = [{"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": t, **({"item": SITE + h} if h else {})} for i, (t, h) in enumerate(crumbs)]}]
    if ld:
        graph.insert(0, ld)
    jsonld = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="canonical" href="{e(url)}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="article">
<meta property="og:site_name" content="citizenlk">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{e(url)}">
<meta property="og:image" content="{SITE}/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Abhaya+Libre:wght@700;800&family=Yaldevi:wght@400;500&display=swap">
<script type="application/ld+json">{jsonld}</script>
<style>{CSS}</style>
</head>
<body>
<div class="petals" aria-hidden="true"></div>
<header><a class="mark" href="/">citizen<b>lk</b></a><nav class="crumbs" aria-label="Breadcrumb">{crumb_html}</nav></header>
<main>
{body}
</main>
<footer><div>{e(FOOTER)}</div></footer>
</body>
</html>
"""


def cta(what: str) -> str:
    return (f'<h2>Ask a question</h2><p>Ask about {e(what)} in your own words and get an answer that cites the official text.</p>'
            f'<p><a class="btn" href="{CHAT}">Open the Bill Assistant</a></p>')


def comparison_sentence(st: dict, bill_label: str) -> str:
    """st = {status: count} for one bill compared with its Act. Wording follows billrag/acts.py: 'dropped' is about wording, not about the topic."""
    total = sum(st.get(k, 0) for k in ("same", "changed", "dropped", "moved"))
    if not total:
        return ""
    parts = []
    if st.get("same"):
        parts.append(f"{st['same']} appear in the Act in the same words")
    if st.get("changed"):
        parts.append(f"{st['changed']} appear with different wording")
    if st.get("moved"):
        parts.append(f"{st['moved']} have no matching clause but similar text appears elsewhere in the Act")
    if st.get("dropped"):
        parts.append(f"{st['dropped']} have wording that is not in the Act (the Act may still deal with the topic in other words)")
    extra = f" The Act also has {st['added']} clause{'s' if st['added'] != 1 else ''} that {'are' if st['added'] != 1 else 'is'} not in the bill." if st.get("added") else ""
    return (f"Compared clause by clause with {bill_label}, which has {total} clauses: " + "; ".join(parts) + "." + extra
            + " This is an automatic comparison of the wording: read the Act itself before relying on it.")


# ---------------------------------------------------------------- builders

def act_page(a, data, fam_by_key):
    key = a["key"]
    title = re.sub(r"\s+", " ", a["title"]).strip()
    cert = nice_date(a["date"])
    long_title = fix_case(a["long_title"] or "")
    summary = clean_summary(a.get("summary"))
    amending = "(Amendment)" in title or "Amendment" in title
    came = sorted(k for k, l in data["links"].items() if l["act_key"] == key and l["direct"])
    desc = sentence_cut(summary or (f"Act of Parliament of Sri Lanka, certified {cert}: " + long_title), 155)
    out = [f"<h1>{e(title)}</h1>"]
    status = f"This is a certified Act of Parliament, certified on {e(cert)}: the law as passed."
    if amending:
        status += " An amending Act states the changes it makes. It is not the current text of the law it amends."
    out.append(f'<p class="status">{status}</p>')
    facts = [("Act", f"No. {a['bill_no']} of {a['year']}"), ("Certified", cert)]
    if a.get("n_sections"):
        facts.append(("Sections", a["n_sections"]))
    out.append("<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in facts) + "</dl>")
    if long_title:
        out.append(f"<h2>What it is for</h2><p>An Act {e(long_title)}.</p>" if long_title[:3].lower() == "to " else f"<h2>What it is for</h2><p>{e(long_title)}</p>")
    if summary:
        out.append(f'<h2>Summary</h2><p>{e(summary)}</p><p class="note">A plain-language summary. It is not the official text: check the document.</p>')
    if came:
        seen, items = set(), []
        for bk in came:
            f = fam_by_key.get(bk)
            gid = f["family"] if f else bk  # a Gazette text and its presented Bill are one entry
            if gid in seen:
                continue
            seen.add(gid)
            label = f"Bill {f['rep']['key'] if f else bk}" + (f": {f['title']}" if f else "")
            st = next((data["stats"][(k, key)] for k in came if (fam_by_key.get(k) or {"family": k})["family"] == gid and (k, key) in data["stats"]), {})
            sent = comparison_sentence(st, "that bill")
            link = f'<a href="/bills/{slug(f["family"])}">{e(label)}</a>' if f else e(label)
            items.append(f"<li>{link}" + (f'<br><span class="note">{e(sent)}</span>' if sent else "") + "</li>")
        out.append("<h2>Where it came from</h2><p>This Act came from the bill" + ("s" if len(items) > 1 else "") + " below.</p><ul class=\"plain\">" + "".join(items) + "</ul>")
    out.append(f'<h2>The official document</h2><p>The text of the Act is published by the Government Printer on <a href="{DOCS}/web/acts" rel="noopener">documents.gov.lk</a> (Acts, {e(a["year"])}, No. {e(a["bill_no"])}).</p>')
    out.append(cta("this Act"))
    ld = {"@type": "Legislation", "name": title, "legislationIdentifier": f"No. {a['bill_no']} of {a['year']}", "legislationType": "Act",
          "legislationDate": (a["date"] or "")[:10], "datePublished": (a["date"] or "")[:10], "inLanguage": "en", "legislationJurisdiction": "LK",
          "url": f"{SITE}/acts/{slug(key)}"}
    if long_title:
        ld["description"] = long_title
    return page(title=f"{title} | Sri Lanka | citizenlk", description=desc, path=f"/acts/{slug(key)}", body="\n".join(out),
                crumbs=[("citizenlk", "/"), ("Acts", "/acts/"), (f"No. {a['bill_no']} of {a['year']}", None)], ld=ld)


def bill_page(f, data, acts_by_key, pdf):
    rep, docs = f["rep"], f["docs"]
    title = f["title"]
    year = rep["year"]
    summary = clean_summary(rep.get("summary"))
    long_title = fix_case(rep.get("long_title") or "")
    link = next((data["links"][d["key"]] for d in docs if d["key"] in data["links"] and data["links"][d["key"]]["direct"]), None) \
        or next((data["links"][d["key"]] for d in docs if d["key"] in data["links"]), None)
    act = acts_by_key.get(link["act_key"]) if link else None
    out = [f"<h1>{e(title)} Bill, {e(year)}</h1>"]
    if act and link["direct"]:
        status = (f'This bill became <a href="/acts/{slug(act["key"])}">{e(re.sub(r"\s+", " ", act["title"]).strip())}</a>, certified {e(nice_date(act["date"]))}. '
                  "Rely on the Act for what became law: the text can change on the way.")
    elif act:
        status = (f'This is an earlier round of a proposal, not itself law. A later bill of similar text became <a href="/acts/{slug(act["key"])}">'
                  f'{e(re.sub(r"\s+", " ", act["title"]).strip())}</a>, certified {e(nice_date(act["date"]))}.')
    else:
        last = max((a["date"] for a in acts_by_key.values()), default="")
        status = ("This is a proposal for a law, not itself law. "
                  + (f"We hold no certified Act for it (Acts checked up to {e(nice_date(last))}), so it has not been shown to have passed. That does not mean it failed. "
                     if last else "") + "Check the latest position with Parliament.")
    out.append(f'<p class="status">{status}</p>')
    facts = [("Bill numbers" if len(docs) > 1 else "Bill number", ", ".join(d["key"] for d in sorted(docs, key=lambda d: d["key"])))]
    dates = [nice_date(x) for x in sorted({d["date"][:10] for d in docs if d["date"]})]
    if dates:
        facts.append(("Published", ", ".join(dates)))
    if rep.get("presented_by"):
        facts.append(("Presented by", fix_case(rep["presented_by"])[:1].upper() + fix_case(rep["presented_by"])[1:]))
    if rep.get("n_sections"):
        facts.append(("Clauses", rep["n_sections"]))
    out.append("<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in facts) + "</dl>")
    if long_title:
        out.append(f"<h2>What it is for</h2><p>A Bill {e(long_title)}.</p>" if long_title[:3].lower() == "to " else f"<h2>What it is for</h2><p>{e(long_title)}</p>")
    if summary:
        out.append(f'<h2>Summary</h2><p>{e(summary)}</p><p class="note">A plain-language summary. It is not the official text: check the document.</p>')
    if act and link["direct"]:
        sent = comparison_sentence(data["stats"].get((rep["key"], act["key"])) or next(
            (data["stats"][(d["key"], act["key"])] for d in docs if (d["key"], act["key"]) in data["stats"]), {}), "the Act")
        if sent:
            out.append(f"<h2>What changed on the way</h2><p>{e(sent)}</p>")
    items = []
    for d in docs:
        label = f"{KIND_LABEL.get(d['kind'], 'Document')}, {nice_date(d['date'])}"
        rf = pdf.get(d["key"])
        items.append(f'<li><a href="{e(pdf_url(rf))}" rel="noopener">{e(label)} (PDF)</a> <span class="muted">· {e(d["key"])}</span></li>' if rf
                     else f'<li>{e(label)} <span class="muted">· {e(d["key"])}</span></li>')
    out.append(f'<h2>The official documents</h2><p>Published by the Government Printer on <a href="{DOCS}/web/bills" rel="noopener">documents.gov.lk</a>.</p><ul class="plain">'
               + "".join(items) + "</ul>")
    out.append(cta("this bill"))
    desc = sentence_cut(summary or (f"{title} Bill, {year}, Sri Lanka: " + long_title), 155)
    newest = (docs[0]["date"] or "")[:10]
    ld = {"@type": "Legislation", "name": f"{title} Bill, {year}", "legislationIdentifier": rep["key"], "legislationType": "Bill",
          "datePublished": newest, "inLanguage": "en", "legislationJurisdiction": "LK", "url": f"{SITE}/bills/{slug(f['family'])}"}
    return page(title=f"{title} Bill, {year} | Sri Lanka bill, status and summary | citizenlk", description=desc, path=f"/bills/{slug(f['family'])}",
                body="\n".join(out), crumbs=[("citizenlk", "/"), ("Bills", "/bills/"), (f["family"], None)], ld=ld)


def hub(kind, items, intro, path, desc):
    """items: [(href, label, sub)] newest first."""
    lis = "".join(f'<li><a href="{e(h)}">{e(l)}</a><br><span class="muted">{e(s)}</span></li>' for h, l, s in items)
    body = f'<h1>{e(kind)}</h1><p>{e(intro)}</p><ul class="plain">{lis}</ul>' + cta(kind.lower().split(" of ")[0])
    return page(title=f"{kind} | citizenlk", description=desc, path=path, body=body, crumbs=[("citizenlk", "/"), (kind.split(" of ")[0], None)])


# ---------------------------------------------------------------- main

def build(index_dir: Path, out: Path, bills_since: int, acts_since: int, csv_path: Path | None = None) -> dict:
    data = load(index_dir, bills_since, acts_since, csv_path or index_dir / "bills.csv")
    acts_by_key = {a["key"]: a for a in data["acts"]}
    fam_by_key = {d["key"]: f for f in data["families"] for d in f["docs"]}
    for sub in ("acts", "bills"):
        shutil.rmtree(out / sub, ignore_errors=True)
        (out / sub).mkdir(parents=True)
    urls = []  # (path, lastmod)

    for a in data["acts"]:
        (out / "acts" / f"{slug(a['key'])}.html").write_text(act_page(a, data, fam_by_key), encoding="utf-8")
        urls.append((f"/acts/{slug(a['key'])}", (a["date"] or "")[:10]))
    for f in data["families"]:
        (out / "bills" / f"{slug(f['family'])}.html").write_text(bill_page(f, data, acts_by_key, data["pdf"]), encoding="utf-8")
        urls.append((f"/bills/{slug(f['family'])}", (f["date"] or "")[:10]))

    first, last = (min(a["year"] for a in data["acts"]), max(a["year"] for a in data["acts"])) if data["acts"] else ("", "")
    (out / "acts" / "index.html").write_text(hub(
        f"Acts of Parliament, {first} to {last}",
        [(f"/acts/{slug(a['key'])}", re.sub(r"\s+", " ", a["title"]).strip(), f"Certified {nice_date(a['date'])}") for a in data["acts"]],
        "Acts certified by Parliament, with a plain-language summary and where each one came from.", "/acts/",
        "Acts of Parliament of Sri Lanka certified from " + str(first) + ", with plain-language summaries and links to the official text."), encoding="utf-8")
    (out / "bills" / "index.html").write_text(hub(
        f"Bills in Parliament, {bills_since} onwards",
        [(f"/bills/{slug(f['family'])}", f["title"], f"Bill {f['rep']['key']}, published {nice_date(f['date'])}") for f in data["families"]],
        "Bills published for Parliament, with a plain-language summary, whether each became an Act, and links to the official text.", "/bills/",
        f"Bills before Sri Lanka's Parliament from {bills_since}, with summaries, status and links to the official text."), encoding="utf-8")
    urls += [("/acts/", ""), ("/bills/", "")]

    today = date.today().isoformat()
    rows = "".join(f"  <url><loc>{SITE}{p}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm and lm <= today else "") + "</url>\n" for p, lm in urls)
    (out / "sitemap-docs.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + rows + "</urlset>\n", encoding="utf-8")
    return {"acts": len(data["acts"]), "bills": len(data["families"]), "urls": len(urls), "pdf_links": sum(1 for f in data["families"] for d in f["docs"] if d["key"] in data["pdf"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", type=Path, required=True, help="directory with index.sqlite (and bills.csv for PDF links)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "site")
    ap.add_argument("--bills-since", type=int, default=2026)
    ap.add_argument("--acts-since", type=int, default=2024)
    a = ap.parse_args()
    print(json.dumps(build(a.index, a.out, a.bills_since, a.acts_since)))


if __name__ == "__main__":
    main()
