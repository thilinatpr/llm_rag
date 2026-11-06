import os
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wholebill.py')).read())
Qs=["What is the penalty for hostage taking?","How is 'hostage taking' defined?","Who can be held liable for attempting or conspiring to commit an offence under this Bill?","What does the Bill say about the Sinhala and Tamil texts if they conflict?","What is the maximum fine for the offence of terrorism causing death?","Which section deals with the penalty for offences where the sentence of death is prescribed in another enactment?"]
e=Engine("078/2026"); tot=0
for q in Qs:
    t=time.time(); r=e.ask(q); c=calltrace.cost_summary(r.trace); tot+=c['usd']
    print(f"{time.time()-t:5.1f}s in={c['in']:6d} hit={c['cache_hit']:6d} ({c['cache_hit']/c['in']:.0%}) usd={c['usd']:.5f} | {q[:55]}\n      -> {r.answer[:230].replace(chr(10),' ')}")
    time.sleep(6)
print("total",round(tot,5))
