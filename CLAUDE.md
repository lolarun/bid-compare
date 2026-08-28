# CLAUDE.md — MEMPAS Engineering Charter

This file is loaded into **every** session, so it stays short and only holds what applies
**everywhere**: project orientation, how to run things, and the cross-cutting invariants that
have no narrower home. Per-domain detail lives in the path-scoped rules under `.claude/rules/`
(loaded automatically when you touch matching files); current product/architecture state lives
in `docs/spec/FUNCTIONAL.md` and `docs/spec/TECHNICAL.md`; the *why* behind it lives in the
archived numbered docs under `archive/design/`.

**Single source of truth.** If a rule here and a rule in `.claude/rules/` or `docs/spec/`
disagree, fix the documents — never keep two contradicting requirements. Do not restate
per-domain rules here; link to them instead.

> Note: `docs/spec/*.md` and `archive/design/*.md` are in English (authoritative).
> `.claude/rules/*.md` remain in Chinese and are authoritative for their path-scoped areas;
> this charter is the English entry point.

---

## 1. What MEMPAS is

A bid-comparison (比价) system. The deliverable pipeline:

```text
Tender document / procurement list
  -> confirm TenderAnchor
  -> recognize N BidSubmission quotes, with human review
  -> align by Anchor
  -> resolve pending / missing / excluded
  -> produce the comparison matrix, exports, and evaluation explanations
```

Current focus: a shippable end-to-end tender-comparison flow **while preserving
generalization**. Never trade generality for a project name, supplier name, file name, fixed
page number, fixed row index, or single-sample layout to make a test pass.

## 2. Repository map

```text
apps/api/            FastAPI backend (Python >=3.11), app object: apps.api.main:app
  routes/            HTTP layer: auth, params, transactions, response mapping only
  services/          Domain/business logic (the authoritative layer)
  intelligence/      Recognition pipeline: OCR, page-role classification, table extraction, LLM
  models/            SQLAlchemy models
  schemas/           Pydantic schemas
  migrations/        Alembic migrations (see docs/spec/TECHNICAL.md §7)
  tests/             pytest (unit + replay + fresh E2E)
apps/www/            Frontend: Vue 3 + Vite + Pinia + Ant Design Vue + ECharts
docs/spec/           Current-state spec: FUNCTIONAL.md (product/business) + TECHNICAL.md (architecture) — read these first
archive/design/ Archived numbered design docs — rationale, measurements, retraction history behind docs/spec/
docs/data/           Governed historical-price data (raw/ and curated/)
tests/fixtures/documents/  E2E fixtures (tender/tender_list/bid/bid_list PDFs+Excel, see MANIFEST.md)
scripts/             One-off / batch / audit scripts (must be parameterized — see rules)
.claude/rules/       Path-scoped rules, auto-loaded when matching files are edited
```

## 3. Quickstart, environment & commands

Environment: **Windows 11 / PowerShell**. Use POSIX syntax only inside the Bash tool.

Fixed ports — do not change; the Vite dev proxy targets `127.0.0.1:8020`:

| Service  | Port | Start command |
|----------|------|---------------|
| Backend  | 8020 | `uvicorn apps.api.main:app --port 8020` |
| Frontend | 5120 | `npm --prefix apps/www run dev -- --port 5120` |

> Moved off 8000/3000 on 2026-08-26: port 8000 on this machine is repeatedly
> re-claimed by an unrelated local service, which silently breaks the dev proxy
> mid-session. Changing these means changing all four places that actually take
> effect — `.claude/launch.json`, `apps/www/vite.config.ts` (`server.port` **and**
> `server.proxy['/api'].target`), and `Settings.CORS_ORIGINS` in
> `apps/api/core/config.py` — not just the table above.

Server lifecycle: **do not use `--reload`.** On a port conflict, kill the existing process
first, then restart on the same fixed port. Do not start ad-hoc ports.

```powershell
# Backend tests (unit + replay; testpaths = apps/api/tests, tests)
python -m pytest apps/api/tests -q

# Frontend type-check and unit tests
npm --prefix apps/www run type-check        # vue-tsc
npm --prefix apps/www run test:unit          # vitest（脚本名是 test:unit，
                                            #  没有 `npm test`）

# Database migrations (Alembic)
alembic upgrade head
```

Evidence types are **not interchangeable** — name them separately when reporting:
unit tests (local contracts) · snapshot **replay** (determinism) · **fresh** E2E (real model chain).

## 4. Cross-cutting invariants

These apply across the whole repo. Domain specifics live in the linked rules.

