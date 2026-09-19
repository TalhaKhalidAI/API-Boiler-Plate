# App/services/video_reaction_service.py

from typing import Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.exceptions import PermissionDeniedError

from App.services.base import BaseService
from App.repository.VideoRepository import VideoRepository


class VideoReactionService(BaseService):
    """Any authenticated user can like/dislike a video."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.videos = VideoRepository(session)

    @staticmethod
    def _uid(u: Dict[str, Any]) -> int:
        uid = u.get("id")
        if not uid:
            raise PermissionDeniedError("Authentication required")
        return uid

    async def set_reaction(self, video_id: int, current_user: Dict[str, Any], reaction: str) -> Dict[str, Any]:
        uid = self._uid(current_user)
        action, previous = await self.videos.upsert_reaction(video_id, uid, reaction)
        if action == "created":
            if reaction == "like":
                await self.videos.increment_like_count(video_id, by=1)
            else:
                await self.videos.increment_dislike_count(video_id, by=1)
        elif action == "updated":
            if previous == "like" and reaction == "dislike":
                await self.videos.increment_like_count(video_id, by=-1)
                await self.videos.increment_dislike_count(video_id, by=1)
            elif previous == "dislike" and reaction == "like":
                await self.videos.increment_dislike_count(video_id, by=-1)
                await self.videos.increment_like_count(video_id, by=1)
        return {"action": action, "previous": previous}

    async def remove_reaction(self, video_id: int, current_user: Dict[str, Any]):
        uid = self._uid(current_user)
        previous = await self.videos.delete_reaction(video_id, uid)
        if previous == "like":
            await self.videos.increment_like_count(video_id, by=-1)
        elif previous == "dislike":
            await self.videos.increment_dislike_count(video_id, by=-1)
        return previous

    async def get_reaction(self, video_id: int, current_user: Dict[str, Any]):
        uid = self._uid(current_user)
        return await self.videos.get_reaction(video_id, uid)