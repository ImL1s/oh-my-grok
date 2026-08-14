"""Versioned hash-anchored edit protocol (#76).

This package is a library contract. It does not write ``.omg/state``,
does not set ``verified``, and does not claim that unobserved host-native
edits used this protocol. The public CLI (``omg edit plan|apply``) wraps
these functions; a protocol claim still requires ``apply_hash_edit`` to
return ``HashEditApplyResultV1``.
"""

from .descriptor import (
    HASH_EDIT_KIND,
    HASH_EDIT_SCHEMA_VERSION,
    REVALIDATION_POLICIES,
    HashEditDescriptorV1,
    content_sha256,
    parse_hash_edit_descriptor,
)
from .apply import APPLY_RESULT_KIND, HashEditApplyResultV1, apply_hash_edit
from .errors import (
    HashEditAmbiguousError,
    HashEditApplyError,
    HashEditBindError,
    HashEditConcurrencyError,
    HashEditDescriptorError,
    HashEditError,
    HashEditInputError,
    HashEditPathError,
    HashEditPlannerError,
    HashEditStaleError,
)
from .planner import (
    HashEditCandidate,
    HashEditCurrentFact,
    HashEditPlanV1,
    plan_hash_edit,
)

__all__ = [
    "APPLY_RESULT_KIND",
    "HASH_EDIT_KIND",
    "HASH_EDIT_SCHEMA_VERSION",
    "REVALIDATION_POLICIES",
    "HashEditAmbiguousError",
    "HashEditApplyError",
    "HashEditApplyResultV1",
    "HashEditBindError",
    "HashEditCandidate",
    "HashEditConcurrencyError",
    "HashEditCurrentFact",
    "HashEditDescriptorError",
    "HashEditDescriptorV1",
    "HashEditError",
    "HashEditInputError",
    "HashEditPathError",
    "HashEditPlanV1",
    "HashEditPlannerError",
    "HashEditStaleError",
    "apply_hash_edit",
    "content_sha256",
    "parse_hash_edit_descriptor",
    "plan_hash_edit",
]
