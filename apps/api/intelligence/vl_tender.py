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

## 一次解析，两个消费方

招标（比价）与邀标对**招标文件解析能力的要求是一致的**：都要采购清单，也都要封面
标量（项目名称/编号/招标单位/日期/截止时间）。差别只在下游怎么用——比价把清单转成
TenderAnchor，邀标把标量存进招标记录。所以解析只有一份
（`parse_tender_document`），两个入口各自映射输出。

封面标量在比价链路里**目前没有消费方**，但仍然抽取并返回：供应商推荐将来会用到，
而"现在没人读"不是不抽的理由——一份解析器对同一种文档应当产出同样的东西。

## 招标要求可扩展

采购清单之外，招标文件还有若干项对投标人的要求（品牌要求、参与品牌，将来可能有
工期、质保、付款方式）。它们由 `TenderRequirement` **声明为数据**：加一项 = 加一条
配置，不改解析逻辑、不加模型调用。一次调用问全部，而不是每项一次——这些要求散落
在同几页里，逐项调用等于把同样的图片重复送 N 遍，且分开问容易让模型混淆
"业主要求的品牌"与"各家申报的品牌"。
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from typing import Sequence

from apps.api.core.config import get_settings
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.extraction_draft import ExtractionDraft
from apps.api.intelligence.vl_quote import (
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
                       unresolved_pages: Sequence[int] = (),
                       parser_mode: str = "vl_direct") -> ExtractionDraft:
    """招标 CSV → ExtractionDraft。结构门与报价侧完全一致，只换列表与字段。

    `parser_mode`：docs/design/25 轨A（文字层直抽）传 "text_layer"，CSV 来自
    pdfplumber 而非视觉模型——透传给 build_draft，不在这层丢掉。
    """
    return build_draft(
        text, file_path=file_path, page_count=page_count,
        processed_pages=processed_pages, rotations=rotations,
        unresolved_pages=unresolved_pages,
        doc_type="tender", slots=TENDER_SLOTS, field_builder=build_tender_fields,
        parser_mode=parser_mode,
    )


@dataclass
class TenderParseResult:
    """一次招标文件解析的全部产出。

    比价与邀标共用它，各取所需——**不给两条流程各写一个解析器**，那样两边会慢慢
    漂移，同一份 PDF 在两个入口给出不同的清单。
    """
    draft: ExtractionDraft                  # 采购清单（锚点行）
    meta: dict                              # 封面标量
    requirements: dict                      # 招标要求（品牌等，可扩展）
    rotations: dict
    unresolved_pages: list


def parse_tender_document(file_path: str, *, vl_call: VLCall,
                          orient_call: OrientCall | None = None,
                          progress_cb=None, votes: int | None = None,
                          target_pages: list[int] | None = None,
                          with_meta: bool = True,
                          requirements: Sequence["TenderRequirement"] | None = None,
                          ) -> TenderParseResult:
    """招标 PDF → 采购清单 + 封面标量。**渲染只做一次**，两项共用同一批图像。

    `target_pages` 是用户手动指定的清单页（1-based）。不指定就整份送——招标文件
    通常几十页而清单只占几页，但**页面角色分类尚未在 VL 侧实现**，整份送是当前
    唯一诚实的做法：宁可多花钱，不要靠猜跳过页（见 docs/design/21 §2.4）。

    指定了清单页时，封面页仍会额外渲染——封面通常不在清单页里，漏掉它就等于
    静默丢掉封面标量。
    """
    from PIL import Image

    s = get_settings()
    votes = votes if votes is not None else int(getattr(s, "QUOTE_ORIENT_VOTES", 3))
    page_count = DocumentLoader.get_page_count(file_path)
    list_pages = [p for p in (target_pages or range(1, page_count + 1))
                  if 1 <= p <= page_count]
    meta_pages = ([p for p in range(1, META_PAGES + 1) if p <= page_count]
                  if with_meta else [])
    need = sorted(set(list_pages) | set(meta_pages))

    def _notify(stage: str, pct: int) -> None:
        if progress_cb:
            progress_cb(stage, pct)

    _notify("渲染页面", 15)
    images: dict[int, bytes] = {}
    for start in range(0, len(need), RENDER_BATCH):
        images.update(DocumentLoader.render_pages(file_path, need[start:start + RENDER_BATCH]))

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
    text = vl_call([images[p] for p in list_pages if p in images], PROMPT_TENDER_CSV)

    _notify("读取封面信息", 80)
    meta = (extract_tender_meta([images[p] for p in meta_pages if p in images], vl_call)
            if with_meta else {k: "" for k in _META_KEYS})

    # 要求（品牌等）散在清单之外的页里。**送整份**：它们的位置不固定，而页面角色
    # 分类尚未在 VL 侧实现——按固定页号去取就是拿样本当规律。
    _notify("读取招标要求", 85)
    reqs = tuple(requirements if requirements is not None
                 else DEFAULT_TENDER_REQUIREMENTS)
    req_out = (extract_tender_requirements(
        [images[p] for p in sorted(images)], vl_call, reqs) if reqs else {})

    _notify("整理结果", 90)
    draft = build_tender_draft(
        text, file_path=file_path, page_count=page_count,
        processed_pages=[p for p in list_pages if p in images],
        rotations=rotations, unresolved_pages=unresolved,
    )
    draft.meta["tender_meta"] = meta
    draft.meta["tender_requirements"] = req_out
    return TenderParseResult(draft=draft, meta=meta, requirements=req_out,
                             rotations=rotations, unresolved_pages=unresolved)


