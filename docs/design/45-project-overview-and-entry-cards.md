# 45 — Project overview page and compare-entry cards

> **Status: designed 2026-08-30, not built.** Continues the numbered series
> archived under `archive/design/` (last: `44-compare-entry-and-round-interaction.md`,
> now shipped). Per `CLAUDE.md` §6, `docs/spec/FUNCTIONAL.md` and
> `docs/spec/TECHNICAL.md` are updated **when this is implemented**, not now.
> §8 records the user's three decisions (2026-08-30).
>
> Every "what exists today" claim in §2 was read from code on 2026-08-30, not
> from a design-doc banner. §3's constraints are the reason this design is
> narrower than the trigger asked for; read them before proposing changes.

## 1. Trigger

User, 2026-08-30, on the end-to-end tender-comparison flow:

> 1. 项目列表的项目卡片需要重新设计，例如在右侧显示当前是第几轮报价
> 2. 项目详情页面，进入项目详情页面要可以看到项目概述，包括以下内容：
>    采购概述、采购清单；供应商清单，以及当前报价轮次，每轮的报价清单及比价建议；
>    要有当前的比价建议

Two asks. The first is **already built and mislocated**; the second is a real
gap. §2 and §4 separate them.

## 2. What exists today (read from code, 2026-08-30)

| Fact | Location |
|---|---|
| Entry cards **already render** `第N轮 · 收集中`, `已确认 N 家`, `定标基准：第N轮` | `EntryView.vue:148-158` |
| `GET /api/projects/overview` already batch-aggregates rounds per (project, category), explicitly avoiding N+1 | `routes/projects.py:73` |
| Card right side holds only a date + 「进入比价」 link; all round state is bottom-left | `EntryView.vue:159-166` |
| `/workspace/:projectId` lands directly on the upload dropzone — no project-level landing surface exists | `WorkspaceView.vue:1500` |
| `WorkspaceView.vue` is **2120 lines** holding upload + preview + compare + round bar + doubt inbox in one component | — |
| `viewMode: 'overview' \| 'detail'` **is already taken** — it means "file-card overview vs. single-file detail", unrelated to a project overview | `WorkspaceView.vue:741` |
| `get_evaluation_policy(project_id)` returns `UNKNOWN_EVALUATION_POLICY` for **every** project (method/award_mode unknown, `final_decision_requires_committee=True`) — deliberately, until tender-document policy persistence exists | `services/matrix/evaluation_policy.py` |
| Three-state `recommendation_level` (firm/conditional/blocked) is produced by `_compute_recommendation`, only as a by-product of computing a full matrix | `services/matrix/bid_recommendation.py:157` |
| Closed-round viewing calls `POST /bid-matrix` with `round_id` — the code comment says 「拉那一轮自己**冻结的**矩阵」 but the implementation **recomputes**; only the `BidAlignmentGroup` set is round-scoped (migration `0011`) | `WorkspaceView.vue:210`, `anchor_match.py:1051` |
| `close_round()` flips `status`/`closed_at` only — it stores **no matrix snapshot** | `quote_round_service.py:125` |
| `BidMatrixVersion` already stores a full `matrix_json` + `readiness_json` + `recommended_supplier` + approval state — but has **no `round_id` column** and 0 rows in the live DB | `models/bid_matrix_version.py` |
| Live DB holds **146 projects, 61 of them auto-generated empty shells** named `新比价项目-<timestamp>` (ids 97–158) | `data/mempas.db`, measured 2026-08-30 |

### 2.1 A finding recorded so it is not lost

The 「frozen matrix」 comment at `WorkspaceView.vue:210` **does not describe the
implementation**. Historical rounds are recomputed on read, so a closed round's
numbers can drift when the procurement list is revised, suppliers are merged, or
the historical-price baseline changes. Today that is tolerable (occasional
look-back at a matrix). It would **not** be tolerable if historical rounds
carried standing conclusions — which is exactly what §8's decision D-2 avoids.
Fixing it properly means adding `round_id` to `BidMatrixVersion` and snapshotting
on `close_round()`; that is **deliberately out of scope here** (§9), recorded so
the next person does not re-derive it.

## 3. Hard constraints

These are not preferences. Each one kills an otherwise-obvious design.

