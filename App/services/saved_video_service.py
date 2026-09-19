# App/services/saved_video_service.py

from typing import Any, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.exceptions import PermissionDeniedError
from App.services.base import BaseService
from App.repository.SavedVideoRepository import SavedVideoRepository
from App.repository.VideoRepository import VideoRepository


class SavedVideoService(BaseService):
    """Self-only. Save/unsave bumps video.save_count in the same transaction."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.saved = SavedVideoRepository(session)
        self.videos = VideoRepository(session)

    @staticmethod
    def _uid(u: Dict[str, Any]) -> int:
        uid = u.get("id")
        if not uid:
            raise PermissionDeniedError("Authentication required")
        return uid

    async def save(self, video_id: int, current_user: Dict[str, Any]):
        uid = self._uid(current_user)
        row = await self.saved.save(uid, video_id)
        await self.videos.increment_save_count(video_id, by=1)
        return row

    async def unsave(self, video_id: int, current_user: Dict[str, Any]) -> None:
        uid = self._uid(current_user)
        await self.saved.unsave(uid, video_id)
        await self.videos.increment_save_count(video_id, by=-1)

    async def toggle(self, video_id: int, current_user: Dict[str, Any]) -> bool:
        uid = self._uid(current_user)
        state = await self.saved.toggle(uid, video_id)
        if state:
            await self.videos.increment_save_count(video_id, by=1)
        else:
            await self.videos.increment_save_count(video_id, by=-1)
        return state

    async def list_my_saves(self, current_user: Dict[str, Any], *,
                            only_available: bool = True, limit: int = 30, offset: int = 0) -> List:
        uid = self._uid(current_user)
        return await self.saved.list_videos_for_user(
            uid, only_available=only_available, limit=limit, offset=offset
        )

    async def is_saved(self, video_id: int, current_user: Dict[str, Any]) -> bool:
        uid = self._uid(current_user)
        return await self.saved.is_saved(uid, video_id)