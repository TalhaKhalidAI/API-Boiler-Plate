"""
Domain exceptions for the repository layer.

These are raised by repositories and translated to HTTP responses
by the route layer. They are deliberately NOT HTTPException subclasses —
the repository should not know about HTTP.
"""


# =====================================================================
# BASE
# =====================================================================

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


# =====================================================================
# USER / AUTH
# =====================================================================

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


class PasswordRequiredError(DomainError):
    default_message = "Password required for this action"


class IncorrectPasswordError(DomainError):
    default_message = "Incorrect password"


class RateLimitError(DomainError):
    """Raised when a caller exceeds a per-action rate limit (e.g. login)."""
    default_message = "Too many attempts. Please try again later."


# =====================================================================
# CHANNEL
# =====================================================================

class ChannelNotFoundError(DomainError):
    default_message = "Channel not found"


class ChannelAlreadyExistsError(DomainError):
    default_message = "Channel already exists for this user"


class DuplicateHandleError(DomainError):
    default_message = "Handle already taken"


class ChannelAlreadyDeletedError(DomainError):
    default_message = "Channel is already deleted"


class ChannelMemberNotFoundError(DomainError):
    default_message = "Channel member not found"


class DuplicateChannelMemberError(DomainError):
    default_message = "User is already a member of this channel"


class CannotAddOwnerAsMemberError(DomainError):
    """Owner is implicit; they must not appear in channel_members."""
    default_message = "Channel owner cannot be added as a member"


class InvalidMemberRoleError(DomainError):
    default_message = "Invalid member role"


# =====================================================================
# CHANNEL GRANTS
# =====================================================================

class ChannelGrantNotFoundError(DomainError):
    default_message = "Channel grant not found"


class DuplicateChannelGrantError(DomainError):
    default_message = "Grant already exists for this grantee"


class InvalidGranteeError(DomainError):
    """Raised when the grantee is invalid (self-grant, missing user, etc.)."""
    default_message = "Invalid grantee"


class GrantExpiredError(DomainError):
    default_message = "Grant has expired"


class GrantRevokedError(DomainError):
    default_message = "Grant has been revoked"


class GrantUsageExhaustedError(DomainError):
    default_message = "Grant has reached its maximum number of uses"


class DuplicateVideoGrantError(DomainError):
    default_message = "Grant already exists for this grantee"

class CommentLikeNotFoundError(DomainError):
    default_message = "Comment like not found"
class PermissionDeniedError(DomainError):
    """Raised when an authenticated user lacks the required permission."""
    default_message = "You don't have permission to perform this action"

class ValidationError(DomainError):
    default_message = "Invalid input"

class IntegrityError(DomainError):
    default_message = "Integraty error"