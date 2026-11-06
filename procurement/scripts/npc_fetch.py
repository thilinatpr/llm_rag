"""List (and optionally download) the documents published on nprocom.gov.lk (National Procurement Commission).

The site is WordPress; documents are shown with the "3D FlipBook" plugin, whose settings are base64 JSON in
`FB3D_CLIENT_DATA.push('...')` calls (title + PDF url + page count). The media REST API hides most of them, and the
pages contain no plain <a href=...pdf> links, so this decodes the flipbook data.

    python procurement/scripts/npc_fetch.py                       # list -> data/procurement/npc/manifest.json
    python procurement/scripts/npc_fetch.py --download --lang English   # also download (skips existing files)

robots.txt allows everything except /wp-admin/. One request per second.
"""
import argparse
import base64
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

SITE = "https://nprocom.gov.lk"
SECTIONS = ["goods-services", "consultancy", "npc-circulars", "other-circulars", "guidance-note", "faq",
            "manuals-on-goods-services", "supplements-on-goods-services", "manuals-consultancy", "supplements-consultancy",
            "sector-specific-manuals", "sbd-goods", "sbd-it", "sbd-service", "procurement-notices", "act"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; procurement-rules-research)"}
OUT = Path("data/procurement/npc")


def get(url):
    url = urllib.parse.quote(url, safe=":/%?=&~+")  # some file names are Sinhala
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


def flipbooks(page_html):
    """[(tab label, flipbook id, title, pdf url, pages)] in page order. The tab is the language/type tab it sits in."""
    tabs = {m.group(1): m.group(2).strip() for m in re.finditer(
        r'id="e-n-tab-title-(\d+)"[^>]*>\s*<span class="e-n-tab-title-text">\s*([^<]+?)\s*</span>', page_html)}
    panels = [(m.start(), tabs.get(m.group(1), "")) for m in re.finditer(r'id="e-n-tab-content-(\d+)"', page_html)]
    out, seen = [], set()
    for m in re.finditer(r"FB3D_CLIENT_DATA\.push\('([A-Za-z0-9+/=]+)'\)", page_html):
        try:
            data = json.loads(base64.b64decode(m.group(1)))
        except ValueError:
            continue
        for pid, post in (data.get("posts") or {}).items():
            if pid in seen:
                continue
            seen.add(pid)
            box = page_html.find(f'data-id="{pid}"')
            tab = next((label for pos, label in reversed(panels) if 0 <= pos < box), "")
            d = post.get("data") or {}
            out.append((tab, pid, post.get("title", ""), (d.get("guid") or "").replace("\\/", "/"), d.get("pdf_pages")))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--lang", help="only this tab when downloading (e.g. English); documents without a tab are included")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in SECTIONS:
        html = get(f"{SITE}/{s}/").decode("utf-8", "replace")
        for tab, pid, title, url, pages in flipbooks(html):
            rows.append(dict(section=s, tab=tab, id=pid, title=title, url=url, pages=pages,
                             file=url.rsplit("/", 1)[-1] if url else None))
        time.sleep(1)
    (a.out / "manifest.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rows)} documents -> {a.out / 'manifest.json'}")
    if a.download:
        for r in rows:
            if not r["url"] or (a.lang and r["tab"] and r["tab"] != a.lang):
                continue
            dest = a.out / r["section"] / r["file"]
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(get(r["url"]))
            print("  downloaded", dest)
            time.sleep(1)


if __name__ == "__main__":
    main()
