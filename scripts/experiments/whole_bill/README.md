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
| `followup_eval.py` | 3-turn chats (title opener, eval question = scored turn 2, generic turn 3); `WHOLE_BILL=0/1` compares the normal pipeline with `Pipeline(whole_bill=True)` |

## Result: bill-focused follow-ups (2026-11-06, deepseek-flash, judge deepseek-v4-pro, 72 recent items, one worker each)

| turn 2 (scored) | strict | cited gold | correct | warned | LLM calls | turn-2 s | turn-2 USD | turn-3 USD (cache hit) |
|---|---|---|---|---|---|---|---|---|
| normal pipeline | 0.90 | 0.94 | 0.92 | 0.01 | 3.17 | 10.0 | 0.00073 | 0.00118 (8%) |
| whole_bill on | 0.97 | 1.00 | 0.97 | 0.01 | 2.29 | 7.6 | 0.00089 | 0.00059 (66%) |

With the mode on: turn 1 left the chat focused on one bill in 59/72; turn 2 used the whole bill in 54 (fallbacks: 4 retrieval pointed
elsewhere, 2 bill too long), turn 3 in 65. Caveats: the opener names the gold bill's title (favourable: real chats may not settle on
one bill this cleanly); both runs ran at the same time on one DeepSeek account, so identical prompts (turn 1 is the same in both) could
hit each other's cache: the normal run's 61-65% turn-1/2 hit rate is probably that, so its USD is understated; the judge is
DeepSeek-family; n=72, so treat gaps under ~0.05 as noise. Rows: `evals/answer_model_comparison/72.followup-{normal,wholebill}.jsonl`.
