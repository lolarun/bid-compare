"""分类筛页：整份 PDF 里只把**真的是报价清单的页**送去 Paddle（docs/design/41）。

## 为什么

Paddle 按页计费（¥0.09/页），而整份投标文件里大半是资质证书、封面、条款。
实测 7 份真实投标 PDF 共 159 页，真正贡献报价行的只有 58 页——**约 63% 的钱
花在跟比价无关的页上**（泰科龙最极端：53 页里只有 10 页有用）。

先用一个便宜得多的视觉模型逐页判"这页是不是报价清单页"，只把命中页重新拼成
PDF 送 Paddle。分类侧用小米 MiMo 订阅制（¥34/月 41 亿 token），实测泰科龙 53 页
分类耗 46,232 token ≈ **¥0.0004**，在 Paddle 的 ¥0.99 面前可以忽略。

## 这件事唯一的真实风险，以及三道防线

`.claude/rules/recognition.md`：「识别必须覆盖文档实际页数…**禁止静默截断**」。
分类判错时那一页**从头到尾没被 Paddle 看过**，报价行不是"空格子"（design/33
处理的那种，有 `AMOUNT_EMPTY` 信号），而是**压根不存在的行，没有任何下游信号**。
所以这个模块的设计全部围绕"不能静默丢页"：

1. **多轮取并集**（`FILTER_ROUNDS`）。实测同一份文档跑两轮，第一轮漏了第
   11/12/13 页、第二轮零漏——**模型有真实的 run-to-run 波动，单轮结果不可信**。
   取并集之后只有"每一轮都判否"的页才会被跳过，波动从风险变成了保护。假阳性
   （多送一页，多花 ¥0.09）和假阴性（丢掉一页报价行）代价差着量级，宁可多送。
2. **一切存疑都送**。窗口调用失败、模型漏答某页、解析不出判定行、渲染不出图
   ——任何一种情况这一页都**计入送检**，不计入跳过。判据是"只有明确判否才跳过"，
   不是"没判真就跳过"。
3. **台账必须闭合**。`PageFilterLedger` 记下 `total = sent + skipped`，逐页留下
   跳过理由；调用方把它写进 `draft.meta`。跳过哪几页、为什么跳过，事后查得到。

## 默认关闭

`classifier=None` 时 `select_pages` 直接返回"全送"，行为跟没有这个模块时逐字节
一致。生产由调用方显式注入，测试因此保持离线可复现（跟 design/33 的 `gap_filler`
同一个约定）。
"""

from __future__ import annotations

import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Sequence

log = logging.getLogger(__name__)

# 一张页图送给分类模型前压到的长边像素。分类只要看得出"这是不是一张表格、
# 表头写着数量/单价/合价"，不需要 OCR 级分辨率（生产渲染是 2x scale，长边常在
# 2000px 量级）。实测 900px 下泰科龙/金桥/徐汇三份的判定与原图一致。
CLASSIFY_LONG_EDGE = 900

# 每次调用送几页。**不是**"越多越省"：实测一次性送 53 页整批失败（模型要一次
# 输出 53 行判定，输出过长），退回单页重试反而更慢更贵。8 页是实测稳定的窗口。
WINDOW_SIZE = 8
# 相邻窗口重叠几页。报价表跨页时续页没有表头，模型需要看到前一页才判得出
# "这是延续"——重叠就是给续页判断留的上下文。
WINDOW_OVERLAP = 1
# 跑几轮取并集。见模块文档第 1 条：单轮有实测到的漏判，两轮并集消除了它。
FILTER_ROUNDS = 2
# 并发窗口数。窗口之间互相独立（重叠只在窗口**内部**提供上下文，不跨调用传状态），
# 没有任何理由串行等待。
MAX_WORKERS = 8

PROMPT = """你会看到一份工程投标文件的多张页面截图，每张图片前有一个 PAGE_<n>
标签标出它在原文档里的页码。

对每一页判断它是不是"报价清单表格页"——表格里有数量/单价/合价/型号/规格这类
列，是投标人报价的明细表；资质证书、公司简介、技术规格说明书、封面、目录、
商务/技术偏离对比表都不算。

**报价表经常跨好几页，续页不会重复表头**：如果某一页看不到"单价""合价"这类
表头文字，但版式（列数、字体、数据形状）明显是紧邻的上一个报价表页的延续
——同一类条目继续往下排、右边也是数字，只是没有列名——也要判成
is_quote_page=true，依据写"续接上一页报价表"。完全独立、看不出任何延续迹象的
页，判 false。

对**每一张**图片各输出一行，按看到的顺序，不要跳过任何一页，格式：
page,is_quote_page,依据(不超过20字)

**page 列只写数字，不要写成 PAGE_14——去掉 PAGE_ 前缀，只留数字 14。**

例（假设看到 PAGE_14 PAGE_15 PAGE_16）：
14,true,表头含数量单价合价规格列
15,true,续接上一页报价表
16,false,无表格仅文字说明
"""


