# Remaining blockers (strictest-wins) — 0.2.4 track

**Date:** 2026-07-20  
**HEAD baseline:** 0.2.4  
**Authority:** `council-v021-strictest-wins.md` + goal plan acceptance criteria

## Closed in 0.2.3 (not re-opened)

- Acceptance token + semantic policy floors (`python -c`, glued `-c`, npx)
- Fanout experimental env gate; fail-closed cancel without starttime
- RO dual/ralplan ignore yolo; security-model honesty
- install-plugin.sh, smoke, canary --dry

## Closed in 0.2.4

| # | Item | Status |
|---|------|--------|
| I11 | integrate ancestry / merge reject / changed_files / `--require-squash` | ✅ closed |
| P17 | pipeline integrate + report before accept | ✅ closed |
| Wseal | no-shell worker prepare/seal | ✅ closed |
| A14 | ask stdin/temp prompt; tighten `--extra` | ✅ closed |
| D19 | dual-review: sequential headless interim + `OMG_DUAL_REVIEW_REQUIRE_NATIVE` | ✅ closed |
| Ops | full verification pack (pytest) | ✅ closed |

## Still open at start of this track (must close for dual-review “complete”)

| # | Item | Plan action |
|---|------|-------------|
| I11 | integrate ancestry / merge reject / changed_files | ✅ implement + tests |
| P17 | pipeline integrate + report before accept | ✅ implement + tests |
| Wseal | no-shell worker prepare/seal | ✅ minimal CLI path + tests |
| A14 | ask stdin/temp prompt; tighten --extra | ✅ implement + tests |
| D19 | dual-review: document as sequential headless (explicit); optional single-leader note | ✅ docs + soft gate flag |
| Ops | full verification pack to scratch | ✅ run suite |

## Non-goals (still)

- tmux team, hard sandbox claim, marketplace publish
- native spawn_subagent dual-review (CLI remains sequential interim)
