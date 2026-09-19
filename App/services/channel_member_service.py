# App/services/channel_member_service.py

import json
from typing import Any, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.ChannelRepository import ChannelRepository
from App.core.exceptions import ChannelNotFoundError,PermissionDeniedError
class ChannelMemberService(BaseService):
    """
    Membership management. Owner controls members; admins bypass.

    Permission keys:
      - channel.members.manage.self
      - admin.channel.members.manage
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.channels = ChannelRepository(session)

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

    async def _assert_can_manage(self, channel_id: int, current_user: Dict[str, Any]):
        ctx = self._ctx(current_user)
        channel = await self.channels.get_by_id(channel_id)
        if channel is None:
            raise ChannelNotFoundError(f"Channel {channel_id} not found")
        if ctx["role"] == "admin" or ctx["permissions"].get("admin.channel.members.manage", False):
            return channel
        if ctx["id"] == channel.owner_id and ctx["permissions"].get("channel.members.manage.self", False):
            return channel
        raise PermissionDeniedError("You don't have permission to manage this channel's members")

    async def add_member(
        self,
        channel_id: int,
        current_user: Dict[str, Any],
        *,
        user_id: int,
        role: str = "viewer",
    ):
        await self._assert_can_manage(channel_id, current_user)
        return await self.channels.add_member(channel_id=channel_id, user_id=user_id, role=role)

    async def remove_member(self, channel_id: int, current_user: Dict[str, Any], user_id: int) -> None:
        await self._assert_can_manage(channel_id, current_user)
        await self.channels.remove_member(channel_id, user_id)

    async def set_role(self, channel_id: int, current_user: Dict[str, Any], user_id: int, role: str):
        await self._assert_can_manage(channel_id, current_user)
        return await self.channels.set_member_role(channel_id, user_id, role)

    async def list_members(self, channel_id: int, current_user: Dict[str, Any]) -> List:
        await self._assert_can_manage(channel_id, current_user)
        return await self.channels.list_members(channel_id)

    async def list_channels_for_user(self, user_id: int) -> List:
        return await self.channels.list_channels_for_user(user_id)