# MEMPAS workspace planning

> Updated: 2026-09-02. This file tracks repository and workspace organization.
> Product behavior and architecture remain authoritative only in `docs/spec/`;
> current implementation tasks and customer decisions remain in the spec. The
> root `TODO.md` is a user-requested, explicitly frozen historical checklist.

## Objectives

1. Keep current documentation small and unambiguous.
2. Separate current specs, historical rationale, governed datasets, executable
   fixtures, customer source files, and local runtime state.
3. Prevent unreviewed customer documents and large duplicate archives from
   entering Git.
4. Make a clean clone testable before it is deployable.

## Target layout

```text
apps/                       application code
docs/spec/                  current product and architecture truth
docs/DEPLOY.md              current deployment runbook
tests/fixtures/             manifested executable test corpus
archive/design/             frozen design and audit history
archive/product-docs/       frozen demos and product documents
archive/scripts/            retired diagnostic and repair scripts
docs/data/                  governed historical data, pending dataset migration
docs/项目资料/              customer sources, pending external artifact storage
data/                       ignored runtime databases, uploads, and outputs
```

Do not add new current-state documents under `archive/`, and do not make runtime
code depend on archived files. Avoid creating one-file documentation subfolders.

## Completed in the 2026-09-02 cleanup

- Removed obsolete `docs/README.md` and `docs/USER_MANUAL.md` at user request.
- Moved the frozen product/UI `TODO.md` to the repository root.
- Archived the May 2026 demo bundle and obsolete testing guide.
- Archived implemented design 45 and the two historical `.claude/plans/` files.
- Moved the two August manual-test feedback records to `archive/feedback/`
  without rewriting their source wording.
- Consolidated active fixture requirements into
  `tests/fixtures/documents/MANIFEST.md`.
- Removed four byte-identical cable CSV copies from `docs/test1/`; the golden
  builder now reads the manifested fixture copies.
- Added repository line-ending/binary attributes and protected `docs/test2/`
  from accidental staging.
- Added a developer-facing root README and a CI gate before deployment.

## Pending decisions and work

### P0 — external customer-artifact storage

`docs/test2/` is an untracked local drop of about 1.04 GB. It contains five ZIP
archives, 32 DWG files, and at least 247 MB of exact extracted duplicates. Do not
commit, delete, or deduplicate it until an external destination and retention
owner are chosen. The eventual repository footprint should be a manifest with
hashes plus only the smallest reviewed regression subset under `tests/fixtures/`.

The same policy should later cover contract files, `docs/test1/`, and binary
customer sources under `docs/项目资料/`. Moving them into `archive/` is not a
solution because they would remain in Git history and every checkout.

### P1 — historical dataset normalization

Keep `docs/data/` unchanged until its consumers are migrated atomically. A live
2026-09-02 hash check found only 40 of 98 flat CSV files byte-identical to their
`raw/2026-06-23/csv/` counterparts (9.48 MB), so the older 94-file estimate must
not be used as deletion authority. Explain the other 58 differences, choose one
canonical layer, update importers/tests, dry-run the import, then remove only
verified duplicates. The eventual home should be a dedicated governed dataset
tree rather than general documentation.

### P2 — repository size policy

Do not rewrite Git history under the current plan. For new large fixtures,
require a manifest entry, provenance, sensitivity review, SHA-256, and an
explicit size exception or external-download strategy. Reconsider Git LFS only
with matching CI/deployment support and an agreed migration boundary.

### P3 — local maintenance

After the current contract/spec/script changes are committed or otherwise
secured, remove empty local directories and run normal Git housekeeping. Local
cleanup must not modify databases, customer artifacts, or unrelated worktree
changes.

## Acceptance gates

- `docs/` contains only current specs/runbooks plus temporarily retained governed
  data and source assets whose pending disposition is recorded above.
- Every executable corpus file is covered by the fixture manifest.
- No current file references the removed `docs/design/45`, `docs/poc`, or old
  `docs/test/*.md` paths.
- Backend tests, frontend type-check, and frontend unit tests pass from the
  documented commands.
- The deployment workflow cannot build or deploy unless the CI test job passes.
- `git status` contains only intentional changes and preserves the user's
  pre-existing contract, spec, and script work.
