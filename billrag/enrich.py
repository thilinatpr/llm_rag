"""Optional index enrichment: dense vectors (incremental) and LLM bill summaries (parallel, resumable)."""
import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor


def _chunk_hash(r):
    return hashlib.sha1(f"{r['id']}\x00{r['ctx']}".encode()).hexdigest()[:16]


def embed_all(store, embedder, path, batch=256, incremental=True, dtype="float32"):
    """Embed every chunk, reusing stored vectors for chunks whose (id, text) hash is unchanged.

    dtype="float16" stores the (L2-normalised) vectors at half the size/RAM with negligible ranking change."""
    import numpy as np
    rows = store.all_chunks()
    hashes = [_chunk_hash(r) for r in rows]
    model = str(getattr(embedder, "model", ""))
    fp = store.fingerprint()
    reuse = {}
    if incremental and path.exists():
        z = np.load(path)
        same_model = "model" not in z.files or str(z["model"]) == model
        if "chunk_hashes" in z.files:
            old_h = [str(h) for h in z["chunk_hashes"]]
        elif "fingerprint" in z.files and str(z["fingerprint"]) == fp:
            old_h = hashes  # legacy file built from exactly this index
        else:
            old_h = None
        if old_h is not None and same_model:
            reuse = {(int(i), h): v.astype("float32") for i, h, v in zip(z["ids"], old_h, z["vectors"])}
    todo = [n for n, (r, h) in enumerate(zip(rows, hashes)) if (r["id"], h) not in reuse]
    print(f"  {len(rows) - len(todo)} vectors reused, {len(todo)} to embed", file=sys.stderr)
    new = {}
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = np.asarray(embedder.embed([rows[n]["ctx"] for n in part]), dtype="float32")
        new.update(zip(part, vecs))
        print(f"  embedded {min(i + batch, len(todo))}/{len(todo)}", file=sys.stderr)
    dim = len(next(iter(new.values()))) if new else len(next(iter(reuse.values())))
    m = np.vstack([new[n] if n in new else reuse[(rows[n]["id"], hashes[n])] for n in range(len(rows))]) if rows \
        else np.zeros((0, dim), dtype="float32")
    m = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    np.savez_compressed(path, ids=np.asarray([r["id"] for r in rows]), vectors=m.astype(dtype),
                        fingerprint=np.asarray(fp), chunk_hashes=np.asarray(hashes), model=np.asarray(model))
    return len(todo)


SUM_PROMPT = ("Summarise this Sri Lankan {doc} for a non-lawyer in at most 110 words: its purpose, who is affected, key "
              "new powers/obligations/penalties, and which existing laws it amends or creates. Then, on a final line "
              "starting 'Keywords:', give 8-12 plain-language search terms a citizen might use to look for it "
              "(topics, institutions, everyday words - not legal jargon). Use ONLY facts stated in the text below: do not add "
              "background knowledge, resolution numbers, dates or names that the text does not contain. "
              "Plain text, no markdown, no preamble.\n\n"
              "TITLE: {title}\nLONG TITLE: {long_title}\n\nTEXT (truncated):\n{text}")


def _summarise(llm, b, body):
    prompt = SUM_PROMPT.format(doc="Act" if b.get("kind") == "Act" else "bill", title=b["title"], long_title=b["long_title"] or "n/a", text=body[:9000])
    for _ in range(2):
        s = llm.chat("You are a careful legislative analyst.", [{"role": "user", "content": prompt}],
                     max_tokens=1500).text.strip()
        if len(s) > 80 and "Keywords:" in s:
            return s
    return s if len(s) > 80 else None


def summarize_all(store, llm, since_year=0, limit=None, overwrite=False, workers=8):
    from .ingest import overview_text
    todo = []
    for b in store.find_bills(limit=100000):
        if (since_year and b["year"] < since_year) or (b["summary"] and not overwrite):
            continue
        body = "\n".join(r["text"] for r in store.db.execute(
            "SELECT text FROM chunks WHERE bill_key=? AND kind IN ('section','schedule') ORDER BY ord", (b["key"],)))
        todo.append((b, body))
        if limit and len(todo) >= limit:
            break
    done = failed = 0
    with ThreadPoolExecutor(workers) as ex:
        for (b, _), s in zip(todo, ex.map(lambda t: _safe(_summarise, llm, t[0], t[1]), todo)):
            if not s:
                failed += 1
                print(f"  ! no usable summary for {b['key']}", file=sys.stderr)
                continue
            store.set_summary(b["key"], s)
            outline = [f"{'\u00a7' + str(o['section_no']) if o['section_no'] is not None else 'Schedule'} "
                       f"{o['heading'] or (o['head'] or '')[:60]}" for o in store.outline(b["key"])]
            store.update_overview(b["key"], overview_text(store.bill(b["key"]), outline, s))
            store.commit()
            done += 1
            if done % 50 == 0:
                print(f"  summarised {done}/{len(todo)}", file=sys.stderr)
    print(f"summarised {done}, failed {failed}", file=sys.stderr)
    return done


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # one bad call must not abort a 1.5k-call job; it is retried on the next run
        print(f"  ! {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return None
