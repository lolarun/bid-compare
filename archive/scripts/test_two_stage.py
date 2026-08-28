"""Two-stage OCR+LLM pipeline — per-page parallel + pipelined.

Stage 1: Qwen-VL-OCR (table_parsing) -> HTML per page (concurrent)
Stage 2: qwen3.6-flash -> structured JSON per page (concurrent)

OCR and LLM run as a pipeline: each page enters Stage 2 as soon as its OCR finishes.
All models use Alibaba DashScope.
"""
from __future__ import annotations
import base64, csv, io, json, os, re, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

env_file = ROOT / "apps" / "api" / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import dashscope

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
dashscope.api_key = DASHSCOPE_API_KEY

TEXT_MODEL = "qwen3.6-flash"
OCR_CONCURRENCY = 20
LLM_CONCURRENCY = 20
RENDER_SCALE = 2.0
MAX_EDGE_PX = 2400

TEST_DIR = ROOT / "tests" / "fixtures" / "documents" / "bid"
TEST_DIR_OTHER = ROOT / "docs" / "test"  # design/28 不迁移的其他材料类别夹具
PDFS = [
    TEST_DIR / "泰科龙投标文件.pdf",
    TEST_DIR / "凯硕新正投标文件.pdf",
    TEST_DIR / "上海绵存投标文件.pdf",
    TEST_DIR_OTHER / "徐汇区华泾镇D5B一期桥架上海浩财实业有限公司桥架报价清单9页.pdf",
]

OUT_DIR = ROOT / "data" / "two_stage"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TENDER_FIELDS = ["name", "category", "spec", "unit", "quantity", "remark"]
QUOTE_FIELDS = ["material", "spec", "brand", "unit", "qty", "unit_price",
                "unit_price_excl_tax", "total_price", "tax_rate", "remark"]

STAGE2_TENDER_PROMPT = """你是机电材料招投标助理。下面是OCR识别出的HTML表格内容。
请从中提取采购材料清单，返回严格的JSON格式。

要求：
- 只提取材料/设备条目，不要表头、合计行、小计行
- 材料名称按原文，不要简化
- 品类从以下选项选择：桥架、母线槽、配电箱、阀门、不锈钢管、水箱、潜水泵、风口风阀、风机盘管、空调泵；无法判断留空
- 数量若为'若干'等非数字，留null
- 无法识别的字段返回空字符串或null

返回JSON格式：
{"supplier_name": "投标单位名称", "items": [{"name": "材料名称", "category": "品类", "spec": "规格型号", "unit": "单位", "quantity": 数量或null, "remark": "备注"}]}

如果该页没有材料清单（如封面、证书等非清单页），返回 {"items": []}"""

STAGE2_QUOTE_PROMPT = """你是机电材料报价单解析助理。下面是OCR识别出的HTML表格内容。
请从中提取报价明细，返回严格的JSON格式。

要求：
- 只提取材料报价行，不要表头、合计行、小计行
- 区分 unit_price（含税单价）与 unit_price_excl_tax（不含税单价）
- 总价若已标注使用原值，否则留null
- 税率用小数如0.13表示13%
- 品牌按原文
- 无法识别的字段返回空字符串或null

返回JSON格式：
{"supplier_name": "供应商名称", "items": [{"material": "材料名称", "spec": "规格型号", "brand": "品牌", "unit": "单位", "qty": 数量, "unit_price": 含税单价, "unit_price_excl_tax": 不含税单价, "total_price": 总价, "tax_rate": 税率小数, "remark": "备注"}]}

如果该页没有报价明细（如封面、证书等非报价页），返回 {"items": []}"""


def guess_type(p: Path) -> str:
    return "quote" if "报价" in p.name else "tender"


# ── Render ──

def render_pdf(path: Path) -> list[bytes]:
    pdf = pdfium.PdfDocument(str(path))
    try:
        images = []
        for i in range(len(pdf)):
            page = pdf[i]
            pil_img = page.render(scale=RENDER_SCALE).to_pil()
            w, h = pil_img.size
            longest = max(w, h)
            if longest > MAX_EDGE_PX:
                scale = MAX_EDGE_PX / longest
                pil_img = pil_img.resize(
                    (int(w * scale), int(h * scale)), Image.LANCZOS
                )
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG", optimize=True)
            images.append(buf.getvalue())
        return images
    finally:
        pdf.close()


# ── Stage 1: OCR ──

