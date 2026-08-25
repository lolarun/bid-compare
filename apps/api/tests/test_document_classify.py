"""design/28 §3 Tier 0 分类器——对全量 MANIFEST 语料的验收测试。

MANIFEST.md 是标注答案集，不是这份测试自己的私有夹具清单：新增/替换语料时
先改 MANIFEST，再改这里，两边必须一直对得上，不然"验收覆盖了全量语料"这
句话就是假的。

金桥招标 xlsx 的"不确定"不是失败用例、是通过用例（design/28 §2）——它是
唯一一个 verdict 断言为 "uncertain" 的样本，别的样本判成 uncertain 才是
真失败。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.intelligence.document_classify import (
    classify_excel,
    classify_pdf,
    classify_tier0,
)

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "tests" / "fixtures" / "documents"


def _skip_if_missing(path: Path):
    if not path.exists():
        pytest.skip(f"夹具缺失：{path}")


# ── Excel：definitive / strong / ambiguous 三档都要覆盖真实语料 ────────────

EXCEL_CASES = [
    # (相对路径, 期望 verdict, 期望 confidence, 备注)
    ("徐汇区华泾镇项目-采购清单.xlsx", "tender_list", "definitive",
     "0 价格列——空白清单表的定义性特征"),
    # 2026-08-23 改判：这份从 "uncertain" 变成 "tender_list/definitive"。
    # 实测它的价格列**一个真实价格都没有**——`单价(不含税)` 整列空，
    # `合计(不含税)`/`税额`/`价税合计` 三列的非空取值无一例外全是 "0"。
    # 旧判据把逐格的 "0" 算成"填了"（那条逐格判据本身没错），于是算出 63%
    # 填充率判成不确定；然后弹窗让人二选一，取消键默认落到"投标文件"，
    # **采购清单就这样变成了比价矩阵里的一列供应商**（0/89、合计 ¥0）。
    # 那不是置信度问题，是类别错误。两条判据因此各补一半：
    #   ① 整列全零的价格列不算价格列（单点的零可以是事实，整列的零是没信息）；
    #   ② 补上"有价格表头但格子几乎全空 → 空白清单表"这一支——原来只有
    #      "几乎全满→报价单"和"压根没有价格列→清单"，最典型的形态反而没有归宿。
    ("金桥地体上盖项目-采购清单.xlsx", "tender_list", "definitive",
     "价格列存在但一个真实价格都没有（整列空 + 整列 0）——空白清单表"),
    ("金桥地体上盖项目-凯硕新正报价清单.xlsx", "bid_list", "strong", "design/28 §2 引用的 100% 参照"),
    ("金桥地体上盖项目-上海绵存报价清单.xlsx", "bid_list", "strong", ""),
    ("金桥地体上盖项目-泰科龙报价清单.xlsx", "bid_list", "strong", ""),
]


@pytest.mark.parametrize("rel_path,expected_verdict,expected_confidence,note", EXCEL_CASES)
def test_excel_tier0_matches_manifest(rel_path, expected_verdict, expected_confidence, note):
    path = DOCS / rel_path
    _skip_if_missing(path)
    result = classify_excel(str(path))
    assert result.verdict == expected_verdict, (
        f"{rel_path}: 期望 verdict={expected_verdict}，实得 {result.verdict}"
        f"（fill_rate={result.fill_rate}，price_columns={result.price_columns}）。{note}"
    )
    assert result.confidence == expected_confidence


def _synthetic_half_filled(tmp_path, filled_rows: int, total_rows: int):
    """造一份价格**真的填了一半**的表——不是全 0、也不是全空，是货真价实的模糊。

    金桥那份原本充当这个角色，2026-08-23 改判之后它不再模糊（它的价格列一个
    真实价格都没有）。`uncertain` 这一支必须继续有语料覆盖，否则"低置信必须
    标注"这条红线就没人守了；真实语料里暂时没有这种样本，就合成一份——**合成
    的是判据边界，不是业务事实**，这跟拿手搓数据冒充真实语料是两回事。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["序号", "名称", "单位", "数量", "单价", "合价"])
    for i in range(1, total_rows + 1):
        priced = i <= filled_rows
        ws.append([i, f"闸阀 DN{i}", "个", 2,
                   100.0 + i if priced else "", (100.0 + i) * 2 if priced else ""])
    p = tmp_path / "半填.xlsx"
    wb.save(p)
    return p


