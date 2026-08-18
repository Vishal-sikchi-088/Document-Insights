"""HTTP layer for user-scoped document queries."""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.core.dependencies import get_document_service
from app.models.document import PaginatedDocumentsResponse
from app.models.enums import DocumentStatus
from app.services.document_service import DocumentService

router = APIRouter(prefix="/users", tags=["users"])

# Settings are fixed for the process lifetime, so reading the pagination
# bounds once at import time (rather than via Depends on every request)
# keeps the route signature declarative — FastAPI validates `page_size`
# against the configured ceiling before the handler even runs.
_settings = get_settings()


@router.get("/{user_id}/documents", response_model=PaginatedDocumentsResponse)
async def list_user_documents(
    user_id: str,
    status: Optional[DocumentStatus] = Query(default=None, description="Filter by document status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=_settings.pagination_default_page_size,
        ge=1,
        le=_settings.pagination_max_page_size,
    ),
    service: DocumentService = Depends(get_document_service),
) -> PaginatedDocumentsResponse:
    return await service.list_user_documents(user_id, status=status, page=page, page_size=page_size)
