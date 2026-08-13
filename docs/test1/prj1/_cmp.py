import io,sys,csv,json,sqlite3,collections,re
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8')
sys.path.insert(0,'.')
from apps.api.services.standardize import normalize_model_code

REF={'上海浦东':94,'亨通':95,'宏胜':96,'远东':97}
c=sqlite3.connect('data/mempas.db')

def key(s):
    s=normalize_model_code(str(s or '')).upper()
    return re.sub(r'[\s\*Xx×+()（）]','',s)

for name,sid in REF.items():
    f=f'docs/test1/prj1/徐汇区华泾镇综合机电分包工程投标文件-{name}.csv'
    rr=list(csv.DictReader(open(f,encoding='utf-8-sig')))
    items=[r for r in rr if r['清单序号']!='总价']
    total=[r for r in rr if r['清单序号']=='总价'][0]
    refpg=collections.Counter(r['PDF页码'] for r in items)
    ref_sum=sum(float(r['合价']) for r in items if r['合价'])
    # ours
    ours=list(c.execute('select spec,qty,unit_price,total_price,extraction_meta from bid_quote_lines where submission_id=?',(sid,)))
    ourpg=collections.Counter()
    for *_ ,m in ours:
        try: ourpg[json.loads(m)['source_ref'].get('page')]+=1
        except Exception: pass
    our_sum=sum(r[3] for r in ours if r[3])
    refk={key(r['规格型号']) for r in items}
    ourk=[key(r[0]) for r in ours]
    hit=sum(1 for k in set(ourk) if k in refk)
    print(f'===== {name} (sub {sid}) =====')
    print(f'  参考 {len(items)} 项  总价 {float(total["合价"]):,.2f}  明细合计 {ref_sum:,.2f}')
    print(f'  我们 {len(ours)} 行  合价求和 {our_sum:,.2f}')
    print(f'  参考页码分布: ' + ' '.join(f'p{k}:{v}' for k,v in sorted(refpg.items(),key=lambda x:int(x[0]))))
    print(f'  我们页码分布: ' + ' '.join(f'p{k}:{v}' for k,v in sorted(ourpg.items(),key=lambda x:(x[0] is None,x[0]))))
    print(f'  规格键命中 {hit}/{len(refk)} 参考唯一键；我们唯一键 {len(set(ourk))}，重复行 {len(ourk)-len(set(ourk))}')
