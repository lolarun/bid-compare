"""QuoteConfirmationService — batch quote confirmation authority.

Extracted from routes/quotes.py (batch_confirm) so the core write path
(ExtractionJob → BidSubmission + BidQuoteLine) lives in a testable service,
not inline in the route handler.

The route delegates here and is responsible only for HTTP mapping.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from apps.api.core.config import PROFESSION_MAP
from apps.api.core.domain_config import (
    CHECKSUM_BLOCK_DELTA_RATIO,
)
from apps.api.core.errors import NotFoundError, ReviewRequiredError, ValidationError
from apps.api.intelligence.price_basis import derive_price_basis
from apps.api.models import (
    BidQuoteLine,
    BidSubmission,
    BrandTier,
    ExtractionJob,
    Material,
    Project,
    Supplier,
)
from apps.api.services.audit import (
    EVENT_BQL_CONFIRM,
    normalize_row_type,
    write_domain_event,
)
from apps.api.services.ingestion.draft_integrity import (
    AMOUNT_NOT_QUOTED,
    ARITHMETIC_FLAG,
    BLOCKED,
    COLUMN_SHIFT_FLAG,
    DUPLICATE_FLAG,
    REVIEW,
    TRUNCATION_FLAG,
    check_arithmetic,
    classify_amount_cell,
    corroborate_truncation,
    detect_truncated_numbers,
    find_duplicate_rows,
)
from apps.api.services.ingestion.list_rows import classify_quote_row
from apps.api.services.ingestion.standardize import standardize_name
from apps.api.services.submission import dry_run_cache

log = logging.getLogger(__name__)

# Grand-total/subtotal name patterns — keep in sync with table_parser / routes/quotes.py.
_GRAND_TOTAL_NAME_RE = re.compile(
    r"价税合计|总计|合计金额|投标总价|^合计$|含税总计|含税合计|详见投标清单"
)


def _num_or_none(v: Any) -> float | None:
    """转成数字，转不了就 None。

    **不能抛异常**：原文写「/」表示不报价是合法的，抛出去会被行级 try 吞掉、
    整行以"处理失败"被跳过——用户看到的是"这行没识别出来"，而不是"这行没报价"。
    判断到底是"不报价"还是"读不到"交给 `classify_amount_cell`。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _declared_total(job) -> float | None:
    """文件封面声明总价（`_doc_meta.bid_total`），没有就是 None。

    被 `_build_checksum`（明细合价之和 vs 声明总价）和 `_dedupe_copies`
    （多副本时选哪一份）共用同一个取值口径——两处都是"拿文档自己的事实做
    判据"，抽出来避免两份实现悄悄漂移。
    """
    doc_meta = (job.result or {}).get("_doc_meta") or {}
    declared = doc_meta.get("bid_total")
    try:
        return float(declared) if declared is not None else None
    except (TypeError, ValueError):
        return None


def _dedupe_copies(items: list[dict], declared_total: float | None) -> tuple[list[dict], dict | None]:
    """design/24 B0：VL-direct 提示词第 3 条要求"同一份清单在文件里重复出现
    （正本与副本、汇总与明细）时照实全部输出，不合并不丢弃"，每行标 copy_no
    属于第几份——**识别层这样做是对的**，是下游从没处理过 copy_no（`grep
    services/ copy_no` 零消费方）。结果是一份清单的两份合法副本被结构完整性门
    /checksum 误判成"重复行占比 50%"甚至 BLOCKED——浦东 272=136×2 正是这个
    缺陷的实例，不是识别 bug（design/24 §4 B0 已核实并记录取证过程）。

    在任何门禁/入库判断之前，按 copy_no 分组选定一份：
    - 声明总价已知：选合价之和与声明总价最接近的一组（文档自己的事实，不是猜——
      与 `_build_checksum` 同一口径，选出来的那组自然会通过后续 checksum 门）。
    - 声明总价未知：退回行数最多的一组（更完整，比"总是选第一份"更安全——
      模型偶尔会把某一份副本识别得残缺）。

    未选中的副本**不从 items 里删除源数据**——本函数只返回"这次要入库的那份"，
    其余副本仍完整保留在 job.result 里（调用方从不改写 job.result），只是本次
    确认不写入 BidQuoteLine。只有 0/1 个 copy_no 分组时不触发（report=None），
    行为与本轮改动前完全一致。
    """
    groups: dict[str, list[dict]] = {}
    for it in items:
        cn = str(it.get("copy_no") or "").strip()
        groups.setdefault(cn, []).append(it)
    if len(groups) <= 1:
        return items, None

    def _group_sum(group: list[dict]) -> float:
        total = 0.0
        for it in group:
            v = it.get("total_price_incl_tax")
            if v in (None, ""):
                v = it.get("total_price")
            try:
                total += float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                pass
        return total

    if declared_total:
        selected_cn = min(groups, key=lambda cn: abs(_group_sum(groups[cn]) - declared_total))
        basis = "closest_to_declared_total"
    else:
        selected_cn = max(groups, key=lambda cn: len(groups[cn]))
        basis = "largest_row_count"

    selected = groups[selected_cn]
    dropped_by_copy = {cn: len(g) for cn, g in groups.items() if cn != selected_cn}
    report = {
        "total_copies": len(groups),
        "copy_nos": sorted(groups.keys()),
        "selected_copy_no": selected_cn,
        "selected_rows": len(selected),
        "dropped_rows": sum(dropped_by_copy.values()),
        "dropped_by_copy": dropped_by_copy,
        "selection_basis": basis,
    }
    log.info("batch_confirm: copy_dedup %s", report)
    return selected, report


