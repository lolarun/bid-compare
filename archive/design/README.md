# Archived design docs

These are **point-in-time analysis reports, planning notes, and — as of
2026-08-27 — the full history of numbered design documents** (`01`–`44`,
plus `TODO.md`/`HANDOFF.md`). Kept verbatim for historical context and
rationale — not maintained, not authoritative for current state.

**For current, authoritative product/architecture truth, read
[`docs/spec/FUNCTIONAL.md`](../../docs/spec/FUNCTIONAL.md) and
[`docs/spec/TECHNICAL.md`](../../docs/spec/TECHNICAL.md) instead.** Those two
files were synthesized 2026-08-27 from every doc in this directory, keeping
only what each doc's own status banner marked as current, and are the
intended entry point for both humans and agents. Come here only when you
need the *why* — the original trigger, measurements, rejected alternatives,
and retraction history behind a decision, cited from the spec files as
`[design/NN]` tags.

## 2026-08-27 batch — the numbered design-doc history (English)

All 40 numbered docs (`01`–`44`, gaps at unused numbers) plus root
`TODO.md` (backend/tech-debt checklist) and `HANDOFF.md` (chronological
recognition-pipeline handoff log) were consolidated into the two spec files
above and moved here unmodified. See `docs/spec/FUNCTIONAL.md` and
`docs/spec/TECHNICAL.md` themselves for the topic-by-topic mapping — every
claim in those two files cites which of these originals it came from.

## May 2026 batch (Chinese, project-inventory era)

Reflect the project state when written. Intentionally left in Chinese.

| File | What it was | Superseded / continued by |
|------|-------------|----------------------------|
| `00-现有资料清单与分析.md` | Initial inventory & analysis of source materials | `docs/spec/TECHNICAL.md` §2–3 |
| `03-数据分析计划.md` | Data-analysis plan for the historical-price datasets | `docs/spec/FUNCTIONAL.md` §10 |
| `05-数据分析报告.md` | Data-analysis report over the 10-category CSV datasets | `docs/spec/FUNCTIONAL.md` §10 |
| `08-用户反馈分析报告.md` | User-feedback & source-material analysis report | `docs/spec/FUNCTIONAL.md` §3, §12 |

> Internal cross-references inside these files point at the original (pre-rename) Chinese
> filenames and are deliberately not updated — these docs are frozen.

## July 2026 batch (English, code/data audits)

One-off audit reports, moved here by `docs/design/43-repository-cleanup.md` Phase 3 —
same genre as the batch above (a snapshot of a moment), different era and language.

| File | What it was | Superseded / continued by |
|------|-------------|----------------------------|
| `code-review-e2e-efficiency.md` | 2026-07-10 full-codebase review + E2E efficiency analysis | Findings folded into subsequent design docs as they were addressed; not tracked as a single successor |
| `data-audit-and-remediation-plan.md` | 2026-07-10 audit of `data/mempas.db` + `docs/data/` + `docs/项目资料/` | `docs/spec/FUNCTIONAL.md` §10 / `docs/spec/TECHNICAL.md` §2 (the curated-data policy this audit's findings fed into) |
