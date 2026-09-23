# App/models/Permissions.py

from enum import Enum


class Permission(str, Enum):
    """Minimal permission set for a FastAPI boilerplate."""

    # =====================================================================
    # USER SELF-MANAGEMENT
    # =====================================================================
    USER_VIEW_SELF = "user.view.self"
    USER_UPDATE_SELF = "user.update.self"
    USER_UPDATE_EMAIL = "user.update.email"
    USER_UPDATE_PASSWORD = "user.update.password"
    USER_UPDATE_PROFILE = "user.update.profile"
    USER_DELETE_SELF = "user.delete.self"
    USER_HISTORY_VIEW = "user.history.view"
    USER_HISTORY_DELETE = "user.history.delete"
    USER_SELF_ENABLE = "user.self.enable"
    USER_SELF_DISABLE = "user.disable.self"

    # =====================================================================
    # USER MANAGEMENT (admin-level on others)
    # =====================================================================
    USER_VIEW_ANY = "user.view.any"
    USER_DELETE_ANY = "user.delete.any"
    USER_ENABLE = "user.enable"
    USER_DISABLE = "user.disable"
    USER_RESTORE = "user.restore"
    USER_PROMOTE = "user.promote"
    GET_USER_ASSIGNED_PERMISSIONS = "user.permission_assign.get"

    # =====================================================================
    # CHANNEL — OWN CHANNEL (self permissions)
    # =====================================================================
    CHANNEL_CREATE_SELF = "channel.create.self"
    CHANNEL_UPDATE_SELF = "channel.update.self"
    CHANNEL_DELETE_SELF = "channel.delete.self"
    CHANNEL_SELF_ENABLE = "channel.self.enable"
    CHANNEL_SELF_DISABLE = "channel.self.disable"
    CHANNEL_MEMBERS_MANAGE_SELF = "channel.members.manage.self"

    # =====================================================================
    # VIDEO — OWN VIDEO (self permissions)
    # =====================================================================
    VIDEO_CREATE_SELF = "video.create.self"
    VIDEO_UPDATE_SELF = "video.update.self"
    VIDEO_DELETE_SELF = "video.delete.self"
    VIDEO_PUBLISH_SELF = "video.publish.self"

    # =====================================================================
    # PLAYLIST — OWN PLAYLIST (self permissions)
    # =====================================================================
    PLAYLIST_CREATE_SELF = "playlist.create.self"
    PLAYLIST_UPDATE_SELF = "playlist.update.self"
    PLAYLIST_DELETE_SELF = "playlist.delete.self"

    # =====================================================================
    # COMMENT — OWN COMMENT (self permissions)
    # =====================================================================
    COMMENT_CREATE_SELF = "comment.create.self"
    COMMENT_UPDATE_SELF = "comment.update.self"
    COMMENT_DELETE_SELF = "comment.delete.self"
    COMMENT_PIN_SELF = "comment.pin.self"

    # =====================================================================
    # SUBSCRIPTION (self)
    # =====================================================================
    SUBSCRIPTION_CREATE_SELF = "subscription.create.self"

    # =====================================================================
    # ADMIN PERMISSIONS
    # =====================================================================
    ADMIN_ACCESS = "admin.access"
    ADMIN_ANY_PASSWORD_UPDATE = "admin.anyuser.password.update"
    ADMIN_USER_ENABLE = "admin.user.enable"
    ADMIN_USERS_VIEW = "admin.users.view"
    ADMIN_USERS_DISABLE = "admin.users.disable"
    ADMIN_USERS_DELETE = "admin.users.delete"
    ADMIN_USERS_RESTORE = "admin.users.restore"
    ADMIN_USERS_PROMOTE = "admin.users.promote"
    ADMIN_USERS = "admin.users"
    ADMIN_SETTINGS_VIEW = "admin.settings.view"
    ADMIN_SETTINGS_UPDATE = "admin.settings.update"
    ADMIN_VIEW_ALL = "admin.view_all"
    ADMIN_SYSTEM_KILL_SWITCH = "admin.system.kill_switch"

    # =====================================================================
    # ADMIN — CHANNEL
    # =====================================================================
    ADMIN_CHANNEL_CREATE = "admin.channel.create"
    ADMIN_CHANNEL_UPDATE = "admin.channel.update"
    ADMIN_CHANNEL_DELETE = "admin.channel.delete"
    ADMIN_CHANNEL_VERIFY = "admin.channel.verify"
    ADMIN_CHANNEL_ENABLE = "admin.channel.enable"
    ADMIN_CHANNEL_MEMBERS_MANAGE = "admin.channel.members.manage"

    # =====================================================================
    # ADMIN — VIDEO
    # =====================================================================
    ADMIN_VIDEO_UPDATE = "admin.video.update"
    ADMIN_VIDEO_DELETE = "admin.video.delete"
    ADMIN_VIDEO_PUBLISH = "admin.video.publish"

    # =====================================================================
    # ADMIN — COMMENT
    # =====================================================================
    ADMIN_COMMENT_DELETE = "admin.comment.delete"
    ADMIN_COMMENT_PIN = "admin.comment.pin"

    # =====================================================================
    # ADMIN — PLAYLIST
    # =====================================================================
    ADMIN_PLAYLIST_DELETE = "admin.playlist.delete"

    # =====================================================================
    # ADMIN — CATEGORY
    # =====================================================================
    ADMIN_CATEGORY_CREATE = "admin.category.create"
    ADMIN_CATEGORY_UPDATE = "admin.category.update"
    ADMIN_CATEGORY_DELETE = "admin.category.delete"

    # =====================================================================
    # ADMIN — TAG
    # =====================================================================
    ADMIN_TAG_DELETE = "admin.tag.delete"

    # =====================================================================
    # ADMIN — NOTIFICATION
    # =====================================================================
    ADMIN_NOTIFICATION_BROADCAST = "admin.notification.broadcast"

    # =====================================================================
    # ADMIN — MODERATION
    # =====================================================================
    ADMIN_MODERATE = "admin.moderate"

    # =====================================================================
    # WORKER SERVICE ACCOUNTS
    # =====================================================================
    WORKER_VIEW_COUNTING = "worker.view_counting"
    WORKER_IMPRESSION = "worker.impression"
    WORKER_TRANSCODE = "worker.transcode"