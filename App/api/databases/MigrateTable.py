# App/models/user.py
from sqlalchemy import Column, Integer, String, Boolean, DateTime, func,JSON,ForeignKey,BigInteger,Index,Text,CheckConstraint,Numeric,UniqueConstraint,SmallInteger,text
from sqlalchemy.dialects.postgresql import JSONB,UUID
from sqlalchemy.orm import relationship
from App.core.Connector import Base  # Import from new connector

class User(Base):  # Changed from Users to User (singular, PEP8)
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)  # Renamed for clarity
    profile_pic = Column(String(500), nullable=True)
    user_role = Column(String(50), default="user", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)  # Better name than 'disable'
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    disabled=Column(Boolean,default=False)
    permissions = Column(JSON, default={})
    is_deleted = Column(Boolean, default=False)
    deleted_at = Column(DateTime, nullable=True)


class Channel(Base):
    __tablename__ = "channels"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    channel_uuid = Column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=func.gen_random_uuid(),
    )
    owner_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    handle = Column(String(50), nullable=False, unique=True)
    display_name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    banner_key = Column(String(500), nullable=True)
    avatar_key = Column(String(500), nullable=True)

    is_verified = Column(Boolean, nullable=False, server_default="false")
    is_active = Column(Boolean, nullable=False, server_default="true")

    subscriber_count = Column(BigInteger, nullable=False, server_default="0")
    video_count = Column(BigInteger, nullable=False, server_default="0")
    total_view_count = Column(BigInteger, nullable=False, server_default="0")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    owner = relationship(
        "User", backref="channel", uselist=False, foreign_keys=[owner_id]
    )
    members = relationship(
        "ChannelMember", back_populates="channel", cascade="all, delete-orphan"
    )
    videos = relationship(
        "Video", back_populates="channel", cascade="all, delete-orphan"
    )
    subscriptions = relationship(
        "Subscription", back_populates="channel", cascade="all, delete-orphan"
    )
    grants = relationship(
        "ChannelGrant", back_populates="channel", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_channels_deleted_at", "deleted_at"),
        Index("ix_channels_owner_id", "owner_id"),
    )


# =====================================================================
# CHANNEL MEMBERS
# =====================================================================

class ChannelMember(Base):
    __tablename__ = "channel_members"

    channel_id = Column(
        BigInteger,
        ForeignKey("channels.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role = Column(String(20), nullable=False, server_default="viewer")
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    channel = relationship("Channel", back_populates="members")
    user = relationship("User", backref="channel_memberships")

    __table_args__ = (
        CheckConstraint("role IN ('editor','viewer')", name="ck_channel_members_role"),
        Index("ix_channel_members_user_id", "user_id"),
    )


# =====================================================================
# VIDEOS
# =====================================================================

class Video(Base):
    __tablename__ = "videos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    video_uuid = Column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=func.gen_random_uuid(),
    )
    channel_id = Column(
        BigInteger,
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
    )

    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    duration_seconds = Column(Numeric(10, 3), nullable=True)
    thumbnail_key = Column(String(500), nullable=True)

    storage_bucket = Column(String(100), nullable=False)
    storage_prefix = Column(String(500), nullable=False)
    master_playlist_key = Column(
        String(500), nullable=False, server_default="master.m3u8"
    )

    status = Column(String(20), nullable=False, server_default="processing")
    visibility = Column(String(20), nullable=False, server_default="private")

    view_count = Column(BigInteger, nullable=False, server_default="0")
    like_count = Column(BigInteger, nullable=False, server_default="0")
    dislike_count = Column(BigInteger, nullable=False, server_default="0")
    comment_count = Column(BigInteger, nullable=False, server_default="0")
    save_count = Column(BigInteger, nullable=False, server_default="0")
    share_count = Column(BigInteger, nullable=False, server_default="0")

    total_size_bytes = Column(BigInteger, nullable=False, server_default="0")

    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    channel = relationship("Channel", back_populates="videos")
    variants = relationship(
        "VideoVariant", back_populates="video", cascade="all, delete-orphan"
    )
    grants = relationship(
        "VideoGrant", back_populates="video", cascade="all, delete-orphan"
    )
    tags = relationship(
        "VideoTag", back_populates="video", cascade="all, delete-orphan"
    )
    categories = relationship(
        "VideoCategory", back_populates="video", cascade="all, delete-orphan"
    )
    reactions = relationship(
        "VideoReaction", back_populates="video", cascade="all, delete-orphan"
    )
    comments = relationship(
        "Comment", back_populates="video", cascade="all, delete-orphan"
    )
    playlist_entries = relationship(
        "PlaylistVideo", back_populates="video", cascade="all, delete-orphan"
    )
    saves = relationship(
        "SavedVideo", back_populates="video", cascade="all, delete-orphan"
    )
    impressions = relationship(
        "VideoImpression", back_populates="video", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('processing','ready','failed','deleted')",
            name="ck_videos_status",
        ),
        CheckConstraint(
            "visibility IN ('private','unlisted','public')",
            name="ck_videos_visibility",
        ),
        Index("ix_videos_channel_id_created_at", "channel_id", "created_at"),
        Index("ix_videos_channel_id_published_at", "channel_id", "published_at"),
        Index("ix_videos_status", "status"),
        Index("ix_videos_visibility_published_at", "visibility", "published_at"),
        Index("ix_videos_deleted_at", "deleted_at"),
    )


