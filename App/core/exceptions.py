"""
Domain exceptions for the repository layer.

These are raised by repositories and translated to HTTP responses
by the route layer. They are deliberately NOT HTTPException subclasses —
the repository should not know about HTTP.
"""


class DomainError(Exception):
    """Base class for all business-logic errors."""
    default_message = "A domain error occurred"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)

class InfrastructureError(DomainError):
    """Raised when a dependent service (Redis, external API) is unreachable.

    Subclass of DomainError so it's raised from the same layer, but routes
    must catch it *before* DomainError to map it to 503 instead of 401/400.
    """
    default_message = "Service temporarily unavailable"
class UserNotFoundError(DomainError):
    default_message = "User not found"


class DuplicateEmailError(DomainError):
    default_message = "Email already registered"


class DuplicateNameError(DomainError):
    default_message = "Name already taken"


class AccountAlreadyDisabledError(DomainError):
    default_message = "Account is already disabled"


class AccountNotDisabledError(DomainError):
    default_message = "Account is not disabled"


class AccountAlreadyDeletedError(DomainError):
    default_message = "Account is already deleted"


class AccountNotDeletedError(DomainError):
    default_message = "Account is not deleted"


class AdminCreationError(DomainError):
    default_message = "Failed to create admin"



class MinIOError(InfrastructureError):
    """Base for MinIO-specific errors. Subclass of InfrastructureError
    so routes that catch InfrastructureError already handle MinIO failures."""
    default_message = "Object storage service unavailable"


class MinIOBucketNotFoundError(MinIOError):
    """The requested bucket does not exist."""
    default_message = "Bucket not found"


class MinIOObjectNotFoundError(MinIOError):
    """The requested object does not exist."""
    default_message = "Object not found"


class MinIOAccessDeniedError(MinIOError):
    """The MinIO user lacks permission for this operation."""
    default_message = "Access denied by object storage"


class MinIOConnectionError(MinIOError):
    """Cannot reach the MinIO server."""
    default_message = "Cannot connect to object storage"


class RateLimitError(DomainError):
    """Raised when a caller exceeds a per-action rate limit (e.g. login)."""
    default_message = "Too many attempts. Please try again later."