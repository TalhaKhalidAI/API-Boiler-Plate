# App/services/subscription_service.py

from typing import Any, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.SubscriptionRepository import SubscriptionRepository
from App.repository.ChannelRepository import ChannelRepository

from App.core.exceptions import PermissionDeniedError

class SubscriptionService(BaseService):
    """Any authenticated user can subscribe. Bumps channel counter in the same transaction."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.subscriptions = SubscriptionRepository(session)
        self.channels = ChannelRepository(session)

    @staticmethod
    def _uid(u: Dict[str, Any]) -> int:
        uid = u.get("id")
        if not uid:
            raise PermissionDeniedError("Authentication required")
        return uid

    async def subscribe(self, channel_id: int, current_user: Dict[str, Any], *, notify: bool = True):
        uid = self._uid(current_user)
        sub = await self.subscriptions.subscribe(uid, channel_id, notify=notify)
        await self.channels.increment_subscriber_count(channel_id, by=1)
        return sub

    async def unsubscribe(self, channel_id: int, current_user: Dict[str, Any]) -> None:
        uid = self._uid(current_user)
        await self.subscriptions.unsubscribe(uid, channel_id)
        await self.channels.increment_subscriber_count(channel_id, by=-1)

    async def toggle(self, channel_id: int, current_user: Dict[str, Any]) -> bool:
        uid = self._uid(current_user)
        state = await self.subscriptions.toggle(uid, channel_id)
        if state:
            await self.channels.increment_subscriber_count(channel_id, by=1)
        else:
            await self.channels.increment_subscriber_count(channel_id, by=-1)
        return state

    async def set_notify(self, channel_id: int, current_user: Dict[str, Any], notify: bool):
        uid = self._uid(current_user)
        return await self.subscriptions.set_notify(uid, channel_id, notify)

    async def list_my_subscriptions(self, current_user: Dict[str, Any], *, limit: int = 50, offset: int = 0) -> List:
        uid = self._uid(current_user)
        return await self.subscriptions.list_channels_for_subscriber(uid, limit=limit, offset=offset)

    async def list_subscribers(self, channel_id: int, *, limit: int = 50, offset: int = 0) -> List:
        return await self.subscriptions.list_subscribers_of_channel(channel_id, limit=limit, offset=offset)