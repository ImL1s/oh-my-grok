"""Versioned hash-anchored edit protocol (#76).

This package is a library contract. It does not write ``.omg/state``,
does not set ``verified``, and does not claim that unobserved host-native
edits used this protocol. The public CLI (``omg edit plan|apply|verify|comments|simplify``) wraps
these functions; a protocol claim still requires ``apply_hash_edit`` to
return ``HashEditApplyResultV1``. ``verify_hash_edit`` is read-only and
does not claim ``omo.edit.hash_anchored`` host parity.
"""

from .descriptor import (
    HASH_EDIT_KIND,
    HASH_EDIT_SCHEMA_VERSION,
    REVALIDATION_POLICIES,
    HashEditDescriptorV1,
    content_sha256,
    parse_hash_edit_descriptor,
)
from .apply import (
    APPLY_RESULT_KIND,
    VERIFY_RESULT_KIND,
    HashEditApplyResultV1,
    HashEditVerifyResultV1,
    apply_hash_edit,
    read_confined_regular_file,
    verify_hash_edit,
    write_confined_regular_file,
)
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
    "VERIFY_RESULT_KIND",
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
    "HashEditVerifyResultV1",
    "apply_hash_edit",
    "content_sha256",
    "parse_hash_edit_descriptor",
    "plan_hash_edit",
    "read_confined_regular_file",
    "verify_hash_edit",
    "write_confined_regular_file",
]
