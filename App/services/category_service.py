# App/services/category_service.py

import json
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.exceptions import PermissionDeniedError
from App.services.base import BaseService
from App.repository.CategoryRepository import CategoryRepository
from App.core.exceptions import CategoryNotFoundError


class CategoryService(BaseService):
    """
    Categories. Only admins can mutate; anyone can read.

    Permission keys:
      - admin.category.create / admin.category.update / admin.category.delete
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.categories = CategoryRepository(session)

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

    def _assert_can(self, ctx: Dict[str, Any], perm: str) -> None:
        if ctx["role"] == "admin" or ctx["permissions"].get(perm, False):
            return
        raise PermissionDeniedError("You don't have permission to manage categories")

    # ---------- read ----------

    async def list_roots(self) -> List:
        return await self.categories.list_roots()

    async def list_children(self, parent_id: int) -> List:
        return await self.categories.list_children(parent_id)

    async def list_all(self, *, limit: int = 100, offset: int = 0) -> List:
        return await self.categories.list_all(limit=limit, offset=offset)

    async def get_category_page(self, category_id: int):
        cat = await self.categories.get_with_children(category_id)
        if cat is None:
            raise CategoryNotFoundError(f"Category {category_id} not found")
        videos = await self.categories.list_videos_in_category(
            category_id, include_subcategories=True, limit=20
        )
        return {"category": cat, "videos": videos}

    # ---------- write ----------

    async def create_category(
        self, current_user: Dict[str, Any], *,
        slug: str, name: str, description: Optional[str] = None,
        icon_key: Optional[str] = None, parent_id: Optional[int] = None, display_order: int = 0,
    ):
        ctx = self._ctx(current_user)
        self._assert_can(ctx, "admin.category.create")
        return await self.categories.create(
            slug=slug, name=name, description=description, icon_key=icon_key,
            parent_id=parent_id, display_order=display_order,
        )

    async def update_category(
        self, category_id: int, current_user: Dict[str, Any], *,
        name: Optional[str] = None, description: Optional[str] = None,
        icon_key: Optional[str] = None, display_order: Optional[int] = None,
    ):
        ctx = self._ctx(current_user)
        self._assert_can(ctx, "admin.category.update")
        return await self.categories.update(
            category_id, name=name, description=description,
            icon_key=icon_key, display_order=display_order,
        )

    async def set_parent(self, category_id: int, new_parent_id: Optional[int], current_user: Dict[str, Any]):
        ctx = self._ctx(current_user)
        self._assert_can(ctx, "admin.category.update")
        return await self.categories.set_parent(category_id, new_parent_id)

    async def delete_category(self, category_id: int, current_user: Dict[str, Any]) -> None:
        ctx = self._ctx(current_user)
        self._assert_can(ctx, "admin.category.delete")
        await self.categories.delete(category_id)