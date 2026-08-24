# Visual Contract V1

Pure, copy-safe comparison contract for Issue #75.

Authoritative implementation: `omg_cli/contracts/visual_contract.py`.
Public CLI: `omg visual compare|capture|verdict|ralph|overlay`.

`compare` wraps `compare()` only and stays pixel-agnostic. `capture` /
`verdict` / `ralph` / `overlay` live in `omg_cli/visual_runtime.py` (PNG
pixels in `omg_cli/visual_pixels.py`) and write artifacts under
`.omg/artifacts/visual/<run_id>/`.

The **contract module** does **not** decode images, talk to agents or
providers, write `.omg/state`, or set OMG `verified`. A visual score is
evidence only. Runtime `omg visual overlay` **may** decode PNG (stdlib
`struct`/`zlib` only; no Pillow) and write an overlay PNG sidecar plus
numeric stats. JSON never inlines image bytes. `--descriptor-only` keeps
the sha/byte-identity sidecar.

## Honesty

Serialized comparison results never include:

- `approved`
- `passes`
- `verified`
- image bytes
- base64 payloads

Status is only `scored` or `blocked`. Callers compare `aggregate` to
`threshold` themselves. Compatibility failures are `blocked` and carry
**no** threshold verdict (`aggregate` is omitted).

## Kinds

| Kind | Role |
| --- | --- |
| `omg.visual.comparison` | Input document (`schema_version` = integer `1`, not `true`) |
| `omg.visual.comparison_result` | Output document |

Canonical JSON v1 is compact UTF-8, code-point-sorted keys, integer/bool/
string/null/array/object only, no BOM, no trailing newline. Digests are
lowercase SHA-256 hex of those bytes.

## Input keys (exact)

`schema_version`, `kind`, `reference`, `candidate`,
`reference_compatibility`, `candidate_compatibility`, `dimensions`,
`threshold`, `masks`, `task_criteria`

### Image descriptor (exact)

`path`, `sha256`, `media_type`, `byte_size`, `width`, `height`

- `path` — canonical workspace-relative POSIX. Rejects absolute paths,
  `..` / `.` segments, backslash, C0 controls, `//`, `./`, trailing `/`.
  UTF-8 including CJK is allowed. The contract never opens the path.
- `sha256` — 64 lowercase hex characters (content hash supplied by the
  caller; this module does not hash files).
- `media_type` — one of `image/png`, `image/jpeg`, `image/webp`, `image/gif`.
- `byte_size` — declared size, integer `1` .. `32 MiB`.
- `width` / `height` — integer `1` .. `16384`, and
  `width * height <= 100_000_000`.

Reference and candidate dimensions must match exactly. Width mismatch blocks
with `block_code=image_dimension_mismatch` and `block_field=image_width`;
height mismatch uses `block_field=image_height`. Width is checked first for a
deterministic field when both differ. A blocked result has no `aggregate` or
`threshold`, while its digest still binds both mismatched descriptors.

### Compatibility (exact fields)

`viewport_width`, `viewport_height`, `dpr_milli`, `platform`, `theme`, `locale`

Numeric viewports are `1` .. `16384`. `dpr_milli` is `1` .. `16000`
(1000 = 1.0 device pixel ratio). Strings are non-empty, max 128 chars,
CJK allowed.

Missing, null, or empty compatibility fields, or any field mismatch
between reference and candidate, yield:

```json
{
  "schema_version": 1,
  "kind": "omg.visual.comparison_result",
  "status": "blocked",
  "block_code": "compatibility_missing|compatibility_mismatch",
  "block_field": "theme",
  "comparison_digest": "…"
}
```

### Dimensions

