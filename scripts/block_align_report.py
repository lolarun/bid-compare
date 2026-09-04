"""block_align_report.py — 用真实产物验证块级对齐：报价清单 → 招标锚点。

锚点侧**只提供招标清单真正给得出的字段**（名称/规格/单位/数量），价格一律不给——
生产里招标采购清单没有价格，用金额对块等于拿答案对答案。

用法：
    python scripts/block_align_report.py --out tmp/vl_rot_vote
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apps.api.services.alignment.block_alignment import (  # noqa: E402
    Row,
    align_quote_to_anchors,
)
from scripts.cable_diff_report import (  # noqa: E402
    DOCS,
    load_golden,
    load_vl,
    select_copy,
    split_rows,
)


def to_rows(records: list[dict], *, with_price: bool) -> list[Row]:
    out = []
    for i, r in enumerate(records):
        out.append(Row(doc_index=i, category=r.get("name") or "", spec=r.get("spec") or "",
                       unit=r.get("unit") or "", qty=r.get("qty"),
                       payload={"total_price": r.get("total_price")} if with_price else {}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="tmp/vl_rot_vote")
    ap.add_argument("--doc", action="append")
    args = ap.parse_args()

    print(f"数据源 {args.out}；锚点侧不含价格\n")
    print(f"{'文档':10}{'报价行':>7}{'锚点行':>7}{'对齐':>6}{'对齐率':>8}{'pending':>9}"
          f"{'标题行':>7}  块判定")
    for name in (args.doc or DOCS):
        slug, declared, basis = DOCS[name]
        got, meta = load_vl(name, basis, Path(args.out))
        if not got:
            print(f"{name:10}  无产物")
            continue
        detail, _sub, _tot = split_rows(got, declared)
        detail, _copies = select_copy(detail)
        anchors = load_golden(slug, meta.get("unit_price_basis", basis),
                              meta.get("total_price_basis", basis))
        res = align_quote_to_anchors(to_rows(detail, with_price=False),
                                     to_rows(anchors, with_price=False))
        d = res.to_dict()
        methods = [f"{b['anchor_block'][:6]}:{b['method']}({b['score']})"
                   for b in d["blocks"]]
        print(f"{name:10}{len(detail):>7}{d['anchor_rows']:>7}{d['aligned']:>6}"
              f"{d['aligned_rate']:>8.0%}{d['pending']:>9}"
              f"{d['section_headers_excluded']:>7}  {' | '.join(methods)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
