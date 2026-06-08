"""招标清单解析 — 结构化 Excel → 锚点行(TenderAnchor)。

这是 `docs/design/05-比价流程的智能化分层.md` §9 第 1 步:把招标清单(工程量
清单)解析成一组"锚点行",作为比价矩阵的纵轴骨架与供应商报价匹配的目标。

第一版只支持**规范表头**(列名可识别;材质可拆多子列)。表头怪异(无标题行、
多级合并到无法按列名识别)的情况退 LLM 读表头,留作后续迭代——见设计文档 §8。

解析是纯代码、确定性的(智能化程度极低,见设计文档 §2),不调用任何 LLM。

通用性:不写死列位置,靠**列名同义识别**列。任何品类的清单只要是带可识别表头
的表格即可解析;品类差异体现在"材质/扩展属性"这些 freeform 列,匹配阶段再用。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import openpyxl


# ── 列名同义映射:把表头单元格文本归一到标准字段 ──────────────────────────
# 第一版规范表头;识别不到的列进 raw(freeform),匹配阶段可用。
_COL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "seq":        ("序号", "序", "编号"),
    "profession": ("专业",),
    "name":       ("项目名称", "材料名称", "材料(设备)名称", "材料（设备）名称", "名称", "品名", "货物名称"),
    "spec":       ("规格", "规格型号"),
    "model":      ("型号",),
    "pressure":   ("公称压力", "压力", "PN"),
    "material":   ("材质",),
    "unit":       ("单位", "计量单位"),
    "qty":        ("数量", "工程量", "招标数量"),
    "brand":      ("品牌",),
    "remark":     ("备注", "说明"),
}

# 材质常见子列(多列归一到一个 materials 字典)。识别不到子标签则按位置编号。
_MATERIAL_SUBCOLS = ("阀体", "阀芯", "阀板", "阀座", "密封圈", "材料", "本体", "衬里")

# 数据区终止信号:序号列出现这些文本视为表尾(合计/说明行)。
_FOOTER_MARKERS = ("含税", "合价", "合计", "说明", "备注：", "总计", "小计")


@dataclass
class TenderAnchor:
    """一条招标清单锚点行。"""

    seq: Any                              # 序号(原值)
    name: str                            # 项目/材料名称
    spec: str = ""                       # 规格(常含 DN)
    model: str = ""                      # 型号
    pressure: str = ""                   # 公称压力(常含 PN)
    materials: dict[str, str] = field(default_factory=dict)  # {子列名: 材质文本}
    unit: str = ""
    qty: float | None = None
    brand: str = ""
    profession: str = ""
    remark: str = ""
    row_index: int = -1                  # 源行号(0-based),便于回溯
    raw: dict[str, str] = field(default_factory=dict)        # 未识别列的 freeform

    def material_text(self) -> str:
        """材质拼成单串(供匹配/展示)。"""
        return "/".join(v for v in self.materials.values() if v)


def parse_tender_xlsx(source: str | bytes | io.BytesIO) -> list[TenderAnchor]:
    """解析招标清单 xlsx,返回锚点行列表。

    Args:
        source: 文件路径、字节内容或 BytesIO。

    Returns:
        TenderAnchor 列表(已剔除标题行、表头行、表尾说明行)。

    Raises:
        ValueError: 找不到可识别的表头(规范表头缺失)。
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    wb = openpyxl.load_workbook(source, data_only=True, read_only=True)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not rows:
        raise ValueError("空工作表")

    header_idx = _find_header_row(rows)
    if header_idx is None:
        raise ValueError("找不到可识别的表头(第一版仅支持规范表头;序号/名称/数量缺失)")

    header = rows[header_idx]
    colmap, mat_cols, has_subheader = _map_columns(rows, header_idx)

    data_start = header_idx + (2 if has_subheader else 1)
    anchors: list[TenderAnchor] = []
    for ri in range(data_start, len(rows)):
        row = rows[ri]
        seq = _cell(row, colmap.get("seq"))
        # 数据区终止:序号空 或 命中表尾标记
        if seq is None or str(seq).strip() == "":
            continue
        if any(m in str(seq) for m in _FOOTER_MARKERS):
            break
        name = _cell(row, colmap.get("name"))
        if name is None or str(name).strip() == "":
            continue  # 无名称行跳过(分隔/空行)

        materials: dict[str, str] = {}
        for label, ci in mat_cols:
            v = _cell(row, ci)
            if v is not None and str(v).strip():
                materials[label] = str(v).strip()

        # 未识别列 → raw
        raw: dict[str, str] = {}
        known_cols = set(colmap.values()) | {ci for _, ci in mat_cols}
        for ci, hv in enumerate(header):
            if ci in known_cols or hv is None or str(hv).strip() == "":
                continue
            v = _cell(row, ci)
            if v is not None and str(v).strip():
                raw[str(hv).strip()] = str(v).strip()

        anchors.append(TenderAnchor(
            seq=seq,
            name=str(name).strip(),
            spec=_str(row, colmap.get("spec")),
            model=_str(row, colmap.get("model")),
            pressure=_str(row, colmap.get("pressure")),
            materials=materials,
            unit=_str(row, colmap.get("unit")),
            qty=_num(_cell(row, colmap.get("qty"))),
            brand=_str(row, colmap.get("brand")),
            profession=_str(row, colmap.get("profession")),
            remark=_str(row, colmap.get("remark")),
            row_index=ri,
            raw=raw,
        ))
    return anchors


