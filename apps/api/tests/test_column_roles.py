"""列→角色映射：词表 / 确定性验证 / 模型兜底（docs/design/40 §5）。

**这份测试全程离线。** 模型调用一律用桩函数——要验证的是"验证器答不答应"和
"接线对不对"，不是"某个模型今天答得准不准"。桩返回什么由测试指定，因此
"模型答错会被挡下"这条能被**正面断言**，而不是碰运气碰不到。

夹具从真实文件生成（改表头、不改数据），不手搓假数据：判据要面对的是真实的
数值分布（空价格列、按根报价的倍率行、原文空洞），手搓的整洁数据验不出这些。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from apps.api.intelligence.column_roles import (
    ROLE_LABELS, _parse_llm_roles, propose_by_llm, verify_roles,
)

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "tests" / "fixtures" / "documents"
REAL_CSV = DOCS / "徐汇区华泾镇项目-宏胜报价清单.csv"


def _read_real() -> tuple[list[str], list[list[str]]]:
    if not REAL_CSV.exists():
        pytest.skip(f"缺夹具：{REAL_CSV.name}")
    with open(REAL_CSV, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh)]
    return rows[0], rows[1:]


# ── 验证器：只认证据，不认提议来源 ──────────────────────────────────────────

def test_correct_mapping_passes():
    header, rows = _read_real()
    roles = {"name": header.index("材料/设备名称"), "spec": header.index("规格型号"),
             "unit": header.index("计量单位"), "quantity": header.index("数量"),
             "unit_price": header.index("单价"), "total_price": header.index("合价")}
    v = verify_roles(roles, rows)
    assert v.ok, v.reasons


def test_quantity_and_unit_price_swap_is_NOT_caught():
    """**如实记录一条真实局限**：数量与单价对调，验证器抓不出来。

    乘法可交换，`数量×单价` 与 `单价×数量` 逐位相同，闭合率一点不变；两列又
    都是数字，类型判据也没话说。写这条测试时最初的断言是"算术能抓"——跑一遍
    就被推翻了，这里把真相钉住，免得以后有人读着验证器的文档以为它全都能挡。

    单文件层面没有独立证据能证伪它（数量不总是整数：实测 1905.25、2882.94）。
    真能证伪的是跨供应商证据——同一条目各家数量必须相同、单价必须不同——但那
    要到对齐阶段才拿得到。风险因此在接线层收窄：`resolve_columns` 只在词表对
    某个角色毫无意见时才让模型填它（`_only_missing`）。
    """
    header, rows = _read_real()
    roles = {"name": header.index("材料/设备名称"),
             "quantity": header.index("单价"),          # ← 对调
             "unit_price": header.index("数量"),        # ← 对调
             "total_price": header.index("合价")}
    assert verify_roles(roles, rows).ok, (
        "如果这条开始失败，说明有了新的判据能抓住数量↔单价对调——"
        "那是好事，请把新判据写进 verify_roles 的文档并改掉这条测试的说法")


def test_wrong_total_column_IS_caught_by_arithmetic():
    """合价列认错——算术抓得住，这才是算术闸门真正的守备范围。"""
    header, rows = _read_real()
    roles = {"name": header.index("材料/设备名称"),
             "quantity": header.index("数量"),
             "unit_price": header.index("单价"),
             "total_price": header.index("单价")}      # ← 合价指到了单价列
    v = verify_roles(roles, rows)
    assert not v.ok
    assert any("认错了" in r for r in v.reasons), v.reasons


def test_llm_may_not_overwrite_a_role_the_keyword_table_already_found():
    """接线层的收窄闸：词表缺角色时模型只准填空，**不准改已认出的角色**。

    这是"数量↔单价对调"在验证器抓不住之后的补偿——不是靠更多验证，是靠不给
    模型改写的机会。
    """
    from apps.api.services.ingestion.tabular_ingestion import _merge_proposal
    kw = {"name": 1, "quantity": 3}
    proposed = {"name": 1, "quantity": 5, "unit_price": 4}   # 想把数量改到第 5 列
    merged = _merge_proposal(kw, proposed, missing_only=True)
    assert merged["quantity"] == 3, "词表认出的数量列被模型改掉了"
    assert merged["unit_price"] == 4, "模型没能填上词表缺的角色"
    # 证据打架时是另一回事：词表的答案已被数据证伪，整份换成模型的。
    assert _merge_proposal(kw, proposed, missing_only=False) == proposed


def test_name_pointed_at_a_numeric_column_is_caught():
    header, rows = _read_real()
    roles = {"name": header.index("数量"), "quantity": header.index("数量")}
    v = verify_roles(roles, rows)
    assert not v.ok
    assert any("纯数字" in r for r in v.reasons), v.reasons


def test_missing_required_role_is_caught():
    header, rows = _read_real()
    v = verify_roles({"name": header.index("材料/设备名称")}, rows)
    assert not v.ok
    assert any("数量" in r for r in v.reasons), v.reasons


def test_empty_price_column_is_no_evidence_not_failure():
    """采购清单的价格列**按定义**是空的（design/28 §2：空白表正是它的判据）。

    早一版把"整列为空"算成"0% 能解析成数"，于是每一份合格的采购清单都验不过、
    都会被推去问模型——而模型也答不出一个不存在的列。没有证据就是没有证据。
    """
    header = ["序号", "名称", "单位", "数量", "单价", "合价"]
    rows = [["1", "闸阀", "个", "10", "", ""], ["2", "球阀", "个", "5", "", ""]]
    v = verify_roles({"name": 1, "unit": 2, "quantity": 3,
                      "unit_price": 4, "total_price": 5}, rows)
    assert v.ok, v.reasons
    assert v.evidence["numeric:unit_price"] == "empty"


# ── 模型提议的净化 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ('{"name":1,"quantity":3}', {"name": 1, "quantity": 3}),
    ('随便说两句然后 {"name": 1, "quantity": 3} 收尾', {"name": 1, "quantity": 3}),
    ('{"name":1,"不存在的角色":2}', {"name": 1}),          # 未知角色丢弃
    ('{"name":1,"quantity":99}', {"name": 1}),             # 越界下标丢弃
    ('{"name":1,"spec":1}', {"name": 1}),                  # 同一列被两个角色认领
    ('{"name":"材料名称"}', None),                          # 下标不是整数
    ('不是 JSON', None),
])
def test_llm_reply_is_sanitised(raw, expect):
    """净化不是防御性编程的仪式：越界下标会让解析崩在跟列映射毫无关系的地方，
    未知角色名会静默污染下游字段，一列被两个角色认领会让取值自相矛盾。"""
    assert _parse_llm_roles(raw, n_cols=6) == expect


def test_proposer_survives_a_broken_call():
    """模型不可用不该打断解析——调用方会退回词表结果。"""
    def _boom(_prompt):
        raise RuntimeError("网络炸了")
    assert propose_by_llm(["a", "b"], [["1", "2"]], _boom) is None


def test_prompt_carries_the_header_and_the_role_vocabulary():
    seen = {}

    def _capture(prompt):
        seen["p"] = prompt
        return "{}"

    propose_by_llm(["序号", "物资描述", "用量"], [["1", "闸阀 DN50", "10"]], _capture)
    assert "物资描述" in seen["p"] and "用量" in seen["p"]
    for role in ("unit_price_excl_tax", "total_price_incl_tax", "tax_rate"):
        assert role in seen["p"], f"角色词表没进提示词：{role}"
    # 生产提示词禁止出现真实供应商/项目/文件名（CLAUDE.md §4）。
    for banned in ("宏胜", "亨通", "泰科龙", "凯硕", "绵存", "徐汇", "金桥", "浦东", "远东"):
        assert banned not in seen["p"], f"提示词里混进了真实语料名：{banned}"


# ── 端到端：词表不认识的表头，模型能不能救回来 ──────────────────────────────

# 把真实表头换成词表**确实认不出**的同义写法。选词的依据是逐条对着
# `_TABULAR_COLUMN_PATTERNS` 验过的，不是随手编的：`物资描述` 撞不上
# 名称/品名/材料名称/设备名称，`用量` 撞不上 数量/工程量。
_UNKNOWN_HEADERS = {"材料/设备名称": "物资描述", "数量": "用量"}


def _renamed_csv(tmp_path: Path) -> Path:
    header, rows = _read_real()
    out = tmp_path / "改了表头的报价清单.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([_UNKNOWN_HEADERS.get(h, h) for h in header])
        w.writerows(rows)
    return out


def test_keyword_table_really_fails_on_unknown_headers(tmp_path):
    """先证明这份夹具**确实**难倒了词表——否则下一条测试就是在自说自话。"""
    from apps.api.services.ingestion.tabular_ingestion import (
        _detect_tabular_columns, _load_dataframe, _sample_rows,
    )
    df = _load_dataframe(str(_renamed_csv(tmp_path)))
    cols = [str(c) for c in df.columns.tolist()]
    kw = _detect_tabular_columns(cols)
    assert not kw.get("name") and not kw.get("quantity")
    v = verify_roles({k: cols.index(x) for k, x in kw.items() if x in cols},
                     _sample_rows(df))
    assert not v.ok


def test_llm_recovers_the_same_parse_on_unknown_headers(tmp_path, monkeypatch):
    """词表认不出 → 模型提议 → 过验证 → 解析结果与原文件**逐项相同**。

    这是"泛化能力"这四个字的可执行版本：换一份没见过的表头写法，不改代码，
    出来的数据一样。桩函数保证离线可复现——要验的是接线，不是模型今天的手感。
    """
    from apps.api.services.ingestion import tabular_ingestion as ti

    renamed = _renamed_csv(tmp_path)
    calls: list[str] = []

    def _fake_client():
        def _call(prompt: str) -> str:
            calls.append(prompt)
            cols = [str(c) for c in ti._load_dataframe(str(renamed)).columns.tolist()]
            return json.dumps({"name": cols.index("物资描述"),
                               "quantity": cols.index("用量")})
        return _call

    monkeypatch.setattr(
        "apps.api.intelligence.paddle_doc_meta.get_text_client_call", _fake_client)

    got = ti.extract_quote_tabular(str(renamed), {"project_id": 0, "category": ""})
    assert got["_doc_meta"]["column_source"] == "llm", got["_doc_meta"]
    assert len(calls) == 1, "一张表只该问一次模型（design/40 §5：按表不按行）"

    baseline = ti.extract_quote_tabular(str(REAL_CSV), {"project_id": 0, "category": ""})
    assert baseline["_doc_meta"]["column_source"] == "keyword"
    keys = ("material", "spec", "qty", "unit_price", "total_price")
    assert ([{k: it.get(k) for k in keys} for it in got["items"]]
            == [{k: it.get(k) for k in keys} for it in baseline["items"]])


def test_known_corpus_never_calls_the_model(monkeypatch):
    """已知语料必须**一次模型都不调**——确定性、离线、可复现，是这套设计的底线。

    把客户端工厂换成会炸的桩：只要主路径碰它一下，这条就红。
    """
    from apps.api.services.ingestion import tabular_ingestion as ti

    def _explode():
        raise AssertionError("已知形状不该走到模型兜底")

    monkeypatch.setattr(
        "apps.api.intelligence.paddle_doc_meta.get_text_client_call", _explode)
    for f in sorted(DOCS.glob("*.xlsx")) + sorted(DOCS.glob("*.csv")):
        r = ti.extract_quote_tabular(str(f), {"project_id": 0, "category": ""})
        assert r["_doc_meta"]["column_source"] == "keyword", f.name


# ── 招标侧：连表头在第几行都得靠模型说 ─────────────────────────────────────

def _tender_xlsx(tmp_path: Path, header: list[str]) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["某某地块一期项目机电材料清单", "", "", ""])   # 标题行
    ws.append(header)
    for i, (name, qty) in enumerate([("闸阀 DN50", 12.5), ("球阀 DN25", 8),
                                     ("蝶阀 DN100", 3.25)], 1):
        ws.append([i, name, "个", qty])
    out = tmp_path / "清单.xlsx"
    wb.save(out)
    return out


def test_tender_keyword_path_needs_no_model(tmp_path, monkeypatch):
    from apps.api.services.tender import tender_list as tl

    def _explode():
        raise AssertionError("规范表头不该走到模型兜底")

    monkeypatch.setattr(
        "apps.api.intelligence.paddle_doc_meta.get_text_client_call", _explode)
    anchors = tl.parse_tender_xlsx(str(_tender_xlsx(tmp_path, ["序号", "名称", "单位", "数量"])))
    assert [a.name for a in anchors] == ["闸阀 DN50", "球阀 DN25", "蝶阀 DN100"]


def test_tender_unknown_header_falls_back_to_model_layout(tmp_path, monkeypatch):
    """表头写成词表不认识的字 → 连表头行都定位不到 → 模型给版式 → 过验证 → 同样的结果。"""
    from apps.api.services.tender import tender_list as tl

    path = _tender_xlsx(tmp_path, ["条目", "物资描述", "计量", "用量"])

    with pytest.raises(ValueError):        # 先证明词表确实认不出
        monkeypatch.setattr(
            "apps.api.intelligence.paddle_doc_meta.get_text_client_call", lambda: None)
        tl.parse_tender_xlsx(str(path))

    monkeypatch.setattr(
        "apps.api.intelligence.paddle_doc_meta.get_text_client_call",
        lambda: (lambda _p: json.dumps(
            {"header_row": 1, "roles": {"seq": 0, "name": 1, "unit": 2, "quantity": 3}})))
    anchors = tl.parse_tender_xlsx(str(path))
    assert [a.name for a in anchors] == ["闸阀 DN50", "球阀 DN25", "蝶阀 DN100"]
    assert [a.qty for a in anchors] == [12.5, 8, 3.25]


def test_tender_model_layout_must_pass_verification(tmp_path, monkeypatch):
    """模型把名称指到序号列——验证器该拦下，整份回到"找不到表头"的诚实报错，
    而不是解析出一列数字当品名。"""
    from apps.api.services.tender import tender_list as tl

    path = _tender_xlsx(tmp_path, ["条目", "物资描述", "计量", "用量"])
    monkeypatch.setattr(
        "apps.api.intelligence.paddle_doc_meta.get_text_client_call",
        lambda: (lambda _p: json.dumps(
            {"header_row": 1, "roles": {"name": 0, "quantity": 3}})))   # name → 序号列
    with pytest.raises(ValueError, match="表头"):
        tl.parse_tender_xlsx(str(path))


def test_role_vocabulary_is_the_single_source():
    """词表匹配、提示词、验证器三处都从 `ROLE_LABELS` 取角色名。这条断言把
    "谁也别自己另写一份"钉住——三份角色名迟早会漂移，而漂移的表现是某个角色
    静默地永远认不出来。"""
    from apps.api.services.ingestion.tabular_ingestion import _TABULAR_COLUMN_PATTERNS
    unknown = set(_TABULAR_COLUMN_PATTERNS) - set(ROLE_LABELS)
    assert not unknown, f"词表里有 ROLE_LABELS 不认识的角色：{unknown}"