# =====================================================================
# VIDEO VARIANTS
# =====================================================================

class VideoVariant(Base):
    __tablename__ = "video_variants"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )

    quality = Column(String(20), nullable=False)
    codec = Column(String(20), nullable=False, server_default="h264")

    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    bitrate_kbps = Column(Integer, nullable=True)

    playlist_key = Column(String(500), nullable=False)
    segment_count = Column(Integer, nullable=True)
    size_bytes = Column(BigInteger, nullable=True)

    status = Column(String(20), nullable=False, server_default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    video = relationship("Video", back_populates="variants")

    __table_args__ = (
        CheckConstraint(
            "quality IN ('240p','360p','480p','720p','1080p','1440p','2160p')",
            name="ck_video_variants_quality",
        ),
        CheckConstraint(
            "codec IN ('h264','h265','av1','vp9')",
            name="ck_video_variants_codec",
        ),
        CheckConstraint(
            "status IN ('pending','processing','ready','failed')",
            name="ck_video_variants_status",
        ),
        UniqueConstraint(
            "video_id", "quality", "codec",
            name="uq_video_variants_video_quality_codec",
        ),
    )


# =====================================================================
# VIDEO GRANTS — exception to visibility='private'
# =====================================================================

class VideoGrant(Base):
    __tablename__ = "video_grants"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )

    grantee_type = Column(String(20), nullable=False)
    grantee_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    link_token = Column(String(64), nullable=True, unique=True)

    granted_by_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    can_view = Column(Boolean, nullable=False, server_default="true")
    can_download = Column(Boolean, nullable=False, server_default="false")
    can_reshare = Column(Boolean, nullable=False, server_default="false")

    password_hash = Column(String(255), nullable=True)
    max_uses = Column(Integer, nullable=True)
    use_count = Column(Integer, nullable=False, server_default="0")

    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    video = relationship("Video", back_populates="grants")
    grantee_user = relationship("User", foreign_keys=[grantee_user_id])
    granted_by = relationship("User", foreign_keys=[granted_by_id])

    __table_args__ = (
        CheckConstraint(
            "grantee_type IN ('user','link')",
            name="ck_video_grants_grantee_type",
        ),
        CheckConstraint(
            "(grantee_type='user' AND grantee_user_id IS NOT NULL AND link_token IS NULL) "
            "OR (grantee_type='link' AND link_token IS NOT NULL AND grantee_user_id IS NULL)",
            name="ck_video_grants_grantee_xor",
        ),
        CheckConstraint(
            "grantee_user_id IS NULL OR granted_by_id IS NULL OR granted_by_id <> grantee_user_id",
            name="ck_video_grants_no_self_grant",
        ),
        UniqueConstraint(
            "video_id", "grantee_user_id",
            name="uq_video_grants_video_grantee",
        ),
        Index(
            "ix_video_grants_grantee_user_id_created_at",
            "grantee_user_id", "created_at",
        ),
        Index("ix_video_grants_video_id_revoked_at", "video_id", "revoked_at"),
    )


# =====================================================================
# CHANNEL GRANTS — channel-wide private access
# =====================================================================

