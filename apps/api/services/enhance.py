"""AI-enhanced OCR post-processing — categorize, standardize names, pre-align.

Called between OCR extraction and batch-confirm.  One LLM call does three
things so the user only needs to review once:

1. **Auto-categorize** each item into one of the known categories
   (阀门, 桥架, 母线槽, …).
2. **Standardize material names** — map vendor-specific names to canonical
   names, matching existing materials in the DB when possible.
3. **Pre-align** — if the project already has quotes from other suppliers,
   flag which incoming items correspond to the same tender line item.

The result is a list of enhanced items ready for the user to review in the
ExtractionEditor, with AI changes highlighted.
"""

import json
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from apps.api.core.config import PROFESSION_MAP, ALL_CATEGORIES, get_settings
from apps.api.services.llm_provider import get_dashscope_client
from apps.api.models import Material, Quote, Supplier

log = logging.getLogger(__name__)

# ── Prompt ───────────────────────────────────────────────────────────────────

ENHANCE_PROMPT = """\
你是建筑机电材料采购数据处理专家。

## 任务

以下是从一份报价PDF中OCR识别出的材料列表。请对每一项执行：

1. **品类分类**：判断该材料属于哪个品类。可选品类：{categories}
   如果无法判断，返回空字符串。

2. **名称标准化**：将供应商特有的材料名称转换为通用标准名称。
   - 去掉品牌前缀（如"某品牌截止阀"→"截止阀"）
   - 统一同义词（如"逆止阀"→"止回阀"，"闸板阀"→"闸阀"）
   - 保留关键修饰词（如"不锈钢"、"青铜"、"消声"等材质/功能描述）
   - 分离名称和规格（如"Y型过滤器DN20"→名称="Y型过滤器"，规格="DN20"）

3. **规格标准化**：统一规格格式
   - DN统一（如"Φ108"→"DN100"，"4寸"→"DN100"）
   - 压力等级格式（如"1.6Mpa"→"PN16"或保留"1.6MPa"）

{existing_context}

## OCR识别数据

{items_text}

## 输出要求

返回JSON：
{{
  "items": [
    {{
      "index": 0,
      "category": "阀门",
      "standard_name": "截止阀",
      "standard_spec": "DN20, PN16, 不锈钢",
      "original_name": "ALFA不锈钢截止阀",
      "original_spec": "DN20 PN16UKG 1.6Mpa",
      "name_note": "去掉品牌前缀ALFA",
      "matched_material_id": 123,
      "alignment_note": "与已有供应商XX的截止阀DN20对应"
    }}
  ]
}}

注意：
- index 必须与输入序号对应
- 如果名称无需修改，standard_name 应与原始名称相同
- matched_material_id 仅在确信匹配到已有材料时才填写，不确定时留 null
- alignment_note 仅在有已有供应商报价可对齐时填写
- 仅返回 JSON，不要解释
"""


def _build_items_text(items: list[dict]) -> str:
    """Build compact text of OCR items for the LLM prompt."""
    lines = []
    for i, it in enumerate(items[:60]):
        line = (
            f"[{i}] 材料={it.get('material', '')} | "
            f"规格={it.get('spec', '')} | "
            f"品牌={it.get('brand', '')} | "
            f"单位={it.get('unit', '')} | "
            f"单价={it.get('unit_price', '')}"
        )
        lines.append(line)
    text = "\n".join(lines)
    if len(items) > 60:
        text += f"\n...（共 {len(items)} 项，仅展示前 60 项）"
    return text


