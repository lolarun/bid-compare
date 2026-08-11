"""Standalone live test for Baidu Unlimited-OCR.

This test is intentionally isolated from the application's OCR providers and
existing test suite.  It submits each PDF as a single document, downloads both
the Markdown and JSON outputs, saves them under ``outputs/baidu_unlimited_ocr``
(gitignored), and produces row/sequence diagnostics for the expected 89-line list.

Run explicitly:
    $env:RUN_BAIDU_UNLIMITED_OCR_E2E='1'
    python -m unittest tests.test_baidu_unlimited_ocr_standalone -v
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO = Path(__file__).resolve().parents[1]
ENV_PATH = REPO / "apps" / "api" / ".env"
OUTPUT_DIR = REPO / "outputs" / "baidu_unlimited_ocr"
DOCS = REPO / "docs" / "test"
DOCUMENTS = [
    ("tender_jinqiao", DOCS / "金桥地体上盖招标文件.pdf", 18),
    ("quote_miancun", DOCS / "上海绵存投标文件.pdf", 31),
    ("quote_taikelong", DOCS / "泰科龙投标文件.pdf", 53),
    ("quote_kaishuo", DOCS / "凯硕新正投标文件.pdf", 19),
]
TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
TASK_URL = "https://aip.baidubce.com/rest/2.0/brain/online/v2/unlimited-ocr-parser/task"
QUERY_URL = f"{TASK_URL}/query"
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _read_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _post_json(url: str, form: dict[str, str], timeout: int = 120) -> dict[str, Any]:
    request = Request(
        url,
        data=urlencode(form).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Baidu API request failed: {exc}") from exc


def _download_text(url: str, timeout: int = 120) -> str:
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Baidu result download failed: {exc}") from exc


def _markdown_sequences(text: str) -> set[int]:
    seqs: set[int] = set()
    for row in _TR_RE.findall(text):
        cells = _CELL_RE.findall(row)
        if not cells:
            continue
        first = html.unescape(_TAG_RE.sub("", cells[0])).replace("\xa0", " ").strip()
        if first.isdigit() and int(first) > 0:
            seqs.add(int(first))
    return seqs


def _json_sequences(value: Any) -> set[int]:
    """Extract sequence values from structured fields and embedded HTML strings."""
    seqs: set[int] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"seq", "sequence", "serial_no", "serial_number"}:
                candidate = str(item).strip()
                if candidate.isdigit() and int(candidate) > 0:
                    seqs.add(int(candidate))
            seqs.update(_json_sequences(item))
    elif isinstance(value, list):
        for item in value:
            seqs.update(_json_sequences(item))
    elif isinstance(value, str):
        seqs.update(_markdown_sequences(value))
    return seqs


def _price_table_rows(markdown: str) -> int:
    """Count row-oriented quote rows when the source table has no 序号 column."""
    count = 0
    for table in re.findall(r"<table\b[^>]*>(.*?)</table\s*>", markdown, re.IGNORECASE | re.DOTALL):
        for row in _TR_RE.findall(table):
            cells = [html.unescape(_TAG_RE.sub("", cell)).replace("\xa0", " ").strip()
                     for cell in _CELL_RE.findall(row)]
            numeric_cells = 0
            for cell in cells:
                try:
                    float(cell.replace(",", "").replace("%", ""))
                    numeric_cells += 1
                except ValueError:
                    pass
            if (len(cells) >= 6 and numeric_cells >= 2
                    and not any("合计" in cell or "小计" in cell for cell in cells)):
                count += 1
    return count


def _json_page_markdown(parsed_json: Any) -> str:
    """Use each page's Markdown and avoid double-counting JSON chunks."""
    if not isinstance(parsed_json, dict) or not isinstance(parsed_json.get("pages"), list):
        return ""
    return "\n".join(
        str(page.get("markdown") or "") for page in parsed_json["pages"]
        if isinstance(page, dict)
    )


