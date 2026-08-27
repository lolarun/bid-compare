# 44 — Compare entry and multi-round interaction

> **Status: designed and decided 2026-08-27, not built.** This is the
> frontend interaction design that design/42 §7.2 deferred ("Project list
> page owns creation and entry; the workspace no longer invents projects" —
> four lines, no design). Backend P0/P1 of design/42 are live and this
> document designs against their real API surface, read from code. §7
> records the user's decisions (2026-08-27); the most consequential one is
> D-2: **design/42 P2 is pulled forward and built first**, so no part of
> this UI ships in a crippled transitional state — see the revised §6.

## 1. Trigger

User, 2026-08-27:

> 对于多轮报价，并没有修改交互部分。比如进入招标比价，应该看到的是项目列表
> （由项目管理员创建好），进入项目上传多轮报价，可以明确是否创建新一轮的比价，
> 可以更新最新一轮的报价——这些都没有规划设计。

Three interaction gaps, all real:

1. **Entry**: the sidebar's 「招标比价分析」 lands on an *empty workspace*, not
   a project list. Projects are supposed to be created by the project-management
   department (design/42 §1 req 1, §8 D1) and picked by tender staff — today the
   workspace lazily invents them instead.
2. **New round vs. current round**: uploads silently attach to the open round
   (backend `get_or_open_round` auto-creates round 1). There is no UI to see
   which round is receiving uploads, no way to open round 2, and no moment
   where the user *states* "this is a new round" versus "this replaces a quote
   in the current round".
3. **Updating the latest round**: re-uploading a supplier's corrected quote is
   a normal event (问询函 responses arrive mid-round). The supersede mechanics
   exist in the backend; the UI never distinguishes "replace within this round"
   from "this starts a new round".

## 2. What exists today (read from code, 2026-08-27)

