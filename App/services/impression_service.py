# App/services/impression_service.py

import json
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.exceptions import PermissionDeniedError
from App.services.base import BaseService
from App.repository.ImpressionRepository import ImpressionRepository


class ImpressionService(BaseService):
    """
    Record video impressions. Worker + admin only for queue inspection.

    Permission keys:
      - worker.impression
      - admin.moderate
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.impressions = ImpressionRepository(session)

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
        if role == "admin" or perms.get("worker.impression") or perms.get("admin.moderate"):
            return
        raise PermissionDeniedError("Only the impression worker or admin can access this")

    async def record_view(
        self, current_user: Dict[str, Any], *,
        video_id: int, user_id: Optional[int] = None, session_id: Optional[str] = None,
        watched_seconds: int = 0, debounce_seconds: int = 1800,
    ):
        self._assert_worker_or_admin(current_user)
        if user_id is not None:
            if await self.impressions.has_recent_impression_for_user(
                video_id=video_id, user_id=user_id, within_seconds=debounce_seconds,
            ):
                return None
        elif session_id is not None:
            if await self.impressions.has_recent_impression_for_session(
                video_id=video_id, session_id=session_id, within_seconds=debounce_seconds,
            ):
                return None
        return await self.impressions.create(
            video_id=video_id, user_id=user_id, session_id=session_id,
            watched_seconds=watched_seconds,
        )

    async def list_uncounted(self, current_user: Dict[str, Any], *, limit: int = 500):
        self._assert_worker_or_admin(current_user)
        return await self.impressions.list_uncounted(limit=limit)

    async def count_uncounted(self, current_user: Dict[str, Any]) -> int:
        self._assert_worker_or_admin(current_user)
        return await self.impressions.count_uncounted()