def recognize_tender_vl(file_path: str, **kw) -> ExtractionDraft:
    """只要清单时的薄封装。保留是因为它是 `extract_bidlist` 的直接调用形态。"""
    return parse_tender_document(file_path, **kw).draft


# ─── 封面元信息 ──────────────────────────────────────────────────────────────
#
# 与采购清单是**两件事**：清单是表格、逐行；封面元信息是几个文档级标量，散落在
# 首页的标题与落款里。硬塞进同一个 CSV 会让两者互相拖累——清单的行数校验对标量
# 无意义，标量的缺失也不该让整份清单降级。
#
# 这几个字段有真实消费方（邀标流程 /invite/save 会存），所以 `extract_tender`
# 切到 VL 时**必须一并提供**，否则就是静默清空。

PROMPT_TENDER_META = """这是一份招标文件的首页。请告诉我下面五项，每行一个，格式 key: value：

project_name  项目名称
project_code  项目编号/招标编号
tenderer      招标单位/招标人/采购人全称（发出这份招标文件的单位，不是投标单位）
tender_date   招标日期
deadline      投标截止时间

文档上没写的就留空，不要推测。只返回这五行，不要其他说明。"""

# tenderer（招标单位）是 design/29 §10 req4"卡片徽标后显示单位名称"的唯一
# 数据来源——招标侧此前只抽项目名/编号/日期，没有任何字段承载"这份文件是
# 谁发出的"。不用 project_name 顶替：项目名不是单位名，拿它冒充等于让卡片
# 陈述一件文档没说过的事。
_META_KEYS = ("project_name", "project_code", "tenderer", "tender_date", "deadline")
# 首页够了。招标文件常有几十页，为几个封面标量把整份送进去是纯浪费；
# 取前两页是为了容忍封面之后紧跟一页"招标公告"的排版。
META_PAGES = 2


def parse_tender_meta(text: str) -> dict[str, str]:
    """`key: value` 逐行 → 字典。认不出的行忽略，**不猜**。"""
    out = {k: "" for k in _META_KEYS}
    for line in (text or "").splitlines():
        if ":" not in line and "：" not in line:
            continue
        key, _, value = line.replace("：", ":").partition(":")
        key = key.strip().lower()
        if key in out:
            out[key] = value.strip()
    return out


def extract_tender_meta(images: list[bytes], vl_call: VLCall) -> dict[str, str]:
    """首页 → 四个文档级标量。失败不抛异常——**清单才是主线**，
    元信息缺失应当留空并可见，不该让整份识别失败。"""
    if not images:
        return {k: "" for k in _META_KEYS}
    try:
        return parse_tender_meta(vl_call(images[:META_PAGES], PROMPT_TENDER_META))
    except Exception:                                            # noqa: BLE001
        log.warning("招标封面元信息抽取失败，封面标量留空", exc_info=True)
        return {k: "" for k in _META_KEYS}


# ─── 招标要求（可扩展）──────────────────────────────────────────────────────
#
# 招标文件除采购清单外还有若干项对投标人的要求：品牌要求、投标单位参与品牌，
# 将来可能还有工期、质保、付款方式……**要求是数据不是代码**：加一项 = 加一条
# `TenderRequirement`，不改解析逻辑，也不加新的模型调用。
#
# 一次调用问全部，而不是每项一次：
#  · 这些要求散落在同几页里，逐项调用等于把同样的图片重复送 N 遍；
#  · 它们之间有互斥关系（品牌表里既有"业主要求品牌"又有"各家参与品牌"，
#    分开问模型容易把两者搞混），一次给全反而更准。
#
# 失败不拖垮清单：要求读不出应当留空且可见——清单才是主线。


