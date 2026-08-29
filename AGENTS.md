# AGENTS.md

This repository's agent charter is [`CLAUDE.md`](CLAUDE.md) — read it first. It
covers repo layout, run commands, and the cross-cutting invariants. This file
exists only because some tools look for `AGENTS.md` by convention instead of
`CLAUDE.md`; it is not a second place to author rules.

One rule is repeated here in full because it governs how much you should
trust `docs/spec/FUNCTIONAL.md` / `docs/spec/TECHNICAL.md` before relying on
either:

**Spec currency.** Any commit that changes whether something is built — flips
a spec claim from "not built" to shipped, or the reverse — must update that
section in the *same* commit, not a later audit. A stale claim there is worse
than an out-of-date `archive/design/` doc: it reads as current and gets
trusted without a code check. `docs/spec/*.md` carries a 2026-08-28
calibration note documenting a real case where a synthesis-time claim was
already wrong at the moment it was written down — treat any "not built" /
"未实现" claim you haven't personally re-checked with the same suspicion.

Everything else — repo map, fixed ports, test commands, quality-tier
invariants, development workflow, reporting format — lives in `CLAUDE.md`.
Do not duplicate it here. If this file and `CLAUDE.md` ever disagree,
`CLAUDE.md` wins and this file is the one that's stale.
