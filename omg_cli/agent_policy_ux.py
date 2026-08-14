"""Host-neutral agent/model policy UX (#134).

Formats the #131 ``AgentPolicyViewV1`` for human CLI. JSON remains the typed
view. This module does not probe providers, write ``verified``, or invent
Medley receipts. Color is never required: status is always plain text.

Width bands:
- narrow  (< 80): stacked cards; progressive disclosure
- normal  (80–119): four-column table
- wide    (>= 120): aliases / policy source / floor columns

``NO_COLOR`` is honored (no ANSI either way). CJK aliases use East-Asian
display width so columns stay aligned.
"""

from __future__ import annotations

import os
import shutil
import unicodedata
from typing import Any, Mapping, Sequence

WIDTH_NARROW = 80
WIDTH_WIDE = 120
ELLIPSIS = "…"

EXPLAIN_ALWAYS = (
    "Identity",
    "Capability/tool floor",
    "Policy source and precedence winner",
    "Original Grok Build baseline behavior",
    "Rejected/blocked reasons",
    "Next action",
)
EXPLAIN_NORMAL = (
    "Capability-gated Medley policy and candidate order",
    "Prompt profile/reasoning preference",
    "Selected host facts, when available",
    "Resume/attempt lineage",
)
EXPLAIN_WIDE = ("Host capability registry",)

POLICY_NATIVE_NOTE = (
    "policy native route is not Team presentation native_host_receipt"
)


