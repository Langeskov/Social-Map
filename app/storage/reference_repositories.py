"""SQL adapter for the reference lookup (read-only, any workspace)."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.entities.reference_validation import ReferenceTarget
from app.storage.models import (EventModel, EvidenceModel, PersonModel,
                                RelationshipModel)

_MODELS = {"PERSON": PersonModel, "RELATIONSHIP": RelationshipModel,
           "EVENT": EventModel, "EVIDENCE": EvidenceModel}


class SQLReferenceLookupRepository:
    def __init__(self, session: Session):
        self.session = session

    def find(self, entity_type: str, entity_id: UUID) -> ReferenceTarget | None:
        """Look up one fact id across every workspace (deleted rows included)."""
        model = _MODELS.get(entity_type)
        if model is None or entity_id is None:
            return None
        row = self.session.get(model, entity_id)
        if row is None:
            return None
        return ReferenceTarget(entity_type=entity_type, entity_id=entity_id,
                               workspace_id=row.workspace_id, deleted_at=row.deleted_at)