@dataclass(frozen=True)
class TenderRequirement:
    """一项要求的抽取声明。

    `shape` 只影响解析：table → CSV 逐行；text → 原文整段。**不做语义校验**，
    要求的内容是什么由下游决定，识别层只负责照实取回来。
    """
    key: str          # 产出字典里的键
    title: str        # 出现在提示词与返回分节里的名字
    hint: str         # 一句话说明去哪找、什么形态
    shape: str = "table"      # table | text


# 默认要求集。**不是穷举**，调用方可以传自己的一组。
# 品牌两项拆开是因为它们语义不同：一个是业主的约束，一个是各投标单位的申报。
DEFAULT_TENDER_REQUIREMENTS: tuple[TenderRequirement, ...] = (
    TenderRequirement(
        key="brand_requirement", title="业主品牌要求",
        hint="业主允许或指定的品牌清单。品牌常是英文+中文组合，"
             "输出两列 brand_en,brand_cn；只有一种时另一列留空。"),
    TenderRequirement(
        key="supplier_brands", title="投标单位参与品牌",
        hint="每个投标单位一行，输出两列 supplier_name,brand。"
             "supplier_name 是公司全称，brand 是该单位申报的品牌。"
             "不要把品牌当成单位，也不要把单位当成品牌。"),
    TenderRequirement(
        key="material_class", title="材料类别",
        hint="本次招标的材料大类，一个词即可。", shape="text"),
)

_SECTION = re.compile(r"^###\s*(.+?)\s*$")


def build_requirements_prompt(reqs: Sequence[TenderRequirement]) -> str:
    """按要求集生成提示词。加一项要求只会让这里多一行，不改结构。"""
    lines = [f"- {r.title}：{r.hint}" for r in reqs]
    shapes = "；".join(f"{r.title} 用{'CSV 表格' if r.shape == 'table' else '原文文字'}"
                       for r in reqs)
    return (
        "这份招标文件里有若干项对投标人的要求。请逐项提取。\n\n"
        "需要提取的是：\n" + "\n".join(lines) + "\n\n"
        f"输出格式：每项以 ### 加名称单独起一节，节内容按 {shapes}。\n"
        "文档里没有的那一项，仍然输出 ### 名称，节内留空。不要推测、不要合并。\n"
        "只返回这些分节，不要其他说明。"
    )


def parse_requirements(text: str, reqs: Sequence[TenderRequirement]) -> dict:
    """`### 名称` 分节 → 字典。认不出的分节忽略；**缺的项留空而不是缺键**，
    否则下游分不清"没这一项"和"这次没读到"。"""
    by_title = {r.title: r for r in reqs}
    out: dict = {r.key: ([] if r.shape == "table" else "") for r in reqs}
    current: TenderRequirement | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is None:
            return
        body = "\n".join(buf).strip()
        if current.shape == "text":
            out[current.key] = body
        else:
            rows = [r for r in csv.reader(io.StringIO(body))
                    if any((c or "").strip() for c in r)]
            if len(rows) >= 2:
                header = [h.strip() for h in rows[0]]
                out[current.key] = [
                    {h: (r[i].strip() if i < len(r) else "")
                     for i, h in enumerate(header) if h}
                    for r in rows[1:]
                ]

    for line in (text or "").splitlines():
        m = _SECTION.match(line.strip())
        if m:
            flush()
            current, buf = by_title.get(m.group(1).strip()), []
            continue
        if current is not None:
            buf.append(line)
    flush()
    return out


def extract_tender_requirements(
    images: list[bytes], vl_call: VLCall,
    reqs: Sequence[TenderRequirement] = DEFAULT_TENDER_REQUIREMENTS,
) -> dict:
    """一次调用取回全部要求。失败留空——清单才是主线。"""
    empty = {r.key: ([] if r.shape == "table" else "") for r in reqs}
    if not images:
        return empty
    try:
        return parse_requirements(vl_call(images, build_requirements_prompt(reqs)), reqs)
    except Exception:                                            # noqa: BLE001
        log.warning("招标要求抽取失败，各项留空", exc_info=True)
        return empty