class ChannelGrant(Base):
    __tablename__ = "channel_grants"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    channel_id = Column(
        BigInteger, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )

    grantee_type = Column(String(20), nullable=False)
    grantee_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    link_token = Column(String(64), nullable=True, unique=True)

    granted_by_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    can_view = Column(Boolean, nullable=False, server_default="true")
    can_download = Column(Boolean, nullable=False, server_default="false")
    can_reshare = Column(Boolean, nullable=False, server_default="false")

    password_hash = Column(String(255), nullable=True)
    max_uses = Column(Integer, nullable=True)
    use_count = Column(Integer, nullable=False, server_default="0")

    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    channel = relationship("Channel", back_populates="grants")
    grantee_user = relationship("User", foreign_keys=[grantee_user_id])
    granted_by = relationship("User", foreign_keys=[granted_by_id])

    __table_args__ = (
        CheckConstraint(
            "grantee_type IN ('user','link')",
            name="ck_channel_grants_grantee_type",
        ),
        CheckConstraint(
            "(grantee_type='user' AND grantee_user_id IS NOT NULL AND link_token IS NULL) "
            "OR (grantee_type='link' AND link_token IS NOT NULL AND grantee_user_id IS NULL)",
            name="ck_channel_grants_grantee_xor",
        ),
        UniqueConstraint(
            "channel_id", "grantee_user_id",
            name="uq_channel_grants_channel_grantee",
        ),
        Index(
            "ix_channel_grants_grantee_user_id_created_at",
            "grantee_user_id", "created_at",
        ),
        Index("ix_channel_grants_channel_id_revoked_at", "channel_id", "revoked_at"),
    )


# =====================================================================
# TAGS
# =====================================================================

class Tag(Base):
    __tablename__ = "tags"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slug = Column(String(50), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    usage_count = Column(BigInteger, nullable=False, server_default="0")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    videos = relationship(
        "VideoTag", back_populates="tag", cascade="all, delete-orphan"
    )


class VideoTag(Base):
    __tablename__ = "video_tags"

    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id = Column(
        BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )

    video = relationship("Video", back_populates="tags")
    tag = relationship("Tag", back_populates="videos")

    __table_args__ = (
        Index("ix_video_tags_tag_id", "tag_id"),
    )


# =====================================================================
# CATEGORIES
# =====================================================================

class Category(Base):
    __tablename__ = "categories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slug = Column(String(50), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    icon_key = Column(String(500), nullable=True)
    parent_id = Column(
        BigInteger, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    display_order = Column(Integer, nullable=False, server_default="0")
    is_active = Column(Boolean, nullable=False, server_default="true")

    # Self-referential: DB-level ON DELETE SET NULL handles the cascade.
    # NO ORM cascade here, or SQLAlchemy + Postgres will double-cascade.
    children = relationship("Category", back_populates="parent")
    parent = relationship("Category", back_populates="children", remote_side=[id])

    videos = relationship(
        "VideoCategory", back_populates="category", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_categories_parent_id", "parent_id"),
    )


class VideoCategory(Base):
    __tablename__ = "video_categories"

    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    category_id = Column(
        BigInteger, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )

    video = relationship("Video", back_populates="categories")
    category = relationship("Category", back_populates="videos")

    __table_args__ = (
        Index("ix_video_categories_category_id", "category_id"),
    )


# =====================================================================
# REACTIONS
# =====================================================================

class VideoReaction(Base):
    __tablename__ = "video_reactions"

    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    reaction = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    video = relationship("Video", back_populates="reactions")
    user = relationship("User", backref="video_reactions")

    __table_args__ = (
        CheckConstraint(
            "reaction IN ('like','dislike')",
            name="ck_video_reactions_reaction",
        ),
        Index("ix_video_reactions_user_id_created_at", "user_id", "created_at"),
    )


# =====================================================================
# COMMENTS
# =====================================================================

class Comment(Base):
    __tablename__ = "comments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    comment_uuid = Column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=func.gen_random_uuid(),
    )
    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    parent_comment_id = Column(
        BigInteger, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )
    depth = Column(SmallInteger, nullable=False, server_default="0")

    content = Column(Text, nullable=False)
    like_count = Column(BigInteger, nullable=False, server_default="0")
    reply_count = Column(BigInteger, nullable=False, server_default="0")
    is_pinned = Column(Boolean, nullable=False, server_default="false")
    is_edited = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    video = relationship("Video", back_populates="comments")
    user = relationship("User", backref="comments")

    # Self-referential: DB-level ON DELETE CASCADE handles children.
    # NO ORM cascade on `replies`, or you'll get double-delete errors.
    parent = relationship("Comment", back_populates="replies", remote_side=[id])
    replies = relationship("Comment", back_populates="parent")

    likes = relationship(
        "CommentLike", back_populates="comment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("depth BETWEEN 0 AND 3", name="ck_comments_depth"),
        Index("ix_comments_video_id_created_at", "video_id", "created_at"),
        Index("ix_comments_user_id_created_at", "user_id", "created_at"),
        Index("ix_comments_parent_comment_id", "parent_comment_id"),
        Index("ix_comments_deleted_at", "deleted_at"),
    )


class CommentLike(Base):
    __tablename__ = "comment_likes"

    comment_id = Column(
        BigInteger, ForeignKey("comments.id", ondelete="CASCADE"), primary_key=True
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    comment = relationship("Comment", back_populates="likes")


# =====================================================================
# PLAYLISTS
# =====================================================================

class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    playlist_uuid = Column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=func.gen_random_uuid(),
    )
    owner_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    visibility = Column(String(20), nullable=False, server_default="private")
    thumbnail_key = Column(String(500), nullable=True)
    video_count = Column(Integer, nullable=False, server_default="0")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    owner = relationship("User", backref="playlists")
    videos = relationship(
        "PlaylistVideo", back_populates="playlist", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "visibility IN ('private','unlisted','public')",
            name="ck_playlists_visibility",
        ),
        Index("ix_playlists_owner_id_created_at", "owner_id", "created_at"),
    )


