"""stdlib ast local probes: omg lsp symbols / diagnostics."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omg_cli.lsp_tools import diagnostics_ast, symbols_ast, symbols_pyright
from omg_cli.main import build_parser

SAMPLE = '''\
import os
from typing import Any

class Foo:
    def method(self) -> None:
        pass

    async def amethod(self) -> int:
        return 1

def top_level(x: Any) -> str:
    return str(x)

async def async_top() -> None:
    pass
'''


def test_symbols_ast_names_and_linenos(tmp_path: Path) -> None:
    p = tmp_path / "sample.py"
    p.write_text(SAMPLE, encoding="utf-8")
    result = symbols_ast(p)
    assert result["ok"] is True
    assert result["path"] == str(p)
    by_name = {s["name"]: s for s in result["symbols"]}
    assert set(by_name) >= {
        "os",
        "Any",
        "Foo",
        "method",
        "amethod",
        "top_level",
        "async_top",
    }
    assert by_name["Foo"]["lineno"] == 4
    assert by_name["method"]["lineno"] == 5
    assert by_name["amethod"]["lineno"] == 8
    assert by_name["top_level"]["lineno"] == 11
    assert by_name["async_top"]["lineno"] == 14
    assert by_name["os"]["lineno"] == 1
    assert by_name["Any"]["lineno"] == 2
    for s in result["symbols"]:
        assert "col_offset" in s
        assert "end_lineno" in s
        assert s["end_lineno"] is None or s["end_lineno"] >= s["lineno"]


def test_symbols_ast_syntax_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.py"
    p.write_text("def broken(\n", encoding="utf-8")
    result = symbols_ast(p)
    assert result["ok"] is False
    assert "error" in result
    assert result["error"].get("line") is not None


def test_diagnostics_ast_syntax_error_line_col_honesty(tmp_path: Path) -> None:
    p = tmp_path / "bad.py"
    # deliberate SyntaxError: unclosed paren on line 2
    p.write_text("x = 1\ndef broken(\n", encoding="utf-8")
    result = diagnostics_ast(p)
    assert result["ok"] is True
    assert "honesty" in result
    assert "syntax-only" in result["honesty"]
    assert "NOT type-checking" in result["honesty"]
    assert result["diagnostics"]
    diag = result["diagnostics"][0]
    assert diag["line"] == 2
    assert diag["col"] is not None
    assert diag["msg"]


def test_diagnostics_ast_clean_file(tmp_path: Path) -> None:
    p = tmp_path / "ok.py"
    p.write_text("x = 1\n", encoding="utf-8")
    result = diagnostics_ast(p)
    assert result["ok"] is True
    assert result["diagnostics"] == []
    assert "honesty" in result


def test_cmd_lsp_symbols_dispatch(tmp_path: Path) -> None:
    p = tmp_path / "sample.py"
    p.write_text("def hello():\n    return 1\n", encoding="utf-8")
    args = build_parser().parse_args(["lsp", "symbols", str(p)])
    assert args.func(args) == 0


def test_cmd_lsp_diagnostics_dispatch_syntax_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.py"
    p.write_text("def broken(\n", encoding="utf-8")
    args = build_parser().parse_args(["lsp", "diagnostics", str(p)])
    # probe succeeds (ok True) even when file has syntax errors
    assert args.func(args) == 0


def test_cmd_lsp_symbols_exit_1_on_syntax_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.py"
    p.write_text("def broken(\n", encoding="utf-8")
    args = build_parser().parse_args(["lsp", "symbols", str(p)])
    assert args.func(args) == 1


@pytest.mark.skipif(
    not (shutil.which("pyright") or shutil.which("basedpyright")),
    reason="pyright/basedpyright not on PATH",
)
def test_symbols_pyright_optional(tmp_path: Path) -> None:
    p = tmp_path / "ok.py"
    p.write_text("def hello() -> int:\n    return 1\n", encoding="utf-8")
    result = symbols_pyright(p)
    assert "ok" in result
    assert result.get("path") == str(p) or result.get("tool")