def ocr_page(page_bytes: bytes, page_idx: int) -> dict:
    b64 = base64.b64encode(page_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64}"
    t0 = time.time()
    try:
        resp = dashscope.MultiModalConversation.call(
            model="qwen-vl-ocr-latest",
            messages=[{
                "role": "user",
                "content": [{"image": data_uri, "min_pixels": 3136, "max_pixels": 8388608}],
            }],
            ocr_options={"task": "table_parsing"},
        )
        elapsed = time.time() - t0
        if resp.status_code != 200:
            return {"page": page_idx + 1, "ok": False, "error": f"{resp.status_code}: {resp.message}",
                    "elapsed": elapsed, "html": "", "tokens": 0}
        text = ""
        tokens = 0
        if resp.output and resp.output.choices:
            choice = resp.output.choices[0]
            if choice.message and choice.message.content:
                for part in choice.message.content:
                    if hasattr(part, "text"):
                        text += part.text
                    elif isinstance(part, dict) and "text" in part:
                        text += part["text"]
        if resp.usage:
            tokens = getattr(resp.usage, "total_tokens", 0) or 0
        return {"page": page_idx + 1, "ok": True, "html": text,
                "elapsed": elapsed, "tokens": tokens, "error": ""}
    except Exception as e:
        return {"page": page_idx + 1, "ok": False, "error": str(e),
                "elapsed": time.time() - t0, "html": "", "tokens": 0}


# ── Stage 2: Text LLM ──

_client = None
def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)
    return _client


def llm_parse_page(html: str, page_idx: int, doc_type: str) -> dict:
    prompt = STAGE2_QUOTE_PROMPT if doc_type == "quote" else STAGE2_TENDER_PROMPT
    t0 = time.time()
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"以下是第{page_idx+1}页的OCR结果：\n\n{html}"},
            ],
            temperature=0.1,
            max_tokens=8192,
            extra_body={"enable_thinking": False},
        )
        elapsed = time.time() - t0
        raw = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0

        raw_clean = raw.strip()
        if raw_clean.startswith("```"):
            raw_clean = re.sub(r"^```(?:json)?\s*", "", raw_clean)
            raw_clean = re.sub(r"\s*```$", "", raw_clean)
        if "</think>" in raw_clean:
            raw_clean = raw_clean.split("</think>")[-1].strip()
        if raw_clean.startswith("```"):
            raw_clean = re.sub(r"^```(?:json)?\s*", "", raw_clean)
            raw_clean = re.sub(r"\s*```$", "", raw_clean)

        data = json.loads(raw_clean)
        items = data.get("items") or []
        return {"page": page_idx + 1, "ok": True, "items": items,
                "supplier": data.get("supplier_name", ""),
                "elapsed": elapsed, "tokens": tokens, "error": ""}
    except Exception as e:
        return {"page": page_idx + 1, "ok": False, "items": [],
                "supplier": "", "elapsed": time.time() - t0,
                "tokens": 0, "error": str(e)}


# ── Pipelined execution ──

def run_pipeline(pdf_path: Path, doc_type: str) -> dict:
    t_start = time.time()

    print(f"  Rendering...", flush=True)
    t_render = time.time()
    images = render_pdf(pdf_path)
    n = len(images)
    print(f"  {n} pages in {time.time()-t_render:.1f}s", flush=True)
    print(f"  Pipeline: OCR({OCR_CONCURRENCY}) -> LLM({LLM_CONCURRENCY})", flush=True)

    ocr_results = [None] * n
    llm_results = [None] * n
    lock = threading.Lock()

    for i in range(n):
        llm_results[i] = {"page": i+1, "ok": True, "items": [],
                          "supplier": "", "elapsed": 0, "tokens": 0, "error": ""}

    llm_pool = ThreadPoolExecutor(max_workers=LLM_CONCURRENCY)

    def on_ocr_done(fut: Future, idx: int):
        ocr_r = fut.result()
        ocr_results[idx] = ocr_r
        with lock:
            tag = "OK" if ocr_r["ok"] else f"FAIL:{ocr_r['error'][:30]}"
            print(f"    OCR p{ocr_r['page']:>3}/{n} {ocr_r['elapsed']:>5.1f}s {ocr_r['tokens']:>5}tok {tag}", flush=True)

        if ocr_r["ok"] and ocr_r["html"].strip():
            llm_fut = llm_pool.submit(llm_parse_page, ocr_r["html"], idx, doc_type)
            llm_fut.add_done_callback(lambda f, i=idx: on_llm_done(f, i))

    def on_llm_done(fut: Future, idx: int):
        llm_r = fut.result()
        llm_results[idx] = llm_r
        with lock:
            tag = f"{len(llm_r['items'])} items" if llm_r["ok"] else f"FAIL:{llm_r['error'][:30]}"
            print(f"    LLM p{llm_r['page']:>3}/{n} {llm_r['elapsed']:>5.1f}s {llm_r['tokens']:>5}tok {tag}", flush=True)

    with ThreadPoolExecutor(max_workers=OCR_CONCURRENCY) as ocr_pool:
        for idx, img in enumerate(images):
            ocr_fut = ocr_pool.submit(ocr_page, img, idx)
            ocr_fut.add_done_callback(lambda f, i=idx: on_ocr_done(f, i))

    llm_pool.shutdown(wait=True)

    total_time = time.time() - t_start
    ocr_tokens = sum(r["tokens"] for r in ocr_results if r)
    llm_tokens = sum(r["tokens"] for r in llm_results if r)
    all_items = []
    supplier = ""
    for r in llm_results:
        if r["supplier"]:
            supplier = r["supplier"]
        for it in r["items"]:
            it["_page"] = r["page"]
            all_items.append(it)

    return {
        "items": all_items, "supplier": supplier,
        "total_time": total_time,
        "ocr_tokens": ocr_tokens, "llm_tokens": llm_tokens,
        "pages": n,
    }


