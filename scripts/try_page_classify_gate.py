"""try_page_classify_gate.py — 用 qwen 逐页分类 + 概述，验证"分类后只送清单页给
Paddle"这条降本思路站不站得住（HANDOFF「Paddle 页面分类降本实验」一节）。

## 这个脚本要回答的问题

不是"能不能省钱"（算账已经算过，理论上能省 60%），是**"qwen 判'这页是不是报价
清单页'准不准"**。这条路唯一的真实风险是：判错时，那一页从头到尾没被 Paddle
看过，报价行**静默消失、没有任何下游信号**——不是空格子（design/33 处理的那种），
是压根不存在的行，跟仓库里所有识别规则一直守的「禁止静默截断」正对冲。所以这
个脚本先测准确率，不动生产代码，测完再决定值不值得写成正式功能。

## 三步管线（对应"渲染缩略图→qwen分类→Paddle解析"）

1. **渲染**：复用 `DocumentLoader.render_pages`，跟生产识别用的是同一套 pdfium
   调用，不是另起一套。分类不需要 OCR 级分辨率，渲染后额外降采样一版（长边压到
   `_CLASSIFY_LONG_EDGE`），顺带验证"分类用低分辨率图能不能再省一笔"这个还没
   验证过的猜想。
2. **qwen 分类 + 概述**：复用 `DashScopeOCRProvider`（跟生产报价识别、方向预检
   同一个 provider 类，不重新写一遍鉴权/重试/多 key 轮换），逐页问"这是不是报价
   清单表格页"+ 一句话概述，要求模型说明依据（表头关键词/看到的列），不是只吐
   一个 yes/no——概述本身也是这次要验证的"顺手拿到摘要"能不能用。
3. **Paddle 解析（`--reparse`，默认不跑，因为真花钱）**：把 qwen 判定为"是"的页
   抽出来重新拼一份 PDF（`pypdf.PdfWriter`），提交给真实 Paddle，跟"整份原样送
   Paddle"这个已知基线（`SUPPLIER_SNAPSHOT_PAGE_COUNTS`/`recognize_snapshot`）比
   行数和合价合计，验证"分类完再解析"这条链路本身产出的数字对不对得上。

## 判据：跟已知的"贡献行的页"逐页比对

`GOLDEN_CONTRIBUTING_PAGES` 是 2026-08-23 从真实识别产物量出来的（每份文档
`source_ref.page`/`page_end` 落在哪几页，见 HANDOFF"降本测量"一节），不是猜的
标注——它本身是 Paddle 已经看过整份文档之后的真实产出，拿它当分类器的参照系
站得住。**假阴性（该留的页被判成不是）比假阳性更危险**，报告里单独算。

## 成本与用法

只跑分类（默认）：全部 7 份约 159 页，qwen-vl-plus 单页约 ¥0.003，全跑约 ¥0.5。
    python scripts/try_page_classify_gate.py --doc taikelong      # 单份，先小范围试
    python scripts/try_page_classify_gate.py --all                # 全部 7 份

加 `--reparse`：额外把命中页重新送一次真实 Paddle，按命中页数 ¥0.09/页 计费
（如果分类器准，命中页数远小于总页数，这笔钱比整份送小很多；如果分类器判得
离谱，这笔钱可能失控——先看 `--classify-only` 的准确率报告，觉得能接受再加
`--reparse`）：
    python scripts/try_page_classify_gate.py --doc taikelong --reparse

## 换供应商：`--provider ernie`（2026-08-24 补）

qwen 那条路在泰科龙上撞见了两个问题（都记在 HANDOFF）：逐页独立发送会漏判续页
（金桥招标文件实测 recall 0.2）；改成整份一次性送能修好续页问题，但泰科龙第
6/9 页触发内容安全审查（`DataInspectionFailed`，大概率是红章），**一页拖累整份
53 页全炸**。

千帆平台的 `ernie-4.5-turbo-vl-32k` 是候选替代（OpenAI 兼容，价格公开：输入
¥0.8/输出¥3.2 每百万 token，比 qwen-vl-plus 略贵但同一量级）。千帆的入门文档
提到"单次请求最多 10 张图"，但这个模型实际调用的 API 参考页没有重复这个数字
——**没有更权威的来源确认，`ERNIE_MAX_IMAGES_PER_CALL=10` 是保守默认值，不是
已核实的硬指标**。不管真实上限是多少，分窗口送本身就是更稳的做法：窗口越小，
红章页拖累的范围越小，顺带也部分解决了 qwen 那次"一页拖累整批"的爆炸半径问题
（`classify_doc_windowed` 的 `window_size`/`overlap` 参数）。

需要一把新凭证：千帆 ModelBuilder 控制台生成的 API Key（`bce-v3/ALTAK-...`
格式），写进 `apps/api/.env` 的 `QIANFAN_API_KEY=`——**跟 Paddle 现在用的
`BAIDU_UNLIMITED_OCR_*` 不是同一套**，那是老式 access_token 认证，这个是新的
Bearer token，需要用户自己去控制台生成，这份脚本不会替你造一个。

    python scripts/try_page_classify_gate.py --doc jinqiao_tender --provider ernie
    python scripts/try_page_classify_gate.py --doc taikelong --provider ernie

只读 `tests/fixtures/documents/` 里的真实投标 PDF，只写
`outputs/page_classify_gate/`（已 gitignore），不碰生产代码、不落库、不改任何
`apps/api` 下的文件。纯粹一次性验证脚本，不是正式接入——如果测出来准确率够，
下一步是把它写成 design 文档再讨论要不要接生产（跟 design/33 走的是同一个流程：
先测、写文档、用户拍板、再实现）。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DOCS = REPO / "tests" / "fixtures" / "documents"
OUT_DIR = REPO / "outputs" / "page_classify_gate"

# 分类阶段的缩略图长边上限——OCR 级渲染是 2x scale（长边常在 2000px 量级），
# 分类只要看得出"这是不是一张表格、表头写着数量/单价/合价"，不需要那个分辨率。
# 这个数字**没有验证过对不对**，是本脚本要顺带测的东西之一：报告里会同时打出
# 原图和降采样图各自的分类结果，看降采样会不会开始把表格页判错。
_CLASSIFY_LONG_EDGE = 900

# 2026-08-23 从真实识别产物量出来的"贡献行的页"（source_ref.page ~ page_end 的
# 并集），见 HANDOFF"降本测量"一节。这是分类器要对照的真值——不是人工标注，是
# Paddle 已经看过整份文档之后的真实产出。
#
# 两份招标文件（xuhui_tender/jinqiao_tender）的真值是**这次新查的**，判据不同：
# 招标文件本来就不进 Paddle 账单（原生文字层，走 `tender_text_layer` 直抽，零
# 成本），所以没有 Paddle 识别产物可以拿来当真值。改用免费的文字层扫描
# （pdfplumber 逐页找"序号+数量"表头）先确定真值，再让 qwen 视觉分类去对——
# 这两份测的**不是**"生产会不会真的送它们去分类"（不会，它们本来就免费），
# 是拿来当分类器的**对照样本**：
#   - `xuhui_tender`：全篇零命中，印证了之前查过的"B1 招标 PDF 无采购清单"——
#     一份**该判全否**的负样本，测分类器会不会到处看出表格来。
#   - `jinqiao_tender`：第 14-18 页原来就嵌着一份完整的 89 项空白清单（单价/
#     合价全 0.00，序号 1-89——跟 `金桥地体上盖项目-采购清单.xlsx` 是同一份
#     数据，只是一份嵌在 PDF 里）。第 12 页是"附件五：合同固定模板"、第 13 页
#     是清单前的说明页，都不算——测分类器能不能在 18 页里精确挑出这 5 页，
#     不被其它表格（合同模板等）带偏。
GOLDEN_CONTRIBUTING_PAGES: dict[str, set[int]] = {
    "taikelong": set(range(5, 15)),                              # 5-14
    "miancun":   set(range(4, 10)),                               # 4-9
    "kaishuo":   set(range(4, 8)),                                # 4-7
    # 2026-08-24 修正：原来写作 `set(range(3,17)) | {18}`，把第 18 页也算进真值。
    # 那是错的——真值本身是从"Paddle 识别产物落在哪几页"反推的，而 Paddle 把
    # 第 18 页那张**商务偏离对比表**也当表格抽了进来（实测该页 3 行全是
    # `采购文件 / 条目 简要内容 / 投标有效期 招标文件中未明确`，数量金额全 None）。
    # 那 3 行本来就是脏数据，不是报价明细，这一页不该算"贡献行的页"。
    # mimo 把它判否、理由写"表格为商务偏离对比"，**判对了**——是真值错怪了它。
    "yuandong":  set(range(3, 17)),                               # 3-16
    "pudong":    set(range(2, 9)),                                # 2-8
    "hengtong":  set(range(2, 11)),                               # 2-10
    "hongsheng": set(range(3, 10)),                               # 3-9
    "xuhui_tender":   set(),                                      # 空——负样本
    "jinqiao_tender": set(range(14, 19)),                         # 14-18
}

DOC_FILES: dict[str, str] = {
    "taikelong": "金桥地体上盖项目-泰科龙投标文件.pdf",
    "miancun":   "金桥地体上盖项目-上海绵存投标文件.pdf",
    "kaishuo":   "金桥地体上盖项目-凯硕新正投标文件.pdf",
    "yuandong":  "徐汇区华泾镇项目-远东投标文件.pdf",
    "pudong":    "徐汇区华泾镇项目-上海浦东投标文件.pdf",
    "hengtong":  "徐汇区华泾镇项目-亨通投标文件.pdf",
    "hongsheng": "徐汇区华泾镇项目-宏胜投标文件.pdf",
    "xuhui_tender":   "徐汇区华泾镇项目-招标文件.pdf",
    "jinqiao_tender": "金桥地体上盖项目-招标文件.pdf",
}

# 这两份不参与 --reparse 的端到端验证：`reparse_with_paddle` 的基线来自
# `test_scenarios_e2e.recognize_snapshot`（报价快照），招标文件没有对应快照
# ——它们本来就不走 Paddle，没有"整份送 Paddle"的基线可比。--reparse 时会
# 直接跳过这两份并说明原因，不是漏掉。
NO_REPARSE_BASELINE = {"xuhui_tender", "jinqiao_tender"}

PROMPT_CLASSIFY = """这是一份工程投标文件里的一页扫描件。判断这一页是不是"报价
清单表格页"——表格里有数量/单价/合价/型号/规格这类列，是投标人报价的明细表；
资质证书、公司简介、技术规格说明书、封面、目录都不算。

