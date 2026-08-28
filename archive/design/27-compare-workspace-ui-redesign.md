# 27 — Compare Workspace UI Redesign (supplier-axis workspace, prototype-first)

> **Status — CONFIRMED 2026-08-13, all user decisions resolved, ready for
> implementation (single-batch delivery per decision D3).**
> Trigger: after the design/24 bridge fixes landed, the user's verdict was that
> patching is over — "现在的界面有点陷入到AI的自以为是了…针对UI交互做一次整体的
> 重新设计，就从招标比价开始". This document supersedes the **frontend flow
> portions of design/24** (its second supersession; design/24's backend assets
> B0-B4 are all retained and consumed here). Recognition-engine work (design/25/26)
> is untouched.
>
> Grounding inputs, in priority order:
> 1. The user's original prototype screenshots (supplier-tab workspace — the
>    form baseline for this redesign, not a reference);
> 2. Four screenshot-feedback items, 2026-08-13 (project auto-fill / tender
>    result partitioning / supplier cover meta / Excel-like grid);
> 3. Manual-test feedback #1-#9 (2026-08-12) and the follow-up
>    (2026-08-13, `docs/项目资料/用户反馈/`);
> 4. design/24's confirmed gate semantics (unchanged: BLOCKED never enters
>    official data; pending never enters totals).

## 1. The root reframe: whose mental model organizes the screen

Every piece of feedback this round points at one mismatch:

