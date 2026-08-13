"""try_paddleocr_vl.py — 百度智能云"文档解析（PaddleOCR-VL）"API 跑投标 PDF，按 golden 打分。

跟 tests/test_baidu_unlimited_ocr_standalone.py 是同一套鉴权/轮询模式（同一个
Baidu AI 应用的 APP_ID/API_KEY/SECRET_KEY，读同一个 apps/api/.env），换了个
产品端点：unlimited-ocr-parser → paddle-vl-parser。

开启 merge_tables=True（跨页表格合并）和 recognize_seal=True（投标文件封面常见
红章）。

打分只做**序号级召回率**（golden 的 89/136 个序号里，这次识别覆盖了几个），
不做逐字段（单价/规格/品牌文字）比对——那需要把百度返回的表格列对齐到
golden 的字段名，不同供应商表格列序不一样，本轮没做这层通用映射，如实标注
为未验证，不能拿序号对上就说"全对"。

只读 fixture PDF，只写 outputs/baidu_paddleocr_vl/（已 gitignore），不碰生产代码、
不落库。纯粹一次性试跑脚本，不是正式接入。

用法：
    python scripts/try_paddleocr_vl.py --doc kaishuo         # 单份
    python scripts/try_paddleocr_vl.py --all --jobs 7        # 七份并行
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 gbk，中文输出会乱码

REPO = Path(__file__).resolve().parent.parent
ENV_PATH = REPO / "apps" / "api" / ".env"
OUT_DIR = REPO / "outputs" / "baidu_paddleocr_vl"
PRJ1 = REPO / "docs/test1/prj1"
TEST = REPO / "docs/test"

# 跟 scripts/vl_prod_e2e.py 的 DOCS 保持同一套七份文档，方便和生产识别器的结果对照。
DOCS = {
    "kaishuo": (TEST / "凯硕新正投标文件.pdf", REPO / "data/golden/quote_kaishuo.json"),
    "taikelong": (TEST / "泰科龙投标文件.pdf", REPO / "data/golden/quote_taikelong.json"),
    "miancun": (TEST / "上海绵存投标文件.pdf", REPO / "data/golden/quote_miancun.json"),
    "pudong": (PRJ1 / "徐汇区华泾镇综合机电分包工程投标文件-上海浦东.pdf",
               REPO / "data/golden/quote_cable_pudong.json"),
    "hengtong": (PRJ1 / "徐汇区华泾镇综合机电分包工程投标文件-亨通.pdf",
                 REPO / "data/golden/quote_cable_hengtong.json"),
    "hongsheng": (PRJ1 / "徐汇区华泾镇综合机电分包工程投标文件-宏胜.pdf",
                  REPO / "data/golden/quote_cable_hongsheng.json"),
    "yuandong": (PRJ1 / "徐汇区华泾镇综合机电分包工程投标文件-远东.pdf",
                 REPO / "data/golden/quote_cable_yuandong.json"),
    "jinqiao": (TEST / "金桥地体上盖招标文件.pdf", None),  # 招标文件，无 quote golden
}
SEVEN_QUOTE_DOCS = ["kaishuo", "taikelong", "miancun", "pudong", "hengtong", "hongsheng", "yuandong"]

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
TASK_URL = "https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task"
QUERY_URL = f"{TASK_URL}/query"

_TAG_RE = re.compile(r"<[^>]+>")


def _read_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    return values


def _post_json(url: str, form: dict[str, str], timeout: int = 120) -> dict:
    req = Request(url, data=urlencode(form).encode("utf-8"),
                  headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Baidu API request failed: {exc}") from exc


def _download_text(url: str, timeout: int = 120) -> str:
    with urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _access_token(api_key: str, secret_key: str) -> str:
    result = _post_json(TOKEN_URL, {
        "grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key,
    })
    token = str(result.get("access_token") or "")
    if not token:
        raise RuntimeError(f"无法获取 access_token：{result}")
    return token


def _first_cell_seqs(markdown: str | None) -> set[int]:
    """从表格 markdown 文本里取每行第一列，统计能解析成正整数序号的行数。

    早期版本读的是 `matrix[row][col]`——踩坑了：那个字段存的是 cells[] 数组的
    索引，不是文字本身，会产出超过 100% 的假信号。改读 `markdown` 字段（跟旧的
    unlimited-ocr-parser 脚本同一套解析方式）。
    """
    seqs: set[int] = set()
    if not markdown:
        return seqs
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        stripped = line.replace("|", "").replace("-", "").replace(" ", "")
        if not stripped:
            continue  # 分隔行 "| --- | --- |"
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        first = html.unescape(_TAG_RE.sub("", cells[0])).replace("\xa0", " ").strip()
        if first.isdigit() and int(first) > 0:
            seqs.add(int(first))
    return seqs


def run_one(doc_key: str, *, api_key: str, secret_key: str,
            merge_tables: bool = True, recognize_seal: bool = True) -> dict:
    pdf_path, golden_path = DOCS[doc_key]
    if not pdf_path.exists():
        return {"doc": doc_key, "error": f"找不到文件：{pdf_path}"}

    golden_row_count = None
    golden_seqs: set[int] = set()
    if golden_path and golden_path.exists():
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        golden_row_count = golden.get("row_count")
        golden_seqs = {int(r["seq"]) for r in golden["rows"] if str(r.get("seq", "")).isdigit()}

    t0 = time.time()
    try:
        token = _access_token(api_key, secret_key)
        submitted = _post_json(f"{TASK_URL}?access_token={token}", {
            "file_data": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
            "file_name": pdf_path.name,
            "merge_tables": "true" if merge_tables else "false",
            "recognize_seal": "true" if recognize_seal else "false",
        })
        if submitted.get("error_code") != 0:
            return {"doc": doc_key, "error": f"提交失败：{submitted}",
                    "seconds": round(time.time() - t0, 1)}
        task_id = str((submitted.get("result") or {}).get("task_id") or "")
        if not task_id:
            return {"doc": doc_key, "error": f"没拿到 task_id：{submitted}",
                    "seconds": round(time.time() - t0, 1)}

        deadline = time.monotonic() + 900
        result: dict = {}
        while time.monotonic() < deadline:
            queried = _post_json(f"{QUERY_URL}?access_token={token}", {"task_id": task_id})
            if queried.get("error_code") != 0:
                return {"doc": doc_key, "error": f"查询失败：{queried}",
                        "seconds": round(time.time() - t0, 1)}
            result = queried.get("result") or {}
            status = result.get("status")
            if status == "success":
                break
            if status == "failed":
                return {"doc": doc_key, "error": f"任务失败：{result.get('task_error')}",
                        "seconds": round(time.time() - t0, 1)}
            time.sleep(5)
        else:
            return {"doc": doc_key, "error": f"超时（900s），task_id={task_id}",
                    "seconds": round(time.time() - t0, 1)}

        duration = round(time.time() - t0, 1)
        markdown_url = str(result.get("markdown_url") or "")
        parse_result_url = str(result.get("parse_result_url") or "")
        if not markdown_url or not parse_result_url:
            return {"doc": doc_key, "error": f"结果缺 URL：{result}", "seconds": duration}

        markdown = _download_text(markdown_url)
        parsed = json.loads(_download_text(parse_result_url))
    except Exception as exc:  # noqa: BLE001
        return {"doc": doc_key, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 1)}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{doc_key}.md").write_text(markdown, encoding="utf-8")
    (OUT_DIR / f"{doc_key}.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=1), encoding="utf-8")

    pages = parsed.get("pages") or []
    all_tables = []
    merge_markers = 0
    for p in pages:
        for t in (p.get("tables") or []):
            all_tables.append(t)
            if t.get("merge_table"):
                merge_markers += 1

    all_seqs: set[int] = set()
    for t in all_tables:
        all_seqs |= _first_cell_seqs(t.get("markdown"))

    missing = sorted(golden_seqs - all_seqs) if golden_seqs else []
    extra = sorted(all_seqs - golden_seqs) if golden_seqs else []

    analysis = {
        "doc": doc_key, "duration_seconds": duration, "task_id": task_id,
        "page_count": len(pages), "table_count": len(all_tables),
        "cross_page_merge_markers": merge_markers,
        "distinct_seq_count": len(all_seqs),
        "golden_row_count": golden_row_count,
        "seq_recall": (round(len(all_seqs & golden_seqs) / len(golden_seqs), 4)
                      if golden_seqs else None),
        "missing_seqs": missing,   # golden 有、这次没找到的序号——真正的漏行
        "extra_seqs": extra,       # 这次多出来的、golden 没有的序号——多半是别的表格串进来了
    }
    (OUT_DIR / f"{doc_key}.analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
    return analysis


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", choices=list(DOCS))
    ap.add_argument("--all", action="store_true", help="跑 SEVEN_QUOTE_DOCS 全部七份（并行）")
    ap.add_argument("--jobs", type=int, default=7)
    ap.add_argument("--merge-tables", action="store_true", default=True)
    ap.add_argument("--no-merge-tables", dest="merge_tables", action="store_false")
    ap.add_argument("--recognize-seal", action="store_true", default=True)
    a = ap.parse_args()

    if not a.doc and not a.all:
        a.doc = "kaishuo"

    env = _read_local_env()
    api_key = env.get("BAIDU_UNLIMITED_OCR_API_KEY", "")
    secret_key = env.get("BAIDU_UNLIMITED_OCR_SECRET_KEY", "")
    if not api_key or not secret_key:
        print("apps/api/.env 里没找到 BAIDU_UNLIMITED_OCR_API_KEY / _SECRET_KEY")
        return 1

    doc_keys = SEVEN_QUOTE_DOCS if a.all else [a.doc]
    print(f"跑 {len(doc_keys)} 份：{doc_keys} · merge_tables={a.merge_tables} · "
          f"recognize_seal={a.recognize_seal}")

    with ThreadPoolExecutor(max_workers=min(a.jobs, len(doc_keys))) as pool:
        results = list(pool.map(
            lambda k: run_one(k, api_key=api_key, secret_key=secret_key,
                              merge_tables=a.merge_tables, recognize_seal=a.recognize_seal),
            doc_keys,
        ))

    print(f"\n{'文档':12s} {'耗时(s)':>8s} {'页数':>5s} {'表格':>5s} {'跨页合并':>7s} "
          f"{'序号召回':>9s} {'漏':>4s} {'多':>4s}")
    ok = True
    for r in results:
        if "error" in r:
            ok = False
            print(f"{r['doc']:12s}  失败：{r['error']}")
            continue
        recall_txt = f"{r['seq_recall']:.1%}" if r.get("seq_recall") is not None else "—"
        print(f"{r['doc']:12s} {r['duration_seconds']:>8.1f} {r['page_count']:>5d} "
              f"{r['table_count']:>5d} {r['cross_page_merge_markers']:>7d} "
              f"{recall_txt:>9s} {len(r.get('missing_seqs', [])):>4d} {len(r.get('extra_seqs', [])):>4d}")

    manifest_path = OUT_DIR / "manifest_batch.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        "merge_tables": a.merge_tables, "recognize_seal": a.recognize_seal,
        "docs": doc_keys, "runs": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物 → {OUT_DIR}/（各文档 .md/.json/.analysis.json）+ manifest_batch.json")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
