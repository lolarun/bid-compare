"""ExtractionPipeline — orchestrates loader → provider → post-processing.

Two public methods:
- extract_tender(file_path) → ExtractionResponse (data matches TENDER_SCHEMA)
- extract_quote(file_path, context) → ExtractionResponse (data matches QUOTE_SCHEMA)

Both are VL-direct only (apps/api/intelligence/vl_quote.py / vl_tender.py):
the whole document renders once, goes to the vision model as one call, and
comes back as CSV → ExtractionDraft. The legacy per-page OCR→HTML→TableGrid
chain (batched multi-page execution, page-role classification, result
aggregation) was deleted 2026-08-11 (best-practice review F1/F2) — both
shipped providers implement vl_extract_csv, so that chain was never reachable
in production. A provider without vl_extract_csv raises, rather than
silently falling back to a path that had zero real callers.

Post-processing:
- coerces numeric fields (qty / unit_price / total_price)
- strips whitespace on strings
- best-effort category inference for tender items
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from apps.api.core.config import get_settings
from apps.api.core.utils import parse_num
from apps.api.intelligence.base import LLMProvider, ExtractionResponse
from apps.api.intelligence.extraction_draft import DETAIL_ROW_TYPE
from apps.api.intelligence.quote_fact import build_canonical, apply_arithmetic_validation
from apps.api.intelligence.price_basis import derive_price_basis

log = logging.getLogger(__name__)

# Used by category inference; matches apps/api/core/config.py ALL_CATEGORIES
KNOWN_CATEGORIES = [
    "桥架", "母线槽", "配电箱", "电缆", "阀门", "不锈钢管",
    "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵",
]

# design/24 B2：progress_cb 的调用方仍可只传 (stage, pct) 两个位置参数——
# stage_current/stage_total 是 Callable 类型省略不了的关键字参数，靠各处调用点
# 自己按需传，类型标注保持宽松（Any-arity）不强行细化。
ProgressCallback = Callable[..., None]


class ExtractionPipeline:
    """Coordinates document loading, VL-direct extraction, and structured post-processing."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    # ─── public API ───────────────────────────────────────────────────────
    def extract_tender(
        self,
        file_path: str,
        progress_cb: ProgressCallback | None = None,
    ) -> ExtractionResponse:
        _notify(progress_cb, "渲染PDF", 10)
        t_start = time.time()

        # 招标（比价）与邀标对**招标文件解析能力的要求是一致的**：都要采购清单，
        # 也都要封面四标量。故两个入口共用同一份解析结果（`parse_tender_document`/
        # `parse_tender_document_paddle`），只在输出映射上不同——本方法映射成
        # TENDER_SCHEMA，`extract_tender_bidlist` 映射成 TenderAnchor。给两条
        # 流程各写一个解析器，同一份 PDF 迟早会给出两种清单。
        from apps.api.intelligence.providers.mock import MockProvider

        if isinstance(self.provider, MockProvider):
            # MockProvider 是命名的测试替身（见 extract_quote 同款分支的完整
            # 理由），招标侧同样有既有集成测试依赖它的 canned 招标 CSV/meta
            # （`test_invite_integration.py` 等）——继续走旧的 vision-shaped
            # 调用契约，不切 Paddle。
            from apps.api.intelligence.vl_tender import parse_tender_document

            s = get_settings()
            parsed = parse_tender_document(
                file_path,
                vl_call=lambda imgs, prompt: self.provider.vl_extract_csv(
                    imgs, prompt, model=s.DASHSCOPE_QUOTE_VL_MODEL),
                orient_call=lambda parts, prompt: self.provider.vl_extract_csv(
                    [b for _t, b in parts], prompt,
                    model=s.DASHSCOPE_QUOTE_ORIENT_MODEL, labels=[t for t, _b in parts]),
                progress_cb=progress_cb,
            )
            return self._tender_draft_to_response(parsed, t_start)

        # 招标识别 = PaddleOCR-VL（design/26 P4 补，招标侧，2026-08-13）。首轮
        # P4 只切了报价侧，把招标侧落成了未完成项——design/26 的确定方案
        # （`a1738c4`/`1953a80`，早于本轮 P4 就定稿）是"Paddle 是所有扫描件
        # 唯一引擎，招标报价一视同仁"，这里补上。整份招标 PDF 只提交一次给
        # Paddle：`build_tender_csv`（表格 → 采购清单）与 `paddle_doc_meta`
        # （每页文字 → 封面标量/招标要求）共用同一次调用结果，不重复识别。
        from apps.api.intelligence.paddle_doc_meta import get_text_client_call
        from apps.api.intelligence.paddle_tender import parse_tender_document_paddle
        from apps.api.intelligence.providers import paddle_ocr

        page_count = _tender_page_count(file_path)
        parsed = parse_tender_document_paddle(
            file_path, submit_and_parse=paddle_ocr.submit_and_parse,
            text_call=get_text_client_call(), page_count=page_count,
            progress_cb=progress_cb,
        )
        return self._tender_draft_to_response(parsed, t_start)

    def _tender_draft_to_response(self, parsed, t_start: float) -> ExtractionResponse:
        """TenderParseResult → TENDER_SCHEMA 形状的 ExtractionResponse。

        与 `extract_tender_bidlist` 读的是**同一个 draft**，只是取的字段不同：
        这里要 TENDER_SCHEMA 的 name/category/spec/unit/quantity/remark，
        那边要 TenderAnchor 的 seq/pressure/materials/canonical。
        """
        draft = parsed.draft
        items = [
            {
                "name": r.fields.get("name") or "",
                # 品类由 _postprocess_tender 的既有推断补齐，识别侧不猜
                "category": "",
                "spec": r.fields.get("spec") or "",
                "unit": r.fields.get("unit") or "",
                "quantity": r.fields.get("qty"),
                "remark": r.fields.get("remark") or "",
                # 未落槽位的列原样带出——换个品类（桥架的表面处理等）全靠它
                "extended_attrs": dict(r.extra_fields or {}),
            }
            for r in draft.rows if r.row_type == DETAIL_ROW_TYPE
        ]
        data = {**parsed.meta, "items": items}
        # 识别来源诚实标注（design/26 §9）：从行本身携带的 parser_mode 读，
        # 不硬编码——Paddle 路径落 "paddle_vl"，MockProvider/qwen 路径落
        # "vl_direct"，不冒充彼此（`.claude/rules/recognition.md`）。
        recognizer = (draft.rows[0].fields.get("parser_mode") if draft.rows else None) or "vl_direct"
        resp = ExtractionResponse(
            data=self._postprocess_tender(data),
            raw_text="", confidence=1.0, tokens_used=0,
            provider=getattr(self.provider, "name", ""),
            duration_ms=int((time.time() - t_start) * 1000),
            metadata={
                "doc_type": "tender",
                "recognizer": recognizer,
                "quality_status": draft.quality.status,
                "quality_blocking_reasons": list(draft.quality.blocking_reasons or []),
                "row_ledger": draft.ledger.to_dict() if draft.ledger else None,
                "rotations": parsed.rotations,
                "orientation_unresolved": parsed.unresolved_pages,
            },
        )
        return resp

    def extract_tender_bidlist(
        self,
        file_path: str,
        progress_cb: ProgressCallback | None = None,
        bidlist_pages: list[int] | None = None,
        brand_page: int | None = None,
    ) -> dict[str, Any]:
        """招标文件 PDF → 投标清单锚点 + 品牌映射（返回 dict，非 ExtractionResponse）。

        委托给 services.tender_pdf；复用本 pipeline 的 provider（OCR 能力）。
        """
        from apps.api.services.tender.tender_pdf import extract_bidlist
        return extract_bidlist(
            file_path, self.provider, progress_cb=progress_cb,
            bidlist_pages=bidlist_pages, brand_page=brand_page,
        )

    def extract_quote(
        self,
        file_path: str,
        context: dict[str, Any] | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ExtractionResponse:
        _notify(progress_cb, "渲染PDF", 10)
        t_start = time.time()

        # 报价识别 = PaddleOCR-VL（design/26 P4 cutover，2026-08-13）。整份 PDF
        # 提交给百度云，结构化 JSON → CSV → ExtractionDraft，走的是跟
        # `apps.api.intelligence.paddle_vl.build_quote_csv` 完全一样的
        # `build_draft()` 出口，字段级校验/质量分级零改动继承。
        #
        # 不再走 `self.provider.vl_extract_csv`——Paddle 的原生 API（整份文件
        # 提交 + 异步轮询 + 结构化 JSON）跟 `LLMProvider.vl_extract_csv`
        # （per-page 图像 + 自由文本指令 → CSV 文本）形状完全不同，从 P1
        # 阶段就已经确认过（design/26 §5），不是能力探测就能兼容的差异。
        # `.claude/rules/recognition.md` 禁止"provider 不具备某能力时静默降级
        # 到另一条路"——这里不是那种情况：报价识别现在**只有** Paddle 一条
        # 路，`self.provider`（qwen/DashScope）继续只服务招标（`extract_tender`/
        # `extract_tender_bidlist`）——招标侧还没有 Paddle 适配器、也从未在
        # design/26 的 P1/P2 里被评测过（那 7 份验收文档全部是报价文档），
        # 在有证据之前不能把它也切过来（design/26 §9 的既定条件："`vl_tender.py`
        # 只在 Paddle 适配器覆盖了扫描招标件之后才退场"，条件目前不成立）。
        from apps.api.intelligence.document_loader import DocumentLoader
        from apps.api.intelligence.providers.mock import MockProvider

        page_count = DocumentLoader.get_page_count(file_path)

        # MockProvider 是命名的、显式的测试替身（"deterministic stub for tests
        # and fallback when API key absent"，见其自身 docstring），不是"provider
        # 缺某个能力就悄悄换一条生产路径"——`.claude/rules/recognition.md` 禁的
        # 是后者。这里判的是"是不是测试替身"，不是探测 Paddle 能力再回退到别的
        # 真实引擎（报价识别现在唯一的真实引擎就是 Paddle，没有第二条生产路可回退）。
        # 不加这个分支：全仓库依赖 `MockProvider.vl_extract_csv` 产出报价数据的
        # 集成测试（document_ingestion/compare_integration/copy_dedup/
        # dry_run_confirm 等，35 个用例）会在 P4 切换 Paddle 后全部失败——它们
        # 测的是入库门/对齐/矩阵这些下游逻辑，不是识别引擎本身，让它们继续吃
        # canned CSV 天经地义，不该因为报价识别换了引擎就要求它们改去伪造
        # Paddle 的 JSON 结构。
        if isinstance(self.provider, MockProvider):
            from apps.api.intelligence.vl_quote import PROMPT_QUOTE_CSV, build_draft
            _notify(progress_cb, "识别报价清单", 55)
            csv_text = self.provider.vl_extract_csv(
                [b"mock"] * max(1, page_count), PROMPT_QUOTE_CSV)
            draft = build_draft(
                csv_text, file_path=file_path, page_count=page_count,
                processed_pages=list(range(1, page_count + 1)), parser_mode="mock")
            return self._draft_to_quote_response(draft, context or {}, t_start)

        # 非 MockProvider（真实生产路）：走 Paddle，理由同上（评审时误留了一份
        # 重复的说明段落在这，已删——上面那段注释就是这里的完整依据，不重复贴）。
        from apps.api.intelligence.paddle_doc_meta import get_text_client_call
        from apps.api.intelligence.paddle_vl import recognize_quote_paddle
        from apps.api.intelligence.providers import paddle_ocr

        _notify(progress_cb, "渲染PDF", 20)
        draft = recognize_quote_paddle(
            file_path,
            submit_and_parse=paddle_ocr.submit_and_parse,
            page_count=page_count,
            text_call=get_text_client_call(),
            progress_cb=progress_cb,
        )
        return self._draft_to_quote_response(draft, context or {}, t_start)

    def _draft_to_quote_response(
        self,
        draft: Any,
        context: dict,
        t_start: float,
    ) -> ExtractionResponse:
        """Convert ExtractionDraft → ExtractionResponse (quote side)."""
        import time as _time
        # Convert DraftRow → postprocess-compatible item dicts
        items = []
        for row in draft.rows:
            if row.row_type != DETAIL_ROW_TYPE:
                continue
            f = row.fields
            items.append({
                "material": f.get("name") or "",
                "spec": f.get("spec") or "",
                "brand": f.get("brand") or "",
                "unit": f.get("unit") or "",
                "qty": f.get("qty"),
                # 全部原始价格字段（含税/不含税/通用），口径桥接与入库均需要，不得丢失
                "unit_price": f.get("unit_price"),
                "unit_price_incl_tax": f.get("unit_price_incl_tax"),
                "unit_price_excl_tax": f.get("unit_price_excl_tax"),
                "total_price": f.get("total_price"),
                "total_price_incl_tax": f.get("total_price_incl_tax"),
                "total_price_excl_tax": f.get("total_price_excl_tax"),
                "tax_rate": f.get("tax_rate"),
                "tax_amount": f.get("tax_amount"),
                "material_type": f.get("material_type") or "",
                "remark": f.get("remark") or "",
                "canonical": f.get("canonical") or {},
                "normalized_material": f.get("normalized_material") or "",
                "ocr_correction_reason": f.get("ocr_correction_reason") or "",
                "source_ref": row.source_ref.to_dict(),
                # 算术校验审计：原值 qty 不改，suggested_qty 仅参考；validation_flags 完整传递
                "validation_flags": list(row.validation_flags or []),
                "raw_qty": f.get("qty"),
                "suggested_qty": f.get("arith_suggested_qty"),
                # 行位证据：顺序直连按位置对齐锚点要用，定向重读要用它回到具体页。
                # 缺了它，`_doc_order` 的三态逻辑拿不到输入，只能退回载入顺序。
                "document_row_index": f.get("document_row_index"),
                "page_row_index": f.get("page_row_index"),
                "source_page": row.source_ref.page or None,
                "copy_no": f.get("copy_no") or "",
                # 「原文明确不报价」——合法事实，与"读不到"分开，入库门据此不阻断
                "not_quoted": bool(f.get("not_quoted")),
            })
        data_in = {
            "supplier_name": (draft.meta or {}).get("supplier_name") or "",
            "items": items,
        }
        processed = self._postprocess_quote(data_in, context)
        # 声明总价核对门的输入（quote_confirmation_service._build_checksum 读
        # job.result["_doc_meta"]，由 document_ingestion.py:236 从这里搬运）。
        # 此前 VL 路径从不产出这个键——门本身逻辑是对的，只是从未接到过输入，
        # 生产上对任何 PDF 报价都判 unknown、不阻断（docs/design/21 §2.2/§2.3）。
        quote_meta = (draft.meta or {}).get("quote_meta")
        # 投标文件声明式要求（design/26 P4 补：跟招标文件同一个模式，见
        # `paddle_doc_meta.DEFAULT_QUOTE_REQUIREMENTS`）——可选字段，不填不影响
        # 既有消费方，跟上面 `doc_meta` 同一个"有才加键"的约定。
        quote_requirements = (draft.meta or {}).get("quote_requirements")
        resp = ExtractionResponse(
            data=processed,
            metadata={
                "doc_type": "quote",
                "quality_status": draft.quality.status,
                "quality_blocking_reasons": draft.quality.blocking_reasons,
                "page_count": draft.page_count,
                "target_pages": draft.target_pages,
                # 行数守恒台账（doc/19 §L3）：丢行必须随结果一起暴露，
                # 否则调用方拿到的 200 无法区分"这份文档只有这些行"和"我们丢了行"。
                "row_ledger": draft.ledger.to_dict() if draft.ledger else None,
                **({"doc_meta": quote_meta} if quote_meta else {}),
                **({"requirements": quote_requirements} if quote_requirements else {}),
            },
            tokens_used=0,
            duration_ms=int((_time.time() - t_start) * 1000),
        )
        _ledger = draft.ledger
        log.info(
            "draft_to_quote_response: quality=%s items=%d supplier=%r ledger=%s",
            draft.quality.status,
            len(processed.get("items") or []),
            processed.get("supplier_name"),
            (f"{_ledger.recognized_rows}/{_ledger.expected_rows} rows, "
             f"{len(_ledger.empty_pages)} empty pages") if _ledger else "n/a",
        )
        return resp

    # ─── post-processing ──────────────────────────────────────────────────
    @staticmethod
    def _postprocess_tender(data: dict) -> dict:
        items = data.get("items") or []
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = (it.get("name") or "").strip()
            if not name:
                continue
            category = (it.get("category") or "").strip()
            if not category:
                category = _infer_category(name)
            ext = it.get("extended_attrs")
            if not isinstance(ext, dict):
                ext = {}
            ext = {k: v for k, v in ext.items() if v is not None and v != ""}
            cleaned.append({
                "name": name,
                "category": category,
                "spec": (it.get("spec") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "quantity": _coerce_num(it.get("quantity")),
                "remark": (it.get("remark") or "").strip(),
                "extended_attrs": ext,
            })
        return {
            "project_name": (data.get("project_name") or "").strip(),
            "project_code": (data.get("project_code") or "").strip(),
            "tender_date": (data.get("tender_date") or "").strip(),
            "deadline": (data.get("deadline") or "").strip(),
            "items": cleaned,
        }

    @staticmethod
    def _postprocess_quote(data: dict, ctx: dict[str, Any]) -> dict:
        items = data.get("items") or []
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            material = (it.get("material") or "").strip()
            if not material:
                continue
            price = _coerce_num(it.get("unit_price"))
            qty = _coerce_num(it.get("qty"))
            total = _coerce_num(it.get("total_price"))
            # 「原文明确不报价」的标记必须在这里就固化下来。_coerce_num 把「/」「无」
            # 「N/A」和空白一律变成 None，**两种语义就此不可分辨**——下游只能把合法的
            # 不报价行当成缺陷，422 逼用户编一个金额。故在还看得到原始文本的这一层
            # 判定一次，用布尔标记随行带走。
            from apps.api.services.ingestion.draft_integrity import (
                AMOUNT_NOT_QUOTED, classify_amount_cell,
            )
            not_quoted = any(
                classify_amount_cell(it.get(k)) == AMOUNT_NOT_QUOTED
                for k in ("total_price", "total_price_incl_tax", "total_price_excl_tax")
            )
            # 不在识别阶段派生合价。上游一旦把 qty×price 填进 total_price，
            # 下游 confirm 就无法区分"原文读到的"和"系统算的"，会误标成 ocr。
            # 只给候选值供前端提示，权威字段保持 None（doc/19 §L2）。
            derived_candidate = None
            if total is None and price is not None and qty is not None:
                derived_candidate = round(price * qty, 4)
            spec_str = (it.get("spec") or "").strip()
            material_type = (it.get("material_type") or "").strip()
            # Layer 1: OCR correction fields from LLM (raw text stays in material)
            norm_material = (it.get("normalized_material") or "").strip()
            ocr_reason = (it.get("ocr_correction_reason") or "").strip()

            # Canonical: merge code extraction with LLM result (LLM overrides).
            # normalized_material takes priority over raw material for code extraction
            # so 形近字 errors don't corrupt canonical.valve_type and kill matching.
            canonical: dict = build_canonical(
                material, spec_str, material_type=material_type,
                llm_canonical=it.get("canonical"),
                normalized_material=norm_material,
            )

            # ── 价格口径桥接（§4/§9）：判定 price_basis + effective 价格，原值不改 ──
            unit_incl = _coerce_num(it.get("unit_price_incl_tax"))
            unit_excl = _coerce_num(it.get("unit_price_excl_tax"))
            total_incl = _coerce_num(it.get("total_price_incl_tax"))
            total_excl = _coerce_num(it.get("total_price_excl_tax"))
            basis_info = derive_price_basis({
                "qty": qty,
                "unit_price": price,
                "unit_price_incl_tax": unit_incl,
                "unit_price_excl_tax": unit_excl,
                "total_price": total,
                "total_price_incl_tax": total_incl,
                "total_price_excl_tax": total_excl,
                "tax_rate": _coerce_num(it.get("tax_rate")),
                "tax_amount": _coerce_num(it.get("tax_amount")),
                "derived_total_candidate": derived_candidate,
            })

            cleaned.append({
                "material": material,
                "spec": spec_str,
                "brand": (it.get("brand") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "qty": qty,
                "unit_price": price,
                "unit_price_incl_tax": unit_incl,
                "unit_price_excl_tax": unit_excl,
                "total_price": total,
                "total_price_incl_tax": total_incl,
                "total_price_excl_tax": total_excl,
                "tax_rate": _coerce_num(it.get("tax_rate")),
                "tax_amount": _coerce_num(it.get("tax_amount")),
                "derived_total_candidate": derived_candidate,
                # 原文明确不报价（「/」「无」「N/A」…）。与"读不到"分开，见上方注释。
                "not_quoted": not_quoted,
                # 比价口径桥接结果
                "price_basis": basis_info["price_basis"],
                "effective_unit_price": basis_info["effective_unit_price"],
                "effective_total_price": basis_info["effective_total_price"],
                "effective_unit_recovered": basis_info.get("effective_unit_recovered", False),
                "material_type": material_type,
                "remark": (it.get("remark") or "").strip(),
                "canonical": canonical,
                "validation_warning": "",
                "validation_flags": list(it.get("validation_flags") or []),
                "raw_qty": _coerce_num(it.get("raw_qty")) if it.get("raw_qty") is not None else qty,
                "suggested_qty": _coerce_num(it.get("suggested_qty")),
                "normalized_material": norm_material,
                "ocr_correction_reason": ocr_reason,
                "source_ref": it.get("source_ref"),  # {page, table, row} from TableGrid
                # 行位证据。**这里是白名单重建，不加就会丢** —— 丢了之后顺序直连
                # 只能退回载入顺序，定向重读也回不到具体页。
                "source_page": it.get("source_page"),
                "page_row_index": it.get("page_row_index"),
                "copy_no": it.get("copy_no") or "",
            })
        apply_arithmetic_validation(cleaned)
        # 全局文档行序（1..N，按 page→table→row 的抽取顺序）。顺序直连对齐用它做行身份，
        # 不依赖会跨页重置的 source_ref.row 或数据库自增 id。
        for _i, _it in enumerate(cleaned, 1):
            _it["document_row_index"] = _i
        return {
            "supplier_name": (data.get("supplier_name") or "").strip(),
            "quote_date": (data.get("quote_date") or "").strip(),
            "items": cleaned,
            "context": ctx,
        }


# ─── helpers ──────────────────────────────────────────────────────────────
def _coerce_num(v: Any) -> float | None:
    # 评审 N7：曾是独立实现，现委托给 core.utils.parse_num 的 lenient 模式
    # （行为不变——剥离一切非数字字符后再解析，从自由文本里挖数字）。
    return parse_num(v, lenient=True)


def _infer_category(name: str) -> str:
    """Heuristic: scan material name for a known category keyword."""
    for cat in KNOWN_CATEGORIES:
        if cat in name:
            return cat
    return ""


def _notify(
    progress_cb: ProgressCallback | None, stage: str, pct: int, *,
    stage_current: int | None = None, stage_total: int | None = None,
) -> None:
    if progress_cb:
        progress_cb(stage, max(0, min(100, pct)),
                    stage_current=stage_current, stage_total=stage_total)


def _tender_page_count(file_path: str) -> int:
    from apps.api.intelligence.document_loader import DocumentLoader
    return DocumentLoader.get_page_count(file_path)
