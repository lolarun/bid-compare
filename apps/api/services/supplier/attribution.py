"""design/28 §4.2 供应商归属：一份已上传的投标文件到底属于哪家供应商。

单文件问题，不是跨文件配对问题（§4.2 明确把它跟旧的"pairing problem"划清
界限——那个问题在 §4 clarification 后已经不存在了，剩下的只是"这一份文件
认领给谁"）。

判据优先级（§5 red line 2 是硬约束，不是建议）：
  1. 封面识别抽到的 supplier_name（`_doc_meta.supplier_name` 或顶层
     `supplier_name`）—— 主信号。
  2. 文件名——**仅供参考，永不用来做决定**。命中/未命中都不改变 source，
     只在抽取信号缺失时才作为弱候选提供给 resolve_supplier 查一次；哪怕
     resolve 出结果，也标成 source="filename_hint"，跟"抽取信号命中"的
     source="extraction" 严格区分，前端展示口径不同（后者可以预选，前者
     必须显式确认）。
  3. 两者都没有 → source="unresolved"，不出现在待选项里，交给确认屏走
     "手动选择/新建" 的既有路径（§4.2："never guessed silently"）。

复用既有 `resolve_supplier`（7 层供应商别名解析），不重新发明模糊匹配。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from apps.api.services.supplier.supplier_resolve import ResolveResult, resolve_supplier

AttributionSource = Literal["extraction", "filename_hint", "unresolved"]

# 跟 apps/www/src/composables/useSupplierUpload.ts::_extractSupplierHintFromFilename
# 保持同一条切分规则（按常见"标书用语"字符切分，取第一段）——前后端各自
# 维护一份轻量同构实现是刻意的：这条启发式本身就"仅供参考"，没有共享成
# 精确契约的必要，比引入一个跨语言共享层更简单（§5 red line 2 决定了它
# 的地位——错了也无所谓，反正不会被当真）。
_FILENAME_SPLIT = re.compile(r"[投标报价文件单_\-\s··【】()（）]+")
_EXT_STRIP = re.compile(r"\.(pdf|xlsx?|csv|docx?)$", re.IGNORECASE)


@dataclass
class SupplierAttribution:
    supplier_name: str | None      # 最终采信的候选名；None 表示彻底无信号
    source: AttributionSource
    resolve: ResolveResult | None  # resolve_supplier() 的完整结果，供确认屏展示候选/引导新建
    filename_hint: str | None      # 文件名启发式抽到的候选，即便 source="extraction" 也记录，供审计对比（从不参与决策）


def extract_filename_hint(filename: str) -> str:
    base = _EXT_STRIP.sub("", filename)
    parts = _FILENAME_SPLIT.split(base)
    hint = (parts[0] if parts else "").strip()
    # 纯数字（扫描批次号、系统生成的临时文件名之类）不是候选供应商名，
    # 当没有提示处理——"文件名是提示"不等于"文件名的任何残片都算数"。
    if hint.isdigit():
        return ""
    return hint


def _extraction_supplier_name(job_result: dict) -> str:
    doc_meta = job_result.get("_doc_meta") or {}
    name = doc_meta.get("supplier_name") or job_result.get("supplier_name") or ""
    return str(name).strip()


def attribute_supplier(db: Session, job_result: dict, filename: str) -> SupplierAttribution:
    filename_hint = extract_filename_hint(filename) or None
    extracted = _extraction_supplier_name(job_result)

    if extracted:
        return SupplierAttribution(
            supplier_name=extracted, source="extraction",
            resolve=resolve_supplier(db, extracted),
            filename_hint=filename_hint,
        )

    if filename_hint:
        return SupplierAttribution(
            supplier_name=filename_hint, source="filename_hint",
            resolve=resolve_supplier(db, filename_hint),
            filename_hint=filename_hint,
        )

    return SupplierAttribution(
        supplier_name=None, source="unresolved", resolve=None, filename_hint=None,
    )
