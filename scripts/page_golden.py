"""page_golden.py — 页面分类 golden 与混淆矩阵基础设施（P0）。

只做页面分类评估，不碰表格抽取/DB/match。

子命令：
  montage <doc>     渲染逐页缩略图 montage（带页码）+ Excel交叉校验信号表，供看图标注 role
  matrix <doc>      需先有 data/golden/pages_<doc>.json + 视觉分类快照，输出混淆矩阵

逐页 golden role 取值（VisualPageRole）：
  cover / bid_letter / tender_table_header / tender_table_continuation /
  quote_table_header / quote_table_continuation / subtotal_or_summary /
  brand_requirement / technical_spec / component_parameter_table / certificate / other
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DOCS = REPO / "docs" / "test"
GOLDEN_DIR = REPO / "data" / "golden"
SNAP_DIR = REPO / "tests" / "fixtures" / "ocr_snapshots"
OUT_DIR = REPO / "outputs" / "page_golden"

# doc → (pdf, snapshot, row-level golden, doc_type)
DOC_CFG = {
    "taikelong": ("泰科龙投标文件.pdf", "quote_taikelong.json", "quote_taikelong.json", "quote"),
    "miancun": ("上海绵存投标文件.pdf", "quote_miancun.json", "quote_miancun.json", "quote"),
    "kaishuo": ("凯硕新正投标文件.pdf", "quote_kaishuo.json", "quote_kaishuo.json", "quote"),
    "jingqiao": ("金桥地体上盖招标文件.pdf", "tender_jingqiao.json", "tender_jingqiao.json", "tender"),
}

QUOTE_TARGET = {"quote_table_header", "quote_table_continuation"}
TENDER_TARGET = {"tender_table_header", "tender_table_continuation"}


def _load_htmls(snap_name: str, pdf_name: str) -> list[str]:
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.document_loader import DocumentLoader, MAX_PAGES_UNLIMITED
    snap = SnapshotProvider(None, SNAP_DIR / snap_name, mode="replay")
    imgs = DocumentLoader.to_images(str(DOCS / pdf_name), max_pages=MAX_PAGES_UNLIMITED)
    roles, _ = snap.ocr_pages_with_roles(imgs)
    return [h for (_c, h) in roles]


def _golden_names(golden_name: str) -> list[str]:
    g = json.loads((GOLDEN_DIR / golden_name).read_text(encoding="utf-8"))
    names = []
    for r in g.get("rows", []):
        nm = (r.get("name") or "").strip()
        if len(nm) >= 2:
            names.append(nm)
    return names


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def cmd_montage(doc: str):
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw

    pdf_name, snap_name, golden_name, doc_type = DOC_CFG[doc]
    htmls = _load_htmls(snap_name, pdf_name)
    names = _golden_names(golden_name)
    out = OUT_DIR / doc
    out.mkdir(parents=True, exist_ok=True)

    cols, thumb_w, per_sheet = 4, 380, 12
    pdf = pdfium.PdfDocument(str(DOCS / pdf_name))
    n = len(pdf)
    sheet, thumbs = 0, []

    def _flush():
        nonlocal sheet, thumbs
        if not thumbs:
            return
        rows = (len(thumbs) + cols - 1) // cols
        th = max(t.height for _p, t in thumbs)
        canvas = Image.new("RGB", (cols * thumb_w, rows * (th + 26)), (235, 235, 235))
        dr = ImageDraw.Draw(canvas)
        for i, (pno, t) in enumerate(thumbs):
            r, c = divmod(i, cols)
            x, y = c * thumb_w, r * (th + 26)
            dr.rectangle([x, y, x + thumb_w - 2, y + 24], fill=(20, 20, 20))
            dr.text((x + 6, y + 6), f"page {pno}", fill=(255, 255, 0))
            canvas.paste(t, (x, y + 26))
        p = out / f"montage_{sheet}.png"
        canvas.save(p)
        print(f"  saved {p.relative_to(REPO)} (pages {thumbs[0][0]}–{thumbs[-1][0]})")
        thumbs, sheet = [], sheet + 1

    for i in range(n):
        img = pdf[i].render(scale=1.0).to_pil().convert("RGB")
        ratio = thumb_w / img.width
        thumbs.append((i + 1, img.resize((thumb_w, int(img.height * ratio)))))
        if len(thumbs) >= per_sheet:
            _flush()
    _flush()
    pdf.close()

    # Excel 交叉校验：每页 OCR HTML 命中多少 golden 行名
    print(f"\n=== {doc} ({doc_type}) 逐页信号：golden 行名命中 + 结构 ===")
    print(f"{'pg':>3} {'goldHit':>7} {'tr':>3} {'amt':>4} {'dn':>3}  price/tech/cert 关键词")
    for i, h in enumerate(htmls):
        hit = sum(1 for nm in names if _norm(nm) and _norm(nm) in _norm(h))
        tr = h.count("<tr")
        amt = len(re.findall(r"\d+\.\d{2}", h))
        dn = len(re.findall(r"DN\s*\d+", h))
        price = [k for k in ("单价", "合价", "价税合计", "含税") if k in h]
        tech = [k for k in ("技术", "规范", "施工", "试压", "条款", "阀体", "密封圈") if k in h]
        cert = [k for k in ("营业执照", "信用代码", "资质证书") if k in h]
        print(f"{i+1:>3} {hit:>7} {tr:>3} {amt:>4} {dn:>3}  "
              f"{','.join(price)}|{','.join(tech[:2])}|{','.join(cert[:1])}")
    print(f"\ngolden 行数={len(names)}；下一步：看 montage + 本表，写 data/golden/pages_{doc}.json")


def _layer_stats(gold_tbl: set, pred_tbl: set, n_total: int) -> dict:
    non_tbl = n_total - len(gold_tbl)
    tp = len(gold_tbl & pred_tbl)
    fn = sorted(gold_tbl - pred_tbl)
    fp = sorted(pred_tbl - gold_tbl)
    recall = tp / len(gold_tbl) if gold_tbl else 1.0
    fpr = len(fp) / non_tbl if non_tbl else 0.0
    return {"tp": tp, "fn": fn, "fp": fp, "recall": recall, "fpr": fpr,
            "gold": len(gold_tbl), "pred": len(pred_tbl)}


def cmd_matrix(doc: str):
    pdf_name, snap_name, golden_name, doc_type = DOC_CFG[doc]
    gpath = GOLDEN_DIR / f"pages_{doc}.json"
    if not gpath.exists():
        raise SystemExit(f"缺少逐页 golden: {gpath}")
    golden = json.loads(gpath.read_text(encoding="utf-8"))
    gold_role = {int(p["page"]): p["role"] for p in golden["pages"]}
    gold_orient = {int(p["page"]): p.get("orientation", 0) for p in golden["pages"]}

    # 完整三阶段管线 + debug 快照
    # 使用 record 模式：已有 v3 缓存命中，缺失时打 API 更新快照（prompt v3 第一次运行）
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.document_loader import DocumentLoader, MAX_PAGES_UNLIMITED
    from apps.api.intelligence.table_recognizer import _classify_pages
    from apps.api.core.config import get_settings
    try:
        _s = get_settings()
        from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
        inner = DashScopeOCRProvider(
            api_key=_s.DASHSCOPE_API_KEY, base_url=_s.DASHSCOPE_BASE_URL,
            ocr_model=_s.DASHSCOPE_OCR_MODEL, llm_model=_s.DASHSCOPE_LLM_MODEL,
        ) if _s.DASHSCOPE_API_KEY else None
    except Exception:
        inner = None
    mode = "record" if inner else "replay"
    snap = SnapshotProvider(inner, SNAP_DIR / snap_name, mode=mode)
    thumbs = DocumentLoader.to_thumbnails(str(DOCS / pdf_name), max_pages=MAX_PAGES_UNLIMITED)
    images = DocumentLoader.to_images(str(DOCS / pdf_name), max_pages=MAX_PAGES_UNLIMITED)
    _debug: dict = {}
    page_cls, _flash_n, _plus_n = _classify_pages(snap, thumbs, images, doc_type, _debug=_debug)
    if mode == "record" and snap._visual_misses:
        snap.save()
        print(f"  [snapshot updated: {snap._visual_misses} new visual entries]")

    target = QUOTE_TARGET if doc_type == "quote" else TENDER_TARGET
    gold_tbl = {p for p, r in gold_role.items() if r in target}
    n = len(gold_role)

    # ── 分层混淆矩阵 ──────────────────────────────────────────────────────────
    layers = {
        "Flash 原始": {c.page: c.role.value for c in _debug.get("flash", page_cls)},
        "Plus 复判后": {c.page: c.role.value for c in _debug.get("after_plus", page_cls)},
        "语义覆写后": {c.page: c.role.value for c in _debug.get("final", page_cls)},
    }

    print(f"\n=== {doc} ({doc_type}) 页面分类混淆矩阵（分层）===")
    prev_tbl: set | None = None
    layer_stats = []
    for layer_name, pred_role in layers.items():
        pred_tbl = {p for p, r in pred_role.items() if r in target}
        stats = _layer_stats(gold_tbl, pred_tbl, n)
        layer_stats.append(stats)

        corrections = new_errors = rescues = 0
        if prev_tbl is not None:
            # 修正：上层是FP/FN，此层变正确
            corrections = len((prev_tbl - gold_tbl) - (pred_tbl - gold_tbl))  # FP修正
            corrections += len((gold_tbl - prev_tbl) - (gold_tbl - pred_tbl))  # FN救回
            # 新增错误：上层正确，此层错误
            new_errors = len((pred_tbl - gold_tbl) - (prev_tbl - gold_tbl))   # 新FP
            new_errors += len((gold_tbl - pred_tbl) - (gold_tbl - prev_tbl))  # 新FN
            rescues = len((gold_tbl - prev_tbl) - (gold_tbl - pred_tbl))       # FN→TP
            status = f"  [修正{corrections} 救回{rescues} 新错{new_errors}]"
        else:
            status = ""

        pass_str = "PASS" if stats["recall"] >= 1.0 and stats["fpr"] <= 0.05 else "FAIL"
        print(f"\n  [{layer_name}]{status}")
        print(f"    召回={stats['tp']}/{stats['gold']}={stats['recall']:.0%}  "
              f"误入={len(stats['fp'])}/{n - stats['gold']}={stats['fpr']:.0%}  {pass_str}")
        if stats["fn"]:
            print(f"    漏掉: {stats['fn']}")
        if stats["fp"]:
            print(f"    误入: {stats['fp']}")
        prev_tbl = pred_tbl

    # ── 逐页详情（不一致项）────────────────────────────────────────────────────
    final_pred = {c.page: c.role.value for c in _debug.get("final", page_cls)}
    flash_pred = {c.page: c.role.value for c in _debug.get("flash", page_cls)}
    plus_pred = {c.page: c.role.value for c in _debug.get("after_plus", page_cls)}
    print("\n  逐页差异（golden vs 各层）:")
    for p in sorted(gold_role):
        g = gold_role[p]
        fv, pv, fiv = flash_pred.get(p, "?"), plus_pred.get(p, "?"), final_pred.get(p, "?")
        if g != fiv or (p in gold_tbl) != (p in {q for q, r in final_pred.items() if r in target}):
            marker = "X" if g != fiv else " "
            print(f"    {marker} p{p:>3} gold={g:<28} flash={fv:<28} plus={pv:<28} final={fiv}")

    # ── 方向统计 ──────────────────────────────────────────────────────────────
    nonzero_gold_pages = {p for p, o in gold_orient.items() if o != 0}
    flash_orient = {c.page: c.orientation for c in _debug.get("flash", page_cls)}
    final_orient = {c.page: c.orientation for c in _debug.get("final", page_cls)}

    visual_correct = sum(1 for p in nonzero_gold_pages
                         if flash_orient.get(p, 0) == gold_orient[p])
    fallback_pages = [p for p in nonzero_gold_pages
                      if flash_orient.get(p, 0) != gold_orient[p]
                      and final_orient.get(p, 0) == gold_orient[p]]

    print(f"\n  方向统计（golden 非零旋转页={len(nonzero_gold_pages)}）：")
    print(f"    qwen3-vl 视觉模型正确: {visual_correct}/{len(nonzero_gold_pages)}"
          + (" OK" if len(nonzero_gold_pages) == 0 or visual_correct == len(nonzero_gold_pages)
             else f" — 算法兜底可修正 {len(fallback_pages)} 页"))
    if nonzero_gold_pages:
        print(f"    算法兜底（OCR质量旋转检测）修正: {len(fallback_pages)} 页 {fallback_pages}")
        wrong_visual = [p for p in nonzero_gold_pages if flash_orient.get(p, 0) != gold_orient[p]]
        if wrong_visual:
            print(f"    视觉方向错误页: {wrong_visual} "
                  f"(gold={[gold_orient[p] for p in wrong_visual]} "
                  f"visual={[flash_orient.get(p,0) for p in wrong_visual]})")

    final = layer_stats[-1]
    return {"doc": doc, "recall": final["recall"], "fpr": final["fpr"],
            "fn": final["fn"], "fp": final["fp"]}


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("montage", "matrix"):
        print(__doc__)
        sys.exit(1)
    cmd, doc = sys.argv[1], sys.argv[2]
    if doc not in DOC_CFG:
        raise SystemExit(f"未知文档: {doc}，可选 {list(DOC_CFG)}")
    (cmd_montage if cmd == "montage" else cmd_matrix)(doc)
