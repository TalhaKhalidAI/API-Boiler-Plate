# App/services/view_counting_service.py

import json
from typing import Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.exceptions import PermissionDeniedError

from App.services.base import BaseService
from App.repository.ImpressionRepository import ImpressionRepository
from App.repository.VideoRepository import VideoRepository


class ViewCountingService(BaseService):
    """
    Worker that drains the impression queue and updates view counts.

    Permission keys:
      - worker.view_counting
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.impressions = ImpressionRepository(session)
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

    def _assert_worker(self, u: Dict[str, Any]) -> None:
        role = u.get("role") or u.get("user_role")
        perms = self._normalize_permissions(u.get("permissions"))
        if role == "admin" or perms.get("worker.view_counting"):
            return
        raise PermissionDeniedError("Only the view-counting worker or admin can run this")

    async def process_batch(self, current_user: Dict[str, Any], *, batch_size: int = 500) -> int:
        """
        Drain up to `batch_size` uncounted impressions. Groups by video,
        increments each video's view_count, and marks impressions counted.
        Returns the number of impressions processed.
        """
        self._assert_worker(current_user)

        impressions = await self.impressions.list_uncounted(limit=batch_size)
        if not impressions:
            return 0

        # Group impression IDs by video
        by_video: Dict[int, list] = {}
        for imp in impressions:
            by_video.setdefault(imp.video_id, []).append(imp.id)

        total = 0
        for video_id, impression_ids in by_video.items():
            await self.videos.increment_view_count(video_id, by=len(impression_ids))
            marked = await self.impressions.mark_counted_bulk(impression_ids)
            total += marked

        return total

    async def queue_depth(self, current_user: Dict[str, Any]) -> int:
        self._assert_worker(current_user)
        return await self.impressions.count_uncounted()