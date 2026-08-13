"""锚点匹配测量(docs/design/05 §9 第2步)——把"30% → ?"跑成实测数。

方法(通用机制,零硬编码同义词表):
  1. 解析招标清单 → 90 锚点行
  2. 载入 project 60 的 213 条供应商报价
  3. DashScope 嵌入做语义召回:每条报价找余弦最近的锚点
  4. DN 规则核对:DN 不一致则判为未命中(进残差)
  5. 报告:报价匹配率 / 锚点覆盖率(≥2家可比) / 残差

这是"嵌入召回 + 规则核对",未接 LLM 复核(闸②)与缓存——先量化上限。
对照:降级路径裸对齐 30%。
"""
import os, re, sys, sqlite3
import numpy as np
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apps.api.services.tender.tender_list import parse_tender_xlsx

TENDER = r"docs\test\金桥地铁上盖J9A-03地块（浦发上城科创智谷）研发及商业项目（阀门）招标清单.xlsx"
DB = os.path.join("data", "mempas.db")
PROJECT_ID = 60
SIM_THRESHOLD = 0.50          # 余弦低于此视为无可信锚点
EMB_MODEL = "text-embedding-v3"


def _key():
    k = os.getenv("DASHSCOPE_API_KEY")
    if not k:
        for line in open("apps/api/.env", encoding="utf-8"):
            if line.startswith("DASHSCOPE_API_KEY="):
                k = line.split("=", 1)[1].strip()
    return k


def dn_of(s: str):
    m = re.search(r"DN\s*0*(\d+)", s or "", re.I)
    return int(m.group(1)) if m else None


def embed(client, texts):
    out = []
    for i in range(0, len(texts), 10):
        r = client.embeddings.create(model=EMB_MODEL, input=texts[i:i + 10])
        out.extend(d.embedding for d in r.data)
    return np.array(out, dtype=np.float32)


def main():
    client = OpenAI(api_key=_key(), base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

    anchors = parse_tender_xlsx(TENDER)
    a_text = [f"{a.name} {a.spec} {a.pressure} {a.material_text()}".strip() for a in anchors]
    a_dn = [dn_of(a.spec) or dn_of(a.name) for a in anchors]

    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    quotes = conn.execute(
        """SELECT q.id qid, q.supplier_id sid, m.standard_name name, m.spec spec
           FROM quotes q JOIN materials m ON q.material_id=m.id
           WHERE q.project_id=?""", (PROJECT_ID,)).fetchall()
    conn.close()
    q_text = [f"{r['name']} {r['spec']}".strip() for r in quotes]
    q_dn = [dn_of(r['spec']) or dn_of(r['name']) for r in quotes]

    print(f"锚点 {len(anchors)} 行, 报价 {len(quotes)} 条. 计算嵌入...", flush=True)
    A = embed(client, a_text)
    Q = embed(client, q_text)
    A /= np.linalg.norm(A, axis=1, keepdims=True)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    sims = Q @ A.T                      # (n_quote, n_anchor)

    # 每条报价:在 DN 一致的锚点里取余弦最高者
    matched = [None] * len(quotes)      # quote idx -> anchor idx
    residue_reason = [""] * len(quotes)
    for qi in range(len(quotes)):
        order = np.argsort(-sims[qi])
        picked = None
        for ai in order:
            if sims[qi, ai] < SIM_THRESHOLD:
                break
            if q_dn[qi] is not None and a_dn[ai] is not None and q_dn[qi] != a_dn[ai]:
                continue               # DN 规则核对不过,看下一个候选
            picked = ai
            break
        if picked is None:
            residue_reason[qi] = "无DN一致且高相似的锚点"
        matched[qi] = picked

    # ── 指标 ──
    n_matched = sum(1 for m in matched if m is not None)
    anchor_suppliers = {}              # anchor idx -> set(supplier)
    for qi, ai in enumerate(matched):
        if ai is not None:
            anchor_suppliers.setdefault(ai, set()).add(quotes[qi]['sid'])
    cov_any = len(anchor_suppliers)
    cov_2 = sum(1 for s in anchor_suppliers.values() if len(s) >= 2)
    cov_3 = sum(1 for s in anchor_suppliers.values() if len(s) >= 3)

    print("\n" + "=" * 56)
    print("锚点匹配测量结果(嵌入召回 + DN核对,无LLM/无词表)")
    print("=" * 56)
    print(f"  报价匹配率:   {n_matched}/{len(quotes)} = {n_matched/len(quotes)*100:.0f}%   (降级裸对齐 30%)")
    print(f"  锚点覆盖:     {cov_any}/{len(anchors)} 行至少1家")
    print(f"  可比锚点≥2家: {cov_2}/{len(anchors)} = {cov_2/len(anchors)*100:.0f}%")
    print(f"  三家齐全:     {cov_3}/{len(anchors)}   (降级仅 3 行)")
    # 残差
    res = [(quotes[i]['name'], quotes[i]['spec'], residue_reason[i]) for i in range(len(quotes)) if matched[i] is None]
    print(f"  未匹配残差:   {len(res)} 条")

    # 写明细供检查
    L = ["=== 未匹配残差(前30) ==="]
    for nm, sp, rsn in res[:30]:
        L.append(f"  {nm} | {sp} | {rsn}")
    L.append("\n=== 低置信匹配(cos<0.7,前30,需复核) ===")
    for qi, ai in enumerate(matched):
        if ai is not None and sims[qi, ai] < 0.7:
            L.append(f"  {quotes[qi]['name']}|{quotes[qi]['spec']} -> #{anchors[ai].seq} {anchors[ai].name}|{anchors[ai].spec} (cos={sims[qi,ai]:.2f})")
    open("_anchor_measure.txt", "w", encoding="utf-8").write("\n".join(L))
    print("\n  明细 -> _anchor_measure.txt")


if __name__ == "__main__":
    main()
