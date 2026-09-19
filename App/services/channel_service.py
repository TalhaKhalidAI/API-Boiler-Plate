# App/services/channel_service.py

import json
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.ChannelRepository import ChannelRepository
from App.repository.UserRepository import UserRepository
from App.repository.VideoRepository import VideoRepository
from App.core.exceptions import (
    ChannelNotFoundError,
    UserNotFoundError,
    ChannelAlreadyExistsError,
    DomainError,
    PermissionDeniedError
)


class ChannelService(BaseService):
    """
    Business logic for channels.

    Authorization:
      - Anyone can read a channel.
      - Owner + `channel.update.self` / `channel.delete.self` / channel.self.* → mutate own channel.
      - Admin or `admin.channel.*` → mutate any channel.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.channels = ChannelRepository(session)
        self.users = UserRepository(session)
        self.videos = VideoRepository(session)

    # ---------- helpers ----------

    @staticmethod
    def _normalize_permissions(perms: Any) -> Dict[str, Any]:
        if not perms:
            return {}
        if isinstance(perms, str):
            try:
                return json.loads(perms)
            except (TypeError, ValueError):
                return {}
        return perms

    def _ctx(self, u: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": u.get("id"),
            "role": u.get("role") or u.get("user_role"),
            "permissions": self._normalize_permissions(u.get("permissions")),
        }

    def _assert_can(
        self,
        ctx: Dict[str, Any],
        channel_owner_id: int,
        *,
        admin_perm: str,
        self_perm: Optional[str] = None,
    ) -> None:
        if ctx["role"] == "admin" or ctx["permissions"].get(admin_perm, False):
            return
        if self_perm and ctx["id"] == channel_owner_id and ctx["permissions"].get(self_perm, False):
            return
        raise PermissionDeniedError("You don't have permission to do this")

    # ---------- read ----------

    async def get_channel(self, channel_id: int):
        channel = await self.channels.get_by_id(channel_id)
        if channel is None:
            raise ChannelNotFoundError(f"Channel {channel_id} not found")
        return channel

    async def get_channel_by_handle(self, handle: str):
        channel = await self.channels.get_by_handle(handle)
        if channel is None:
            raise ChannelNotFoundError(f"Channel with handle {handle!r} not found")
        return channel

    async def get_my_channel(self, current_user: Dict[str, Any]):
        ctx = self._ctx(current_user)
        if not ctx["id"]:
            raise PermissionDeniedError("Authentication required")
        channel = await self.channels.get_by_owner(ctx["id"])
        if channel is None:
            raise ChannelNotFoundError(f"User {ctx['id']} has no channel")
        return channel

    async def get_channel_page(self, channel_id: int):
        channel = await self.get_channel(channel_id)
        videos = await self.videos.list_by_channel(
            channel_id, visibility="public", status="ready", limit=20, offset=0
        )
        return {"channel": channel, "videos": videos}

    # ---------- write ----------

    async def create_channel(
        self,
        current_user: Dict[str, Any],
        *,
        handle: str,
        display_name: str,
        description: Optional[str] = None,
        banner_key: Optional[str] = None,
        avatar_key: Optional[str] = None,
    ):
        ctx = self._ctx(current_user)
        if not ctx["id"]:
            raise PermissionDeniedError("Authentication required")
        if not (
            ctx["role"] == "admin"
            or ctx["permissions"].get("channel.create.self", False)
            or ctx["permissions"].get("admin.channel.create", False)
        ):
            raise PermissionDeniedError("You don't have permission to create a channel")

        if await self.users.get_by_id(ctx["id"]) is None:
            raise UserNotFoundError(f"User {ctx['id']} not found")

        existing = await self.channels.get_by_owner(ctx["id"])
        if existing is not None:
            raise ChannelAlreadyExistsError("You already own a channel")

        return await self.channels.create(
            owner_id=ctx["id"],
            handle=handle,
            display_name=display_name,
            description=description,
            banner_key=banner_key,
            avatar_key=avatar_key,
        )

    async def update_profile(
        self,
        channel_id: int,
        current_user: Dict[str, Any],
        *,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        banner_key: Optional[str] = None,
        avatar_key: Optional[str] = None,
    ):
        ctx = self._ctx(current_user)
        channel = await self.get_channel(channel_id)
        self._assert_can(
            ctx, channel.owner_id,
            admin_perm="admin.channel.update",
            self_perm="channel.update.self",
        )
        return await self.channels.update_profile(
            channel_id,
            display_name=display_name,
            description=description,
            banner_key=banner_key,
            avatar_key=avatar_key,
        )

    async def update_handle(
        self, channel_id: int, current_user: Dict[str, Any], new_handle: str
    ):
        ctx = self._ctx(current_user)
        channel = await self.get_channel(channel_id)
        self._assert_can(
            ctx, channel.owner_id,
            admin_perm="admin.channel.update",
            self_perm="channel.update.self",
        )
        return await self.channels.update_handle(channel_id, new_handle)

    async def set_verified(
        self, channel_id: int, current_user: Dict[str, Any], verified: bool
    ):
        ctx = self._ctx(current_user)
        await self.get_channel(channel_id)
        if not (ctx["role"] == "admin" or ctx["permissions"].get("admin.channel.verify", False)):
            raise PermissionDeniedError("You don't have permission to verify channels")
        return await self.channels.set_verified(channel_id, verified)

    async def set_active(
        self, channel_id: int, current_user: Dict[str, Any], active: bool
    ):
        ctx = self._ctx(current_user)
        channel = await self.get_channel(channel_id)
        self._assert_can(
            ctx, channel.owner_id,
            admin_perm="admin.channel.enable",
            self_perm="channel.self.enable" if active else "channel.self.disable",
        )
        return await self.channels.set_active(channel_id, active)

    async def delete_channel(
        self, channel_id: int, current_user: Dict[str, Any]
    ) -> None:
        ctx = self._ctx(current_user)
        channel = await self.get_channel(channel_id)
        self._assert_can(
            ctx, channel.owner_id,
            admin_perm="admin.channel.delete",
            self_perm="channel.delete.self",
        )
        await self.channels.soft_delete(channel_id)