# App/services/watch_history_service.py

from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.exceptions import PermissionDeniedError

from App.services.base import BaseService
from App.repository.WatchHistoryRepository import WatchHistoryRepository


class WatchHistoryService(BaseService):
    """Self-only. Does NOT touch video.view_count (that's the impression worker)."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.history = WatchHistoryRepository(session)

    @staticmethod
    def _uid(u: Dict[str, Any]) -> int:
        uid = u.get("id")
        if not uid:
            raise PermissionDeniedError("Authentication required")
        return uid

    async def record_progress(
        self, current_user: Dict[str, Any], *,
        video_id: int, channel_id: int,
        last_position_sec: int = 0, watched_seconds: int = 0, completed: bool = False,
    ):
        uid = self._uid(current_user)
        return await self.history.upsert(
            user_id=uid, video_id=video_id, channel_id=channel_id,
            last_position_sec=last_position_sec,
            watched_seconds=watched_seconds, completed=completed,
        )

    async def mark_completed(self, video_id: int, current_user: Dict[str, Any], *, watched_seconds: Optional[int] = None):
        uid = self._uid(current_user)
        return await self.history.mark_completed(uid, video_id, watched_seconds=watched_seconds)

    async def get_progress(self, video_id: int, current_user: Dict[str, Any]):
        uid = self._uid(current_user)
        return await self.history.get(uid, video_id)

    async def list_continue_watching(self, current_user: Dict[str, Any], *, limit: int = 10) -> List:
        uid = self._uid(current_user)
        return await self.history.list_continue_watching(uid, limit=limit)

    async def list_my_history(self, current_user: Dict[str, Any], *, limit: int = 30, offset: int = 0) -> List:
        uid = self._uid(current_user)
        return await self.history.list_videos_for_user(uid, limit=limit, offset=offset)

    async def clear_history(self, current_user: Dict[str, Any]) -> int:
        uid = self._uid(current_user)
        return await self.history.clear_for_user(uid)

    async def delete_entry(self, video_id: int, current_user: Dict[str, Any]) -> None:
        uid = self._uid(current_user)
        await self.history.delete(uid, video_id)