def _build_checksum(job, line_total_sum: float, line_count: int) -> dict:
    """明细合价之和 vs 文件声明总价。

    三态，**`unknown` 不等于通过**：文件没给声明总价时我们就是没有这个证据，
    调用方不得把它当成"校验过了"。
    """
    declared = _declared_total(job)
    if not declared or declared <= 0 or line_count <= 0:
        log.info("checksum_sample status=unknown line_count=%s line_sum=%.2f declared=%s",
                 line_count, line_total_sum, declared)
        return {"declared": declared, "line_sum": round(line_total_sum, 2),
                "delta_pct": None, "status": "unknown", "line_count": line_count,
                "reason": "文件未给出声明总价，无法闭环校验"}
    abs_delta = line_total_sum - declared
    delta = abs(abs_delta) / declared
    status = "pass" if delta <= CHECKSUM_BLOCK_DELTA_RATIO else "fail"

    # 埋点（docs/design/20 §5）。现行阈值是**比例**，而合法残差来自逐行两位小数的
    # 舍入——那随行数增长、不随金额增长。要换成按行数给容差，先得知道真实分布，
    # 而这个分布现在拿不到：七份离线样本既不含清单外项目（税费/优惠/暂列金）的
    # 情形，也不代表生产文档的构成。
    #
    # 故先只记录、不改判定：绝对差、行数、每行摊到多少，三样都要有。
    # 只记 delta_pct 是不够的——它正是被质疑的那个口径。
    log.info(
        "checksum_sample status=%s line_count=%d declared=%.2f line_sum=%.2f "
        "abs_delta=%.2f delta_pct=%.4f per_row_delta=%.4f",
        status, line_count, declared, line_total_sum, abs_delta,
        delta * 100, abs_delta / line_count,
    )
    return {"declared": declared, "line_sum": round(line_total_sum, 2),
            "delta_pct": round(delta * 100, 3),
            "threshold_pct": round(CHECKSUM_BLOCK_DELTA_RATIO * 100, 3),
            # 下面两个字段供分布分析用，不参与判定（判定仍只看 delta_pct）
            "line_count": line_count,
            "abs_delta": round(abs_delta, 2),
            "per_row_delta": round(abs_delta / line_count, 4),
            "status": status}


def _integrity_row(items: list[dict], i: int, flags: list[str], column: str | None = None) -> dict:
    it = items[i]
    return {
        "index": i,
        "material": str(it.get("material") or ""),
        "spec": str(it.get("spec") or ""),
        "qty": it.get("qty"),
        "unit_price": it.get("unit_price"),
        "total_price": it.get("total_price"),
        "flags": flags,
        "reason": _INTEGRITY_REASONS.get(flags[0], "结构完整性存疑") if flags else "",
        # design/27 §4/§10 步骤4：前端逐格标色需要知道"哪一列"，不只是"哪一行"。
        # 缺省落 material——没有更具体列信息时，用身份列做锚点总比不给列强
        # （column_shift 这类整行性问题就是这种情况：错位牵连所有列，没有
        # "唯一正确列"这个概念，选身份列纯粹是给前端一个可点的锚点）。
        "column": column or "material",
    }


_INTEGRITY_REASONS = {
    COLUMN_SHIFT_FLAG: "数据列数与表头不一致，按列名取到的值整体错位",
    DUPLICATE_FLAG: "与前面某行的名称/规格/数量/单价完全相同，疑似重复抽取",
    ARITHMETIC_FLAG: "数量×单价与合价对不上，三者中至少一个读错",
    TRUNCATION_FLAG: "该数值卡在本列的宽度上限且小数位偏少，疑似被截断",
}


def _truncation_from_items(items: list[dict]):
    """在 items 这一层做截断检测。

    截断只能从**原始文本**看出来——值一旦被 float() 解析过，`1956390.` 与
    `1956390.45` 就再也分不开了。所以这里只检查仍是字符串的原值；上游若已把价格
    转成数字，就必须在还是表格的时候调 detect_truncated_numbers（见 draft_integrity）。
    """
    keys = ("total_price", "total_price_incl_tax", "total_price_excl_tax",
            "unit_price", "unit_price_incl_tax", "unit_price_excl_tax", "qty")
    raw_keys = [k for k in keys
                if any(isinstance(it.get(k), str) and it.get(k) for it in items)]
    if not raw_keys:
        return None
    rows = [[str(it.get(k) if isinstance(it.get(k), str) else "") for k in raw_keys]
            for it in items]
    rep = detect_truncated_numbers(raw_keys, rows)
    return corroborate_truncation(rep, items) if rep.suspects else rep


