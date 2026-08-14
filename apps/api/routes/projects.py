"""Project CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models import Project
from apps.api.schemas import ProjectCreate, ProjectUpdate, ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


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
        # projects 表 (name, code) 唯一约束——真实触发场景：design/27 工作台
        # 打开空白页即建占位项目，重名会撞 uq_project_name_code。之前这里没
        # 捕获，直接把裸的 IntegrityError 500 甩给前端，报"服务器错误"，用户
        # 无从判断是不是自己的操作有问题；现在给出可读的 409。
        db.rollback()
        raise HTTPException(409, f"项目名称「{proj.name}」+ 编号「{proj.code}」已存在，请修改后重试")
    db.refresh(proj)
    return proj


@router.put("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectUpdate, db: Session = Depends(get_db)):
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "Project not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(proj, field, value)

    db.commit()
    db.refresh(proj)
    return proj


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db)):
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "Project not found")
    db.delete(proj)
    db.commit()
