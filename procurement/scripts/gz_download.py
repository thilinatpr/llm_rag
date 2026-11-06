"""Download gazette PDFs listed in raw/gazettes/index.json for the given gazette numbers (default: English only)."""
import json, sys, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from gz_search import BASE, UA
root = Path(__file__).resolve().parent.parent / "raw" / "gazettes"
want = sys.argv[1].split(",")
langs = (sys.argv[2] if len(sys.argv) > 2 else "E").split(",")
for g in json.loads((root / "index.json").read_text()):
    if g["gazetteNoText"] not in want: continue
    for c in g["contents"]:
        lang = c["uploadedFile"].rsplit("_", 2)[-2] if c["uploadedFile"].count("_") >= 2 else "?"
        if lang not in langs: continue
        dest = root / f"{g['gazetteNoText'].replace('/', '-')}_{lang}.pdf"
        if dest.exists() and dest.stat().st_size: print("skip", dest.name); continue
        url = f"{BASE}/api/content-file-proxy?" + urllib.parse.urlencode({"file": "/" + c["uploadedFile"]})
        data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=180).read()
        ok = data.startswith(b"%PDF")
        if ok: dest.write_bytes(data)
        print(dest.name, len(data), "ok" if ok else "NOT A PDF")