- **Identity.** Every comparison has exactly one row axis, and every result states which
  kind it is (`axis_kind`). A `TenderAnchor` set from a confirmed procurement list is the
  default and the only kind official alignment, evaluation totals, exports, and
  recommendations may use. When no confirmed procurement list exists, a comparison may fall
  back to a quote-derived axis (one supplier's own item rows as the reference, others
  aligned to it by position and confirmed by quantity) — but that axis may feed only the
  preview lane, never an official result, and it carries no tender-side truth: it can show
  that suppliers priced the same row differently, never that a supplier omitted an item the
  tender required. `BidSubmission.id` is a quote's column identity — never substitute
  `supplier_id` for submission identity. API fields must distinguish `anchor_id` /
  `submission_id` / `supplier_id` / `material_id`.
- **Fact lifecycle.** Recognition output enters `ExtractionDraft`; it becomes an official
  quote fact only after user confirmation.
- **Quality tiers gate everything.** **AUTO** = structure/amounts/source/completeness pass →
  may enter official alignment. **REVIEW** = system pre-fills and exposes doubts; pending /
  review_candidate stay out of official quotes, evaluation totals, and recommendations
  (conditional explanations allowed, final procurement confirmation not). **BLOCKED** = severe
  page loss, no reliable structure, key amount conflict, or no valid quote → no storage,
  alignment, or recommendation. Tiers must never be raised by silent fill or downstream
  guessing; any auto-correction keeps the original value, its basis, and a flag.
- **Isolation of comparison data.** Test / E2E / demo / draft / excluded / unconfirmed quotes
  must never flow into official historical prices, supplier master data, supplier recall, or
  brand evidence.
- **One business result.** Pages, exports, recommendations, and AI explanations must consume
  the **same** business-service result — they may not each recompute their own semantics.
- **LLM stays explanatory.** The LLM explains deterministic results, evidence, and risk only.
  It may not re-rank candidates, split line items, award bids, or fabricate evaluation facts.
- **Thresholds centralized.** No magic numbers scattered in code; name and centralize them.
- **Production prompts use fictional examples** — never real suppliers, brands, projects, file
  names, or sample-specific column orders.
- **Respect the workspace.** Do not modify or roll back workspace changes the user did not
  authorize.

## 5. Detailed rules (path-scoped) and specs

Each rule file auto-loads when you edit a file it covers; read it before changing that area.
The "Spec section" column points into `docs/spec/TECHNICAL.md` (architecture) /
`docs/spec/FUNCTIONAL.md` (product); those sections cite the original `archive/design/`
doc(s) for full rationale.

| Area | Rule file | Spec section |
|------|-----------|--------------------|
| Recognition pipeline (OCR / page roles / table extraction / fallback / bbox / rendering) | `.claude/rules/recognition.md` | `TECHNICAL.md` §3 |
| Bid-compare backend (alignment / matrix / evaluation policy / readiness) | `.claude/rules/bid-compare-backend.md` | `TECHNICAL.md` §5, `FUNCTIONAL.md` §7 |
| Historical prices & supplier/brand evidence | `.claude/rules/historical-data.md` | `FUNCTIONAL.md` §10 |
| Database & repair-script safety | `.claude/rules/database-safety.md` | `TECHNICAL.md` §7 |
| Tests & acceptance | `.claude/rules/tests.md` | — |

## 6. Development workflow

Before changing code:
1. Read the real call path, data model, and existing tests.
2. Find the earliest layer where things go wrong; do not keep patching downstream of bad data.
3. State this round's stopping point and what is explicitly out of scope.

After changing code:
1. Run the directly relevant unit and integration tests.
2. Run **replay** when the model chain is involved; run **fresh** E2E only when real-capability
   evidence is required.
3. Report the actual scope run, pass/fail, items not run, and residual risk.
4. Never exclude directly relevant tests and then claim "all green" or "production-ready".

## 7. Reporting format

Every status report keeps these sections separate:
Confirmed facts · Inferences and their basis · Unverified items · Code changes · Data changes ·
Tests and E2E · Remaining blockers.

Wrong conclusions must be explicitly retracted — never carried forward as a premise.

## 8. Definition of delivery

- Procurement list and quote detail complete; doubts explicitly **REVIEW** / **BLOCKED**.
- Each quote independent; no pollution of historical prices or supplier master data.
- Alignment results human-reviewable; pending rows excluded from official calculations.
- Pages, exports, and evaluation explanations report consistent semantics.
- Data operations have backup, audit, and rollback paths.

Ongoing product goals (broader layout coverage, pixel-level bbox traceability, stable fresh
E2E, versioned migrations) must be recorded honestly as gaps — never faked by repeatedly
rewriting an already-passing chain.
