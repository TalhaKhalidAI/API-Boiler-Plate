# App/services/feed_service.py

from typing import Any, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.SubscriptionRepository import SubscriptionRepository
from App.repository.VideoRepository import VideoRepository
from App.repository.WatchHistoryRepository import WatchHistoryRepository
from App.core.exceptions import PermissionDeniedError

class FeedService(BaseService):
    """
    Personal home feed: videos from channels the user subscribes to,
    minus what they've already watched.

    Read-only. Any authenticated user.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.subscriptions = SubscriptionRepository(session)
        self.videos = VideoRepository(session)
        self.history = WatchHistoryRepository(session)

    @staticmethod
    def _uid(u: Dict[str, Any]) -> int:
        uid = u.get("id")
        if not uid:
            raise PermissionDeniedError("Authentication required")
        return uid

    async def get_home_feed(
        self, current_user: Dict[str, Any], *,
        per_channel: int = 5, exclude_watched: bool = True,
    ) -> List:
        uid = self._uid(current_user)

        channels = await self.subscriptions.list_channels_for_subscriber(uid, limit=50)
        if not channels:
            return []

        feed = []
        for channel in channels:
            videos = await self.videos.list_by_channel(
                channel.id,
                visibility="public",
                status="ready",
                limit=per_channel,
                offset=0,
            )
            feed.extend(videos)

        if exclude_watched:
            filtered = []
            for v in feed:
                if not await self.history.has_watched(uid, v.id):
                    filtered.append(v)
            feed = filtered

        feed.sort(
            key=lambda v: (v.published_at is None, v.published_at),
            reverse=True,
        )
        return feed