def _build_existing_context(
    project_id: int | None,
    db: Session,
) -> str:
    """Build context about existing materials and project quotes."""
    parts: list[str] = []

    # Existing materials in DB (for name matching)
    mats = (
        db.query(Material.id, Material.standard_name, Material.spec, Material.category)
        .filter(Material.standard_name != "")
        .order_by(Material.category, Material.standard_name)
        .limit(150)
        .all()
    )
    if mats:
        by_cat: dict[str, list[str]] = {}
        for mid, name, spec, cat in mats:
            key = cat or "未分类"
            entry = f"[id={mid}] {name}"
            if spec:
                entry += f" ({spec})"
            by_cat.setdefault(key, []).append(entry)
        lines = ["## 已有材料库（用于名称匹配）"]
        for cat, entries in by_cat.items():
            seen: set[str] = set()
            unique: list[str] = []
            for e in entries:
                name_part = e.split(" (")[0]
                if name_part not in seen:
                    seen.add(name_part)
                    unique.append(e)
            lines.append(f"\n### {cat}")
            for e in unique[:15]:  # cap per category
                lines.append(f"  {e}")
        parts.append("\n".join(lines))

    # Existing quotes in this project (for pre-alignment)
    if project_id:
        quotes = (
            db.query(
                Material.standard_name,
                Material.spec,
                Material.category,
                Supplier.name,
            )
            .select_from(Quote)
            .join(Material, Quote.material_id == Material.id)
            .join(Supplier, Quote.supplier_id == Supplier.id)
            .filter(Quote.project_id == project_id)
            .filter(Quote.unit_price > 0)
            .order_by(Material.standard_name)
            .limit(60)
            .all()
        )
        if quotes:
            lines = ["\n## 本项目已有供应商报价（用于对齐匹配）"]
            for mat_name, spec, cat, sup_name in quotes:
                lines.append(f"  供应商={sup_name} | {mat_name} | {spec}")
            parts.append("\n".join(lines))

    return "\n".join(parts) if parts else ""


