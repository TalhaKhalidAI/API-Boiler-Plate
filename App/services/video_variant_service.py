# App/services/video_variant_service.py

import json
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.exceptions import PermissionDeniedError
from App.services.base import BaseService
from App.repository.VideoRepository import VideoRepository


class VideoVariantService(BaseService):
    """
    Worker-facing variant management.

    Permission keys:
      - worker.transcode
      - admin.video.update
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.videos = VideoRepository(session)

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

    def _assert_worker_or_admin(self, u: Dict[str, Any]) -> None:
        role = u.get("role") or u.get("user_role")
        perms = self._normalize_permissions(u.get("permissions"))
        if role == "admin" or perms.get("worker.transcode") or perms.get("admin.video.update"):
            return
        raise PermissionDeniedError("Only the transcode worker or an admin can manage variants")

    async def add_variant(
        self, current_user: Dict[str, Any], *,
        video_id: int, quality: str, codec: str, playlist_key: str,
        width: Optional[int] = None, height: Optional[int] = None, bitrate_kbps: Optional[int] = None,
    ):
        self._assert_worker_or_admin(current_user)
        return await self.videos.add_variant(
            video_id=video_id, quality=quality, codec=codec, playlist_key=playlist_key,
            width=width, height=height, bitrate_kbps=bitrate_kbps,
        )

    async def set_status(
        self, variant_id: int, current_user: Dict[str, Any], status: str, *,
        size_bytes: Optional[int] = None, segment_count: Optional[int] = None,
    ) -> None:
        self._assert_worker_or_admin(current_user)
        await self.videos.set_variant_status(
            variant_id, status, size_bytes=size_bytes, segment_count=segment_count
        )

    async def update_media_info(
        self, video_id: int, current_user: Dict[str, Any], *,
        duration_seconds: Optional[float] = None, total_size_bytes: Optional[int] = None,
    ):
        self._assert_worker_or_admin(current_user)
        return await self.videos.update_media_info(
            video_id, duration_seconds=duration_seconds, total_size_bytes=total_size_bytes
        )

    async def delete_variant(self, variant_id: int, current_user: Dict[str, Any]) -> None:
        self._assert_worker_or_admin(current_user)
        await self.videos.delete_variant(variant_id)

    async def list_ready_variants(self, video_id: int) -> List:
        return await self.videos.list_ready_variants(video_id)

    async def list_all_variants(self, video_id: int, current_user: Dict[str, Any]) -> List:
        self._assert_worker_or_admin(current_user)
        return await self.videos.list_all_variants(video_id)