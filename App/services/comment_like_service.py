# App/services/comment_like_service.py

from typing import Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.CommentRepository import CommentRepository
from App.core.exceptions import PermissionDeniedError

class CommentLikeService(BaseService):
    """Any authenticated user can like/unlike a comment."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.comments = CommentRepository(session)

    @staticmethod
    def _uid(u: Dict[str, Any]) -> int:
        uid = u.get("id")
        if not uid:
            raise PermissionDeniedError("Authentication required")
        return uid

    async def like(self, comment_id: int, current_user: Dict[str, Any]) -> bool:
        return await self.comments.like(comment_id, self._uid(current_user))

    async def unlike(self, comment_id: int, current_user: Dict[str, Any]) -> bool:
        return await self.comments.unlike(comment_id, self._uid(current_user))

    async def has_liked(self, comment_id: int, current_user: Dict[str, Any]) -> bool:
        return await self.comments.has_liked(comment_id, self._uid(current_user))

    async def list_likers(self, comment_id: int, *, limit: int = 50, offset: int = 0):
        return await self.comments.list_likers(comment_id, limit=limit, offset=offset)