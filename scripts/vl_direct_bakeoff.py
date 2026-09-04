"""vl_direct_bakeoff.py — VL 直出 CSV vs 现有多阶段管线，同一把尺子对照。

背景：客户用多模态模型直接读同样四份 PDF，得到 136 行且四个总价分毫不差；
现有多阶段管线（OCR→HTML→TableGrid→LLM）在同样文档上是 248/131/62/29。
本脚本把「图 → CSV」这条路跑一遍，用同一套 golden 打分，决定是否值得换架构。

设计要点：
- 默认**整份一次调用**（所有页图像放进同一个 content 数组）。实测该端点不接受
  PDF（image=PDF → 400「图像格式非法」；pdf/file/document 键 → 400「项类型不支持」），
  对话框里能直接丢 PDF 是前端替你栅格化了，API 层没有这一步。
  整份送的价值不只是省调用：续页表头在前页、重复副本要跨页才看得见、
  序号连续性也只有整份才能校验——逐页送等于把 VL 最大的优势丢掉；
- --per-page 保留逐页模式，用于定位单页问题；
- 英文提示词，直接要求 CSV，不要求 JSON（减少结构化失败）；
- 原样转录：不做单位换算、不补算合价、空值保持空——与 golden 的口径一致；
- 产物落盘每页原始返回，便于事后追溯，不静默丢弃。

用法：
    python scripts/vl_direct_bakeoff.py --doc 远东 --pages 3 5    # 先验管道
    python scripts/vl_direct_bakeoff.py                           # 四份全跑
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SRC = REPO / "docs" / "test1" / "prj1"
OUT_DIR = REPO / "tmp" / "vl_bakeoff"

# 七份投标文件（四份电缆 + 三份既有基线），doc 名 → (PDF 所在目录, golden slug)
DOCS = {
    "上海浦东": ("tests/fixtures/documents", "quote_cable_pudong"),
    "亨通":     ("tests/fixtures/documents", "quote_cable_hengtong"),
    "宏胜":     ("tests/fixtures/documents", "quote_cable_hongsheng"),
    "远东":     ("tests/fixtures/documents", "quote_cable_yuandong"),
    "凯硕新正": ("tests/fixtures/documents", "quote_kaishuo"),
    "上海绵存": ("tests/fixtures/documents", "quote_miancun"),
    "泰科龙":   ("tests/fixtures/documents", "quote_taikelong"),
}

# PDF 渲染全局串行。pypdfium2 不是线程安全的：七路并发渲染实测直接触发原生崩溃
# （WinError 0xc000001d，整个进程死掉，已跑完的文档一起丢）。渲染只占总耗时的几个
# 百分点（瓶颈是 API 往返），串行化的代价可以忽略，换来的是并发不会整锅端。
_RENDER_LOCK = threading.Lock()

# 2026-08-09 实测（均带方向转正）：七份金额差绝对值合计
#   3.7-flash 24,622 元 / **3.7-plus 0.05 元** / 3.8-max 1,300 元
# 逐字段全对率 92% / 96% / 96%；3.8-max 更慢更贵却不更准，故主模型取 3.7-plus。
MODEL = "qwen3.7-plus"

# 方向预检默认跟随主模型。选型依据与已否决的方案见 PROMPT_ORIENT 上方的对照表。
# qwen3-vl-plus 虽快 15 倍（2.6s），但四种提示词 + 开 think 都稳定漏判真正侧躺的页，
# 是系统性偏差，投票压不住 —— 已否决。
ROTATE_MODEL = None
BASE_URL = "https://dashscope.aliyuncs.com/api/v1"

# 极简提示词 + 三条业务语义规则。2026-08-09 两条教训：
# (1) 告诉模型**怎么看**的约束全部有害——「按原始左右顺序输出列」让侧向页整张表转置；
#     「用文档自己的表头」让无表头续页把第一条数据行当成表头吃掉。已全部删除。
# (2) 告诉模型**输出什么**的约束有用——那是业务语义，模型从图像无从推断。上一版把这类
#     一起砍了，于是合计行混入明细求和（宏胜读数从 +1862 万翻到 −197 万）、
#     重复副本被当独立行抽两遍（上海浦东 59 行、金额翻倍）。
# 三条规则都只规定输出内容，且统一为「标注而非丢弃」：抽取层不做业务决策，
# 合计行与重复副本都是证据，丢掉之后下游和人工再也拿不回来（CLAUDE.md §4）。
PROMPT_DOC = """请将这份投标文件中的报价清单导出为 CSV 格式给我。只返回 CSV，不要其他说明。

