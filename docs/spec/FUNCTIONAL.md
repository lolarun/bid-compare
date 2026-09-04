# MEMPAS — Functional Specification

> Consolidated 2026-08-27 from 40 numbered `docs/design/*.md` documents plus
> `TODO.md` and `HANDOFF.md`. Each statement below is what a design document's
> own status banner marked **current truth** at consolidation time — not
> every idea ever proposed. The originals are preserved under
> `archive/design/` with their full rationale, experiment data, and
> retraction history; this file exists so an agent (or a person) can learn
> **current product behavior** without reading 40 chronological documents.
> A bracketed tag like `[design/32]` means "see the archived original for
> the reasoning and measurements behind this."
>
> For architecture / implementation, see `docs/spec/TECHNICAL.md`.

---

## 1. What the product does

MEMPAS is a bid-comparison (比价) system. The core pipeline:

```
Tender document / procurement list
  → confirm TenderAnchor (the row axis)
  → recognize N supplier BidSubmission quotes, with human review
  → align supplier rows to the anchor
  → resolve pending / missing / excluded rows
  → produce the comparison matrix, exports, and evaluation explanations
```

A comparison always has exactly one row axis, and every result states which
kind it is (`axis_kind`):

- **Anchor mode** (`tender_anchor`) — rows come from a confirmed procurement
  list. This is the default and the *only* kind official alignment,
  evaluation totals, exports, and recommendations may use.
- **Quote-derived mode** (`quote_derived`) — when no confirmed procurement
  list exists, one supplier's own item rows (the one with the most rows)
  serve as the reference axis; other suppliers align to it positionally,
  confirmed by quantity. This axis may feed **only the preview lane**, never
  an official result — it can show suppliers priced the same row
  differently, but never that a supplier omitted a tender-required item,
  because nothing states what was required `[design/32]`.

Production deployment: **`https://bid.hotcrp.cn/`** (first verified deploy
2026-08-25). Two operational gotchas: port 80 is blocked by the Aliyun
gateway (must use HTTPS), and `deploy.sh`'s health check can false-negative
(sleeps 5s before probing) — always curl `/api/health` manually after a
deploy. Full detail: `docs/DEPLOY.md`.

---

## 2. Material master data

Materials are classified in a three-level hierarchy mirroring SAP
MTART>MATKL>MATNR: **Profession** (8 defined, 4 confirmed in use: Electrical,
Plumbing & Drainage, HVAC, Fire Protection) → **Category** (阀门/桥架/etc.,
10 total, see §4) → **Sub-category**. Material coding is
`{PROF}-{CAT}-{SEQ}` `[design/01]`.

Name/spec standardization matches in priority order: exact match > rule
match > AI semantic match > manual confirmation, covering spec synonyms,
name synonyms, category synonyms, dimension format, and type synonyms
`[design/01]`.

Extended attributes are category-specific (e.g. valves carry
`valve_type`/`pressure`/`body_material`/`connection`), each attribute marked
either a **match** role (must agree for two quotes to be "the same item") or
a **difference** role (may legitimately vary, shown as a remark, not a
mismatch) `[design/01]`.

---

## 3. Supplier scoring & comparison weights

Two independent weight layers `[design/02]`:

- **Layer A — supplier composite score** ("which supplier to choose"): 4
  dimensions — price competitiveness 45%, history cooperation 25%, quote
  completeness 15%, commercial terms 15%. (A 5th dimension, brand/standard
  compliance, was removed 2026-06-06 because manual bid comparison never
  actually scored by brand tier; its 15% weight was redistributed to price
  +5% and history +5%.)
