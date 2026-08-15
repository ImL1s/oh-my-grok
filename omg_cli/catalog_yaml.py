"""Deterministic YAML subset for ``agents/catalog.yaml`` (no PyYAML).

Supports mappings, sequences, plain/quoted scalars, and ``|`` / ``|-``
block scalars. Used as the human-editable source of truth; committed
``agents/catalog.json`` is generated for the fail-closed loader.
"""

from __future__ import annotations

from typing import Any

_PLAIN_FORBIDDEN = set("[]{},:&*!|>'\"%@`")


class CatalogYamlError(ValueError):
    """Fail-closed YAML subset parse / dump error."""


def dump_yaml(value: Any) -> str:
    """Serialize *value* to a stable 2-space YAML document."""
    lines: list[str] = []
    _dump(value, lines, indent=0)
    return "\n".join(lines) + "\n"


def parse_yaml(text: str) -> Any:
    """Parse the YAML subset produced by :func:`dump_yaml`."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[tuple[int, str]] = []
    for index, line in enumerate(raw_lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rows.append((index, line.rstrip()))
    if not rows:
        raise CatalogYamlError("empty YAML document")
    value, next_index = _parse_node(rows, 0, 0)
    if next_index != len(rows):
        line_no, leftover = rows[next_index]
        raise CatalogYamlError(f"line {line_no}: unexpected content {leftover!r}")
    return value


def _spaces(indent: int) -> str:
    return "  " * indent


def _dump(value: Any, lines: list[str], *, indent: int) -> None:
    pad = _spaces(indent)
    if isinstance(value, dict):
        if not value:
            lines.append(f"{pad}{{}}")
            return
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise CatalogYamlError("mapping keys must be non-empty strings")
            rendered_key = _dump_key(key)
            _dump_map_item(rendered_key, item, lines, indent=indent)
        return
    if isinstance(value, list):
        if not value:
            lines.append(f"{pad}[]")
            return
        for item in value:
            _dump_list_item(item, lines, indent=indent)
        return
    lines.append(f"{pad}{_dump_scalar(value)}")


def _dump_map_item(key: str, item: Any, lines: list[str], *, indent: int) -> None:
    pad = _spaces(indent)
    if isinstance(item, dict):
        if not item:
            lines.append(f"{pad}{key}: {{}}")
            return
        lines.append(f"{pad}{key}:")
        _dump(item, lines, indent=indent + 1)
        return
    if isinstance(item, list):
        if not item:
            lines.append(f"{pad}{key}: []")
            return
        lines.append(f"{pad}{key}:")
        _dump(item, lines, indent=indent + 1)
        return
    if isinstance(item, str) and ("\n" in item or _needs_block(item)):
        chomp = "|" if item.endswith("\n") else "|-"
        lines.append(f"{pad}{key}: {chomp}")
        body = item[:-1] if item.endswith("\n") else item
        child = _spaces(indent + 1)
        for part in body.split("\n"):
            lines.append(f"{child}{part}")
        return
    lines.append(f"{pad}{key}: {_dump_scalar(item)}")


def _dump_list_item(item: Any, lines: list[str], *, indent: int) -> None:
    pad = _spaces(indent)
    if isinstance(item, dict):
        if not item:
            lines.append(f"{pad}- {{}}")
            return
        first = True
        for key, nested in item.items():
            rendered_key = _dump_key(key)
            prefix = f"{pad}- " if first else f"{_spaces(indent + 1)}"
            first = False
            if isinstance(nested, dict):
                if not nested:
                    lines.append(f"{prefix}{rendered_key}: {{}}")
                else:
                    lines.append(f"{prefix}{rendered_key}:")
                    _dump(nested, lines, indent=indent + 2)
            elif isinstance(nested, list):
                if not nested:
                    lines.append(f"{prefix}{rendered_key}: []")
                else:
                    lines.append(f"{prefix}{rendered_key}:")
                    _dump(nested, lines, indent=indent + 2)
            elif isinstance(nested, str) and (
                "\n" in nested or _needs_block(nested)
            ):
                chomp = "|" if nested.endswith("\n") else "|-"
                lines.append(f"{prefix}{rendered_key}: {chomp}")
                body = nested[:-1] if nested.endswith("\n") else nested
                child = _spaces(indent + 2)
                for part in body.split("\n"):
                    lines.append(f"{child}{part}")
            else:
                lines.append(f"{prefix}{rendered_key}: {_dump_scalar(nested)}")
        return
    if isinstance(item, list):
        lines.append(f"{pad}-")
        _dump(item, lines, indent=indent + 1)
        return
    if isinstance(item, str) and ("\n" in item or _needs_block(item)):
        chomp = "|" if item.endswith("\n") else "|-"
        lines.append(f"{pad}- {chomp}")
        body = item[:-1] if item.endswith("\n") else item
        child = _spaces(indent + 1)
        for part in body.split("\n"):
            lines.append(f"{child}{part}")
        return
    lines.append(f"{pad}- {_dump_scalar(item)}")


def _needs_block(value: str) -> bool:
    return len(value) > 120


def _dump_key(key: str) -> str:
    if _is_plain(key):
        return key
    return _quote(key)


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        if _is_plain(value):
            return value
        return _quote(value)
    raise CatalogYamlError(f"unsupported YAML value type {type(value).__name__}")


def _is_plain(value: str) -> bool:
    if not value:
        return False
    if value.strip() != value:
        return False
    if value.lower() in {"true", "false", "null", "yes", "no", "on", "off"}:
        return False
    if value[:1] in "-?:" or value[:1].isdigit():
        return False
    if any(ch in _PLAIN_FORBIDDEN or ch in "\t#" for ch in value):
        return False
    if ": " in value or " #" in value:
        return False
    return True


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_node(
    rows: list[tuple[int, str]], index: int, min_indent: int
) -> tuple[Any, int]:
    line_no, line = rows[index]
    indent = _indent_of(line)
    if indent < min_indent:
        raise CatalogYamlError(f"line {line_no}: indent underflow")
    stripped = line[indent:]
    if stripped in {"{}", "[]"}:
        return ({} if stripped == "{}" else []), index + 1
    if stripped.startswith("-"):
        return _parse_list(rows, index, indent)
    if _looks_like_map(stripped):
        return _parse_map(rows, index, indent)
    return _parse_scalar_line(stripped, line_no), index + 1


def _looks_like_map(stripped: str) -> bool:
    if stripped.startswith(("'", '"')):
        return False
    if stripped.startswith("|"):
        return False
    return ":" in stripped


def _parse_map(
    rows: list[tuple[int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while index < len(rows):
        line_no, line = rows[index]
        current = _indent_of(line)
        if current < indent:
            break
        if current > indent:
            raise CatalogYamlError(f"line {line_no}: unexpected indent")
        stripped = line[current:]
        if stripped.startswith("-"):
            break
        key_raw, sep, rest = stripped.partition(":")
        if not sep:
            raise CatalogYamlError(f"line {line_no}: expected mapping")
        key = _parse_key(key_raw.strip(), line_no)
        if key in out:
            raise CatalogYamlError(f"line {line_no}: duplicate key {key!r}")
        rest = rest.strip()
        if rest == "":
            if index + 1 >= len(rows):
                out[key] = {}
                index += 1
                continue
            next_indent = _indent_of(rows[index + 1][1])
            if next_indent <= indent:
                out[key] = {}
                index += 1
                continue
            value, index = _parse_node(rows, index + 1, indent + 2)
            out[key] = value
            continue
        if rest in {"|", "|-"}:
            value, index = _parse_block(rows, index + 1, indent + 2, rest)
            out[key] = value
            continue
        out[key] = _parse_scalar_line(rest, line_no)
        index += 1
    return out, index


def _parse_list(
    rows: list[tuple[int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    out: list[Any] = []
    while index < len(rows):
        line_no, line = rows[index]
        current = _indent_of(line)
        if current < indent:
            break
        if current > indent:
            raise CatalogYamlError(f"line {line_no}: unexpected list indent")
        stripped = line[current:]
        if not stripped.startswith("-"):
            break
        body = stripped[1:]
        if body == "":
            if index + 1 >= len(rows):
                out.append(None)
                index += 1
                continue
            value, index = _parse_node(rows, index + 1, indent + 2)
            out.append(value)
            continue
        if not body.startswith(" "):
            raise CatalogYamlError(f"line {line_no}: malformed list item")
        body = body[1:]
        if body in {"|", "|-"}:
            value, index = _parse_block(rows, index + 1, indent + 2, body)
            out.append(value)
            continue
        if _looks_like_map(body):
            nested = [(line_no, (" " * (indent + 2)) + body)]
            look = index + 1
            while look < len(rows):
                look_no, look_line = rows[look]
                look_indent = _indent_of(look_line)
                if look_indent <= indent:
                    break
                nested.append((look_no, look_line))
                look += 1
            value, consumed = _parse_map(nested, 0, indent + 2)
            if consumed != len(nested):
                raise CatalogYamlError(f"line {line_no}: malformed list mapping")
            out.append(value)
            index = look
            continue
        out.append(_parse_scalar_line(body, line_no))
        index += 1
    return out, index


def _parse_block(
    rows: list[tuple[int, str]], index: int, min_spaces: int, marker: str
) -> tuple[str, int]:
    parts: list[str] = []
    while index < len(rows):
        _no, line = rows[index]
        spaces = _indent_of(line)
        if spaces < min_spaces:
            break
        parts.append(line[min_spaces:])
        index += 1
    text = "\n".join(parts)
    if marker == "|":
        text += "\n"
    return text, index


def _parse_key(raw: str, line_no: int) -> str:
    if not raw:
        raise CatalogYamlError(f"line {line_no}: empty mapping key")
    if raw[0] in {'"', "'"}:
        value = _parse_scalar_line(raw, line_no)
        if not isinstance(value, str):
            raise CatalogYamlError(f"line {line_no}: key must be a string")
        return value
    return raw


def _parse_scalar_line(raw: str, line_no: int) -> Any:
    text = raw.strip()
    if text in {"{}", "[]"}:
        return {} if text == "{}" else []
    if text in {"null", "~"}:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return _unescape(text[1:-1], line_no)
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return text


def _unescape(value: str, line_no: int) -> str:
    out: list[str] = []
    escaped = False
    for ch in value:
        if escaped:
            if ch == "n":
                out.append("\n")
            elif ch in {'"', "\\"}:
                out.append(ch)
            else:
                raise CatalogYamlError(f"line {line_no}: bad escape \\{ch}")
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        out.append(ch)
    if escaped:
        raise CatalogYamlError(f"line {line_no}: dangling escape")
    return "".join(out)