class PlaylistVideo(Base):
    __tablename__ = "playlist_videos"

    playlist_id = Column(
        BigInteger, ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True
    )
    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    position = Column(Integer, nullable=False)
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    playlist = relationship("Playlist", back_populates="videos")
    video = relationship("Video", back_populates="playlist_entries")

    __table_args__ = (
        UniqueConstraint("playlist_id", "position", name="uq_playlist_videos_position"),
        Index("ix_playlist_videos_video_id", "video_id"),
    )


# =====================================================================
# SAVED VIDEOS
# =====================================================================

class SavedVideo(Base):
    __tablename__ = "saved_videos"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    saved_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", backref="saved_videos")
    video = relationship("Video", back_populates="saves")

    __table_args__ = (
        Index("ix_saved_videos_user_id_saved_at", "user_id", "saved_at"),
    )


# =====================================================================
# SUBSCRIPTIONS
# =====================================================================

class Subscription(Base):
    __tablename__ = "subscriptions"

    subscriber_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id = Column(
        BigInteger, ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    notify = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    subscriber = relationship("User", backref="subscriptions")
    channel = relationship("Channel", back_populates="subscriptions")

    __table_args__ = (
        Index(
            "ix_subscriptions_subscriber_id_created_at",
            "subscriber_id", "created_at",
        ),
        Index(
            "ix_subscriptions_channel_id_created_at",
            "channel_id", "created_at",
        ),
    )


# =====================================================================
# WATCH HISTORY
# =====================================================================

class WatchHistory(Base):
    __tablename__ = "watch_history"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    channel_id = Column(
        BigInteger, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    last_position_sec = Column(Integer, nullable=False, server_default="0")
    watched_seconds = Column(Integer, nullable=False, server_default="0")
    completed = Column(Boolean, nullable=False, server_default="false")
    last_watched_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", backref="watch_history")
    video = relationship("Video")
    channel = relationship("Channel")

    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_watch_history_user_video"),
        Index(
            "ix_watch_history_user_id_last_watched_at",
            "user_id", "last_watched_at",
        ),
        Index("ix_watch_history_channel_id", "channel_id"),
    )


# =====================================================================
# VIDEO IMPRESSIONS
# =====================================================================

class VideoImpression(Base):
    __tablename__ = "video_impressions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    session_id = Column(String(64), nullable=True)
    watched_seconds = Column(Integer, nullable=False, server_default="0")
    counted = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    video = relationship("Video", back_populates="impressions")
    user = relationship("User")

    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND session_id IS NULL) "
            "OR (user_id IS NULL AND session_id IS NOT NULL)",
            name="ck_video_impressions_user_or_session",
        ),
        Index(
            "ix_video_impressions_video_id_created_at",
            "video_id", "created_at",
        ),
        Index(
            "ix_video_impressions_user_id_created_at",
            "user_id", "created_at",
        ),
        Index("ix_video_impressions_counted", "counted"),
    )


# =====================================================================
# NOTIFICATIONS
# =====================================================================

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recipient_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    actor_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    type = Column(String(30), nullable=False)

    video_id = Column(
        BigInteger, ForeignKey("videos.id", ondelete="CASCADE"), nullable=True
    )
    comment_id = Column(
        BigInteger, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )
    channel_id = Column(
        BigInteger, ForeignKey("channels.id", ondelete="CASCADE"), nullable=True
    )

    payload = Column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    is_read = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    recipient = relationship(
        "User", foreign_keys=[recipient_id], backref="notifications_received"
    )
    actor = relationship("User", foreign_keys=[actor_id])
    video = relationship("Video")
    comment = relationship("Comment")
    channel = relationship("Channel")

    __table_args__ = (
        CheckConstraint(
            "type IN ('new_video','comment','reply','like','subscribe','mention','system')",
            name="ck_notifications_type",
        ),
        Index(
            "ix_notifications_recipient_id_is_read_created_at",
            "recipient_id", "is_read", "created_at",
        ),
        Index(
            "ix_notifications_recipient_id_created_at",
            "recipient_id", "created_at",
        ),
    )
