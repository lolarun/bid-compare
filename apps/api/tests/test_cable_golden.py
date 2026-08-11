"""电缆四文件 golden 回归 —— 七个验收用例（doc/19 §5）。

Golden 来源：客户提供的参考 CSV，经 scripts/build_cable_golden.py 审计后落盘到
data/golden/quote_cable_*.json。审计口径见该脚本；四份均为 audit_status=clean。

三层，**不得互相冒充**（`.claude/rules/tests.md` 第一条）：

  A 层（1–3）本文件  golden 自身可信度。纯数据校验，无 API，始终执行。
  B 层（4–7）本文件  **确定性**重放：同一份 CSV 恒产出同一个 draft、解析不吞行、
                     结构门按基线触发。不看 golden，无 API。
  C 层  test_cable_accuracy_e2e.py  **准确率** vs golden。标 @e2e，默认不跑，
                     报累积分布而非单次红绿。

2026-08-10 两次调整：
  · B 层基线从 legacy（OCR→HTML→TableGrid）改为 VL-direct。
  · B 层由"断言准确率"改为"断言确定性"。原因：准确率跑在冻结快照上就是在测那一次
    抽签——实测亨通同图同提示词 temperature=0，两次抽取差 37,632 元（0.18%）；
    同一份代码红绿取决于冻住了哪一次。那不是门。准确率因此移入 C 层。

录制快照：python scripts/record_vl_snapshots.py --all
刷新基线：python scripts/record_vl_snapshots.py --refresh-expected   # 不打模型
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO / "data" / "golden"
VL_SNAP_DIR = REPO / "tests" / "fixtures" / "vl_snapshots"

CABLE_DOCS = {
    "quote_cable_pudong": ("上海浦东", 20629762.68),
    "quote_cable_hengtong": ("亨通", 20966959.43),
    "quote_cable_hongsheng": ("宏胜", 20597048.33),
    "quote_cable_yuandong": ("远东", 20014715.08),
}
EXPECTED_ROWS = 136


def _golden(slug: str) -> dict:
    path = GOLDEN_DIR / f"{slug}.json"
    if not path.exists():
        pytest.skip(f"golden 缺失：{path}；先跑 scripts/build_cable_golden.py")
    return json.loads(path.read_text(encoding="utf-8"))


def _replay(slug: str):
    """从 VL 快照重放出 ExtractionDraft。

    重放的**被测对象不是模型**——模型输出本身不确定（同份同配置的行数、页码、
    金额都会变）。被测的是它下游那条确定性链路：CSV 解析 → 列名映射 → 结构门 →
    ExtractionDraft。故快照只冻住模型给的两样东西（原始 CSV + 那次的旋转表），
    重放直接从 `build_draft` 进入，不重新渲染 PDF：渲染是确定的，且模型看到了
    什么已经固化在 CSV 里。
    """
    snap_path = VL_SNAP_DIR / f"{slug}.json"
    if not snap_path.exists():
        pytest.skip(
            f"VL 快照缺失：{snap_path}。B 层验证的是真实识别链路，没有快照就无法"
            f"判定，不得视为通过。录制："
            f"python scripts/record_vl_snapshots.py --doc {CABLE_DOCS[slug][0]}"
        )
    snap = json.loads(snap_path.read_text(encoding="utf-8"))

    from apps.api.intelligence.vl_direct import PROMPT_QUOTE_CSV, build_draft

    # 提示词变了 → **失败而不是跳过**。旧快照是旧格式的输入，拿它验证新解析器，
    # 测试还绿着但验证的东西已经不存在了（.claude/rules/tests.md：replay 缓存
    # miss 必须使测试失败，禁止假绿）。
    want = hashlib.sha256(PROMPT_QUOTE_CSV.encode("utf-8")).hexdigest()[:16]
    assert snap.get("prompt_sha256") == want, (
        f"快照录于另一版提示词（{snap.get('prompt_sha256')} != {want}），"
        f"重放它等于验证一个已经不存在的输入格式。重新录制："
        f"python scripts/record_vl_snapshots.py --doc {CABLE_DOCS[slug][0]}"
    )

    return build_draft(
        snap["csv"],
        file_path=snap["pdf"],
        page_count=snap["page_count"],
        processed_pages=snap["processed_pages"],
        rotations={int(k): v for k, v in (snap.get("rotations") or {}).items()},
        unresolved_pages=snap.get("unresolved_pages") or [],
    )


def _recognized(slug: str) -> list[dict]:
    """一套报价的明细行。

    VL-direct 的提示词**要求输出全部副本**（正本/副本、汇总/明细都照实输出，
    合并或丢弃都是在销毁证据）。因此"取哪一套"是消费方的职责，不是识别器的。

    这里按 `copy_no` 取**序号最小的那一套**——确定性、且不看答案。不按"与声明
    总价最接近"选：那是拿被测的结论去挑被测的数据，选出来再断言它对，是循环的。
    """
    draft = _replay(slug)
    lines = [r for r in draft.rows if r.row_type == "quote_line"]
    copies = sorted({(r.fields.get("copy_no") or "").strip() for r in lines})
    if len(copies) > 1:
        first = copies[0] if copies[0] else (copies[1] if len(copies) > 1 else "")
        lines = [r for r in lines
                 if (r.fields.get("copy_no") or "").strip() == first]
    # validation_flags 是 DraftRow 的属性而非 fields 的键，用例 7 要读它
    return [dict(r.fields, validation_flags=list(r.validation_flags)) for r in lines]


# ── A 层：golden 自身可信度 ──────────────────────────────────────────────────

@pytest.mark.parametrize("slug", CABLE_DOCS)
def test_case1_golden_has_136_continuous_rows(slug):
    """用例 1：四份参考各 136 条明细，序号连续 1..136。"""
    g = _golden(slug)
    assert g["row_count"] == EXPECTED_ROWS
    assert [r["seq"] for r in g["rows"]] == [str(i) for i in range(1, EXPECTED_ROWS + 1)]


@pytest.mark.parametrize("slug", CABLE_DOCS)
def test_case2_golden_line_sum_matches_declared_total(slug):
    """用例 2：明细合价之和 = 官方总价（宏胜允许 0.04 元舍入累计）。"""
    g = _golden(slug)
    expected_total = CABLE_DOCS[slug][1]
    assert g["declared_total"] == expected_total
    tolerance = 0.05 if slug == "quote_cable_hongsheng" else 0.001
    assert abs(g["line_sum"] - expected_total) <= tolerance, g["line_sum_minus_declared"]


def test_case3_known_exceptions_are_recorded_not_smoothed():
    """用例 3：三处已知边界必须原样保留，不得被抹平。

    · 亨通第 112 项原文为 "/" → 单价与合价均为空
    · 宏胜逐行合价累加比官方总价多 0.04 元（舍入）
    · 上海浦东第 114 项合价 = 2 × 数量 × 单价（报单根价，其余三家报双根合价）
    """
    ht = {r["seq"]: r for r in _golden("quote_cable_hengtong")["rows"]}["112"]
    assert ht["unit_price"] is None and ht["total_price"] is None

    hs = _golden("quote_cable_hongsheng")
    assert abs(hs["line_sum_minus_declared"] - 0.04) < 1e-6

    pd_row = {r["seq"]: r for r in _golden("quote_cable_pudong")["rows"]}["114"]
    assert pd_row["implied_multiplier"] == 2.0
    for other in ("quote_cable_hengtong", "quote_cable_hongsheng", "quote_cable_yuandong"):
        row = {r["seq"]: r for r in _golden(other)["rows"]}["114"]
        assert row["spec"] == pd_row["spec"], "规格相同"
        assert row["implied_multiplier"] == 1.0, "口径不同 → 单价不可直接横向比"


# ── B 层：确定性重放 ─────────────────────────────────────────────────────────
#
# **这一层不看 golden。** 用例 4-7 原本断言"识别结果对得上 golden"——那是准确率，
# 而准确率跑在冻结快照上就变成了"那一次抽签中没中"：实测亨通同图同提示词
# temperature=0，两次抽取差 37,632 元（0.18%）。同一份代码红还是绿取决于冻住了
# 哪一次，那不是门。
#
# `.claude/rules/tests.md`：snapshot replay 验证**确定性**，fresh E2E 验证真实
# 模型链路，三者不得互相冒充。故准确率移到 C 层（test_cable_accuracy_e2e.py），
# 这里只守确定性链路：CSV → 解析 → 列名映射 → 结构门 → ExtractionDraft。


def _expected(slug: str) -> dict:
    snap_path = VL_SNAP_DIR / f"{slug}.json"
    if not snap_path.exists():
        pytest.skip(f"VL 快照缺失：{snap_path}")
    exp = json.loads(snap_path.read_text(encoding="utf-8")).get("expected")
    assert exp, (f"{slug} 快照缺 expected 块。刷新："
                 f"python scripts/record_vl_snapshots.py --refresh-expected")
    return exp


@pytest.mark.parametrize("slug", CABLE_DOCS)
def test_case4_replay_is_deterministic(slug):
    """同一份 CSV 重放两次必须产出完全相同的 draft。

    这是整条链路可重放的**前提**：不成立的话，下面所有基线比对都失去意义，
    快照也不再能重现任何东西。
    """
    def fingerprint(d):
        return [
            (r.row_index, r.row_type, r.source_ref.page,
             sorted(r.validation_flags), sorted(r.fields.items(), key=lambda kv: kv[0]))
            for r in d.rows
        ]

    a, b = _replay(slug), _replay(slug)
    assert fingerprint(a) == fingerprint(b)
    assert a.quality.status == b.quality.status
    assert sorted(a.quality.blocking_reasons or []) == sorted(b.quality.blocking_reasons or [])


@pytest.mark.parametrize("slug", CABLE_DOCS)
def test_case5_no_rows_lost_between_csv_and_draft(slug):
    """CSV 有几行数据，draft 就必须有几行 —— 解析过程不得静默吞行。

    这是**真不变量**，不是特征化基线：与模型抽得准不准无关，只要解析器丢了行
    就一定错。空行不算（`parse_csv` 明确跳过）。
    """
    import csv as _csv
    import io as _io

    snap = json.loads((VL_SNAP_DIR / f"{slug}.json").read_text(encoding="utf-8"))
    rows = [r for r in _csv.reader(_io.StringIO(snap["csv"]))
            if any((c or "").strip() for c in r)]
    csv_data_rows = max(len(rows) - 1, 0)          # 去掉表头
    assert len(_replay(slug).rows) == csv_data_rows


@pytest.mark.parametrize("slug", CABLE_DOCS)
def test_case6_gates_fire_as_recorded(slug):
    """结构门与质量判定必须与基线一致。

    这是**特征化基线**：钉住的是当前行为（含当前缺陷），作用不是"证明结果正确"，
    而是"结果一旦变化必须有人知道"。有意改进时用
    `scripts/record_vl_snapshots.py --refresh-expected` 显式刷新——**不打模型**，
    只重放已冻结的 CSV，所以刷新是零成本且可审阅的（diff 就是行为变化本身）。
    """
    import collections

    d = _replay(slug)
    exp = _expected(slug)
    diag = d.meta.get("diagnostics") or {}

    assert d.quality.status == exp["quality_status"]
    assert sorted(d.quality.blocking_reasons or []) == exp["blocking_reasons"]
    assert dict(collections.Counter(r.row_type for r in d.rows)) == exp["row_counts"]
    for k, v in exp["alignment"].items():
        assert (diag.get("alignment") or {}).get(k) == v, f"alignment.{k}"
    for k, v in exp["sequence"].items():
        assert (diag.get("sequence") or {}).get(k) == v, f"sequence.{k}"
    assert diag.get("has_price_column") == exp["has_price_column"]
    assert diag.get("rows_without_page") == exp["rows_without_page"]
    assert sum(1 for r in d.rows
               if "column_shift" in r.validation_flags) == exp["column_shift_rows"]


@pytest.mark.parametrize("slug", CABLE_DOCS)
def test_case7_no_silently_derived_totals(slug):
    """零静默派生 —— 合价必须来自原文，不得由 qty×单价 补出。

    与准确率无关，是不变量：派生值既凭空造钱，又让算术校验恒成立、把列错位行洗白
    （见 test_derived_total_guard.py）。
    """
    derived = [r for r in _recognized(slug)
               if "derived_total" in (r.get("validation_flags") or [])]
    assert derived == [], f"{len(derived)} 行合价是系统补出来的"
