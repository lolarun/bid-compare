"""vl_tender.py — 招标采购清单的 VL-direct 识别器。

与报价侧共用 `vl_direct` 的解析、结构门、方向预检与行数台账；**只在"有哪些列"上
不同**。复制一份解析器出来只会让两边慢慢漂移，所以差异全部收敛在本模块的
`TENDER_SLOTS` 与 `build_tender_fields` 两处。

## 与报价侧的三点实质差异

1. **没有价格。** 采购清单是留给投标人填的空表，价格列存在但为空。故
   `doc_type="tender"`，跳过"读到行却读不到钱→BLOCKED"那道门——否则每一份招标
   文件都会被误判。

2. **序号是行轴，不是参考。** `TenderAnchor.seq` 是比价矩阵唯一的行标识
   （CLAUDE.md §4）；报价侧序号缺失只是少了个校验，招标侧缺失就没有轴可用。
   故序号缺口在这里更严重，由 `vl_direct` 的序号门统一判定，本模块不另设阈值。

3. **两级表头。** 实测样本的「材质」跨 阀体/阀芯/阀板/阀杆/密封圈 五个子列。
   提示词要求用 `父列_子列` 拍平（通用做法，不是为这份样本定制），本模块再把
   `材质_*` 收进 `materials` 字典——`_draft_row_to_anchor` 正是这么读的。

## 品牌页不在这里

`brand_requirement` / `supplier_brands` 来自另一页、另一种版式，属于独立的抽取任务。
本模块只出清单锚点；品牌页仍走既有路径，见 `services/tender_pdf.py`。
"""
from __future__ import annotations

import io
import logging
import re
from typing import Sequence

from apps.api.core.config import get_settings
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.extraction_draft import ExtractionDraft
from apps.api.intelligence.vl_direct import (
    ORIENT_PROBE_MAX_EDGE_PX,
    PROMPT_ORIENT,
    RENDER_BATCH,
    OrientCall,
    VLCall,
    _num,
    build_draft,
    detect_rotations,
)

log = logging.getLogger(__name__)

PROMPT_TENDER_CSV = """请将这份招标文件中的采购清单（材料/设备明细表）导出为 CSV 格式给我。只返回 CSV，不要其他说明。

另外遵守四条规则：
1. 表头如果是两级的（上面一个大类、下面几个子列），请拍平成"父列_子列"，
   例如父列写「材质」、子列写「阀体」，就输出 材质_阀体。
2. 只转录文档上确实写着的内容。单元格为空或看不清就留空，不要推断、不要补齐。
   清单里的单价、合价等列通常是空的（留给投标人填），空着就是空着。
3. 序号列必须完整逐行转录，一行都不能少也不要重编号——后续所有比价都以它为准。
   第一列固定为 row_type：明细行填 detail，小计行填 subtotal，合计行填 total。
4. 最后一列固定为 page，填该行来自第几页（按我给你的图像顺序，从 1 开始）。"""

# 招标清单的槽位。与报价共用同名槽位（seq/name/spec/unit/qty/brand/remark/page），
# 另加招标专有的 model / pressure / profession；**没有任何价格槽位**。
TENDER_SLOTS: dict[str, list[tuple[str, ...]]] = {
    "name":       [("项目名称",), ("材料名称",), ("设备名称",), ("名称",), ("品名",),
                   ("name",), ("item",)],
    "spec":       [("规格型号",), ("规格",), ("spec",)],
    "model":      [("型号",), ("model",)],
    "pressure":   [("工作压力",), ("压力",), ("pressure",)],
    "profession": [("专业",), ("系统",), ("profession",)],
    "unit":       [("计量单位",), ("单位",), ("unit",)],
    "qty":        [("数量",), ("工程量",), ("quantity",), ("qty",)],
    "brand":      [("品牌",), ("厂家",), ("brand",)],
    "remark":     [("备注",), ("remark",), ("note",)],
    "seq":        [("序号",), ("seq",), ("no",)],
    "row_type":   [("row_type",)],
    "page":       [("page",), ("页码",), ("页",)],
}

