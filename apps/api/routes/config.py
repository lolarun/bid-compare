"""Configuration management API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.config import DEFAULT_SCORING_WEIGHTS, DEFAULT_THRESHOLDS, EXTENDED_ATTR_SCHEMAS
from apps.api.models import AnalysisConfig
from apps.api.schemas import ConfigUpdate, ConfigOut

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("", response_model=list[ConfigOut])
def list_configs(db: Session = Depends(get_db)):
    configs = db.query(AnalysisConfig).all()
    if not configs:
        _init_defaults(db)
        configs = db.query(AnalysisConfig).all()
    return configs


@router.get("/{key}", response_model=ConfigOut)
def get_config(key: str, db: Session = Depends(get_db)):
    cfg = db.query(AnalysisConfig).filter(AnalysisConfig.key == key).first()
    if not cfg:
        raise HTTPException(404, f"Config key '{key}' not found")
    return cfg


@router.put("/{key}", response_model=ConfigOut)
def update_config(key: str, body: ConfigUpdate, db: Session = Depends(get_db)):
    cfg = db.query(AnalysisConfig).filter(AnalysisConfig.key == key).first()
    if not cfg:
        raise HTTPException(404, f"Config key '{key}' not found")

    # Validation
    if key == "scoring_weights":
        total = sum(body.value.values())
        if abs(total - 1.0) > 0.01:
            raise HTTPException(422, f"评分权重合计必须等于 1.0，当前合计 {total:.4f}")
    if key == "thresholds":
        for cat, v in body.value.items():
            if isinstance(v, dict):
                y = v.get("yellow", 0)
                r = v.get("red", 0)
                if y >= r:
                    raise HTTPException(422, f"品类 '{cat}' 的 yellow ({y}) 必须小于 red ({r})")

    cfg.value = body.value
    if body.description is not None:
        cfg.description = body.description
    db.commit()
    db.refresh(cfg)
    return cfg


def _init_defaults(db: Session):
    defaults = [
        ("scoring_weights", DEFAULT_SCORING_WEIGHTS, "供应商评分权重：price/history/completeness/brand/commercial"),
        ("thresholds", DEFAULT_THRESHOLDS, "各品类价格偏差阈值：{yellow, red}"),
        ("extended_attr_schemas", EXTENDED_ATTR_SCHEMAS, "各品类扩展属性 Schema"),
    ]
    for key, value, desc in defaults:
        existing = db.query(AnalysisConfig).filter(AnalysisConfig.key == key).first()
        if not existing:
            db.add(AnalysisConfig(key=key, value=value, description=desc))
    db.commit()
