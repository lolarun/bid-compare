"""snapshot_provider.py — 确定性 OCR/LLM 快照层（阶段二回归基座）。

包裹真实 provider，把每次 OCR 和 LLM 调用按输入内容哈希缓存到磁盘。
- record 模式：调用真实 provider 并落盘快照。
- replay 模式：只从快照读，缺失即报错（保证测试不偷偷打真实 API）。

用途（CLAUDE.md §13 / 用户阶段二要求）：
  真实 OCR HTML/JSON 快照 → TableGrid/tiling/dedup/质量门 → ExtractionDraft → diff
确定性回归测试从快照输入跑，不受在线模型随机性影响；PR 必跑。
在线稳定性测试用真实 provider 连跑多轮另算。

键设计：
- OCR：sha256(image_bytes) → html（分类由 classify_page(html) 重建，无需缓存）
- LLM：sha256(enable_thinking | prompt | input) → {data, raw, tok}

只缓存 ocr_pages_with_roles 和 _llm_call_json 两个 recognize_tables 真正消费的方法。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SnapshotProvider:
    """OCR/LLM 快照包裹器。mode ∈ {'record', 'replay'}。

    replay 模式下 inner=None 是合法的——所有调用均从快照读取，
    不透传任何真实 API。record 模式需要真实 inner。
    """

    def __init__(self, inner: Any | None, snapshot_path: str | Path, mode: str = "record"):
        if mode not in ("record", "replay"):
            raise ValueError(f"mode must be record|replay, got {mode}")
        if mode == "record" and inner is None:
            raise ValueError("record 模式需要真实 inner provider")
        self.inner = inner
        self.path = Path(snapshot_path)
        self.mode = mode
        self._ocr: dict[str, str] = {}        # img_hash → html
        self._llm: dict[str, dict] = {}        # prompt_hash → {data, raw, tok}
        self._meta: dict[str, dict] = {}       # html_hash → extract_doc_meta result
        self._visual: dict[str, Any] = {}      # model+ver+thumbs_hash → classify/review result
        self._failures: dict[str, dict] = {}   # img_hash → {pdf_page, error}
        self._ocr_misses = 0
        self._llm_misses = 0
        self._meta_misses = 0
        self._visual_misses = 0
        if mode == "replay":
            self._load()
        elif self.path.exists():
            # record 续写：载入已有，避免重复打 API
            self._load()

    # ── 持久化 ────────────────────────────────────────────────────────────
    def _load(self):
        if not self.path.exists():
            if self.mode == "replay":
                raise FileNotFoundError(f"replay 快照不存在: {self.path}")
            return
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return  # empty file (fresh temp snapshot) — nothing to load
        d = json.loads(raw)
        self._ocr = d.get("ocr", {})
        self._llm = d.get("llm", {})
        self._meta = d.get("meta", {})
        self._visual = d.get("visual", {})
        self._failures = d.get("failures", {})
        log.info("snapshot loaded: %d ocr, %d llm, %d meta, %d visual from %s",
                 len(self._ocr), len(self._llm), len(self._meta), len(self._visual),
                 self.path.name)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({
                "ocr": self._ocr, "llm": self._llm,
                "meta": self._meta, "visual": self._visual,
                "failures": self._failures,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("snapshot saved: %d ocr, %d llm, %d meta, %d visual, %d failures → %s",
                 len(self._ocr), len(self._llm), len(self._meta), len(self._visual),
                 len(self._failures), self.path.name)

    @property
    def stats(self) -> dict:
        return {
            "ocr_entries": len(self._ocr), "llm_entries": len(self._llm),
            "meta_entries": len(self._meta), "visual_entries": len(self._visual),
            "ocr_misses": self._ocr_misses, "llm_misses": self._llm_misses,
            "meta_misses": self._meta_misses, "visual_misses": self._visual_misses,
            "ocr_failures": len(self._failures),
        }

    # ── 被包裹的三个方法 ─────────────────────────────────────────────────

    def ocr_pages_with_roles(self, images: list[bytes]):
        from apps.api.intelligence.page_classifier import classify_page, PageClassification, PageRole

        # Track (original_index, image_bytes) for pages not in cache
        miss_indexed = [(i, img) for i, img in enumerate(images) if _h(img) not in self._ocr]
        if miss_indexed:
            if self.mode != "record":
                raise KeyError(
                    f"OCR 快照缺失 {len(miss_indexed)} 张图（replay 模式不打真实 API）"
                )
            miss_imgs = [img for _, img in miss_indexed]
            self._ocr_misses += len(miss_imgs)
            res, failures = self.inner.ocr_pages_with_roles(miss_imgs)
            for (orig_idx, img), (_cls, html) in zip(miss_indexed, res):
                h = _h(img)
                self._ocr[h] = html
                if not html:
                    # subset index within miss_imgs (for matching provider failure records)
                    subset_idx = next(j for j, (oi, _) in enumerate(miss_indexed) if oi == orig_idx)
                    fail_info = next(
                        (f for f in failures if f.get("page") == subset_idx + 1),
                        {"error": "unknown"},
                    )
                    # Save with original 1-based PDF page number
                    self._failures[h] = {**fail_info, "pdf_page": orig_idx + 1}

        out = []
        replay_failures = []
        for img in images:
            h = _h(img)
            html = self._ocr[h]
            if not html and h in self._failures:
                # OCR failure: restore empty HTML + failure record (same as real provider)
                out.append((PageClassification(primary_role=PageRole.UNKNOWN), ""))
                replay_failures.append(self._failures[h])
            else:
                out.append((classify_page(html), html))
        return out, replay_failures

    def _llm_call_json(self, prompt: str, content: str, enable_thinking: bool = False):
        key = _h(f"{int(enable_thinking)}\x00{prompt}\x00{content}".encode("utf-8"))
        if key not in self._llm:
            if self.mode != "record":
                raise KeyError("LLM 快照缺失（replay 模式不打真实 API）")
            self._llm_misses += 1
            data, raw, tok = self.inner._llm_call_json(
                prompt, content, enable_thinking=enable_thinking
            )
            self._llm[key] = {"data": data, "raw": raw, "tok": tok}
        e = self._llm[key]
        return e["data"], e["raw"], e["tok"]

    def extract_doc_meta(self, meta_htmls: list[str]) -> dict:
        """Cached wrapper around inner.extract_doc_meta (LLM call for meta extraction)."""
        key = _h("\x00".join(meta_htmls).encode("utf-8"))
        if key not in self._meta:
            if self.mode != "record":
                raise KeyError("extract_doc_meta 快照缺失（replay 模式不打真实 API）")
            self._meta_misses += 1
            result = self.inner.extract_doc_meta(meta_htmls)
            self._meta[key] = result
        return self._meta[key]

    def classify_pages_visual(self, thumbnails: list[bytes], doc_type: str, *,
                              model=None, prompt_version: str = "v2",
                              batch_size: int = 10, overlap: int = 1,
                              temperature: float = 0.0, max_pixels: int = 2_000_000,
                              file_path: str | None = None):
        """Cached visual page classification.

        Primary key = model+prompt_version+doc_type+batch_size+overlap+temperature
                      +max_pixels + per-thumbnail SHA256 (thumbnail content may
                      vary slightly between renders due to PDF engine non-determinism).
        Stable fallback key = same params + PDF-file-content SHA256 + page count
                              (invariant for the same PDF file).

        On record: stores result under BOTH keys so replay can find it either way.
        On replay: tries primary key first; if miss and file_path provided, tries stable key.
        """
        mdl = model or "qwen3-vl-flash"
        thumb_hashes = "\x00".join(_h(t) for t in thumbnails)
        param_base = (f"classify\x00{mdl}\x00{prompt_version}\x00{doc_type}\x00"
                      f"{batch_size}\x00{overlap}\x00{temperature}\x00{max_pixels}")
        primary_key = _h(f"{param_base}\x00{thumb_hashes}".encode("utf-8"))

        stable_key = None
        if file_path:
            pdf_bytes = Path(file_path).read_bytes()
            pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()[:32]
            stable_key = _h(f"{param_base}\x00pdf_stable\x00{pdf_hash}\x00{len(thumbnails)}"
                            .encode("utf-8"))

        # Lookup: primary first, then stable fallback
        for lookup_key in filter(None, [primary_key, stable_key]):
            if lookup_key in self._visual:
                return self._visual[lookup_key]["result"], self._visual[lookup_key].get("failures", [])

        if self.mode != "record":
            raise KeyError("classify_pages_visual 快照缺失（replay 模式不打真实 API）")
        self._visual_misses += 1
        result, failures = self.inner.classify_pages_visual(
            thumbnails, doc_type, model=mdl, prompt_version=prompt_version,
            batch_size=batch_size, overlap=overlap,
            temperature=temperature, max_pixels=max_pixels)
        entry = {"result": result, "failures": failures}
        self._visual[primary_key] = entry
        if stable_key:
            self._visual[stable_key] = entry
        return result, failures

    def review_pages_visual(self, page_image: bytes, neighbor_thumbs: list[bytes],
                            flash_result: dict, page_no: int, *,
                            chain_context: list | None = None,
                            model=None):
        mdl = model or "qwen3-vl-plus"
        nb = "\x00".join(_h(t) for t in neighbor_thumbs)
        flash_str = json.dumps(flash_result, ensure_ascii=False, sort_keys=True)
        # chain_context 非空时用新版 key（review2），保持对旧无-chain 条目向后兼容
        if chain_context:
            chain_str = json.dumps(chain_context, ensure_ascii=False, sort_keys=True)
            key = _h(f"review2\x00{mdl}\x00{page_no}\x00{_h(page_image)}\x00{nb}\x00"
                     f"{flash_str}\x00{chain_str}".encode("utf-8"))
        else:
            key = _h(f"review\x00{mdl}\x00{page_no}\x00{_h(page_image)}\x00{nb}\x00"
                     f"{flash_str}".encode("utf-8"))
        if key not in self._visual:
            if self.mode != "record":
                raise KeyError("review_pages_visual 快照缺失（replay 模式不打真实 API）")
            self._visual_misses += 1
            self._visual[key] = self.inner.review_pages_visual(
                page_image, neighbor_thumbs, flash_result, page_no,
                chain_context=chain_context, model=mdl)
        return self._visual[key]

    # ── replay 模式不透传任何属性到 inner ────────────────────────────────
    def __getattr__(self, name):
        _guarded = ("inner", "path", "mode",
                    "_ocr", "_llm", "_meta", "_visual", "_failures",
                    "_ocr_misses", "_llm_misses", "_meta_misses", "_visual_misses")
        if name in _guarded:
            raise AttributeError(name)
        inner = self.__dict__.get("inner")
        if inner is not None and hasattr(inner, name):
            return getattr(inner, name)
        raise AttributeError(name)
