"""MockProvider — deterministic stub for tests and fallback when API key absent.

Three modes:
1. Fixture mode: read JSON fixtures from `mock_responses/{schema_hash}.json`
2. Inline mode: caller injects `MockProvider(canned_response=...)`
3. Default mode: returns a minimal valid object derived from the schema's required fields

This lets the rest of the system (ingestion service, routes, frontend) work
end-to-end without consuming LLM quota.
"""

from __future__ import annotations

import json
import time
import re
from pathlib import Path
from typing import Any

from apps.api.intelligence.base import LLMProvider, ExtractionResponse


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(
        self,
        canned: dict[str, Any] | None = None,
        fixture_dir: str | Path | None = None,
        vl_csv: str | None = None,
    ):
        self.canned = canned
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.vl_csv = vl_csv

    # ─── VL-direct 能力 ──────────────────────────────────────────────────────
    #
    # 没有这个方法时，`ExtractionPipeline.extract_quote` 的 VL 分支条件
    # `hasattr(provider, "vl_extract_csv")` 恒为 False —— **所有基于 mock 的集成
    # 测试都静默落回 legacy**，哪怕 QUOTE_RECOGNIZER=vl_direct。也就是说在此之前
    # VL 分支在测试里根本走不到，而且没有任何提示。

    def vl_extract_csv(
        self, images: list[bytes], prompt: str, *,
        model: str | None = None, labels: list[str] | None = None, **_kw,
    ) -> str:
        """抽取与方向预检共用此方法，靠 `labels` 区分（与真实 provider 一致）。"""
        if labels:
            # 方向预检：从 PAGE_<n>_ROT_<deg> 标签里取出页号，一律答"不用转"。
            pages = sorted({int(m.group(1)) for l in labels
                            if (m := re.match(r"PAGE_(\d+)_ROT_", l))})
            return "\n".join(f"{p},0" for p in pages)
        # 招标文件解析有两种调用：采购清单（CSV）与封面标量（key: value）。
        # **必须按提示词区分**——它们的产出形状完全不同，混用会让招标任务拿到
        # 报价形状的数据（实测：封面标量全空，邀标流程存不进招标记录）。
        if "key: value" in prompt:
            return self._canned_tender_meta()
        is_tender = "采购清单" in prompt or "招标文件" in prompt

        if self.vl_csv is not None:
            return self.vl_csv
        if self.fixture_dir and (self.fixture_dir / "quote_vl.csv").exists():
            return (self.fixture_dir / "quote_vl.csv").read_text(encoding="utf-8")
        if is_tender:
            return self._tender_csv_from_canned(images)

        # 默认从 `self.extract()` 派生 —— **同一个数据源，两种表达**。
        #
        # 这样做的理由：报价 legacy 分支归档后，所有既有测试的 canned 数据都是喂给
        # `extract` 的。若 VL 路径另起一套合成数据，那些测试会拿到与自己声明的完全
        # 无关的行（实测：整套集成测试的断言全部落在"模拟材料0"上）。走 extract
        # 就自动尊重子类的覆盖（如按次轮换的 _CycleProvider），测试保留自己的数据、
        # 只是换了条路——这正是迁移该有的样子。
        try:
            from apps.api.intelligence.schemas import QUOTE_SCHEMA
            resp = self.extract(images, QUOTE_SCHEMA, prompt)
            items = (resp.data or {}).get("items") or []
        except Exception:                                        # noqa: BLE001
            items = []
        if items:
            return self._items_to_csv(items)

        # 连 canned 都没有：每页一行的最小合法 CSV，便于断言"页→行"的归属。
        head = "row_type,名称,规格,单位,数量,单价,合价,copy_no,page"
        body = [f"detail,模拟材料{i},SPEC-{i},米,{i + 1},10.00,{(i + 1) * 10:.2f},1,{i + 1}"
                for i in range(len(images))]
        return "\n".join([head, *body])

    def _tender_data(self) -> dict:
        """招标 canned 数据。与报价同理，走 self.extract 以尊重子类覆盖。"""
        try:
            from apps.api.intelligence.schemas import TENDER_SCHEMA
            return (self.extract([], TENDER_SCHEMA, "").data or {})
        except Exception:                                        # noqa: BLE001
            return {}

    def _canned_tender_meta(self) -> str:
        """封面标量 → `key: value` 逐行。取自与清单**同一份** canned 数据。"""
        d = self._tender_data()
        return "\n".join(
            f"{k}: {d.get(k) or ''}"
            for k in ("project_name", "project_code", "tender_date", "deadline")
        )

    def _tender_csv_from_canned(self, images: list[bytes]) -> str:
        """招标采购清单 CSV。序号按顺序生成——**它是行轴**，canned 数据里没有就得有。"""
        items = self._tender_data().get("items") or []
        if not items:
            head = "row_type,序号,项目名称,规格,计量单位,数量,page"
            return "\n".join([head] + [
                f"detail,{i + 1},模拟材料{i},SPEC-{i},个,{i + 1},{i + 1}"
                for i in range(len(images))])
        head = "row_type,序号,项目名称,规格,计量单位,数量,备注,page"

        def cell(v) -> str:
            return "" if v is None else str(v).replace(",", "、")

        return "\n".join([head] + [
            ",".join(["detail", str(i + 1), cell(it.get("name")), cell(it.get("spec")),
                      cell(it.get("unit")), cell(it.get("quantity")),
                      cell(it.get("remark")), "1"])
            for i, it in enumerate(items)])

    @staticmethod
    def _items_to_csv(items: list[dict]) -> str:
        """canned item dict → 报价 CSV。**空值留空，绝不补算**（与真实提示词第 2 条一致）。"""
        head = "row_type,名称,规格,品牌,单位,数量,单价,合价,copy_no,page"

        def cell(v) -> str:
            if v is None:
                return ""
            s = str(v)
            # CSV 字段内的逗号会撑破列数，进而触发列错位门 —— 换成顿号而不是加引号，
            # 因为这里只是测试数据，不值得为它引入引号转义的解析分支。
            return s.replace(",", "、")

        rows = [
            ",".join([
                "detail", cell(it.get("material")), cell(it.get("spec")),
                cell(it.get("brand")), cell(it.get("unit")), cell(it.get("qty")),
                cell(it.get("unit_price")), cell(it.get("total_price")),
                # design/24 B0：此前硬编码 "1"，canned 数据里的 copy_no 从未被读过——
                # 想测"多副本"场景（如 test_copy_dedup.py）时数据从下游看永远只有一份，
                # 门根本测不到。default 仍是 "1"，不设置 copy_no 的既有测试行为不变。
                cell(it.get("copy_no")) or "1", str(i + 1),
            ])
            for i, it in enumerate(items)
        ]
        return "\n".join([head, *rows])

    def extract_doc_meta(self, meta_htmls: list[str]) -> dict:
        return {"supplier_name": None, "bid_total": None,
                "bid_total_basis": "unknown", "tax_rate": None}

    def classify_pages_visual(self, thumbnails: list[bytes], doc_type: str, **_kw):
        """Stub: every page = table_header（保留旧 mock 行为：所有页都是目标页）。"""
        role = "tender_table_header" if doc_type == "tender" else "quote_table_header"
        out = [{"page": i + 1, "role": role, "confidence": 1.0,
                "contains_table": True, "orientation": 0,
                "continues_from_page": None, "mixed_content": False,
                "evidence": ["mock"], "source": "flash"}
               for i in range(len(thumbnails))]
        return out, []

    def review_pages_visual(self, page_image, neighbor_thumbs, flash_result, page_no, **_kw):
        return dict(flash_result)

    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
        page_html: str | None = None,
        table_grids=None,  # accepted but ignored — mock always uses canned data
    ) -> ExtractionResponse:
        t0 = time.time()
        if self.canned is not None:
            data = self.canned
        elif self.fixture_dir:
            data = self._load_fixture(schema)
        else:
            data = self._minimal_from_schema(schema)
        return ExtractionResponse(
            data=data,
            raw_text=json.dumps(data, ensure_ascii=False),
            confidence=1.0,
            tokens_used=0,
            provider=self.name,
            duration_ms=int((time.time() - t0) * 1000),
            metadata={"image_count": len(images)},
        )

    # ─── helpers ───────────────────────────────────────────────────────────
    def _load_fixture(self, schema: dict[str, Any]) -> dict[str, Any]:
        # Determine fixture by checking which top-level required key is present
        required = schema.get("required") or []
        kind = "tender" if "project_name" in required else "quote"
        fixture_path = self.fixture_dir / f"{kind}.json" if self.fixture_dir else None
        if fixture_path and fixture_path.is_file():
            return json.loads(fixture_path.read_text(encoding="utf-8"))
        return self._minimal_from_schema(schema)

    @staticmethod
    def _minimal_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """Return the simplest object that satisfies `required` top-level fields."""
        result: dict[str, Any] = {}
        for field in schema.get("required", []):
            spec = (schema.get("properties") or {}).get(field, {})
            t = spec.get("type")
            if t == "string":
                result[field] = ""
            elif t == "array":
                result[field] = []
            elif t == "object":
                result[field] = {}
            elif t in ("number", "integer"):
                result[field] = 0
            else:
                result[field] = None
        # Sensible defaults for the two schemas we care about
        if "items" in result and isinstance(result["items"], list):
            result.setdefault("items", [])
        return result