def enhance_ocr_items(
    items: list[dict],
    project_id: int | None,
    db: Session,
) -> dict[str, Any]:
    """Call LLM to enhance OCR-extracted items.

    Returns:
        {
            "items": [... enhanced items ...],
            "summary": { "total", "categorized", "renamed", "aligned", "errors" },
            "tokens_used": int,
            "duration_ms": int,
        }
        On failure: adds "error" key.
    """
    settings = get_settings()
    client = get_dashscope_client()
    if client is None:
        return {"items": items, "summary": {}, "error": "LLM API key not configured"}

    try:
        items_text = _build_items_text(items)
        existing_context = _build_existing_context(project_id, db)
        prompt = ENHANCE_PROMPT.format(
            categories="、".join(ALL_CATEGORIES),
            existing_context=existing_context,
            items_text=items_text,
        )


        t0 = time.time()
        resp = client.chat.completions.create(
            model=settings.DASHSCOPE_LLM_MODEL,  # qwen3.6-flash: fast enough, cheaper
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=120,
        )
        raw = resp.choices[0].message.content or "{}"
        duration_ms = int((time.time() - t0) * 1000)

        tokens = 0
        if resp.usage:
            tokens = getattr(resp.usage, "total_tokens", 0)

        # Parse JSON
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            text = text[i : j + 1]
        data = json.loads(text)

        llm_items = data.get("items", [])

        # Merge LLM enhancements back into original items
        enhanced = []
        n_categorized = 0
        n_renamed = 0
        n_aligned = 0
        n_errors = 0

        # Build index lookup for LLM results
        llm_by_idx: dict[int, dict] = {}
        for li in llm_items:
            if isinstance(li, dict) and "index" in li:
                try:
                    llm_by_idx[int(li["index"])] = li
                except (ValueError, TypeError):
                    pass

        valid_categories = set(ALL_CATEGORIES)

        # Determine majority category from LLM results — used as fallback
        # for items the LLM didn't cover (beyond the cap).
        from collections import Counter
        cat_votes: Counter = Counter()
        for li in llm_items:
            if isinstance(li, dict):
                cat = str(li.get("category") or "").strip()
                if cat and cat in valid_categories:
                    cat_votes[cat] += 1
        majority_category = cat_votes.most_common(1)[0][0] if cat_votes else ""

        for idx, original in enumerate(items):
            item = dict(original)  # shallow copy
            llm = llm_by_idx.get(idx)

            if llm:
                # Category
                cat = str(llm.get("category") or "").strip()
                if cat and cat in valid_categories:
                    item["category"] = cat
                    n_categorized += 1
                else:
                    # Heuristic first, then majority vote from LLM
                    item["category"] = (
                        _infer_category(item.get("material", ""))
                        or majority_category
                    )

                # Standard name
                std_name = str(llm.get("standard_name") or "").strip()
                original_name = item.get("material", "")
                if std_name and std_name != original_name:
                    item["standard_name"] = std_name
                    item["original_name"] = original_name
                    item["name_note"] = str(llm.get("name_note") or "")
                    n_renamed += 1
                else:
                    item["standard_name"] = original_name
                    item["original_name"] = original_name
                    item["name_note"] = ""

                # Standard spec
                std_spec = str(llm.get("standard_spec") or "").strip()
                original_spec = item.get("spec", "")
                if std_spec and std_spec != original_spec:
                    item["standard_spec"] = std_spec
                    item["original_spec"] = original_spec
                else:
                    item["standard_spec"] = original_spec
                    item["original_spec"] = original_spec

                # Matched material ID
                matched_mid = llm.get("matched_material_id")
                if matched_mid is not None:
                    try:
                        matched_mid = int(matched_mid)
                        # Validate it exists
                        exists = db.query(Material.id).filter(
                            Material.id == matched_mid
                        ).first()
                        if exists:
                            item["matched_material_id"] = matched_mid
                        else:
                            item["matched_material_id"] = None
                    except (ValueError, TypeError):
                        item["matched_material_id"] = None
                else:
                    item["matched_material_id"] = None

                # Alignment note
                align_note = str(llm.get("alignment_note") or "").strip()
                if align_note:
                    item["alignment_note"] = align_note
                    n_aligned += 1
                else:
                    item["alignment_note"] = ""
            else:
                # LLM didn't return this item (beyond cap or missing).
                # Use heuristic first, fall back to majority category from LLM results.
                item["category"] = (
                    _infer_category(item.get("material", ""))
                    or majority_category
                )
                item["standard_name"] = item.get("material", "")
                item["original_name"] = item.get("material", "")
                item["standard_spec"] = item.get("spec", "")
                item["original_spec"] = item.get("spec", "")
                item["name_note"] = ""
                item["alignment_note"] = ""
                item["matched_material_id"] = None
                if item["category"]:
                    n_categorized += 1
                else:
                    n_errors += 1

            enhanced.append(item)

        summary = {
            "total": len(items),
            "categorized": n_categorized,
            "renamed": n_renamed,
            "aligned": n_aligned,
            "errors": n_errors,
        }

        return {
            "items": enhanced,
            "summary": summary,
            "tokens_used": tokens,
            "duration_ms": duration_ms,
        }

    except Exception as e:
        log.warning("enhance_ocr_items LLM call failed: %s", e)
        # Fallback: return items with heuristic category only
        for item in items:
            item["category"] = _infer_category(item.get("material", ""))
            item["standard_name"] = item.get("material", "")
            item["original_name"] = item.get("material", "")
            item["standard_spec"] = item.get("spec", "")
            item["original_spec"] = item.get("spec", "")
            item["name_note"] = ""
            item["alignment_note"] = ""
            item["matched_material_id"] = None
        return {
            "items": items,
            "summary": {"total": len(items), "categorized": 0, "renamed": 0,
                         "aligned": 0, "errors": len(items)},
            "error": str(e),
        }


def _infer_category(name: str) -> str:
    """Heuristic fallback: scan material name for a known category keyword."""
    for cat in ALL_CATEGORIES:
        if cat in name:
            return cat
    # Extended heuristics for common MEP items
    if any(kw in name for kw in ["阀", "过滤器", "止回", "减压", "倒流防止", "排气"]):
        return "阀门"
    if any(kw in name for kw in ["桥架", "线槽", "槽式", "托盘"]):
        return "桥架"
    if any(kw in name for kw in ["水泵", "消防泵", "排污泵"]):
        return "潜水泵"
    if any(kw in name for kw in ["风机", "盘管", "风口", "风阀"]):
        return "风机盘管"
    return ""
