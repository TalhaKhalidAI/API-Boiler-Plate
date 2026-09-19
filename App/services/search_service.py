# App/services/search_service.py

from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.VideoRepository import VideoRepository
from App.repository.ChannelRepository import ChannelRepository
from App.repository.TagRepository import TagRepository


class SearchService(BaseService):
    """Public search across videos, channels, and tags. No auth."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.videos = VideoRepository(session)
        self.channels = ChannelRepository(session)
        self.tags = TagRepository(session)

    async def search(
        self, query: str, *,
        limit_videos: int = 20, limit_channels: int = 10, limit_tags: int = 10,
    ) -> Dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"videos": [], "channels": [], "tags": []}

        videos = await self.videos.search_by_title(query, limit=limit_videos)
        channels = await self.channels.search_by_handle(query, limit=limit_channels)
        tags = await self.tags.search_by_name(query, limit=limit_tags)

        return {"videos": videos, "channels": channels, "tags": tags}

    async def search_videos(self, query: str, *, limit: int = 20, offset: int = 0) -> List:
        return await self.videos.search_by_title(query, limit=limit, offset=offset)

    async def search_channels(self, query: str, *, limit: int = 20, offset: int = 0) -> List:
        return await self.channels.search_by_handle(query, limit=limit, offset=offset)

    async def search_tags(self, query: str, *, limit: int = 20, offset: int = 0) -> List:
        return await self.tags.search_by_name(query, limit=limit, offset=offset)