按下面的格式回答，只回答这一行，不要别的文字：
is_quote_page,依据(不超过20字),一句话概述这一页内容

例：
true,表头含数量单价合价规格列,阀门报价明细表第1-10行
false,无表格仅文字说明,投标人资质证书扫描件
"""

# 2026-08-24 改：逐页独立发送在金桥招标文件上实测 recall 只有 0.2——第 14 页有
# 表头，判对；第 15-18 页是续页、没有重复表头，模型单独看一页看不出那几列数字
# 是"单价""合价"，四页全部漏判。根因是**每次请求只给一张图，页与页互相看不到**，
# 不是 qwen 判断力不够。改成整份一次性送，页前插 `PAGE_<n>` 标签——跟仓库里
# 方向预检已经在用的约定同一套（`dashscope_ocr.py` 的 `labels` 参数，"不交错
# 标签会串页"是它自己的教训），不是发明新用法。提示词里显式教它认续页：
# "跟前一页同一张表的延续也算"，逼它做跨页推理而不是每页各自为战。
PROMPT_CLASSIFY_BATCH = """你会看到一份工程投标文件的多张页面截图，每张图片前
有一个 PAGE_<n> 标签标出它在原文档里的页码（不连续也可能，缺的页是渲染失败，
不用管）。

对每一页判断它是不是"报价清单表格页"——表格里有数量/单价/合价/型号/规格这类
列，是投标人报价的明细表；资质证书、公司简介、技术规格说明书、封面、目录都
不算。