def _gate_integrity(db: Session, items: list[dict], dry_run: bool = False,
                   gates_advisory: bool = False) -> dict:
    """列错位 / 重复行门。两者的处置**不同**，因为它们的合法性不同：

    - **列错位**：按列名取到的值可能是别的列的值，一行也不放行——**但这只约束正式
      入库**。预览走 `gates_advisory=True`，本门与其它三道门一样只收集不阻断
      （2026-08-22 修：本门当初漏接了这个开关，见 `confirm_batch` 的 design/32 §8
      说明；后果是识别侧补上位移检测后，一份 89 行的报价只因 1 行错位就整份进不了
      预览，三家里只剩一家能比价——而预览是沙箱自动跑的，没有人能去逐行 ack）。
    - **重复行**：**合法的重复真实存在**——同一型号阀门同量同价出现在给水和排水两
      个系统里，是正常清单。实测三份真实阀门文档各有 3~6 组这样的行，且逐行核对
      与 golden 完全一致。故 REVIEW 级重复只标注、不阻断；只有当重复金额占比越过
      阈值（domain_config）才升级为 BLOCKED——那种规模不可能是真实重复。

    放行 BLOCKED 行的唯一方式是用户在预览里标 `integrity_ack=true`，与派生金额门一致。
    返回给响应体的告警摘要；不改写任何原值。

    design/24 B3：`dry_run=True` 时，命中阻断也不 raise/rollback——把原本要 raise
    的 payload 塞进返回值的 "blocking_issue" 键，让调用方（confirm_batch）继续往
    下跑完整条链路，一次收集所有门的问题，而不是在这里就打断。`dry_run=False`
    时的行为（包括 rollback 时机）与改动前逐字节一致——真实路径不受这轮改动影响。
    """
    dup = find_duplicate_rows(items)
    arith = check_arithmetic(items)
    trunc = _truncation_from_items(items)
    shifted = {i for i, it in enumerate(items)
               if COLUMN_SHIFT_FLAG in (it.get("validation_flags") or [])}
    dup_rows = dup.duplicate_row_indices
    arith_rows = set(arith.mismatch_indices)
    trunc_rows = trunc.suspect_row_indices if trunc else set()

    # 逐行"该标哪一列"（design/27 §4/§10 步骤4）：三个判据各自天然带着列信息，
    # 这里只是把已经算出来的东西收拢成 row_index -> column 查表，不新增判据。
    # 一行可能命中多种判据，取值时按 flags 优先级（跟 _INTEGRITY_REASONS 的
    # flags[0] 取法一致）由调用方决定用哪个，这里只负责把每种判据自己的列
    # 算对。
    arith_column: dict[int, str] = {}
    for i in arith_rows:
        basis = arith.results[i].basis  # "unit_price_excl_tax|total_price_excl_tax"
        if basis and "|" in basis:
            arith_column[i] = basis.split("|", 1)[1]   # 合价那一侧，不是单价——
            # 算术门比的是"合价对不对"，出问题时该看的是合价这一列。
    trunc_column: dict[int, str] = {}
    if trunc:
        for s in trunc.suspects:
            trunc_column.setdefault(s.row_index, s.column)  # 一行多个疑似列时取第一个

    def _column_for(i: int, flags: list[str]) -> str | None:
        for f in flags:
            if f == ARITHMETIC_FLAG and i in arith_column:
                return arith_column[i]
            if f == TRUNCATION_FLAG and i in trunc_column:
                return trunc_column[i]
            if f == DUPLICATE_FLAG:
                return "material"
        return None

    # 只标注、放行的那部分：写进 validation_flags，下游据此知道这行被怀疑过
    warn: dict[int, list[str]] = {}
    if dup.verdict == REVIEW:
        for i in sorted(dup_rows):
            warn.setdefault(i, []).append(DUPLICATE_FLAG)
    if arith.verdict == REVIEW:
        for i in sorted(arith_rows):
            warn.setdefault(i, []).append(ARITHMETIC_FLAG)
    # 截断永远只标注不阻断：值仍然近似正确（丢的是小数位），行本身可以入库，
    # 但逐行金额不能当精确值用，必须让人看见。
    for i in sorted(trunc_rows):
        warn.setdefault(i, []).append(TRUNCATION_FLAG)

    warn_rows: list[dict] = []
    for i, flags in sorted(warn.items()):
        existing = list(items[i].get("validation_flags") or [])
        items[i]["validation_flags"] = existing + [f for f in flags if f not in existing]
        warn_rows.append(_integrity_row(items, i, flags, _column_for(i, flags)))

    blocking_dup = dup_rows if dup.verdict == BLOCKED else set()
    blocking_arith = arith_rows if arith.verdict == BLOCKED else set()
    blocking_shift = shifted
    block_rows = [
        _integrity_row(items, i,
                       ([COLUMN_SHIFT_FLAG] if i in blocking_shift else [])
                       + ([DUPLICATE_FLAG] if i in blocking_dup else [])
                       + ([ARITHMETIC_FLAG] if i in blocking_arith else []),
                       _column_for(i, ([COLUMN_SHIFT_FLAG] if i in blocking_shift else [])
                                  + ([DUPLICATE_FLAG] if i in blocking_dup else [])
                                  + ([ARITHMETIC_FLAG] if i in blocking_arith else [])))
        for i in sorted(blocking_shift | blocking_dup | blocking_arith)
        if not items[i].get("integrity_ack")
    ]

    if block_rows:
        payload = {
            "error": "structural_integrity_requires_review",
            "message": (
                f"{len(block_rows)} 行未通过结构完整性检查"
                f"（列错位 {len(blocking_shift)} 行 / 重复 {len(blocking_dup)} 行 / "
                f"算术不闭合 {len(blocking_arith)} 行，"
                f"重复金额占比 {dup.amount_ratio:.1%}，"
                f"算术错误率 {arith.error_rate:.1%}）。"
                f"系统不会代为删除或重排，请核对原文后逐行确认。"
            ),
            "review_rows": block_rows[:50],
            "review_row_count": len(block_rows),
            "duplicates": dup.to_dict(),
            "arithmetic": arith.to_dict(),
        }
        # `gates_advisory`：门的结论照算、照带回（payload 进 `blocking_issue`），
        # 只是不中止流程——跟 dry_run 同一个语义，本门此前漏接了这个参数。
        if not (dry_run or gates_advisory):
            db.rollback()
            raise ReviewRequiredError(payload)
    else:
        payload = None

    return {"duplicate_verdict": dup.verdict,
            "duplicate_rows": len(dup_rows),
            "duplicate_amount_ratio": round(dup.amount_ratio, 4),
            "column_shift_rows": len(shifted),
            "arithmetic": arith.to_dict(),
            "truncation": trunc.to_dict() if trunc else None,
            "warnings": warn_rows[:50],
            "blocking_issue": payload}