| Fact | Location |
|---|---|
| Sidebar entry `/workspace/:projectId?` — no projectId renders an empty workspace | `router/index.ts:140` |
| Workspace lazy-creates a project on first drag-in, placeholder name 「新比价项目-<timestamp>」; a 2026-08-21 fix stopped *page-open* auto-creation after 23 empty shells accumulated | `WorkspaceView.vue:69-82,161-164` |
| `/projects` (数据管理 → 项目管理) already has create/edit/delete with modal | `views/projects/IndexView.vue` |
| Round CRUD API live: `GET/POST/PATCH /api/projects/{id}/quote-rounds`, plus `GET …/current` (read-only, explicitly does **not** auto-create — built for a round selector that doesn't exist yet) | `routes/quote_rounds.py` |
| At most one `open` round per (project, category); opening a new round closes the previous; round 1 auto-opens on first quote confirm | `quote_round_service.py` |
| `is_final_basis` is explicit-only (design/42 D3); no consumer wired yet (P2) | same |
| **Latent hazard**: `/tender-list/match` wipe-and-rebuild is not round-scoped — running match after opening round 2 destroys round 1's alignment result | design/42 §4.1, `anchor_match.py` |
| Re-upload of the same file bypasses recognition via job idempotency; a manual 「重新识别」 escape hatch exists per file card | `document_ingestion.py::create_job(force=)`, `WorkspaceView.vue` |

## 3. Entry: the compare project list

### 3.1 Route and placement

`/workspace` (no projectId) stops rendering an empty workspace and renders a
**project list screen** instead. The sidebar entry 「招标比价分析」 is unchanged;
what changes is what "no project yet" looks like. `/workspace/:projectId`
continues straight into that project's workspace — deep links and the
`/compare/*` legacy redirects keep working.

This list is **not** a duplicate of `/projects` (项目管理). The two answer
different questions:

| | `/projects` 项目管理 | `/workspace` 比价入口列表 |
|---|---|---|
| Question | "维护项目主数据" | "我要对哪个项目做比价" |
| Operations | create / edit / delete | open + create (D-1: 新建项目 available to all compare roles until P3 restricts it to 管理员) |
| Columns | name, code, created_at | name/code + **比价状态**: 品类, 轮次 (第N轮·收集中/已关闭), 已确认供应商数, 定标基准轮, 最近活动 |
| Group | 数据管理 | 业务功能 |

### 3.2 List content

Each row: project name/code · category chips · current round chip
(「第2轮 · 正式 · 收集中」, or 「无轮次」 for a fresh project) · confirmed
supplier count · 基准轮 badge when a round carries `is_final_basis` ·
last-activity time · one primary action 「进入比价」.

Empty state (no projects at all): a 「新建项目」 action right there (D-1) —
the same create modal 项目管理 uses, not a new form. When P3 lands, this
button becomes 管理员-only and the empty state for non-admins changes to
"请联系项目管理员创建项目".

**Backend gap (small)**: the list needs per-project round/supplier aggregates.
`GET /api/projects` returns master data only; per-project N+1 calls to
`/quote-rounds` won't scale past a handful. Add one aggregate endpoint
(`GET /api/compare/projects-overview`) that joins projects × quote_rounds ×
submission counts — read-only, one query, no new writes.

### 3.3 Retiring workspace lazy-creation

Once the entry list exists, the workspace's lazy project creation
(`ensureProject`, placeholder 「新比价项目-<timestamp>」) is retired: the
workspace is only ever entered *with* a projectId. This is the design/42 §7.2
sentence ("the workspace no longer invents projects") made concrete. The
metadata *backfill* (recognized project name auto-fills an empty name field)
stays — it corrects a name, it no longer creates an entity.

Pending D-1: whether 比价员 can still create projects anywhere before P3's
role gate lands.

## 4. In-project round interaction

### 4.1 The round bar

Workspace header (below the project name row, scoped to the current category)
gains a **round bar**:

```
[第1轮 询价 · 已关闭]  [第2轮 正式 · 收集中 ●]   [+ 开启新一轮]     ⚑ 定标基准: 未设置
```

- One chip per round, newest last; the open round is visually current and is
  where uploads land. Clicking a closed round switches the workspace into
  **read-only view of that round** (§4.4).
- The bar reads `GET /quote-rounds` + `/current`; a fresh project shows
  「首轮将在首次确认报价时自动开启」 rather than forcing a ceremony before the
  first upload — round 1 stays implicit (matches backend `get_or_open_round`),
  only round 2+ requires an explicit act.
- Chip context menu (open round only): 重命名 · 关闭本轮 · 设为定标基准.
  Closed rounds: 设为定标基准 only. All map 1:1 onto the existing `PATCH`.

### 4.2 「开启新一轮」 — the explicit moment

Opening a new round is the deliberate act the user asked for. Modal:

- **轮次名称** (required free text — design/42 req 3: staff name rounds
  themselves; placeholder suggests 「第N轮」)
- **阶段**: 询价 (pre_tender) / 正式 (formal)
- **备注** (optional)
- Consequence statement, verbatim on the modal: 「开启后：第N轮将关闭，此后
  上传的报价全部归入新一轮。已关闭轮次的报价与结果保留，可随时查看。」

Confirm calls `POST /quote-rounds`. No upload path ever creates round ≥2 as a
side effect — if a user drags files while intending a new round but hasn't
opened one, the files land in the current round, and the round bar makes that
visible before 确认入库 (the cards show 「将归入：第2轮 正式」).

**P2 ordering (D-2 decision)**: the match-wipe hazard (§2 last row) is
resolved by **building design/42 P2 first**, before any of this UI ships —
so 开启新一轮 is never disabled and never carries a data-loss warning. There
is no transitional state to design.

### 4.3 「更新本轮报价」 — replace, not append

Within the open round, a supplier sending a corrected quote is handled on
that supplier's existing card, not by re-dragging into the generic drop zone:

- Card action 「更新报价文件」 → file picker → new submission for the same
  supplier in the same round; the previous one is superseded (existing
  backend semantics). The card shows 「已更新 · 替换 <time> 的版本」.
- If the user *does* drag a duplicate-supplier file into the drop zone while
  that supplier already has a confirmed submission in the open round, the
  attribution step (design/28) already detects the supplier match — add one
  question at that moment: 「<供应商> 在本轮已有报价：替换本轮报价 / 这是新
  一轮的报价（需先开启新一轮）/ 取消」. This is the single place the
  new-round-vs-update ambiguity actually arises, so this is where the system
  asks — once, concretely, with the file in hand — instead of a standing mode
  switch somewhere in settings.
- 「重新识别」 (the existing escape hatch) is unchanged and orthogonal: same
  file, re-run recognition; 「更新报价文件」 is a different file, same round.

### 4.4 Closed rounds are read-only

Selecting a closed round shows that round's suppliers, matrix and export
exactly as they were — uploads disabled, 确认入库 hidden, a persistent banner
「正在查看已关闭的第N轮 · 只读」 with a one-click return to the open round.
Reproducibility of a closed round is the point of rounds (design/42 §3.1);
the UI must not offer any mutation on one. (Reopening a closed round is
design/42 §9's open item — not offered in v1.)

**P2 dependency, resolved by ordering (D-2)**: truly per-round matrix display
needs alignment scoped by `round_id`. P2 is built first, so the closed-round
view ships with its historical matrix from day one — the transitional
"recognition results only" state this section previously had to design is no
longer needed.

### 4.5 定标基准 (is_final_basis)

Set explicitly from a round chip's menu (D3: never automatic). The header
flag shows which round carries it. When P2 wires consumers, official export /
evaluation / recommendation read only this round; the UI surfaces the refusal
state the backend already defines ("no basis round set → no official result")
as a callout on the export/evaluation buttons rather than a silent failure.

## 5. Trend view (P2, interaction sketch only)

A 「轮次趋势」 tab appears beside the matrix once ≥2 rounds have aligned
results. Per design/42 §6 and R3/R4: supplier-level discount lines; per-row
round columns keyed by `anchor_uid`; every non-comparable pair renders 「不可
比 (口径不同)」 and every absent supplier 「未参与」— never a zero, never an
interpolated number. Detailed layout deferred until P2's data shape is real.

## 6. Phasing (revised per D-2: backend P2 first, then one frontend batch)

| Phase | Scope | Depends on | Notes |
|---|---|---|---|
| **B0** | design/42 P2, pulled forward: `BidAlignmentGroup.round_id` + `anchor_uid`, round-scoped wipe/rebuild in `anchor_match`, `record_submission_scope` → `QuoteRound`, `used_submission_ids` gates re-pointed, `round_trend` service + API, migration backfill | design/42 P0/P1 (live) | The delicate part (design/42 §4.1 blast-radius warning stands: `anchor_match.py` and `routes/analysis.py` are large, heavily-tested files) |
| **F1** | §3 project-list entry + `projects-overview` endpoint + retire lazy-create; §4.1 round bar; §4.2 new-round modal; §4.3 update-quote flow; §4.4 read-only closed rounds **with** historical matrix; §4.5 basis-round consumer gates; §5 trend tab | B0 | One coherent frontend delivery, no crippled intermediate states |
| **F3** | Creation restricted to 管理员 (entry list + 项目管理), `created_by_user_id` | design/42 P3 | Role plumbing only; the D-1 button flips to admin-only here |

## 7. Decisions (taken 2026-08-27)

- **D-1 — Project creation before P3: offer it.** The entry list carries
  「新建项目」 for all compare roles (same modal 项目管理 uses); when P3
  lands the button becomes 管理员-only. User chose availability over
  born-in-target-shape; the cost is one behavior change at P3, accepted.
- **D-2 — Pull design/42 P2 forward and build it first.** No disabled
  buttons, no red-warning transitional state: the match-wipe hazard is fixed
  at the root before any round UI ships. Longest path, zero crippled states.
  This reorders design/42 §7.4's plan: P2 stops being "not started, later"
  and becomes the immediate next implementation step (B0 above).
- **D-3 — Round bar follows the workspace's existing category context.** No
  new switcher; switching category (where the workspace already has that
  concept) switches the round bar with it. Single-category projects — the
  common case — see zero extra UI.

## 8. Out of scope

- Reopening closed rounds (design/42 §9, still undecided).
- Retro-alignment UX for pre_tender rounds (design/42 D4 — needs P2 first).
- Any change to recognition, alignment, or matrix semantics — this document
  is interaction only.
- `docs/design/06-functional-design-v2.md` is a frozen May-2026 audit record
  (its F4/F6 predate the anchor flow entirely) and is deliberately not
  updated; the numbered design docs are the living functional spec, and this
  document plus design/42 are the functional design for multi-round compare.
