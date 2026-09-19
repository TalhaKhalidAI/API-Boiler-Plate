# App/repository/SubscriptionRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    SubscriptionNotFoundError,
    AlreadySubscribedError,
    NotSubscribedError,
    CannotSubscribeToSelfError,
    InfrastructureError,
)
from App.api.databases.MigrateTable import (
    Subscription,
    Channel,
    User,
)


class SubscriptionRepository:
    """
    Repository for the Subscription aggregate.

    Owns: subscriptions.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Semantics:
        A Subscription is a directed edge: `subscriber_id` (user)
        follows `channel_id` (channel). It is NOT symmetric. It
        is NOT a membership. It is NOT a grant.

    Counter policy:
        `channels.subscriber_count` is denormalized. The service
        that calls `subscribe` or `unsubscribe` must ALSO call
        `ChannelRepository.increment_subscriber_count(+1 / -1)`
        in the same transaction. This repo does NOT touch the
        channel's counter — that's a cross-aggregate mutation and
        belongs in the service.

    Self-subscribe policy:
        A user cannot subscribe to their own channel. The check
        happens in app code (`subscribe`) because it requires
        loading the channel's `owner_id`. There is no DB
        constraint for this — it would need a subquery, which
        Postgres CHECK constraints can't do.

    Notify policy:
        `notify` is a per-subscription boolean. Default is True.
        Toggling it is idempotent. Setting it does NOT affect the
        subscription's existence — the user is still subscribed,
        they just opted out of notifications.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # =================================================================
    # INTERNAL HELPERS
    # =================================================================

    async def _safe_flush(self) -> None:
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

    # =================================================================
    # SUBSCRIPTION — READ
    # =================================================================

    async def get(
        self, subscriber_id: int, channel_id: int
    ) -> Optional[Subscription]:
        """Fetch the subscription row, if it exists."""
        stmt = select(Subscription).where(
            Subscription.subscriber_id == subscriber_id,
            Subscription.channel_id == channel_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def is_subscribed(self, subscriber_id: int, channel_id: int) -> bool:
        """True if the user is subscribed to the channel."""
        stmt = (
            select(Subscription.channel_id)
            .where(
                Subscription.subscriber_id == subscriber_id,
                Subscription.channel_id == channel_id,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_with_channel(
        self, subscriber_id: int, channel_id: int
    ) -> Optional[Subscription]:
        """Eager-load the channel for the subscription detail view."""
        stmt = (
            select(Subscription)
            .where(
                Subscription.subscriber_id == subscriber_id,
                Subscription.channel_id == channel_id,
            )
            .options(joinedload(Subscription.channel))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_subscriber(
        self,
        subscriber_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Subscription]:
        """
        Channels this user is subscribed to, newest first.
        Returns junction rows — use `list_channels_for_subscriber`
        for the actual Channel objects.
        """
        stmt = (
            select(Subscription)
            .where(Subscription.subscriber_id == subscriber_id)
            .order_by(Subscription.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_channels_for_subscriber(
        self,
        subscriber_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Channel]:
        """
        Channels a user is subscribed to, newest subscription first.
        Used for the user's "Subscriptions" feed sidebar.
        """
        stmt = (
            select(Channel)
            .join(Subscription, Subscription.channel_id == Channel.id)
            .where(
                Subscription.subscriber_id == subscriber_id,
                Channel.deleted_at.is_(None),
            )
            .order_by(Subscription.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_subscribers_of_channel(
        self,
        channel_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Subscription]:
        """
        Users subscribed to a channel, newest first.
        For the channel owner's "subscribers" admin view.
        """
        stmt = (
            select(Subscription)
            .where(Subscription.channel_id == channel_id)
            .order_by(Subscription.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_users_subscribed_to_channel(
        self,
        channel_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> List[User]:
        """
        Actual User objects subscribed to a channel, newest first.
        For sending notifications to subscribers.
        """
        stmt = (
            select(User)
            .join(Subscription, Subscription.subscriber_id == User.id)
            .where(
                Subscription.channel_id == channel_id,
                User.is_deleted.is_(False),
            )
            .order_by(Subscription.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_subscriber_ids(
        self, channel_id: int
    ) -> List[int]:
        """
        Just the user IDs subscribed to a channel. For fan-out —
        notification workers use this to build recipient lists
        without loading full User objects.
        """
        stmt = (
            select(Subscription.subscriber_id)
            .where(Subscription.channel_id == channel_id)
        )
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def list_subscriber_ids_with_notify(
        self, channel_id: int
    ) -> List[int]:
        """
        Same as `list_subscriber_ids` but only users who have
        `notify=True` on their subscription. For notification fan-out
        where users can opt out per-channel.
        """
        stmt = (
            select(Subscription.subscriber_id)
            .where(
                Subscription.channel_id == channel_id,
                Subscription.notify.is_(True),
            )
        )
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def count_subscribers(self, channel_id: int) -> int:
        """How many users subscribe to this channel."""
        stmt = (
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.channel_id == channel_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_for_subscriber(self, subscriber_id: int) -> int:
        """How many channels this user subscribes to."""
        stmt = (
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.subscriber_id == subscriber_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # SUBSCRIPTION — WRITE
    # =================================================================

    async def subscribe(
        self,
        subscriber_id: int,
        channel_id: int,
        *,
        notify: bool = True,
    ) -> Subscription:
        """
        Subscribe a user to a channel.

        Validates:
          - The user is not the channel's owner (CannotSubscribeToSelfError)
          - The subscription doesn't already exist (AlreadySubscribedError)

        Caller must ALSO call
        ChannelRepository.increment_subscriber_count(+1) in the
        same transaction.
        """
        # Load the channel to check ownership
        channel_stmt = select(Channel).where(Channel.id == channel_id)
        channel = (
            await self.session.execute(channel_stmt)
        ).scalar_one_or_none()

        if channel is None:
            raise SubscriptionNotFoundError(
                f"Channel {channel_id} not found"
            )

        if channel.owner_id == subscriber_id:
            raise CannotSubscribeToSelfError(
                f"User {subscriber_id} cannot subscribe to their own channel"
            )

        if await self.is_subscribed(subscriber_id, channel_id):
            raise AlreadySubscribedError(
                f"User {subscriber_id} already subscribed to channel {channel_id}"
            )

        subscription = Subscription(
            subscriber_id=subscriber_id,
            channel_id=channel_id,
            notify=notify,
        )
        self.session.add(subscription)
        await self._safe_flush()
        await self.session.refresh(subscription)
        return subscription

    async def unsubscribe(
        self, subscriber_id: int, channel_id: int
    ) -> None:
        """
        Remove a subscription.

        Raises NotSubscribedError if the subscription doesn't exist.
        Caller must ALSO call
        ChannelRepository.increment_subscriber_count(-1) in the
        same transaction.
        """
        result = await self.session.execute(
            delete(Subscription)
            .where(
                Subscription.subscriber_id == subscriber_id,
                Subscription.channel_id == channel_id,
            )
            .returning(Subscription.subscriber_id)
        )
        if result.scalar_one_or_none() is None:
            raise NotSubscribedError(
                f"User {subscriber_id} not subscribed to channel {channel_id}"
            )

    async def toggle(
        self, subscriber_id: int, channel_id: int
    ) -> bool:
        """
        Flip the subscription state.

        Returns:
          - True if the user is NOW subscribed
          - False if the user is NOW unsubscribed

        Caller must update the channel's subscriber_count
        accordingly. Raises CannotSubscribeToSelfError if the user
        owns the channel and is not currently subscribed.
        """
        if await self.is_subscribed(subscriber_id, channel_id):
            await self.unsubscribe(subscriber_id, channel_id)
            return False
        else:
            await self.subscribe(subscriber_id, channel_id)
            return True

    async def set_notify(
        self, subscriber_id: int, channel_id: int, notify: bool
    ) -> Subscription:
        """
        Toggle the per-subscription notification flag. Idempotent.
        Does NOT change subscription state (still subscribed either
        way).

        Raises NotSubscribedError if the user isn't subscribed.
        """
        subscription = await self.get(subscriber_id, channel_id)
        if subscription is None:
            raise NotSubscribedError(
                f"User {subscriber_id} not subscribed to channel {channel_id}"
            )
        if subscription.notify == notify:
            return subscription
        subscription.notify = notify
        await self._safe_flush()
        return subscription

    async def subscribe_bulk(
        self,
        subscriber_id: int,
        channel_ids: List[int],
        *,
        notify: bool = True,
    ) -> int:
        """
        Subscribe a user to many channels at once.
        Idempotent-safe: existing subscriptions are skipped.
        Returns the number of NEW subscriptions created.

        Does NOT validate that the user isn't a channel owner.
        Caller must pre-validate or use `subscribe` per channel if
        that check matters.

        Caller must increment each channel's subscriber_count for
        the newly created subscriptions.
        """
        if not channel_ids:
            return 0

        stmt = (
            pg_insert(Subscription)
            .values(
                [
                    {
                        "subscriber_id": subscriber_id,
                        "channel_id": cid,
                        "notify": notify,
                    }
                    for cid in channel_ids
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["subscriber_id", "channel_id"]
            )
            .returning(Subscription.channel_id)
        )
        result = await self.session.execute(stmt)
        return len(result.all())

    async def unsubscribe_bulk(
        self, subscriber_id: int, channel_ids: List[int]
    ) -> int:
        """
        Remove many subscriptions at once. Idempotent-safe.
        Returns the number of rows actually removed.

        Caller must decrement each channel's subscriber_count.
        """
        if not channel_ids:
            return 0

        result = await self.session.execute(
            delete(Subscription)
            .where(
                Subscription.subscriber_id == subscriber_id,
                Subscription.channel_id.in_(channel_ids),
            )
            .returning(Subscription.channel_id)
        )
        return len(result.all())

    async def remove_all_subscribers_of_channel(
        self, channel_id: int
    ) -> int:
        """
        Remove every subscription pointing at a channel.
        Returns the number of rows deleted.

        Used when a channel is hard-deleted (soft-delete keeps the
        subscriptions). Caller must reconcile the channel's
        subscriber_count if the counter still matters.
        """
        result = await self.session.execute(
            delete(Subscription)
            .where(Subscription.channel_id == channel_id)
            .returning(Subscription.subscriber_id)
        )
        return len(result.all())

    async def remove_all_subscriptions_for_user(
        self, subscriber_id: int
    ) -> int:
        """
        Remove every channel this user subscribes to.
        Returns the number of rows deleted.

        Caller must decrement each affected channel's
        subscriber_count.
        """
        result = await self.session.execute(
            delete(Subscription)
            .where(Subscription.subscriber_id == subscriber_id)
            .returning(Subscription.channel_id)
        )
        return len(result.all())