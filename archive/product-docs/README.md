# Archived product documents and presentation decks

Moved here beginning 2026-08-28 to keep `docs/spec/FUNCTIONAL.md` /
`docs/spec/TECHNICAL.md` the single place that describes current product
behavior — see `archive/design/README.md` for the same rationale applied to
the 53 numbered design docs. These are early-stage functional/technical
presentation decks, not maintained design documents; none of the demo
tooling under `docs/` (`build-pptx.js` et al.) generates or reads them, so
moving them is a pure filing change, not a pipeline change.

| File | What it was |
|---|---|
| `MEMPAS_功能原型.pptx` | Early functional-prototype deck (2026-08-12) — not indexed in `docs/README.md`, superseded by the current product state described in `docs/spec/FUNCTIONAL.md`. |
| `MEMPAS_项目说明_V1.0.pptx` | Superseded by V1.1 below (2026-04-26; `docs/README.md` had already stopped listing it). |
| `MEMPAS_项目说明_V1.1.pptx` / `.pdf` | Project-description deck/PDF (2026-04-27) — a customer-facing snapshot of the product at that date, kept for provenance; the product has changed substantially since (multi-round quoting, closed-roster invitation design, Paddle-VL recognition, etc. — none of that is reflected here). Not maintained; do not treat as current.

Additional cleanup approved 2026-09-02:

- `demo-2026-05/` preserves the obsolete scripted demo, prototypes, report,
  and generation tooling as one bundle.
- `TESTING-2026-05.md` preserves the obsolete QA guide. Current commands and
  test boundaries live in `CLAUDE.md`, `.claude/rules/tests.md`, and the
  fixture manifest.
- The obsolete `docs/USER_MANUAL.md` was removed at the user's request; Git
  history remains the provenance copy.