# ── Output ──

def save_csv(pdf_path: Path, doc_type: str, all_items: list[dict], stats: dict):
    safe_name = pdf_path.stem[:40]
    csv_path = OUT_DIR / f"{safe_name}__two_stage.csv"
    fields = QUOTE_FIELDS if doc_type == "quote" else TENDER_FIELDS
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["#", "page"] + fields)
        for i, it in enumerate(all_items, 1):
            w.writerow([i, it.get("_page", "")] + [it.get(k, "") for k in fields])
        w.writerow([])
        w.writerow(["model", TEXT_MODEL])
        w.writerow(["ocr", "qwen-vl-ocr-latest"])
        w.writerow(["file", pdf_path.name])
        w.writerow(["items", len(all_items)])
        w.writerow(["total_time_s", f"{stats['total_time']:.1f}"])
        w.writerow(["ocr_tokens", stats["ocr_tokens"]])
        w.writerow(["llm_tokens", stats["llm_tokens"]])
    print(f"  -> CSV: {csv_path.relative_to(ROOT)}", flush=True)


# ── Main ──
print("=" * 80, flush=True)
print("Two-stage OCR+LLM pipeline (per-page parallel + pipelined)", flush=True)
print(f"OCR: qwen-vl-ocr-latest (x{OCR_CONCURRENCY}) | LLM: {TEXT_MODEL} (x{LLM_CONCURRENCY})", flush=True)
print("=" * 80, flush=True)

summary = []

for pdf in PDFS:
    if not pdf.exists():
        print(f"\n  SKIP: {pdf.name}", flush=True)
        continue

    doc_type = guess_type(pdf)
    print(f"\n{'='*80}", flush=True)
    print(f"[PDF] {pdf.name}  [{doc_type}]", flush=True)

    result = run_pipeline(pdf, doc_type)

    print(f"\n  RESULT: {len(result['items'])} items | supplier={result['supplier']}", flush=True)
    print(f"  TIME: {result['total_time']:.0f}s", flush=True)
    print(f"  TOKENS: ocr={result['ocr_tokens']} + llm={result['llm_tokens']} = {result['ocr_tokens']+result['llm_tokens']}", flush=True)

    save_csv(pdf, doc_type, result["items"], {
        "total_time": result["total_time"],
        "ocr_tokens": result["ocr_tokens"],
        "llm_tokens": result["llm_tokens"],
    })

    summary.append({
        "pdf": pdf.name, "type": doc_type, "items": len(result["items"]),
        "total_s": result["total_time"],
        "ocr_tok": result["ocr_tokens"], "llm_tok": result["llm_tokens"],
    })

print(f"\n\n{'='*80}", flush=True)
print(f"{'SUMMARY':^80}", flush=True)
print(f"{'='*80}", flush=True)
print(f"  {'PDF':<35} {'type':<7} {'items':>5} {'total':>6} {'ocr_tok':>8} {'llm_tok':>8}", flush=True)
print(f"  {'-'*72}", flush=True)
for s in summary:
    print(
        f"  {s['pdf'][:35]:<35} {s['type']:<7} {s['items']:>5} "
        f"{s['total_s']:>5.0f}s "
        f"{s['ocr_tok']:>8} {s['llm_tok']:>8}",
        flush=True,
    )
print(flush=True)
