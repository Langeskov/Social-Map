"""Reference lookup repository contract (Phase 13.2).

The verifier needs cross-workspace existence facts to distinguish "belongs to
another workspace" from "does not exist at all"; the regular fact repositories
are workspace-scoped, so this read-only lookup is its own small contract.
"""
from typing import Protocol
from uuid import UUID

from app.domain.entities.reference_validation import ReferenceTarget


class ReferenceLookupRepository(Protocol):
    def find(self, entity_type: str, entity_id: UUID) -> ReferenceTarget | None: ...