另外遵守三条规则：
1. 小计/合计/总计行要保留，不要跳过。第一列固定为 row_type，标注每行类型：
   明细行填 detail，小计行填 subtotal，总计/合计行填 total。
2. 只转录文档上确实写着的数字。任何单元格为空或看不清就留空，
   不要用数量×单价补算合价，也不要补算任何其他数字。
3. 如果同一份清单在文件里重复出现（例如正本与副本、汇总与明细），照实全部输出，
   不要合并也不要丢弃。最后一列固定为 copy_no，标注该行属于第几份（1、2……）。"""
PROMPT_PAGE = PROMPT_DOC

# 最简提示词（不带三条业务规则）。用户反馈：直接把 PDF 丢给 GPT、什么规则都不加，
# 拿到的就是标准答案。留作对照——规则是为了让下游拿到 row_type/copy_no 这些业务
# 语义，不是为了提高转录准确率；两者要分开验证。
PROMPT_PLAIN = "请将这份投标文件中的报价清单导出为 CSV 格式给我。只返回 CSV，不要其他说明。"

PROMPT = PROMPT_DOC          # 由 main() 按 --plain-prompt 覆盖


def _api_key() -> str:
    """与生产 provider 同源取密钥：环境变量优先，其次 apps/api/.env（Settings）。"""
    key = os.getenv("DASHSCOPE_API_KEY") or ""
    if not key:
        from apps.api.core.config import get_settings
        s = get_settings()
        key = (getattr(s, "DASHSCOPE_API_KEYS", "") or "").split(",")[0].strip()             or getattr(s, "DASHSCOPE_API_KEY", "")
    return key


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _is_openai_compatible(base_url: str | None) -> bool:
    """DashScope 原生接口与 OpenAI 兼容接口的载荷结构不同，按 base_url 分流。"""
    return bool(base_url) and "dashscope" not in (base_url or "")


def _openai_key() -> str:
    """OpenAI 兼容端点的密钥只从环境变量取——第三方中转密钥不进仓库、不落盘。"""
    return (os.getenv("OPENAI_API_KEY") or os.getenv("VL_OPENAI_API_KEY") or "").strip()


def _call_openai(content_parts: list[dict], prompt: str, *, model: str, base_url: str,
                 retries: int = 2, temperature: float | None = None) -> tuple[str, str]:
    """OpenAI 兼容端点：图像走 image_url + data URI，文本与图像可交错。

    交错能力是方向预检需要的（PAGE_n_ROT_r 标签必须紧挨对应图像）；
    OpenAI 的 content 数组天然支持，与 DashScope 侧行为一致。
    """
    key = _openai_key()
    if not key:
        return "", "no OPENAI_API_KEY in env"
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url, timeout=1800.0, max_retries=0)
    parts = list(content_parts) + [{"type": "text", "text": prompt}]
    last = ""
    for attempt in range(retries + 1):
        try:
            kw = {} if temperature is None else {"temperature": temperature}
            stream = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": parts}],
                stream=True, **kw)
            chunks = []
            for ev in stream:
                if ev.choices and ev.choices[0].delta and ev.choices[0].delta.content:
                    chunks.append(ev.choices[0].delta.content)
            text = "".join(chunks)
            if not text.strip():
                last = "empty response"
                time.sleep(2 * (attempt + 1))
                continue
            return text.replace("```csv", "").replace("```", "").strip(), ""
        except Exception as exc:                      # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:300]
            time.sleep(2 * (attempt + 1))
    return "", last


def call_pdf(pdf_path: Path, prompt: str, *, model: str, base_url: str,
             retries: int = 2) -> tuple[str, str]:
    """把 PDF 原文件整个交给模型（OpenAI Responses API 的 input_file）。

    这条路避开了本地渲染的全部问题——不用定方向、不用挑分辨率、不用担心页数上限，
    模型自己解析 PDF。实测该端点：`responses.create` + input_file 可用；
    `chat.completions` 的 `type=file` 不支持（返回空结果）。
    DashScope 侧没有等价能力（image=PDF 返回「图像格式非法」），故仅 OpenAI 兼容端点可用。
    """
    key = _openai_key()
    if not key:
        return "", "no OPENAI_API_KEY in env"
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url, timeout=1800.0, max_retries=0)
    b64 = base64.b64encode(pdf_path.read_bytes()).decode()
    last = ""
    for attempt in range(retries + 1):
        try:
            r = client.responses.create(model=model, input=[{"role": "user", "content": [
                {"type": "input_file", "filename": pdf_path.name,
                 "file_data": f"data:application/pdf;base64,{b64}"},
                {"type": "input_text", "text": prompt}]}])
            text = (r.output_text or "").replace("```csv", "").replace("```", "").strip()
            if not text:
                last = "empty response"
                time.sleep(2 * (attempt + 1))
                continue
            return text, ""
        except Exception as exc:                      # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:300]
            time.sleep(2 * (attempt + 1))
    return "", last


def call_images(pngs: list[bytes], prompt: str, retries: int = 2,
                model: str | None = None, base_url: str | None = None) -> tuple[str, str]:
    """一次调用送 N 张页图。返回 (csv_text, error)；失败不抛，交调用方记账。

    该端点只接受 image 项——实测 image=PDF 返回 400「图像格式非法」，
    pdf/file/document 键返回 400「项类型不支持」。故整份送 = 多张图放同一 content。
    """
    if _is_openai_compatible(base_url):
        return _call_openai([{"type": "image_url", "image_url": {"url": _data_uri(b)}}
                             for b in pngs], prompt, model=model or MODEL,
                            base_url=base_url, retries=retries)
    import dashscope
    dashscope.base_http_api_url = base_url or BASE_URL
    key = _api_key()
    if not key:
        return "", "no DASHSCOPE_API_KEY (env or apps/api/.env)"
    content = [{"image": _data_uri(b)} for b in pngs]
    content.append({"text": prompt})
    messages = [{"role": "user", "content": content}]
    last = ""
    for attempt in range(retries + 1):
        try:
            # 1) 关思考：实测同一页 thinking 开/关都是 14 行，但耗时 104s vs 52s。
            #    转录不需要推理链，纯属浪费。
            # 2) 流式：整份 11 页非流式会撞 SDK 的 300s 读超时（实测重试 3 次共 915s
            #    全废）。流式下增量到达，不会因为总时长超限而整体失败。
            responses = dashscope.MultiModalConversation.call(
                api_key=key, model=model or MODEL, messages=messages,
                stream=True, incremental_output=True,
                extra_body={"enable_thinking": False},
            )
            chunks, status = [], 200
            for r in responses:
                status = getattr(r, "status_code", 200)
                if status != 200:
                    last = f"status={status} {getattr(r, 'message', '')}"
                    break
                c = r.output.choices[0].message.content
                if isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("text"):
                            chunks.append(part["text"])
                elif c:
                    chunks.append(str(c))
            if status != 200:
                time.sleep(2 * (attempt + 1))
                continue
            text = "".join(chunks)
            return text.replace("```csv", "").replace("```", "").strip(), ""
        except Exception as exc:                      # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
    return "", last


# 方向预检 = 四选一：把同一页的 4 个旋转版本并排给模型，让它挑正立的那张。
#
# 2026-08-09 在同一份文档（含 90°/180°/270° 三种偏转）上把各种方案量了一遍：
#
#   方案                     图/页  耗时     精确对   横竖错(致命)
#   四选一 · 3.7-plus           4   95.7s   11/11        0     ← 采用
#   四选一 · flash              4   20.3s   10/11        0
#   两问纯二元 · 3.7-plus        2   96.1s    3/11        1
#   两问+对比图 · flash          3   45.4s   11/11        0
#
# **决定性的是"让模型比较候选"，不是图数。** 纯二元地问「这页倒了吗」三个模型都答不好
# （3/11）；把同一页和它转 180° 的版本并排一比，立刻全对。四选一本质就是比较。
#
# **模型必须用主模型**：上表里 flash 的 10/11 是**单次测量**。换到完整七份上重跑，
# 同一份文档 flash 漏掉了 6 页的 180°（只认出 1 页），七份金额差从 0.05 元变成
# 55,952 元。它不是不行，是**不稳定**，而且多轮之间错误相关，3 轮投票压不住。
# 主模型两次跑都把那 7 页全部认出。**教训：单次结果不能作为选型判据。**
#
# 另一条已撤回的判断：「只判横竖就够，180° 无所谓」——错。单页倒置确实 23/23 全对、
# 整批统一倒置只差 −239 元，但**同一份里方向混杂**时损失 −25.6 万，而混杂是常态。
PROMPT_ORIENT = """下面每一页给了 4 个旋转版本，标签形如 PAGE_<页号>_ROT_<角度>。
对每一页，挑出文字正立、可以正常阅读的那个角度。
只返回 CSV 两列：page,rotation。"""


def detect_orientations(render, pages: list[int], *, model: str, base_url: str,
                        scale: float = 0.30, votes: int = 3
                        ) -> tuple[dict[int, int], set[int]]:
    """方向预检：把同一页的 4 个旋转版本并排给模型，让它挑正立的那张。

    方向的价值极大：同一份文档转正前后，明细求和差从 +176 万变成 +0.04 元。
    为什么是「比较候选」而不是「凭空判断」，见 PROMPT_ORIENT 上方的实测对照表。

    为什么不用投影法/墨迹能量免费定轴向：实测不成立，表格框线压过文字行信号
    （HANDOFF §2 已否决，别再试）。这些页是纯扫描图，get_text() 为空；
    /Rotate 元数据也不可用——实测 7 页全部倒置而 /Rotate 全是 0。

    交错 PAGE_n_ROT_r 标签是必要的：视觉分类稳定性 v4 的教训，不交错标签模型会串页。

    返回 (需旋转的页 → 角度, 已达成共识的页集合)。两者必须分开：
    没有共识的页不等于不用转，只是这一轮没问出来——调用方据此决定是否缓存。
    """
    # 多轮投票：单轮不稳定是实测事实——同一份文档两次预检，某页一次判 180、一次判 0，
    # 而误判的代价很大（一页转错，那一页的行全废）。缩略图便宜，投票最划算。
    tally: dict[int, dict[int, int]] = {p: {} for p in pages}
    for _round in range(max(1, votes)):
        one = _detect_once(render, pages, model=model, base_url=base_url, scale=scale)
        for p, r in one.items():
            tally.setdefault(p, {})[r] = tally.setdefault(p, {}).get(r, 0) + 1
    rotations: dict[int, int] = {}
    decided: set[int] = set()
    for p, counts in tally.items():
        if not counts:
            continue
        best, n = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
        if n * 2 > max(1, votes):       # 过半才算判定；否则该页视为"没结论"
            decided.add(p)
            if best:
                rotations[p] = best
    # 必须把「判定为 0°」和「没有共识」分开返回。合并成同一个东西会让调用方
    # 把"检测整体失败"误当成"全都不用转"——实测缓存过一次这样的坏结论，
    # 之后两份文档的金额差从 ±0.00 变成 −71 万和 −20 万。
    return rotations, decided


def _detect_once(render, pages: list[int], *, model: str, base_url: str,
                 scale: float) -> dict[int, int]:
    if _is_openai_compatible(base_url):
        parts: list[dict] = []
        for p in pages:
            for rot in (0, 90, 180, 270):
                parts.append({"type": "text", "text": f"PAGE_{p}_ROT_{rot}"})
                parts.append({"type": "image_url",
                              "image_url": {"url": _data_uri(render(p, scale, rot))}})
        text, err = _call_openai(parts, PROMPT_ORIENT, model=model or MODEL,
                                 base_url=base_url, retries=1, temperature=0)
        return {} if err else _parse_orientations(text, pages)
    content: list[dict] = []
    for p in pages:
        for rot in (0, 90, 180, 270):
            content.append({"text": f"PAGE_{p}_ROT_{rot}"})
            content.append({"image": _data_uri(render(p, scale, rot))})
    content.append({"text": PROMPT_ORIENT})
    import dashscope
    dashscope.base_http_api_url = base_url or BASE_URL
    key = _api_key()
    if not key:
        return {}
    try:
        responses = dashscope.MultiModalConversation.call(
            api_key=key, model=model or MODEL,
            messages=[{"role": "user", "content": content}],
            stream=True, incremental_output=True,
            extra_body={"enable_thinking": False}, temperature=0,
        )
        chunks = []
        for r in responses:
            if getattr(r, "status_code", 200) != 200:
                return {}
            c = r.output.choices[0].message.content
            if isinstance(c, list):
                chunks += [x["text"] for x in c if isinstance(x, dict) and x.get("text")]
            elif c:
                chunks.append(str(c))
    except Exception:                                 # noqa: BLE001
        return {}
    return _parse_orientations("".join(chunks), pages)


def _parse_orientations(text: str, pages: list[int]) -> dict[int, int]:
    """解析 `page,rotation`。答不上来的页不出现在结果里——**不默认成 0°**，
    那会把"没问出来"和"确定不用转"混为一谈（曾因此缓存过一份全错的结论）。
    """
    out: dict[int, int] = {}
    valid = set(pages)
    for line in text.replace("```", "").splitlines():
        cells = [x.strip() for x in line.split(",")]
        if len(cells) < 2:
            continue
        page, rot = re.sub(r"\D", "", cells[0]), re.sub(r"\D", "", cells[1])
        if (page.isdigit() and int(page) in valid
                and rot.isdigit() and int(rot) in (0, 90, 180, 270)):
            out[int(page)] = int(rot)
    return out


def _page_arith_score(csv_text: str) -> tuple[int, int]:
    """单页产物的「行内算术闭合」得分：返回 (闭合行数, 可评估行数)。

    这是方向验证的判据——**不问模型"这页正不正"，问"按这个角度读出来的东西自不自洽"**。
    转正的页能抽出 数量×单价≈合价 成立的行；转错的页抽出来的数字对不上。
    判据完全确定性，不依赖某次调用的手气。
    """
    import csv as _csv

    from apps.api.services.ingestion.draft_integrity import check_row_arithmetic

    lines = [l for l in csv_text.splitlines() if l.strip()]
    if len(lines) < 2:
        return 0, 0
    rows = [r for r in _csv.DictReader(lines) if any(r.values())]
    if not rows:
        return 0, 0
    hdr = [h for h in rows[0].keys() if h]

    def col(*names):
        for n in names:
            for h in hdr:
                if n in h:
                    return h
        return ""

    cq, cu, ct = col("数量", "quantity", "qty"), col("单价", "unit_price"), \
        col("合价", "金额", "价税合计", "total")
    ok = total = 0
    for r in rows:
        res = check_row_arithmetic({"qty": r.get(cq), "unit_price": r.get(cu),
                                    "total_price": r.get(ct)})
        if res.status == "not_evaluable":
            continue
        total += 1
        if res.status in ("ok", "multiplier"):
            ok += 1
    return ok, total


# 验证一组方向假设时，闭合行数至少要多出这么多才算「另一个角度更好」。
# 差距太小说明两个角度都读不出结构，不该据此翻转。
_VERIFY_MIN_MARGIN = 3


def verify_rotations(render, pages: list[int], rotations: dict[int, int], *,
                     model: str, base_url: str, scale: float) -> tuple[dict[int, int], list[dict]]:
    """用抽取结果反过来验证方向假设，只验 180° 这一维。

    为什么只验 180°：24 次稳定性实测里，轴向（横竖）只错过 3 次，其余全部错在 180° 上。
    而 180° 恰恰是模型最判不准、投票也压不住的那一维（同一份文档同一配置三次跑出
    3/10、10/10、10/10）。

    为什么按**组**验而不是逐页：同一份文档里方向通常成段一致，逐页验证要几十次调用。
    按提议角度分组，每组抽一页当代表，验完整组套用。典型 2~3 组 → 4~6 次单页调用。

    返回 (修正后的角度, 验证记录)。判不出来时保持原判并标注——不猜。
    """
    groups: dict[int, list[int]] = {}
    for p in pages:
        groups.setdefault(rotations.get(p, 0), []).append(p)

    fixed = dict(rotations)
    report: list[dict] = []
    for rot, members in sorted(groups.items()):
        probe = members[len(members) // 2]          # 取中间页，避开封面/封底
        scores = {}
        for cand in (rot, (rot + 180) % 360):
            text, err = call_images([render(probe, scale, cand)], PROMPT,
                                    model=model, base_url=base_url, retries=1)
            scores[cand] = (0, 0) if err else _page_arith_score(text)
        best = max(scores, key=lambda c: scores[c][0])
        rec = {"proposed": rot, "pages": len(members), "probe_page": probe,
               "scores": {str(c): f"{v[0]}/{v[1]}" for c, v in scores.items()}}
        if best != rot and scores[best][0] - scores[rot][0] >= _VERIFY_MIN_MARGIN:
            for p in members:
                fixed[p] = best if best else 0
                if not best:
                    fixed.pop(p, None)
            rec["action"] = f"翻转 {rot}° → {best}°"
        elif scores[rot][1] == 0 and scores[best][1] == 0:
            rec["action"] = "两个角度都读不出结构，保持原判并标注存疑"
        else:
            rec["action"] = "保持"
        report.append(rec)
    return fixed, report


_ROT_CACHE_DIR = REPO / "tmp" / "orientation_cache"


def _rot_cache_key(pdf_path: Path, pages: list[int], model: str) -> Path:
    """按 PDF 内容哈希 + 页集合 + 模型缓存方向结论。

    同一份 PDF 的页面方向是文件的固有属性，不会因为重跑而改变；重复检测纯属浪费。
    以内容哈希为键而不是文件名——换个名字的同一份文件应当命中缓存。
    """
    import hashlib
    h = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16]
    tag = f"{h}_{min(pages)}-{max(pages)}-{len(pages)}_{model}"
    return _ROT_CACHE_DIR / f"{tag}.json"


def _n_rows(csv_text: str) -> int:
    return max(0, len([l for l in csv_text.splitlines() if l.strip()]) - 1)


def _merge_csv(parts: list[str]) -> str:
    """拼接分段结果：保留第一段表头，后续段落丢弃各自表头行。"""
    out: list[str] = []
    for i, p in enumerate(parts):
        lines = [l for l in p.splitlines() if l.strip()]
        if not lines:
            continue
        out.extend(lines if i == 0 else lines[1:])
    return "\n".join(out)


def run_doc(name: str, args, out: Path) -> dict:
    """跑一份文档。整份一次调用；撞内容审核误杀时降分辨率、再不行分段重试。

    泰科龙实测返回 `DataInspectionFailed: Input image data may contain inappropriate
    content` —— 53 页一次性送审，任何一页被误判整份就废。降分辨率与分段都是把误判面
    缩小，不是绕过审核。
    """
    import pypdfium2 as pdfium

    sub_dir, _slug = DOCS[name]
    pdf_path = next((REPO / sub_dir).glob(f"*{name}*.pdf"))
    doc = pdfium.PdfDocument(str(pdf_path))
    pages = args.pages or list(range(1, len(doc) + 1))
    doc_dir = out / name
    doc_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict] = []
    t0 = time.time()

    rotations: dict[int, int] = {}

    def _render(pno: int, scale: float, rotation: int | None = None) -> bytes:
        with _RENDER_LOCK:                      # pypdfium2 非线程安全，见文件头说明
            page = doc[pno - 1]
            buf = io.BytesIO()
            # rotation 显式给出时用它（预检渲 4 个版本）；否则用预检结论，
            # 预检没覆盖到的页按 0 处理——不猜。
            rot = rotations.get(pno, 0) if rotation is None else rotation
            page.render(scale=scale, rotation=rot).to_pil().convert("RGB").save(buf, "PNG")
            page.close()
            return buf.getvalue()

    if args.rotate_map:
        # 逐页指定角度，用于做单变量对照（例如"轴向全对、只差 180°"损失多少）。
        rotations = {int(k): int(v) for k, v in json.loads(args.rotate_map).items()}
        print(f"[{name}] 指定方向 {dict(sorted(rotations.items()))}")
    elif args.force_rotate:
        rotations = {p: args.force_rotate for p in pages}
        print(f"[{name}] 强制旋转 {args.force_rotate}°（用于验证方向假设）")
    elif args.auto_rotate:
        rot_model = args.rotate_model or args.model
        cache = _rot_cache_key(pdf_path, pages, rot_model)
        t_rot = time.time()
        if cache.exists() and not args.no_rotate_cache:
            rotations = {int(k): v for k, v in
                         json.loads(cache.read_text(encoding="utf-8")).items()}
            src = "缓存"
        else:
            rotations, decided = detect_orientations(
                _render, pages, model=rot_model, base_url=args.base_url,
                votes=args.rotate_votes)
            coverage = len(decided) / max(len(pages), 1)
            src = f"{rot_model} × {args.rotate_votes} 轮，共识覆盖 {coverage:.0%}"
            if args.verify_rotate:
                rotations, vreport = verify_rotations(
                    _render, pages, rotations, model=args.model,
                    base_url=args.base_url, scale=args.scale)
                for rec in vreport:
                    print(f"[{name}] 方向验证 {rec['proposed']}°×{rec['pages']}页 "
                          f"(p{rec['probe_page']} 算术闭合 {rec['scores']}) → {rec['action']}")
                src += " + 算术闭合验证"
            # 只有每一页都达成共识才写缓存。部分失败的结论一旦落盘会被后续所有运行
            # 沿用，而"少转了几页"在下游是看不出来的——只会表现为金额莫名其妙地差。
            if coverage >= 1.0:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(rotations), encoding="utf-8")
            else:
                src += "（未达全覆盖，不写缓存）"
        skew = {p: r for p, r in rotations.items() if r}
        rot_secs = round(time.time() - t_rot, 1)
        print(f"[{name}] 方向预检({src}, {rot_secs}s)：{len(skew)}/{len(pages)} 页需旋转 "
              f"{dict(sorted(skew.items())) if skew else ''}")

    print(f"[{name}] {len(doc)} 页，本次跑 {len(pages)} 页")

    if args.pdf_direct:
        doc.close()
        text, err = call_pdf(pdf_path, PROMPT, model=args.model, base_url=args.base_url)
        (doc_dir / "document.csv").write_text(text, encoding="utf-8")
        rec = {"doc": name, "pages_run": len(pages), "rows": _n_rows(text) if text else 0,
               "strategy": "PDF直传", "errors": ([{"page": "all", "error": err}] if err else []),
               "seconds": round(time.time() - t0, 1)}
        print(f"[{name}] → {rec['rows']} 行 / {rec['seconds']}s / PDF直传"
              + (f" / 失败 {err}" if err else ""))
        return rec

    if args.per_page:
        rows_total = 0
        for pno in pages:
            csv_text, err = call_images([_render(pno, args.scale)], PROMPT,
                                        model=args.model, base_url=args.base_url)
            (doc_dir / f"p{pno}.csv").write_text(csv_text, encoding="utf-8")
            if err:
                errors.append({"page": pno, "error": err})
                continue
            rows_total += _n_rows(csv_text)
        doc.close()
        rec = {"doc": name, "pages_run": len(pages), "rows": rows_total,
               "errors": errors, "seconds": round(time.time() - t0, 1)}
        print(f"[{name}] → {rows_total} 行 / {rec['seconds']}s / 失败 {len(errors)} 页")
        return rec

    csv_text, err, strategy = "", "", "整份"
    for scale in (args.scale, args.scale * 0.7):
        pngs = [_render(p, scale) for p in pages]
        mb = sum(len(b) for b in pngs) * 4 / 3 / 1e6          # base64 膨胀约 4/3
        print(f"[{name}] 整份 {len(pngs)} 张图 @scale{scale:.2f}，约 {mb:.1f} MB")
        csv_text, err = call_images(pngs, PROMPT,
                                    model=args.model, base_url=args.base_url)
        if not err:
            strategy = f"整份@{scale:.2f}"
            break
        errors.append({"page": "all", "scale": round(scale, 2), "error": err})
        print(f"[{name}] 失败：{err}")
        if "DataInspection" not in err and "inappropriate" not in err:
            break

    if err and ("DataInspection" in err or "inappropriate" in err):
        # 分段：把送审面缩小到 chunk 页，逐段跑，只丢失被误杀的那一段。
        chunk = max(1, args.chunk)
        parts, bad = [], 0
        for i in range(0, len(pages), chunk):
            seg = pages[i:i + chunk]
            pngs = [_render(p, args.scale * 0.7) for p in seg]
            t, e = call_images(pngs, PROMPT,
                               model=args.model, base_url=args.base_url)
            if e:
                bad += 1
                errors.append({"page": f"{seg[0]}-{seg[-1]}", "error": e})
                print(f"[{name}] 段 {seg[0]}-{seg[-1]} 失败：{e}")
                continue
            parts.append(t)
        if parts:
            csv_text, err = _merge_csv(parts), ""
            strategy = f"分段{chunk}页（{bad} 段失败）"

    (doc_dir / "document.csv").write_text(csv_text, encoding="utf-8")
    doc.close()
    rows_total = _n_rows(csv_text) if csv_text else 0
    rec = {"doc": name, "pages_run": len(pages), "rows": rows_total,
           "strategy": strategy, "errors": errors,
           "seconds": round(time.time() - t0, 1)}
    print(f"[{name}] → {rows_total} 行 / {rec['seconds']}s / {strategy}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", action="append", help="供应商名，可重复；省略则四份全跑")
    ap.add_argument("--pages", type=int, nargs="*", help="只跑这些页（1-based），用于验管道")
    ap.add_argument("--scale", type=float, default=1.6,
                    help="整份送时多张图叠加，默认比逐页略低以控制载荷")
    ap.add_argument("--per-page", action="store_true", help="逐页模式，用于定位单页问题")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--jobs", type=int, default=7, help="并行文档数（本轮之前是串行）")
    ap.add_argument("--pdf-direct", action="store_true",
                    help="把 PDF 原文件交给模型（仅 OpenAI 兼容端点支持），不做本地渲染")
    ap.add_argument("--plain-prompt", action="store_true",
                    help="用不带三条业务规则的最简提示词（对照用）")
    ap.add_argument("--rotate-model", default=ROTATE_MODEL,
                    help="方向预检模型；默认跟随 --model。换快模型实测会判错方向，勿改")
    ap.add_argument("--no-rotate-cache", action="store_true",
                    help="忽略方向缓存，强制重新检测")
    ap.add_argument("--rotate-map", default="",
                    help="逐页指定角度的 JSON（如 {\"2\":90,\"10\":270}）；用于单变量对照")
    ap.add_argument("--verify-rotate", action="store_true",
                    help="方向判定后用「抽一页看算术闭不闭合」反验 180° 维（准确优先时开）")
    ap.add_argument("--rotate-votes", type=int, default=3,
                    help="方向预检投票轮数；单轮实测不稳，默认 3 轮取多数")
    ap.add_argument("--force-rotate", type=int, default=0, choices=(0, 90, 180, 270),
                    help="所有页强制旋转（验证方向假设用，不做检测）")
    ap.add_argument("--auto-rotate", action="store_true",
                    help="先做一次缩略图方向预检，把页面转正再抽取")
    ap.add_argument("--chunk", type=int, default=8,
                    help="内容审核误杀后的分段页数")
    args = ap.parse_args()

    global PROMPT
    PROMPT = PROMPT_PLAIN if args.plain_prompt else PROMPT_DOC

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    names = args.doc or list(DOCS)
    from concurrent.futures import ThreadPoolExecutor
    print(f"并行 {min(len(names), args.jobs)} 路，共 {len(names)} 份")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        summary = list(pool.map(lambda n: run_doc(n, args, out), names))

    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
