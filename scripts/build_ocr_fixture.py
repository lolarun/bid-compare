"""build_ocr_fixture.py — 把指定 PDF 页在指定角度的 OCR HTML 落盘成离线夹具。

用途：让方向纠正 / 表头继承 / 列映射这类**确定性**逻辑可以零成本、秒级回归，
不必每改一行就跑一轮 fresh E2E（doc/19 §L1；识别规则「输入输出必须可快照重放」）。

用法：
    python scripts/build_ocr_fixture.py \
        --pdf tests/fixtures/documents/xxx.pdf --page 3 --page 5 --angle 0 --angle 270 \
        --out apps/api/tests/fixtures/ocr_html/yuandong

每个 (页, 角度) 落一个文件：p<页>_r<角度>.html，并写一份 manifest.json 记录
来源文件、SHA256、渲染参数，便于追溯。已存在的文件默认跳过（--force 覆盖）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--page", type=int, action="append", required=True, help="1-based，可重复")
    ap.add_argument("--angle", type=int, action="append", default=None,
                    help="顺时针角度，可重复；默认 0/90/180/270 全取")
    ap.add_argument("--out", required=True)
    ap.add_argument("--scale", type=float, default=2.0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    import pypdfium2 as pdfium
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider

    angles = args.angle or [0, 90, 180, 270]
    pdf = Path(args.pdf)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    provider = DashScopeOCRProvider()
    doc = pdfium.PdfDocument(str(pdf))
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.setdefault("source_pdf", pdf.name)
    manifest.setdefault("render_scale", args.scale)
    manifest.setdefault("pages", {})

    for page_no in args.page:
        page = doc[page_no - 1]
        pil = page.render(scale=args.scale).to_pil().convert("RGB")
        page.close()
        for angle in angles:
            name = f"p{page_no}_r{angle}.html"
            target = out / name
            if target.exists() and not args.force:
                print(f"  skip {name} (exists)")
                continue
            img = pil if angle == 0 else pil.rotate(-angle, expand=True)  # PIL 正角=逆时针
            buf = io.BytesIO()
            img.save(buf, "PNG")
            raw = buf.getvalue()
            try:
                results, _fail = provider.ocr_pages_with_roles([raw])
                html = results[0][1] if results else ""
            except Exception as exc:
                print(f"  !! {name} OCR 失败: {type(exc).__name__}: {exc}")
                continue
            # 去掉 markdown 围栏，夹具存纯 HTML
            html = html.replace("```html", "").replace("```", "").strip()
            target.write_text(html, encoding="utf-8")
            manifest["pages"].setdefault(str(page_no), {})[str(angle)] = {
                "file": name,
                "chars": len(html),
                "sha256": hashlib.sha256(html.encode()).hexdigest()[:16],
            }
            print(f"  wrote {name}  {len(html)} chars")

    doc.close()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"manifest → {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
