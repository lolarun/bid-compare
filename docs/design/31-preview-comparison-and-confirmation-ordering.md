# 31 — Preview comparison + confirmation ordering

> **Status: agreed, implementation starting 2026-08-21.** §2 is what exists
> today (verified in code); §4–§5 are the agreed design; §7 records the
> scope and the two decisions that were open. Cut status is tracked in §6.

## 1. Trigger

> 是否可以先比价，也就是说先进行分析，然后高亮待确认的部分，因为比较这个
> 过程可以是需要一个初步的模糊结果即可，而不是要逐行都那么精确

Today the comparison matrix runs on `confirmedSubmissionIds` — every
supplier must go through 校对入库 before *any* comparison is visible. With
89–136 line items per supplier and 3–4 suppliers, that is several hundred
rows of review before the user learns anything at all, including whether the
review was worth doing.

The request is reasonable and the framing is right: for *deciding where to
look*, a rough answer is enough. It is not a request to lower the precision
of the result that gets acted on.

## 2. What already exists (verified, not assumed)

- **Per-cell status is already modelled.** `bid_matrix.py` emits
  `quoted / aggregated / pending / excluded / missing` per cell and
  `matrix_stats.py` aggregates by the same priority order. Highlighting
  待确认 needs no new data model.
- **`bid_recommendation.py` already excludes** `pending / excluded /
  basis_unconfirmed` rows from 评标总价.
- **A no-commit execution path already exists.** `confirm_batch(..., dry_run=True)`
  runs the full gate chain and collects every issue **without persisting**
  (`quote_confirmation_service.py:353`). The workbench already calls it per
  file (`useDoubtInbox.refreshDryRun`).
- **`quote_readiness.py`** already reports "N 条待确认行已排除在矩阵外".

So the missing piece is narrow: dry-run exists *per file*; there is no
*cross-supplier* dry run, and the matrix cannot take draft input.

## 3. The red line, stated plainly

CLAUDE.md §4: pending / review_candidate "stay out of official quotes,
evaluation totals, and recommendations (conditional explanations allowed,
final procurement confirmation not)". §8: "pending rows excluded from
official calculations."

What the charter forbids is unconfirmed data **entering the official
result**. It does not forbid computing and showing a clearly-marked
non-official view. This design stays on the right side by three hard
constraints, none of them optional:

1. **The preview writes nothing.** No `BidSubmission`, no `BidQuoteLine`, no
   historical price, no supplier master data. It is a pure function of draft
   data.
2. **The preview produces no recommendation and no award.** Ranking and
   totals may be shown; 推荐主供 / 定标 may not. (CLAUDE.md: the LLM
   explains, it does not rank or award — and neither may a preview.)
3. **It runs through the same business services** as the official path, with
   an explicit `basis` flag, never a second implementation. Two code paths
   computing "the comparison" would guarantee the preview and the official
   result eventually disagree, which is the failure this charter rule exists
   to prevent.

**Residual risk, accepted by the user (§7):** a preview number can be
screenshotted and used as a decision. Mitigation is labeling, not
prevention — the basis marker must sit on the matrix itself, not only on a
banner that a screenshot can crop out. Because labeling cannot follow a
file out of the app, the preview has **no export at all** (§7).

## 4. Design part A — preview comparison

### 4.0 Correction: bid comparison never creates suppliers

An earlier note in this session claimed `confirm_batch` creates `Supplier`
rows for unknown suppliers, and used that to argue against the
write-then-rollback approach. **That was wrong.** Verified in code:

- `supplier_resolve.resolve_supplier` only *looks up* — its layer 7 returns
  "not found" and creates nothing.
- An unresolved supplier is stored as `BidSubmission.supplier_raw_name` with
  `supplier_id = NULL`. No master-data row is written.
- The only two places that construct a `Supplier` are
  `routes/suppliers.py` (explicit human CRUD) and
  `ingestion/import_service.py::_get_or_create_supplier` (**historical price
  import**) — which is exactly the policy the user stated: 供应商的来源
  暂时只有历史采购价格导入.

So there is nothing to disable for 「暂时放弃招标比价中新增供应商」 — the
comparison path already does not do it. CLAUDE.md's isolation invariant is
being honoured here today, not aspirationally.

### 4.1 Approach: reuse `dry_run`'s existing write-then-rollback

`confirm_batch(dry_run=True)` **already** writes the submission/lines/job
lifecycle and then rolls back — see the eight `db.rollback()` sites and the
comment at `quote_confirmation_service.py:896`
(「从不写：submission/lines/job.lifecycle 全部撤销」). The doubt inbox calls
it per file on every upload today. Write-then-rollback is therefore not a
new technique being introduced for the preview; it is the mechanism this
service already ships, extended from one file to the cross-supplier case.

Given §4.0, the objection that made an in-memory refactor look necessary
(unconfirmed data touching supplier master data) does not exist. The preview
takes the same route.

#### SQLite needed one more thing than the design assumed

The first implementation of the sandbox **leaked** — rows written inside it
were still there after rollback. Cause: pysqlite manages `BEGIN` implicitly,
so the outer transaction never really started and there was nothing to roll
back. Caught by the row-count test, not by reading the code; recorded here
because "we wrap it in a transaction" reads as obviously sufficient and is
not.

Fix: SQLAlchemy's documented recipe (disable the implicit BEGIN, emit
`BEGIN` on the `begin` event), applied to a **sandbox-only engine** rather
than to `database.engine` — changing the transaction semantics of every
request in the app to serve one preview feature is not a trade worth making.
Cost is one extra connection pool, which on SQLite is nothing.

