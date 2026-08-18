"""Domain-level exceptions.

Services raise these; `app/core/error_handlers.py` is the single place
that translates them into HTTP responses. Routers never construct an
`HTTPException` themselves — that would scatter the mapping from business
condition to status code across every endpoint instead of keeping it in
one place that's easy to audit.
"""


class DocumentInsightsError(Exception):
    """Base class for every domain error raised by the service layer."""


class RateLimitExceededError(DocumentInsightsError):
    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"user {user_id!r} already has the maximum number of active documents")


class DocumentNotFoundError(DocumentInsightsError):
    def __init__(self, document_id: str):
        self.document_id = document_id
        super().__init__(f"document {document_id!r} not found")