def test_genuinely_half_filled_prices_stay_uncertain(tmp_path):
    """红线（design/28 §5 red line 3）：介于清单和报价单之间就必须说"不确定"，
    不许朝任何一边猜。"""
    r = classify_excel(str(_synthetic_half_filled(tmp_path, 25, 50)))
    assert (r.verdict, r.confidence) == ("uncertain", "ambiguous"), r.reason


def test_verdict_is_deterministic_across_runs(tmp_path):
    """同一份文件反复跑，verdict/confidence/fill_rate 必须逐次相同——不能因为
    浮点比较边界抖动，也不能靠某次运气对上真实答案就悄悄升级成 definitive。"""
    p = _synthetic_half_filled(tmp_path, 25, 50)
    results = [classify_excel(str(p)) for _ in range(3)]
    assert len({(r.verdict, r.confidence, r.fill_rate) for r in results}) == 1


def test_all_zero_price_column_is_not_a_filled_price_column(tmp_path):
    """整列全 0 = 占位，不是报价。逐格判据（"0" 算填了）保持不变，这里加的是
    列级判据——金桥采购清单正是靠这一条从"不确定"回到"采购清单"的。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["序号", "名称", "单位", "数量", "单价", "合价"])
    for i in range(1, 30):
        ws.append([i, f"球阀 DN{i}", "个", 3, 0, 0])
    p = tmp_path / "全零.xlsx"
    wb.save(p)
    r = classify_excel(str(p))
    assert r.verdict == "tender_list", r.reason


def test_excel_definitive_has_no_price_columns():
    path = DOCS / "徐汇区华泾镇项目-采购清单.xlsx"
    _skip_if_missing(path)
    result = classify_excel(str(path))
    assert result.price_columns == []
    assert result.fill_rate is None  # 没有价格列，填充率概念本身不适用，不是 0.0


def test_excel_strong_fill_rate_above_threshold():
    from apps.api.intelligence.document_classify import FILL_RATE_STRONG
    path = DOCS / "金桥地体上盖项目-凯硕新正报价清单.xlsx"
    _skip_if_missing(path)
    result = classify_excel(str(path))
    assert result.fill_rate is not None and result.fill_rate >= FILL_RATE_STRONG
    assert len(result.price_columns) >= 1


# ── PDF：Tier 0 只给文字层信号，不判招标/投标 ──────────────────────────────

PDF_TEXT_LAYER_CASES = [
    ("金桥地体上盖项目-招标文件.pdf", "native", "文本型招标 PDF（E2E_FIXTURES.md 记录 ~11771 字）"),
    ("徐汇区华泾镇项目-招标文件.pdf", "native", ""),
    ("金桥地体上盖项目-上海绵存投标文件.pdf", "scanned", "纯扫描件，无文字层"),
    ("金桥地体上盖项目-凯硕新正投标文件.pdf", "scanned", ""),
    ("金桥地体上盖项目-泰科龙投标文件.pdf", "scanned", "53 页扫描件，另有转置表/旋转已知坑，见 MANIFEST"),
    ("徐汇区华泾镇项目-上海浦东投标文件.pdf", "scanned", ""),
    ("徐汇区华泾镇项目-亨通投标文件.pdf", "scanned", ""),
    ("徐汇区华泾镇项目-宏胜投标文件.pdf", "scanned", ""),
    ("徐汇区华泾镇项目-远东投标文件.pdf", "scanned", ""),
]


@pytest.mark.parametrize("rel_path,expected_layer,note", PDF_TEXT_LAYER_CASES)
def test_pdf_tier0_text_layer_matches_manifest(rel_path, expected_layer, note):
    path = DOCS / rel_path
    _skip_if_missing(path)
    result = classify_pdf(str(path))
    assert result.text_layer == expected_layer, (
        f"{rel_path}: 期望 text_layer={expected_layer}，实得 {result.text_layer}。{note}"
    )


def test_pdf_tier0_never_decides_tender_vs_bid():
    """Tier 0 对 PDF 的输出类型本身就不含"招标/投标"这个字段——这条测试
    与其说是断言行为，不如说是锁定 schema：PdfClassification 只能长出
    text_layer 这类结构信号，谁往这个 dataclass 上加 verdict 字段就是在
    悄悄把 Tier 1/2 的职责挪进 Tier 0，必须先改设计文档再改代码。"""
    from apps.api.intelligence.document_classify import PdfClassification
    field_names = {f for f in PdfClassification.__dataclass_fields__}
    assert "verdict" not in field_names
    assert "type" not in field_names


# ── classify_tier0：扩展名分派 + MANIFEST 全量覆盖 ─────────────────────────

def test_classify_tier0_dispatches_by_extension():
    xlsx = DOCS / "金桥地体上盖项目-凯硕新正报价清单.xlsx"
    pdf = DOCS / "金桥地体上盖项目-凯硕新正投标文件.pdf"
    _skip_if_missing(xlsx)
    _skip_if_missing(pdf)
    from apps.api.intelligence.document_classify import ExcelClassification, PdfClassification
    assert isinstance(classify_tier0(str(xlsx)), ExcelClassification)
    assert isinstance(classify_tier0(str(pdf)), PdfClassification)


def test_classify_tier0_unsupported_extension_returns_none(tmp_path):
    p = tmp_path / "not_a_document.txt"
    p.write_text("irrelevant")
    assert classify_tier0(str(p)) is None


def test_classify_tier0_accepts_csv():
    """CSV 报价清单必须能过 Tier 0。

    2026-08-23 之前 `.csv` 落在"其他扩展名"里返回 None，路由据此答
    `kind="unsupported"`，界面显示「不支持的文件类型」——**而下游一直读得
    好好的**（`extract_quote_tabular` 实测每份 136 项）。用户拖四份报价 CSV
    进来，四张卡片全是红的。
    """
    csvs = sorted(DOCS.glob("*.csv"))
    if not csvs:
        pytest.skip("语料目录里没有 CSV 夹具")
    from apps.api.intelligence.document_classify import ExcelClassification
    for f in csvs:
        r = classify_tier0(str(f))
        assert isinstance(r, ExcelClassification), (
            f"{f.name} 分类返回 {type(r).__name__}——CSV 判据与 xlsx 同源，"
            "应当返回 ExcelClassification（前端按 kind === 'excel' 分流）")
        assert r.verdict != "unsupported"


def _manifest_filenames() -> set[str]:
    """MANIFEST.md 的 Files 表里登记在案的文件名。

    只认**表格行的首格**（`| \\`名字.ext\\` |`）。早一版在全文搜反引号，把正文里
    顺带提到的 `*.xlsx`、`docs/test/...` 之类也抓了进来——答案集的边界必须自己
    说清楚，不能靠"文中出现过"这种模糊判据。
    """
    import re
    text = (DOCS / "MANIFEST.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*`([^`*]+\.(?:xlsx|xls|pdf|csv))`\s*\|",
                          text, re.M))


def test_manifest_corpus_full_coverage():
    """cut 2 的验收口径（design/28 §7）：unit tests over the whole MANIFEST corpus。
    逐一跑 Tier 0，不遗漏、不静默跳过。

    **对照的是 MANIFEST 本身，不是一个写死的数字**（2026-08-23 改）。原来这里
    断言"应当有 N 份"，于是语料一增减就得改两处，而这两处一旦不同步，测试要么
    误报要么被顺手把数字改大——后者等于把答案集悄悄放宽。现在两边直接比集合：
    目录里多出的文件必须先登记进 MANIFEST，MANIFEST 里写着的文件必须真的在。

    **2026-08-23 另一处：`*.csv` 补进 glob。** 在此之前这条"全量覆盖"测试只 glob
    xlsx/xls/pdf——它自己把唯一坏掉的那个扩展名排除在外了，于是"全量覆盖"绿着，
    而真实界面把四份 CSV 全拒了。覆盖面是测试自己声明的，声明得比实际窄，绿色
    就没有意义。
    """
    all_files = (sorted(DOCS.glob("*.xlsx")) + sorted(DOCS.glob("*.xls"))
                 + sorted(DOCS.glob("*.pdf")) + sorted(DOCS.glob("*.csv")))
    if not all_files:
        pytest.skip("MANIFEST 语料目录不存在（未在这套环境里签出真实夹具）")
    on_disk = {f.name for f in all_files}
    recorded = _manifest_filenames()
    assert on_disk == recorded, (
        f"目录与 MANIFEST 对不上。多出未登记：{sorted(on_disk - recorded)}；"
        f"登记了但找不到：{sorted(recorded - on_disk)}。"
        "语料增减必须先改 MANIFEST.md——它是标注答案集，不是事后补的清单。"
    )
    for f in all_files:
        result = classify_tier0(str(f))
        assert result is not None, f"{f} 分类返回 None——扩展名分派没覆盖到这份真实语料"
