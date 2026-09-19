# App/services/tag_service.py

import json
from typing import Any, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.TagRepository import TagRepository
from App.core.exceptions import TagNotFoundError,PermissionDeniedError


class TagService(BaseService):
    """
    Tags. Anyone can list; get_or_create is available to anyone with
    video.update.self (they're the entry point for attaching tags).

    Permission keys:
      - admin.tag.delete
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.tags = TagRepository(session)

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

    async def get_or_create(self, name: str, current_user: Dict[str, Any]):
        ctx = self._ctx(current_user)
        if not ctx["id"]:
            raise PermissionDeniedError("Authentication required")
        tag, created = await self.tags.get_or_create(name)
        return {"tag": tag, "created": created}

    async def get_tag(self, tag_id: int):
        tag = await self.tags.get_by_id(tag_id)
        if tag is None:
            raise TagNotFoundError(f"Tag {tag_id} not found")
        return tag

    async def list_popular(self, *, limit: int = 50, offset: int = 0) -> List:
        return await self.tags.list_popular(limit=limit, offset=offset)

    async def search(self, query: str, *, limit: int = 20) -> List:
        return await self.tags.search_by_name(query, limit=limit)

    async def delete_unused(self, current_user: Dict[str, Any]) -> int:
        ctx = self._ctx(current_user)
        if not (ctx["role"] == "admin" or ctx["permissions"].get("admin.tag.delete", False)):
            raise PermissionDeniedError("You don't have permission to delete tags")
        return await self.tags.delete_unused()

    async def get_tag_page(self, tag_id: int):
        tag = await self.get_tag(tag_id)
        videos = await self.tags.list_videos_for_tag(tag_id, limit=20)
        return {"tag": tag, "videos": videos}