def confirm_batch(db: Session, body, dry_run: bool = False,
                  gates_advisory: bool = False) -> dict:
    """将 OCR 提取结果暂存为 BidSubmission + BidQuoteLine（P0 新版）。

    `body` must have the same fields as BatchConfirmRequest in routes/quotes.py.

    Returns the response dict the route should return directly.

    design/24 B3（dry_run）：请求形状校验（job/supplier/project/category/items
    形状——第一段，到 items 列表建好为止）**不受 dry_run 影响，始终立即 raise**：
    这些是"请求本身不成立"，不是"文档内容有疑点"，dry-run 预览一个不存在的
    job 没有意义。从结构完整性门开始，四道数据质量门（结构完整性/原文无合价/
    全部跳过/声明总价核对）在 dry_run=True 时改为**收集而非阻断**——让入库
    循环整条链路都跑完，一次性报出所有疑点，而不是像真实路径那样命中第一道
    就停（真实路径的行为逐字节不变，见各处 `if not dry_run` 分支）。dry_run
    永远不 commit，函数末尾统一 rollback，包括 project 自动建档这类中途 flush
    的副作用。

    design/32 §8（gates_advisory）：把"四道门收集不阻断"从 dry_run 里**拆出来**
    成独立开关。这两件事原本焊在一起，但它们回答的是不同问题：
      - `dry_run`        —— 要不要落库；
      - `gates_advisory` —— 门失败了要不要阻断。
    预览比价需要的组合是"照常写（写在沙箱里，外层统一回滚）+ 门只警告"：
    它必须真的写进去，后面的对齐才读得到；但一家供应商没过质量门不该让**整个
    预览**无法进行——用户原话：「能不能比价是一个等级，有几个能比价是另外一个
    等级」。`dry_run=True` 隐含 `gates_advisory=True`，既有行为逐字节不变。

    **这不是把门放松了。** 门的存在是为了拦住脏数据进入官方结果；预览沙箱
    从不落库、结果强制 `basis="preview"`，官方侧一点没动。门的结论照样算、
    照样带回给调用方（`issues`），只是不再中止流程。
    """
    job = db.get(ExtractionJob, body.job_id)
    if not job:
        raise NotFoundError(f"Job {body.job_id} not found")
    if job.type != "quote":
        raise ValidationError(f"Job type is {job.type}; must be 'quote'")
    if job.status != "done":
        raise ValidationError(f"Job status is {job.status}; must be 'done'")

    # ── Supplier 验证（弱关联：supplier_id 可选，有则校验状态）──────────────────
    supplier: Supplier | None = None
    if body.supplier_id is not None:
        supplier = db.get(Supplier, body.supplier_id)
        if not supplier:
            raise NotFoundError(f"Supplier {body.supplier_id} not found")
        if supplier.merge_status != "active":
            raise ValidationError(
                f"Supplier {supplier.name!r} merge_status={supplier.merge_status}，"
                "只允许选择 active 供应商",
            )
    else:
        if not body.supplier_name.strip():
            raise ReviewRequiredError("陌生供应商必须提供 supplier_name")

    # ── Project（允许按名查找或创建，project 不是污染来源）─────────────────────
    project: Project | None = None
    if body.project_id:
        project = db.get(Project, body.project_id)
        if not project:
            raise NotFoundError(f"Project {body.project_id} not found")
    elif body.project_name.strip():
        pname = body.project_name.strip()
        project = db.scalar(select(Project).where(Project.name == pname))
        if not project:
            project = Project(name=pname)
            db.add(project)
            db.flush()
    elif (job.context or {}).get("project_id"):
        ctx_pid = job.context["project_id"]
        project = db.get(Project, ctx_pid)
        if not project:
            raise ValidationError(
                f"Project {ctx_pid} from job context no longer exists; "
                "specify project_name or project_id to proceed.",
            )

    # ── 默认 category ──────────────────────────────────────────────────────────
    default_category = (
        body.category.strip()
        or (job.context or {}).get("category", "")
        or ""
    )
    if default_category and default_category not in PROFESSION_MAP:
        raise ValidationError(f"Unknown category: {default_category}")

    # ── Item list ──────────────────────────────────────────────────────────────
    raw_items: Any = (
        body.overrides
        if body.overrides is not None
        else (job.result or {}).get("items")
    )
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ReviewRequiredError(f"Expected items list, got {type(raw_items).__name__}")

    # 早期校验：有 items 但 category 为空 → 立即拒绝，不创建空壳 submission
    _has_real_items = any(
        str(r.get("material") or "").strip() for r in raw_items if isinstance(r, dict)
    )
    if _has_real_items and not default_category:
        raise ReviewRequiredError(
            "category 不能为空：无法确定报价品类，入库中止。"
            "请在前端选择品类（如「阀门」）后重新点击「校对入库」。",
        )

    items: list[dict[str, Any]] = []
    shape_errors: list[dict] = []
    for idx, item in enumerate(raw_items):
        if isinstance(item, dict):
            items.append(item)
        else:
            shape_errors.append({"row": idx + 1, "reason": f"not an object: {type(item).__name__}"})

    # design/24 B0：多份合法副本（copy_no）先选定一份，不让重复副本冒充"重复行"
    # 去踩结构完整性/checksum 门。必须在任何门禁之前做——晚了就是在给假疑点找
    # 借口，不是在防真疑点。
    items, copy_dedup = _dedupe_copies(items, _declared_total(job))

    # design/24 B4：dry_run 命中缓存直接返回，不碰 DB。真实写入路径不查缓存——
    # 缓存只为"反复预览、不打算真写"这个场景服务，写入必须永远走一遍真实判据。
    cache_hit_key: str | None = None
    if dry_run:
        cache_hit_key = dry_run_cache.cache_key(
            body.job_id, items, category=default_category,
            supplier_id=body.supplier_id, checksum_ack=getattr(body, "checksum_ack", False),
        )
        cached = dry_run_cache.get(cache_hit_key)
        if cached is not None:
            return cached

    # ── QuoteRound（docs/design/42 P0）──────────────────────────────────────────
    # 省略 round_id 时落到 (project, category) 当前打开的轮次，没有就自动开
    # 第一轮——单轮项目因此不用改调用方就落到旧行为。project 为空时（陌生上下文，
    # 连项目都没定）没有 (project, category) 可挂，round_id 留空，不强求。
    round_id: int | None = getattr(body, "round_id", None)
    if round_id is None and project is not None:
        from apps.api.services.tender.quote_round_service import get_or_open_round
        round_id = get_or_open_round(db, project.id, default_category).id

    # ── 幂等：BidSubmission.batch_id 检查（一个 job 最多一条 BidSubmission）────────
    batch_id = f"BID-{job.id}"
    prior_submission = db.scalar(select(BidSubmission).where(BidSubmission.batch_id == batch_id))
    display_name = (
        body.supplier_name.strip()
        or (supplier.name if supplier else "")
        or (job.result or {}).get("supplier_name", "")
    )
    if prior_submission:
        # 同一文件→同一 job→同一 batch_id。废弃状态不能作为幂等命中。
        _stale = prior_submission.status in ("superseded", "rejected")
        prior_line_count = db.scalar(
            select(func.count(BidQuoteLine.id)).where(
                BidQuoteLine.submission_id == prior_submission.id
            )
        ) or 0
        if prior_line_count > 0 and not _stale:
            log.info(
                "batch_confirm: idempotent hit, submission_id=%d batch=%s lines=%d",
                prior_submission.id, batch_id, prior_line_count,
            )
            # 幂等命中**不得成为门的旁路**。已入库的数据不能回滚删除（那会毁掉合法数据），
            # 但必须按当前判据重新评估、并把结论如实带回响应——否则一份在门存在之前
            # 写进去的、或经由其他路径写进去的数据，会永远以裸 "ok" 的形式被下游当成
            # 已校验通过。
            prior_sum = db.scalar(
                select(func.coalesce(func.sum(BidQuoteLine.total_price), 0.0)).where(
                    BidQuoteLine.submission_id == prior_submission.id
                )
            ) or 0.0
            checksum = _build_checksum(job, float(prior_sum), prior_line_count)
            idempotent_issue = None
            if checksum["status"] == "fail" and not getattr(body, "checksum_ack", False):
                payload = {
                    "error": "declared_total_mismatch",
                    "message": (
                        f"该 job 已入库 {prior_line_count} 行，但明细合价之和 "
                        f"{checksum['line_sum']:,.2f} 与声明总价 "
                        f"{checksum['declared']:,.2f} 相差 {checksum['delta_pct']}%，"
                        f"超过 {CHECKSUM_BLOCK_DELTA_RATIO:.1%}。"
                        f"**数据已在库中**，需人工核对后确认或重新识别。"
                    ),
                    "checksum": checksum,
                    "already_stored": True,
                    "submission_id": prior_submission.id,
                }
                # dry_run：已入库这件事本身不该被 dry-run "预览"到——库里已经有
                # 数据了，不是"将要写什么"。仍按 checksum 现状报一条 issue，
                # 但不 raise（没有事务要保护，这条分支从不写任何东西）。
                if not (dry_run or gates_advisory):
                    raise ReviewRequiredError(payload)
                idempotent_issue = payload
            if not dry_run:
                if job.lifecycle != "confirmed":
                    job.lifecycle = "confirmed"
                    db.commit()
            # 注意：这里不带 copy_dedup——它是对本次 items 重算的结果，跟
            # prior_line_count（数据库里实际存的、可能是本轮 B0 修复前写入的）
            # 不是同一件事，混进响应会误导前端以为库里的数据也经过了去重。
            result = {
                "status": "ok",
                "submission_id": prior_submission.id,
                "line_count": prior_line_count,
                "skipped_count": 0,
                "errors": [],
                "unknown_brands": [],
                "supplier_id": prior_submission.supplier_id,
                "project_id": project.id if project else None,
                "batch_id": batch_id,
                "idempotent": True,
                "checksum": checksum,
            }
            if dry_run:
                result["dry_run"] = True
                result["already_stored"] = True
                result["would_succeed"] = idempotent_issue is None
                result["issues"] = [idempotent_issue] if idempotent_issue else []
            return result
        if _stale:
            deleted = db.execute(
                delete(BidQuoteLine).where(BidQuoteLine.submission_id == prior_submission.id)
            ).rowcount
            log.warning(
                "batch_confirm: reviving %s submission_id=%d batch=%s "
                "(cleared %d stale lines → pending)",
                prior_submission.status, prior_submission.id, batch_id, deleted,
            )
            prior_submission.status = "pending"
            prior_submission.supplier_id = supplier.id if supplier else None
            if project:
                prior_submission.project_id = project.id
        else:
            log.warning(
                "batch_confirm: rebuilding empty shell submission_id=%d batch=%s",
                prior_submission.id, batch_id,
            )
        if display_name:
            prior_submission.supplier_raw_name = display_name
        if body.bid_status:
            prior_submission.bid_status = body.bid_status
        if round_id is not None:
            prior_submission.round_id = round_id
        submission = prior_submission
    else:
        submission = BidSubmission(
            job_id=job.id,
            supplier_id=supplier.id if supplier else None,
            supplier_raw_name=display_name,
            project_id=project.id if project else None,
            round_id=round_id,
            batch_id=batch_id,
            status="pending",
            bid_status=body.bid_status,
        )
        db.add(submission)
        db.flush()

    job.lifecycle = "confirmed"

    if not items:
        result = {
            "status": "ok",
            "submission_id": submission.id,
            "line_count": 0,
            "skipped_count": 0,
            "errors": shape_errors,
            "unknown_brands": [],
            "supplier_id": submission.supplier_id,
            "project_id": project.id if project else None,
            "batch_id": batch_id,
        }
        if dry_run:
            db.rollback()
            result["dry_run"] = True
            result["would_succeed"] = True
            result["issues"] = []
            if cache_hit_key:
                dry_run_cache.put(cache_hit_key, result)
        else:
            db.commit()
        return result

    # ── 结构完整性门（doc/19 §L4）──────────────────────────────────────────────
    # 在写任何一行之前先看**表的形状**：列错位与重复行是下游唯一察觉不到的两类缺陷，
    # 错位后的金额仍是合法数字、重复行仍能通过逐行算术校验。两者都只标注和阻断，
    # 不删行、不改值、不猜正确列序——恢复正确值必须回原始页面重读。
    integrity = _gate_integrity(db, items, dry_run=dry_run, gates_advisory=gates_advisory)

    # ── 逐行处理 → BidQuoteLine ────────────────────────────────────────────────
    from apps.api.services.history.comparison import (
        determine_alert,
        get_category_thresholds,
    )

    thresholds_cache: dict[str, dict] = {}
    line_count = 0
    skipped_count = 0
    errors: list[dict] = list(shape_errors)
    unknown_brands: set[str] = set()
    line_total_sum: float = 0.0
    missing_total_rows: list[dict] = []   # 原文无合价、未经人工确认的行
    not_quoted_rows: list[dict] = []      # 原文明确写"不报价"的行——合法，不阻断
    # design/32 A1：判定为合计/表尾、未入库的行。**必须报出来**——"我们排除了
    # 一行"这件事静默发生，就等于用删行让门通过。
    aggregate_rows: list[dict] = []

    for idx, item in enumerate(items):
        try:
            raw_name = str(item.get("material") or "").strip()
            if not raw_name:
                skipped_count += 1
                continue
            if _GRAND_TOTAL_NAME_RE.search(raw_name):
                log.info("batch_confirm: skipping aggregate row %r", raw_name)
                skipped_count += 1
                continue

            # design/32 A1：合计/表尾行不是报价行。上面那条正则是这条判据的
            # 旧的、更窄的版本（实测漏掉了「含税合价（元）：」——它只有
            # 「含税合计」，一个字之差），保留是为了不改动它已经拦下的形状；
            # 下面这条用**共用词表 + 三列同值**的正面证据补齐，且**要求该行
            # 没有数量**——有数量的行永远按条目走，绝不因为名字像表尾就删掉
            # 一条真实报价（同一份文件里就有一条 qty 丢失的真条目，金额
            # 3,460 元，见 list_rows.py 模块文档）。
            _agg = classify_quote_row(
                idx,
                name=raw_name,
                spec=str(item.get("standard_spec") or item.get("spec") or ""),
                unit=str(item.get("unit") or ""),
                qty=_num_or_none(item.get("qty")),
            )
            if _agg is not None:
                log.info("batch_confirm: skipping aggregate row #%d %r (%s)",
                         _agg.index + 1, _agg.label, _agg.reason)
                aggregate_rows.append({
                    "index": _agg.index + 1, "label": _agg.label, "reason": _agg.reason,
                })
                skipped_count += 1
                continue

            item_category = str(item.get("category") or "").strip() or default_category
            if not item_category or item_category not in PROFESSION_MAP:
                errors.append({"row": idx + 1, "reason": f"invalid category: {item_category!r}"})
                skipped_count += 1
                continue

            ai_std_name = str(item.get("standard_name") or "").strip()
            standard_name = ai_std_name if ai_std_name else standardize_name(raw_name, item_category)["standardized"]

            spec = str(item.get("standard_spec") or item.get("spec") or "").strip()

            # Material 查找（P0：仅查找，不创建）
            mat: Material | None = None
            matched_mid = item.get("matched_material_id")
            if matched_mid is not None:
                try:
                    mat = db.get(Material, int(matched_mid))
                except (ValueError, TypeError):
                    pass
            if not mat:
                mat = db.scalar(select(Material).where(
                    Material.category == item_category,
                    Material.standard_name == standard_name,
                    Material.spec == spec,
                ))

            brand = str(item.get("brand") or "").strip()
            brand_tier = ""
            if brand:
                bt = db.scalar(select(BrandTier).where(BrandTier.brand_name == brand))
                if bt:
                    brand_tier = bt.tier
                else:
                    unknown_brands.add(brand)

            qty = _num_or_none(item.get("qty"))

            # 价格口径桥接（§4/§9）：现场 re-derive，不信任客户端回传的 price_basis
            basis_info = derive_price_basis(item)
            price_basis = basis_info["price_basis"]
            confirmed_unit = _num_or_none(item.get("unit_price"))
            confirmed_total = _num_or_none(item.get("total_price"))
            price = confirmed_unit if confirmed_unit is not None else basis_info["effective_unit_price"]
            total = confirmed_total if confirmed_total is not None else basis_info["effective_total_price"]
            # 权威合价只能来自原文或人工补写，**系统不得自行派生**（doc/19 §L2）。
            #   ocr     — 原文读到（raw_total_price 非空）
            #   manual  — 原文没有、用户在预览中明确补写（override 带值）
            #   missing — 原文没有、也没人工补写 → 权威值保持 None，仅留候选
            # 2026-08-09 教训：静默派生既凭空造钱（亨通单行虚增约 2000 万），
            # 又让算术校验 |qty×price − total| 恒成立，把列错位行洗白。
            raw_total_any = next(
                (v for k in ("total_price", "total_price_incl_tax", "total_price_excl_tax")
                 if (v := _num_or_none(item.get(k))) is not None), None)
            derived_candidate = _num_or_none(item.get("derived_total_candidate"))
            if derived_candidate is None and raw_total_any is None and price and qty:
                derived_candidate = round(price * qty, 4)

            # 原文明确写了"不报价"（整格是 / — 无 N/A 之类）是**合法事实**，不是缺陷。
            # 这类行合价保持 None、不参与金额比较、**不阻断**——把它当缺失会逼着用户
            # 编一个金额出来，正好制造这套系统最该防的东西。
            not_quoted = any(
                classify_amount_cell(item.get(k)) == AMOUNT_NOT_QUOTED
                for k in ("total_price", "total_price_incl_tax", "total_price_excl_tax")
            ) or bool(item.get("not_quoted"))

            if raw_total_any is not None:
                # 上游若已派生过，这里绝不能再当成 ocr —— 用 item 自带的标记判定
                total_source = "manual" if item.get("total_is_manual") else "ocr"
                total = float(confirmed_total if confirmed_total is not None
                              else basis_info["effective_total_price"] or raw_total_any)
            elif not_quoted:
                total_source = "not_quoted"
                total = None
                not_quoted_rows.append({"index": idx, "material": raw_name,
                                        "spec": str(item.get("spec") or "")})
            else:
                total_source = "missing"
                total = None
                missing_total_rows.append({
                    "index": idx,
                    "material": raw_name,
                    "spec": str(item.get("spec") or ""),
                    "qty": qty,
                    "unit_price": price,
                    "derived_total_candidate": derived_candidate,
                    "reason": "原文无合价；需人工确认后方可入库",
                    # 2026-08-23：随行带上原文备注/核对说明。真实语料里出现过这种情况：
                    # 原表在单价/合价格子印的是「/」（明确不报价），但经过 CSV/Excel
                    # 转换后，这个符号只留在"核对说明"列的文字里（映射进 remark），
                    # 单价合价两格本身变成纯空白——分类器在格子层面看不出"读不到"
                    # 和"明确不报"的区别，只能来问人。人要判断，就得看到这条备注，
                    # 而不是重新翻一遍原文。
                    "remark": str(item.get("remark") or ""),
                })

            deviation: float | None = None
            alert: str = ""
            if mat and price:
                ref = mat.ref_price_reasonable_low or mat.ref_price_median
                if ref and ref > 0:
                    if item_category not in thresholds_cache:
                        thresholds_cache[item_category] = get_category_thresholds(db, item_category)
                    deviation = round((price - ref) / ref, 4)
                    alert = determine_alert(deviation, thresholds_cache[item_category])

            extraction_meta = {
                "extraction_job_id": body.job_id,
                "source_ref": item.get("source_ref"),
                "raw_material": raw_name,
                "raw_spec": str(item.get("spec") or "").strip(),
                "raw_unit": str(item.get("unit") or "").strip(),
                "raw_remark": str(item.get("remark") or "").strip(),
                "material_type": str(item.get("material_type") or "").strip(),
                "canonical": item.get("canonical") or {},
                "validation_warning": item.get("validation_warning") or "",
                "normalized_material": str(item.get("normalized_material") or "").strip(),
                "ocr_correction_reason": str(item.get("ocr_correction_reason") or "").strip(),
                "price_basis": price_basis,
                "effective_unit_price": basis_info["effective_unit_price"],
                "effective_total_price": basis_info["effective_total_price"],
                "effective_unit_recovered": basis_info.get("effective_unit_recovered", False),
                "raw_unit_price": _num_or_none(item.get("unit_price")),
                "raw_unit_price_incl_tax": _num_or_none(item.get("unit_price_incl_tax")),
                "raw_unit_price_excl_tax": _num_or_none(item.get("unit_price_excl_tax")),
                "raw_total_price": _num_or_none(item.get("total_price")),
                "raw_total_price_incl_tax": _num_or_none(item.get("total_price_incl_tax")),
                "raw_total_price_excl_tax": _num_or_none(item.get("total_price_excl_tax")),
                "tax_rate": _num_or_none(item.get("tax_rate")),
                "tax_amount": _num_or_none(item.get("tax_amount")),
                "document_row_index": (
                    int(v) if (v := item.get("document_row_index")) is not None else None
                ),
                # 下游校验据此排除不携带算术信息的行，而不是把它们当成通过
                "total_source": total_source,
                "derived_total_candidate": derived_candidate,
                "validation_flags": (
                    list(item.get("validation_flags") or [])
                    + (["derived_total"] if total_source == "derived" else [])
                ),
                "raw_qty": _num_or_none(item.get("raw_qty")) if item.get("raw_qty") is not None else qty,
                "suggested_qty": _num_or_none(item.get("suggested_qty")),
            }

            line = BidQuoteLine(
                submission_id=submission.id,
                material_id=mat.id if mat else None,
                raw_name=raw_name,
                standard_name=standard_name,
                category=item_category,
                spec=spec,
                unit=str(item.get("unit") or ""),
                qty=qty,
                unit_price=price,
                unit_price_excl_tax=(
                    float(v) if (v := item.get("unit_price_excl_tax")) is not None else None
                ),
                tax_rate=(float(v) if (v := item.get("tax_rate")) is not None else None),
                total_price=total,
                brand=brand,
                brand_tier=brand_tier,
                remark=str(item.get("remark") or "")[:500],
                quote_date=str(item.get("quote_date") or ""),
                canonical=item.get("canonical"),
                extraction_meta=extraction_meta,
                deviation_pct=deviation,
                alert_level=alert,
                row_type=normalize_row_type(item.get("row_type")),
            )
            db.add(line)
            line_count += 1
            # design/33 §6 决策②：补位金额不进声明总价闭环门。声明总价是这份文件
            # 里**唯一不依赖抽取质量的事实**（上面的门禁注释原话）——把补位喂给它，
            # 等于用同一次识别的产物去验证那次识别是否完整，独立性就没了。
            # `gap_filled` 是 `intelligence/gap_fill.py` 写在行上的标记，一路经
            # `pipeline.py` 的 validation_flags 透传到这里的 item。
            # 2026-08-23 复核：design/33 文档原本就写着这条"已实现、有测试锁住"，
            # 但两者都不是真的——line_total_sum 之前无条件累加了每一行，这里补上。
            if total is not None and "gap_filled" not in (item.get("validation_flags") or []):
                line_total_sum += total

        except Exception as e:
            errors.append({"row": idx + 1, "reason": f"{type(e).__name__}: {e}"})
            skipped_count += 1

    # design/24 B3：dry_run 时四道门（结构完整性/原文无合价/全部跳过/声明总价）
    # 全部改"收集不阻断"——issues 攒够了再一次性报给调用方，而不是命中第一道
    # 就 raise+rollback，逼用户来回提交四次才看全四个问题。真实路径（dry_run=
    # False）三处 `if not dry_run` 分支之外的代码逐字节未变——命中即 rollback+
    # raise，跟改动前完全一样。
    issues: list[dict] = []
    if integrity.get("blocking_issue"):
        issues.append(integrity["blocking_issue"])

    # ── 派生金额安全闭环（doc/19 §L2）──────────────────────────────────────
    # 原文无合价且未经人工确认的行，一律阻断自动确认：回滚整个事务，不写
    # BidQuoteLine，不把 job 标成 confirmed。试点期采用最保守规则——**单行即阻断**，
    # 不用占比阈值：亨通实测单行列错位即可造成约 2000 万误差，5% 的行数门槛护不住。
    if missing_total_rows:
        payload = {
            "error": "missing_total_requires_review",
            "message": (
                f"{len(missing_total_rows)} 行原文无合价。系统不会代为计算，"
                f"请在预览中人工补写或确认后再提交。"
            ),
            "review_rows": missing_total_rows[:50],
            "review_row_count": len(missing_total_rows),
        }
        if dry_run or gates_advisory:
            issues.append(payload)
        else:
            db.rollback()
            raise ReviewRequiredError(payload)

    # 强校验：items 非空但全部被跳过 → 回滚并返回 422
    if items and line_count == 0:
        reason_summary = "; ".join({e["reason"] for e in errors[:3]}) if errors else "品类无效或所有行被过滤"
        all_skipped_msg = f"所有 {len(items)} 行报价均被跳过，入库已回滚。原因：{reason_summary}"
        if dry_run or gates_advisory:
            issues.append({"error": "all_rows_skipped", "message": all_skipped_msg})
        else:
            db.rollback()
            raise ReviewRequiredError(all_skipped_msg)

    # ── 声明总价闭环门（提交前，阻断）──────────────────────────────────────
    # 2026-08-09 修正：这段原本在 db.commit() **之后**执行，阈值 5%，只写 job.result
    # 不阻断。实测方向判错一页造成 0.63% 的偏差（129,532 元）会被判 pass 并正常入库——
    # 等于没有门。现在移到提交前、收紧到 CHECKSUM_BLOCK_DELTA_RATIO，且失败即回滚。
    #
    # 声明总价是这份文件里**唯一不依赖抽取质量的事实**，必须当成不可动摇的证据；
    # 对不上时要么是漏行、要么是读错值，两种都不该静默入库。
    checksum = _build_checksum(job, line_total_sum, line_count)
    if checksum["status"] == "fail" and not getattr(body, "checksum_ack", False):
        payload = {
            "error": "declared_total_mismatch",
            "message": (
                f"明细合价之和 {checksum['line_sum']:,.2f} 与文件声明总价 "
                f"{checksum['declared']:,.2f} 相差 {checksum['delta_pct']}%，"
                f"超过 {CHECKSUM_BLOCK_DELTA_RATIO:.1%} 的允许范围。"
                f"通常意味着漏行或数值读错，请核对后再提交。"
            ),
            "checksum": checksum,
        }
        if dry_run or gates_advisory:
            issues.append(payload)
        else:
            db.rollback()
            raise ReviewRequiredError(payload)

    if dry_run:
        # 先把要用的值读出来再 rollback——rollback 之后 submission/project 这些
        # 从未提交过的 ORM 对象会被 session 过期/摘除，再访问属性会炸
        # （DetachedInstanceError 之类），不是"读到旧值"那么温和。
        result = {
            "status": "ok",
            "dry_run": True,
            "would_succeed": len(issues) == 0,
            "issues": issues,
            "submission_id": submission.id,
            "line_count": line_count,
            "skipped_count": skipped_count,
            "not_quoted_rows": len(not_quoted_rows),
            "not_quoted_detail": not_quoted_rows[:50],
            "aggregate_rows": aggregate_rows,
            "integrity": integrity,
            "checksum": checksum,
            "errors": errors,
            "unknown_brands": sorted(unknown_brands),
            "supplier_id": submission.supplier_id,
            "project_id": project.id if project else None,
            "batch_id": batch_id,
            "copy_dedup": copy_dedup,
        }
        db.rollback()   # 从不写：submission/lines/job.lifecycle 全部撤销
        if cache_hit_key:
            dry_run_cache.put(cache_hit_key, result)
        return result

    # gates_advisory 下门不阻断，但结论必须原样带回调用方——不然预览就成了
    # "悄悄放行"，比阻断更糟。真实路径（两个开关都关）时 issues 恒为空列表，
    # 响应形状对既有调用方没有变化。
    advisory_issues = list(issues)

    job.result = {**(job.result or {}), "_checksum": checksum}
    ctx = dict(job.context or {})
    if submission.supplier_id and ctx.get("supplier_id") != submission.supplier_id:
        ctx["supplier_id"] = submission.supplier_id
        job.context = ctx
    db.add(job)

    write_domain_event(
        db, user="system", event_type=EVENT_BQL_CONFIRM,
        identity={
            "project_id": project.id if project else None,
            "submission_id": submission.id,
        },
        after={
            "line_count": line_count,
            "supplier_name": display_name,
            "category": default_category,
            "batch_id": batch_id,
            "checksum_status": checksum["status"],
            "copy_dedup": copy_dedup,
        },
        meta={"skipped_count": skipped_count},
    )
    db.commit()

    return {
        "status": "ok",
        "submission_id": submission.id,
        "line_count": line_count,
        "skipped_count": skipped_count,
        "missing_total_rows": 0,
        # 原文明确不报价的行：已入库、合价为 None、不参与金额比较。
        # 必须报出来，否则下游会把"没有金额"误读成"漏识别"。
        "not_quoted_rows": len(not_quoted_rows),
        "not_quoted_detail": not_quoted_rows[:50],
        # design/32 A1：被判为合计/表尾而未入库的行。空列表是常态；非空时
        # 调用方必须让用户看见——静默排除等于用删行让门通过。
        "aggregate_rows": aggregate_rows,
        # 通过但被怀疑过的行：重复行已入库，但带着 duplicate_row 标记，
        # 前端应提示人工复核（REVIEW 不等于拒收——合法的重复真实存在）
        "integrity": integrity,
        # gates_advisory 下被降级成警告的门（声明总价对不上、原文无合价…）。
        # 两个开关都关时恒为 []，既有调用方读不到任何新东西。
        "issues": advisory_issues,
        "errors": errors,
        "unknown_brands": sorted(unknown_brands),
        "supplier_id": submission.supplier_id,
        "project_id": project.id if project else None,
        "batch_id": batch_id,
        # design/24 B0：非 None 表示识别到多份合法副本（copy_no），本次只选了
        # 其中一份入库——前端据此提示用户"识别到 N 份重复清单，已选第 X 份"，
        # 而不是让用户看着 line_count 比预期少一半却不知道为什么。
        "copy_dedup": copy_dedup,
    }
