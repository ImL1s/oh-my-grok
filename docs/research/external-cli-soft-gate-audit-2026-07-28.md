# External-CLI soft-gate audit — 2026-07-28

Status: **documented v0.7.2 baseline cases verified; broader parser hardening planned**

This document records the evidence behind the external-agent CLI policy in
[`../security-model.md`](../security-model.md). It is an audit record, not a
claim that PreToolUse is a complete shell sandbox.

## Scope and proof boundary

The audit compared:

- canonical `omg_cli.deny` behavior;
- the committed generated standalone hook;
- the globally installed standalone hook;
- documented `omg ask` child-process authorization;
- passive discovery/inspection, inert text, direct execution, wrappers, shell
  bodies, substitutions, assignments, redirections, and malformed inputs.

The hook was fed host-shaped JSON. Protected provider commands used in the
matrix were treated as data and were not executed.

## Verified v0.7.2 baseline cases

| Case | Decision |
|------|----------|
| Direct protected provider execution, including `--version`/`--help` | Deny |
| `which`, `command -v`, `type`, and equivalent passive discovery | Allow |
| `strings`, `file`, `stat`, `readlink`, and hash tools with provider paths | Allow |
| Quoted arguments, comments, inert heredoc data, and `fix(kimi)` commit text | Allow |
| Recognized substitutions or shell bodies that directly execute a protected CLI | Deny |
| `OMG_ALLOW_EXTERNAL_CLI=1` inside the fixed `omg ask` child | Allow |
| The same variable absent from the child or merely written in command text | No authorization |

This confirms the v0.7.2 quoted-literal fix shipped by PR #15. A denial for a
simple passive discovery/inspection command on the same version indicates one
of:

1. a stale or locally modified installed hook;
2. a compound command that also contains an active protected executable;
3. another host hook or permission layer;
4. a separate user/UI cancellation rather than an OMG deny.

## Installed-state drift found during the audit

The audited machine contained a non-canonical persistent flag-file bypass in
the global hook installation. It allowed every external-CLI case and violated
the child-only `OMG_ALLOW_EXTERNAL_CLI` contract.

The flag was quarantined. The committed standalone was then restored and
checked with:

```bash
python3 -m omg_cli.hook_install
omg doctor
```

After repair:

- installed and committed hook SHA-256 values matched;
- direct provider execution denied;
- passive discovery, passive path inspection, and `fix(kimi)` text allowed;
- all global-hook smoke, freshness, and hash checks passed.

Do not add an ambient environment export or persistent flag file to solve a
false positive. Repair the generated/installed bytes instead.

## Known limitations

A broader adversarial matrix found additional correctness and bounded-runtime
gaps that remain under coordinated remediation. Exact classes, counts, and
payloads are intentionally omitted from this public audit so it does not become
an attack roadmap before a fix is available. Report security-sensitive
reproductions through the private advisory path described in
[`../../SECURITY.md`](../../SECURITY.md).

These findings do **not** change the product boundary: `capability_mode` and
tool removal remain primary; PreToolUse remains a fail-open, name-based
defense-in-depth layer.

## Remediation direction

1. Complete issue #18 so `omg install-hook` is always recognized as a local CLI
   subcommand rather than being sent to the Grok launcher.
2. Replace the current command-head recognizer with a bounded parser that uses
   exact executable semantics.
3. Add structured decision/reason output while preserving
   `should_deny_command() -> bool` compatibility.
4. Keep canonical, generated, and installed decision/reason matrices identical
   and enforce bounded performance in regression tests.
5. Add a discovery-only provider inspection command before considering any
   fixed-argv version probe. Discovery must resolve/stat/hash without executing
   the provider; a future probe requires a separate threat-model gate.

## Validation evidence

Fresh local evidence collected after restoring the canonical hook:

```text
pytest -q tests/test_deny.py tests/test_hook_install.py
312 passed

python3 -m pytest -q -m 'not live' --tb=short
1602 passed in 1551.48s

ruff check \
  omg_cli/__init__.py omg_cli/main.py omg_cli/autopilot.py omg_cli/modes.py \
  omg_cli/pipeline.py omg_cli/ralplan.py omg_cli/review.py omg_cli/qa.py \
  omg_cli/guidance.py tests/test_cli_router.py tests/test_autopilot.py \
  tests/test_modes.py tests/test_pipeline.py tests/test_ralplan.py \
  tests/test_review.py tests/test_qa.py tests/test_packaging.py \
  tests/test_docs_cli_drift.py tests/test_release_readback.py
All checks passed!

python3 -m mypy --follow-imports=skip omg_cli/main.py omg_cli/__init__.py tests/test_release_readback.py
Success: no issues found in 3 source files

OMG_E2E=1 OMG_SMOKE_STRICT=0 ./scripts/smoke.sh
smoke OK
ALL_REAL_E2E_OK
```

Generator, capabilities-lock, compileall, and diff checks also passed. The
installed v0.7.2 `omg doctor` command returned 0 and all global-hook checks
passed. A checkout-local `./bin/omg doctor` can correctly return nonzero after
the checkout diverges from the immutable installed receipt; strict checks may
also report unrelated foreign-orchestration, installed-snapshot, or existing
user-level compatibility risks. No user-level Claude configuration was
modified.

## Status

- The original quoted-literal false positive is fixed and shipped in v0.7.2.
- The audited machine's installed hook drift is repaired.
- Broader parser/security limitations remain under coordinated remediation and
  are not represented here as implemented or released.
