# App/services/video_service.py

import json
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.VideoRepository import VideoRepository
from App.repository.ChannelRepository import ChannelRepository
from App.repository.TagRepository import TagRepository
from App.repository.CategoryRepository import CategoryRepository
from App.core.exceptions import VideoNotFoundError, ChannelNotFoundError,PermissionDeniedError


class VideoService(BaseService):
    """
    Video CRUD and attachments.

    Permission keys:
      - video.create.self / video.update.self / video.delete.self / video.publish.self
      - admin.video.update / admin.video.delete / admin.video.publish
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.videos = VideoRepository(session)
        self.channels = ChannelRepository(session)
        self.tags = TagRepository(session)
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

    def _assert_can_modify(
        self, ctx: Dict[str, Any], channel_owner_id: int, *, admin_perm: str, self_perm: str
    ) -> None:
        if ctx["role"] == "admin" or ctx["permissions"].get(admin_perm, False):
            return
        if ctx["id"] == channel_owner_id and ctx["permissions"].get(self_perm, False):
            return
        raise PermissionDeniedError("You don't have permission to modify this video")

    async def _get_channel_for_video(self, video_id: int):
        video = await self.videos.get_by_id(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        channel = await self.channels.get_by_id(video.channel_id)
        if channel is None:
            raise ChannelNotFoundError(f"Channel {video.channel_id} not found")
        return video, channel

    # ---------- read ----------

    async def get_video(self, video_id: int):
        video = await self.videos.get_by_id(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        return video

    async def get_watch_page(self, video_id: int):
        video = await self.videos.get_with_relations(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        return video

    async def list_channel_videos(
        self,
        channel_id: int,
        *,
        visibility: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List:
        return await self.videos.list_by_channel(
            channel_id, visibility=visibility, status=status, limit=limit, offset=offset
        )

    # ---------- write ----------

    async def create_video(
        self,
        current_user: Dict[str, Any],
        *,
        channel_id: int,
        title: str,
        storage_bucket: str,
        storage_prefix: str,
        description: Optional[str] = None,
        thumbnail_key: Optional[str] = None,
        master_playlist_key: str = "master.m3u8",
    ):
        ctx = self._ctx(current_user)
        channel = await self.channels.get_by_id(channel_id)
        if channel is None:
            raise ChannelNotFoundError(f"Channel {channel_id} not found")
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.create.self",
        )
        video = await self.videos.create(
            channel_id=channel_id,
            title=title,
            description=description,
            thumbnail_key=thumbnail_key,
            storage_bucket=storage_bucket,
            storage_prefix=storage_prefix,
            master_playlist_key=master_playlist_key,
        )
        await self.channels.increment_video_count(channel_id, by=1)
        return video

    async def update_metadata(
        self,
        video_id: int,
        current_user: Dict[str, Any],
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_key: Optional[str] = None,
    ):
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        return await self.videos.update_metadata(
            video_id, title=title, description=description, thumbnail_key=thumbnail_key
        )

    async def set_status(self, video_id: int, current_user: Dict[str, Any], status: str):
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        return await self.videos.set_status(video_id, status)

    async def set_visibility(self, video_id: int, current_user: Dict[str, Any], visibility: str):
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.publish",
            self_perm="video.publish.self",
        )
        return await self.videos.set_visibility(video_id, visibility)

    async def delete_video(self, video_id: int, current_user: Dict[str, Any]) -> None:
        ctx = self._ctx(current_user)
        video, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.delete",
            self_perm="video.delete.self",
        )
        await self.videos.soft_delete(video_id)
        await self.channels.increment_video_count(video.channel_id, by=-1)

    async def set_tags(self, video_id: int, current_user: Dict[str, Any], tag_ids: List[int]) -> None:
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        added, removed = await self.tags.set_for_video(video_id, tag_ids)
        for tid in added:
            await self.tags.increment_usage_count(tid, by=1)
        for tid in removed:
            await self.tags.increment_usage_count(tid, by=-1)

    async def set_categories(self, video_id: int, current_user: Dict[str, Any], category_ids: List[int]) -> None:
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        await self.categories.set_for_video(video_id, category_ids)