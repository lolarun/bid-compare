"""build_cable_golden.py — 把客户提供的参考 CSV 转成版本化 golden JSON。

来源：tests/fixtures/documents/徐汇区华泾镇项目-<供应商>报价清单.csv
（多模态模型逐页转录，客户提供并已登记在 MANIFEST.md）
产物：data/golden/quote_cable_<slug>.json，schema 与既有 golden 保持一致。

**标准答案先审计来源**（测试规则）：本脚本不盲信 CSV，落盘前强制核对
  · 明细行数必须为 136，序号必须是连续的 1..136
  · 逐行 数量 × 单价 ≈ 合价（记录不成立的行，不静默修正）
  · 明细合价之和 vs CSV 自带的总价行（记录差额，不静默抹平）
核对结果写进 audit_notes，任何一项不通过都在 audit_status 里显式标出，
由人决定是否采信，脚本不替人做判断。

用法：
    python scripts/build_cable_golden.py            # 全部四家
    python scripts/build_cable_golden.py --check    # 只审计不落盘
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SRC = REPO / "tests" / "fixtures" / "documents"
OUT = REPO / "data" / "golden"

# 供应商 → (CSV 文件名去掉扩展, golden slug, 官方总价)
SUPPLIERS = {
    "上海浦东": ("quote_cable_pudong", 20629762.68),
    "亨通": ("quote_cable_hengtong", 20966959.43),
    "宏胜": ("quote_cable_hongsheng", 20597048.33),
    "远东": ("quote_cable_yuandong", 20014715.08),
}

EXPECTED_ROWS = 136
ARITH_TOL = 0.05        # 元；逐行 qty×price vs 合价
TOTAL_TOL = 0.05        # 元；明细合计 vs 官方总价


def implied_multiplier(qty, price, total) -> float | None:
    """观测合价相对 数量×单价 的倍数，**不做推断**。

    实测四家第 114 项规格完全相同（WDZA-YJY-2*(4*240+E120)）、数量也相同，但
    上海浦东倍数为 2.0（报单根价 884.75），其余三家为 1.0（报双根合价 ~1700）。
    884.75×2=1769.5 正落在其余三家区间——**倍率是各家报价口径的选择，不是规格属性**。
    因此绝不能从规格串推断倍率去"修正"数据；只能观测、记录、交人工确认。
    直接比单价会把浦东排成半价最低，这正是必须拦下的口径不一致。
    """
    if not qty or not price or total is None:
        return None
    base = qty * price
    return round(total / base, 3) if base else None


def _num(s: str | None) -> float | None:
    s = (s or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def build_one(name: str, slug: str, declared: float, *, check_only: bool) -> dict:
    csv_path = next(SRC.glob(f"*{name}报价清单.csv"))
    raw = csv_path.read_bytes()
    records = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    items = [r for r in records if r["清单序号"] != "总价"]
    total_rows = [r for r in records if r["清单序号"] == "总价"]

    notes: list[str] = []
    problems: list[str] = []

    # ── 审计 1：行数与序号连续性 ──────────────────────────────────────────
    if len(items) != EXPECTED_ROWS:
        problems.append(f"明细行数 {len(items)} ≠ {EXPECTED_ROWS}")
    seqs = [int(r["清单序号"]) for r in items if r["清单序号"].isdigit()]
    if seqs != list(range(1, len(items) + 1)):
        problems.append("序号不是连续的 1..N")

    # ── 审计 2：逐行算术 ─────────────────────────────────────────────────
    arith_bad, blank_price, multiplied = [], [], []
    for r in items:
        qty, price, total = _num(r["数量"]), _num(r["单价"]), _num(r["合价"])
        if price is None and total is None:
            blank_price.append(r["清单序号"])
            continue
        if None in (qty, price, total):
            arith_bad.append((r["清单序号"], "字段缺失"))
            continue
        mult = implied_multiplier(qty, price, total)
        if mult is not None and abs(mult - 1.0) > 0.001:
            multiplied.append((r["清单序号"], mult))
        elif abs(qty * price - total) > ARITH_TOL:
            arith_bad.append((r["清单序号"], round(qty * price - total, 4)))
    if blank_price:
        notes.append(f"单价与合价均为空的行（原文以 / 表示）：{blank_price}")
    if multiplied:
        notes.append(f"合价相对 数量×单价 存在倍数的行（报价口径差异，须人工确认）：{multiplied}")
    if arith_bad:
        notes.append(f"逐行算术不成立：{arith_bad[:10]}")

    # ── 审计 3：明细合计 vs 官方总价 ──────────────────────────────────────
    line_sum = sum(v for r in items if (v := _num(r["合价"])) is not None)
    csv_declared = _num(total_rows[0]["合价"]) if total_rows else None
    if csv_declared is not None and abs(csv_declared - declared) > TOTAL_TOL:
        problems.append(f"CSV 总价 {csv_declared} 与传入官方总价 {declared} 不一致")
    delta = round(line_sum - declared, 4)
    if abs(delta) > TOTAL_TOL:
        notes.append(f"明细合计 {line_sum:.2f} − 官方总价 {declared:.2f} = {delta:+.4f}")

    golden = {
        "doc_type": "quote",
        "source_file": csv_path.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "supplier": name,
        "declared_total": declared,
        "line_sum": round(line_sum, 4),
        "line_sum_minus_declared": delta,
        "row_count": len(items),
        "field_sources": "customer_reference_csv(multimodal transcription)",
        "audit_status": "clean" if not problems else "problems",
        "audit_notes": notes,
        "audit_problems": problems,
        "rows": [
            {
                "seq": r["清单序号"],
                "page": r["PDF页码"],
                "name": r["材料/设备名称"].strip(),
                "spec": r["规格型号"].strip(),
                "quality_standard": r["质量标准/技术指标"].strip(),
                "unit": r["计量单位"].strip(),
                "qty": _num(r["数量"]),
                "unit_price": _num(r["单价"]),
                "total_price": _num(r["合价"]),
                "implied_multiplier": implied_multiplier(
                    _num(r["数量"]), _num(r["单价"]), _num(r["合价"])),
                "note": r["核对说明"].strip(),
            }
            for r in items
        ],
    }

    status = "OK " if not problems else "!! "
    print(f"{status}{name:6} rows={len(items):>3} line_sum={line_sum:>15,.2f} "
          f"declared={declared:>15,.2f} delta={delta:+.4f}")
    for n in notes:
        print(f"      note: {n}")
    for pb in problems:
        print(f"      PROBLEM: {pb}")

    if not check_only:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{slug}.json").write_text(
            json.dumps(golden, ensure_ascii=False, indent=1), encoding="utf-8")
    return golden


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只审计不落盘")
    args = ap.parse_args()
    any_problem = False
    for name, (slug, declared) in SUPPLIERS.items():
        g = build_one(name, slug, declared, check_only=args.check)
        any_problem |= bool(g["audit_problems"])
    if not args.check:
        print(f"\ngolden → {OUT}")
    return 1 if any_problem else 0


if __name__ == "__main__":
    sys.exit(main())
