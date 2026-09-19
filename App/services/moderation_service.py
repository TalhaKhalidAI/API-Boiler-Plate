# App/services/moderation_service.py

import json
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.VideoRepository import VideoRepository
from App.repository.CommentRepository import CommentRepository
from App.repository.UserRepository import UserRepository
from App.core.exceptions import VideoNotFoundError, CommentNotFoundError, UserNotFoundError,PermissionDeniedError


class ModerationService(BaseService):
    """
    Admin-only moderation actions across aggregates.

    Permission keys:
      - admin.moderate
      - admin.video.delete / admin.comment.delete / admin.users.disable
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.videos = VideoRepository(session)
        self.comments = CommentRepository(session)
        self.users = UserRepository(session)

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

    def _assert_moderator(self, u: Dict[str, Any]) -> None:
        role = u.get("role") or u.get("user_role")
        perms = self._normalize_permissions(u.get("permissions"))
        if role == "admin" or perms.get("admin.moderate"):
            return
        raise PermissionDeniedError("You don't have permission to moderate")

    async def hide_video(self, video_id: int, current_user: Dict[str, Any]) -> None:
        self._assert_moderator(current_user)
        video = await self.videos.get_by_id(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        await self.videos.set_visibility(video_id, "private")

    async def delete_video(self, video_id: int, current_user: Dict[str, Any]) -> None:
        self._assert_moderator(current_user)
        video = await self.videos.get_by_id(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        await self.videos.soft_delete(video_id)

    async def delete_comment(self, comment_id: int, current_user: Dict[str, Any]) -> None:
        self._assert_moderator(current_user)
        comment = await self.comments.get_by_id(comment_id)
        if comment is None:
            raise CommentNotFoundError(f"Comment {comment_id} not found")
        await self.comments.soft_delete(comment_id)

    async def hard_delete_comment(self, comment_id: int, current_user: Dict[str, Any]) -> None:
        self._assert_moderator(current_user)
        comment = await self.comments.get_by_id(comment_id, include_deleted=True)
        if comment is None:
            raise CommentNotFoundError(f"Comment {comment_id} not found")
        await self.comments.hard_delete(comment_id)

    async def disable_user(self, user_id: int, current_user: Dict[str, Any]) -> None:
        self._assert_moderator(current_user)
        target = await self.users.get_by_id(user_id)
        if target is None:
            raise UserNotFoundError(f"User {user_id} not found")
        await self.users.disable_account(user_id)