# 「材质」子列的识别。取父列前缀而不是穷举子列名（阀体/阀芯/阀板…），
# 因为子列名随品类变化——阀门是阀体阀芯，桥架就完全是另一套。
_MATERIAL_PREFIXES = ("材质", "material_")


def build_tender_fields(cell, raw_cells: dict, cmap: dict) -> dict:
    """招标清单行字段。

    `materials` 收所有「材质_*」子列。**不做归一、不猜缺省**：`_draft_row_to_anchor`
    只保留非空值，空的子列本来就该是空的（不是每种阀门都有阀板）。
    """
    mapped = set(cmap.values())
    materials = {}
    for header, value in raw_cells.items():
        if header in mapped or not str(value or "").strip():
            continue
        low = header.lower()
        if any(low.startswith(p) or p in low for p in _MATERIAL_PREFIXES):
            # 「材质_阀体」→「阀体」；没有父列前缀的（凯硕那种裸「阀体」列）保持原样
            key = header.split("_", 1)[1] if "_" in header else header
            materials[key.strip()] = str(value).strip()
    return {
        "seq": cell("seq").strip(),
        "name": cell("name").strip(),
        "spec": cell("spec").strip(),
        "model": cell("model").strip(),
        "pressure": cell("pressure").strip(),
        "profession": cell("profession").strip(),
        "unit": cell("unit").strip(),
        "brand": cell("brand").strip(),
        "remark": cell("remark").strip(),
        "qty": _num(cell("qty")),
        "materials": materials,
    }


def build_tender_draft(text: str, *, file_path: str, page_count: int,
                       processed_pages: list[int],
                       rotations: dict[int, int] | None = None,
                       unresolved_pages: Sequence[int] = ()) -> ExtractionDraft:
    """招标 CSV → ExtractionDraft。结构门与报价侧完全一致，只换列表与字段。"""
    return build_draft(
        text, file_path=file_path, page_count=page_count,
        processed_pages=processed_pages, rotations=rotations,
        unresolved_pages=unresolved_pages,
        doc_type="tender", slots=TENDER_SLOTS, field_builder=build_tender_fields,
    )


def recognize_tender_vl(file_path: str, *, vl_call: VLCall,
                        orient_call: OrientCall | None = None,
                        progress_cb=None, votes: int | None = None,
                        target_pages: list[int] | None = None) -> ExtractionDraft:
    """生产入口：招标 PDF → ExtractionDraft（锚点行）。

    `target_pages` 是用户手动指定的清单页（1-based）。不指定就整份送——招标文件
    通常几十页而清单只占几页，但**页面角色分类尚未在 VL 侧实现**，整份送是当前
    唯一诚实的做法：宁可多花钱，不要靠猜跳过页（见 docs/design/21 §2.4）。
    """
    from PIL import Image

    s = get_settings()
    votes = votes if votes is not None else int(getattr(s, "QUOTE_ORIENT_VOTES", 3))
    page_count = DocumentLoader.get_page_count(file_path)
    pages = [p for p in (target_pages or range(1, page_count + 1)) if 1 <= p <= page_count]

    def _notify(stage: str, pct: int) -> None:
        if progress_cb:
            progress_cb(stage, pct)

    _notify("渲染页面", 15)
    images: dict[int, bytes] = {}
    for start in range(0, len(pages), RENDER_BATCH):
        images.update(DocumentLoader.render_pages(file_path, pages[start:start + RENDER_BATCH]))

    rotations: dict[int, int] = {}
    unresolved: list[int] = []
    if orient_call is not None:
        _notify("方向预检", 30)
        rotations, unresolved = detect_rotations(images, orient_call, votes=votes)
        for p, deg in rotations.items():
            with Image.open(io.BytesIO(images[p])) as im:
                buf = io.BytesIO()
                im.convert("RGB").rotate(-deg, expand=True).save(buf, "PNG")
                images[p] = buf.getvalue()

    _notify("识别采购清单", 55)
    text = vl_call([images[p] for p in pages if p in images], PROMPT_TENDER_CSV)

    _notify("整理结果", 90)
    return build_tender_draft(
        text, file_path=file_path, page_count=page_count,
        processed_pages=[p for p in pages if p in images],
        rotations=rotations, unresolved_pages=unresolved,
    )
