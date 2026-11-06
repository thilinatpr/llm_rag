import os
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wholebill.py')).read())
import json, dataclasses
from billrag import answer_eval as ae
from billrag.evaluate import load_items
from billrag.retrieve import Retriever
from billrag.llm import make_embedder, make_reranker, make_answer_llm
r=Retriever(st,make_embedder(s),s.vectors_path,None,s.rerank,make_reranker(s))
def make():
    p=Pipeline(st,r,llm,grade_effort=s.grade_effort,answer_effort=s.answer_effort,answer_llm=make_answer_llm(s))
    _llm=p._llm
    def q_last(system,user,*a,**k):
        if user.startswith("QUESTION: ") and "\n\nEVIDENCE:\n\n" in user:
            q,rest=user.split("\n\nEVIDENCE:\n\n",1); user="EVIDENCE:\n\n"+rest+"\n\n"+q
        return _llm(system,user,*a,**k)
    orig=p.summarise
    def summarise(question,evidence,enough,missing,res,**k):
        if evidence:
            p._llm=q_last
            try: return orig(question,whole_bill(evidence[0].key),enough,missing,res,**k)
            finally: p._llm=_llm
        return orig(question,evidence,enough,missing,res,**k)
    p.summarise=summarise
    return p
items=[ae.expand_gold(st,i) for i in load_items('evals/retrieval_eval.jsonl') if i['source']['year']>=2025]
e=os.environ
judge=make_llm(dataclasses.replace(s,chat_model=e["BILLRAG_JUDGE_MODEL"],chat_provider="openai",chat_base_url=e["BILLRAG_JUDGE_BASE_URL"],chat_api_key=e["BILLRAG_JUDGE_API_KEY"]))
rows=ae.run(items,make,judge,workers=1)
print(ae.format_report(ae.summarize(rows),"hybrid: retrieve+grade picks the bill, whole bill answers (question last)"))
print("warned:",sum(bool(x.get('warnings')) for x in rows),"/",len(rows),"errors:",sum(1 for x in rows if x.get('error')))
json.dump(rows,open('evals/answer_model_comparison/72.deepseek-flash-hybrid-wholebill.jsonl','w'),ensure_ascii=False)
print("DONE")