- **Layer B — per-item price deviation tolerance** ("is this quote
  anomalous"): deviation is measured against **reasonable historical low**
  (IQR-filtered minimum: `min(price where price ≥ Q1 − 1.5×IQR)`), not mean
  or median. This replaced an earlier mean/median baseline and a 4-level
  green/yellow/red/blue alarm scheme, per direct 一建 user feedback (May
  2026) — deviation is now shown as a value + 2-color flag only
  (≤5% none / 5–10% yellow / >10% red, configurable per category); Modified
  Z-score outlier detection was explicitly cancelled `[design/02][design/06-functional-design-v2]`.
- History-cooperation scoring bands: ≥5 wins → 100 pts, 3–4 → 80, 1–2 → 60,
  0 → 40 pts.
- Weights and thresholds are configurable by an administrator (must sum to
  100%; per-category deviation tolerance individually settable).

---

## 4. Procurement list / tender handling

**Category vs. profession**: category (11 defined as of `config.py`'s
`PROFESSION_MAP` — corrected 2026-08-28, was recorded as 10; 电缆/cable was
added after the 10-category dataset this section's number traces back to:
阀门, 桥架, 母线槽, 配电箱, 电缆, 不锈钢管, 水箱, 潜水泵, 风口风阀, 风机盘管,
空调泵), not profession, is what material matching and session grouping
actually use. Category is inferred from item name/content — never from the
Excel "profession" column, which frequently doesn't match the category
dropdown options `[design/07]`.

**Multi-category lists** are split into one `TenderListSession` per detected
category, each independently versioned; the UI exposes a category switcher.
Uploading/matching without an explicit prior list-confirmation
self-heals — the backend auto-creates the needed session(s) rather than
409ing `[design/07]`.

**Multi-sheet Excel**: every list-like sheet in a procurement workbook is
merged (not just the largest sheet), sheets that are entirely footer/total
rows are excluded as non-item, and original per-sheet numbering is preserved
alongside a renumbered global `seq` `[design/30][design/39]` (design/30
measured the silent-truncation defect and specified this fix; design/39
implemented it, plus the matching fix that gave the tabular/CSV quote
column table the same tax-basis roles — `unit_price_incl_tax`/`_excl_tax`,
`total_price_incl_tax`/`_excl_tax` — the PDF path already had, and gave the
xlsx tender-anchor parser the same 材质 sub-column parity the PDF path
already had).

**Text-layer PDFs** (born-digital tender documents with a usable embedded
text layer): procurement list, cover-page scalars, and brand requirements
are extracted via fast deterministic text parsing (~20–25× faster than the
vision model), with automatic, logged fallback to the vision path for
scanned PDFs or text layers that don't parse cleanly `[design/25]`.

**Closed-roster invitation** — **not built** (checked in code 2026-08-28;
the 2026-08-27 consolidation left this as "verify shipped status", so here is
the verification). `roster_mode`, `invited_suppliers`, and design/18's
`BidInvitation` shape have zero occurrences outside the design doc, and
migration `0006_procurement_case` is an explicit **tombstone** — a no-op kept
only so already-stamped databases still resolve. The `BidInvitation` model
that does exist is the older brand/supplier *recommendation* invitation
(`rank`-carrying, used by `routes/invite.py`), a different feature. Everything
below is design intent for if design/18 is ever adopted, not current
behavior: when a tender document is itself an invitation naming allowed
bidders, that named list becomes a confirmed closed roster. Automatic supplier creation is forbidden in this mode; the
bid matrix's columns are driven by roster membership (an invited supplier
with no submission shows "未响应", not absent); binding a supplier outside
the roster requires an explicit action that flips the case to an "exception"
state with an audit log entry. Public tenders / ad-hoc quotes stay in open
mode (today's default, unchanged) `[design/18]`.

---

## 5. Document upload & classification

Users drop all files at once (tender / procurement list / bid / quote list,
any mix); the system classifies each and shows the derived structure for
confirmation before commit. Classification is always editable and may
honestly say "不确定" — filename is a hint, never decisive `[design/28]`.

Classification runs a three-tier ladder, cheapest first: instant
extension/header-based rules → post-recognition cover-scalar/table-shape
signals → (residual cases only) a narrow LLM multiple-choice call. On the
measured corpus, residue reaching the LLM tier was 0, so that tier was never
built `[design/28]`.

When same-supplier duplicates exist (PDF + Excel both present), Excel is
primary and the PDF becomes a cross-check; there's no dedicated arbitration
UI — the user re-assigns on the confirmation screen if needed `[design/28]`.

Scanned PDFs are classified by a real vision-model call (live, not paused);
native PDFs route via a zero-model-call keyword judge. A genuinely uncertain
Excel currently pops a binary modal (招标文件/投标文件) — **known bug**:
dismissing the modal (Escape/mask-click) is silently treated as "投标文件"
rather than leaving the file unrouted `[design/29][design/38]`.

Each dropped file becomes one status card, cycling 分析中 → one of four
categories (招标文件/采购清单/投标文件/报价清单); the card shows unit name
(招标单位/投标单位, distinct from project name), item counts always in 项
(never 行) regardless of source format, and two separate totals — 明细合计
(computed) vs. 文件声明总价 (declared) — never merged into one number
`[design/29]`.

---

## 6. Recognition (what the system extracts from a document)

PDF recognition — for both procurement lists and supplier quotes — produces
an **unconfirmed** `ExtractionDraft` for human review; domain objects
(`TenderAnchor`, `BidQuoteLine`) are created only after user confirmation.
Recognition never writes the database directly `[design/10]`.

Every document gets a quality tier:

- **PASS** — structure/amounts/source/completeness all check out; may enter
  official alignment.
- **REVIEW** — the system pre-fills and surfaces doubts; excluded from
  official quotes, totals, and recommendations until a human confirms
  (conditional explanations allowed, final procurement confirmation is not).
- **BLOCKED** — severe page loss, no reliable structure, a key-amount
  conflict, or no valid quote at all; nothing is stored, aligned, or
  recommended.

Pseudo-complete results (silently filling in defaults) are forbidden;
page-count conservation is a hard constraint — fixed page numbers or
supplier-specific logic are never used to locate data `[design/10]`.

**Known, currently-unfixed recognition defects** (surfaced as flagged rows
in the doubt/preview UI, not silently absorbed):

- A 4-row loss of `total_price_incl_tax` on one real document (2.74%
  checksum gap, correctly still red — do not fix by loosening the
  threshold) `[TODO §-1.1]`.
- A column-shift on one row (spec text spilling into the `unit` column,
  losing `qty`) that ingests fine but silently disappears from the
  comparison matrix with no on-screen indication — "89 ingested rows, 88
  matrix rows," the missing ¥3,460 currently invisible `[TODO §-1.2]`.

**Gap-fill** (narrow, deliberate exception to "no mixed extraction within
one document"): when a table column clearly exists but the engine returned
nothing for a cell (`AMOUNT_EMPTY`), a second model re-reads just that cell.
Four conditions, all required: only genuinely empty cells (never overwrites
a present value); every filled value is tagged with its source
(`field_sources[field]="llm"`); the filled row must pass an arithmetic
identity check or the fill is discarded entirely; and quality tier is
**never** raised by a fill — a row that was REVIEW stays REVIEW
`[design/33]`. Filled amounts are excluded from the declared-total checksum,
since that checksum is supposed to be independent evidence recognition was
complete. **Known UI gap**: filled cells don't yet render visually distinct
in the grid, despite being spec'd to.

**Column-shift refusal**: when a numeric column instead holds free text (not
a number, not a "not quoted" marker like `/`/`无`), the row is flagged
`column_shift` and its `qty`/`unit_price`/`tax` fields are refused rather
than stored wrong — the `total` field is often still recoverable and is,
under narrow conditions. Shifted rows stay REVIEW at the row level; a whole
submission is only blocked when shifted rows exceed a ratio/count threshold
`[design/34]`. (The exact thresholds are flagged as possibly miscalibrated
for this newer detector — open, unresolved.)

Suppliers quoting only part of the procurement list is treated as normal
business behavior, not corruption — unquoted anchors are truthfully
reported as unquoted, never invented or zero-filled `[design/39]`.

---

## 7. Alignment & comparison review

The LLM's role across the entire flow is exactly one thing: eliminate
expression differences by normalizing to a canonical representation.
Bid-comparison itself is pure deterministic math — the LLM never re-ranks
candidates, splits line items, awards a bid, or fabricates evaluation facts
`[design/05]` (this rule is also stated as a repo-wide invariant in
CLAUDE.md §4).

Review is staged in gates: automated code validation (free, deterministic,
live) → LLM adversarial review (designed, not implemented) → human review
(final authority). Review concentrates at the matching boundary, not on
every individual attribute `[design/05]`.

A reviewer can mark a "no quote for this anchor" cell as explicitly
acknowledged ("已确认无报价") — a real, persisted, audit-logged action, not
mere UI suppression. Acknowledging does not change the cell's evaluability
or pricing status; a missing cell stays excluded from totals regardless of
ack `[design/23]`.

**Preview comparison** can run before every supplier is confirmed — computed
through the exact same business services as the official path (never a
second implementation), writing nothing (it runs, then rolls back). It shows
no recommendation and no award. A per-supplier gate failure (e.g. checksum)
no longer blocks the whole preview — it downgrades to an advisory note on
that supplier only, while the official confirm path stays strict
`[design/31][design/32]`. Preview has no export — a "non-official" label
can't travel with an exported file, so the surface simply isn't offered.

For anchors still unresolved during confirmation, the UI shows an advisory
(never certain) impact estimate computed from peer suppliers' prices for the
same row — explicitly `estimated` (≥2 peers) or `unbounded` (fewer peers, or
missing quantity); the tool never claims ranking can't flip when any row is
`unbounded` `[design/31]`.

**Preview screen UX (current)**: `showConclusions` hides
★最低/highlight/recommendation columns in the preview lane specifically
("look before you commit," not a conclusion); unit price and total price are
now separate columns everywhere; the AI summary card sits above the matrix
in the formal lane only `[design/36 §7]`.

Three problems design/36 recorded here are **fixed** (re-verified against
code 2026-08-28): doubt notes now go through a plain-language mapping table
(`apps/www/src/utils/doubtCopy.ts::translateReason`); there is an explicit
「进入正式比价」 button in the preview lane; and the 「纳入」/「排除」
buttons on pending cells — which this file said were dead **app-wide** (see
§12's former launch-blocker bullet, now removed) — were fixed the same day,
in the same commit (`bd33e4d`) that made the app-wide-dead finding. `BidMatrix.vue`
no longer has those buttons at all: the formal lane shows a 「去复核 →」
link to `/workspace/:projectId/align` (`AnchorReviewMatrix.vue`, which
already handled confirm/exclude correctly); the preview lane shows only the
待确认 badge, no fake action, gated by an explicit `preview` prop instead of
reusing `showConclusions`'s semantics.

Still open from design/36 §4 (not yet built): a bulk "校对入库" action that
names outstanding suppliers by name, and moving doubt-copy phrasing from a
frontend-only mapping table to a backend-authored `user_message` field.

### 7.1 Project entry and overview (shipped 2026-08-30, `[design/45]`)

**Entry list** (`/workspace`, `compare/EntryView.vue`): one row per project,
carrying project-level summary only (see **Entry list layout** below for the
columns and for what the 2026-09-03 rework deliberately stopped showing). Every
row ends in a deterministic **next-action** label. The label is a state readout,
never advice: five branches, first match wins (`待上传报价` → `清单未确认` → `待校对入库 N 份` → `已定标基准：第N轮` →
`可出比价`), all derived from rows that already exist in the database, with no
model call. Backend `services/tender/project_overview.py::derive_next_action`
is the single authority; the frontend renders `next_action.label` verbatim so
the list and the overview page cannot phrase the same state two ways.

Empty projects (**no round, no submission, no list session**) can be filtered
out with a non-persisted 「显示空项目」 toggle, but the toggle is **on by
default**. The filter itself is **semantic, never name-based** — matching the
auto-generated `新比价项目-<timestamp>` shape would both swallow
legitimately-named projects and miss differently-named shells; a test pins that
no application code matches that pattern.
数据管理 → 项目管理 (`/projects`) is unchanged and still lists everything.

> **Retracted 2026-09-03 — `[design/45]` §4.4 decision D-3 ("hidden by
> default") no longer holds.** D-3's premise was that an empty project is an
> auto-generated shell and therefore a useless card. Those shells came from the
> lazy "create on page open" behaviour, which was **stopped 2026-08-21**. Under
> the actual division of labour — a project administrator creates each tender
> project (at that moment it is a title and nothing else), and the project team
> then uploads quotes to compare — an empty project is the normal starting
> state and precisely what the project team must find in order to begin. Hiding
> it emptied the entry list exactly when it was needed most, and the empty state
> claimed 「还没有项目」 while the database held several. Default flipped to
> show; the empty states now distinguish "filtered out", "no keyword match" and
> "genuinely no projects".

**Entry list layout** (reworked 2026-09-03, user decision): the card list became
a **table**, one row per project carrying summary columns only (项目名称 /
项目编号 / 品类 / 待办 / 待校对入库 / 最近活动). Per-category detail — each
category's round and intake counts — is no longer on the list; it lives on the
project overview page. The 待办 column de-duplicates categories by
`next_action.code` and, when several categories share a code, shows the category
count instead of summing their `count` fields — summing would fabricate a number
the backend never adjudicated.

**Project overview** (`/workspace/:projectId`,
`compare/ProjectOverviewView.vue`) is the new **read-only** landing page; the
three-stage workspace moved to `/workspace/:projectId/compare`. Reworked
**2026-09-03** (user decision): a project-level `a-descriptions` overview
(code / location / category count / project-scoped pending-intake count) sits
above a **left category navigator** (one card per category, each carrying its
current round and a mini progress track) with the selected category's detail on
the right — previously one stacked status card per category, which made a
multi-category project scroll for pages and left the project's own facts with
nowhere to live. An intermediate tabs version was replaced the same day: tabs
crowd once a project has 6+ categories. Per-category scoping is unchanged
(rounds/axes/matrices are all `(project, category)`-scoped, so a single
project-level round number would lie about a multi-category project); only the
arrangement changed, from stacked cards to tabs.

Inside a category tab, a five-step pipeline (`上传识别 → 确认采购清单 → 报价入库
→ 校对确认 → 定标基准`) shows where that category stands. The current step is a
**pure display mapping** from the backend's `next_action.code`
(`utils/pipeline.ts`, unit-tested): the frontend never re-derives progress from
"is there a list / how many quotes", because `derive_next_action` is the single
authority and a second derivation would drift — and drift in the direction of
looking more complete than it is. An unknown code falls back to step 0, never to
"done". Loading and load-failure now have their own branches (skeleton, and an
`a-result` with retry); previously both rendered as a blank page.

The selected category's rounds render as a **descending list of round cards**
(current round highlighted and expanded to its supplier table; closed rounds
collapsed to a supplier-name line). Rounds are a time series, so they are shown
in full rather than folded behind a 当前轮/历史轮 tab. Round **stage**
(`pre_tender`/`formal`, the only two values `models/quote_round.py::STAGES`
accepts) is rendered separately from the round's free-text **name**, so a name
like 「最终澄清报价」 can never be mistaken for a stage the system gates on.

Per-round money is listed **per submission only — no cross-supplier aggregate of
any kind** (retracted 2026-09-03: an earlier same-day version showed a
「明细合计区间」 min~max, which real material proved misleading — see below), and
never 评标总价. The overview endpoint
[does not compute the matrix](../../apps/api/routes/projects.py); an evaluation
total requires `import_and_match`, the three-state gate and an anchor axis, and
computing a cheap lookalike here is how a project ends up with two different
answers to "who is cheaper".

> **Comparability is not established by the system yet.** Real tender material
> (`docs/test2`, 临港中科院) shows that within one round, one supplier quoted
> 「不含安装」 (827,034) while the other three quoted 「含安装」, and all four
> quoted against *different* copper benchmarks (77,540 / 76,600 / 77,470 /
> 77,680 元/吨, unified to 73,410 only in round 2). A price range over those four
> numbers presents non-comparable figures as a comparable spread. Until the
> system models 交付范围 / 原材料价格基准 / 付款条件 (see
> `.claude/plans/comparability-basis-dimensions.md`), rounds carry a standing
> caution instead of an aggregate. The `remark` field that carries these facts
> exists on `BidQuoteLine`, `TenderAnchor` and `Quote`, but is referenced **zero
> times** in `services/matrix/`. The project header also carries **建档时间/建档人**
(`created_at` / `created_by`, added 2026-09-03 — the data was already on
`projects`, it just wasn't exposed; the name falls back nickname → username and
is null when the creating user is gone, rather than inventing 「未知用户」).

Each category carries:

- **采购概述 / 采购清单** — anchor count, version, source (Excel vs tender PDF),
  confirm time, brand requirements. A procurement list carries **no amounts** —
  `TenderAnchor` has no price fields at all, by design (it is the blank form
  bidders fill in).
- **供应商与轮次** — per supplier: row count and the two totals **kept
  separate**, 明细合计 (computed from the detail lines) vs. 文件声明总价
  (what the document declared; `null`, never `0`, when the document declared
  none). Closed rounds list **only** their quote roster — no totals, no
  ranking, no recommendation.
- **当前轮比价建议** — lazy, rendered only for `axis_kind='tender_anchor'`,
  and fetched from the *same* `POST /api/analysis/bid-matrix` the matrix page
  uses, so the two surfaces cannot drift. It shows 评标总价排名 (explicitly a
  price ranking, never an official evaluation score), the three-state
  `recommendation_level` (all three states render, `blocked` included — it
  explains the block), excluded/undecided rows **with their monetary impact**
  rather than a bare count, and 「综合评审待评标小组确认」 whenever non-price
  factors carry no weights — which, given `UNKNOWN_EVALUATION_POLICY` (§7,
  `TECHNICAL.md` §5), is currently always. **It never names a winner.**

The page is read-only by construction: every action links out to the surface
that owns it (workspace, alignment review, round bar).

**Root cause left unfixed, deliberately**: the 61 placeholder projects that
motivated the filter are produced by `WorkspaceView.vue::ensureProject()`,
which still lazy-creates a project named `新比价项目-<timestamp>` on first
drag-in. The default filter treats the symptom; removing lazy creation is
recorded as out of scope in `[design/45]` §9 and pinned by a test so a second
producer cannot appear unnoticed.

---

## 8. Multi-round quoting

A project/category can have multiple named, staff-created **rounds**
(pre_tender or formal stage; open or closed status). Only a round explicitly
flagged `is_final_basis` may produce an official conclusion — other rounds
are explanatory only. The first round auto-opens implicitly — no ceremony
needed `[design/42]`.

> **Corrected 2026-09-03.** This paragraph used to say the first round opens
> "on first upload". It does not: `get_or_open_round` is called from
> `services/submission/quote_confirmation_service.py` (the confirm path) and
> from `routes/analysis.py`'s matrix entry, never from the upload/recognition
> path — pinned by `tests/test_confirm_batch_round_attach.py`. The round opens
> at **first quote confirmation**, which is also what the UI says
> (「首轮将在首次确认报价时自动开启」). The distinction is not cosmetic: a file
> that is still being recognized, or that comes back BLOCKED and never gets
> confirmed, must not leave an empty round behind.

**Current state (re-verified against code 2026-08-28 — the 2026-08-27
consolidation understated this, because it was synthesized from the design
docs' own status banners and those banners predated commits `57cf535` /
`fc023e9`, which had already landed):**

Built and shipped:
- **Round-scoped match (design/44's "B0")** — running match for round 2 no
  longer destroys round 1. `import_and_match(round_id=...)` scopes its
  wipe-and-rebuild by round, and `BidAlignmentGroup` carries
  `round_id`+`anchor_uid` (migration `0011_alignment_round_scope`, which
  also backfills pre-round groups onto round 1).
- **Trend computation** — `services/matrix/round_trend.py`, exposed as
  `GET /api/analysis/round-trend`, rendered by
  `apps/www/src/views/compare/components/RoundTrendPanel.vue` (shown only
  when ≥2 rounds exist and not in the preview lane).
- **The whole design/44 round UI** — `/workspace` is a project list screen
  (`compare/EntryView.vue`, route name `CompareEntry`); the round bar with
  round chips, the "开启新一轮" modal that states the closing consequence
  verbatim, the "更新报价文件" per-file action, and read-only viewing of a
  closed round's frozen matrix all exist in `WorkspaceView.vue`.
- **Project creation restricted to 管理员** (commit `79c7bd3`), i.e. the
  "later phase" design/44 deferred has also happened.

Business rules for trend comparison — **corrected 2026-08-28: these are
enforced, not merely stored**, by `services/matrix/round_trend.py`
(`compute_round_trend`), which this section previously didn't cite at all: a
trend figure requires matching item identity + tax basis + quantity or is
marked `not_comparable_reason` rather than guessed; a supplier absent from a
round is `participating=False`, never zero-filled or interpolated as a
-100% discount; a round that can't be reconstructed (no
`tender_list_session_id` recorded) is reported in `skipped_rounds`, not
silently dropped. The procurement list stays editable across rounds via
`anchor_uid`, a stable per-row identity that survives list revisions.

---

## 9. Brand recommendation

When a user uploads a tender document, the system infers procurement
categories and recommends **approved** brands (must have
`is_approved=True`), ranked by an explainable composite score — not a hard
tier cutoff, so a well-documented domestic brand can outrank an
undocumented joint-venture brand. The LLM may only explain the deterministic
score, never re-rank candidates. This is a per-category recommendation,
independent of the supplier composite score in §3 `[design/15]`.

---

## 10. Historical price governance

Historical procurement prices are governed as reviewed business facts, not
arbitrary queries `[design/11]`:

- Test / E2E / demo / ad-hoc-repair data must never enter the production
  price baseline or supplier/brand evidence.
- Staging a bid submission does **not** automatically become historical
  price — archiving to history is a separate, explicit user action.
- Raw supplier/brand names in uploaded files must never auto-create or
  auto-merge supplier master data; "a supplier once quoted a brand" is not
  the same fact as "this supplier holds formal agency authorization" for
  that brand.
- A price baseline must be computed over genuinely similar
  material/spec/unit/tax-basis/time-range; when the sample is insufficient,
  the correct answer is **no baseline**, never a silent fallback to an
  all-category minimum.

**Current remediation state** (measured 2026-08-28 against the live DB,
following up on `archive/design/data-audit-and-remediation-plan.md`'s
2026-07-10 audit): partial. `materials` fell from the audited 8,303 rows to
6,288, and the audited 363-row duplicate set (same
category/standard_name/spec/unit) no longer exists — that part of
remediation happened. Two of the audit's five problems have **not**:
`quotes.supplier_id` is still `NULL` for all rows in the table (supplier
linkage was never done), and `docs/data/curated/` — the cleaned-data layer
this section's governance rules assume exists — still does not exist on
disk; raw imports write straight to the production tables the same way they
did at audit time.

---

## 11. Users & roles

Three roles `[design/16]`:

| Role | Access |
|---|---|
| 管理员 (admin) | everything — user management, system settings, logs, all business ops |
| 比价员 (buyer) | business ops — invite, compare, import, data edit, export |
| 查看者 (viewer) | read-only — dashboard, materials/suppliers/projects view only |

A default admin is auto-seeded on first login when the users table is empty.
The header shows a read-only role tag; the sidebar menu filters items by
role; a router guard redirects to `/403` on insufficient role.

---

## 12. Known limitations & open product decisions

These are not bugs to silently work around — they're honestly-tracked gaps
or decisions genuinely waiting on the customer/business owner, not
engineering.

> **Calibration note (2026-08-28, two passes).** This file was consolidated
> 2026-08-27 from the design docs' own status banners, without re-reading
> the code. Pass 1 (same day) re-verified §7, §8, and §4's closed-roster
> paragraph, and found both **understated** work (§7 preview fixes, all of
> §8's round work) and **missed** gaps (the four items below, all fixed by
> the end of pass 1's session). **Pass 2** ran six parallel code-verification
> agents over all 53 `archive/design/*.md` docs (not just the handful pass 1
> happened to touch) and caught something pass 1 itself got wrong within the
> same session: two of the four "launch-blocking" items below were already
> fixed by the time pass 1 ended, but the fix was never reflected in this
> section — the exact "spec currency" lapse CLAUDE.md §6 now has a rule
> against. Everything below is current as of pass 2. Any claim elsewhere in
> this file not touched by either pass is still unverified until grepped.

**Launch items found by code audit 2026-08-28 — all four resolved the same
day, listed here so the fix is traceable, not because they're still open:**

- **`MIMO_API_KEY` was absent from every deployment artifact.** The default
  vendor for both text and vision calls became `mimo`
  (`domain_config.TEXT_CLIENT_VENDOR` / `VISION_CLIENT_VENDOR`, changed
  2026-08-27), but the key that makes it work was absent from
  `apps/api/.env.example` and `docker-compose.prod.yml` entirely, and
  `docs/DEPLOY.md` described it as an *optional* entry that "only enables
  design/41's page filter" — true when written, wrong after the default
  flipped. Production ran the logged dashscope fallback on all 8 migrated
  call sites: the "switch everything to mimo" decision had no effect in
  production, and nothing surfaced that except a log line. **Fixed** in all
  three artifacts.
- **One env var silently decided two unrelated things.** Page-filter
  cost-reduction was enabled purely by `MIMO_API_KEY` being present
  (`page_filter.get_production_classifier`). Setting the key to make the
  mimo default take effect therefore *also* switched on the page filter —
  whose cost/speed tradeoff (79% cheaper, 33% slower end-to-end) is listed
  in `TECHNICAL.md` §8 as an explicitly undecided product question that
  "currently ships default-off". **Fixed**: the filter now has its own
  switch, `domain_config.PAGE_FILTER_ENABLED` (default `False`); the key
  remains necessary but is no longer sufficient, pinned by
  `test_page_filter.py::test_enabled_flag_is_independent_of_the_mimo_credential`.
- **Matrix 「纳入」/「排除」 buttons were dead app-wide.** **Fixed** the same
  day, in the same commit (`bd33e4d`) that first wrote this bullet — see §7
  for what changed. Pass 1 never went back to update this section after its
  own fix landed; pass 2 caught it.
- **HEAD's test suite was red on its own change.** Commit `49b4c17` flipped
  the vendor defaults to `mimo` but left `test_text_client_switch.py`
  asserting `dashscope`. **Fixed** the same session.

- **Weighted attribute-based comparison** — customer wants it, but
  structured data today only reliably supports brand as a comparable
  attribute; data source and scope unconfirmed `[TODO §4]`.
- ~~**Phase B-1 deterministic TableGrid shadow mode** — validated in shadow,
  awaiting a user go/no-go to switch it on.~~ **Moot, found 2026-08-28**:
  the TableGrid engine this shadow mode was built on top of was physically
  deleted 2026-08-11 (`[design/21]`, commit `c60217f`) before any go/no-go
  decision was made — `table_recognizer.py` and the rest of the legacy
  OCR→HTML→TableGrid chain no longer exist; current source comments refer
  to it only as "已删除的 legacy TableGrid 链路." There is nothing left to
  switch on; this line item is retired, not decided.
- **Baseline + recommendation redesign** (same-spec baseline / checksum
  semantics / three-state gating / deterministic primary supplier / AI only
  explains) — implementation finalization/confirmation still pending
  `[TODO §4]`.
- **sub18/sub19 data repair** — unverified/unaccepted; sub18 shows only
  ¥124k against a known ¥1.07M, with `source_ref` carrying only a page
  number, no row `[TODO §4]`.
- **qwen retention** — whether to delete the qwen model dependency entirely
  or keep it as a third-tier pixel-reread fallback on `BLOCKED` documents is
  suspended pending a user call, informed by real BLOCKED-rate evidence from
  manual testing `[TODO §5]`. See `docs/spec/TECHNICAL.md` for the two
  candidate technical shapes and their required safety constraints.
- Two known recognition defects (§6 above) are deliberately left unfixed
  this round, tracked, and visible as doubts rather than silently absorbed.
- ~~The comparison-page AI summary panel is not wired up yet.~~
  **Retracted 2026-08-28 — it is wired.** `analysisApi.bidInsight` →
  `POST /api/analysis/bid-insight` → `services/matrix/bid_insight.py`,
  rendered above the matrix in the formal lane only (reconnected
  2026-08-26). The claim came from a design-doc banner written before that.
- **A quote workbook priced excluding tax can still be blocked.**
  `BidQuoteLine` has `unit_price_excl_tax` but no `total_price_excl_tax`, so
  a submission whose totals are excl-tax trips `systematic_vat_mismatch` and
  stays out of official comparison. A real fix needs a new column plus a
  migration; an earlier column-preference workaround was tried and reverted
  because it fixed one sample by breaking another.

---

## 13. Explicitly out of scope (recorded so it isn't re-proposed)

- ERP, purchase orders, contracts, automatic award, automatic master-data
  creation `[design/17][design/18]`.
- Arbitrary skew/scan-tilt correction beyond the four right angles;
  clarification/amendment documents that modify the tender list
  `[design/19]`.
- BOM-level component splitting for switchgear cabinets — horizontal
  comparison only, cancelled 2026-05-20 `[design/06-functional-design-v2]`.
- Excel report export (F6.4) and the to-do queue (F1.4) were never built in
  the original historical-price product surface and were superseded by the
  anchor-based flow before being revisited.
