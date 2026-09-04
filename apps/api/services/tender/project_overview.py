"""项目概述聚合（archive/design/45；当前口径见 docs/spec）。

两个消费者共用这里的口径，不各算各的：
  - `GET /api/projects/overview`（比价入口列表，design/45 §4）
  - `GET /api/projects/{id}/overview`（项目概述页，design/45 §6）

放服务层而不是路由层，是因为「下一步动作」和「空项目」都是**业务判据**，
两个入口给出不一样的答案就等于系统对同一个项目有两种说法。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from apps.api.models.bid_submission import BidSubmission
from apps.api.models.extraction_job import ExtractionJob
from apps.api.models.quote_round import QuoteRound
from apps.api.models.tender_list_session import TenderListSession

# 报价件的生命周期取值（`ExtractionJob.lifecycle`）。
# 2026-08-30 实测：`BidSubmission.status` **恒为 `"pending"`**——`confirm_batch`
# 建记录时写死 `status="pending"`（`quote_confirmation_service.py:614`）且此后
# 不再改它，所以它不是"是否已校对"的信号，拿它做判据会让每个项目都显示
# 「待校对」。真正的信号是 job 的 lifecycle：`active` = 已识别待校对，
# `confirmed` = 已入库（活库实测 58 个 confirmed job 对 58 条 submission，1:1）。
JOB_LIFECYCLE_ACTIVE = "active"

# submission 的两个"不算数"状态，与 `/compare-state` 的过滤口径保持一致
# （那里同样 `notin_(["rejected", "superseded"])`）——两处若不一致，入口卡片
# 的家数会跟工作台的卡片数对不上。
INACTIVE_SUBMISSION_STATUSES = ("rejected", "superseded")


@dataclass(frozen=True)
class NextAction:
    """一个品类的「下一步动作」。

    是**状态读数，不是建议**：每个分支都对应数据库里已经成立的事实，没有
    模型调用、没有启发式。`count` 只在需要报数量的分支上非空。
    """

    code: str
    label: str
    count: int | None = None

    def to_dict(self) -> dict:
        return {"code": self.code, "label": self.label, "count": self.count}


def derive_next_action(
    *,
    submission_count: int,
    pending_intake_count: int,
    has_confirmed_list: bool,
    final_basis_seq: int | None,
) -> NextAction:
    """design/45 §4.3 的五分支表，首个命中生效。

    分支顺序即优先级：先问"有没有东西"，再问"清单确认了没"，再问"识别的
    东西校对了没"，最后才问"定标基准定了没"。倒过来问会在清单还没确认时
    就报「可出比价」。
    """
    if submission_count == 0 and pending_intake_count == 0:
        return NextAction("pending_upload", "待上传报价")
    if not has_confirmed_list:
        return NextAction("list_unconfirmed", "清单未确认")
    if pending_intake_count > 0:
        return NextAction("pending_intake", "待校对入库", pending_intake_count)
    if final_basis_seq is not None:
        return NextAction("basis_set", f"已定标基准：第{final_basis_seq}轮")
    return NextAction("ready_to_compare", "可出比价")


def non_empty_project_ids_subquery():
    """「非空项目」的 id 子查询——按语义判定，不按名字模式。

    空 = 既没有轮次、也没有报价、也没有清单会话。**刻意不匹配
    `新比价项目-<时间戳>` 这类名字**：用户完全可能合法地这么命名，而换个
    名字的空壳照样漏网；"空"才是让卡片没有价值的那个属性
    （design/45 §4.4，并有测试钉住应用代码里不出现该名字模式）。
    """
    return (
        select(QuoteRound.project_id.label("pid"))
        .where(QuoteRound.project_id.isnot(None))
        .union(
            select(BidSubmission.project_id.label("pid")).where(
                BidSubmission.project_id.isnot(None),
                BidSubmission.status.notin_(INACTIVE_SUBMISSION_STATUSES),
            ),
            select(TenderListSession.project_id.label("pid")).where(
                TenderListSession.project_id.isnot(None)
            ),
        )
    )


def load_rounds_by_project(db: Session, project_ids: list[int]) -> dict[int, list[QuoteRound]]:
    """一次取回所有轮次，按 project_id 分桶（避免逐项目 N+1）。"""
    if not project_ids:
        return {}
    rounds = db.scalars(
        select(QuoteRound)
        .where(QuoteRound.project_id.in_(project_ids))
        .order_by(QuoteRound.project_id, QuoteRound.category, QuoteRound.seq.desc())
    ).all()
    out: dict[int, list[QuoteRound]] = {}
    for r in rounds:
        out.setdefault(r.project_id, []).append(r)
    return out


def load_confirmed_list_categories(db: Session, project_ids: list[int]) -> set[tuple[int, str]]:
    """已确认采购清单的 (project_id, category) 集合。

    `is_current` 与 `status='confirmed'` **两个条件都要**——只查其中一个会
    取到已被取代或尚未确认的会话。这跟
    `tender_session_service.get_current_confirmed_session` 和
    `/compare-state` 用的是同一道闸门，不得在这里放松。
    """
    if not project_ids:
        return set()
    rows = db.execute(
        select(TenderListSession.project_id, TenderListSession.category).where(
            TenderListSession.project_id.in_(project_ids),
            TenderListSession.is_current.is_(True),
            TenderListSession.status == "confirmed",
        )
    ).all()
    return {(pid, cat or "") for pid, cat in rows}


def load_all_list_categories(db: Session, project_ids: list[int]) -> set[tuple[int, str]]:
    """出现过清单会话的 (project_id, category)——**含未确认的**。

    品类集合要取 轮次 ∪ 清单会话 的并集：只看轮次的话，"传了采购清单但还
    没确认报价"的项目会一个品类都没有，`清单未确认` / `待上传报价` 两个
    分支就永远走不到，而那恰恰是新项目最常见的状态。
    """
    if not project_ids:
        return set()
    rows = db.execute(
        select(TenderListSession.project_id, TenderListSession.category)
        .where(TenderListSession.project_id.in_(project_ids))
        .distinct()
    ).all()
    return {(pid, cat or "") for pid, cat in rows}


def load_submission_counts(db: Session, project_ids: list[int]) -> dict[tuple[int, str], int]:
    """已入库报价数，按 (project_id, category) 计。

    品类经 `round_id → QuoteRound.category` 取得——`BidSubmission` 自己没有
    category 列。活库实测 `round_id IS NULL` 为 0（迁移 0009/0011 已回填），
    但这里仍用外连接：真出现无轮次的存量记录时，它落进 `None` 桶被如实忽略，
    而不是被硬塞进某个品类。
    """
    if not project_ids:
        return {}
    rows = db.execute(
        select(
            BidSubmission.project_id,
            QuoteRound.category,
            func.count(BidSubmission.id),
        )
        .join(QuoteRound, BidSubmission.round_id == QuoteRound.id, isouter=True)
        .where(
            BidSubmission.project_id.in_(project_ids),
            BidSubmission.status.notin_(INACTIVE_SUBMISSION_STATUSES),
        )
        .group_by(BidSubmission.project_id, QuoteRound.category)
    ).all()
    return {(pid, cat): int(n) for pid, cat, n in rows if cat is not None}


def load_pending_intake_counts(db: Session, project_ids: list[int]) -> dict[int, int]:
    """待校对入库的报价件数，**按项目计，不按品类**。

    刻意只到项目粒度：job 上没有可靠的 category——它要么还没识别完，要么只
    有识别产物里的 `detected_category` 这种推测值。把推测值当品类归属会让
    卡片上的数字在识别完成后自己跳动。宁可少一维，不可给个会变的数。

    `status` 不设条件：识别中（running）、已识别待确认（done）、识别失败
    （failed）**都还没入库**，都属于"这个项目还有东西没处理完"。
    """
    if not project_ids:
        return {}
    # SQLite 的 json_extract 返回 TEXT，不 CAST 会跟 int 参数比不上
    pid_expr = func.cast(
        func.json_extract(ExtractionJob.context, "$.project_id"), Integer
    )
    rows = db.execute(
        select(pid_expr.label("pid"), func.count(ExtractionJob.id))
        .where(
            ExtractionJob.type == "quote",
            ExtractionJob.lifecycle == JOB_LIFECYCLE_ACTIVE,
            pid_expr.in_(project_ids),      # 在 SQL 里就收窄，不要全表聚合完再筛
        )
        .group_by("pid")
    ).all()
    return {int(pid): int(n) for pid, n in rows if pid is not None}


# ─── 项目概述页聚合（design/45 §6）────────────────────────────────────────


def _list_session_summary(s: TenderListSession | None) -> dict | None:
    """采购清单摘要（design/45 §5.2 A/B）。

    **没有金额字段**：`TenderAnchor` 只有 seq/name/spec/model/pressure/
    materials/unit/qty/brand/profession/remark——采购清单本来就是"给投标方
    填价的空表"，金额是报价侧的事实（见 §5.2 C 的两个总价）。这里给出来源、
    版本和确认信息，让人判断"这条轴可不可信"。
    """
    if s is None:
        return None
    return {
        "session_id": s.id,
        "confirmed": True,
        "anchor_count": int(s.anchors_total or 0),
        "version": int(s.version or 1),
        "source_type": s.source_type or "excel",
        "file_name": s.file_name or "",
        "confirmed_at": s.confirmed_at.isoformat() if s.confirmed_at else None,
        "confirmed_by": s.confirmed_by,
        "brand_requirement": s.brand_requirement or [],
    }


def build_project_overview(db: Session, project_id: int) -> dict:
    """项目概述页的一次性聚合（design/45 §6）。

    **不算矩阵**：评标总价排名、三态门禁、被排除行的金额影响都要跑
    `import_and_match`，那是概述页 D 区块的事，走既有的
    `POST /api/analysis/bid-matrix` 懒加载（design/45 C4：同一业务结果，
    不许在这里长出第二套"大致谁便宜"的实现）。这里只回答"有什么、到哪了"。
    """
    from apps.api.models.bid_submission import BidQuoteLine
    from apps.api.models.project import Project
    from apps.api.models.supplier import Supplier

    proj = db.get(Project, project_id)
    if proj is None:
        return {}

    created_by_name: str | None = None
    if proj.created_by_user_id is not None:
        from apps.api.models.user import User

        creator = db.get(User, proj.created_by_user_id)
        if creator is not None:
            created_by_name = creator.nickname or creator.username

    pids = [project_id]
    rounds = load_rounds_by_project(db, pids).get(project_id, [])
    confirmed_cats = {c for (_p, c) in load_confirmed_list_categories(db, pids)}
    all_list_cats = {c for (_p, c) in load_all_list_categories(db, pids)}
    sub_counts = load_submission_counts(db, pids)
    pending_intake = load_pending_intake_counts(db, pids).get(project_id, 0)

    # 当前且已确认的清单会话，按品类取——`is_current` + `status='confirmed'`
    # 两个条件都要（与全仓同一道闸门）。
    sessions = db.scalars(
        select(TenderListSession).where(
            TenderListSession.project_id == project_id,
            TenderListSession.is_current.is_(True),
            TenderListSession.status == "confirmed",
        ).order_by(TenderListSession.id.desc())
    ).all()
    session_by_cat: dict[str, TenderListSession] = {}
    for s in sessions:
        session_by_cat.setdefault(s.category or "", s)

    # 每份报价的两个总价 + 行数。一次 GROUP BY，不逐 submission 查。
    line_agg = {
        int(sid): (int(n), float(total or 0.0))
        for sid, n, total in db.execute(
            select(
                BidQuoteLine.submission_id,
                func.count(BidQuoteLine.id),
                func.sum(BidQuoteLine.total_price),
            ).group_by(BidQuoteLine.submission_id)
        ).all()
    }

    subs = db.scalars(
        select(BidSubmission).where(
            BidSubmission.project_id == project_id,
            BidSubmission.status.notin_(INACTIVE_SUBMISSION_STATUSES),
        ).order_by(BidSubmission.id.asc())
    ).all()
    supplier_names = {
        s.id: s.name for s in db.scalars(
            select(Supplier).where(
                Supplier.id.in_([x.supplier_id for x in subs if x.supplier_id])
            )
        ).all()
    } if any(x.supplier_id for x in subs) else {}

    jobs = {
        j.id: j for j in db.scalars(
            select(ExtractionJob).where(
                ExtractionJob.id.in_([s.job_id for s in subs if s.job_id])
            )
        ).all()
    } if subs else {}

    def _submission_row(s: BidSubmission) -> dict:
        n, detail_total = line_agg.get(s.id, (0, 0.0))
        job = jobs.get(s.job_id) if s.job_id else None
        declared = None
        if job is not None:
            doc_meta = (job.result or {}).get("_doc_meta") or {}
            raw = doc_meta.get("bid_total")
            try:
                declared = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                declared = None
        return {
            # 列身份是 submission_id，不是 supplier_id（CLAUDE.md §4）
            "submission_id": s.id,
            "supplier_id": s.supplier_id,
            "supplier_name": supplier_names.get(s.supplier_id) or s.supplier_raw_name or "",
            "round_id": s.round_id,
            "line_count": n,
            # 两个总价永远分开（FUNCTIONAL §5）：一个是明细算出来的，一个是
            # 文件自己声明的；合并成一个数就把"识别是否完整"的独立证据抹掉了。
            "detail_total": detail_total,
            "declared_total": declared,
            "submitted_at": s.created_at.isoformat() if s.created_at else None,
        }

    subs_by_round: dict[int | None, list[dict]] = {}
    subs_by_cat: dict[str, list[dict]] = {}
    round_cat = {r.id: r.category for r in rounds}
    for s in subs:
        row = _submission_row(s)
        subs_by_round.setdefault(s.round_id, []).append(row)
        cat = round_cat.get(s.round_id)
        if cat is not None:
            subs_by_cat.setdefault(cat, []).append(row)

    # 各轮口径体检。一次性算完：check_round_basis 每次都要查 submission_basis，
    # 放进下面的推导循环里就是每品类每轮各查一次。
    from apps.api.services.matrix.basis_consistency import check_round_basis

    _basis_reports: dict[int, dict] = {}
    for _r in rounds:
        _pairs = [
            (row["submission_id"], row["supplier_name"] or f"#{row['submission_id']}")
            for row in subs_by_round.get(_r.id, [])
        ]
        _basis_reports[_r.id] = check_round_basis(db, _pairs).as_dict()

    categories = sorted(set(round_cat.values()) | all_list_cats)
    out_cats = []
    for cat in categories:
        rs = [r for r in rounds if r.category == cat]
        current = rs[0] if rs else None
        basis = next((r for r in rs if r.is_final_basis), None)
        has_list = cat in confirmed_cats
        sess = session_by_cat.get(cat)
        cat_pending = pending_intake if len(categories) == 1 else 0
        out_cats.append({
            "category": cat,
            # 有确认清单 → 锚点轴；否则报价派生轴（只能进预览，design/45 C3）。
            # 没有任何报价时两者都谈不上，给 null 而不是硬塞一个。
            "axis_kind": (
                "tender_anchor" if has_list
                else ("quote_derived" if subs_by_cat.get(cat) else None)
            ),
            "list": _list_session_summary(sess),
            "current_round": (
                {
                    "id": current.id, "seq": current.seq, "name": current.name,
                    "stage": current.stage, "status": current.status,
                    "is_final_basis": bool(current.is_final_basis),
                } if current else None
            ),
            "rounds": [
                {
                    "id": r.id, "seq": r.seq, "name": r.name,
                    "stage": r.stage, "status": r.status,
                    "is_final_basis": bool(r.is_final_basis),
                    "opened_at": r.opened_at.isoformat() if r.opened_at else None,
                    "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                    "submissions": subs_by_round.get(r.id, []),
                    # 口径体检（P1）：随轮次一起给，页面不必为每一轮再发一次请求。
                    # 判定本身在 services/matrix/basis_consistency.py，确定性、无模型。
                    "basis": _basis_reports.get(r.id, {"comparable": True, "conflicts": [], "unresolved": []}),
                }
                for r in sorted(rs, key=lambda x: x.seq)
            ],
            "suppliers": subs_by_cat.get(cat, []),
            "final_basis_round": (
                {"id": basis.id, "seq": basis.seq, "name": basis.name} if basis else None
            ),
            "has_confirmed_list": has_list,
            "submission_count": sub_counts.get((project_id, cat), 0),
            "next_action": derive_next_action(
                submission_count=sub_counts.get((project_id, cat), 0),
                pending_intake_count=cat_pending,
                has_confirmed_list=has_list,
                final_basis_seq=basis.seq if basis else None,
            ).to_dict(),
        })

    return {
        "project": {
            "id": proj.id,
            "name": proj.name,
            "code": proj.code,
            "status": proj.status,
            "location": proj.location,
            "remark": proj.remark,
            # 2026-09-03：概述页页眉要回答"这项目是谁什么时候建的"。数据本来就在
            # `projects` 表上，只是没往外给。建档人取 nickname/username，取不到
            # （用户已删）就是 None——不编一个"未知用户"当人名。
            "created_at": proj.created_at.isoformat() if proj.created_at else None,
            "created_by": created_by_name,
        },
        "categories": out_cats,
        "pending_intake_count": pending_intake,
    }
