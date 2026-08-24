"""Edit hygiene helpers for #76: comments, simplifier, Team authority.

The CLI does not write ``passes`` / ``verified``. Apply artifacts are
copy-safe (no raw source). This does not claim ``omo.edit.hash_anchored``
host parity.
"""

from __future__ import annotations

from omg_cli.edit_hygiene.artifacts import write_edit_artifact
from omg_cli.edit_hygiene.authority import (
    EditHygieneError,
    OwnershipEditError,
    ReadOnlyEditError,
    assert_mutative_edit_allowed,
    resolve_run_task_ids,
)
from omg_cli.edit_hygiene.comments import (
    AUTO_FIXABLE_RULES,
    CommentFinding,
    CommentReport,
    apply_comment_fixes,
    check_comments,
    load_comment_config,
)
from omg_cli.edit_hygiene.simplify import (
    SimplifyBlocked,
    SimplifyError,
    SimplifyRollback,
    run_simplify,
)

__all__ = [
    "AUTO_FIXABLE_RULES",
    "CommentFinding",
    "CommentReport",
    "EditHygieneError",
    "OwnershipEditError",
    "ReadOnlyEditError",
    "SimplifyBlocked",
    "SimplifyError",
    "SimplifyRollback",
    "apply_comment_fixes",
    "assert_mutative_edit_allowed",
    "check_comments",
    "load_comment_config",
    "resolve_run_task_ids",
    "run_simplify",
    "write_edit_artifact",
]
