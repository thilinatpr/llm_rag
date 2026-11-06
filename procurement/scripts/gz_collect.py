"""Collect procurement-related extra-gazettes (metadata -> raw/gazettes/index.json) and download English PDFs."""
import json, sys, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from gz_search import page, BASE, UA

QUERIES = ["Procurement", "National Procurement Commission", "Procurement Manual", "Procurement Guidelines",
           "Public Procurement", "Bidding", "Tender", "Public Finance Management", "National Procurement Agency"]
out = Path(__file__).resolve().parent.parent / "raw" / "gazettes"
found = {}
for q in QUERIES:
    p = 1
    while True:
        r = page(q, p)
        for g in r["data"]:
            found[g["gazetteNoText"]] = g
        if p >= int(r["pagination"]["totalPages"]) or p >= 5: break
        p += 1
    print(f"{q!r}: total {r['pagination']['total']}, unique so far {len(found)}", file=sys.stderr)
rows = sorted(found.values(), key=lambda g: g["date"], reverse=True)
(out / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
for g in rows:
    print(g["date"][:10], g["gazetteNoText"], (g.get("descriptionEnglish") or "")[:100])
