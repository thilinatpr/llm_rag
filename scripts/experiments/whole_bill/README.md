# Whole-bill (cached corpus) experiments, 2026-11-06

Question: can the whole text of a bill be the stable *prefix* of the answer prompt so DeepSeek's context cache makes follow-up
questions about that bill cheap and complete? See the GitHub issue and `TODO.md` ("Open: bill-wise cached corpus").

Run from the repo root with the DeepSeek settings (`BILLRAG_CHAT_*` = deepseek-flash, `BILLRAG_GRADE_EFFORT=off`; the eval scripts also
need `BILLRAG_JUDGE_*`, `CLOUDFLARE_*` for embeddings/rerank, and `data/bills/index.sqlite` from `scripts/fetch_index.py`):

| script | what it does |
|---|---|
| `wholebill.py` | shared: `whole_bill(key)` = Evidence for EVERY section in order; `Engine(key)` answers from it; the prompt is reordered so the question is LAST |
| `cache_exp.py` | 6 questions on 078/2026, prints cache-hit share and USD per question (set `QUESTION_LAST=0` for the current order) |
| `wb_eval.py` | 72 recent eval items, ORACLE bill (the gold bill is given), single worker (the SQLite handle is not thread-safe) |
| `hybrid_eval.py` | 72 items, retrieve+grade picks the bill, whole bill answers |
| `router.py` | bill-family routing accuracy of retrieval alone (RRF vote over the top hits) |
