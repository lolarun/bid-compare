"""design/31 cut 4 收尾：导出永远拿不到预览数据。

设计文档原本写的是"预览态要把导出禁掉"。**核对真实代码后这个说法要修正**：
`get_bid_matrix_for_export` 是从库里重算的（`get_current_confirmed_session`
+ 已确认的 `used_submission_ids`），而预览数据从不落库，所以预览结果**物理
上不可能**流进导出。禁不禁按钮都不改变这一点。

那真正的风险是什么？是**反过来的**：用户屏幕上看着预览矩阵，点导出，拿到
一份用"已确认数据"算出来的、数字完全不同的表，而且没有任何提示。这才是要
挡的——挡的地方在前端（不让在预览态点导出），不在后端。

这份测试守的是后端那一半：证明"导出只认已确认数据"这个前提**继续成立**。
它是前端那道闸门的立论基础，哪天有人给导出加了"也带上草稿"的开关，
这里会先红。
"""
from __future__ import annotations

from sqlalchemy import func, select

from apps.api.models.bid_submission import BidSubmission
from apps.api.services.matrix.bid_export_service import get_bid_matrix_for_export
from apps.api.services.matrix.preview_service import build_preview_matrix
from apps.api.tests.test_compare_integration import compare_client  # noqa: F401
from apps.api.tests.test_preview_service import (  # noqa: F401
    _setup_unconfirmed,
    _upload_quote,
)


def test_export_sees_nothing_after_a_preview(compare_client, temp_db):
    """跑完预览之后立刻导出：库里一条报价都没有，导出必须照样什么也拿不到。

    如果这条挂了，说明预览把数据漏进库了——那是 preview_sandbox 的失效，
    后果比"导出多了几行"严重得多。

    **实测更正**：一开始这里断言的是"导出会拒绝"，实际不会。一条 submission
    都不存在时 `used_submission_ids` 为空且没有 active submission，硬闸门那
    一支不成立，导出**照常返回一份只有锚点、没有供应商的空表**。

    空表本身没错（招标清单确实在，只是没人报价）。但它把这轮真正要防的风险
    暴露得更清楚了：屏幕上摆着一份有 2 家供应商的预览矩阵，点导出拿到的是一
    张空表，**没有任何提示**。所以闸门必须加在前端——后端这一层能保证的只是
    "不泄漏"，保证不了"跟屏幕上一致"。
    """
    _engine, SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)

    result = build_preview_matrix(state["project_id"], "阀门", state["confirmations"])
    assert result.matrix["basis"] == "preview"
    assert result.matrix["rows"], "预览本身没跑通，这条测试就失去意义了"
    assert len(result.matrix["suppliers"]) == 2

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(BidSubmission)) == 0
        exported = get_bid_matrix_for_export(db, state["project_id"], "阀门", [])

    # 关键断言：预览里那 2 家一个都没进导出。
    assert exported["suppliers"] == [], "预览的供应商漏进了导出"
    priced = [
        c for row in exported["rows"] for c in (row.get("suppliers") or [])
        if isinstance(c, dict) and c.get("price") is not None
    ]
    assert priced == [], "预览的价格漏进了导出"


def test_export_only_reflects_confirmed_quotes(compare_client, temp_db):
    """预览里有 2 家，正式只确认 1 家 → 导出只能看到那 1 家。

    这是"导出=已确认口径"最直接的表述：同一个项目、同一批文件，预览和导出
    看到的东西不一样是**正确**行为，因为两者口径本来就不同。
    """
    _engine, SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)

    preview = build_preview_matrix(state["project_id"], "阀门", state["confirmations"])
    assert len(preview.matrix["suppliers"]) == 2

    # 只把第一家真正入库 + 对齐（走真实 HTTP 路径）
    first = state["confirmations"][0]
    r = compare_client.post("/api/quotes/batch-confirm", json=first.model_dump())
    assert r.status_code == 200, r.text
    sub_id = r.json()["submission_id"]
    r = compare_client.post("/api/analysis/tender-list/match", data={
        "project_id": str(state["project_id"]), "category": "阀门",
        "supplier_ids": "", "submission_ids": str(sub_id),
    })
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        exported = get_bid_matrix_for_export(db, state["project_id"], "阀门", [])

    assert len(exported["suppliers"]) == 1, (
        "导出看到了没确认过的供应商——导出口径被污染了")
    # 导出产物不带预览标记：它本来就不是预览。
    assert exported.get("basis", "official") == "official"
    assert not exported.get("preview_unconfirmed_rows")


def test_preview_does_not_disturb_a_later_export(compare_client, temp_db):
    """先预览、再确认、再导出——预览跑过一遍不该改变导出的结果。

    沙箱里 confirm_batch 真的建过 submission（只是回滚了）。如果它留下了
    自增序列以外的任何痕迹（batch_id 幂等键、supplier 记录…），后续正式
    入库就可能撞上，导出跟着变形。
    """
    _engine, SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)

    for c in state["confirmations"]:
        r = compare_client.post("/api/quotes/batch-confirm", json=c.model_dump())
        assert r.status_code == 200, r.text
    sub_ids = [
        r for r in [
            compare_client.post("/api/quotes/batch-confirm", json=c.model_dump()).json()["submission_id"]
            for c in state["confirmations"]
        ]
    ]
    r = compare_client.post("/api/analysis/tender-list/match", data={
        "project_id": str(state["project_id"]), "category": "阀门",
        "supplier_ids": "", "submission_ids": ",".join(str(s) for s in sub_ids),
    })
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        baseline = get_bid_matrix_for_export(db, state["project_id"], "阀门", [])

    # 再跑一次预览，然后重新导出
    build_preview_matrix(state["project_id"], "阀门", state["confirmations"])

    with SessionLocal() as db:
        after = get_bid_matrix_for_export(db, state["project_id"], "阀门", [])

    assert len(after["suppliers"]) == len(baseline["suppliers"])
    assert len(after["rows"]) == len(baseline["rows"])
