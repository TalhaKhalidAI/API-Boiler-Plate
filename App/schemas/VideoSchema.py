# App/schemas/VideoSchema.py
"""
Pydantic response (and request) models for video endpoints.

Using ConfigDict(from_attributes=True) so every schema can be constructed
directly from a SQLAlchemy ORM model instance.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Upload flow
# ---------------------------------------------------------------------------

class InitUploadResponse(BaseModel):
    """Response for POST /videos/init-upload (Step 1)."""

    model_config = ConfigDict(from_attributes=True)

    video_id: int
    upload_url: str
    object_key: str
    expires_in: int
    content_type: str


class CompleteUploadResponse(BaseModel):
    """
    Response for POST /videos/{video_id}/complete-upload (Step 2).

    The client receives the video id and its new status so it can poll
    or subscribe for transcoding progress.
    """

    model_config = ConfigDict(from_attributes=True)

    video_id: int
    status: str


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

class PlaybackUrlResponse(BaseModel):
    """Response for GET /videos/{video_id}/playback-url."""

    model_config = ConfigDict(from_attributes=True)

    playback_url: str
    format: str
    content_type: str
    expires_in: int


# ---------------------------------------------------------------------------
# Video detail
# ---------------------------------------------------------------------------

class VideoResponse(BaseModel):
    """Full video metadata returned by GET /videos/{video_id}."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    title: str
    description: Optional[str] = None
    status: str
    visibility: str
    duration_seconds: Optional[float] = None
    thumbnail_key: Optional[str] = None
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Mutation acknowledgements
# ---------------------------------------------------------------------------

class VideoUpdatedResponse(BaseModel):
    """Generic acknowledgement for PATCH /videos/{video_id}."""

    model_config = ConfigDict(from_attributes=True)

    video_id: int
    updated: bool


class VideoVisibilityResponse(BaseModel):
    """Response for PUT /videos/{video_id}/visibility."""

    model_config = ConfigDict(from_attributes=True)

    video_id: int
    visibility: str


class VideoTagsResponse(BaseModel):
    """Response for PUT /videos/{video_id}/tags."""

    model_config = ConfigDict(from_attributes=True)

    video_id: int
    tag_ids: list[int]


class VideoCategoriesResponse(BaseModel):
    """Response for PUT /videos/{video_id}/categories."""

    model_config = ConfigDict(from_attributes=True)

    video_id: int
    category_ids: list[int]


class VideoThumbnailResponse(BaseModel):
    """Response for POST /videos/{video_id}/thumbnail."""

    model_config = ConfigDict(from_attributes=True)

    video_id: int
    thumbnail_key: Optional[str] = None


# ---------------------------------------------------------------------------
# Public listing
# ---------------------------------------------------------------------------

class VideoListItem(BaseModel):
    """
    Compact video card used in public listing endpoints.
    Does NOT include presigned URLs — clients call /playback-url separately.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    title: str
    description: Optional[str] = None
    status: str
    visibility: str
    duration_seconds: Optional[float] = None
    thumbnail_key: Optional[str] = None
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class VideoListResponse(BaseModel):
    """Paginated list of public videos."""

    items: list[VideoListItem]
    limit: int
    offset: int
    count: int  # number of items in this page


class ChannelListItem(BaseModel):
    """Compact channel card used in the public channel directory."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    handle: str
    display_name: str
    description: Optional[str] = None
    avatar_key: Optional[str] = None
    banner_key: Optional[str] = None
    is_verified: bool = False
    subscriber_count: int = 0
    video_count: int = 0
    created_at: Optional[datetime] = None


class ChannelListResponse(BaseModel):
    """Paginated list of public channels."""

    items: list[ChannelListItem]
    limit: int
    offset: int
    count: int


class ChannelVideoListResponse(BaseModel):
    """Paginated public videos for a specific channel."""

    channel_id: int
    items: list[VideoListItem]
    limit: int
    offset: int
    count: int
