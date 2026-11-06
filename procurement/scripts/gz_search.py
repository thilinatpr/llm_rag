"""Search documents.gov.lk extra-gazettes through the site's generic TableDataAction (same action id as scrape_bills.py)."""
import json, sys, urllib.request
BASE = "https://documents.gov.lk"
ACTION = "40dd03742cae2465a031936880def365fdd014c22b"
EP = "http://gvp-api:4500/website-data/extra-gazette/get-all"
UA = "Mozilla/5.0 (compatible; llm-rag-procurement/1.0)"

def page(q, p=1, limit=100, ep=EP, path="/web/extra_gazettes"):
    body = json.dumps([{"apiEndpoint": ep, "page": p, "limit": limit, "q": q, "search": q, "forwarded": {}}]).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers={
        "Next-Action": ACTION, "Accept": "text/x-component",
        "Content-Type": "text/plain;charset=UTF-8", "User-Agent": UA})
    text = urllib.request.urlopen(req, timeout=90).read().decode()
    for line in text.split("\n"):
        if line.startswith("1:"):
            return json.loads(line[2:])
    raise RuntimeError(text[:300])

if __name__ == "__main__":
    q = sys.argv[1]
    r = page(q)
    print(r["pagination"])
    for g in r["data"][:int(sys.argv[2]) if len(sys.argv) > 2 else 20]:
        print(g["date"][:10], g["gazetteNoText"], (g.get("descriptionEnglish") or "")[:110],
              [c["uploadedFile"] for c in g.get("contents", [])])
