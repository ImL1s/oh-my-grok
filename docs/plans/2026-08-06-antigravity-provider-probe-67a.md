# Antigravity Provider Probe (#67-A) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Land a typed Antigravity provider probe package and `omg provider antigravity {capabilities,doctor}` without changing ask/Team launch behavior.

**Architecture:** New `omg_cli/providers/` is the single future launch surface. Slice A only discovers the binary, parses version, classifies compatibility against the pinned upstream snapshot, and emits a golden JSON capabilities envelope. Ask remains fail-closed for `agy`; Team keeps `_build_agy`. Inventory maturity stays `catalogued`; only `omg_paths` is updated.

**Tech Stack:** Python 3.11+, argparse via `command_registry` / `commands/*`, hermetic fake-`agy` fixtures, pytest.

**Issue hygiene:** Partial work for issue 67 (slice A). Issue 67 remains open. No `closes`/`fixes`/`close` + `#67`.

---

### Task 1: Failing probe tests + fake binary fixtures

**Files:**
- Create: `tests/fixtures/antigravity/fake_agy.py` (or shell stub)
- Create: `tests/fixtures/antigravity/version.txt`, `help.txt` (pinned capture notes)
- Create: `tests/test_antigravity_provider_probe.py`
- Create: `omg_cli/providers/` package (minimal stubs only after RED)

**Step 1: Write failing tests** for missing binary, version parse, compat range, capabilities JSON schema, doctor `--strict` exit codes.

**Step 2: Run** `pytest tests/test_antigravity_provider_probe.py -q` → expect FAIL (import/missing).

**Step 3: Commit** fixtures + failing tests.

---

### Task 2: Typed models + errors

**Files:**
- Create: `omg_cli/providers/__init__.py`
- Create: `omg_cli/providers/models.py`
- Create: `omg_cli/providers/errors.py`
- Create: `omg_cli/providers/base.py` (Protocol / ABC)

**Step 1: Implement** `ProviderCapabilities`, `CompatStatus`, `ProviderError` subclasses.

**Step 2: Make** probe tests import models successfully; keep behavior failures.

---

### Task 3: Antigravity discovery + probe

**Files:**
- Create: `omg_cli/providers/antigravity.py`
- Modify: pin constant to `docs/parity/upstream-snapshots/Antigravity.json` `pin_revision` (`bfab12da…`) for docs cross-ref only (compat range is version string based)

**Step 1: Implement** `discover_binary`, `probe_version`, `probe_capabilities`, `doctor` (argv arrays; no shell; bounded env).

**Step 2: Green** focused probe tests.

---

### Task 4: CLI `omg provider`

**Files:**
- Modify: `omg_cli/command_registry.py` — add `CommandSpec("provider", …, "inspect")`
- Create: `omg_cli/commands/provider.py` — `register_provider_parsers` + handlers
- Modify: `omg_cli/main.py` — compose register hook
- Modify: `docs/skills.md` / CLI inventory if drift tests require
- Modify: `omg_cli/contracts/parity_schema.py` — own new paths under suitable wave (OMG-W3 near team providers or new W patterns)
- Modify: `docs/parity/omg-parity.json` — `omg_paths` for `antigravity.provider.adapter` include `omg_cli/providers/`

**Step 1: CLI tests** for `capabilities --json` / `doctor --strict` with fake PATH.

**Step 2: Run** hermetic unit gate (`-m "not live"`).

**Step 3: Commit + push PR** — body: Partial work for issue 67 (slice A); Issue 67 remains open.

---

### Out of scope (later slices)

- **#67-B:** headless `run` + json/stream-json parse, timeout/cancel
- **#67-C:** session/resume fields; route `omg ask` through adapter
- **#67-D:** Team pane envelope via same adapter; docs migration