def color_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Status is never color-only. True only if a future tint is allowed."""
    environ = env if env is not None else os.environ
    if environ.get("NO_COLOR"):
        return False
    if environ.get("OMG_NO_COLOR"):
        return False
    return False


def terminal_width(
    *,
    env: Mapping[str, str] | None = None,
    override: int | None = None,
) -> int:
    if override is not None and override > 0:
        return int(override)
    environ = env if env is not None else os.environ
    raw = environ.get("COLUMNS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    try:
        return max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
    except OSError:
        return 80


def width_band(columns: int) -> str:
    if columns < WIDTH_NARROW:
        return "narrow"
    if columns < WIDTH_WIDE:
        return "normal"
    return "wide"


def display_width(text: str) -> int:
    total = 0
    for char in text:
        if unicodedata.east_asian_width(char) in {"F", "W"}:
            total += 2
        else:
            total += 1
    return total


def pad_display(text: str, width: int) -> str:
    clipped = truncate_display(text, width)
    return clipped + (" " * max(0, width - display_width(clipped)))


def truncate_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    ellipsis_w = display_width(ELLIPSIS)
    if width <= ellipsis_w:
        return ELLIPSIS[:width]
    budget = width - ellipsis_w
    out: list[str] = []
    used = 0
    for char in text:
        size = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if used + size > budget:
            break
        out.append(char)
        used += size
    return "".join(out) + ELLIPSIS


def _fit_chars(text: str, width: int) -> tuple[str, str]:
    """Split ``text`` so ``head`` fits in ``width`` display columns."""
    if width <= 0:
        return "", text
    if display_width(text) <= width:
        return text, ""
    out: list[str] = []
    used = 0
    for index, char in enumerate(text):
        size = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if used + size > width:
            if not out:
                return char, text[index + 1 :]
            return "".join(out), text[index:]
        out.append(char)
        used += size
    return "".join(out), ""


def _wrap_unbroken(text: str, width: int) -> list[str]:
    if display_width(text) <= width:
        return [text]
    lines: list[str] = []
    rest = text
    while rest:
        head, rest = _fit_chars(rest, width)
        if not head:
            head, rest = rest[:1], rest[1:]
        lines.append(head)
    return lines


def wrap_display(
    text: str,
    width: int,
    *,
    subsequent_indent: str | None = None,
) -> list[str]:
    """Wrap ``text`` to ``width`` display columns (CJK-aware).

    Oversized tokens continue on the next line; characters are never dropped.
    """
    if width < 1:
        return [""]
    lead_n = len(text) - len(text.lstrip(" "))
    lead = text[:lead_n]
    body = text[lead_n:]
    cont = subsequent_indent if subsequent_indent is not None else lead
    tokens = body.split()
    if not tokens:
        return _wrap_unbroken(text, width)
    lines: list[str] = []
    prefix = lead
    parts: list[str] = []

    def current() -> str:
        return prefix + " ".join(parts)

    def flush() -> None:
        nonlocal prefix, parts
        lines.append(current())
        prefix = cont
        parts = []

    for token in tokens:
        trial = prefix + " ".join(parts + [token])
        if display_width(trial) <= width:
            parts.append(token)
            continue
        if parts:
            flush()
            if display_width(prefix + token) <= width:
                parts = [token]
                continue
        rest = token
        while rest:
            budget = width - display_width(prefix)
            if budget < 1:
                lines.append(prefix)
                prefix = cont
                budget = width - display_width(prefix)
                if budget < 1:
                    prefix = ""
                    budget = width
            head, rest = _fit_chars(rest, budget)
            if not head and rest:
                head, rest = rest[0], rest[1:]
            lines.append(prefix + head)
            prefix = cont
        parts = []
    if parts:
        flush()
    return lines or [lead]


def _wrap_lines(lines: Sequence[str], columns: int) -> list[str]:
    out: list[str] = []
    for line in lines:
        indent = "    " if line.startswith(" ") else ""
        out.extend(wrap_display(line, columns, subsequent_indent=indent))
    return out


def host_policy_label(view: object) -> str:
    if getattr(view, "requested_extension", None):
        return "optional extension"
    return "baseline"


def _requested_extension_state(view: object) -> str | None:
    extension = getattr(view, "requested_extension", None)
    if not extension:
        return None
    rows = getattr(view, "host_capabilities", ()) or ()
    for item in rows:
        if isinstance(item, Mapping) and item.get("capability_id") == extension:
            state = item.get("state")
            return str(state) if state is not None else None
    return None


def model_intent_label(view: object, *, band: str) -> str:
    extension = getattr(view, "requested_extension", None)
    candidates = tuple(getattr(view, "candidate_ids", ()) or ())
    mode = str(getattr(view, "baseline_mode", "") or "")
    outcome = _requested_extension_state(view)
    if extension and candidates:
        if outcome != "supported":
            shown = (
                outcome
                if outcome in {"unsupported", "unavailable", "incompatible", "unknown"}
                else "unsupported"
            )
            return f"{mode} ({shown})"
        if band != "narrow":
            shown = " -> ".join(str(item) for item in candidates[:2])
            if len(candidates) > 2:
                shown += " -> …"
            return shown
    return mode


def render_list_human(
    rows: Sequence[object],
    *,
    columns: int,
    env: Mapping[str, str] | None = None,
) -> str:
    _ = color_enabled(env)
    band = width_band(columns)
    if band == "narrow":
        return _render_list_narrow(rows, columns=columns)
    if band == "wide":
        return _render_list_table(
            rows,
            headers=("Agent", "Host policy", "Model intent", "Status", "Source", "Floor"),
            cells=lambda row: (
                str(getattr(row, "agent_id")),
                host_policy_label(row),
                model_intent_label(row, band=band),
                str(getattr(row, "status")),
                str(getattr(row, "policy_source")),
                str(getattr(row, "capability_floor")),
            ),
            columns=columns,
        )
    return _render_list_table(
        rows,
        headers=("Agent", "Host policy", "Model intent", "Status"),
        cells=lambda row: (
            str(getattr(row, "agent_id")),
            host_policy_label(row),
            model_intent_label(row, band=band),
            str(getattr(row, "status")),
        ),
        columns=columns,
    )


def _render_list_narrow(rows: Sequence[object], *, columns: int) -> str:
    blocks: list[str] = []
    for row in rows:
        reasons = getattr(row, "reasons", ()) or ()
        next_action = "baseline inherit is ready on original Grok Build"
        if reasons:
            next_action = str(getattr(reasons[0], "next_action"))
        aliases = ", ".join(getattr(row, "aliases", ()) or ())
        blocks.append(
            "\n".join(
                _wrap_lines(
                    [
                        str(getattr(row, "agent_id")),
                        f"  aliases: {aliases or '(none)'}",
                        f"  host policy: {host_policy_label(row)}",
                        f"  model intent: {model_intent_label(row, band='narrow')}",
                        f"  status: {getattr(row, 'status')}",
                        f"  next: {next_action}",
                    ],
                    columns,
                )
            )
        )
    return "\n\n".join(blocks)


def _render_list_table(
    rows: Sequence[object],
    *,
    headers: tuple[str, ...],
    cells: Any,
    columns: int,
) -> str:
    table = [tuple(str(item) for item in cells(row)) for row in rows]
    widths = [display_width(h) for h in headers]
    for line in table:
        for index, cell in enumerate(line):
            widths[index] = max(widths[index], display_width(cell))
    # Shrink trailing columns first so Agent/Status stay identifiable.
    while sum(widths) + 2 * (len(widths) - 1) > columns and len(widths) > 2:
        shrinkable = max(range(1, len(widths) - 1), key=lambda i: widths[i])
        if widths[shrinkable] <= 8:
            break
        widths[shrinkable] -= 1
    out = [
        "  ".join(pad_display(headers[i], widths[i]) for i in range(len(headers)))
    ]
    for line in table:
        out.append(
            "  ".join(pad_display(line[i], widths[i]) for i in range(len(headers)))
        )
    return "\n".join(out)


def render_explain_human(
    view: object,
    *,
    columns: int,
    env: Mapping[str, str] | None = None,
) -> str:
    _ = color_enabled(env)
    band = width_band(columns)
    facts = getattr(view, "host_facts", {}) or {}
    reasons = getattr(view, "reasons", ()) or ()
    aliases = ", ".join(getattr(view, "aliases", ()) or ()) or "(none)"
    sections: dict[str, list[str]] = {
        "Identity": [
            f"  agent_id: {getattr(view, 'agent_id')}",
            f"  aliases: {aliases}",
            f"  category: {getattr(view, 'category')}",
            f"  tier: {getattr(view, 'tier')}",
        ],
        "Capability/tool floor": [
            f"  capability_floor: {getattr(view, 'capability_floor')}",
            f"  tool_floor: {', '.join(getattr(view, 'tool_floor', ()) or ()) or '(none)'}",
        ],
        "Policy source and precedence winner": [
            f"  policy_id: {getattr(view, 'policy_id')}",
            f"  policy_source: {getattr(view, 'policy_source')}",
            f"  policy_digest: {getattr(view, 'policy_digest')}",
        ],
        "Original Grok Build baseline behavior": [
            f"  baseline_mode: {getattr(view, 'baseline_mode')}",
            f"  baseline_model: {getattr(view, 'baseline_model')}",
            f"  host policy: {host_policy_label(view)}",
        ],
        "Capability-gated Medley policy and candidate order": [
            f"  requested_extension: {getattr(view, 'requested_extension')}",
            f"  candidate_ids: {', '.join(getattr(view, 'candidate_ids', ()) or ()) or '(none)'}",
            f"  medley_capability_outcome: {facts.get('medley_capability_outcome')}",
            f"  route_specific_facts: {facts.get('route_specific_facts')}",
        ],
        "Prompt profile/reasoning preference": [
            f"  prompt_profile: {getattr(view, 'prompt_profile')}",
            f"  reasoning_preference: {getattr(view, 'reasoning_preference')}",
        ],
        "Selected host facts, when available": [
            f"  selected_model_ref: {getattr(view, 'selected_model_ref')}",
            f"  route_kind: {getattr(view, 'route_kind')}  ({POLICY_NATIVE_NOTE})",
            f"  route_receipt_digest: {getattr(view, 'route_receipt_digest')}",
        ],
        "Rejected/blocked reasons": _reason_lines(reasons),
        "Resume/attempt lineage": [
            f"  attempt: {getattr(view, 'attempt')}",
        ],
        "Next action": _next_action_lines(reasons),
        "Host capability registry": _capability_lines(view),
    }
    names = list(EXPLAIN_ALWAYS)
    if band in {"normal", "wide"}:
        names.extend(EXPLAIN_NORMAL)
    if band == "wide":
        names.extend(EXPLAIN_WIDE)
    # Keep issue order rather than ALWAYS-then-NORMAL append order.
    ordered = [
        "Identity",
        "Capability/tool floor",
        "Policy source and precedence winner",
        "Original Grok Build baseline behavior",
        "Capability-gated Medley policy and candidate order",
        "Prompt profile/reasoning preference",
        "Selected host facts, when available",
        "Rejected/blocked reasons",
        "Resume/attempt lineage",
        "Next action",
        "Host capability registry",
    ]
    out: list[str] = []
    wanted = set(names)
    for name in ordered:
        if name not in wanted:
            continue
        out.append(name)
        out.extend(sections[name])
    out.append(f"status: {getattr(view, 'status')}")
    return "\n".join(_wrap_lines(out, columns))


def _reason_lines(reasons: Sequence[object]) -> list[str]:
    if not reasons:
        return ["  (none)"]
    lines: list[str] = []
    for reason in reasons:
        lines.append(f"  {getattr(reason, 'code')}: {getattr(reason, 'message')}")
    return lines


def _next_action_lines(reasons: Sequence[object]) -> list[str]:
    if reasons:
        return [f"  {getattr(reasons[0], 'next_action')}"]
    return ["  baseline inherit is ready on original Grok Build"]


def _capability_lines(view: object) -> list[str]:
    rows = getattr(view, "host_capabilities", ()) or ()
    if not rows:
        return ["  (none)"]
    lines: list[str] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            f"  {item.get('capability_id')}={item.get('state')}"
        )
    return lines or ["  (none)"]


def format_doctor_routing_human(snapshot: object) -> str:
    """Plain-text doctor addendum. Missing Medley is unsupported, not fail."""
    state_of = getattr(snapshot, "state_of")
    host_tier = getattr(snapshot, "host_tier", "unknown")
    return "\n".join(
        [
            "agent/model routing (read-only inspect; no paid probe)",
            f"  host tier: {host_tier}  (original Grok Build is first-class)",
            f"  inherit: {state_of('host.native-inherit-model.v1')}",
            f"  exact: {state_of('host.native-exact-model.v1')}",
            f"  Medley optional extension: {state_of('medley.native-ordered-candidates.v1')}"
            "  (not installation failed)",
            "  inspect: omg agents list / omg agents explain <agent>",
            f"  {POLICY_NATIVE_NOTE}",
        ]
    )


def format_presentation_human(state: Mapping[str, Any], *, columns: int = 120) -> str:
    """Human Team Presentation V1. Does not change locked ``omg team status --json``."""
    header = [
        "Team presentation (schema v1; not omg team status --json)",
        f"run_id: {state.get('run_id')}",
        f"team_id: {state.get('team_id')}",
        f"  {POLICY_NATIVE_NOTE}",
        "",
    ]
    members = [
        member
        for member in (state.get("members") or [])
        if isinstance(member, Mapping)
    ]
    headers = (
        "member",
        "role",
        "presentation_route",
        "executor",
        "attempt",
        "status",
    )
    table_rows = [_presentation_cells(member) for member in members]
    out = _wrap_lines(header, columns)
    if (
        width_band(columns) != "narrow"
        and _natural_table_width(headers, table_rows) <= columns
    ):
        out.append(
            _render_list_table(
                members,
                headers=headers,
                cells=_presentation_cells,
                columns=columns,
            )
        )
        return "\n".join(out)
    out.extend(_wrap_lines(_presentation_stacked_lines(members), columns))
    return "\n".join(out).rstrip()


def _natural_table_width(
    headers: tuple[str, ...],
    rows: Sequence[tuple[str, ...]],
) -> int:
    widths = [display_width(h) for h in headers]
    for line in rows:
        for index, cell in enumerate(line):
            widths[index] = max(widths[index], display_width(cell))
    gaps = 2 * max(0, len(widths) - 1)
    return sum(widths) + gaps


def _presentation_cells(member: Mapping[str, Any]) -> tuple[str, ...]:
    route = member.get("route") if isinstance(member.get("route"), Mapping) else {}
    current = (
        member.get("current_attempt")
        if isinstance(member.get("current_attempt"), Mapping)
        else {}
    )
    kind = str(route.get("kind") or "unknown")
    return (
        str(member.get("logical_worker_id") or ""),
        str(member.get("role") or ""),
        kind,
        str(route.get("executor") or "-"),
        str(current.get("attempt") or "-"),
        str(current.get("status") or "-"),
    )


def _presentation_stacked_lines(members: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for member in members:
        cells = _presentation_cells(member)
        if lines:
            lines.append("")
        lines.extend(
            [
                cells[0] or "(member)",
                f"  role: {cells[1] or '-'}",
                f"  presentation_route: {cells[2]}",
                f"  executor: {cells[3]}",
                f"  attempt: {cells[4]}",
                f"  status: {cells[5]}",
            ]
        )
    return lines


__all__ = [
    "POLICY_NATIVE_NOTE",
    "WIDTH_NARROW",
    "WIDTH_WIDE",
    "color_enabled",
    "display_width",
    "format_doctor_routing_human",
    "format_presentation_human",
    "pad_display",
    "render_explain_human",
    "render_list_human",
    "terminal_width",
    "truncate_display",
    "width_band",
    "wrap_display",
]
