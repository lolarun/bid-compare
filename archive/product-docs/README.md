# Archived product presentation decks

Moved here 2026-08-28 to keep `docs/spec/FUNCTIONAL.md` /
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

Not touched, on purpose, because they aren't design documents in this sense:
`docs/DEMO.md`/`DEMO.html` (a scripted demo walkthrough), `docs/USER_MANUAL.md`
(user-facing manual), `docs/TESTING.md` (a QA guide, partially superseded by
`.claude/rules/tests.md`), `docs/demo-test-report.md`, and the demo-generation
tooling (`docs/build-pptx.js`, `docs/capture-*.js`). These are a different
genre — ask before touching them if the goal expands to cover them too.
