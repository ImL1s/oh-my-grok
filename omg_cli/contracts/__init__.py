"""Frozen, side-effect-minimal contracts for OMG orchestration.

The package intentionally contains schemas, canonical encoders and narrowly
scoped persistence primitives.  Runtime projectors and host adapters consume
these contracts in later waves; they do not become authoritative merely by
importing them.
"""

from .capability_schema import CAPABILITY_TIERS, PARITY_CLASSIFICATIONS
from .resume_contract import RECOVERY_CAPS, WARNING_ORDER
from .writer_chain import PARENT_HASH_ORACLE, canonical_json_bytes, sha256_hex

__all__ = [
    "CAPABILITY_TIERS",
    "PARENT_HASH_ORACLE",
    "PARITY_CLASSIFICATIONS",
    "RECOVERY_CAPS",
    "WARNING_ORDER",
    "canonical_json_bytes",
    "sha256_hex",
]
