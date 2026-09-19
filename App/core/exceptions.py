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


# =====================================================================
# VIDEO
# =====================================================================

class VideoNotFoundError(DomainError):
    default_message = "Video not found"


class VideoAlreadyDeletedError(DomainError):
    default_message = "Video is already deleted"


class VideoNotReadyError(DomainError):
    """Raised when trying to publish/watch a video that isn't transcoded yet."""
    default_message = "Video is not ready"


class InvalidVideoStatusError(DomainError):
    default_message = "Invalid video status"


class InvalidVideoVisibilityError(DomainError):
    default_message = "Invalid video visibility"


class VideoAccessDeniedError(DomainError):
    """Raised when a user has no right to view/download a video."""
    default_message = "Access denied"


# =====================================================================
# VIDEO VARIANTS
# =====================================================================

class VideoVariantNotFoundError(DomainError):
    default_message = "Video variant not found"


class DuplicateVideoVariantError(DomainError):
    default_message = "Variant already exists for this video/quality/codec"


class InvalidVariantQualityError(DomainError):
    default_message = "Invalid variant quality"


class InvalidVariantCodecError(DomainError):
    default_message = "Invalid variant codec"


# =====================================================================
# VIDEO GRANTS
# =====================================================================

class VideoGrantNotFoundError(DomainError):
    default_message = "Video grant not found"


class DuplicateVideoGrantError(DomainError):
    default_message = "Grant already exists for this grantee"


# =====================================================================
# TAGS
# =====================================================================

class TagNotFoundError(DomainError):
    default_message = "Tag not found"


class DuplicateTagError(DomainError):
    default_message = "Tag already exists"


class InvalidTagSlugError(DomainError):
    default_message = "Invalid tag slug"


# =====================================================================
# CATEGORIES
# =====================================================================

class CategoryNotFoundError(DomainError):
    default_message = "Category not found"


class DuplicateCategoryError(DomainError):
    default_message = "Category already exists"


class InvalidCategoryParentError(DomainError):
    """Raised when the parent would create a cycle or is inactive."""
    default_message = "Invalid parent category"


# =====================================================================
# COMMENTS
# =====================================================================

class CommentNotFoundError(DomainError):
    default_message = "Comment not found"


class CommentAlreadyDeletedError(DomainError):
    default_message = "Comment is already deleted"


class CommentDepthExceededError(DomainError):
    """Raised when a reply would exceed the max nesting depth (3)."""
    default_message = "Maximum reply depth exceeded"


class InvalidCommentParentError(DomainError):
    """Raised when the parent comment belongs to a different video."""
    default_message = "Invalid parent comment"


class CommentLikeNotFoundError(DomainError):
    default_message = "Comment like not found"


# =====================================================================
# PLAYLISTS
# =====================================================================

class PlaylistNotFoundError(DomainError):
    default_message = "Playlist not found"


class PlaylistAlreadyDeletedError(DomainError):
    default_message = "Playlist is already deleted"


class DuplicatePlaylistVideoError(DomainError):
    default_message = "Video already exists in this playlist"


class PlaylistVideoNotFoundError(DomainError):
    default_message = "Video not found in this playlist"


class PlaylistPositionConflictError(DomainError):
    """Raised when a position insert would violate the unique constraint."""
    default_message = "Position already occupied in playlist"


# =====================================================================
# SAVED VIDEOS
# =====================================================================

class VideoAlreadySavedError(DomainError):
    default_message = "Video is already saved"


class VideoNotSavedError(DomainError):
    default_message = "Video is not saved"


# =====================================================================
# SUBSCRIPTIONS
# =====================================================================

class SubscriptionNotFoundError(DomainError):
    default_message = "Subscription not found"


class AlreadySubscribedError(DomainError):
    default_message = "Already subscribed to this channel"


class NotSubscribedError(DomainError):
    default_message = "Not subscribed to this channel"


class CannotSubscribeToSelfError(DomainError):
    default_message = "Cannot subscribe to your own channel"


# =====================================================================
# WATCH HISTORY
# =====================================================================

class WatchHistoryNotFoundError(DomainError):
    default_message = "Watch history entry not found"


class InvalidWatchPositionError(DomainError):
    """Raised when last_position_sec is negative or exceeds duration."""
    default_message = "Invalid watch position"


