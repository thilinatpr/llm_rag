"""Hybrid retrieval: BM25 (+ optional dense) -> reciprocal-rank fusion -> optional LLM rerank -> diversity cap."""
import json
import re
from collections import defaultdict

RRF_K = 60


class VectorsError(RuntimeError):
    """Dense retrieval is configured but the vectors are missing or do not match the current index."""


def rrf(rankings, k=RRF_K):
    """Reciprocal rank fusion of several ranked id lists -> ids ordered by fused score."""
    score = defaultdict(float)
    for ranked in rankings:
        for r, i in enumerate(ranked):
            score[i] += 1.0 / (k + r + 1)
    return sorted(score, key=score.get, reverse=True)


class Retriever:
    def __init__(self, store, embedder=None, vectors_path=None, llm=None, rerank="none", reranker=None):
        self.store, self.embedder, self.llm, self.rerank_mode, self.reranker = store, embedder, llm, rerank, reranker
        self.ids = self.mat = None
        if embedder is not None and vectors_path is not None:
            self._load_vectors(vectors_path)

    def _load_vectors(self, path):
        import numpy as np
        hint = "Run `python -m billrag embed`, or set BILLRAG_EMBED_PROVIDER=none to use keyword search only."
        if not path.exists():
            raise VectorsError(f"Dense retrieval is enabled but {path} does not exist. {hint}")
        z = np.load(path)
        if "fingerprint" not in z.files or str(z["fingerprint"]) != self.store.fingerprint():
            raise VectorsError(f"{path} was not built from the current index (stale or unstamped). {hint}")
        self.ids, self.mat = z["ids"], z["vectors"]

    def dense(self, query, limit, year=None, keys=None):
        if self.mat is None:
            return []
        import numpy as np
        q = np.asarray(self.embedder.embed([query]), dtype="float32")[0]
        q /= (np.linalg.norm(q) or 1.0)
        sims = self.mat @ q
        order = np.argsort(-sims)[: limit * 4 if (year or keys) else limit]
        ids = [int(self.ids[i]) for i in order]
        if year or keys:
            ch = self.store.chunks_by_ids(ids)
            ids = [i for i in ids if i in ch and (not year or int(ch[i]["bill_key"][-4:]) == int(year))
                   and (not keys or ch[i]["bill_key"] in keys)][:limit]
        return ids

    def search(self, query, k=8, year=None, keys=None, pool=40):
        lists = [self.store.bm25(query, pool, year, keys)]
        d = self.dense(query, pool, year, keys)
        if d:
            lists.append(d)
        first_stage = rrf(lists)
        fused = first_stage[: max(pool * 3 // 4 if self.reranker else pool // 2, k)]
        chunks = self.store.chunks_by_ids(fused)
        fused = [i for i in fused if i in chunks]
        if self.rerank_mode == "llm" and self.llm and len(fused) > 1:
            fused = self.llm_rerank(query, fused, chunks)
        elif self.rerank_mode.startswith("cloudflare") and self.reranker and len(fused) > 1:
            fused = self.cross_rerank(query, fused, chunks)
        # collapse identical text across versions (e.g. GS vs Bill) and remember where else it appears
        seen, uniq = {}, []
        for i in fused:
            c = chunks[i]
            sig = re.sub(r"\W+", "", c["text"].lower())
            if sig in seen:
                seen[sig].setdefault("also_in", []).append(c["bill_key"])
            else:
                seen[sig] = c
                uniq.append(i)
        fused = uniq
        # diversity: avoid one long bill flooding the results (unless the caller pinned bills)
        cap = k if keys and len(keys) == 1 else max(3, k // 2)
        per, out = defaultdict(int), []
        for i in fused:
            b = chunks[i]["bill_key"]
            if per[b] < cap:
                per[b] += 1
                out.append(chunks[i])
            if len(out) >= k:
                break
        return out

    def cross_rerank(self, query, ids, chunks):
        """Reorder by cross-encoder score (stable on ties). 'cloudflare-fused' RRF-merges that order with the
        first-stage order, which hedges against a reranker whose scores saturate near 1.0."""
        try:
            sc = self.reranker.scores(query, [chunks[i]["ctx"] for i in ids])
        except Exception:
            return ids  # reranking is best-effort
        by_score = [ids[n] for n in sorted(range(len(ids)), key=lambda n: (-sc[n], n))]
        return rrf([by_score, ids]) if self.rerank_mode == "cloudflare-fused" else by_score

    def llm_rerank(self, query, ids, chunks):
        cands = "\n".join(f"[{n}] {chunks[i]['text'][:450].replace(chr(10), ' ')}" for n, i in enumerate(ids))
        prompt = (f"Question: {query}\n\nScore each passage 0-10 for how directly it helps answer the question. "
                  f"Reply with ONLY a JSON list of numbers, one per passage, in order.\n\n{cands}")
        try:
            txt = self.llm.chat("You are a precise relevance judge.", [{"role": "user", "content": prompt}],
                                max_tokens=400).text
            scores = json.loads(re.search(r"\[.*\]", txt, re.S).group(0))
            if len(scores) != len(ids):
                return ids
            ranked = sorted(range(len(ids)), key=lambda n: (-float(scores[n]), n))
            return [ids[n] for n in ranked]
        except Exception:
            return ids  # reranking is best-effort; keep the fused order
