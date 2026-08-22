"""Project CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
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


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "Project not found")
    return proj


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    proj = Project(**body.model_dump())
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
