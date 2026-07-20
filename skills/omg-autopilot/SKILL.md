---
name: omg-autopilot
description: Strict Autopilot v2 coordinator — interview→ralplan→implement→review→qa→acceptance with legal transitions only.
---

# omg-autopilot

Strict phase machine for multi-stage delivery. Illegal transitions fail closed.
Verified only after same-process CLI acceptance.

## Phases

`interview → ralplan → implement → review → (rework) → qa → acceptance → verified`

Gates:

- interview complete before ralplan
- consensus before implement
- review clean before qa
- qa clean before acceptance
- acceptance token in-process before verified

## CLI

```bash
omg autopilot start "goal text"
omg autopilot transition --run RUN --phase ralplan --evidence-json '{"interview_complete":true}'
omg autopilot status --run RUN
omg accept --run RUN   # then
omg autopilot complete --run RUN
```

Do not invent verified by writing status files.
