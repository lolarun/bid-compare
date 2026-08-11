# 18 — Closed-Roster Invitation Flow

> Status: **Draft** · Created: 2026-08-03 · Author: system
> Supersedes nothing. Depends on `15-invite-brand-recommendation`, `17-agent-rebuild-pilot`.

## 1. Goal

In the pilot scenario the tender document **is an invitation (邀标)**: it names the
suppliers that are allowed to bid. Today the system throws that fact away and
re-derives supplier identity from each uploaded bid PDF against the full supplier
master table.

This design turns the invited list into a **confirmed closed roster** carried by
`ProcurementCase`, so that bid ingestion becomes *matching against N known
candidates* instead of *open-set recognition*.

Non-goals: ERP, purchase orders, contracts, auto-award, automatic master-data
creation. The roster changes **who a column can be**, not how a page is read.

## 2. Current state (verified)

| Fact | Evidence |
|---|---|
| Tender extraction captures only project name/code/date/deadline | `pipeline.py:410-413`, `prompts.py:TENDER_PROMPT` |
| Invited suppliers already exist as rows, written by the invite page | `models/bid_invitation.py` — `(tender_id, supplier_id, status)` |
| Bid ingestion never reads them | `quote_confirmation_service.confirm_batch` takes client-supplied `supplier_name` |
| Supplier identity is resolved open-set, 7 layers over all suppliers | `services/supplier_resolve.py` |
| Bid supplier name is recognized per document, with a cover-page LLM fallback | `pipeline.py:273-283` |

So the data model is 80% there; the two chains are simply not connected, and the
tender side never extracts the roster in the first place.

## 3. Design

### 3.1 Tender extraction — two new draft fields

`TENDER_PROMPT` and `_postprocess_tender` gain:

| Field | Meaning | Rule |
|---|---|---|
| `invited_suppliers: list[{raw_name, source_ref}]` | 受邀单位 / 投标单位 / 被邀请单位 as written | Literal text only. No normalization, no guessing. Empty list when absent. |
| `price_basis: {tax_included, tax_rate, currency}` | 计价口径 | `tax_included` is tri-state (`true`/`false`/`null`). Never defaulted. |

Both land in `ExtractionDraft` like every other recognition output. **Neither
becomes fact without user confirmation** (charter §4, fact lifecycle).

`price_basis` is included here deliberately: an unstated tax basis corrupts every
downstream baseline comparison, and it lives on the same pages as the roster.

### 3.2 Roster confirmation

`/api/tender-list/confirm` accepts `invited_suppliers: [{raw_name, supplier_id|null}]`.

For each entry the frontend pre-resolves via existing `resolve_supplier(raw_name)`:

- exact hit → pre-selected, user may override
- ambiguous (candidates) → user must choose; no auto-selection (existing layer-7 rule)
- miss → user picks an existing supplier or explicitly creates one

On confirm the backend creates:

- `ProcurementCase(roster_mode="closed")`
- one `BidInvitation(tender_id, supplier_id, raw_name, source="tender_document", status="pending")` per entry

`BidInvitation.supplier_id` stays NOT NULL — resolution happens before save, so the
roster is always canonical. `raw_name` is kept for audit (what the document said).

The invite page's existing recommendation flow writes the same rows with
`source="recommendation"`. Both paths produce one roster; nothing forks.

### 3.3 Bid ingestion — closed-set matching

`confirm_batch` already accepts `procurement_case_id`. When the case is
`roster_mode="closed"`:

1. Candidate set = the case's roster (typically 3).
2. Evidence, in priority order: user selection → cover-page OCR name resolved
   **within the roster** → file name alias.
3. Exactly one candidate → bind, record `matched_layer` for audit.
4. Zero or multiple → **REVIEW**; the frontend forces a manual pick.
5. Automatic creation of a new `Supplier` is **forbidden** in closed mode.
6. Binding a supplier outside the roster requires an explicit user action that
   flips the case to `roster_mode="exception"` and writes an operation log.

`SupplierAlias` still runs — the roster narrows candidates from hundreds to three,
which turns alias matching from a guess into a confirmation.

### 3.4 Matrix and completeness

- `bid_matrix` columns come from the roster, not from "how many files were uploaded".
- A roster member with no confirmed submission renders as **未响应**, not absent.
- Readiness surfaces `responded / roster_size`. It reports, it does not block.

### 3.5 Quality gate gains a denominator

Confirmed `TenderAnchor` count is the expected row count for every submission in
the case. Per-submission recall = `matched_anchors / expected_anchors` becomes an
input to `compute_quality`.

This closes a real hole: today a dropped list page lowers the row count silently,
because nothing knows how many rows there should have been.

## 4. What this unlocks (separate designs)

With a known roster and a known anchor count, the recognition fast path
(text-layer page triage, deterministic `TableGrid → DraftRow` without the Stage-2
LLM call) becomes verifiable rather than speculative — a dropped page is now
detectable. That work is **doc 19**, not this one.

## 5. Data changes

Migration `0007`, idempotent, following `13-alembic-migration-introduction`:

- `bid_invitations`: add `raw_name VARCHAR(200) DEFAULT ''`, `source VARCHAR(20) DEFAULT 'recommendation'`
- `procurement_cases`: add `roster_mode VARCHAR(16) DEFAULT 'open'`
- `tender_documents`: add `price_basis JSON DEFAULT '{}'`

Existing rows keep `roster_mode='open'`, i.e. today's behavior, unchanged.

## 6. Scope boundaries

- **Not every case is an invitation.** Public tenders, ad-hoc quotes and
  single-supplier enquiries keep `roster_mode="open"` and behave exactly as today.
  Closed mode is a per-case attribute, never a global switch.
- **OCR does not decide eligibility.** The extracted roster is a suggestion; only
  a confirmed roster gates comparison.
- **The roster does not shorten the recognition chain.** Line items still have to
  be read from the page; only supplier identity moves from recognition to matching.

## 7. Test plan

| Level | Coverage |
|---|---|
| unit | roster matcher: single hit / ambiguous / zero / outside-roster override |
| unit | tender postprocess: roster and `price_basis` absent → empty, never defaulted |
| integration | confirm roster → upload 2 of 3 bids → matrix shows 3 columns, 1 未响应 |
| integration | closed mode never auto-creates a Supplier |
| replay | existing snapshots unchanged (open mode is the default) |
| fresh E2E | 金桥 fixture — roster extracted from the tender PDF, human-confirmable |

## 8. Open risks

1. **Roster written informally** in the tender (short names, subsidiaries). Mitigated
   by `SupplierAlias` + mandatory human confirmation, not by fuzzy auto-matching.
2. **OCR misses one invited unit.** The confirmation page must allow add/remove, so a
   missed line is a review cost, never a silent exclusion.
3. **A supplier bids under a different legal entity.** Handled by rule 3.3.6
   (explicit exception + audit), never silently.