class TestBaiduUnlimitedOcrStandalone(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.getenv("RUN_BAIDU_UNLIMITED_OCR_E2E") != "1":
            raise unittest.SkipTest("Set RUN_BAIDU_UNLIMITED_OCR_E2E=1 to upload real PDFs")
        env = _read_local_env()
        cls.api_key = env.get("BAIDU_UNLIMITED_OCR_API_KEY", "")
        cls.secret_key = env.get("BAIDU_UNLIMITED_OCR_SECRET_KEY", "")
        if not cls.api_key or not cls.secret_key:
            raise unittest.SkipTest("Baidu Unlimited-OCR credentials are missing from apps/api/.env")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def _access_token(self) -> str:
        result = _post_json(TOKEN_URL, {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        })
        token = str(result.get("access_token") or "")
        if not token:
            self.fail(f"Could not obtain Baidu access token: {result}")
        return token

    def _parse_document(self, pdf: Path) -> tuple[dict[str, Any], str, Any]:
        token = self._access_token()
        submitted = _post_json(f"{TASK_URL}?access_token={token}", {
            "file_data": base64.b64encode(pdf.read_bytes()).decode("ascii"),
            "file_name": pdf.name,
        })
        self.assertEqual(submitted.get("error_code"), 0, submitted)
        task_id = str((submitted.get("result") or {}).get("task_id") or "")
        self.assertTrue(task_id, submitted)

        deadline = time.monotonic() + 900
        result: dict[str, Any] = {}
        while time.monotonic() < deadline:
            queried = _post_json(f"{QUERY_URL}?access_token={token}", {"task_id": task_id})
            self.assertEqual(queried.get("error_code"), 0, queried)
            result = queried.get("result") or {}
            if result.get("status") == "success":
                break
            if result.get("status") == "failed":
                self.fail(f"Baidu task failed: {result.get('task_error')}")
            time.sleep(5)
        else:
            self.fail(f"Baidu task timed out: {task_id}")

        markdown_url = str(result.get("markdown_url") or "")
        json_url = str(result.get("parse_result_url") or "")
        self.assertTrue(markdown_url, result)
        self.assertTrue(json_url, f"JSON result URL was not returned: {result}")
        markdown = _download_text(markdown_url)
        json_text = _download_text(json_url)
        return result, markdown, json.loads(json_text)

    def test_four_e2e_pdfs_download_markdown_and_json(self) -> None:
        """Submit full PDFs and save a comparable MD/JSON diagnostic for each."""
        for name, pdf, pages in DOCUMENTS:
            with self.subTest(document=name):
                self.assertTrue(pdf.exists(), f"Fixture missing: {pdf}")
                started = time.monotonic()
                result, markdown, parsed_json = self._parse_document(pdf)
                duration = time.monotonic() - started

                (OUTPUT_DIR / f"{name}.md").write_text(markdown, encoding="utf-8")
                (OUTPUT_DIR / f"{name}.json").write_text(
                    json.dumps(parsed_json, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                md_seqs = _markdown_sequences(markdown)
                json_seqs = _json_sequences(parsed_json)
                json_markdown = _json_page_markdown(parsed_json)
                analysis = {
                    "document": name,
                    "expected_pdf_pages": pages,
                    "expected_material_rows": 89,
                    "duration_seconds": round(duration, 1),
                    "markdown_sequence_count": len(md_seqs),
                    "markdown_price_table_row_count": _price_table_rows(markdown),
                    "json_sequence_count": len(json_seqs),
                    "json_price_table_row_count": _price_table_rows(json_markdown),
                }
                (OUTPUT_DIR / f"{name}.analysis.json").write_text(
                    json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                print(
                    f"{name}: pages={pages}, duration={duration:.1f}s, task={result.get('task_id')}, "
                    f"markdown seq={len(md_seqs)}, price_rows={analysis['markdown_price_table_row_count']}; "
                    f"json seq={len(json_seqs)}, price_rows={analysis['json_price_table_row_count']}"
                )
                self.assertTrue(markdown.strip(), "Markdown result is empty")
                self.assertTrue(json_markdown.strip(), "JSON result contains no page Markdown")