@dataclass
class PageFilterLedger:
    """台账：**`total == len(sent) + len(skipped)` 必须恒成立**。

    这不是装饰性的统计——它是"禁止静默截断"这条规则在本模块的落地形式。
    跳过一页这件事必须有人能事后查到，且能查到为什么。
    """
    total_pages: int = 0
    sent: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    skip_reasons: dict[int, str] = field(default_factory=dict)
    rounds: int = 0
    errors: list[str] = field(default_factory=list)
    enabled: bool = False

    def balanced(self) -> bool:
        return self.total_pages == len(self.sent) + len(self.skipped)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "total_pages": self.total_pages,
            "sent_pages": sorted(self.sent),
            "skipped_pages": sorted(self.skipped),
            "skip_reasons": {str(k): v for k, v in sorted(self.skip_reasons.items())},
            "rounds": self.rounds,
            "errors": self.errors,
            # 台账不闭合本身就是一个必须被看见的缺陷，所以它也进 meta。
            "balanced": self.balanced(),
        }


def _downscale(png: bytes, long_edge: int = CLASSIFY_LONG_EDGE) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        w, h = img.size
        scale = long_edge / max(w, h)
        if scale >= 1:
            return png
        out = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        out.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()


def _parse(raw: str, expected: Sequence[int]) -> dict[int, tuple[bool, str]]:
    """模型回复 → `{页码: (是不是报价页, 依据)}`。

    **只收录明确解析出来的行。** 没出现在返回里的页不会被当成 false——那是
    `select_pages` 那边"存疑即送"的输入，在这里静默补成 false 就等于把防线
    拆掉了。
    """
    out: dict[int, tuple[bool, str]] = {}
    want = set(expected)
    for line in (raw or "").splitlines():
        m = re.match(r"\s*(?:PAGE_)?(\d+)\s*,\s*(true|false)\s*,\s*(.*)$", line.strip(), re.I)
        if not m:
            continue
        page = int(m.group(1))
        if page in want:
            out[page] = (m.group(2).lower() == "true", m.group(3).strip()[:40])
    return out


def _one_round(classifier, imgs: dict[int, bytes], pages: list[int],
               ledger: PageFilterLedger) -> dict[int, tuple[bool, str]]:
    windows: list[list[int]] = []
    i = 0
    while i < len(pages):
        windows.append(pages[i:i + WINDOW_SIZE])
        if i + WINDOW_SIZE >= len(pages):
            break
        i += WINDOW_SIZE - WINDOW_OVERLAP

    def _run(window: list[int]) -> dict[int, tuple[bool, str]]:
        try:
            raw = classifier([imgs[p] for p in window], PROMPT,
                             labels=[f"PAGE_{p}" for p in window])
            return _parse(raw, window)
        except Exception as exc:                                   # noqa: BLE001
            # 窗口失败 → 这一窗一个判定都拿不到 → 这些页在本轮"没有明确判否"
            # → `select_pages` 会把它们送检。失败**不会**变成跳过。
            ledger.errors.append(f"窗口 {window[0]}-{window[-1]} 调用失败：{exc}")
            log.warning("分类窗口 %s-%s 失败，这些页按存疑送检：%s", window[0], window[-1], exc)
            return {}

    merged: dict[int, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=min(len(windows), MAX_WORKERS)) as ex:
        for part in ex.map(_run, windows):
            merged.update(part)
    return merged


