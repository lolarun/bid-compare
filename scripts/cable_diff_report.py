"""cable_diff_report.py — 识别结果 vs 标准答案（golden），逐条对齐差异表。

为什么不按行序对齐：识别多出/少掉一行，后面全部错位，位置对齐会把一份几乎正确的
结果报成 33% 正确（实测宏胜：总额只差 2.96 元，按位置却只有 46/136 对得上）。
故按**内容**对齐：分层贪心一对一匹配（seq → 名称+规格+数量 → 规格+数量 → 规格），
剩下的才算 missing/extra。分层是因为阀门三份里 89 行共用少量规格（DN20…），
只按规格+数量会把不同的行随机配对，把价格错误凭空造出来。

七份文档跨两套 golden 口径：
  - 四份电缆：unit_price / total_price（无税分列），declared_total = 合价之和
  - 三份阀门：含税列为权威（declared_total = 含税合价之和）；不含税列多为 derived，
    泰科龙 unit_price_incl_tax 在 golden 里是 null（Excel 未存）——**null 不评估**，
    单独计入「未评估」列，不算对也不算错。

输出：missing / extra / duplicate / spec / qty / unit_price / total + 合价求和差。

数据源：
  --source vl         读 <out>/<doc>/document.csv（VL 直出产物，默认）
  --source pipeline   走 SnapshotProvider 重放多阶段管线（仅四份电缆有快照）

用法：
    python scripts/cable_diff_report.py --source vl --out tmp/vl_bakeoff_v2
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SRC = REPO / "docs" / "test1" / "prj1"
GOLDEN = REPO / "data" / "golden"
SNAP = REPO / "tests" / "fixtures" / "ocr_snapshots"
VL_DIR = REPO / "tmp" / "vl_bakeoff"

# doc → (golden slug, declared_total, basis)。basis 决定用哪一套价格列对齐。
DOCS = {
    "上海浦东": ("quote_cable_pudong", 20629762.68, "plain"),
    "亨通": ("quote_cable_hengtong", 20966959.43, "plain"),
    "宏胜": ("quote_cable_hongsheng", 20597048.33, "plain"),
    "远东": ("quote_cable_yuandong", 20014715.08, "plain"),
    "凯硕新正": ("quote_kaishuo", 932154.0, "incl"),
    "上海绵存": ("quote_miancun", 1667051.0, "incl"),
    "泰科龙": ("quote_taikelong", 1067616.41, "incl"),
}
TOL = 0.02


def norm_spec(s: str | None) -> str:
    """型号归一：去空格、统一乘号与大小写。OCR 会把字母游程拆开（RTTY Z）。"""
    s = (s or "").upper()
    s = re.sub(r"[\s　]", "", s)
    return s.replace("×", "*").replace("X", "*")


def norm_name(s: str | None) -> str:
    return re.sub(r"[\s　()（）]", "", (s or "")).upper()


def close(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= TOL


def _f(x):
    if x is None or x == "":
        return None
    try:
        return float(re.sub(r"[,¥￥\s]", "", str(x)))
    except ValueError:
        return None


def _seq(x) -> str:
    m = re.match(r"^\s*(\d+)", str(x or ""))
    return m.group(1) if m else ""


def load_golden(slug: str, up_basis: str = "plain", tp_basis: str = "plain") -> list[dict]:
    """按实际评分口径取 golden 的价格列。口径必须与识别侧同源，否则是在比两把尺子。"""
    col = {"plain": ("unit_price", "total_price"),
           "incl": ("unit_price_incl_tax", "total_price_incl_tax"),
           "excl": ("unit_price_excl_tax", "total_price_excl_tax"),
           "none": (None, None)}
    up = col[up_basis][0]
    tp = col[tp_basis][1]
    g = json.loads((GOLDEN / f"{slug}.json").read_text(encoding="utf-8"))
    return [{"seq": _seq(r.get("seq")), "name": r.get("name") or "",
             "spec": r.get("spec") or "", "qty": _f(r.get("qty")),
             "unit_price": _f(r.get(up)) if up else None,
             "total_price": _f(r.get(tp)) if tp else None}
            for r in g["rows"]]


def load_pipeline(slug: str, doc_name: str) -> list[dict]:
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter
    pdf = next(SRC.glob(f"*{doc_name}.pdf"))
    draft = recognize_tables(
        file_path=str(pdf),
        provider=SnapshotProvider(inner=None, snapshot_path=SNAP / f"{slug}.json",
                                  mode="replay"),
        adapter=_get_quote_adapter())
    out = []
    for r in draft.rows:
        if r.row_type != "quote_line":
            continue
        f = r.fields
        out.append({"seq": _seq(f.get("seq")), "name": f.get("name") or "",
                    "spec": f.get("spec") or f.get("name") or "",
                    "qty": _f(f.get("qty")), "unit_price": _f(f.get("unit_price")),
                    "total_price": _f(f.get("total_price")), "row_type": "detail",
                    "copy_no": ""})
    return out


# 列名映射：极简提示词下模型输出**文档自己的列名**（这正是泛化要的），消费方必须自己
# 映射。含税/不含税同时存在时必须按 basis 选对那一列，否则把税前税后混为一谈。
# 含税/不含税的排除词。英文口径一并列入：同一个模型在不同文档上会自发切换中英表头
# （实测 qwen3.7-plus 对某份输出 seq/name/spec/quantity，对另一份输出中文表头），
# 消费方只认一种语言就会把一份完全正确的产物判成 0 行。
_EXCL = ("不含税", "未含税", "税前", "excl", "EXCL", "Excl", "pre_tax", "pretax")
_NAME_HINTS = [("名称",), ("品名",), ("材料",), ("name",), ("Name",), ("NAME",)]


def _pick(headers: list[str], tiers: list[tuple], exclude: tuple = ()) -> str:
    for tier in tiers:
        for h in headers:
            if not h or any(x in h for x in exclude):
                continue
            if all(k in h for k in tier):
                return h
    return ""


def map_columns(headers: list[str]) -> dict[str, str]:
    """映射到比价槽位。含税/不含税分列时两套都取，由 load_vl 按可得性选评分口径。

    极简提示词下模型输出的是**文档自己的列名**（这正是泛化要的），消费方必须自己
    映射；绝不能假定固定列名，也不能把税前税后混为一谈。
    """
    lower = [h.lower() for h in headers]

    def en(*tiers):                      # 英文表头按小写匹配，回填成原始列名
        for t in tiers:
            for h, lo in zip(headers, lower):
                if any(x in lo for x in ("excl", "pre_tax", "pretax")):
                    continue
                if all(k in lo for k in t):
                    return h
        return ""

    out = {
        "spec": _pick(headers, [("规格",), ("型号",)]) or en(("spec",), ("model",)),
        "qty": _pick(headers, [("数量",), ("工程量",)]) or en(("quantity",), ("qty",)),
        "name": _pick(headers, _NAME_HINTS),
        "seq": _pick(headers, [("序号",), ("序",)]) or en(("seq",), ("no.",), ("index",)),
        "row_type": _pick(headers, [("row_type",)]),
        "copy_no": _pick(headers, [("copy_no",)]),
        "unit_price_incl": (_pick(headers, [("含税单价",), ("单价", "含税"),
                                            ("综合单价",), ("单价",)], exclude=_EXCL)
                            or en(("unit_price",), ("unit", "price"), ("price",))),
        "total_price_incl": (_pick(headers, [("价税合计",), ("含税合价",), ("含税金额",),
                                             ("合价",), ("金额",), ("总价",)],
                                   exclude=_EXCL + ("税额", "税率"))
                             or en(("total_price",), ("total", "amount"), ("amount",))),
        "unit_price_excl": (_pick(headers, [("单价", "不含税"), ("不含税单价",)])
                            or _pick(headers, [("unit_price_excl_tax",),
                                               ("unit_price_excl",)])),
        "total_price_excl": (_pick(headers, [("合计", "不含税"), ("合价", "不含税"),
                                             ("不含税金额",)])
                             or _pick(headers, [("total_price_excl_tax",),
                                                ("total_price_excl",)])),
    }
    return {k: v for k, v in out.items() if v}


def load_vl(doc_name: str, basis: str, vl_dir: Path) -> tuple[list[dict], dict]:
    """返回 (rows, meta)。meta 记录实际用于评分的价格口径，报告里必须说明。

    阀门三份的 golden 权威口径是含税；但凯硕 PDF 只有「不含税单价」+「价税合计」，
    没有含税单价列 —— 此时单价按不含税口径对不含税 golden 评分，并如实标注，
    不能拿不含税单价去对含税 golden（那会造出 89 行假错误）。
    """
    path = vl_dir / doc_name / "document.csv"
    if not path.exists():
        return [], {"error": "产物缺失"}
    rows = [r for r in csv.DictReader(path.open(encoding="utf-8"), restkey="_over")
            if any(r.values())]
    if not rows:
        return [], {"error": "空产物"}
    headers = [h for h in rows[0].keys() if h and h != "_over"]
    # 表头列数 < 数据列数 = 整表右移：凯硕把「规格/型号」两列并成一个表头，数据仍是两列，
    # 于是 5 号列往后全部错位，价格字段整列作废。必须显式报出来，不能静默按列名取值。
    overflow = sum(1 for r in rows if r.get("_over"))
    short = sum(1 for r in rows if sum(v is None for k, v in r.items()
                                       if k != "_over") > 0)
    cmap = map_columns(headers)

    def choose(slot: str) -> tuple[str, str]:
        if basis == "plain":
            return cmap.get(f"{slot}_incl", ""), "plain"
        if cmap.get(f"{slot}_incl"):
            return cmap[f"{slot}_incl"], "incl"
        if cmap.get(f"{slot}_excl"):
            return cmap[f"{slot}_excl"], "excl"
        return "", "none"

    up_col, up_basis = choose("unit_price")
    tp_col, tp_basis = choose("total_price")
    meta = {"headers": headers, "unit_price_col": up_col, "unit_price_basis": up_basis,
            "total_price_col": tp_col, "total_price_basis": tp_basis,
            "rows_with_extra_field": overflow, "rows_with_missing_field": short}
    if not tp_col:
        print(f"  ! {doc_name} 找不到合价列；表头 {headers}")
    if overflow:
        print(f"  ! {doc_name} {overflow}/{len(rows)} 行的字段数多于表头 "
              f"({len(headers)} 列) —— 整表右移，价格字段不可信")

    def g(r, col):
        return r.get(col, "") or "" if col else ""

    out = [{"seq": _seq(g(r, cmap.get("seq", ""))), "name": g(r, cmap.get("name", "")),
            "spec": g(r, cmap.get("spec", "")), "qty": _f(g(r, cmap.get("qty", ""))),
            "unit_price": _f(g(r, up_col)), "total_price": _f(g(r, tp_col)),
            "row_type": (g(r, cmap.get("row_type", "")) or "").strip().lower(),
            "copy_no": (g(r, cmap.get("copy_no", "")) or "").strip(),
            "_cells": [_f(v) for v in r.values() if _f(v) is not None]} for r in rows]
    return out, meta


def align(got: list[dict], want: list[dict]) -> dict:
    """分层贪心一对一匹配：seq → 名称+规格+数量 → 规格+数量 → 规格。

    只按规格匹配在阀门文档上会乱配（89 行共用少量 DN 规格），故先用更强的键。
    """
    def keys(r) -> list[tuple]:
        s, n, q = norm_spec(r["spec"]), norm_name(r["name"]), (
            round(r["qty"], 2) if r["qty"] is not None else None)
        return [("seq", r["seq"]) if r["seq"] else None,
                ("nsq", n, s, q), ("sq", s, q), ("s", s)]

    idx: list[dict[tuple, list[int]]] = [{} for _ in range(4)]
    for i, g in enumerate(got):
        for tier, k in enumerate(keys(g)):
            if k:
                idx[tier].setdefault(k, []).append(i)

    used: set[int] = set()
    pairs, missing = [], []
    for w in want:
        hit = None
        for tier, k in enumerate(keys(w)):
            if not k:
                continue
            hit = next((i for i in idx[tier].get(k, []) if i not in used), None)
            if hit is not None:
                break
        if hit is None:
            missing.append(w)
        else:
            used.add(hit)
            pairs.append((got[hit], w))
    extra = [g for i, g in enumerate(got) if i not in used]

    spec_pool: dict[str, int] = {}
    for g in got:
        spec_pool[norm_spec(g["spec"])] = spec_pool.get(norm_spec(g["spec"]), 0) + 1
    dup = sum(1 for k, v in spec_pool.items() if v > 1 and k)

    bad = {"spec": 0, "qty": 0, "unit_price": 0, "total_price": 0}
    skipped = {"qty": 0, "unit_price": 0, "total_price": 0}
    for g, w in pairs:
        if norm_spec(g["spec"]) != norm_spec(w["spec"]):
            bad["spec"] += 1
        for f in ("qty", "unit_price", "total_price"):
            if w[f] is None:                 # golden 无权威值（derived/未存）→ 不评估
                skipped[f] += 1
                continue
            if not close(g[f], w[f]):
                bad[f] += 1
    return {"matched": len(pairs), "missing": missing, "extra": extra,
            "duplicate_specs": dup, "field_bad": bad, "not_scored": skipped,
            "pairs": pairs}


def select_copy(detail: list[dict]) -> tuple[list[dict], list[str]]:
    """同一份文件里出现多套清单时，只对**一套**打分，并报出共有几套。

    这不是去重：实测某份投标文件的 PDF 里正本(第 2-8 页)与盖章副本(第 9-15 页)各印了
    一遍完整清单，模型照实输出 256 行是**对的**，golden 只誊录了其中一套。把两套一起
    求和会得到正好两倍的金额——那是评分口径错，不是识别错。

    只有当 copy_no 把行切成 ≥2 组、且各组规模相当（最小组 ≥ 最大组的一半）时才认定
    是多套副本；否则原样返回（避免把被当成页计数的 copy_no 误判成副本）。
    """
    groups: dict[str, list[dict]] = {}
    for r in detail:
        groups.setdefault(r.get("copy_no") or "", []).append(r)
    keys = [k for k in groups if k]
    if len(keys) < 2 or "" in groups:
        return detail, []
    sizes = [len(groups[k]) for k in keys]
    if min(sizes) < max(sizes) / 2:
        return detail, []                # 组间规模悬殊 → 不像副本，可能是页计数
    return groups[sorted(keys)[0]], sorted(keys)


def document_total(total_rows: list[dict], detail_sum: float,
                   *, tol: float = 0.01) -> tuple[float | None, str]:
    """从抽到的合计行推出**这份文件自己声明的总价**——完全不看 golden。

    清单常按章节分段各出一个合计（实测两份文档都是：矿物电缆 + 普通电缆各一个），
    所以候选有两种：某个单一合计值，或全部去重合计值之和。取与明细求和最接近的那个，
    并要求落在 tol 以内——否则如实返回"无法闭环"，绝不硬凑一个数字出来。

    有了它，系统不需要 golden 就能说出"我漏了多少钱"：实测五份文档的自校验差与
    对照 golden 的真实差**完全相等**。
    """
    vals = sorted({round(v, 2) for r in total_rows
                   for v in (r.get("_cells") or [])
                   if v is not None and abs(v) > 1}, reverse=True)
    if not vals:
        return None, "未抽到合计行"
    cands = [(v, "单一合计") for v in vals] + [(round(sum(vals), 2), "章节合计之和")]
    best, how = min(cands, key=lambda c: abs(c[0] - detail_sum))
    if best and abs(best - detail_sum) / max(abs(best), 1.0) > tol:
        return None, f"合计行 {vals[:3]} 与明细求和 {detail_sum:,.2f} 差距过大，无法闭环"
    return best, how


def split_rows(got: list[dict], declared: float) -> tuple[list[dict], list[dict], list[dict]]:
    """按 row_type 拆明细 / 小计 / 合计。没有 row_type 列时退回按金额识别合计行。

    合计行是**证据不是噪声**：明细之和 vs 声明总价是免费的闭环校验。抽取端不该丢弃，
    评分端也不该把它算进明细和（否则总额凭空翻倍——宏胜曾因此报成 +1862 万）。
    """
    if any(r.get("row_type") for r in got):
        sub = [r for r in got if r.get("row_type") == "subtotal"]
        total = [r for r in got if r.get("row_type") in ("total", "grand_total")]
        # 其余一律当明细：未知标签**不能静默丢弃**（丢弃会把召回凭空做高）。
        detail = [r for r in got if r not in sub and r not in total]
        return detail, sub, total
    detail, total = [], []
    for r in got:
        v = r["total_price"]
        if v is not None and abs(v - declared) <= max(1.0, declared * 1e-6):
            total.append(r)
        else:
            detail.append(r)
    return detail, [], total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("pipeline", "vl"), default="vl")
    ap.add_argument("--doc", action="append")
    ap.add_argument("--out", default=str(VL_DIR), help="VL 产物目录")
    args = ap.parse_args()

    vl_dir = Path(args.out)
    print(f"数据源：{args.source}（{vl_dir}）\n")
    print(f"{'文档':10}{'明细':>6}{'参考':>6}{'匹配':>6}{'缺失':>6}{'多余':>6}"
          f"{'重复规格':>9}{'规格错':>7}{'数量错':>7}{'单价错':>7}{'合价错':>7}"
          f"{'合价求和差':>15}  校验")
    out_rows = []
    for name in (args.doc or DOCS):
        slug, declared, basis = DOCS[name]
        if args.source == "pipeline":
            got, meta = load_pipeline(slug, name), {"unit_price_basis": basis,
                                                    "total_price_basis": basis}
        else:
            got, meta = load_vl(name, basis, vl_dir)
        want = load_golden(slug, meta.get("unit_price_basis", basis),
                           meta.get("total_price_basis", basis))
        if not got:
            print(f"{name:10}  无数据（{meta.get('error', '识别失败')}）")
            out_rows.append({"doc": name, "source": args.source, "detail": 0,
                             "want": len(want), "failed": True,
                             "error": meta.get("error")})
            continue
        detail, subtotal, total_rows = split_rows(got, declared)
        # copy_no 只观测不过滤。实测模型把它当「第几张表/第几页」在数（宏胜 1..7、
        # 绵存 1..5，这两份并没有 5~7 份副本），拿它筛行会把大半明细silently 丢掉。
        copies = sorted({r.get("copy_no") for r in detail if r.get("copy_no")})
        a = align(detail, want)
        s = sum(v for r in detail if (v := r["total_price"]) is not None)
        if total_rows:
            # 合计行常常整行左右错位（「总价,,,总金额,,,,20014715.08」），认列会漏；
            # 取该行**任一**数字与官方总价比对才是稳的。
            vals = [v for r in total_rows for v in (r.get("_cells")
                    or [r["total_price"]]) if v is not None]
            uniq = sorted({round(v, 2) for v in vals}, reverse=True)
            # 清单常分章节（浦东：矿物 9,007,761.86 + 普通 11,622,000.82 = 官方总价），
            # 故除了「单个合计=官方」，还要看章节合计的子集之和。
            from itertools import combinations
            subset = next((c for n in (2, 3, 4) for c in combinations(uniq, n)
                           if abs(sum(c) - declared) <= 0.05), None)
            if any(abs(v - declared) <= 0.05 for v in vals):
                chk = "合计行=官方总价"
            elif subset:
                chk = f"{len(subset)} 个章节合计之和=官方总价"
            else:
                chk = f"合计行 {[f'{v:,.2f}' for v in uniq[:2]]} ≠ 官方 {declared:,.2f}"
        else:
            chk = "未抽到合计行"
        fb, ns = a["field_bad"], a["not_scored"]
        print(f"{name:10}{len(detail):>6}{len(want):>6}{a['matched']:>6}"
              f"{len(a['missing']):>6}{len(a['extra']):>6}{a['duplicate_specs']:>9}"
              f"{fb['spec']:>7}{fb['qty']:>7}{fb['unit_price']:>7}{fb['total_price']:>7}"
              f"{s - declared:>+15,.2f}  {chk}")
        out_rows.append({
            "doc": name, "source": args.source, "basis": basis,
            "detail": len(detail), "subtotal_rows": len(subtotal),
            "total_rows": len(total_rows), "copies": copies,
            "want": len(want), "matched": a["matched"],
            "missing": len(a["missing"]), "extra": len(a["extra"]),
            "duplicate_specs": a["duplicate_specs"], "field_bad": fb,
            "price_basis": {k: meta.get(k) for k in
                            ("unit_price_col", "unit_price_basis",
                             "total_price_col", "total_price_basis")},
            "not_scored": ns, "detail_sum": round(s, 2),
            "declared_total": declared, "sum_delta": round(s - declared, 2),
            "checksum": chk,
            "missing_examples": [f'{m["seq"]}|{m["name"]}|{m["spec"]}'
                                 for m in a["missing"][:8]],
            "extra_examples": [f'{e["seq"]}|{e["name"]}|{e["spec"]}'
                               for e in a["extra"][:8]],
        })
        if any(ns.values()):
            print(f"{'':10}  未评估（golden 无权威值）：{ns}")

    path = REPO / "tmp" / f"cable_diff_{args.source}.json"
    path.write_text(json.dumps(out_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
