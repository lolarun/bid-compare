"""Project CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.security import require_admin
from apps.api.models import Project
from apps.api.schemas import ProjectCreate, ProjectUpdate, ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _duplicate_project_409(name: str, code: str) -> HTTPException:
    """projects 表 (name, code) 唯一约束 uq_project_name_code 的统一出口。

    创建和更新走同一条文案：裸的 IntegrityError 会以 500 + 堆栈的形式甩给
    前端，用户只看到"服务器错误"，无从判断是不是自己的操作有问题。
    """
    return HTTPException(
        409, f"项目名称「{name}」+ 编号「{code}」已存在，请修改名称或编号后重试"
    )


@router.get("/find-exact", response_model=ProjectOut | None)
def find_project_exact(
    name: str = Query(..., min_length=1),
    code: str = Query(""),
    db: Session = Depends(get_db),
):
    """按 (name, code) **精确**找已有项目；没有返回 null。

    给工作台用：招标文件识别出的项目名回填时会撞 `uq_project_name_code`，
    而那个名字是系统识别出来的、不是用户起的——让用户去改一个他没起过的
    名字解决冲突，是把系统的问题推给他。撞名基本只有一种真实含义：**这份
    招标文件之前已经比过一次**。所以要能拿到那个已有项目，把"打开它"作为
    首选出路。

    用 `list_projects(keyword=...)` 代替不了：那是 `contains` 模糊匹配，
    "金桥17B-06" 和 "金桥地铁上盖…" 会互相命中，而这里要的恰恰是"跟唯一
    约束同一个判据"——只有精确相等才是同一个项目。
    """
    proj = db.scalar(select(Project).where(Project.name == name, Project.code == code))
    return proj


@router.get("", response_model=dict)
def list_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Project)
    if keyword:
        stmt = stmt.where(Project.name.contains(keyword) | Project.code.contains(keyword))
    if status:
        stmt = stmt.where(Project.status == status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(Project.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [ProjectOut.model_validate(i).model_dump() for i in items],
    }


@router.get("/overview", response_model=dict)
def projects_overview(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    include_empty: bool = Query(
        False,
        description="是否包含空项目（无轮次、无报价、无清单会话）。默认不含。",
    ),
    db: Session = Depends(get_db),
):
    """比价入口列表（docs/design/44 §3.2，design/45 §4 扩展）。

    只读聚合，一次批量查询，不对每个项目单独调用 quote-rounds 接口
    （N+1：项目一多就会拖垮页面）。

    「品类」取 **轮次 ∪ 清单会话** 的并集（design/45 §4.3）——design/44 只看
    轮次，于是"传了采购清单、还没确认任何报价"的项目一个品类都不显示，
    `清单未确认` / `待上传报价` 两个状态永远走不到，而那正是新项目最常见的
    状态。既有的"没有任何轮次时 categories 为空"仍然成立：清单也没有的项目
    照旧是空数组，前端继续显示「首轮将在首次确认报价时自动开启」。

    `include_empty=False`（默认）在**分页之前**过滤空项目，所以 `total` 与
    `items` 口径一致；判据是语义上的"空"，不是名字模式（design/45 §4.4）。
    """
    from apps.api.models.quote_round import QuoteRound
    from apps.api.services.tender.project_overview import (
        derive_next_action,
        load_all_list_categories,
        load_confirmed_list_categories,
        load_pending_intake_counts,
        load_rounds_by_project,
        load_submission_counts,
        non_empty_project_ids_subquery,
    )

    stmt = select(Project)
    if keyword:
        stmt = stmt.where(Project.name.contains(keyword) | Project.code.contains(keyword))
    if not include_empty:
        stmt = stmt.where(Project.id.in_(non_empty_project_ids_subquery()))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    projects = db.scalars(
        stmt.order_by(Project.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    if not projects:
        return {"total": total, "page": page, "page_size": page_size, "items": []}

    pids = [p.id for p in projects]
    rounds_by_project = load_rounds_by_project(db, pids)
    confirmed_list_cats = load_confirmed_list_categories(db, pids)
    all_list_cats = load_all_list_categories(db, pids)
    submission_counts = load_submission_counts(db, pids)
    pending_intake = load_pending_intake_counts(db, pids)

    items = []
    for proj in projects:
        cats: dict[str, list[QuoteRound]] = {}
        for r in rounds_by_project.get(proj.id, []):
            cats.setdefault(r.category, []).append(r)
        # 清单会话带来的品类：可能还没有任何轮次，补进来才有卡片可显示。
        for pid, cat in all_list_cats:
            if pid == proj.id:
                cats.setdefault(cat, [])

        proj_pending = pending_intake.get(proj.id, 0)
        category_summaries = []
        for cat, rs in sorted(cats.items()):
            # rs 已按 seq desc 排（同一 (project,category) 分组内），[0] 即最新轮
            current = rs[0] if rs else None
            basis = next((r for r in rs if r.is_final_basis), None)
            last_activity = max(
                (r.updated_at for r in rs if r.updated_at is not None), default=None
            )
            has_list = (proj.id, cat) in confirmed_list_cats
            sub_count = submission_counts.get((proj.id, cat), 0)
            # 待校对数只有项目粒度（job 上没有可靠品类，见 service 的说明）：
            # 单品类项目直接归到该品类；多品类时不猜，留 0，由卡片另行展示。
            cat_pending = proj_pending if len(cats) == 1 else 0
            next_action = derive_next_action(
                submission_count=sub_count,
                pending_intake_count=cat_pending,
                has_confirmed_list=has_list,
                final_basis_seq=basis.seq if basis else None,
            )
            category_summaries.append({
                "category": cat,
                "current_round": (
                    {
                        "id": current.id, "seq": current.seq, "name": current.name,
                        "stage": current.stage, "status": current.status,
                    }
                    if current else None
                ),
                "round_count": len(rs),
                "confirmed_supplier_count": (
                    len(current.confirmed_supplier_ids or []) if current else 0
                ),
                "final_basis_round": (
                    {"id": basis.id, "seq": basis.seq, "name": basis.name} if basis else None
                ),
                "last_activity": last_activity.isoformat() if last_activity else None,
                # ── design/45 §4.3 新增 ──────────────────────────────────
                "has_confirmed_list": has_list,
                "submission_count": sub_count,
                "next_action": next_action.to_dict(),
            })
        items.append({
            "project": ProjectOut.model_validate(proj).model_dump(),
            "categories": category_summaries,
            "pending_intake_count": proj_pending,
        })

    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/{project_id}/overview", response_model=dict)
def project_overview(project_id: int, db: Session = Depends(get_db)):
    """项目概述页的只读聚合（docs/design/45 §6）。

    一次批量查询给出：项目标量 + 每品类的 清单 / 当前轮 / 各轮报价清单 /
    供应商 / 下一步动作。

    **刻意不算矩阵。** 评标总价排名、三态门禁、被排除行的金额影响都要跑
    `import_and_match`，那是概述页 D 区块的事，由前端懒加载既有的
    `POST /api/analysis/bid-matrix`——同一个业务服务、同一份结果
    （CLAUDE.md §4「一份业务结果」）。在这里另写一套便宜的"大致谁便宜"，
    就是让系统对同一个项目有两种说法的开始。

    路由顺序注意：本路由必须排在 `GET /{project_id}` **之前**，否则
    `/{project_id}` 会先匹配上 `123/overview` 里的路径段。
    """
    from apps.api.services.tender.project_overview import build_project_overview

    out = build_project_overview(db, project_id)
    if not out:
        raise HTTPException(404, "Project not found")
    return out


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "Project not found")
    return proj


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    """docs/design/42 §8 D1 / design/44 F3：项目创建收口给管理员。

    D-1 决策记录：F1 落地时按钮对所有比价角色开放，明说了"P3 上线后再收
    权限"——这就是那个收权限的时刻，不是新决定。
    """
    proj = Project(**body.model_dump(), created_by_user_id=current_user.get("user_id"))
    db.add(proj)
    try:
        db.commit()
    except IntegrityError:
        # 真实触发场景：design/27 工作台打开空白页即建占位项目，重名会撞
        # uq_project_name_code。
        name, code = proj.name, proj.code
        db.rollback()
        raise _duplicate_project_409(name, code)
    db.refresh(proj)
    return proj


@router.put("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectUpdate, db: Session = Depends(get_db)):
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "Project not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(proj, field, value)

    # rollback 会把 proj 上的字段还原成库里的旧值，所以先留一份"用户想改成
    # 什么"用于报错文案。
    attempted_name, attempted_code = proj.name, proj.code
    try:
        db.commit()
    except IntegrityError:
        # 真实触发场景（2026-08-21 服务端日志）：两个工作台各自识别同一份招标
        # 文件，WorkspaceView 回填出同样的项目名后各写一次 PUT，第二次撞
        # uq_project_name_code。重开一次比价、上传失败后重试都会走到这里，是
        # 正常用户路径，不是异常操作。原来这个 commit 没有任何保护，直接 500
        # + 裸堆栈，且前端没有 catch，用户既看不到报错也不知道名字没存上。
        db.rollback()
        raise _duplicate_project_409(attempted_name, attempted_code)
    db.refresh(proj)
    return proj


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db)):
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "Project not found")
    db.delete(proj)
    db.commit()