def select_pages(
    page_images: dict[int, bytes],
    *,
    classifier: Callable[..., str] | None,
    rounds: int = FILTER_ROUNDS,
) -> tuple[list[int], PageFilterLedger]:
    """`{页码: PNG}` → `(要送 Paddle 的页码, 台账)`。

    `classifier` 为 None 时**全送**——本模块等于不存在，行为与接入前一致。

    判据只有一条：**某一页在所有轮次里都被明确判成"不是报价页"，才跳过。**
    其余一切情况（任一轮判真、模型没答、窗口失败、解析不出）一律送检。
    """
    ledger = PageFilterLedger(total_pages=len(page_images))
    pages = sorted(page_images)
    if classifier is None or not pages:
        ledger.sent = list(pages)
        return list(pages), ledger

    ledger.enabled = True
    ledger.rounds = rounds
    try:
        small = {p: _downscale(png) for p, png in page_images.items()}
    except Exception as exc:                                       # noqa: BLE001
        # 连缩略图都做不出来 → 不冒险筛页，整份送。
        ledger.errors.append(f"缩略图渲染失败，本次不筛页：{exc}")
        ledger.sent = list(pages)
        return list(pages), ledger

    # **轮次之间也并发**（2026-08-24）。多轮取并集是为了消除 run-to-run 波动，
    # 轮与轮之间不传递任何状态——串行跑纯粹是浪费墙钟时间。实测泰科龙 53 页
    # 两轮串行 136.7s，并发后约减半。
    n_rounds = max(1, rounds)
    with ThreadPoolExecutor(max_workers=n_rounds) as ex:
        verdicts = list(ex.map(
            lambda _: _one_round(classifier, small, pages, ledger), range(n_rounds)))

    sent, skipped = [], []
    for p in pages:
        # 并集语义：任何一轮说"是"就送。全部轮次都明确说"否"才跳过。
        said_true = any(v.get(p, (False, ""))[0] for v in verdicts)
        said_false_every_round = all(p in v and not v[p][0] for v in verdicts)
        if said_true or not said_false_every_round:
            sent.append(p)
        else:
            skipped.append(p)
            reason = next((v[p][1] for v in verdicts if p in v and v[p][1]), "判定为非报价页")
            ledger.skip_reasons[p] = reason

    ledger.sent, ledger.skipped = sent, skipped
    if not ledger.balanced():                                      # pragma: no cover
        # 理论上不可能——写在这里是因为台账不闭合意味着有页凭空消失，
        # 那正是本模块存在要防的事，宁可整份送也不能带着这个状态往下走。
        ledger.errors.append("台账不闭合，保险起见整份送检")
        ledger.sent, ledger.skipped, ledger.skip_reasons = list(pages), [], {}
        return list(pages), ledger

    log.info("分类筛页：%d 页 → 送 %d 页，跳过 %d 页（%d 轮取并集）",
             ledger.total_pages, len(sent), len(skipped), rounds)
    return sent, ledger


def build_subset_pdf(file_path: str, pages: Sequence[int], out_path: str) -> str | None:
    """把选中的页抽出来拼成一份新 PDF。失败返回 None（调用方退回整份送）。"""
    try:
        import pypdf

        reader = pypdf.PdfReader(file_path)
        writer = pypdf.PdfWriter()
        for p in sorted(pages):
            if 1 <= p <= len(reader.pages):
                writer.add_page(reader.pages[p - 1])
        if not writer.pages:
            return None
        with open(out_path, "wb") as fh:
            writer.write(fh)
        return out_path
    except Exception:                                              # noqa: BLE001
        log.warning("子集 PDF 拼装失败，本次整份送", exc_info=True)
        return None


def get_production_classifier():
    """生产用分类器：`(images, prompt, labels=) -> 文本`。关掉或没配 key 时返回
    None，调用方据此整体关闭筛页——**不做能力探测后的静默降级**，筛不了就整份送。

    **两个条件，缺一不可**（2026-08-28 拆开）：`PAGE_FILTER_ENABLED` 是产品决策
    开关，`MIMO_API_KEY` 是凭据。此前只看 key，而 key 同时又是 mimo 厂商默认
    （domain_config §厂商开关）的凭据，于是"配 key 让厂商默认生效"会顺手把筛页
    也打开——把一个未决的产品取舍变成了另一件事的副作用。
    """
    import os

    from apps.api.core.domain_config import PAGE_FILTER_ENABLED

    if not PAGE_FILTER_ENABLED:
        return None
    key = (os.environ.get("MIMO_API_KEY") or "").strip()
    if not key:
        return None
    from apps.api.core.domain_config import (
        PAGE_FILTER_MODEL, PAGE_FILTER_BASE_URL, PAGE_FILTER_MAX_TOKENS,
    )
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=PAGE_FILTER_BASE_URL)

    def _call(images: list[bytes], prompt: str, *, labels: list[str] | None = None) -> str:
        import base64

        content: list[dict] = []
        for i, img in enumerate(images):
            if labels and i < len(labels):
                content.append({"type": "text", "text": labels[i]})
            b64 = base64.b64encode(img).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
        content.append({"type": "text", "text": prompt})
        resp = client.chat.completions.create(
            model=PAGE_FILTER_MODEL, messages=[{"role": "user", "content": content}],
            temperature=0.0, max_tokens=PAGE_FILTER_MAX_TOKENS,
        )
        return resp.choices[0].message.content or ""

    return _call