**报价表经常跨好几页，续页不会重复表头**：如果某一页看不到"单价""合价"这类
表头文字，但版式（列数、字体、数据形状）明显是紧邻的上一个报价表页的延续
——同一类条目继续往下排、右边也是数字，只是没有列名——也要判成
is_quote_page=true，依据写"续接上一页报价表"。不确定是不是续页时，看它跟最近
一次判为 true 的页在结构上像不像；完全独立、看不出任何延续迹象的页，判 false。

对**每一张**图片各输出一行，按看到的顺序，不要跳过任何一页，格式：
page,is_quote_page,依据(不超过20字),一句话概述

**page 列只写数字，不要写成 PAGE_14——去掉 PAGE_ 前缀，只留数字 14。**

例（假设看到 PAGE_14 PAGE_15 PAGE_16）：
14,true,表头含数量单价合价规格列,阀门报价明细表第1-10行
15,true,续接上一页报价表,同一张表续行11-20无表头
16,false,无表格仅文字说明,投标人资质证书扫描件
"""


@dataclass
class PageVerdict:
    page: int
    is_quote_page: bool
    reason: str
    summary: str
    raw: str
    error: str = ""


@dataclass
class DocReport:
    slug: str
    total_pages: int
    golden: set[int]
    verdicts: list[PageVerdict] = field(default_factory=list)

    @property
    def predicted(self) -> set[int]:
        return {v.page for v in self.verdicts if v.is_quote_page}

    @property
    def false_negatives(self) -> set[int]:
        """**最危险的一类**：真正贡献报价行的页，被判成"不是"——这一页会被
        整页跳过，行数据静默消失，且没有任何下游信号能发现。"""
        return self.golden - self.predicted

    @property
    def false_positives(self) -> set[int]:
        """判成"是"但其实没有贡献行的页——只是浪费一次 Paddle 调用，不丢数据，
        危险程度跟假阴性不是一个量级，但要如实报告，不能因为"不危险"就不提。"""
        return self.predicted - self.golden

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "total_pages": self.total_pages,
            "golden_pages": sorted(self.golden), "predicted_pages": sorted(self.predicted),
            "false_negatives": sorted(self.false_negatives),
            "false_positives": sorted(self.false_positives),
            "recall": round(1 - len(self.false_negatives) / len(self.golden), 4) if self.golden else None,
            "precision": (round(len(self.golden & self.predicted) / len(self.predicted), 4)
                         if self.predicted else None),
            "would_send_to_paddle": len(self.predicted),
            "would_send_baseline": self.total_pages,
            "pages": [
                {"page": v.page, "predicted": v.is_quote_page, "in_golden": v.page in self.golden,
                 "reason": v.reason, "summary": v.summary, "error": v.error, "raw": v.raw}
                for v in sorted(self.verdicts, key=lambda x: x.page)
            ],
        }


def _downscale(png: bytes, long_edge: int) -> bytes:
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


def _parse_verdict(page: int, raw: str) -> PageVerdict:
    line = (raw or "").strip().splitlines()[0] if raw and raw.strip() else ""
    m = re.match(r"\s*(true|false)\s*,\s*([^,]*)\s*,\s*(.*)$", line, re.I)
    if not m:
        return PageVerdict(page=page, is_quote_page=False, reason="", summary="",
                           raw=raw, error=f"解析失败，模型原始回复：{raw!r}")
    return PageVerdict(page=page, is_quote_page=m.group(1).lower() == "true",
                       reason=m.group(2).strip(), summary=m.group(3).strip(), raw=raw)


def _classify_one(provider, page: int, png: bytes) -> PageVerdict:
    try:
        raw = provider.vl_extract_csv([png], PROMPT_CLASSIFY, temperature=0.0)
        return _parse_verdict(page, raw)
    except Exception as exc:                                       # noqa: BLE001
        return PageVerdict(page=page, is_quote_page=False, reason="", summary="",
                           raw="", error=f"模型调用异常：{exc}")


def _render_all(slug: str, *, downscale: bool) -> tuple[int, dict[int, bytes]]:
    from apps.api.intelligence.document_loader import DocumentLoader

    path = DOCS / DOC_FILES[slug]
    if not path.exists():
        raise SystemExit(f"缺夹具：{path}")
    n = DocumentLoader.get_page_count(str(path))
    rendered = DocumentLoader.render_pages(str(path), list(range(1, n + 1)))
    imgs = {p: (_downscale(png, _CLASSIFY_LONG_EDGE) if downscale else png)
           for p, png in rendered.items()}
    return n, imgs


def classify_doc_per_page(slug: str, *, downscale: bool, jobs: int) -> DocReport:
    """**保留下来做对照，不是当前默认路径。** 2026-08-24 实测：金桥招标文件
    18 页里的续页（15-18）全部漏判，recall 只有 0.2——根因见 `PROMPT_CLASSIFY_BATCH`
    上面那段注释：逐页独立发送时页与页互相看不到，续页没有表头自证身份。
    `classify_doc` 是修完之后的默认路径；这个函数留着，是为了让"改了之后到底
    有没有真的变好"这句话有旧结果可以对比，不是死代码。
    """
    n, imgs = _render_all(slug, downscale=downscale)
    provider = _make_provider()
    report = DocReport(slug=slug, total_pages=n, golden=GOLDEN_CONTRIBUTING_PAGES.get(slug, set()))
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_classify_one, provider, p, img): p for p, img in imgs.items()}
        for fut in as_completed(futs):
            report.verdicts.append(fut.result())
    return report


def _parse_batch_verdicts(raw: str, expected_pages: list[int]) -> list[PageVerdict]:
    """整份响应里逐行抠出 `page,is_quote_page,依据,概述`。

    **模型漏 answer 某一页，必须报出来，不能悄悄当 false 处理**——这份脚本存在
    的全部意义就是"分类错了不能没有信号"，解析器自己先犯这个错就本末倒置了。

    2026-08-24 修①：漏答页原来的 error 文案写着"整份响应见 DocReport 之外的
    日志"——**是空话，那份原始响应从没被存下来过**，金桥招标文件用 ernie 复测
    时撞见页 12/13/14 一起漏答，想查原因才发现这个诊断信息本身是断的。现在把
    整份原始响应带在每一条漏答记录的 `raw` 里，不管这一页有没有匹配上都能
    回头看模型到底说了什么，不用为了诊断一次失败再花一次钱重新调用。

    2026-08-24 修②：加了修①之后才看清真正的根因——ernie 有时会把提示词里
    插的 `PAGE_14` 标签原样回显在页码列（"PAGE_14,true,..."），不是提示词要求
    的纯数字（"14,true,..."）。旧正则只认纯数字开头，这类行整行被跳过——泰科龙
    这份文档窗口 2（页 8-15）整窗都是这种格式，9-14 六页因此全部解析失败，
    14 页两头窗口都没有重叠、彻底漏判。这是**输出格式没遵守指令**，不是模型
    看不懂内容（同一份原始响应里，reason/summary 字段的判断本身是对的）——
    解析器理应容忍自己塞进提示词的同一个标签被原样带回来，不该假设模型
    100% 照抄示例格式。
    """
    seen: dict[int, PageVerdict] = {}
    for line in (raw or "").strip().splitlines():
        m = re.match(r"\s*(?:page[_\s]*)?(\d+)\s*,\s*(true|false)\s*,\s*([^,]*)\s*,\s*(.*)$",
                     line.strip(), re.I)
        if not m:
            continue
        p = int(m.group(1))
        seen[p] = PageVerdict(page=p, is_quote_page=m.group(2).lower() == "true",
                              reason=m.group(3).strip(), summary=m.group(4).strip(), raw=line)
    out = []
    for p in expected_pages:
        if p in seen:
            out.append(seen[p])
        else:
            out.append(PageVerdict(
                page=p, is_quote_page=False, reason="", summary="", raw=raw or "",
                error="模型响应里没有这一页的判定行——完整响应文本见本条的 raw 字段"))
    return out


def classify_doc(slug: str, *, downscale: bool, jobs: int = 1) -> DocReport:
    """整份一次性送给 qwen，每页前插 `PAGE_<n>` 标签，让模型能做跨页推理
    （2026-08-24 改，见 `PROMPT_CLASSIFY_BATCH` 上方的根因说明）。

    `jobs` 参数保留只是为了跟 `classify_doc_per_page` 同一个调用签名，整份送
    本来就是一次调用，不需要并发。
    """
    n, imgs = _render_all(slug, downscale=downscale)
    provider = _make_provider()
    pages = sorted(imgs)
    labels = [f"PAGE_{p}" for p in pages]
    images = [imgs[p] for p in pages]

    report = DocReport(slug=slug, total_pages=n, golden=GOLDEN_CONTRIBUTING_PAGES.get(slug, set()))
    try:
        raw = provider.vl_extract_csv(images, PROMPT_CLASSIFY_BATCH, labels=labels, temperature=0.0)
        report.verdicts = _parse_batch_verdicts(raw, pages)
    except Exception as exc:                                       # noqa: BLE001
        report.verdicts = [PageVerdict(page=p, is_quote_page=False, reason="", summary="",
                                       raw="", error=f"模型调用异常：{exc}") for p in pages]
    return report


def _make_provider():
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.core.config import get_settings

    s = get_settings()
    if not (s.DASHSCOPE_API_KEY or getattr(s, "DASHSCOPE_API_KEYS", "")):
        raise SystemExit(
            "没有配置 DASHSCOPE_API_KEY/DASHSCOPE_API_KEYS（读 apps/api/.env）——"
            "这个脚本要真的调用 qwen，不是离线可跑的单元测试。")
    return DashScopeOCRProvider()


# ── 千帆 / ernie-4.5-turbo-vl（2026-08-24，qwen 撞见整份送炸批问题之后的候选）──

ERNIE_MODEL = "ernie-4.5-turbo-vl-32k"
# "最多 10 张图"来自千帆的入门介绍页（fm8r1ndsm），**不是**这个模型实际调用的
# API 参考页（rm7u7qdiq）——后者只写了单图大小/URL 长度限制，没有重复这个数字，
# 两份文档不完全一致。这里按保守值定死，不是"已核实的硬指标"：真实上限可能
# 更高也可能因模型而异，超限时服务端会直接报错，`classify_doc_windowed` 的
# try/except 接得住（那一窗标 error，不会让整个脚本崩），不是拿这个数字当
# 生死线。
ERNIE_MAX_IMAGES_PER_CALL = 10
ERNIE_BASE_URL = "https://qianfan.baidubce.com/v2"


class QianfanVLProvider:
    """千帆 v2 是 OpenAI 兼容接口，`content` 数组是标准 OpenAI 视觉消息格式
    （`{"type":"image_url","image_url":{"url":...}}`），跟 `DashScopeOCRProvider`
    的私有 `_img_part`/`_mm_call` 不是一回事，不能直接复用那个类——但对外暴露
    的方法签名故意跟 `vl_extract_csv` 保持一致（`images, prompt, labels=,
    temperature=`），这样 `_classify_one`/`_parse_batch_verdicts` 这些解析逻辑
    两边通用，不用为了换供应商另写一套。
    """

    def __init__(self, api_key: str):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=ERNIE_BASE_URL)

    def vl_extract_csv(self, images: list[bytes], prompt: str, *,
                       labels: list[str] | None = None, temperature: float = 0.0,
                       **_kw) -> str:
        import base64
        content: list[dict] = []
        for i, img in enumerate(images):
            if labels and i < len(labels):
                content.append({"type": "text", "text": labels[i]})
            b64 = base64.b64encode(img).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
        content.append({"type": "text", "text": prompt})
        # 官方 API 参考页（rm7u7qdiq）没写 max_tokens 的默认值是多少——一个窗口
        # 最多要出 10 行判定文本，默认值如果偏小会把响应从中间截断，那样
        # `_parse_batch_verdicts` 会把被截掉的那几页当成"模型没回答"处理（有
        # 信号，不是静默丢——但没必要放着这个隐患不管）。给够余量：每行最长
        # 也就四五十字，10 行内文按中文 token 密度算远不到 2000。
        resp = self._client.chat.completions.create(
            model=ERNIE_MODEL, messages=[{"role": "user", "content": content}],
            temperature=temperature, max_tokens=2000,
        )
        return resp.choices[0].message.content or ""


def _make_ernie_provider() -> QianfanVLProvider:
    import os
    from dotenv import load_dotenv

    load_dotenv(REPO / "apps" / "api" / ".env")
    key = (os.environ.get("QIANFAN_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "没有配置 QIANFAN_API_KEY（读 apps/api/.env）——去千帆 ModelBuilder "
            "控制台生成一把新的 API Key（bce-v3/ALTAK-... 格式），写入 "
            "apps/api/.env 的 QIANFAN_API_KEY= 这一行。**这不是** Paddle 已经在用的 "
            "BAIDU_UNLIMITED_OCR_* 那套凭证——两边是不同的认证体系，不能混用。")
    return QianfanVLProvider(key)


# ── gemini-3.6-flash 经第三方网关（2026-08-24，qwen/ernie 都被内容审查绊住之后）──
#
# **注意来源**：`packyapi.com` 是第三方中转网关，不是 Google 官方 Gemini 接口。
# 这意味着测出来的"会不会被内容审查拒"反映的是**这个中转商的策略**，不一定
# 等于官方 Gemini 的行为——结论必须带着这个限定，不能写成"Gemini 不拦红章"。
#
# 最小连通性测试（2026-08-24 实测）已确认：视觉调用可用，且**泰科龙第 5 页
# 那张带红章的图没有被拒**——qwen 在这份文档上是 HTTP 400 硬拦截、ernie 是
# 软拒答，这是第一个两样都没发生的候选。
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_BASE_URL = "https://www.packyapi.com/v1"
# 这个模型**会先推理再回答**：实测一次简单的看图提问，596 个输出 token 里
# 565 个是 reasoning token，正式答案只占 31 个。`max_tokens` 给小了会把答案
# 整个截掉（实测 max_tokens=200 时只返回了"这是一张包含"半句就断了，不是
# 模型不会答）。一个窗口要出 8-10 行判定，推理开销还要叠加，所以给得比
# ernie（2000）宽得多。
GEMINI_MAX_TOKENS = 8000


class GeminiVLProvider:
    """OpenAI 兼容接口，`content` 数组形状跟 `QianfanVLProvider` 完全一样——
    对外方法签名同样对齐 `vl_extract_csv`，所以 `classify_doc_windowed` /
    `_classify_one` / `_parse_batch_verdicts` 这些逻辑一行都不用改。
    """

    def __init__(self, api_key: str, model: str = GEMINI_MODEL,
                 max_tokens: int = GEMINI_MAX_TOKENS):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)
        self._model = model
        self._max_tokens = max_tokens

    def vl_extract_csv(self, images: list[bytes], prompt: str, *,
                       labels: list[str] | None = None, temperature: float = 0.0,
                       **_kw) -> str:
        import base64
        content: list[dict] = []
        for i, img in enumerate(images):
            if labels and i < len(labels):
                content.append({"type": "text", "text": labels[i]})
            b64 = base64.b64encode(img).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
        content.append({"type": "text", "text": prompt})
        resp = self._client.chat.completions.create(
            model=self._model, messages=[{"role": "user", "content": content}],
            temperature=temperature, max_tokens=self._max_tokens,
        )
        return resp.choices[0].message.content or ""


# 同一个网关上的其它候选模型（2026-08-24）。gemini-3.6-flash 实测准确率满分但
# 单价 $5.25/$26.25，折算每页分类 ¥0.074，跟 Paddle 精细解析整页的 ¥0.09 是同一
# 量级——**筛页省下的钱被分类本身吃掉了**，省钱前提不成立。所以要试更便宜的：
#   grok-4.6      $0.20/$0.60  —— 协议对上了但网关侧 0/4 全 503（可用率仅 54.9%）
#   mimo-v2.5     小米，OpenAI 协议，实测红章页不拒、零推理 token
PACKY_MODELS: dict[str, int] = {
    # 模型名 → max_tokens。会先推理再回答的模型要给足余量，否则答案被整个截断。
    "gemini-3.6-flash": 8000,
    "mimo-v2.5": 4000,          # 实测 reasoning_tokens=0，不需要 gemini 那么大的余量
    "gemini-2.5-flash": 8000,
}


def _make_gemini_provider() -> GeminiVLProvider:
    import os
    from dotenv import load_dotenv

    load_dotenv(REPO / "apps" / "api" / ".env")
    key = (os.environ.get("PACKY_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "没有配置 PACKY_API_KEY——写进 apps/api/.env 的 PACKY_API_KEY= 这一行，"
            "或者临时用环境变量传：PACKY_API_KEY=sk-... python scripts/...。"
            "这是第三方中转网关（packyapi.com）的 key，不是 Google 官方 Gemini 凭证。")
    return GeminiVLProvider(key)


# 小米 MiMo **直连官方**（2026-08-24）。走网关（packyapi）时成本算不准——网关有
# 折扣（截图里 grok 标 0.1折、gemini-2.5-flash 标 4.3折）和令牌分组倍率（账单里
# 显示 x7），这些参数拿不全，拿 gemini 真实账单校准出的公式去预测别的模型全部
# 对不上（差 2.3~70 倍，已自查作废）。直连官方端点按官方价计费，成本才可算。
MIMO_DIRECT_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"


class MimoDirectProvider(GeminiVLProvider):
    """跟 `GeminiVLProvider` 只差一个 base_url——OpenAI 兼容协议完全一致，
    所以直接继承，不复制一遍 content 组装逻辑。"""

    def __init__(self, api_key: str, model: str = "mimo-v2.5", max_tokens: int = 4000):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=MIMO_DIRECT_BASE_URL)
        self._model = model
        self._max_tokens = max_tokens


def _make_mimo_direct_provider(model: str = "mimo-v2.5") -> MimoDirectProvider:
    import os
    from dotenv import load_dotenv

    load_dotenv(REPO / "apps" / "api" / ".env")
    key = (os.environ.get("MIMO_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "没有配置 MIMO_API_KEY——写进 apps/api/.env 的 MIMO_API_KEY= 这一行，"
            "或临时用环境变量传：MIMO_API_KEY=tp-... python scripts/...。"
            "这是小米 MiMo 官方端点的 key，不是 packyapi 网关那把。")
    return MimoDirectProvider(key, model=model)


def _make_packy_provider(model: str) -> GeminiVLProvider:
    """同一个网关、指定模型。`_make_gemini_provider` 是它的 gemini-3.6-flash 特例。"""
    import os
    from dotenv import load_dotenv

    load_dotenv(REPO / "apps" / "api" / ".env")
    key = (os.environ.get("PACKY_API_KEY") or "").strip()
    if not key:
        raise SystemExit("没有配置 PACKY_API_KEY——见 _make_gemini_provider 的说明。")
    return GeminiVLProvider(key, model=model, max_tokens=PACKY_MODELS.get(model, 8000))


def classify_doc_windowed(slug: str, *, provider, downscale: bool = True,
                          window_size: int = 8, overlap: int = 1,
                          max_workers: int = 8) -> DocReport:
    """**分窗口**送，不是整份一次送——`ERNIE_MAX_IMAGES_PER_CALL` 见
    该常量上方的说明（保守默认值，来源不完全权威），53 页的泰科龙不管真实上限
    是多少都不该塞进一次请求（图片体积本身也大，分窗口是更稳的做法）。

    **`provider` 由调用方传入，不在函数里构造**（2026-08-24 改）：窗口切分、
    重叠合并、单窗失败退回单页重试——这套机制跟供应商没有任何关系，两边的
    provider 都实现了同样的 `vl_extract_csv(images, prompt, labels=,
    temperature=)` 签名。原来写死 `_make_ernie_provider()` 造成了一个真实的
    测试疏漏：这套机制是为 ernie 写的，qwen 一直停留在"整份一次送"的老路上，
    于是泰科龙那次 53 页因为 2 页红章**整份全灭**（recall 0.0），而 ernie 靠
    这套机制从 0.4 救到 0.8——两边根本没在同一个条件下比过。

    `overlap` 页在相邻窗口间重复送，给续页判断留上下文（跟 qwen"整份一起送"
    要解决的是同一个问题：续页没有表头，需要看到前一页才能判断"这是延续"）。
    窗口比整份小，也顺带降低了红章页拖累的范围——qwen 那次 53 页因为 2 页
    触发内容审查全灭，这里最多只拖累它所在的那一个窗口（`window_size` 页）。

    **一页出现在两个重叠窗口时的合并策略：任一窗口判 true 就算 true。**
    假阴性（该留的页被判掉）比假阳性危险得多，宁可多送一页给 Paddle 也不能漏
    一页——这是本脚本从第一版设计就定死的优先级，不是这里临时决定的。两次
    判定都保留在 `raw` 里，不静默丢弃被合并掉的那一份。

    2026-08-24 补：泰科龙实测撞见了重叠也救不回的情况——某个窗口整窗被模型
    拒答（原始响应是"作为一个人工智能语言模型，我还没学习如何回答这个问题
    ……"这类标准话术，HTTP 层面调用是成功的，不会被上面的 try/except 抓到，
    是 `_parse_batch_verdicts` 找不到任何一行能解析才报出来的），窗口内没有
    重叠覆盖到的中间几页因此彻底没有答案——8 页的窗口，边界 1 页重叠，中间
    6 页只要窗口一整窗失败就没有第二条命。**合并完之后，凡是所有覆盖过它的
    窗口全部报错的页，退回单页调用逐个重试**——只对真正丢失的页多花钱，不是
    给每一页都加保险；用的是已经验证过的 `_classify_one`/`PROMPT_CLASSIFY`
    单页路径（`provider` 参数是鸭子类型，qwen/ernie 通用，不用另写一份）。
    """
    # `ERNIE_MAX_IMAGES_PER_CALL` 是**千帆的**保守上限，对别的供应商不成立
    # （mimo 上下文 100 万 token，实测 8 页才 5157 token，53 页约 3.4 万，
    # 一次送得下）。所以这里只在 ernie 上断言，其它供应商由服务端自己报错，
    # 上面的 try/except 接得住。
    if isinstance(provider, QianfanVLProvider) and not isinstance(provider, MimoDirectProvider):
        assert window_size <= ERNIE_MAX_IMAGES_PER_CALL, (
            f"window_size={window_size} 超过千帆保守上限 {ERNIE_MAX_IMAGES_PER_CALL}")
    assert 0 <= overlap < window_size

    n, imgs = _render_all(slug, downscale=downscale)
    pages = sorted(imgs)

    per_page: dict[int, list[PageVerdict]] = {p: [] for p in pages}

    # 先把窗口切好，再**并发**发出去。窗口之间是互相独立的——重叠只在窗口
    # **内部**给续页判断提供上下文，不跨调用传递任何状态，所以没有任何理由
    # 串行等待。（2026-08-24 修：原来是 `while` 循环逐个窗口同步调用，纯粹
    # 是从 ernie 那版沿用下来的结构，没想过这件事，白白拖慢了整份文档的耗时。）
    windows: list[list[int]] = []
    i = 0
    while i < len(pages):
        windows.append(pages[i:i + window_size])
        if i + window_size >= len(pages):
            break
        i += window_size - overlap

    def _run_window(window: list[int]) -> list[PageVerdict]:
        labels = [f"PAGE_{p}" for p in window]
        images = [imgs[p] for p in window]
        try:
            raw = provider.vl_extract_csv(images, PROMPT_CLASSIFY_BATCH,
                                          labels=labels, temperature=0.0)
            return _parse_batch_verdicts(raw, window)
        except Exception as exc:                                   # noqa: BLE001
            return [PageVerdict(page=p, is_quote_page=False, reason="", summary="",
                                raw="", error=f"模型调用异常：{exc}") for p in window]

    with ThreadPoolExecutor(max_workers=min(len(windows), max_workers)) as ex:
        for verdicts in ex.map(_run_window, windows):
            for v in verdicts:
                per_page[v.page].append(v)

    # 单页重试：所有覆盖过这一页的窗口都失败了，才退回单页调用——重叠窗口
    # 里只要有一个成功的判定，这一页就不算丢，不重试。
    lost_pages = [p for p in pages if per_page[p] and all(v.error for v in per_page[p])]
    if lost_pages:
        log_lines = [f"{len(lost_pages)} 页所在的窗口整窗失败，退回单页重试：{lost_pages}"]
        print(f"  {log_lines[0]}", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=min(len(lost_pages), max_workers)) as ex:
            for v in ex.map(lambda pg: _classify_one(provider, pg, imgs[pg]), lost_pages):
                per_page[v.page].append(v)

    final: list[PageVerdict] = []
    for p in pages:
        vs = per_page[p]
        any_true = any(v.is_quote_page for v in vs)
        all_errored = all(bool(v.error) for v in vs)
        chosen = next((v for v in vs if v.is_quote_page), vs[0])
        final.append(PageVerdict(
            page=p, is_quote_page=any_true, reason=chosen.reason, summary=chosen.summary,
            raw=" | ".join(v.raw for v in vs if v.raw),
            error="" if not all_errored else " | ".join(v.error for v in vs if v.error)))

    report = DocReport(slug=slug, total_pages=n, golden=GOLDEN_CONTRIBUTING_PAGES.get(slug, set()))
    report.verdicts = final
    return report


def reparse_with_paddle(slug: str, predicted_pages: set[int]) -> dict:
    """把命中页抽出来重新拼一份 PDF，提交真实 Paddle，跟基线（整份识别）比对。

    基线来自 `apps.api.tests.test_scenarios_e2e` 已经跑过的快照回放——**不重新
    调用 Paddle 拿基线**，基线本来就有，只有"筛选后的子集"这条新路径需要真花钱
    验证。

    招标文件（`NO_REPARSE_BASELINE`）没有这份快照基线——它们本来就走文字层
    直抽、不进 Paddle，没有"整份送 Paddle"这件事可比，跳过并说明原因，不是
    漏掉。
    """
    if slug in NO_REPARSE_BASELINE:
        return {"skipped": "招标文件本来就走文字层直抽、不进 Paddle 账单，没有可比的基线"}

    import pypdf
    from apps.api.intelligence.providers import paddle_ocr
    from apps.api.intelligence.paddle_vl import recognize_quote_paddle
    from apps.api.intelligence.document_loader import DocumentLoader
    from apps.api.tests.test_scenarios_e2e import (
        recognize_snapshot, SUPPLIER_SNAPSHOT_PAGE_COUNTS, _total_of,
    )

    slug_full = f"quote_{slug}"
    baseline_rows = recognize_snapshot(slug_full)
    baseline_total = sum(_total_of(r.fields) or 0 for r in baseline_rows)

    if not predicted_pages:
        return {"skipped": "分类器一页都没选中，没有子集可送"}

    path = DOCS / DOC_FILES[slug]
    reader = pypdf.PdfReader(str(path))
    writer = pypdf.PdfWriter()
    for p in sorted(predicted_pages):
        writer.add_page(reader.pages[p - 1])
    subset_path = OUT_DIR / f"{slug}_subset.pdf"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(subset_path, "wb") as fh:
        writer.write(fh)

    n = DocumentLoader.get_page_count(str(subset_path))
    draft = recognize_quote_paddle(
        str(subset_path), submit_and_parse=paddle_ocr.submit_and_parse, page_count=n)
    rows = [r for r in draft.rows if r.row_type == "quote_line"]
    subset_total = sum(_total_of(r.fields) or 0 for r in rows)

    return {
        "baseline_rows": len(baseline_rows), "baseline_total": round(baseline_total, 2),
        "subset_pages_sent": len(predicted_pages), "subset_rows": len(rows),
        "subset_total": round(subset_total, 2),
        "row_count_matches": len(rows) == len(baseline_rows),
        "total_delta_pct": (round((subset_total - baseline_total) / baseline_total * 100, 2)
                            if baseline_total else None),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", choices=sorted(DOC_FILES), help="只跑一份")
    ap.add_argument("--all", action="store_true", help="跑全部 7 份")
    ap.add_argument("--no-downscale", action="store_true",
                    help="用原图（OCR 级分辨率）分类，默认用降采样图")
    ap.add_argument("--reparse", action="store_true",
                    help="额外把命中页重新送真实 Paddle 验证——真花钱，默认不开")
    ap.add_argument("--jobs", type=int, default=4, help="逐页模式的并发分类请求数（仅 qwen）")
    ap.add_argument("--per-page", action="store_true",
                    help="用旧的逐页独立分类（2026-08-24 前的默认路径，已知会漏判续页，"
                         "只用于跟新路径对照，不建议单独使用）")
    ap.add_argument("--provider", choices=("qwen", "ernie", "gemini", "mimo"), default="qwen",
                    help="qwen=DashScope（默认）；ernie=千帆 ernie-4.5-turbo-vl-32k"
                         "（需 QIANFAN_API_KEY）；gemini=gemini-3.6-flash 经第三方"
                         "网关 packyapi.com（需 PACKY_API_KEY，非 Google 官方接口）")
    ap.add_argument("--packy-model", default="gemini-3.6-flash",
                    help="仅 --provider gemini：走 packyapi 网关时用哪个模型。"
                         f"已配 max_tokens 的：{', '.join(PACKY_MODELS)}")
    ap.add_argument("--windowed", action="store_true",
                    help="分窗口送（每窗带页码标签+重叠+单窗失败退回单页重试）。"
                         "ernie 恒为此模式；qwen 加上它才走这条路——不加就是"
                         "整份一次送，红章页会拖累整份文档（泰科龙实测 53/53 全灭）")
    ap.add_argument("--window-size", type=int, default=8,
                    help="分窗口模式：每个窗口送几页（≤10）")
    ap.add_argument("--overlap", type=int, default=1,
                    help="分窗口模式：相邻窗口重叠几页，给续页判断留上下文")
    args = ap.parse_args()

    if not args.doc and not args.all:
        ap.error("要么 --doc <名字>，要么 --all")
    slugs = [args.doc] if args.doc else sorted(DOC_FILES)

    # 分窗口逻辑跟供应商无关（`classify_doc_windowed` 的 provider 由这里传入），
    # ernie 只能走这条；qwen 三条路都能走，`--windowed` 决定走哪条。
    if args.provider == "ernie":
        if args.per_page:
            ap.error("--per-page 是 qwen 专用的对照路径，ernie 没有逐页模式")
        provider = _make_ernie_provider()
        def classify(slug, *, downscale, jobs):        # noqa: ARG001 - jobs 占位对齐签名
            return classify_doc_windowed(slug, provider=provider, downscale=downscale,
                                         window_size=args.window_size, overlap=args.overlap)
        mode_label = f"千帆 ernie（窗口{args.window_size}/重叠{args.overlap}）"
    elif args.provider == "mimo":
        if args.per_page:
            ap.error("--per-page 是 qwen 专用的对照路径")
        provider = _make_mimo_direct_provider()
        def classify(slug, *, downscale, jobs):        # noqa: ARG001
            return classify_doc_windowed(slug, provider=provider, downscale=downscale,
                                         window_size=args.window_size, overlap=args.overlap)
        mode_label = f"mimo-v2.5 直连（窗口{args.window_size}/重叠{args.overlap}）"
    elif args.provider == "gemini":
        if args.per_page:
            ap.error("--per-page 是 qwen 专用的对照路径")
        provider = _make_packy_provider(args.packy_model)
        def classify(slug, *, downscale, jobs):        # noqa: ARG001
            return classify_doc_windowed(slug, provider=provider, downscale=downscale,
                                         window_size=args.window_size, overlap=args.overlap)
        mode_label = f"{args.packy_model}（窗口{args.window_size}/重叠{args.overlap}）"
    elif args.windowed:
        if args.per_page:
            ap.error("--windowed 和 --per-page 是互斥的两条路")
        provider = _make_provider()
        def classify(slug, *, downscale, jobs):        # noqa: ARG001
            return classify_doc_windowed(slug, provider=provider, downscale=downscale,
                                         window_size=args.window_size, overlap=args.overlap)
        mode_label = f"qwen（窗口{args.window_size}/重叠{args.overlap}）"
    else:
        classify = classify_doc_per_page if args.per_page else classify_doc
        mode_label = "逐页（对照用）" if args.per_page else "qwen 整份+页码标签"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_reports = []
    for slug in slugs:
        print(f"=== {slug}：分类中（{mode_label}）… ===", file=sys.stderr)
        report = classify(slug, downscale=not args.no_downscale, jobs=args.jobs)
        d = report.to_dict()
        if args.reparse:
            print(f"=== {slug}：命中 {len(report.predicted)} 页，重新送 Paddle 验证… ===", file=sys.stderr)
            d["reparse"] = reparse_with_paddle(slug, report.predicted)
        all_reports.append(d)

        # 文件名带供应商/模式后缀——不然 qwen 和 ernie 跑同一份文档会互相覆盖，
        # 之前的结果就没法比对了。
        if args.provider == "mimo":
            suffix = "mimo_direct"
        elif args.provider == "gemini":
            suffix = args.packy_model.replace(".", "_")
        elif args.provider == "ernie":
            suffix = args.provider
        elif args.per_page:
            suffix = "per_page"
        elif args.windowed:
            suffix = "qwen_windowed"      # 别覆盖掉"整份一次送"那次的结果，两者要能对比
        else:
            suffix = "qwen"
        fn = OUT_DIR / f"{slug}__{suffix}.json"
        fn.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        fn_note = " ⚠️ 有假阴性" if d["false_negatives"] else ""
        print(f"{slug}: {report.total_pages}页 → 命中{len(report.predicted)}页 "
             f"| recall={d['recall']} precision={d['precision']} "
             f"| 假阴性={d['false_negatives']}{fn_note}", file=sys.stderr)

    summary_path = OUT_DIR / f"_summary__{args.provider}.json"
    summary_path.write_text(json.dumps(all_reports, ensure_ascii=False, indent=2), encoding="utf-8")
    total_fn = sum(len(r["false_negatives"]) for r in all_reports)
    total_golden = sum(len(r["golden_pages"]) for r in all_reports)
    total_pages = sum(r["total_pages"] for r in all_reports)
    total_sent = sum(r["would_send_to_paddle"] for r in all_reports)
    print(f"\n=== 汇总（{len(slugs)} 份）===", file=sys.stderr)
    print(f"总页数 {total_pages} → 分类后会送 {total_sent} 页 "
         f"（省 {(1 - total_sent/total_pages)*100:.0f}%，理论账见 HANDOFF）", file=sys.stderr)
    print(f"假阴性 {total_fn}/{total_golden}（真正贡献行的页里，被判掉的有几页——"
         f"这是唯一真正危险的数字）", file=sys.stderr)
    print(f"详情已写 {summary_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
