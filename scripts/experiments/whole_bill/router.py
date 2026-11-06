import os,sys,collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
from billrag.config import load_settings
from billrag.store import Store
from billrag.retrieve import Retriever
from billrag.llm import make_embedder, make_reranker
from billrag import answer_eval as ae, meta
from billrag.evaluate import load_items
s=load_settings(); st=Store(s.db_path)
r=Retriever(st,make_embedder(s),s.vectors_path,None,s.rerank,make_reranker(s))
items=[ae.expand_gold(st,i) for i in load_items('evals/retrieval_eval.jsonl') if i['source']['year']>=2025]
def fam(k):
    m=meta.meta(st,k); return m['family'] if m else k
top1=top2=top3=0; miss=[]
for it in items:
    gold={fam(b) for b,_ in ae._gold_pairs(it)}|{fam(it['source']['bill'])}
    q=it['question']
    res=r.search(q,k=14,kinds=("section","schedule"))+r.search(q,k=4,kinds=("overview",))
    score=collections.defaultdict(float)
    for rank,c in enumerate(res): score[fam(c['bill_key'])]+=1/(rank+3)       # reciprocal-rank vote per bill family
    order=[f for f,_ in sorted(score.items(),key=lambda x:-x[1])]
    h=[bool({f} & gold) for f in order[:3]]
    top1+=h[0] if h else 0; top2+=any(h[:2]); top3+=any(h[:3])
    if not (h and h[0]): miss.append((it['id'],it['source']['bill'],order[:3],q[:70]))
n=len(items); print(f"bill-family routing on {n} recent items: top1 {top1/n:.0%} ({top1}/{n}), top2 {top2/n:.0%}, top3 {top3/n:.0%}")
for m in miss: print("  MISS",m)
