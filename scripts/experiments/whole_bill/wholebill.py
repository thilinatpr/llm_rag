import os,sys,time,json,os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
from billrag.config import load_settings
from billrag.store import Store
from billrag.llm import make_llm
from billrag.pipeline import Pipeline, Evidence, Result
from billrag import calltrace
s=load_settings(); st=Store(s.db_path); llm=make_llm(s)
P=Pipeline(st,None,llm,grade_effort=s.grade_effort,answer_effort=s.answer_effort)
_orig=P._llm
def _llm(system,user,*a,**k):
    # cache-friendly order: the bill (stable) first, the question (changes) LAST
    if os.environ.get("QUESTION_LAST","1")=="1" and user.startswith("QUESTION: ") and "\n\nEVIDENCE:\n\n" in user:
        q,rest=user.split("\n\nEVIDENCE:\n\n",1); user="EVIDENCE:\n\n"+rest+"\n\n"+q
    return _orig(system,user,*a,**k)
P._llm=_llm
def whole_bill(key, max_chars=None):
    """Evidence for EVERY section of the bill in a fixed order (stable prefix => cacheable)."""
    ev=[]
    for r in st.db.execute("select min(id) id, section_no from chunks where bill_key=? and kind='section' and section_no is not null group by section_no order by section_no",(key,)):
        ch=st.chunks_by_ids([r['id']])[r['id']]; t=st.section_text(key,r['section_no'])['text']
        ev.append(Evidence({**ch,'text':t},3))
    return ev
class Engine:
    def __init__(self,key): self.key=key
    def ask(self,q):
        ev=whole_bill(self.key); res=Result(question=q,answer=""); res.evidence=res.graded=ev
        with calltrace.tracing() as tr:
            res.answer=P.summarise(q,ev,True,"",res)
        res.llm_calls=1; res.trace=tr.calls; return res