# =====================================================================
# VIDEO IMPRESSIONS
# =====================================================================

class InvalidImpressionError(DomainError):
    """Raised when neither user_id nor session_id is provided (or both)."""
    default_message = "Impression must have exactly one of user_id or session_id"


# =====================================================================
# NOTIFICATIONS
# =====================================================================

class NotificationNotFoundError(DomainError):
    default_message = "Notification not found"


class InvalidNotificationTypeError(DomainError):
    default_message = "Invalid notification type"


# =====================================================================
# INFRASTRUCTURE
# =====================================================================

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

class InvalidVideoReactionError(DomainError):
    default_message = "Invalid video reaction"


class CategoryNotFoundError(DomainError):
    default_message = "Category not found"

class DuplicateCategoryError(DomainError):
    default_message = "Category already exists"

class InvalidCategoryParentError(DomainError):
    """Raised when the parent would create a cycle or is inactive."""
    default_message = "Invalid parent category"

class CategoryAlreadyDeletedError(DomainError):
    default_message = "Category is already deleted"

class CommentNotFoundError(DomainError):
    default_message = "Comment not found"

class CommentAlreadyDeletedError(DomainError):
    default_message = "Comment is already deleted"

class CommentDepthExceededError(DomainError):
    """Raised when a reply would exceed the max nesting depth (3)."""
    default_message = "Maximum reply depth exceeded"

class InvalidCommentParentError(DomainError):
    """Raised when the parent comment belongs to a different video."""
    default_message = "Invalid parent comment"

class CommentLikeNotFoundError(DomainError):
    default_message = "Comment like not found"

class ImpressionNotFoundError(DomainError):
    default_message = "Impression not found"

class InvalidImpressionError(DomainError):
    """Raised when neither user_id nor session_id is provided (or both)."""
    default_message = "Impression must have exactly one of user_id or session_id"


class NotificationNotFoundError(DomainError):
    default_message = "Notification not found"

class InvalidNotificationTypeError(DomainError):
    default_message = "Invalid notification type"

class PlaylistNotFoundError(DomainError):
    default_message = "Playlist not found"

class PlaylistAlreadyDeletedError(DomainError):
    default_message = "Playlist is already deleted"

class DuplicatePlaylistVideoError(DomainError):
    default_message = "Video already exists in this playlist"

class PlaylistVideoNotFoundError(DomainError):
    default_message = "Video not found in this playlist"

class PlaylistPositionConflictError(DomainError):
    """Raised when a position insert would violate the unique constraint."""
    default_message = "Position already occupied in playlist"

class VideoAlreadySavedError(DomainError):
    default_message = "Video is already saved"

class VideoNotSavedError(DomainError):
    default_message = "Video is not saved"

# =====================================================================
# SUBSCRIPTIONS
# =====================================================================
class SubscriptionNotFoundError(DomainError):
    default_message = "Subscription not found"

class AlreadySubscribedError(DomainError):
    default_message = "Already subscribed to this channel"

class NotSubscribedError(DomainError):
    default_message = "Not subscribed to this channel"

class CannotSubscribeToSelfError(DomainError):
    default_message = "Cannot subscribe to your own channel"

# =====================================================================
# TAGS
# =====================================================================
class TagNotFoundError(DomainError):
    default_message = "Tag not found"

class DuplicateTagError(DomainError):
    default_message = "Tag already exists"

class InvalidTagSlugError(DomainError):
    default_message = "Invalid tag slug"

# =====================================================================
# WATCH HISTORY
# =====================================================================
class WatchHistoryNotFoundError(DomainError):
    default_message = "Watch history entry not found"

class InvalidWatchPositionError(DomainError):
    """Raised when last_position_sec is negative or exceeds duration."""
    default_message = "Invalid watch position"

class InvalidPlaylistVisibilityError(DomainError):
    default_message = "Invalid playlist visibility"

class InvalidChannelVisibilityError(DomainError):
    default_message = "Invalid channel visibility"


class PermissionDeniedError(DomainError):
    """Raised when an authenticated user lacks the required permission."""
    default_message = "You don't have permission to perform this action"

class ValidationError(DomainError):
    default_message = "Invalid input"
