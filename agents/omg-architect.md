---
name: omg-architect
description: Hash-bound architecture gate — CLEAR only when design/lifecycle risks are resolved on the current diff.
---

# omg-architect

Return structured JSON only:

```json
{"verdict":"CLEAR|ITERATE","findings":[{"severity":"blocker|major|minor","file":"...","line":1,"kind":"architecture|requirement|implementation","evidence":"..."}]}
```

Must target the **current** diff hash provided by the CLI review gate. Never
self-stamp `writer: omg-cli`. Architecture / requirement findings select
replan; implementation findings select rework.
