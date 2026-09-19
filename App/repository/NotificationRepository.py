# App/repository/NotificationRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, or_, func
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    NotificationNotFoundError,
    InvalidNotificationTypeError,
    InfrastructureError,
    ValidationError
)
from App.api.databases.MigrateTable import (
    Notification,
)


class NotificationRepository:
    """
    Repository for the Notification aggregate.

    Owns: notifications.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Deletion policy:
        Notifications are hard-deleted. There is no `deleted_at`
        column on the model. "Dismissed" is represented by deleting
        the row; "read" is represented by `is_read = true`.

    Type policy:
        `type` is a CHECK-constrained varchar. Valid values live in
        `VALID_TYPES` below and MUST match the DB CHECK
        `ck_notifications_type`. Adding a new type requires:
          1. Adding it to `VALID_TYPES` here
          2. Adding it to the CHECK constraint in a migration

    Fan-out policy:
        Creating one notification per recipient is the caller's job.
        This repo exposes `create_many` for the service to fan-out in
        a single flush. It does NOT do fan-out internally — no
        "notify all subscribers of X" logic belongs in a repository.

    Payload policy:
        `payload` is a JSONB column for type-specific extras
        (e.g. {"preview": "...", "thumbnail": "..."}). This repo
        treats it as an opaque dict — it does not validate keys.
        Each notification type has its own schema; validation lives
        in the service layer.
    """

    # Must match DB CHECK `ck_notifications_type`.
    VALID_TYPES = {
        "new_video",
        "comment",
        "reply",
        "like",
        "subscribe",
        "mention",
        "system",
    }

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # =================================================================
    # INTERNAL HELPERS
    # =================================================================

    async def _safe_flush(self) -> None:
        """Flush pending ORM changes, translating DB errors to domain errors."""
        try:
            await self.session.flush()
        except IntegrityError as e:
            core_logger.warning(
                f"IntegrityError on flush: {e.orig if hasattr(e, 'orig') else e}"
            )
            raise
        except SQLAlchemyError as e:
            core_logger.error(f"SQLAlchemyError on flush: {e}")
            raise InfrastructureError("Database error during flush") from e

    async def _get_or_raise(self, notification_id: int) -> Notification:
        stmt = select(Notification).where(Notification.id == notification_id)
        result = await self.session.execute(stmt)
        notification = result.scalar_one_or_none()
        if notification is None:
            raise NotificationNotFoundError(
                f"Notification {notification_id} not found"
            )
        return notification

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _validate_type(notification_type: str) -> None:
        if notification_type not in NotificationRepository.VALID_TYPES:
            raise InvalidNotificationTypeError(
                f"Invalid notification type: {notification_type!r}. "
                f"Allowed: {sorted(NotificationRepository.VALID_TYPES)}"
            )

    # =================================================================
    # NOTIFICATION — READ
    # =================================================================

    async def get_by_id(self, notification_id: int) -> Optional[Notification]:
        stmt = select(Notification).where(Notification.id == notification_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_relations(self, notification_id: int) -> Optional[Notification]:
        """Eager-load actor, video, comment, channel for detail views."""
        stmt = (
            select(Notification)
            .where(Notification.id == notification_id)
            .options(
                joinedload(Notification.actor),
                joinedload(Notification.video),
                joinedload(Notification.comment),
                joinedload(Notification.channel),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_recipient(
        self,
        recipient_id: int,
        *,
        unread_only: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> List[Notification]:
        """
        Notifications for a recipient, newest first.
        The default page size is small (30) because this is a feed.
        """
        stmt = (
            select(Notification)
            .where(Notification.recipient_id == recipient_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_type_for_recipient(
        self,
        recipient_id: int,
        notification_type: str,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> List[Notification]:
        """Filter a user's notifications by type."""
        self._validate_type(notification_type)
        stmt = (
            select(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.type == notification_type,
            )
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_video(
        self,
        video_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Notification]:
        """All notifications pointing at a specific video (admin view)."""
        stmt = (
            select(Notification)
            .where(Notification.video_id == video_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_comment(
        self,
        comment_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Notification]:
        """All notifications pointing at a specific comment."""
        stmt = (
            select(Notification)
            .where(Notification.comment_id == comment_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # =================================================================
    # NOTIFICATION — COUNTS
    # =================================================================

    async def count_for_recipient(
        self, recipient_id: int, *, unread_only: bool = False
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(Notification.recipient_id == recipient_id)
        )
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        return (await self.session.execute(stmt)).scalar_one()

    async def count_unread(self, recipient_id: int) -> int:
        """Convenience wrapper — the most common count query."""
        return await self.count_for_recipient(recipient_id, unread_only=True)

    # =================================================================
    # NOTIFICATION — WRITE
    # =================================================================

    async def create(
        self,
        *,
        recipient_id: int,
        notification_type: str,
        actor_id: Optional[int] = None,
        video_id: Optional[int] = None,
        comment_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Notification:
        """
        Create a single notification.

        `notification_type` must be in VALID_TYPES.
        At least one of video_id / comment_id / channel_id should be
        set for context — the service decides which is required per
        type (e.g. `like` needs video_id, `comment` needs both
        video_id and comment_id).
        """
        self._validate_type(notification_type)

        notification = Notification(
            recipient_id=recipient_id,
            actor_id=actor_id,
            type=notification_type,
            video_id=video_id,
            comment_id=comment_id,
            channel_id=channel_id,
            payload=payload or {},
            is_read=False,
        )
        self.session.add(notification)
        await self._safe_flush()
        await self.session.refresh(notification)
        return notification

    async def create_many(
        self,
        notifications: List[Dict[str, Any]],
    ) -> List[Notification]:
        """
        Bulk-create notifications. Each item in the list is a dict
        with the same keys as `create`'s kwargs.

        Used for fan-out (e.g. "new video → notify all subscribers").
        Validates every item BEFORE inserting any, so a bad item at
        position N does not leave positions 0..N-1 committed.

        Returns the created rows in insertion order.
        """
        if not notifications:
            return []

        # Validate all first
        for idx, item in enumerate(notifications):
            notification_type = item.get("type")
            if notification_type not in self.VALID_TYPES:
                raise InvalidNotificationTypeError(
                    f"Invalid notification type at index {idx}: "
                    f"{notification_type!r}. "
                    f"Allowed: {sorted(self.VALID_TYPES)}"
                )

        # Build ORM objects
        rows: List[Notification] = []
        for item in notifications:
            rows.append(
                Notification(
                    recipient_id=item["recipient_id"],
                    actor_id=item.get("actor_id"),
                    type=item["type"],
                    video_id=item.get("video_id"),
                    comment_id=item.get("comment_id"),
                    channel_id=item.get("channel_id"),
                    payload=item.get("payload") or {},
                    is_read=False,
                )
            )

        self.session.add_all(rows)
        await self._safe_flush()
        for row in rows:
            await self.session.refresh(row)
        return rows

    async def mark_read(self, notification_id: int) -> Notification:
        """
        Mark a single notification as read. Idempotent — calling
        twice is a no-op the second time.
        """
        notification = await self._get_or_raise(notification_id)
        if notification.is_read:
            return notification
        notification.is_read = True
        await self._safe_flush()
        return notification

    async def mark_unread(self, notification_id: int) -> Notification:
        """Inverse of mark_read — useful for "mark as unread" UI."""
        notification = await self._get_or_raise(notification_id)
        if not notification.is_read:
            return notification
        notification.is_read = False
        await self._safe_flush()
        return notification

    async def mark_all_read(self, recipient_id: int) -> int:
        """
        Mark every unread notification for a recipient as read.
        Returns the number of rows updated.
        """
        result = await self.session.execute(
            update(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True)
            .returning(Notification.id)
        )
        return len(result.all())

    async def mark_many_read(self, notification_ids: List[int]) -> int:
        """
        Mark a specific set of notifications as read.
        Returns the number of rows actually updated.
        """
        if not notification_ids:
            return 0
        result = await self.session.execute(
            update(Notification)
            .where(
                Notification.id.in_(notification_ids),
                Notification.is_read.is_(False),
            )
            .values(is_read=True)
            .returning(Notification.id)
        )
        return len(result.all())

    async def delete(self, notification_id: int) -> None:
        result = await self.session.execute(
            delete(Notification)
            .where(Notification.id == notification_id)
            .returning(Notification.id)
        )
        if result.scalar_one_or_none() is None:
            raise NotificationNotFoundError(
                f"Notification {notification_id} not found"
            )

    async def delete_all_read_for_recipient(self, recipient_id: int) -> int:
        """
        Bulk cleanup — remove every READ notification for a recipient.
        Used for "clear read notifications" UI and retention jobs.
        Returns the number of rows deleted.
        """
        result = await self.session.execute(
            delete(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.is_read.is_(True),
            )
            .returning(Notification.id)
        )
        return len(result.all())

    async def delete_older_than(self, days: int) -> int:
        """
        Retention helper. Deletes notifications older than `days`.
        Returns the number of rows removed.
        """
        if days <= 0:
            raise ValidationError("days must be positive")
        from datetime import timedelta
        cutoff = self._now() - timedelta(days=days)
        result = await self.session.execute(
            delete(Notification)
            .where(Notification.created_at < cutoff)
            .returning(Notification.id)
        )
        return len(result.all())