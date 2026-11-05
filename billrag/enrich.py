"""Optional index enrichment: dense vectors and LLM bill summaries (both feed retrieval)."""
import sys


def embed_all(store, embedder, path, batch=256):
    import numpy as np
    rows = store.all_chunks()
    vecs = []
    for i in range(0, len(rows), batch):
        vecs.append(np.asarray(embedder.embed([r["ctx"] for r in rows[i:i + batch]]), dtype="float32"))
        print(f"  embedded {min(i + batch, len(rows))}/{len(rows)}", file=sys.stderr)
    m = np.vstack(vecs)
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    np.savez_compressed(path, ids=np.asarray([r["id"] for r in rows]), vectors=m, fingerprint=np.asarray(store.fingerprint()))


SUM_PROMPT = ("Summarise this Sri Lankan bill for a non-lawyer in at most 120 words: purpose, who is affected, key new "
              "powers/obligations/penalties, and which existing laws it amends. Plain text, no preamble.\n\n"
              "TITLE: {title}\nLONG TITLE: {long_title}\n\nTEXT (truncated):\n{text}")


def summarize_all(store, llm, since_year=0, limit=None, overwrite=False):
    n = 0
    for b in store.find_bills(limit=100000):
        if (since_year and b["year"] < since_year) or (b["summary"] and not overwrite):
            continue
        body = "\n".join(r["text"] for r in store.db.execute(
            "SELECT text FROM chunks WHERE bill_key=? AND kind IN ('section','schedule') ORDER BY ord", (b["key"],)))
        s = llm.chat("You are a careful legislative analyst.",
                     [{"role": "user", "content": SUM_PROMPT.format(title=b["title"], long_title=b["long_title"],
                                                                      text=body[:14000])}], max_tokens=400).text.strip()
        store.set_summary(b["key"], s)
        ov = store.db.execute("SELECT text FROM chunks WHERE bill_key=? AND kind='overview'", (b["key"],)).fetchone()
        if ov:
            store.update_overview(b["key"], (f"Summary: {s}\n" + ov["text"])[:3600])
        store.commit()
        n += 1
        print(f"  summarised {b['key']} ({n})", file=sys.stderr)
        if limit and n >= limit:
            break
    return n
