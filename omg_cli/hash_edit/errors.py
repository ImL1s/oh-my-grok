"""Typed errors for the hash-anchored edit protocol (#76 PR1)."""

from __future__ import annotations


class HashEditError(ValueError):
    """Base error for the versioned hash-edit protocol."""


class HashEditDescriptorError(HashEditError):
    """Descriptor failed schema, allowlist, path, or content-hash checks."""


class HashEditPlannerError(HashEditError):
    """Planner failed before producing an immutable plan."""


class HashEditInputError(HashEditPlannerError):
    """Current bytes/path fact is not usable (UTF-8, binary, oversize, path)."""


class HashEditBindError(HashEditPlannerError):
    """Base digest matched but old/context/hint did not bind."""


class HashEditStaleError(HashEditPlannerError):
    """Zero exact candidates (or require_base digest mismatch)."""


class HashEditAmbiguousError(HashEditPlannerError):
    """More than one exact text+context candidate."""


class HashEditApplyError(HashEditError):
    """Apply failed; the target file must be unchanged."""


class HashEditPathError(HashEditApplyError):
    """Workspace confinement or file-kind check failed."""


class HashEditConcurrencyError(HashEditApplyError):
    """File bytes changed between plan and apply."""
