# App/api/v1/Channels.py
"""
Channel endpoints.

CRUD:
    POST   /channels                      → create channel
    GET    /channels/{id}                 → get channel
    GET    /channels/handle/{handle}      → get by handle
    GET    /channels/me                   → my channel
    PATCH  /channels/{id}                 → update profile
    PUT    /channels/{id}/handle          → change handle
    DELETE /channels/{id}                 → soft-delete

Media:
    POST   /channels/{id}/banner          → upload banner
    POST   /channels/{id}/avatar          → upload avatar
"""
from typing import Optional
from fastapi import APIRouter, Depends, File, Query, UploadFile, Body
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.api.dependencies.auth import (
    get_current_active_user,
    get_current_user_optional,
)
from App.services.channel_service import ChannelService
from App.services.playlist_service import PlaylistService
from App.schemas.VideoSchema import ChannelListItem, ChannelListResponse

chanel_router = APIRouter(prefix="/channels", tags=["Channels"])


# ============================================================================
# PUBLIC LISTING
# ============================================================================

@chanel_router.get("", response_model=ChannelListResponse, summary="List public channels")
async def list_channels(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    Public directory of active channels, ordered by subscriber count.
    No authentication required.
    """
    channels = await ChannelService(db).list_channels_public(limit=limit, offset=offset)
    items = [ChannelListItem.model_validate(ch) for ch in channels]
    return ChannelListResponse(items=items, limit=limit, offset=offset, count=len(items))


# ============================================================================
# READ
# ============================================================================

@chanel_router.get("/{channel_id}")
async def get_channel(
    channel_id: int,
    current_user=Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Get channel by ID."""
    channel = await ChannelService(db).get_channel(channel_id)
    return {
        "id": channel.id,
        "handle": channel.handle,
        "display_name": channel.display_name,
        "description": channel.description,
        "banner_key": channel.banner_key,
        "avatar_key": channel.avatar_key,
        "is_verified": channel.is_verified,
        "is_active": channel.is_active,
        "subscriber_count": channel.subscriber_count,
        "video_count": channel.video_count,
        "total_view_count": channel.total_view_count,
        "created_at": channel.created_at,
    }


@chanel_router.get("/handle/{handle}")
async def get_channel_by_handle(
    handle: str,
    current_user=Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Get channel by handle (e.g. /channels/handle/tech-with-ali)."""
    channel = await ChannelService(db).get_channel_by_handle(handle)
    return {
        "id": channel.id,
        "handle": channel.handle,
        "display_name": channel.display_name,
        "description": channel.description,
        "banner_key": channel.banner_key,
        "avatar_key": channel.avatar_key,
        "is_verified": channel.is_verified,
        "subscriber_count": channel.subscriber_count,
        "video_count": channel.video_count,
    }


@chanel_router.get("/me")
async def get_my_channel(
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current user's channel."""
    channel = await ChannelService(db).get_my_channel(current_user)
    return {
        "id": channel.id,
        "handle": channel.handle,
        "display_name": channel.display_name,
        "description": channel.description,
        "banner_key": channel.banner_key,
        "avatar_key": channel.avatar_key,
        "video_count": channel.video_count,
        "subscriber_count": channel.subscriber_count,
    }


@chanel_router.get("/{channel_id}/page")
async def get_channel_page(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get channel + its recent public videos (for channel home page)."""
    return await ChannelService(db).get_channel_page(channel_id)


@chanel_router.get(
    "/{channel_id}/playlists",
    summary="List a channel's public playlists",
)
async def list_channel_playlists(
    channel_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    Public playlists (visibility=public) for a channel.
    No authentication required.
    """
    channel = await ChannelService(db).get_channel(channel_id)
    return await PlaylistService(db).list_channel_playlists(
        channel.owner_id, limit=limit, offset=offset
    )


# ============================================================================
# WRITE
# ============================================================================

@chanel_router.post("", status_code=201)
async def create_channel(
    handle: str = Body(...),
    display_name: str = Body(...),
    description: Optional[str] = Body(None),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a channel. Handle must be lowercase alphanumeric with hyphens
    (3-50 chars, no leading/trailing hyphen).
    """
    channel = await ChannelService(db).create_channel(
        current_user,
        handle=handle,
        display_name=display_name,
        description=description,
    )
    return {"id": channel.id, "handle": channel.handle}


@chanel_router.patch("/{channel_id}")
async def update_channel_profile(
    channel_id: int,
    display_name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update channel display name / description."""
    channel = await ChannelService(db).update_profile(
        channel_id, current_user,
        display_name=display_name, description=description,
    )
    return {"id": channel.id, "updated": True}


@chanel_router.put("/{channel_id}/handle")
async def update_handle(
    channel_id: int,
    new_handle: str = Body(..., embed=True),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the channel handle. Must be unique."""
    channel = await ChannelService(db).update_handle(
        channel_id, current_user, new_handle
    )
    return {"id": channel.id, "handle": channel.handle}


@chanel_router.delete("/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a channel."""
    await ChannelService(db).delete_channel(channel_id, current_user)


# ============================================================================
# MEDIA
# ============================================================================

@chanel_router.post("/{channel_id}/banner", status_code=200)
async def upload_banner(
    channel_id: int,
    file: UploadFile = File(...),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a banner (image, max 10 MB)."""
    channel = await ChannelService(db).upload_banner(
        channel_id, current_user, file=file
    )
    return {"channel_id": channel.id, "banner_key": channel.banner_key}


@chanel_router.post("/{channel_id}/avatar", status_code=200)
async def upload_avatar(
    channel_id: int,
    file: UploadFile = File(...),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload an avatar (image, max 5 MB)."""
    channel = await ChannelService(db).upload_avatar(
        channel_id, current_user, file=file
    )
    return {"channel_id": channel.id, "avatar_key": channel.avatar_key}