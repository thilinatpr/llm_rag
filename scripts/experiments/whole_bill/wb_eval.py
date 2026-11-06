import os
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wholebill.py')).read())
import json
from billrag import answer_eval as ae
from billrag.evaluate import load_items
items=[ae.expand_gold(st,i) for i in load_items('evals/retrieval_eval.jsonl') if i['source']['year']>=2025]
def size(k): return st.db.execute("select sum(length(text)) from chunks where bill_key=? and kind='section'",(k,)).fetchone()[0] or 0
big=[i for i in items if size(i['source']['bill'])>400_000]; items=[i for i in items if size(i['source']['bill'])<=400_000]
print("skipped (bill > 400k chars):",[i['source']['bill'] for i in big],"| evaluating",len(items))
import dataclasses
e=os.environ
judge=make_llm(dataclasses.replace(s,chat_model=e["BILLRAG_JUDGE_MODEL"],chat_provider="openai",chat_base_url=e["BILLRAG_JUDGE_BASE_URL"],chat_api_key=e["BILLRAG_JUDGE_API_KEY"]))
from concurrent.futures import ThreadPoolExecutor
def one(it):
    r=ae.run_one(it,lambda: Engine(it['source']['bill']),judge)
    return r
with ThreadPoolExecutor(1) as ex: rows=list(ex.map(one,items))
rows_ok=[r for r in rows]
summ=ae.summarize(rows); print(ae.format_report(summ,"whole-bill (oracle bill), question last"))
json.dump(rows,open('evals/answer_model_comparison/72.deepseek-flash-wholebill-oracle.jsonl','w'),ensure_ascii=False)
print("warned:",sum(bool(r.get('warnings')) for r in rows),"/",len(rows),"errors:",sum(1 for r in rows if r.get('error')))
