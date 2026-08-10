"""calibrate_golden_raw_name.py — 给 golden 增加 raw_name（PDF 字面名）双字段。

背景（Step 0 审计结论）：
  golden 忠实镜像 Excel 标准答案，但 Excel 用的是另一套（编制者）命名，与 PDF
  字面名在 ~70% 行不同（有些是不同器件类型）。因此「名称 vs golden」不能用来评
  识别准确率。解决办法：给每条 golden 增加 raw_name = PDF 字面名，让 e2e_diff 能
  区分「忠实抽取 PDF」与「真识别错误」。**不改行数、不改 name、不改任何价格。**

映射方法：按 (规格 spec + 含税合价 total) 强键，把 OCR 物理行的字面名写入 golden
的 raw_name。歧义（同 key 多个不同名）或无匹配（DN 标号两边不同 / Excel 重复行）→
不猜，标 raw_name=null + raw_name_status，留待人工对照真实 PDF。

安全（CLAUDE.md §10/§12）：
  - 默认 dry-run，只打印逐行计划，不写盘。
  - --apply 才写：先备份 <golden>.bak，单次写入，附守恒报告（行数/总额/name 不变）。

用法：
    python scripts/calibrate_golden_raw_name.py                 # dry-run（默认）
    python scripts/calibrate_golden_raw_name.py --apply         # 备份后写入
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.disable(logging.CRITICAL)

DOC = "quote_miancun"
PDF = REPO / "docs/test/上海绵存投标文件.pdf"
GOLDEN = REPO / "data/golden/quote_miancun.json"
SNAP = REPO / "tests/fixtures/ocr_snapshots/quote_miancun.json"


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("，", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm(s) -> str:
    return "".join(str(s or "").split()).lower()


def load_ocr_rows() -> list[dict]:
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.document_loader import DocumentLoader
    provider = SnapshotProvider(None, SNAP, mode="replay")
    imgs = DocumentLoader.to_images(str(PDF))
    ocr = provider._ocr

    def h(b):
        return hashlib.sha256(b).hexdigest()

    rows = []
    for pageno in range(1, len(imgs) + 1):
        html = ocr.get(h(imgs[pageno - 1]), "")
        if not html:
            continue
        for tr in re.split(r"<tr[^>]*>", html):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            txt = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            txt = [t for t in txt if t]
            if len(txt) < 4:
                continue
            name = txt[0]
            if _num(name) is not None:
                continue
            spec = next((t for t in txt if re.match(r"^DN\d+", t, re.I)), "")
            nums = [_num(t) for t in txt if _num(t) is not None]
            total = nums[-1] if nums else None
            if not spec or total is None:
                continue
            rows.append({"page": pageno, "name": name, "spec": spec, "total": total})
    return rows


def main():
    apply = "--apply" in sys.argv[1:]
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    grows = golden["rows"]
    ocr = load_ocr_rows()

    # OCR index by (spec, total) → set of distinct literal names
    idx: dict[tuple, list[str]] = {}
    for r in ocr:
        k = (_norm(r["spec"]), r["total"])
        nm = r["name"]
        if nm not in idx.setdefault(k, []):
            idx[k].append(nm)

    mapped = ambiguous = unmatched = 0
    plan: list[dict] = []
    for r in grows:
        spec = _norm(r.get("spec"))
        total = _num(r.get("total_price_incl_tax") or r.get("total_price"))
        k = (spec, total)
        cands = idx.get(k, [])
        if len(cands) == 1:
            status, raw = "mapped", cands[0]
            mapped += 1
        elif len(cands) > 1:
            status, raw = "ambiguous", None
            ambiguous += 1
        else:
            status, raw = "unmatched", None
            unmatched += 1
        plan.append({"seq": r.get("seq"), "name": r.get("name"), "spec": r.get("spec"),
                     "total": total, "raw_name": raw, "status": status, "cands": cands})

    print(f"{'='*72}\nGolden raw_name 校准计划 (dry-run={'否' if apply else '是'}): {DOC}\n{'='*72}")
    print(f"golden 行: {len(grows)}  | mapped={mapped}  ambiguous={ambiguous}  unmatched={unmatched}\n")
    print("── 逐行计划（仅显示 name≠raw_name 或 非 mapped 的行）──")
    for p in plan:
        if p["status"] != "mapped" or _norm(p["name"]) != _norm(p["raw_name"]):
            tag = p["status"]
            raw = p["raw_name"] if p["raw_name"] is not None else f"<{tag}: {p['cands']}>"
            print(f"  seq={p['seq']:>3} {p['spec']:<7} total={p['total']:<10} "
                  f"name='{p['name']}' → raw_name='{raw}'")

    if not apply:
        print(f"\n{'='*72}\n[dry-run] 未写盘。确认后加 --apply 执行（会先备份）。\n{'='*72}")
        return

    # ── apply：备份 → 写入 → 守恒报告 ───────────────────────────────────────
    before_txt = GOLDEN.read_text(encoding="utf-8")
    bak = GOLDEN.with_suffix(".json.bak")
    bak.write_text(before_txt, encoding="utf-8")
    before_sha = hashlib.sha256(before_txt.encode()).hexdigest()

    # mutate in place: add raw_name + raw_name_status; never touch name/价格/行数
    name_changes = 0
    total_before = sum(_num(r.get("total_price_incl_tax") or r.get("total_price")) or 0 for r in grows)
    for r, p in zip(grows, plan):
        if r.get("name") != p["name"]:
            name_changes += 1  # 不应发生
        r["raw_name"] = p["raw_name"]
        r["raw_name_status"] = p["status"]
    golden["raw_name_calibrated"] = True

    total_after = sum(_num(r.get("total_price_incl_tax") or r.get("total_price")) or 0 for r in grows)

    assert name_changes == 0, "name 字段被意外修改，已中止（未写盘前请检查）"
    assert len(grows) == len(plan), "行数变化，已中止"
    assert abs(total_after - total_before) < 1e-6, "总额变化，已中止"

    GOLDEN.write_text(json.dumps(golden, ensure_ascii=False, indent=1), encoding="utf-8")
    after_sha = hashlib.sha256(GOLDEN.read_text(encoding="utf-8").encode()).hexdigest()

    print(f"\n{'='*72}\n[applied] 守恒报告\n{'='*72}")
    print(f"  备份: {bak.name}  (before sha256={before_sha[:12]})")
    print(f"  行数: {len(grows)} 不变 | name 改动: {name_changes} | 总额: {total_before:.2f} → {total_after:.2f} (不变)")
    print(f"  新增字段: raw_name + raw_name_status（mapped={mapped} ambiguous={ambiguous} unmatched={unmatched}）")
    print(f"  after sha256={after_sha[:12]}")
    print(f"  验证: python scripts/audit_golden_vs_ocr.py {DOC}")


if __name__ == "__main__":
    main()
