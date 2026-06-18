"""7层供应商解析服务。

用途：将原始供应商名称文本（来自 OCR / 文件名 / 用户输入）映射到
canonical Supplier.id，替代旧版 batch_confirm 中的模糊字符串匹配。

解析层级（优先级从高到低）：
  1. Supplier.name 精确匹配（不区分大小写）
  2. Supplier.short_name 精确匹配
  3. SupplierAlias(alias_type='legal_name') normalized 精确匹配
  4. SupplierAlias(alias_type='short_name') normalized 精确匹配
  5. SupplierAlias(alias_type='filename') normalized 精确匹配
  6. SupplierAlias(alias_type='historical') normalized 精确匹配
  7. 有多个候选 → 返回 candidates 列表，禁止自动选择

规则：
  - 任一层命中 1 个 active supplier → 直接返回
  - 命中多个不同 supplier_id → 进入层 7，返回 candidates，无自动选择
  - 全部层均未命中 → 返回 None（前端必须引导用户手动选择或新建）
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from apps.api.models.supplier import Supplier
from apps.api.models.supplier_alias import SupplierAlias, normalize_alias


@dataclass
class ResolveResult:
    supplier: Supplier | None = None        # 精确命中 → 非空
    candidates: list[dict] | None = None    # 歧义 → 非空（多个候选）
    matched_layer: str = ""                 # 命中层标识，用于日志/审计
    normalized: str = ""                    # 实际匹配的规范化文本


def resolve_supplier(
    db: Session,
    raw_name: str,
    *,
    active_only: bool = True,
) -> ResolveResult:
    """将原始供应商名解析为 Supplier。

    active_only=True：只查 merge_status='active' 的供应商（默认）。
    """
    norm = normalize_alias(raw_name)
    if not norm:
        return ResolveResult(normalized=norm)

    def _active_filter(q):
        if active_only:
            return q.filter(Supplier.merge_status == "active")
        return q

    # ── 层 1: Supplier.name 精确（不区分大小写）────────────────────────────────
    exact_name = _active_filter(
        db.query(Supplier).filter(Supplier.name.ilike(raw_name.strip()))
    ).all()
    if len(exact_name) == 1:
        return ResolveResult(supplier=exact_name[0], matched_layer="1:name_exact", normalized=norm)
    if len(exact_name) > 1:
        return ResolveResult(
            candidates=[{"id": s.id, "name": s.name, "layer": "1:name_exact"} for s in exact_name],
            matched_layer="ambiguous",
            normalized=norm,
        )

    # ── 层 2: Supplier.short_name 精确────────────────────────────────────────
    short_name = raw_name.strip()
    exact_short = _active_filter(
        db.query(Supplier).filter(Supplier.short_name.ilike(short_name))
    ).filter(Supplier.short_name != "").all()
    if len(exact_short) == 1:
        return ResolveResult(supplier=exact_short[0], matched_layer="2:short_name_exact", normalized=norm)
    if len(exact_short) > 1:
        return ResolveResult(
            candidates=[{"id": s.id, "name": s.name, "layer": "2:short_name_exact"} for s in exact_short],
            matched_layer="ambiguous",
            normalized=norm,
        )

    # ── 层 3-6: SupplierAlias（按 alias_type 优先级依次查） ───────────────────
    for layer_idx, alias_type in enumerate(
        ("legal_name", "short_name", "filename", "historical"), start=3
    ):
        aliases = (
            db.query(SupplierAlias)
            .filter(
                SupplierAlias.normalized_alias == norm,
                SupplierAlias.alias_type == alias_type,
                SupplierAlias.active == 1,
            )
            .all()
        )
        if not aliases:
            continue

        # 获取对应的 active supplier
        sup_ids = set(a.supplier_id for a in aliases)
        sups = _active_filter(
            db.query(Supplier).filter(Supplier.id.in_(sup_ids))
        ).all()

        if len(sups) == 1:
            return ResolveResult(
                supplier=sups[0],
                matched_layer=f"{layer_idx}:alias_{alias_type}",
                normalized=norm,
            )
        if len(sups) > 1:
            return ResolveResult(
                candidates=[
                    {"id": s.id, "name": s.name, "layer": f"{layer_idx}:alias_{alias_type}"}
                    for s in sups
                ],
                matched_layer="ambiguous",
                normalized=norm,
            )

    # ── 层 7: 未找到 ────────────────────────────────────────────────────────
    return ResolveResult(normalized=norm)


def search_suppliers_by_name(
    db: Session,
    query: str,
    *,
    limit: int = 10,
    active_only: bool = True,
) -> list[dict]:
    """供应商模糊搜索（用于前端下拉/自动补全）。

    先做 Supplier.name / short_name LIKE 搜索，再做 SupplierAlias 精确匹配。
    结果按 name 排序，去重返回，上限 `limit` 条。
    """
    query = query.strip()
    if not query:
        return []

    base_q = db.query(Supplier)
    if active_only:
        base_q = base_q.filter(Supplier.merge_status == "active")

    # Supplier 名称模糊搜索
    name_results = base_q.filter(
        Supplier.name.contains(query) | Supplier.short_name.contains(query)
    ).limit(limit).all()

    seen_ids = {s.id for s in name_results}
    alias_results: list[Supplier] = []

    # SupplierAlias 精确 normalized 匹配
    norm = normalize_alias(query)
    if norm:
        alias_hits = (
            db.query(SupplierAlias)
            .filter(
                SupplierAlias.normalized_alias == norm,
                SupplierAlias.active == 1,
            )
            .all()
        )
        alias_sup_ids = {a.supplier_id for a in alias_hits} - seen_ids
        if alias_sup_ids:
            extra = base_q.filter(Supplier.id.in_(alias_sup_ids)).all()
            alias_results = extra

    combined = name_results + alias_results
    return [
        {
            "id": s.id,
            "name": s.name,
            "short_name": s.short_name or "",
            "merge_status": s.merge_status,
        }
        for s in combined
    ][:limit]