Exactly these ten IDs (Issue #75 comparison dimensions), each once:

1. `geometry_layout_alignment`
2. `spacing_sizing`
3. `typography`
4. `color_contrast`
5. `component_state`
6. `missing_extra_elements`
7. `overflow_clipping_responsiveness`
8. `imagery_icons`
9. `accessibility_visible`
10. `task_specific_behavior`

Each row is `{id, score, weight}` with `score` in `0..10000` and `weight`
a positive integer. Weights **sum to exactly 10000**. Output order is the
list above, regardless of input order.

Integer half-up aggregate:

```
total = sum(score_i * weight_i)
aggregate = (total + 5000) // 10000
```

`threshold` is an integer `0..10000`. The contract does not decide pass/fail.

### Masks

Axis-aligned rectangles `{x, y, width, height}` in **reference** pixel
space. Empty list is allowed. At most `256` input rectangles (`MAX_MASKS`) are
accepted; the raw count is checked before deduplication or union work. Each rectangle must have positive area and
lie in-bounds (`x+width <= ref_width`, exclusive end). Exact duplicates
are dropped; remaining rectangles sort by `(x, y, width, height)`.

Union area is overlap-safe (scanline, no bitmap). V1 rejects a set whose
union covers more than 25% of reference pixels. Exactly 25% is allowed.

## Digest binding

`comparison_digest` is SHA-256 of canonical JSON of:

`schema_version`, `kind`, `reference`, `candidate`,
`reference_compatibility`, `candidate_compatibility`, `dimensions`,
`threshold`, `masks`, `task_criteria`

Any mutation of image hashes/metadata, task criteria, dimension scores or
weights, threshold, or masks changes the digest.

## Purity

`visual_contract.py` uses only the Python standard library
(`json`, `hashlib`, `re`, `collections.abc`, `typing`, `pathlib.PurePosixPath`).
It does not import other `omg_cli` modules, does not touch the filesystem
or network, and adds no third-party dependency.

## Public API

- `compare(document) -> dict` — only high-level entry
- `validate_image_descriptor` / `validate_compatibility` /
  `validate_dimensions` / `validate_masks`
- `mask_union_area` / `aggregate_score` / `comparison_digest` /
  `canonical_json_bytes` / `sha256_hex`
- `VisualContractError`

## CLI (`omg visual compare|capture|verdict|ralph|overlay`)

`omg visual compare --input <json>` reads a comparison document and wraps
`compare()`. The CLI document is size-bounded (1 MiB) before JSON load.
It always emits a schema_version 1 JSON envelope:

| Exit | When |
| --- | --- |
| `0` | `compare()` returned `scored` or `blocked` (`ok: true`; callers still compare `aggregate` to `threshold`) |
| `2` | missing `--input`, unreadable or oversized JSON (`E_VISUAL_INPUT`), or `VisualContractError` (`E_VISUAL_CONTRACT`) |

`E_VISUAL_CONTRACT` uses a sanitized validation message. Rejected contract
values (including image/base64 stuffed into a field) are not copied into
the envelope.

The nested `result` object is the contract output. `compare` does not decode
images, talk to agents or providers, or write `passes` / `verified`.
`aggregate < threshold` is still `status: scored` and exit `0`.

### Capture / verdict / Ralph

| Command | Contract |
| --- | --- |
| `omg visual capture --config visual.yaml --json` | Run `capture.command` argv, else `OMG_VISUAL_CAPTURE`, else PATH `screencapture` (`screencapture -x` when the binary is on PATH or at `/usr/sbin/screencapture`), else **blocked** (not a fake pass). Playwright is not required. Not an Antigravity vision model. Records command/tool, target, viewport/DPR/platform/theme/locale, readiness, timestamp, content hash, exit/error. Width/height are declared metadata — files are hashed, not decoded. |
| `omg visual verdict --reference ref.png --actual current.png --threshold 90 --json` | Wrap `compare()` plus artifacts under `.omg/artifacts/visual/<run_id>/`. PNG overlay sidecar is written unless `--descriptor-only`. `--threshold` is percent `0..100` (90 → score 9000). Independent read-only reviewer required (`reviewer_status`, never `verified`). `E_VISUAL_REVIEWER` if editor==reviewer or reviewer is read-write. |
| `omg visual ralph --config visual.yaml --max-iter 5 --json` | Bounded freeze → capture/actual → verdict → repair-prompt artifact → recapture loop. Stops on threshold, blocked, or budget. Does not spawn agents or edit UI. |
| `omg visual overlay --reference ref.png --candidate current.png --json` | Stdlib PNG pixel overlay. Writes `overlay.png` + numeric `changed_pixels` / `changed_ratio_milli` / `bbox`. `pixel_decode: true`. Fail-closed on non-PNG, truncated, oversize, symlink, or path escape (`E_VISUAL_PIXEL` / `E_VISUAL_PATH`). `--descriptor-only` skips decode. Never `verified`. |

Config is JSON or a restricted YAML subset (PyYAML is not a dependency; JSON is valid YAML 1.2). Capture precedence is always printed by `omg doctor` (`visual capture adapter`).

## Remaining gaps (#75 does not close)

Live screenshot smoke and an Antigravity vision-model reviewer remain
unproven. PNG pixel overlay evidence is in-repo (`omg visual overlay`);
it is **not** a live capture smoke and **not** AG vision. Any write to
`passes` / `verified` stays out of scope.
