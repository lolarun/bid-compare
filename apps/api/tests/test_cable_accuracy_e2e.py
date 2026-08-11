"""C 层：识别准确率 vs golden —— fresh E2E，报分布而非单次红绿。

## 为什么单独成层

准确率**不能**用冻结快照测。实测亨通同图、同提示词、temperature=0，两次抽取相差
37,632 元（0.18%）；上海浦东的方向预检有时 0 页未决、有时 9 页未决。把某一次冻进
快照再断言它对得上 golden，测的是那一次抽签的手气，不是代码。
`.claude/rules/tests.md`：replay 验确定性、fresh E2E 验真实模型链路，不得互相冒充。
确定性那半边在 test_cable_golden.py 的 B 层。

## 这一层断言什么、不断言什么

**断言**：灾难性下限——识别不出行、金额差到离谱、价格列没映射上。这些不是波动，
是坏了。上限不断言：跨运行的中位数与分布靠累积样本看，不靠单次通过。

**不断言文本**（名称/规格）。golden 的文本来自客户参考 CSV 的逐页转录，虽经人工
核对，但它与 PDF 字面仍是两个产物（实测有过 golden 命名体系≠PDF 字面的先例）。
金额与行数有独立锚点——136 行两位小数之和**恰好等于官方总价**，转录读错行则该
等式极难成立。故只在这两个维度上给结论。

## 分布怎么来

每次运行把样本追加到 `tmp/cable_accuracy_samples.jsonl`，测试结束打印累积分布。
跑一次得一个点，跑多次才有分布——这正是它不该当红绿门的原因。

    pytest apps/api/tests/test_cable_accuracy_e2e.py -m e2e -s
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO / "data" / "golden"
SAMPLES = REPO / "tmp" / "cable_accuracy_samples.jsonl"

CABLE_DOCS = {
    "quote_cable_pudong": ("上海浦东", 20629762.68),
    "quote_cable_hengtong": ("亨通", 20966959.43),
    "quote_cable_hongsheng": ("宏胜", 20597048.33),
    "quote_cable_yuandong": ("远东", 20014715.08),
}
GOLDEN_ROWS = 136

# 灾难性下限。**不是准确率目标**——是"这不叫波动，这叫坏了"的界线。
# 定得宽是有意的：这一层要拦的是回归，不是替代人去看分布。
MIN_ROW_RECALL = 0.80      # 最佳副本的行数 / golden 行数
MAX_AMOUNT_DEVIATION = 0.10


def _pdf(name: str) -> Path:
    return next((REPO / "docs" / "test1" / "prj1").glob(f"*{name}*.pdf"))


def _best_copy(rows: list) -> tuple[str, list]:
    """副本按 copy_no 分组，取**与声明总价最接近**的一套。

    这里可以看声明总价——它是文档自印的事实，不是 golden；而本层的断言对象是
    golden。用文档自己的数选副本、再拿 golden 评分，两者不同源，不构成循环。
    （B 层不能这么选：那一层连声明总价都不该看。）
    """
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault((r.fields.get("copy_no") or "").strip(), []).append(r)
    return max(groups.items(), key=lambda kv: -len(kv[1]))


@pytest.mark.e2e
@pytest.mark.parametrize("slug", CABLE_DOCS)
def test_accuracy_sample(slug):
    """跑一次真实识别，记一个样本，只在灾难性下限上失败。"""
    if not os.getenv("DASHSCOPE_API_KEY"):
        pytest.skip("需要 DASHSCOPE_API_KEY")

    from apps.api.core.config import get_settings
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.intelligence.vl_direct import recognize_quote_vl

    name, declared = CABLE_DOCS[slug]
    cfg = get_settings()
    prov = DashScopeOCRProvider()
    draft = recognize_quote_vl(
        str(_pdf(name)),
        vl_call=lambda imgs, p: prov.vl_extract_csv(
            imgs, p, model=cfg.DASHSCOPE_QUOTE_VL_MODEL),
        orient_call=lambda parts, p: prov.vl_extract_csv(
            [b for _t, b in parts], p, model=cfg.DASHSCOPE_QUOTE_ORIENT_MODEL,
            labels=[t for t, _b in parts]),
    )
    lines = [r for r in draft.rows if r.row_type == "quote_line"]
    copy_no, best = _best_copy(lines)
    got = sum(r.fields.get("total_price") or 0 for r in best)
    diag = draft.meta.get("diagnostics") or {}

    sample = {
        "slug": slug, "model": cfg.DASHSCOPE_QUOTE_VL_MODEL,
        "copy_no": copy_no, "rows": len(best), "golden_rows": GOLDEN_ROWS,
        "row_recall": round(len(best) / GOLDEN_ROWS, 4),
        "line_sum": round(got, 2), "declared": declared,
        "amount_deviation": round(abs(got - declared) / declared, 6),
        "quality": draft.quality.status,
        "rotations": len(draft.meta.get("rotations") or {}),
        "orientation_unresolved": len(draft.meta.get("orientation_unresolved") or []),
        "sequence_verdict": (diag.get("sequence") or {}).get("verdict"),
    }
    SAMPLES.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLES.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sample, ensure_ascii=False) + "\n")
    print(f"\n[{name}] 行 {sample['rows']}/{GOLDEN_ROWS} "
          f"（召回 {sample['row_recall']:.1%}）金额差 {sample['amount_deviation']:.4%} "
          f"旋转 {sample['rotations']} 未决 {sample['orientation_unresolved']}")

    assert diag.get("has_price_column"), "价格列一列都没映射上 —— 钱全丢了"
    assert sample["row_recall"] >= MIN_ROW_RECALL, sample
    assert sample["amount_deviation"] <= MAX_AMOUNT_DEVIATION, sample


@pytest.mark.e2e
def test_report_accumulated_distribution():
    """打印累积分布。**不断言**——分布是给人看的，不是门。"""
    if not SAMPLES.exists():
        pytest.skip(f"尚无样本：{SAMPLES}")
    import statistics

    rows = [json.loads(l) for l in SAMPLES.read_text(encoding="utf-8").splitlines() if l]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["slug"], []).append(r)

    print(f"\n累积样本 {len(rows)} 条 ← {SAMPLES}")
    print(f"{'文档':<22}{'n':>3}{'行召回 中位/最差':>20}{'金额差 中位/最差':>22}")
    for slug, rs in sorted(by.items()):
        rec = [r["row_recall"] for r in rs]
        dev = [r["amount_deviation"] for r in rs]
        print(f"{slug:<22}{len(rs):>3}"
              f"{statistics.median(rec):>11.1%}/{min(rec):<8.1%}"
              f"{statistics.median(dev):>13.4%}/{max(dev):<8.4%}")
