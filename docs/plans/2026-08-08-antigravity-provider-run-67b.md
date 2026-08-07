# Antigravity Headless Run (#67-B) Implementation Plan

> Partial work for issue 67 (slice B). Issue 67 remains open. No `closes`/`fixes`.

**Goal:** Land provider-neutral headless execution (`ProviderAdapter.run`) for
Antigravity on top of the slice A probe, without routing ask or Team.

**Architecture:** Extend `omg_cli/providers/` models/errors; keep a single
subprocess stack via `run_provider_process` (probe is a thin wrapper); Antigravity
`run` assembles argv, parses text/json/stream-json, preserves partial output on
timeout/cancel, and records session/resume *metadata* only.

**Non-goals:** ask cutover (#67-C), Team pane/envelope (#67-D), live-network CI,
vendoring Antigravity source.
