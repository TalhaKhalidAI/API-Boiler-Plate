# App/api/v1/Playlists.py
"""
Playlist endpoints.

CRUD:
    POST   /playlists                     → create
    GET    /playlists/mine                → my playlists
    GET    /playlists/public              → public playlists
    GET    /playlists/{id}                → playlist page
    PATCH  /playlists/{id}                → update
    DELETE /playlists/{id}                → soft-delete

Entries:
    POST   /playlists/{id}/videos         → add video
    DELETE /playlists/{id}/videos/{vid}   → remove video
    PUT    /playlists/{id}/reorder        → reorder

Media:
    POST   /playlists/{id}/thumbnail      → upload thumbnail
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, File, UploadFile, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.api.dependencies.auth import (
    get_current_active_user,
    get_current_user_optional,
)
from App.services.playlist_service import PlaylistService

router = APIRouter(prefix="/playlists", tags=["Playlists"])


# ============================================================================
# READ
# ============================================================================

@router.get("/mine")
async def list_my_playlists(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List playlists owned by the current user."""
    return await PlaylistService(db).list_my_playlists(
        current_user, limit=limit, offset=offset
    )


@router.get("/public")
async def list_public_playlists(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List public playlists (no auth)."""
    return await PlaylistService(db).list_public_playlists(
        limit=limit, offset=offset
    )


@router.get("/{playlist_id}")
async def get_playlist_page(
    playlist_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get playlist with entries + videos."""
    return await PlaylistService(db).get_playlist_page(playlist_id)


# ============================================================================
# WRITE
# ============================================================================

@router.post("", status_code=201)
async def create_playlist(
    title: str = Body(...),
    description: Optional[str] = Body(None),
    visibility: str = Body("private"),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a playlist."""
    pl = await PlaylistService(db).create_playlist(
        current_user,
        title=title, description=description, visibility=visibility,
    )
    return {"id": pl.id, "title": pl.title, "visibility": pl.visibility}


@router.patch("/{playlist_id}")
async def update_playlist(
    playlist_id: int,
    title: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    visibility: Optional[str] = Body(None),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update playlist metadata."""
    pl = await PlaylistService(db).update_playlist(
        playlist_id, current_user,
        title=title, description=description, visibility=visibility,
    )
    return {"id": pl.id, "updated": True}


@router.delete("/{playlist_id}", status_code=204)
async def delete_playlist(
    playlist_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a playlist."""
    await PlaylistService(db).delete_playlist(playlist_id, current_user)


# ============================================================================
# ENTRIES
# ============================================================================

@router.post("/{playlist_id}/videos", status_code=201)
async def add_video(
    playlist_id: int,
    video_id: int = Body(..., embed=True),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a video to a playlist."""
    entry = await PlaylistService(db).add_video(
        playlist_id, video_id, current_user
    )
    return {"playlist_id": playlist_id, "video_id": video_id, "added": True}


@router.delete("/{playlist_id}/videos/{video_id}", status_code=204)
async def remove_video(
    playlist_id: int,
    video_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a video from a playlist."""
    await PlaylistService(db).remove_video(playlist_id, video_id, current_user)


@router.put("/{playlist_id}/reorder")
async def reorder_video(
    playlist_id: int,
    video_id: int = Body(...),
    new_position: int = Body(...),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Move a video to a new position in the playlist."""
    await PlaylistService(db).reorder_video(
        playlist_id, video_id, new_position, current_user
    )
    return {"playlist_id": playlist_id, "video_id": video_id, "new_position": new_position}


# ============================================================================
# MEDIA
# ============================================================================

@router.post("/{playlist_id}/thumbnail", status_code=200)
async def upload_thumbnail(
    playlist_id: int,
    file: UploadFile = File(...),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a playlist thumbnail (max 10 MB)."""
    pl = await PlaylistService(db).upload_thumbnail(
        playlist_id, current_user, file=file
    )
    return {"playlist_id": pl.id, "thumbnail_key": pl.thumbnail_key}