# App/services/playlist_service.py
"""
Playlists.

Permission keys:
  - playlist.create.self / playlist.update.self / playlist.delete.self
  - admin.playlist.delete

Storage policy:
  - Thumbnail uploads go through FastAPI (< 10 MB).
  - MinIO keys derived from playlist_id.
"""

import json
from typing import Any, Dict, List, Optional

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.PlaylistRepository import PlaylistRepository
from App.repository.VideoRepository import VideoRepository
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger
from App.core.exceptions import (
    PlaylistNotFoundError,
    VideoNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from App.storage.minio_storage import minio_storage
from App.storage import paths as storage_paths


logger = get_core_logger(__name__)


class PlaylistService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.playlists = PlaylistRepository(session)
        self.videos = VideoRepository(session)

    # =====================================================================
    # HELPERS
    # =====================================================================

    @staticmethod
    def _normalize_permissions(p: Any) -> Dict[str, Any]:
        if not p:
            return {}
        if isinstance(p, str):
            try:
                return json.loads(p)
            except (TypeError, ValueError):
                return {}
        return p

    def _ctx(self, u: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": u.get("id"),
            "role": u.get("role") or u.get("user_role"),
            "permissions": self._normalize_permissions(u.get("permissions")),
        }

    async def _get_or_raise(self, playlist_id: int):
        pl = await self.playlists.get_by_id(playlist_id)
        if pl is None:
            raise PlaylistNotFoundError(f"Playlist {playlist_id} not found")
        return pl

    def _assert_can_modify(
        self,
        ctx: Dict[str, Any],
        owner_id: int,
        *,
        admin_perm: str,
        self_perm: str,
    ) -> None:
        if ctx["role"] == "admin" or ctx["permissions"].get(admin_perm, False):
            return
        if ctx["id"] == owner_id and ctx["permissions"].get(self_perm, False):
            return
        raise PermissionDeniedError("You don't have permission to modify this playlist")

    # =====================================================================
    # READ
    # =====================================================================

    async def get_playlist_page(self, playlist_id: int):
        pl = await self.playlists.get_with_entries(playlist_id)
        if pl is None:
            raise PlaylistNotFoundError(f"Playlist {playlist_id} not found")
        return pl

    async def list_my_playlists(
        self, current_user: Dict[str, Any], *, limit: int = 20, offset: int = 0
    ) -> List:
        ctx = self._ctx(current_user)
        if not ctx["id"]:
            raise PermissionDeniedError("Authentication required")
        return await self.playlists.list_by_owner(ctx["id"], limit=limit, offset=offset)

    async def list_public_playlists(
        self, *, limit: int = 20, offset: int = 0
    ) -> List:
        return await self.playlists.list_public(limit=limit, offset=offset)

    async def list_channel_playlists(
        self,
        owner_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List:
        """
        Public playlists for a channel's owner.
        Only returns visibility='public' playlists — private/unlisted are hidden.
        """
        return await self.playlists.list_by_owner(
            owner_id, visibility="public", limit=limit, offset=offset
        )


    # =====================================================================
    # WRITE
    # =====================================================================

    async def create_playlist(
        self,
        current_user: Dict[str, Any],
        *,
        title: str,
        description: Optional[str] = None,
        visibility: str = "private",
        thumbnail_key: Optional[str] = None,
    ):
        ctx = self._ctx(current_user)
        if not ctx["id"]:
            raise PermissionDeniedError("Authentication required")
        if not (
            ctx["role"] == "admin"
            or ctx["permissions"].get("playlist.create.self", False)
        ):
            raise PermissionDeniedError("You don't have permission to create playlists")
        return await self.playlists.create(
            owner_id=ctx["id"],
            title=title,
            description=description,
            visibility=visibility,
            thumbnail_key=thumbnail_key,
        )

    async def update_playlist(
        self,
        playlist_id: int,
        current_user: Dict[str, Any],
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        visibility: Optional[str] = None,
        thumbnail_key: Optional[str] = None,
    ):
        ctx = self._ctx(current_user)
        pl = await self._get_or_raise(playlist_id)
        self._assert_can_modify(
            ctx, pl.owner_id,
            admin_perm="admin.playlist.delete",
            self_perm="playlist.update.self",
        )
        return await self.playlists.update(
            playlist_id,
            title=title,
            description=description,
            visibility=visibility,
            thumbnail_key=thumbnail_key,
        )

    async def delete_playlist(
        self, playlist_id: int, current_user: Dict[str, Any]
    ) -> None:
        ctx = self._ctx(current_user)
        pl = await self._get_or_raise(playlist_id)
        self._assert_can_modify(
            ctx, pl.owner_id,
            admin_perm="admin.playlist.delete",
            self_perm="playlist.delete.self",
        )
        await self.playlists.soft_delete(playlist_id)

    # =====================================================================
    # ENTRIES
    # =====================================================================

    async def add_video(
        self, playlist_id: int, video_id: int, current_user: Dict[str, Any]
    ):
        ctx = self._ctx(current_user)
        pl = await self._get_or_raise(playlist_id)
        self._assert_can_modify(
            ctx, pl.owner_id,
            admin_perm="admin.playlist.delete",
            self_perm="playlist.update.self",
        )
        if await self.videos.get_by_id(video_id) is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        return await self.playlists.add_video(playlist_id, video_id)

    async def remove_video(
        self, playlist_id: int, video_id: int, current_user: Dict[str, Any]
    ) -> None:
        ctx = self._ctx(current_user)
        pl = await self._get_or_raise(playlist_id)
        self._assert_can_modify(
            ctx, pl.owner_id,
            admin_perm="admin.playlist.delete",
            self_perm="playlist.update.self",
        )
        await self.playlists.remove_video(playlist_id, video_id)

    async def reorder_video(
        self,
        playlist_id: int,
        video_id: int,
        new_position: int,
        current_user: Dict[str, Any],
    ) -> None:
        ctx = self._ctx(current_user)
        pl = await self._get_or_raise(playlist_id)
        self._assert_can_modify(
            ctx, pl.owner_id,
            admin_perm="admin.playlist.delete",
            self_perm="playlist.update.self",
        )
        await self.playlists.reorder_video(playlist_id, video_id, new_position)

    # =====================================================================
    # MINIO — THUMBNAIL
    # =====================================================================

    async def upload_thumbnail(
        self,
        playlist_id: int,
        current_user: Dict[str, Any],
        *,
        file: UploadFile,
    ):
        """Upload a thumbnail image for the playlist."""
        ctx = self._ctx(current_user)
        pl = await self._get_or_raise(playlist_id)
        self._assert_can_modify(
            ctx, pl.owner_id,
            admin_perm="admin.playlist.delete",
            self_perm="playlist.update.self",
        )

        if not file.content_type or not file.content_type.startswith("image/"):
            raise ValidationError("Thumbnail must be an image")

        data = await file.read()
        if len(data) == 0:
            raise ValidationError("Thumbnail is empty")
        if len(data) > 10 * 1024 * 1024:
            raise ValidationError("Thumbnail too large (max 10 MB)")

        key = storage_paths.playlist_thumbnail_key(playlist_id)

        await minio_storage.ensure_bucket(settings.MINIO_DEFAULT_BUCKET)
        await minio_storage.put_object(
            bucket=settings.MINIO_DEFAULT_BUCKET,
            key=key,
            data=data,
            content_type=file.content_type or "image/jpeg",
        )

        logger.info(f"Thumbnail uploaded for playlist {playlist_id} key={key}")

        return await self.playlists.update(
            playlist_id,
            thumbnail_key=storage_paths.THUMBNAIL_FILENAME,
        )