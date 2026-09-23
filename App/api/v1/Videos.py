# App/api/v1/Videos.py
"""
Video endpoints: upload orchestration + playback.

Upload flow (3 steps):
    1. POST /videos/init-upload           → presigned PUT URL
    2. [client PUTs directly to MinIO]
    3. POST /videos/{id}/complete-upload  → verify + trigger worker

Playback:
    GET /videos/{id}/playback-url         → presigned GET URL

Management:
    GET    /videos/{id}                   → video details
    PATCH  /videos/{id}                   → update metadata
    DELETE /videos/{id}                   → soft-delete
    POST   /videos/{id}/thumbnail         → upload thumbnail
    PUT    /videos/{id}/visibility        → publish/unpublish
    PUT    /videos/{id}/tags              → set tags
    PUT    /videos/{id}/categories        → set categories
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, File, Form, UploadFile, Query, Body
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.api.dependencies.auth import (
    get_current_active_user,
    get_current_user_optional,
)
from App.services.video_service import VideoService
from App.schemas.VideoSchema import (
    InitUploadResponse,
    CompleteUploadResponse,
    PlaybackUrlResponse,
    VideoResponse,
    VideoUpdatedResponse,
    VideoVisibilityResponse,
    VideoTagsResponse,
    VideoCategoriesResponse,
    VideoThumbnailResponse,
    VideoListItem,
    VideoListResponse,
    ChannelVideoListResponse,
)

video_router = APIRouter(prefix="/videos", tags=["Videos"])


# ============================================================================
# UPLOAD FLOW
# ============================================================================

@video_router.post("/init-upload", status_code=200, response_model=InitUploadResponse)
async def init_video_upload(
    channel_id: int = Form(...),
    title: str = Form(...),
    filename: str = Form(...),
    content_type: str = Form(...),
    description: Optional[str] = Form(None),
    file_size: Optional[int] = Form(None),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Step 1 of video upload.

    Returns a presigned PUT URL. The client uploads the file **directly**
    to MinIO (FastAPI never sees the bytes). Then calls /complete-upload.
    """
    return await VideoService(db).init_video_upload(
        current_user,
        channel_id=channel_id,
        title=title,
        filename=filename,
        content_type=content_type,
        description=description,
        file_size=file_size,
    )


@video_router.post(
    "/{video_id}/complete-upload",
    status_code=200,
    response_model=CompleteUploadResponse,
)
async def complete_video_upload(
    video_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Step 2 of video upload.

    Verifies the object exists in MinIO. The transcode worker picks up
    the video automatically (status='processing').
    """
    video = await VideoService(db).complete_video_upload(video_id, current_user)
    return CompleteUploadResponse(video_id=video.id, status=video.status)


# ============================================================================
# PLAYBACK
# ============================================================================

@video_router.get("/{video_id}/playback-url", response_model=PlaybackUrlResponse)
async def get_playback_url(
    video_id: int,
    format: str = Query("hls", pattern="^(hls|dash)$"),
    current_user=Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a presigned URL for the manifest (HLS or DASH).
    Access control is enforced before generating the URL.
    """
    return await VideoService(db).get_video_playback_url(
        video_id, current_user, format=format
    )


# ============================================================================
# PUBLIC LISTING
# ============================================================================

@video_router.get("", response_model=VideoListResponse, summary="List public videos")
async def list_videos(
    tag_id: Optional[int] = Query(None, description="Filter by tag ID"),
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    Public feed of ready + public videos. No authentication required.

    Optional filters (mutually exclusive, tag_id takes priority):
    - `tag_id`      — videos with a specific tag
    - `category_id` — videos in a specific category
    """
    videos = await VideoService(db).list_public_videos(
        tag_id=tag_id,
        category_id=category_id,
        limit=limit,
        offset=offset,
    )
    items = [VideoListItem.model_validate(v) for v in videos]
    return VideoListResponse(items=items, limit=limit, offset=offset, count=len(items))


@video_router.get(
    "/channel/{channel_id}",
    response_model=ChannelVideoListResponse,
    summary="List a channel's public videos",
)
async def list_channel_videos(
    channel_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    Public videos (status=ready, visibility=public) for a specific channel.
    No authentication required.
    """
    videos = await VideoService(db).list_channel_videos(
        channel_id,
        visibility="public",
        status="ready",
        limit=limit,
        offset=offset,
    )
    items = [VideoListItem.model_validate(v) for v in videos]
    return ChannelVideoListResponse(
        channel_id=channel_id,
        items=items,
        limit=limit,
        offset=offset,
        count=len(items),
    )


# ============================================================================
# READ
# ============================================================================

@video_router.get("/{video_id}", response_model=VideoResponse)
async def get_video(
    video_id: int,
    current_user=Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Get video metadata (no playback URL)."""
    video = await VideoService(db).get_video(video_id)
    return VideoResponse.model_validate(video)


@video_router.get("/{video_id}/watch")
async def get_watch_page(
    video_id: int,
    current_user=Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Get video with variants, tags, categories for the watch page."""
    video = await VideoService(db).get_watch_page(video_id)
    return video


# ============================================================================
# UPDATE
# ============================================================================

@video_router.patch("/{video_id}", response_model=VideoUpdatedResponse)
async def update_video_metadata(
    video_id: int,
    title: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    thumbnail_key: Optional[str] = Body(None),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update video title/description/thumbnail_key."""
    video = await VideoService(db).update_metadata(
        video_id, current_user,
        title=title, description=description, thumbnail_key=thumbnail_key,
    )
    return VideoUpdatedResponse(video_id=video.id, updated=True)


@video_router.put("/{video_id}/visibility", response_model=VideoVisibilityResponse)
async def set_visibility(
    video_id: int,
    visibility: str = Body(..., embed=True),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Set video visibility: private | unlisted | public."""
    video = await VideoService(db).set_visibility(video_id, current_user, visibility)
    return VideoVisibilityResponse(video_id=video.id, visibility=video.visibility)


@video_router.put("/{video_id}/tags", response_model=VideoTagsResponse)
async def set_tags(
    video_id: int,
    tag_ids: List[int] = Body(..., embed=True),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the tag set for a video."""
    await VideoService(db).set_tags(video_id, current_user, tag_ids)
    return VideoTagsResponse(video_id=video_id, tag_ids=tag_ids)


@video_router.put("/{video_id}/categories", response_model=VideoCategoriesResponse)
async def set_categories(
    video_id: int,
    category_ids: List[int] = Body(..., embed=True),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the category set for a video."""
    await VideoService(db).set_categories(video_id, current_user, category_ids)
    return VideoCategoriesResponse(video_id=video_id, category_ids=category_ids)


# ============================================================================
# THUMBNAIL
# ============================================================================

@video_router.post("/{video_id}/thumbnail", status_code=200, response_model=VideoThumbnailResponse)
async def upload_thumbnail(
    video_id: int,
    file: UploadFile = File(...),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a custom thumbnail image (max 10 MB)."""
    video = await VideoService(db).update_thumbnail(
        video_id, current_user, file=file
    )
    return VideoThumbnailResponse(video_id=video.id, thumbnail_key=video.thumbnail_key)


# ============================================================================
# DELETE
# ============================================================================

@video_router.delete("/{video_id}", status_code=204)
async def delete_video(
    video_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete video + fire-and-forget MinIO cleanup."""
    await VideoService(db).delete_video(video_id, current_user)