**C1 — The overview may not recommend a winner.** `EvaluationPolicy` is
`UNKNOWN` for every project because the evaluation method must come from the
tender document, and the system may not invent one (`CLAUDE.md` §4;
`.claude/rules/bid-compare-backend.md`). The overview's 「比价建议」 is therefore
bounded to: **evaluated-total ranking** (labelled 评标总价排名, never "official
reasonable-low-price scoring"), the three-state `recommendation_level`, evidence
gaps, and risk notes. An overview page is read as a conclusion more readily than
a matrix is — so this constraint binds *harder* here, not less.

**C2 — The unit is (project × category), not project.** Rounds, anchor axes and
matrices are all keyed by `(project_id, category)` (`design/42` D2). A project
overview that prints one round number lies about a multi-category project. This
is what decision D-1 (one status card per category) resolves.

**C3 — Preview-lane results may not appear as conclusions.** `axis_kind='quote_derived'`
is schema-forbidden from pairing with anything but `basis='preview'`
(`BidMatrixResult._quote_derived_axis_is_preview_only`). The overview shows
official-lane results only; a category with no confirmed procurement list shows
its state honestly and links to preview, rather than borrowing preview numbers.

**C4 — One business result.** Pages, exports and AI explanations must consume
the *same* business-service result (`CLAUDE.md` §4). The overview's
recommendation card therefore calls the **same** `POST /api/analysis/bid-matrix`
the matrix page calls. It must not gain a cheaper second implementation of
"roughly who's winning" — that is precisely how two divergent semantics get born.

**C5 — Naming.** `viewMode: 'overview' | 'detail'` is taken (§2). The new
surface is named **`ProjectOverview`** / route segment `overview`; no existing
identifier is repurposed.

## 4. Entry cards (the user's ask #1)

### 4.1 What actually needs fixing

Round display is built. Three real defects remain:

1. **Layout inversion** — round state sits bottom-left in small grey text while
   the visually dominant right column holds only a timestamp.
2. **No next action** — the card says where the project *is*, never what to *do*.
3. **Signal drowned by noise** — 61 of 146 projects are empty auto-created
   shells. In the screenshot the one real project renders the empty-state line
   while eight junk projects render full round state.

### 4.2 Card layout

Right column becomes the status column, one row per category (C2):

```
┌────────────────────────────────────────────────────────────────────────┐
│ ▣ 徐汇区华泾镇项目                          阀门   第2轮·收集中          │
│   XHPO-0001                                       4 家已入库 · 待复核 2 │
│                                            电缆   第1轮·收集中          │
│                                                   清单未确认            │
│   最近活动 2026-08-26 15:01                              进入比价 ›     │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Next-action label (deterministic, no new data)

Derived from fields `GET /api/projects/overview` already returns, plus two cheap
additions (`has_confirmed_list`, `pending_review_count`). First match wins:

| Condition | Label |
|---|---|
| no round and no submission | `待上传报价` |
| submissions exist, no confirmed procurement list | `清单未确认` |
| confirmed list, submissions awaiting review | `待校对入库 N 份` |
| all confirmed, no `is_final_basis` round | `可出比价` |
| `is_final_basis` set | `已定标基准：第N轮` |

The label is a **state readout, not advice** — every branch is a fact already in
the database. No model call, no heuristic.

### 4.4 Default filter (decision D-3)

The compare-entry list hides **empty projects** by default, with a
`显示空项目` toggle that is off by default and not persisted.

"Empty" is defined **semantically, not by name**: no `QuoteRound`, no
`BidSubmission`, and no `TenderListSession` for that project. Matching on the
`新比价项目-<timestamp>` name pattern is rejected — a user may legitimately name
a project that way, and a differently-named empty shell would still slip through.
Emptiness is the property that actually makes a card useless.

The filter is applied in `projects_overview` (server-side), so pagination counts
stay correct. `/projects` (数据管理 → 项目管理) is **unchanged** — it is the
master-data surface and must keep showing everything.

## 5. Project overview page (the user's ask #2)

### 5.1 Route and shape

- `/workspace/:projectId` → renders **ProjectOverview** (new landing).
- `/workspace/:projectId/compare` → the existing three-stage workspace, unchanged.
- `/workspace/:projectId/align` → unchanged (`AnchorReviewMatrix`).

The overview is **read-only**. Every action links out to the surface that already
owns it. This is the single most important structural rule here: `WorkspaceView.vue`
is already 2120 lines, and an overview that grows write actions becomes a second
one within a release.

### 5.2 Sections

**A. 采购概述** — project name, code, tender unit (招标单位, distinct from
project name), detected categories, confirmed-list status per category, upload
count. Fields that were never extracted render as 「未识别，点击填写」; fields the
source document genuinely lacks render as a static annotation and do not nag —
the existing distinction at `WorkspaceView.vue::fieldSourceLabel()` is reused,
not reinvented.

**B. 采购清单** — per category: anchor count, confirmed version, source type
(excel/pdf), confirm time and confirmer, and brand requirements when the list
came from a tender PDF. Links to the list-detail surface; does not re-render the
whole list.

> **Corrected 2026-08-30 during implementation.** This section originally said B
> should show 明细合计 vs 文件声明总价. It cannot: `TenderAnchor`
> (`services/tender/tender_list.py:53`) carries **no price fields at all** —
> seq/name/spec/model/pressure/materials/unit/qty/brand/profession/remark. That
> is correct by design (a procurement list is a blank form bidders fill in; the
> 金桥 list's three price columns are literally all-zero, see the fixture
> MANIFEST). The two-totals rule is a **quote** concern and moves to section C.

**C. 供应商与轮次** — one card per category (D-1):

```
阀门 · 第2轮（收集中）· 定标基准：未设定
  凯硕新正   已入库  89 行   checksum 通过
  上海绵存   已入库  87 行   ⚠ 2 行结构缺口 → 去复核
  泰科龙     待校对  —       → 去校对
  ─────────────────────────────────────────
  第1轮（已关闭）  3 家报价    查看报价清单 ›
```

Current round shows, per supplier: row count and the two totals **kept
separate** — 明细合计 (`SUM(bid_quote_lines.total_price)`) vs. 文件声明总价
(`ExtractionJob.result._doc_meta.bid_total`, via the shared
`quote_confirmation_service._declared_total` reading). `FUNCTIONAL.md` §5
already requires this separation; an overview page is the most tempting place to
merge them into one headline number, so it is called out explicitly. Both are
cheap aggregates — no alignment run.

**Gate results are *not* in section C.** `QuoteReadiness`
(`services/submission/quote_readiness.py`) needs `stats` from `import_and_match`,
i.e. the full alignment pass. Checksum verdicts, pending-row accounting and
ranking therefore live in section D, which is lazy. Section C stays a cheap
"who submitted what" readout; §5.3's strip links to the doubt inbox for gates.

Closed rounds show **the quote list only** (supplier, row count, submitted-at) —
no totals, no ranking, no recommendation (D-2, and see §2.1 for why).

**D. 当前轮比价建议** — lazy-loaded, official lane only, and only for a category
whose axis is `tender_anchor` (C3). Renders:

- `评标总价排名` — evaluated totals, explicitly labelled as a price ranking, not
  an official evaluation score.
- `recommendation_level` banner: firm / conditional / blocked, each with its
  reason.
- Excluded-from-total accounting: pending rows, unquoted anchors, rows with no
  reliable price basis — **with their monetary impact**, not merely counted.
- 「综合评审待评标小组确认」 whenever non-price factors carry no weights (C1) —
  which, given `UNKNOWN_EVALUATION_POLICY`, is currently always.

If the category has no confirmed procurement list, this card renders a single
line stating so and links to preview — it never borrows preview numbers (C3).

### 5.3 Top status strip

One line above the sections, every segment a link:

```
清单已确认 89 项 · 4 家已入库 / 1 家待复核 · 2 处结构缺口 · 第2轮收集中
```

This directly discharges two items from the 2026-08-12 manual-test feedback that
have never been scheduled: **#5** (quality-gate messages show internal metric
names, unreadable and with no entry point) and **#7** (structural-integrity
errors offer no "where do I go to fix this" link). Copy goes through the existing
`utils/doubtCopy.ts::translateReason` mapping — no second phrasing table.

### 5.4 Explicitly not on this page

- **No AI summary card.** `bid_insight` already renders above the matrix in the
  formal lane. A second placement is a second semantics waiting to drift (C4).
- **No round-trend chart.** `RoundTrendPanel.vue` exists; the overview links to it.
- **No write actions** (§5.1).

## 6. API

One new endpoint plus reuse. No existing response shape changes.

**New — `GET /api/projects/{project_id}/overview`** (cheap, no matrix computation):

```jsonc
{
  "project": { "id", "name", "code", "tender_unit" },
  "categories": [{
    "category": "阀门",
    "axis_kind": "tender_anchor" | "quote_derived" | null,
    "list": { "confirmed": true, "anchor_count": 89, "version": 3,
              "detail_total": 1070234.50, "declared_total": 1070234.50,
              "confirmed_at": "..." },
    "current_round": { "id", "seq", "status", "stage" },
    "rounds": [{ "id", "seq", "status", "is_final_basis",
                 "submissions": [{ "submission_id", "supplier_name",
                                   "line_count", "submitted_at" }] }],
    "suppliers": [{ "submission_id", "supplier_id", "supplier_name",
                    "status", "line_count", "gate_flags": [] }],
    "next_action": "待校对入库"
  }]
}
```

Aggregate in one batched pass, same discipline as `projects_overview` (§2) —
no per-category round-trip.

**Reused unchanged**: `POST /api/analysis/bid-matrix` (section D, lazy, C4),
`GET /api/analysis/round-trend`, `GET /api/analysis/tender-list/current`,
`GET /api/analysis/compare-state`.

**Extended**: `GET /api/projects/overview` gains `has_confirmed_list`,
`pending_review_count` (for §4.3) and the `include_empty=false` default (§4.4).

Field naming follows the repo invariant: `submission_id` / `supplier_id` /
`anchor_id` / `material_id` stay distinct; a quote column's identity is
`submission_id`, never `supplier_id`.

## 7. Data cleanup

Live DB, measured 2026-08-30 (`data/mempas.db`): 146 projects, of which **61**
match `新比价项目-<timestamp>` (ids 97–158). Dependent rows:

| Table | Junk rows | Table total |
|---|---|---|
| `quotes` (historical price) | **0** | 8303 |
| `bid_submissions` | 16 | 119 |
| `bid_quote_lines` (via submission) | 1753 | 9463 |
| `bid_alignment_groups` | 267 | 1391 |
| `bid_alignment_items` (via group) | 801 | 1997 |
| `tender_list_sessions` | 17 | 54 |
| `quote_rounds` | 5 | 22 |
| `extraction_jobs` (via `context.project_id`) | 119 | 318 |
| `tender_documents` / `bid_matrix_versions` / `alignment_finalizations` / `anchor_missing_acks` | 0 | — |

**`quotes` junk = 0 is the load-bearing number**: it is direct evidence that the
staging-never-pollutes-master-data invariant (`CLAUDE.md` §4, `design/09`) held
across 61 throwaway projects. Deleting them cannot touch the historical price
baseline.

44 of the 61 are completely empty; 17 carry data, the largest being ids 149/152/157
(3 submissions + 89 alignment groups each).

Deletion follows `.claude/rules/database-safety.md`: backup taken **before** any
write (`data/mempas.before-junk-purge-<ts>.db`, via the SQLite backup API — a
plain file copy is unsafe while the app holds a WAL), dry-run counts published
(above), user confirms the specific figures, then a single transaction with
before/after counts. The purge is a **one-off data action, not a code path** —
no name-pattern matching ships in application code (§4.4).

## 8. Decisions (user, 2026-08-30)

- **D-1 — one status card per category**, not a category tab switcher. Resolves C2.
- **D-2 — current round shows the comparison recommendation; historical rounds
  show the quote list only, no conclusions.** Also avoids §2.1's recompute-drift
  problem without any snapshot work this round.
- **D-3 — empty projects filtered by default**, and the 61 existing shells
  deleted manually (§7).
- **D-4 — this document lands first; `docs/spec/*` is updated when the code
  ships**, per `CLAUDE.md` §6.

## 9. Out of scope

- **`BidMatrixVersion.round_id` + snapshot on `close_round()`** — the real fix
  for §2.1. Unblocked only if historical rounds ever need standing conclusions;
  D-2 says they do not, this round.
- **Weighted attribute comparison** — still blocked on data availability
  (`FUNCTIONAL.md` §12).
- **Splitting `WorkspaceView.vue`** — real debt, but this design deliberately
  adds a *sibling* route rather than touching it.
- **Persisting evaluation policy from the tender document** — the thing that
  would let C1 relax. Unchanged here.

## 10. Build order and acceptance

1. Backend: `include_empty` + two extra fields on `/projects/overview`.
2. Frontend: entry-card layout + next-action label (§4).
3. Backend: `GET /api/projects/{id}/overview` (§6).
4. Frontend: `ProjectOverview` route + sections A/B/C (§5).
5. Frontend: section D, lazy, reusing `POST /bid-matrix` (C4).
6. Update `docs/spec/FUNCTIONAL.md` §7 and `docs/spec/TECHNICAL.md` §5 (D-4).

Acceptance — each is a test, not a screenshot:

- A multi-category project renders one card per category; no single round number
  is presented as the project's round (C2).
- A project whose axis is `quote_derived` renders **no** recommendation card, and
  no preview number appears in section D (C3).
- Section D's evaluated totals are byte-identical to the matrix page's for the
  same project/category/round (C4).
- `recommendation_level='blocked'` still renders the card, explaining the block
  (per `project_baseline_recommendation_redesign` rule 3).
- Excluded rows report monetary impact, not just a count (§5.2 D).
- The default entry list omits empty projects; the toggle reveals them; pagination
  totals agree with the applied filter (§4.4).
- No application code matches on the `新比价项目` name pattern (§4.4).
