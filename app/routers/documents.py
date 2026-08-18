"""HTTP layer for document submission and lookup.

Deliberately thin: every handler just translates between HTTP and
`DocumentService` and carries no business logic of its own.
"""
from fastapi import APIRouter, Depends, status

from app.core.dependencies import get_document_service
from app.models.document import DocumentCreateRequest, DocumentResponse, DocumentSubmitResponse
from app.services.document_service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("", response_model=DocumentSubmitResponse, status_code=status.HTTP_201_CREATED)
async def submit_document(
    payload: DocumentCreateRequest,
    service: DocumentService = Depends(get_document_service),
) -> DocumentSubmitResponse:
    return await service.submit_document(payload)


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    return await service.get_document(document_id)
