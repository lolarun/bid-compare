# Archived design docs

These are **point-in-time analysis reports and planning notes**, not living design specs —
kept verbatim for historical context, not maintained, not authoritative.

For the current authoritative design, see the numbered specs in `docs/design/` (English).

## May 2026 batch (Chinese, project-inventory era)

Reflect the project state when written. Intentionally left in Chinese.

| File | What it was | Superseded / continued by |
|------|-------------|----------------------------|
| `00-现有资料清单与分析.md` | Initial inventory & analysis of source materials | `04`, `09` |
| `03-数据分析计划.md` | Data-analysis plan for the historical-price datasets | `11-historical-price-governance.md` |
| `05-数据分析报告.md` | Data-analysis report over the 10-category CSV datasets | `11-historical-price-governance.md` |
| `08-用户反馈分析报告.md` | User-feedback & source-material analysis report | `06-functional-design-v2.md` |

> Internal cross-references inside these files point at the original (pre-rename) Chinese
> filenames and are deliberately not updated — these docs are frozen.

## July 2026 batch (English, code/data audits)

One-off audit reports, moved here by `docs/design/43-repository-cleanup.md` Phase 3 —
same genre as the batch above (a snapshot of a moment), different era and language.

| File | What it was | Superseded / continued by |
|------|-------------|----------------------------|
| `code-review-e2e-efficiency.md` | 2026-07-10 full-codebase review + E2E efficiency analysis | Findings folded into subsequent design docs as they were addressed; not tracked as a single successor |
| `data-audit-and-remediation-plan.md` | 2026-07-10 audit of `data/mempas.db` + `docs/data/` + `docs/项目资料/` | `11-historical-price-governance.md` (the curated-data policy this audit's findings fed into) |
