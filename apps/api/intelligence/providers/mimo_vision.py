"""小米 MiMo 视觉 provider（docs/design/41）——跟 `DashScopeOCRProvider` 的视觉
方法同签名，可以整体替换。

## 为什么要单独写一个类，而不是给现有 provider 换个 base_url

`DashScopeOCRProvider` 的视觉调用走的是 **dashscope 私有 SDK**
（`dashscope.MultiModalConversation.call`，消息体是 `{"image": ...}` /
`{"text": ...}` 这种自有形状），mimo 是 **OpenAI 兼容**（`chat.completions`，
`{"type":"image_url"}`）。协议不同，换 base_url 换不过去，必须另起一个实现。

## 只实现真正在生产里被调用的两个方法

- `vl_extract_csv(images, prompt, *, model=, labels=, temperature=)`
  —— 空格子补位（design/33）、招标 VL-direct 回落都用它；
- `classify_document_kind(page_images, *, model=)`
  —— 扫描件招标/投标判定（design/29 §3 Tier 1.5）。

`extract()`（两阶段 OCR→LLM 那条 legacy 路径）**故意不实现**：报价识别在
design/26 P4 之后唯一的真实引擎是 Paddle，那条路生产上已经不可达。留一个抛
`NotImplementedError` 的桩，好过实现一个没人验证过、将来被误当成"能用"的方法。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time

log = logging.getLogger(__name__)


class MimoVisionProvider:
    """OpenAI 兼容的视觉调用。构造时不发请求，没配 key 由工厂函数拦在外面。"""

    name = "mimo_vision"

    def __init__(self, api_key: str, base_url: str, model: str, max_tokens: int = 4000):
        from openai import OpenAI

        from apps.api.core.domain_config import LLM_MAX_RETRIES

        # 重试次数显式声明，不吃 openai SDK 的默认值（2）：这一侧是
        # VISION_CLIENT_VENDOR 的默认路径，而 dashscope 那侧有手写的 5 次退避——
        # 不写出来，就没人看得出默认路径的重试反而更弱。SDK 自带的退避覆盖
        # 超时/连接错误/408/409/429/5xx，够用，不需要在这里再套一圈手写循环。
        self._client = OpenAI(api_key=api_key, base_url=base_url,
                              max_retries=LLM_MAX_RETRIES)
        self._model = model
        self._max_tokens = max_tokens

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _call(self, images: list[bytes], prompt: str, *,
              labels: list[str] | None = None, temperature: float = 0.0,
              model: str | None = None) -> str:
        content: list[dict] = []
        for i, img in enumerate(images):
            # 标签跟图交错插入——`dashscope_ocr.vl_extract_csv` 的注释记着
            # "不交错模型会串页"，那条教训对这边同样成立，不是 dashscope 专属。
            if labels and i < len(labels):
                content.append({"type": "text", "text": labels[i]})
            b64 = base64.b64encode(img).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
        content.append({"type": "text", "text": prompt})
        # 之前这里没配超时：openai SDK 默认等 600s，一次挂起的调用能悄无声息拖
        # 到后台孤儿任务清扫线（30 分钟）都不报错，日志上看跟"根本没在跑"一样。
        # 显式超时 + 前后各一行日志，让"卡住"和"慢但活着"从日志上能分清。
        from apps.api.core.domain_config import MIMO_VISION_TIMEOUT_S

        started = time.monotonic()
        try:
            resp = self._client.chat.completions.create(
                model=model or self._model,
                messages=[{"role": "user", "content": content}],
                temperature=temperature, max_tokens=self._max_tokens,
                timeout=MIMO_VISION_TIMEOUT_S,
            )
        except Exception:
            log.warning("mimo 视觉调用失败，耗时 %.1fs（超时=%ss）",
                        time.monotonic() - started, MIMO_VISION_TIMEOUT_S,
                        exc_info=True)
            raise
        log.info("mimo 视觉调用完成，耗时 %.1fs", time.monotonic() - started)
        return resp.choices[0].message.content or ""

    @staticmethod
    def _clean_json_text(raw: str) -> str:
        """跟 `DashScopeOCRProvider._clean_json_text` 同一套清洗（去 markdown
        围栏、去 `<think>`）。**有意复制而不是 import**：那是另一个 provider 的
        私有静态方法，跨 provider 依赖它等于把两家的实现焊在一起，将来任一边
        改动都会牵连另一边。逻辑很短，各自持有更清晰。"""
        clean = (raw or "").strip()
        if "</think>" in clean:
            clean = clean.split("</think>")[-1].strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        return clean.strip()

    # ── 生产接口 ────────────────────────────────────────────────────────────

    def vl_extract_csv(self, images: list[bytes], prompt: str, *,
                       model: str | None = None, labels: list[str] | None = None,
                       temperature: float = 0.0, **_kw) -> str:
        """整份/多张页图 → 模型原始文本。**不解析、不清洗**，原样返回给调用方
        （跟 dashscope 那版同一个约定：解析规则属于识别器，不属于 provider）。

        `**_kw` 吞掉 `max_pixels`/`row_progress_cb` 这些 dashscope 专属参数——
        调用方是共用的，不该为了换 provider 去改每个调用点。"""
        return self._call(images, prompt, labels=labels, temperature=temperature,
                          model=model)

    def classify_document_kind(self, page_images: list[bytes], *,
                               model: str | None = None) -> dict:
        """扫描件招标/投标判定。返回
        `{"doc_type": "tender"|"bid"|"uncertain", "project_name_hint", "supplier_name_hint", "evidence"}`。

        **判不出来就答 uncertain，不抛异常**——跟 dashscope 那版、跟 design/28
        Tier 0/1 同一条"失败不拖垮主线"约定。"""
        from apps.api.intelligence.providers.dashscope_ocr import (
            _CLASSIFY_DOC_KIND_PROMPT,
        )

        try:
            raw = self._call(page_images, _CLASSIFY_DOC_KIND_PROMPT, model=model)
            data = json.loads(self._clean_json_text(raw))
            doc_type = data.get("doc_type")
            if doc_type not in ("tender", "bid", "uncertain"):
                doc_type = "uncertain"
            return {
                "doc_type": doc_type,
                "project_name_hint": str(data.get("project_name_hint") or ""),
                "supplier_name_hint": str(data.get("supplier_name_hint") or ""),
                "evidence": data.get("evidence") or [],
            }
        except Exception:                                          # noqa: BLE001
            log.warning("mimo 扫描件类型判定失败，答 uncertain", exc_info=True)
            return {"doc_type": "uncertain", "project_name_hint": "",
                    "supplier_name_hint": "", "evidence": []}

    def extract(self, *_a, **_kw):                                 # pragma: no cover
        raise NotImplementedError(
            "MimoVisionProvider 不实现两阶段 OCR→LLM 抽取——报价识别在 design/26 P4 "
            "之后唯一的真实引擎是 Paddle，那条 legacy 路径生产上不可达。"
            "需要它说明调用方走错了路。")


def get_mimo_vision_provider() -> MimoVisionProvider | None:
    """没配 `MIMO_API_KEY` 时返回 None，调用方据此回落 dashscope——
    **不做能力探测后的静默降级**，回落必须记日志。"""
    import os

    key = (os.environ.get("MIMO_API_KEY") or "").strip()
    if not key:
        return None
    from apps.api.core.domain_config import (
        PAGE_FILTER_BASE_URL,
        PAGE_FILTER_MAX_TOKENS,
        PAGE_FILTER_MODEL,
    )
    return MimoVisionProvider(key, PAGE_FILTER_BASE_URL, PAGE_FILTER_MODEL,
                              max_tokens=PAGE_FILTER_MAX_TOKENS)
