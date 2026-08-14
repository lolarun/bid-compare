"""stitch_vs_multi_bench.py — 拼图 vs 多图：抽取阶段耗时 + 准确率对比实验。

背景：生产管线（vl_quote.py::recognize_quote_vl）已经是"整份文档一次调用"——
N 页图片作为同一次请求里的 N 个独立 image part 一起发送，不是逐页调用 LLM。
本实验想验证的不是"减少调用次数"（已经是 1 次），而是"把 N 个 image part
合并成 1 张拼接大图会不会更快"。

动手前先说清楚两条结构性张力，免得看到数字误读：
1. 拼接后单图总像素远超单页；若沿用生产的 max_pixels=8_000_000 上限，
   N 页均分下来分辨率会被压得很低，表格文字大概率读不清（B 组）。
   若把 max_pixels 按页数等比例调大以保清晰度（C 组），总像素预算基本没变，
   "更快"这件事本身就存疑——省下的至多是每个 image part 的固定编码开销，
   不是像素对应的 token 本身。
2. PROMPT_QUOTE_CSV 第 4 条规则把 page 列定义为"按我给你的图像顺序"——
   拼成 1 张图后这个定义直接失效，B/C 组的 page 列预期会退化（不影响
   diff_doc 的内容级评分，如实报告，不悄悄略过）。

三组条件，同一份 PDF、同一次方向校正结果、同一个 prompt，只变"怎么打包图片"：
  A. baseline         —— 生产现状：N 张图分开传（对照组）
  B. stitched_capped  —— 拼成 1 张图，max_pixels 沿用生产默认 8,000,000
  C. stitched_scaled  —— 拼成 1 张图，max_pixels 按原始总像素等比例放大

用法：
    python scripts/stitch_vs_multi_bench.py --doc kaishuo --repeats 1 --out tmp/stitch_bench

产物：<out>/manifest.json（含每组耗时/行召回率/字段准确率）+ 每组的行级 CSV 解析结果。
只读 fixture PDF + golden JSON，不落库、不改生产代码——纯粹一次性测量脚本。
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from apps.api.core.config import get_settings                              # noqa: E402
from apps.api.intelligence.document_loader import DocumentLoader            # noqa: E402
from apps.api.intelligence.vl_quote import (                               # noqa: E402
    PROMPT_QUOTE_CSV, build_draft, detect_rotations,
)
from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider  # noqa: E402
from e2e_diff import diff_doc                                              # noqa: E402

DOCS = {
    "kaishuo": {"pdf": "tests/fixtures/documents/bid/凯硕新正投标文件.pdf",
                "golden": "data/golden/quote_kaishuo.json", "name": "凯硕新正"},
    "taikelong": {"pdf": "tests/fixtures/documents/bid/泰科龙投标文件.pdf",
                  "golden": "data/golden/quote_taikelong.json", "name": "泰科龙"},
    "miancun": {"pdf": "tests/fixtures/documents/bid/上海绵存投标文件.pdf",
                "golden": "data/golden/quote_miancun.json", "name": "上海绵存"},
}

# 生产 vl_extract_csv 的默认单图像素上限（dashscope_ocr.py），B 组沿用它。
PER_PAGE_MAX_PIXELS = 8_000_000
# C 组"等比例放大"时的硬上限，避免单次请求过大被服务端拒绝或拖慢到不可比。
STITCH_SCALED_CAP = 30_000_000
SEP_PX = 6  # 拼接页间分隔线，帮模型看清页边界（不算样本专属 hack，纯通用排版）


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def stitch_vertical(images: dict[int, bytes], *, max_pixels: int | None = None) -> tuple[bytes, int]:
    """按页码顺序竖直拼接，宽度对齐到最宽的一页，返回 (png_bytes, 总像素)。

    max_pixels 不是像 vl_extract_csv 里那样只当服务端提示——那只影响服务端怎么
    解读图片，本地上传的字节数不受它约束。第一版脚本就是踩了这个坑：拼出
    68M 像素的画布，PNG 72MB / base64 96MB，Windows 网络栈直接把连接中止
    （WinError 10053），A/B/C 三组根本没法比。这里改成真的在本地把画布缩到
    目标像素预算以内再编码——这也是任何真实想拼图省时间的实现必须做的事，
    不是为了测试而加的特殊处理。
    """
    from PIL import Image

    pages = sorted(images)
    pil_imgs = [Image.open(io.BytesIO(images[p])).convert("RGB") for p in pages]
    target_w = max(im.width for im in pil_imgs)
    resized = []
    for im in pil_imgs:
        if im.width != target_w:
            k = target_w / im.width
            im = im.resize((target_w, max(1, int(im.height * k))), Image.LANCZOS)
        resized.append(im)
    total_h = sum(im.height for im in resized) + SEP_PX * (len(resized) - 1)
    canvas = Image.new("RGB", (target_w, total_h), (255, 255, 255))
    y = 0
    for i, im in enumerate(resized):
        canvas.paste(im, (0, y))
        y += im.height
        if i < len(resized) - 1:
            y += SEP_PX

    if max_pixels is not None:
        actual = canvas.width * canvas.height
        if actual > max_pixels:
            k = (max_pixels / actual) ** 0.5
            new_size = (max(1, int(canvas.width * k)), max(1, int(canvas.height * k)))
            canvas = canvas.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    canvas.save(buf, "PNG", compress_level=1)
    return buf.getvalue(), canvas.width * canvas.height


def run_condition(label: str, call_images: list[bytes], max_pixels: int, *,
                   model: str, prov: DashScopeOCRProvider, page_count: int,
                   pdf_path: Path, rotations: dict[int, int], unresolved: list[int]) -> dict:
    t0 = time.time()
    try:
        csv_text = prov.vl_extract_csv(call_images, PROMPT_QUOTE_CSV,
                                       model=model, max_pixels=max_pixels)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 1)}
    seconds = round(time.time() - t0, 1)
    draft = build_draft(
        csv_text, file_path=str(pdf_path), page_count=page_count,
        processed_pages=list(range(1, page_count + 1)),
        rotations=rotations, unresolved_pages=unresolved,
    )
    return {"label": label, "seconds": seconds, "quality": draft.quality.status,
            "row_count": len(draft.rows),
            "quote_lines": sum(1 for r in draft.rows if r.row_type == "quote_line"),
            "_draft_rows": draft.rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="kaishuo", choices=list(DOCS))
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--votes", type=int, default=3, help="方向校正投票轮数（同生产默认）")
    ap.add_argument("--out", default="tmp/stitch_bench")
    ap.add_argument("--only", default="A,B,C",
                     help="只跑指定组（逗号分隔，如 B,C）——方向校正每次都会重跑（三组共用同一次结果），"
                          "但已经拿到干净数据的组没必要重付一次 API 调用")
    ap.add_argument("--c-pixels", type=int, default=STITCH_SCALED_CAP,
                     help="C 组拼接图目标像素上限，用于在“传不上去”和“太糊”之间找折中点")
    a = ap.parse_args()
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}

    doc = DOCS[a.doc]
    pdf_path = REPO / doc["pdf"]
    golden = json.loads((REPO / doc["golden"]).read_text(encoding="utf-8"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    s = get_settings()
    prov = DashScopeOCRProvider()
    model = s.DASHSCOPE_QUOTE_VL_MODEL
    orient_model = s.DASHSCOPE_QUOTE_ORIENT_MODEL

    page_count = DocumentLoader.get_page_count(str(pdf_path))
    print(f"[{doc['name']}] {page_count} 页 · 抽取模型 {model} · 方向模型 {orient_model}")

    images = DocumentLoader.render_pages(str(pdf_path), list(range(1, page_count + 1)))

    # ── 方向校正：三组条件共用同一次结果——这正是用户问题里"识别完方向之后"的前提 ──
    def orient_call(parts, prompt):
        return prov.vl_extract_csv([b for _t, b in parts], prompt, model=orient_model,
                                   labels=[t for t, _b in parts])

    t_orient = time.time()
    rotations, unresolved = detect_rotations(images, orient_call, votes=a.votes)
    orient_seconds = round(time.time() - t_orient, 1)
    from PIL import Image
    for p, deg in rotations.items():
        with Image.open(io.BytesIO(images[p])) as im:
            buf = io.BytesIO()
            im.convert("RGB").rotate(-deg, expand=True).save(buf, "PNG")
            images[p] = buf.getvalue()
    print(f"方向校正（{orient_seconds}s）：{len(rotations)}/{page_count} 页已转正，"
          f"{len(unresolved)} 页未达成共识（不猜，按 REVIEW 处理）")

    ordered_pages = sorted(images)
    per_page_pixels = 0
    for p in ordered_pages:
        with Image.open(io.BytesIO(images[p])) as im:
            per_page_pixels += im.width * im.height

    scaled_cap = min(a.c_pixels, int(per_page_pixels))
    # B/C 各自本地缩到自己的目标像素预算——不是共用一张原始大图再靠服务端
    # max_pixels 提示"顺便"缩小，那样上传的字节数不会变（见 stitch_vertical 注释）。
    stitched_b_bytes, stitched_b_pixels = stitch_vertical(images, max_pixels=PER_PAGE_MAX_PIXELS)
    stitched_c_bytes, stitched_c_pixels = stitch_vertical(images, max_pixels=scaled_cap)
    print(f"单页合计像素 {per_page_pixels:,} · B 组拼接后 {stitched_b_pixels:,} 像素 "
          f"({len(stitched_b_bytes)/1024/1024:.1f}MB) · "
          f"C 组拼接后 {stitched_c_pixels:,} 像素 ({len(stitched_c_bytes)/1024/1024:.1f}MB, "
          f"目标上限 {scaled_cap:,})")

    results: list[dict] = []
    for rep in range(a.repeats):
        suffix = f"_rep{rep}" if a.repeats > 1 else ""
        if "A" in only:
            results.append(run_condition(
                f"A_baseline_multi_image{suffix}",
                [images[p] for p in ordered_pages], PER_PAGE_MAX_PIXELS,
                model=model, prov=prov, page_count=page_count, pdf_path=pdf_path,
                rotations=rotations, unresolved=unresolved))
        if "B" in only:
            results.append(run_condition(
                f"B_stitched_capped_8M{suffix}",
                [stitched_b_bytes], PER_PAGE_MAX_PIXELS,
                model=model, prov=prov, page_count=page_count, pdf_path=pdf_path,
                rotations=rotations, unresolved=unresolved))
        if "C" in only:
            results.append(run_condition(
                f"C_stitched_scaled{suffix}",
                [stitched_c_bytes], scaled_cap,
                model=model, prov=prov, page_count=page_count, pdf_path=pdf_path,
                rotations=rotations, unresolved=unresolved))

    manifest = {
        "doc": doc["name"], "pdf": doc["pdf"], "pages": page_count,
        "model": model, "orient_model": orient_model, "orient_votes": a.votes,
        "orient_seconds": orient_seconds,
        "rotations": rotations, "orientation_unresolved": unresolved,
        "per_page_total_pixels": per_page_pixels,
        "stitched_b_pixels": stitched_b_pixels, "stitched_b_mb": round(len(stitched_b_bytes) / 1024 / 1024, 1),
        "stitched_c_pixels": stitched_c_pixels, "stitched_c_mb": round(len(stitched_c_bytes) / 1024 / 1024, 1),
        "code_sha": _git_sha(),
        "runs": [],
    }

    print(f"\n{'条件':38s} {'耗时(s)':>8s} {'总行数':>6s} {'明细行':>6s} "
          f"{'行召回率':>9s} {'行准确率':>9s} {'quality':>8s}")
    for r in results:
        entry = {k: v for k, v in r.items() if k != "_draft_rows"}
        if "error" in r:
            print(f"{r['label']:38s}  失败: {r['error']}")
        else:
            scored = diff_doc(doc["name"], golden, r["_draft_rows"])
            row_level = scored["summary"]["row_level"]
            entry["row_recall"] = row_level["row_recall"]
            entry["row_precision"] = row_level["row_precision"]
            entry["declared_vs_all_diff"] = scored["summary"]["document_level"]["declared_vs_all_diff"]
            entry["field_metrics"] = scored["summary"]["field_metrics"]
            (out / f"{r['label']}_rows.json").write_text(
                json.dumps([{"row_index": row.row_index, "row_type": row.row_type,
                            **row.fields} for row in r["_draft_rows"]],
                           ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{r['label']:38s} {r['seconds']:>8.1f} {entry['row_count']:>6d} "
                  f"{entry['quote_lines']:>6d} {entry['row_recall']:>9.1%} "
                  f"{entry['row_precision']:>9.1%} {r['quality']:>8s}")
        manifest["runs"].append(entry)

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n产物 → {out}（含 manifest.json 与每组行级解析结果）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
