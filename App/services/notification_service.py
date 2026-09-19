# App/services/notification_service.py

import json
from typing import Any, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.NotificationRepository import NotificationRepository
from App.repository.SubscriptionRepository import SubscriptionRepository
from App.repository.UserRepository import UserRepository
from App.core.exceptions import NotificationNotFoundError, UserNotFoundError,PermissionDeniedError


class NotificationService(BaseService):
    """
    Self-only for reads. Admin-only for broadcast fan-out.

    Permission keys:
      - admin.notification.broadcast
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.notifications = NotificationRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        self.users = UserRepository(session)

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

    def _uid(self, u: Dict[str, Any]) -> int:
        uid = u.get("id")
        if not uid:
            raise PermissionDeniedError("Authentication required")
        return uid

    async def _assert_owns_notification(self, notification_id: int, current_user: Dict[str, Any]):
        uid = self._uid(current_user)
        n = await self.notifications.get_by_id(notification_id)
        if n is None:
            raise NotificationNotFoundError(f"Notification {notification_id} not found")
        role = current_user.get("role") or current_user.get("user_role")
        if n.recipient_id != uid and role != "admin":
            raise PermissionDeniedError("Not your notification")
        return n

    async def list_my_notifications(self, current_user: Dict[str, Any], *,
                                    unread_only: bool = False, limit: int = 30, offset: int = 0) -> List:
        uid = self._uid(current_user)
        return await self.notifications.list_for_recipient(
            uid, unread_only=unread_only, limit=limit, offset=offset
        )

    async def count_unread(self, current_user: Dict[str, Any]) -> int:
        uid = self._uid(current_user)
        return await self.notifications.count_unread(uid)

    async def mark_read(self, notification_id: int, current_user: Dict[str, Any]):
        await self._assert_owns_notification(notification_id, current_user)
        return await self.notifications.mark_read(notification_id)

    async def mark_all_read(self, current_user: Dict[str, Any]) -> int:
        uid = self._uid(current_user)
        return await self.notifications.mark_all_read(uid)

    async def delete_notification(self, notification_id: int, current_user: Dict[str, Any]) -> None:
        await self._assert_owns_notification(notification_id, current_user)
        await self.notifications.delete(notification_id)

    async def clear_read(self, current_user: Dict[str, Any]) -> int:
        uid = self._uid(current_user)
        return await self.notifications.delete_all_read_for_recipient(uid)

    async def broadcast_new_video(
        self, current_user: Dict[str, Any], *,
        video_id: int, channel_id: int, actor_id: int, payload: Dict[str, Any],
    ) -> int:
        """Admin-only fan-out: notify every subscriber of a channel."""
        role = current_user.get("role") or current_user.get("user_role")
        perms = self._normalize_permissions(current_user.get("permissions"))
        if not (role == "admin" or perms.get("admin.notification.broadcast", False)):
            raise PermissionDeniedError("You don't have permission to broadcast notifications")

        recipient_ids = await self.subscriptions.list_subscriber_ids_with_notify(channel_id)
        if not recipient_ids:
            return 0

        rows = [
            {
                "recipient_id": rid,
                "type": "new_video",
                "actor_id": actor_id,
                "video_id": video_id,
                "channel_id": channel_id,
                "payload": payload,
            }
            for rid in recipient_ids
        ]
        created = await self.notifications.create_many(rows)
        return len(created)