# ── helpers ────────────────────────────────────────────────────────────────
def _find_header_row(rows: list[list], scan_limit: int = 10) -> int | None:
    """表头行 = 前若干行中,同时出现"序号(类)"和"名称(类)"的那一行。"""
    seq_kw = _COL_SYNONYMS["seq"]
    name_kw = _COL_SYNONYMS["name"]
    for i, row in enumerate(rows[:scan_limit]):
        texts = [str(c).strip() for c in row if c is not None]
        has_seq = any(any(k == t for k in seq_kw) for t in texts)
        has_name = any(any(k in t for k in name_kw) for t in texts)
        if has_seq and has_name:
            return i
    return None


def _map_columns(
    rows: list[list], header_idx: int,
) -> tuple[dict[str, int], list[tuple[str, int]], bool]:
    """把表头列映射到标准字段;识别材质多子列(可能用到下一行子表头)。

    Returns:
        (colmap, material_cols, has_subheader)
        colmap: {标准字段: 列号}
        material_cols: [(子列标签, 列号), ...]
        has_subheader: 材质是否使用了下一行作为子表头
    """
    header = rows[header_idx]
    sub = rows[header_idx + 1] if header_idx + 1 < len(rows) else []

    colmap: dict[str, int] = {}
    for ci, cell in enumerate(header):
        if cell is None:
            continue
        text = str(cell).strip()
        if not text:
            continue
        for fld, syns in _COL_SYNONYMS.items():
            if fld == "material":
                continue  # 材质单独处理
            if fld in colmap:
                continue
            if any(s == text for s in syns) or any(s in text for s in syns):
                colmap[fld] = ci
                break

    # 材质列:找到主表头"材质"所在列;若其右侧主表头为空而子表头有标签 → 多子列
    mat_cols: list[tuple[str, int]] = []
    has_subheader = False
    mat_start = None
    for ci, cell in enumerate(header):
        if cell is not None and str(cell).strip() in _COL_SYNONYMS["material"]:
            mat_start = ci
            break
    if mat_start is not None:
        # 向右扩展:主表头为空(被合并)的列归入材质组
        ci = mat_start
        while ci < len(header):
            main = header[ci]
            if ci > mat_start and main is not None and str(main).strip():
                break  # 遇到下一个主表头,材质组结束
            sub_label = ""
            if ci < len(sub) and sub[ci] is not None:
                sub_label = str(sub[ci]).strip()
            if sub_label and sub_label in _MATERIAL_SUBCOLS:
                has_subheader = True
                mat_cols.append((sub_label, ci))
            elif sub_label:
                has_subheader = True
                mat_cols.append((sub_label, ci))
            else:
                # 无子标签:单列材质
                mat_cols.append(("材质", ci))
            ci += 1
        # 去重保序
        seen = set()
        mat_cols = [(l, c) for l, c in mat_cols if not (c in seen or seen.add(c))]

    return colmap, mat_cols, has_subheader


def _cell(row: list, ci: int | None):
    if ci is None or ci >= len(row):
        return None
    return row[ci]


def _str(row: list, ci: int | None) -> str:
    v = _cell(row, ci)
    return "" if v is None else str(v).strip()


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    try:
        return float(s)
    except ValueError:
        return None
