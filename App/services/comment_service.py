# App/services/comment_service.py

import json
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.CommentRepository import CommentRepository
from App.repository.VideoRepository import VideoRepository
from App.core.exceptions import CommentNotFoundError,PermissionDeniedError


class CommentService(BaseService):
    """
    Comment CRUD.

    Permission keys:
      - comment.create.self
      - comment.update.self / comment.delete.self
      - comment.pin.self
      - admin.comment.delete / admin.comment.pin
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.comments = CommentRepository(session)
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

    def _ctx(self, u: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": u.get("id"),
            "role": u.get("role") or u.get("user_role"),
            "permissions": self._normalize_permissions(u.get("permissions")),
        }

    async def _get_or_raise(self, comment_id: int):
        c = await self.comments.get_by_id(comment_id)
        if c is None:
            raise CommentNotFoundError(f"Comment {comment_id} not found")
        return c

    # ---------- read ----------

    async def list_thread(self, video_id: int, *, limit: int = 20, offset: int = 0) -> List:
        return await self.comments.list_top_level_for_video(video_id, limit=limit, offset=offset)

    async def list_replies(self, parent_comment_id: int, *, limit: int = 20, offset: int = 0) -> List:
        return await self.comments.list_replies(parent_comment_id, limit=limit, offset=offset)

    async def get_comment(self, comment_id: int):
        return await self._get_or_raise(comment_id)

    # ---------- write ----------

    async def create_comment(
        self,
        video_id: int,
        current_user: Dict[str, Any],
        *,
        content: str,
        parent_comment_id: Optional[int] = None,
    ):
        ctx = self._ctx(current_user)
        if not ctx["id"]:
            raise PermissionDeniedError("Authentication required")
        if not (ctx["role"] == "admin" or ctx["permissions"].get("comment.create.self", False)):
            raise PermissionDeniedError("You don't have permission to comment")

        comment = await self.comments.create(
            video_id=video_id, user_id=ctx["id"], content=content,
            parent_comment_id=parent_comment_id,
        )
        if parent_comment_id is None:
            await self.videos.increment_comment_count(video_id, by=1)
        return comment

    async def update_comment(self, comment_id: int, current_user: Dict[str, Any], *, content: str):
        ctx = self._ctx(current_user)
        comment = await self._get_or_raise(comment_id)
        if ctx["role"] == "admin" or ctx["permissions"].get("admin.comment.delete", False):
            pass
        elif ctx["id"] == comment.user_id and ctx["permissions"].get("comment.update.self", False):
            pass
        else:
            raise PermissionDeniedError("You don't have permission to edit this comment")
        return await self.comments.update_content(comment_id, content)

    async def delete_comment(self, comment_id: int, current_user: Dict[str, Any]) -> None:
        ctx = self._ctx(current_user)
        comment = await self._get_or_raise(comment_id)
        if ctx["role"] == "admin" or ctx["permissions"].get("admin.comment.delete", False):
            pass
        elif ctx["id"] == comment.user_id and ctx["permissions"].get("comment.delete.self", False):
            pass
        else:
            raise PermissionDeniedError("You don't have permission to delete this comment")
        await self.comments.soft_delete(comment_id)
        if comment.parent_comment_id is None:
            await self.videos.increment_comment_count(comment.video_id, by=-1)

    async def pin(self, comment_id: int, current_user: Dict[str, Any]):
        ctx = self._ctx(current_user)
        if not (
            ctx["role"] == "admin"
            or ctx["permissions"].get("admin.comment.pin", False)
            or ctx["permissions"].get("comment.pin.self", False)
        ):
            raise PermissionDeniedError("You don't have permission to pin comments")
        return await self.comments.pin(comment_id)

    async def unpin(self, comment_id: int, current_user: Dict[str, Any]):
        ctx = self._ctx(current_user)
        if not (
            ctx["role"] == "admin"
            or ctx["permissions"].get("admin.comment.pin", False)
            or ctx["permissions"].get("comment.pin.self", False)
        ):
            raise PermissionDeniedError("You don't have permission to unpin comments")
        return await self.comments.unpin(comment_id)