Caveats that stay live and must be handled in cut 2b:

- **SQLite writer lock.** A preview holds a write transaction for the length
  of alignment + matrix, not just one file's gates. If that blocks concurrent
  real confirms, the preview must be bounded or moved off the write path.
  Measure before shipping; do not assume per-file timings scale.
- **Reuse `dry_run_cache`** so repeated previews do not re-run the chain.
- **Prove it.** Cut 2 needs a test that counts rows in every table the path
  touches before and after a preview and asserts they are unchanged — the
  rollback must be verified, not trusted.

Input: the recognition drafts of the uploaded files (`job.result` items),
not `BidSubmission` rows.

```
drafts ──▶ per-file dry-run gates (existing)
       ──▶ alignment (existing service, unchanged)
       ──▶ bid matrix (existing service, `basis="preview"`)
       ──▶ cells: quoted / aggregated / pending / excluded / missing
```

Every cell that came from an unconfirmed row is `pending` — which the matrix
already knows how to represent and already excludes from totals. The preview
therefore does **not** need "include pending in the total": it shows the same
total the official path would show, plus an explicit statement of how much is
*not* in it.

The workbench shows: the matrix, pending cells highlighted, and a header
stating `非正式预览 · 含 N 项待确认，未计入总价`.

## 5. Design part B — confirmation ordering (the part that matters)

Rough numbers alone do not save the user any review. What saves review is
knowing **which rows can still change the answer**. That ordering is
deterministic — no model involved.

### 5.1 Correction to the first draft of this section

The first version of this design said the tool could declare
「名次已经确定，剩下的行确认与否不改变结论」. **That claim is not
provable and has been removed.** An unread price can be anything; a range
derived from what *other* suppliers quoted is an estimate of plausible
dispersion, not a bound on the true value. Stating it as certainty would be
exactly the kind of overclaim CLAUDE.md forbids ("tiers must never be raised
by silent fill or downstream guessing"). What follows is deliberately
advisory.

### 5.2 Per-row impact estimate

For each unresolved cell (anchor *a*, supplier *s*), using the other
suppliers' resolved unit prices for *a* as peers:

| peers resolved | classification | value |
|---|---|---|
| ≥ 2 | `estimated` | `qty × (max_peer − min_peer)` — plausible swing |
| 1 | `unbounded` | swing not estimable; row **magnitude** `qty × peer` is still known and used for ranking |
| 0, or qty missing | `unbounded` | neither swing nor magnitude known |

`estimated` is a *plausible* swing, never a guarantee — the label must
travel with the number wherever it is shown.

### 5.3 Ordering and the advisory statement

Sort: `unbounded` rows first (a row nobody could read is the one most likely
to matter and the least understood), then `estimated` rows by value
descending. Within `unbounded`, those with a known magnitude sort by it.

The statement shown to the user:

> 当前第 1 名领先第 2 名 X 元。剩余 K 项待确认中，按同行报价区间**估算**
> 最多影响 Y 元；另有 M 项无法估算。

- `M > 0` → the tool says nothing about whether the ranking could flip.
  Those M rows are simply first in the queue.
- `M = 0 且 Y < X` → 「按估算，剩余待确认行不足以改变名次」— phrased as an
  estimate, and never as permission to stop.

The value delivered is scope reduction with the basis stated: confirm the 12
rows that dominate the estimate first, and know exactly what the estimate
does and does not cover. Precision is not lowered; the order of work is.

## 6. Delivery cuts

| Cut | Content |
|---|---|
| 1 | `basis` flag on `BidMatrixResult` + a contract-level validator forbidding `firm` under `preview` — **done** |
| 2a | `preview_sandbox` — a session whose writes are always rolled back, proven by row counts taken from outside it — **done** |
| 2b | Orchestrator + `POST /analysis/bid-matrix/preview` — **done** |
| 3 | Impact estimate + ordering, as a pure function with unit tests (incl. one 金桥 fixture-derived case) — **done** |
| 4 | Workbench: preview banner, ordered confirmation queue, preview matrix reuses `BidMatrix` — **done**; export-disabled-for-preview still open |

## 7. Decisions and open questions

- **Scope chosen by the user (2026-08-21):** 出数字 + 排序 — show the full
  preview matrix *and* the confirmation ordering. (The narrower "ordering
  only, no numbers" option was declined.)
- **Decided (2026-08-21): the preview has no export.** An exported file
  separated from its banner is exactly how a preview becomes someone's
  decision, and §3 accepts labeling as the only mitigation — so the surface
  that labeling cannot follow is simply not offered. May be revisited once
  the marker can be embedded in sheet content.
- **Decided (2026-08-21): a supplier with no resolvable rows for an anchor
  still gets its column.** 先对齐，再高亮 — alignment runs for every
  supplier regardless of how much resolved, and the unresolved cells are
  highlighted in place. Holding a column out would hide the very thing the
  user is looking for (who is missing what), and would make the preview's
  column set differ from the official matrix's — two different shapes for
  "the comparison", which §3 constraint 3 exists to prevent. The
  consequence is that such a supplier's influence bound is 无法界定 and the
  §5 stopping rule cannot claim the ranking is settled; that must be stated
  on screen, not smoothed over.
- **Out of scope:** any change to gate semantics, to the official matrix
  computation, or to recommendation logic. This round adds a view and an
  ordering; it does not touch what "official" means.
