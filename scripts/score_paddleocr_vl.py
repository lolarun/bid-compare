"""score_paddleocr_vl.py — 把百度 PaddleOCR-VL 的表格输出接到 e2e_diff.diff_doc()，
产出跟生产识别器同一套指标（行召回率/精确率/字段级 exact/tolerance）。

不重新调 API——读 scripts/try_paddleocr_vl.py 已经落盘的 outputs/baidu_paddleocr_vl/*.json。

为什么不用 try_paddleocr_vl.py 里那个"第一列是不是正整数"的粗糙判断：那只在文档
本身有序号列时有意义（凯硕/泰科龙/远东），另外4份（浦东/绵存/亨通/宏胜）投标文件
第一列是材料名称，不是序号——不是识别失败，是这几份文档的表格结构本身就没有
序号列（design/21 §5.1 记录过同一个事实：凯硕/泰科龙/远东有序号列，其余没有）。

解析用 `matrix`（解析成 cells[] 索引的规整二维表），不用 `markdown` 文本——markdown
里同一行的列数会因为合并单元格跳变，`matrix` 每行列数固定跟表头对齐，更不容易解析错位。

列名→字段的映射按表头关键词匹配，不针对任何一份文档的具体列序硬编码——不同文档
表头文字不同，这里用的是通用的中文财务表头关键词，换一批新文档不用改代码。

用法：
    python scripts/score_paddleocr_vl.py --doc kaishuo
    python scripts/score_paddleocr_vl.py --all
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from e2e_diff import diff_doc  # noqa: E402
from try_paddleocr_vl import DOCS, OUT_DIR, SEVEN_QUOTE_DOCS, _git_sha  # noqa: E402

# design/26 §3.3：本脚本的列位置映射有已知未解决的缺陷——同一张表不同行对
# "空单元格"的处理方式不一致，导致固定表头位置的映射会在部分行上错位
# （已用 seq=1 vs seq=3 两行实测证实）。逐字段准确率数字**不可信**，这不是
# 本轮 P0 要修的东西——正式修法是 P1 的 ExtractionDraft 适配器（design/26 §5），
# 不是继续在这个探索脚本里打补丁。P0 只做 SHA 绑定，不碰这层解析逻辑。
KNOWN_LIMITATION = (
    "字段级数字有已知列漂移缺陷（design/26 §3.3），仅供方向参考，不作为"
    "验收依据——验收走 P1 适配器 + 生产四道门（design/26 §5-6）。"
)

# 表头关键词 → golden 字段名（e2e_diff._FIELD_MAP 用的那套名字）。
# 顺序有意义：更具体的（"不含税"）排在通用的（"单价"）前面，避免通用词提前命中。
_HEADER_KEYWORDS: list[tuple[str, str]] = [
    ("序号", "seq"),
    ("项目名称", "name"), ("材料", "name"), ("名称", "name"), ("设备", "name"),
    ("规格型号", "spec"), ("规格", "spec"),
    ("型号", "model"),
    ("计量单位", "unit"), ("单位", "unit"),
    ("数量", "qty"),
    ("品牌", "brand"),
    ("税率", "tax_rate"),
    ("税额", "tax_amount"),
    ("单价（元）不含税", "unit_price_excl_tax"), ("单价(元)不含税", "unit_price_excl_tax"),
    ("不含税单价", "unit_price_excl_tax"),
    ("合计（元）不含税", "total_price_excl_tax"), ("合计(元)不含税", "total_price_excl_tax"),
    ("不含税合计", "total_price_excl_tax"),
    ("价税合计", "total_price_incl_tax"), ("含税合计", "total_price_incl_tax"),
    ("含税单价", "unit_price_incl_tax"),
    ("单价", "unit_price_incl_tax"),   # 通用兜底，放最后
    ("合价", "total_price_incl_tax"), ("合计", "total_price_incl_tax"),  # 通用兜底
]

_QUOTE_TABLE_HINTS = ("单价", "合价", "合计", "数量")  # 表头里出现任一即视为报价表，排除纯规格参考表


def _resolve_matrix(table: dict) -> list[list[str]]:
    """matrix 是 cells[] 的索引二维数组，这里解出真正的文字。"""
    matrix = table.get("matrix")
    cells = table.get("cells") or []
    if not matrix:
        return []
    out = []
    for row in matrix:
        texts = []
        for idx in row:
            if isinstance(idx, int) and 0 <= idx < len(cells):
                texts.append(str(cells[idx].get("text") or "").strip())
            else:
                texts.append("")
        out.append(texts)
    return out


def _map_header(header_row: list[str]) -> dict[int, str]:
    col_field: dict[int, str] = {}
    for i, h in enumerate(header_row):
        h = (h or "").strip()
        if not h:
            continue
        for kw, field_name in _HEADER_KEYWORDS:
            if kw in h and field_name not in col_field.values():
                col_field[i] = field_name
                break
    return col_field


def _is_header_like(row: list[str]) -> bool:
    """整行都是表头关键词（不含数字数据）→ 判定为表头/表头重复行，跳过。"""
    if not row or not any(row):
        return False
    hits = sum(1 for c in row if any(kw in c for kw, _ in _HEADER_KEYWORDS))
    return hits >= max(2, len(row) // 3)


@dataclass
class _Row:
    row_type: str = "quote_line"
    fields: dict = field(default_factory=dict)


_TEXT_FIELDS = {"name", "spec", "model", "unit", "brand"}
_NUM_FIELDS = {"qty", "tax_rate", "unit_price_excl_tax", "total_price_excl_tax",
              "tax_amount", "unit_price_incl_tax", "total_price_incl_tax"}


def _looks_numeric(s: str) -> bool:
    t = s.strip().replace(",", "").replace("%", "")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def _type_sane(field_name: str, value: str) -> bool:
    """数字不该出现在品牌/名称这类文本字段，反之亦然。

    根源：同一张表格里不同行对"空单元格"的处理不一致——某一行品牌为空时，
    后面数值列的值会顶替填进品牌位（实测：seq=1 行"品牌"位是价税合计的
    重复值 71.00，seq=3 行同一位置却是正常品牌名 KITZ）。这不是解析器的
    下标错误——两行读的都是表头对齐的同一个列位置，是这一行数据本身的
    结构跟表头对不上。宁可把这种情况丢成缺失，也不能把错位的数字当品牌用。
    """
    if field_name in _TEXT_FIELDS and field_name != "unit":
        return not _looks_numeric(value)
    if field_name in _NUM_FIELDS:
        return _looks_numeric(value) or field_name == "tax_rate"
    return True


def build_draft_rows(doc_json: dict) -> list[_Row]:
    rows: list[_Row] = []
    last_good_col_map: dict[int, str] | None = None
    for page in doc_json.get("pages") or []:
        for table in page.get("tables") or []:
            grid = _resolve_matrix(table)
            if not grid:
                continue
            col_map = _map_header(grid[0])
            has_price_col = any(v in ("unit_price_incl_tax", "unit_price_excl_tax", "qty")
                                for v in col_map.values())
            has_price_kw = any(kw in c for c in grid[0] for kw in _QUOTE_TABLE_HINTS)
            if has_price_col and has_price_kw:
                last_good_col_map = col_map  # 记住这份文档的列映射，供无表头续页复用
            elif last_good_col_map is not None:
                # 合并表格的续页没有自己的表头行——沿用同一份文档上一次成功识别的列映射，
                # 不能因为这页第一行不是表头就整页跳过（kaishuo seq 47-89 曾经因此丢失）。
                col_map = last_good_col_map
            else:
                continue  # 还没见过有效表头，大概率是规格参考表，跳过

            for r in grid:
                if _is_header_like(r):
                    continue
                if not any(c.strip() for c in r):
                    continue  # 全空行（合并单元格续行的占位符）
                fields: dict = {}
                for i, val in col_map.items():
                    if i < len(r) and r[i] and _type_sane(val, r[i]):
                        fields.setdefault(val, r[i])
                if not fields.get("name") and not fields.get("qty"):
                    continue  # 关键字段都拿不到，大概率是脏行，不计入
                rows.append(_Row(row_type="quote_line", fields=fields))
    return rows


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", choices=SEVEN_QUOTE_DOCS)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    doc_keys = SEVEN_QUOTE_DOCS if a.all else [a.doc or "kaishuo"]

    print(f"{'文档':12s} {'match_mode':14s} {'行召回率':>9s} {'行准确率':>9s} "
          f"{'name准确':>9s} {'qty准确':>9s} {'含税合价准确':>12s} {'声明总价差':>10s}")
    results = []
    for doc_key in doc_keys:
        _, golden_path = DOCS[doc_key]
        result_path = OUT_DIR / f"{doc_key}.json"
        if not golden_path or not golden_path.exists() or not result_path.exists():
            print(f"{doc_key:12s}  跳过（缺 golden 或缺已跑的识别结果）")
            continue
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        doc_json = json.loads(result_path.read_text(encoding="utf-8"))
        draft_rows = build_draft_rows(doc_json)
        scored = diff_doc(doc_key, golden, draft_rows)
        row_level = scored["summary"]["row_level"]
        fm = scored["summary"]["field_metrics"]
        mode = row_level.get("match_mode", "seq")

        def _rate(key: str, metric: str = "exact_rate") -> str:
            v = fm.get(key, {}).get(metric)
            return f"{v:.1%}" if v is not None else "—"

        print(f"{doc_key:12s} {mode:14s} {row_level['row_recall']:>9.1%} "
              f"{row_level['row_precision']:>9.1%} {_rate('name'):>9s} {_rate('qty'):>9s} "
              f"{_rate('total_price_incl_tax'):>12s} "
              f"{scored['summary']['document_level']['declared_vs_all_diff']!s:>10s}")
        results.append({"doc": doc_key, "extracted_rows": len(draft_rows), "summary": scored["summary"]})

    (OUT_DIR / "field_score_summary.json").write_text(
        json.dumps({
            "code_sha": _git_sha(), "known_limitation": KNOWN_LIMITATION,
            "docs": doc_keys, "runs": results,
        }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n完整字段级指标 → {OUT_DIR}/field_score_summary.json（{KNOWN_LIMITATION}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