| What got built (system's model) | The user's model (prototype) |
|---|---|
| Pipeline steps are the navigation axis (5-step wizard) | **The comparison project is the container; suppliers are the axis** (A/B/C/D tabs, per-tab completion badges) |
| Create the project first, then feed documents | **Documents come in, information grows out of them** (project name/code, supplier name, declared total auto-filled from covers; hand-editing is the fallback, never the first step) |
| Recognition output rendered as form-per-cell | **A quote is a spreadsheet** (Excel-like grid, familiar editing) |
| System interrupts: banners, toasts, native dialogs, internal metric names | Issues sit quietly *on the data* (cell coloring + badge counts); the user looks when they choose to |

## 2. Four design red lines (acceptance criteria, all screens)

1. **Never ask the user for information the system already has.** Any field
   the user must type that a document already stated is a defect.
2. **The system states facts and locations; it never pushes judgments.**
   Zero popups, zero banners, zero toasts for quality findings. Exactly two
   hard gates exist — per-supplier 确认入库 and global 导出 — and when a gate
   blocks, it lists its reasons *in place* with per-item jumps.
3. **All copy is business language.** "第69行没读到数量", never
   `no_seq_rows=132`. (`doubtCopy.ts` translations are the vocabulary source.)
4. **Grid interaction follows Excel conventions** — copy/paste (Excel
   clipboard round-trip), arrow-key navigation, batch edits, IME-friendly.

Progress corollary of red line 2: **every percentage must derive from a real
signal** (bytes, pages, elapsed/expected time). Where no signal exists, show
honest elapsed/typical text instead of inventing motion (§6).

## 3. Information architecture

Two views, freely switchable — not wizard steps (user decision D1):

### 3.1 Workspace view (parse/entry/result — one scrolling page, per prototype)

```
┌─ Project header ────────────────────────────────────────────┐
│ 华泾镇D5B-1电缆比价 (auto-filled from tender cover, editable) │
│ 编号 XHPO-0001 · 电缆 · 4家投标      [对齐核查] [历史] [导出] │
├─ Materials strip (collapsible) ─────────────────────────────┤
│ [招标文件 ✓] [采购清单 Excel·Sheet切换 ▾] [+ 拖入投标文件]     │
│ per-file: name + one-line stage + per-stage progress (§6)    │
├─ Tabs ──────────────────────────────────────────────────────┤
│ [清单·92] [宏胜·132 ●3] [远东 ●1] [浦东] [亨通] [+添加]        │
├─ Tab content (supplier tab) ────────────────────────────────┤
│ Supplier card: 名称/声明总价/税率口径 — each value labeled     │
│   with its source ("识别自P1"), editable       [确认入库]     │
│ Univer grid: recognition rows straight into the sheet;       │
│   problem cells colored (missing qty=red, arithmetic=yellow, │
│   truncation=orange), hover shows plain-language explanation │
├─ Result section (below tabs, per prototype) ────────────────┤
│ [开始比价分析/重新分析] → summary stat cards + 横向对比矩阵     │
│   (BidMatrix.vue reused; pending cells excluded from totals) │
└─────────────────────────────────────────────────────────────┘
```

- **Project bootstrap (feedback #1)**: the entry point is one "新建比价"
  button → empty workspace → dropping the first document back-fills project
  name/code/date from cover scalars (editable). Manual creation remains as
  the fallback, not the gate.
- **Tender artifact partitioning (feedback #2)**: the tender-file card
  expands into three independent blocks — procurement list ("正文无清单，
  请上传 Excel 附件" + highlight the list slot when absent), cover info,
  brand-requirement table. A blanket "识别结果为空" is forbidden: each
  artifact reports its own presence.
- **Supplier cover meta (feedback #3)**: the supplier card's data source is
  the quote-side cover-meta extraction (§7 backend item — currently missing
  on the Paddle path; restoring it also revives the declared-total checksum
  gate, which is silently unfed today).
- **List tab (user decision D2)**: **read-only in v1** + "重新上传" for
  corrections (axis stability: hand-editing the anchor axis mid-comparison
  would silently invalidate alignment work). **Conditional clause, explicit:**
  read-only is acceptable *only while* alignment review never dead-ends on a
  list-side error — §3.2 guarantees every row has a terminal action that does
  not require editing the list. If manual testing surfaces a case where a
  list error blocks alignment resolution, list editing gets scheduled
  (inline edit with session re-version, or fast re-upload) — this clause is
  the trigger, recorded here so it isn't relitigated from scratch.

### 3.2 Alignment review view (separate, user decision D1)

`AnchorReviewMatrix` becomes its own routed view, entered from the workspace
header button; a persistent "返回工作台" switch goes back — free navigation
both directions, no step ordering, no state loss on switching. Per-row
resolution actions must be **terminal without list editing**: align to
another anchor / pending / exclude / missing-ack (design/23). A visible
"清单有误？重新上传" escape hatch covers the anchor-side-wrong case
(re-match runs after re-upload; finalization invalidation semantics
unchanged).

## 4. Quality findings presentation (replaces design/24's inbox stage)

The "doubt inbox" as a *separate stage* is retired. The same aggregation
(`useDoubtInbox`, dry-run collector, reconcile diffs, pending counts) now
feeds:

- **badge counts** on supplier tabs (宏胜 ●3) — click scrolls the grid to
  the first problem row;
- **cell coloring + hover copy** inside the grid (`doubtCopy.ts` vocabulary);
- **gate-time listings**: 确认入库 / 导出 blocked → reasons listed in place
  with jumps.

Nothing else. No auto-shown banners (the bridge round's auto-precheck banner
is removed; the dry-run still runs — its result renders as badge numbers).

## 5. Grid component: Univer

Selected: **Univer** (Apache-2.0, Luckysheet successor, active, zh ecosystem,
true spreadsheet semantics). Rejected: Handsontable (non-commercial license),
AG Grid Community (data grid, not a spreadsheet; weaker Excel feel).
Verification bar before committing the integration: Excel clipboard
round-trip works; 300+ rows fluent; per-cell validation coloring API
sufficient for the §4 marks; Chinese IME entry clean. If Univer fails the
bar during implementation, fall back to AG Grid Community and record why.

## 6. Progress presentation spec (feedback #3/#4 of 2026-08-12, re-confirmed 2026-08-13)

The current 8-chip strip renders **qwen-era stage names hardcoded in the
frontend** — the Paddle backend never emits them, and the long cloud stage
emits nothing between 20%→60%, reproducing "stuck at 20%". Replacement:

| User-facing stage | Real signal | Form |
|---|---|---|
| ① 上传 | browser byte progress | true % |
| ② 识别内容 (dominant, 20-90s) | none from Baidu poll (`status` only) → **elapsed ÷ expected**, expected = page_count × per-page constant derived from P2 timing (11p≈20s … 53p≈85s) | estimate bar capped at 95% + "11 页 · 已进行 24 秒 / 预计约 30 秒" |
| ③ 提取信息 | real sub-steps (cover meta, requirements) | quick advance |
| ④ 整理完成 | local parse | instant |

Backend: `submit_and_parse` gains a poll-progress callback feeding the
already-shipped `stage_current/stage_total` fields (design/24 B2); the
per-page expected-duration constant goes to `domain_config.py` with its
derivation comment. Both quote and tender paths. Stage names carry no engine
jargon ("提交 PaddleOCR-VL 识别" → 识别内容).

## 7. Backend items folded into this round (small, all serve the UI)

1. **Quote-side cover meta extraction on the Paddle path** (supplier_name /
   declared_total via `paddle_doc_meta` text call over `page["text"]` —
   the qwen-era `PROMPT_QUOTE_META` capability that the engine cutover
   dropped). Restores the silently-unfed declared-total checksum gate.
   This is the supplier card's data source, not a separate patch
   (user decision: no isolated short-term fixes).
2. Poll-progress callback + expected-duration constants (§6).
3. Tender recognition response: ensure the three artifacts (list / cover /
   brand table) are independently addressable by the frontend (they exist
   in the draft meta today; verify the route response exposes them
   distinctly).

## 8. Asset inventory

- **Untouched**: Paddle engine + adapters, four quality gates, dry-run
  collector + cache, multi-sheet, copy dedup, B2 stage fields, all of
  design/26.
- **Adapted**: `doubtCopy.ts` (→ cell hover copy), `useDoubtInbox` (→ badge
  counts), `useSupplierUpload` (→ materials strip), `BidMatrix.vue` (→
  result section), `AnchorReviewMatrix.vue` (→ standalone view).
- **New**: Univer integration + coloring layer, supplier-tab workspace
  shell, project bootstrap flow, tender artifact partition cards, §7 backend
  items.
- **Retired**: 5-step wizard, 8-chip strip, batch-card flow, all
  window.confirm/alert, design/24's standalone inbox stage, bridge-round
  auto-precheck banner.

## 9. User decisions log (2026-08-13)

- **D1**: Alignment review is a **separate view**, freely switchable with
  the workspace (original prototype had entry+result in one page; review is
  the one thing that leaves the page).
- **D2**: List tab **read-only for now**, with the explicit conditional in
  §3.1 (read-only must never block alignment resolution; if it does,
  editing gets scheduled).
- **D3**: **Single-batch delivery** — build the whole redesign, then manual
  testing drives the next iteration (速战速决; no mid-way preview round).
- Prior confirmations inherited: gate semantics unchanged; supplier-name
  conflicts never interrupt (design/24 D-round); progress must be honest (§6).

## 10. Delivery plan (single batch, internal order)

1. §7 backend items (meta extraction + checksum revival + poll progress);
2. Univer integration + one supplier grid against real 宏胜 data;
3. Workspace shell (header + materials strip + tabs + project bootstrap);
4. Tender partition cards + gate-time blocked-listings; alignment review as
   standalone view;
5. Retire the wizard + route redirects + prj2 full-flow regression
   (the manual-test dataset is the acceptance dataset).

Estimate 4-6 working days. Full backend suite + vue-tsc + vitest green per
commit; browser verification against the running dev servers before报告完成.

## 11. Out of scope

- Invite (邀标) page redesign — later round reuses the same components.
- Recognition accuracy work (design/26 P2 gates) — separate track.
- List-tab editing (D2's conditional trigger only).
