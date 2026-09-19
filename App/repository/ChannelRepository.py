# App/repository/ChannelRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, or_, func
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List, Tuple
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    ChannelNotFoundError,
    ChannelAlreadyExistsError,
    ChannelAlreadyDeletedError,
    DuplicateHandleError,
    ChannelMemberNotFoundError,
    DuplicateChannelMemberError,
    CannotAddOwnerAsMemberError,
    InvalidMemberRoleError,
    ChannelGrantNotFoundError,
    DuplicateChannelGrantError,
    InvalidGranteeError,
    GrantUsageExhaustedError,
    InfrastructureError,
)
from App.api.databases.MigrateTable import (
    Channel,
    ChannelMember,
    ChannelGrant,
    Video,
    Subscription,
)


class ChannelRepository:
    """
    Repository for the Channel aggregate.

    Owns: channels, channel_members, channel_grants.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries. Callers must call `await session.commit()`
        (or use a unit-of-work) after all desired mutations.

    Storage policy:
        This repository NEVER touches MinIO or any external storage.
        Banner/avatar existence checks and presigning live in the service layer.

    Deletion policy:
        Soft-delete is tracked by `deleted_at IS NULL`. Hard-deletion is
        not exposed — channels are soft-deleted only. Queries filter on
        `deleted_at` consistently.

    Ownership policy:
        Owner lives in `channels.owner_id`. The owner must NEVER appear
        in `channel_members` — that table is for editors/viewers only.
    """

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

    async def _get_channel_or_raise(
        self, channel_id: int, *, include_deleted: bool = False
    ) -> Channel:
        stmt = select(Channel).where(Channel.id == channel_id)
        if not include_deleted:
            stmt = stmt.where(Channel.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        channel = result.scalar_one_or_none()
        if channel is None:
            raise ChannelNotFoundError(f"Channel {channel_id} not found")
        return channel

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # =================================================================
    # CHANNEL — READ
    # =================================================================

    async def get_by_id(
        self, channel_id: int, *, include_deleted: bool = False
    ) -> Optional[Channel]:
        stmt = select(Channel).where(Channel.id == channel_id)
        if not include_deleted:
            stmt = stmt.where(Channel.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_uuid(
        self, channel_uuid: str, *, include_deleted: bool = False
    ) -> Optional[Channel]:
        stmt = select(Channel).where(Channel.channel_uuid == channel_uuid)
        if not include_deleted:
            stmt = stmt.where(Channel.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_handle(
        self, handle: str, *, include_deleted: bool = False
    ) -> Optional[Channel]:
        stmt = select(Channel).where(Channel.handle == handle)
        if not include_deleted:
            stmt = stmt.where(Channel.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_owner(
        self, owner_id: int, *, include_deleted: bool = False
    ) -> Optional[Channel]:
        """One channel per user (owner_id is unique)."""
        stmt = select(Channel).where(Channel.owner_id == owner_id)
        if not include_deleted:
            stmt = stmt.where(Channel.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_relations(self, channel_id: int) -> Optional[Channel]:
        """Eager-load members and grants for the channel management page."""
        stmt = (
            select(Channel)
            .where(Channel.id == channel_id, Channel.deleted_at.is_(None))
            .options(
                selectinload(Channel.members).joinedload(ChannelMember.user),
                selectinload(Channel.grants),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(
        self, *, limit: int = 20, offset: int = 0
    ) -> List[Channel]:
        """List non-deleted, active channels. For admin / directory views."""
        stmt = (
            select(Channel)
            .where(Channel.deleted_at.is_(None), Channel.is_active.is_(True))
            .order_by(Channel.subscriber_count.desc().nullslast(), Channel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_by_handle(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Channel]:
        """
        Case-insensitive substring match on handle.
        Pair with a pg_trgm GIN index on channels.handle for large tables.
        """
        pattern = f"%{query}%"
        stmt = (
            select(Channel)
            .where(
                Channel.handle.ilike(pattern),
                Channel.deleted_at.is_(None),
                Channel.is_active.is_(True),
            )
            .order_by(Channel.subscriber_count.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_active(self) -> int:
        stmt = (
            select(func.count())
            .select_from(Channel)
            .where(Channel.deleted_at.is_(None), Channel.is_active.is_(True))
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_search_by_handle(self, query: str) -> int:
        pattern = f"%{query}%"
        stmt = (
            select(func.count())
            .select_from(Channel)
            .where(
                Channel.handle.ilike(pattern),
                Channel.deleted_at.is_(None),
                Channel.is_active.is_(True),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # CHANNEL — WRITE
    # =================================================================

    async def create(
        self,
        *,
        owner_id: int,
        handle: str,
        display_name: str,
        description: Optional[str] = None,
        banner_key: Optional[str] = None,
        avatar_key: Optional[str] = None,
    ) -> Channel:
        """
        Create a channel for a user. Enforces one-channel-per-user and
        unique handle at the application layer for clearer errors.
        """
        # One channel per user
        existing_owner = await self.get_by_owner(owner_id, include_deleted=True)
        if existing_owner is not None:
            raise ChannelAlreadyExistsError(
                f"User {owner_id} already owns a channel"
            )

        # Handle uniqueness — check including soft-deleted so we don't
        # reuse a handle someone else abandoned.
        existing_handle = await self.get_by_handle(handle, include_deleted=True)
        if existing_handle is not None:
            raise DuplicateHandleError(f"Handle '{handle}' is already taken")

        channel = Channel(
            owner_id=owner_id,
            handle=handle,
            display_name=display_name,
            description=description,
            banner_key=banner_key,
            avatar_key=avatar_key,
            is_verified=False,
            is_active=True,
            subscriber_count=0,
            video_count=0,
            total_view_count=0,
        )
        self.session.add(channel)
        await self._safe_flush()
        await self.session.refresh(channel)
        return channel

    async def update_profile(
        self,
        channel_id: int,
        *,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        banner_key: Optional[str] = None,
        avatar_key: Optional[str] = None,
    ) -> Channel:
        channel = await self._get_channel_or_raise(channel_id)
        if display_name is not None:
            channel.display_name = display_name
        if description is not None:
            channel.description = description
        if banner_key is not None:
            channel.banner_key = banner_key
        if avatar_key is not None:
            channel.avatar_key = avatar_key
        await self._safe_flush()
        return channel

    async def update_handle(self, channel_id: int, new_handle: str) -> Channel:
        channel = await self._get_channel_or_raise(channel_id)

        if channel.handle == new_handle:
            return channel

        # Check across ALL channels including soft-deleted
        existing = await self.get_by_handle(new_handle, include_deleted=True)
        if existing is not None and existing.id != channel_id:
            raise DuplicateHandleError(f"Handle '{new_handle}' is already taken")

        channel.handle = new_handle
        await self._safe_flush()
        return channel

    async def set_verified(self, channel_id: int, verified: bool) -> Channel:
        channel = await self._get_channel_or_raise(channel_id)
        channel.is_verified = verified
        await self._safe_flush()
        return channel

    async def set_active(self, channel_id: int, active: bool) -> Channel:
        channel = await self._get_channel_or_raise(channel_id)
        channel.is_active = active
        await self._safe_flush()
        return channel

    async def soft_delete(self, channel_id: int) -> None:
        channel = await self._get_channel_or_raise(channel_id, include_deleted=True)
        if channel.deleted_at is not None:
            raise ChannelAlreadyDeletedError(f"Channel {channel_id} already deleted")
        channel.deleted_at = self._now()
        channel.is_active = False
        await self._safe_flush()

    # =================================================================
    # CHANNEL — COUNTERS (atomic, worker-only)
    # =================================================================

    async def increment_subscriber_count(self, channel_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Channel)
            .where(Channel.id == channel_id)
            .values(subscriber_count=Channel.subscriber_count + by)
        )

    async def increment_video_count(self, channel_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Channel)
            .where(Channel.id == channel_id)
            .values(video_count=Channel.video_count + by)
        )

    async def increment_view_count(self, channel_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Channel)
            .where(Channel.id == channel_id)
            .values(total_view_count=Channel.total_view_count + by)
        )

    # =================================================================
    # CHANNEL MEMBERS — editors / viewers only
    # =================================================================

    async def add_member(
        self,
        *,
        channel_id: int,
        user_id: int,
        role: str = "viewer",
    ) -> ChannelMember:
        """
        Add an editor or viewer to a channel.
        The owner is implicitly a member — never add them here.
        """
        if role not in ("editor", "viewer"):
            raise InvalidMemberRoleError(f"Invalid member role: {role}")

        channel = await self._get_channel_or_raise(channel_id)

        if channel.owner_id == user_id:
            raise CannotAddOwnerAsMemberError(
                "Owner is implicit and cannot be added as a member"
            )

        existing = await self.get_member(channel_id, user_id)
        if existing is not None:
            raise DuplicateChannelMemberError(
                f"User {user_id} is already a member of channel {channel_id}"
            )

        member = ChannelMember(
            channel_id=channel_id,
            user_id=user_id,
            role=role,
        )
        self.session.add(member)
        await self._safe_flush()
        await self.session.refresh(member)
        return member

    async def get_member(
        self, channel_id: int, user_id: int
    ) -> Optional[ChannelMember]:
        stmt = select(ChannelMember).where(
            ChannelMember.channel_id == channel_id,
            ChannelMember.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_members(self, channel_id: int) -> List[ChannelMember]:
        stmt = (
            select(ChannelMember)
            .where(ChannelMember.channel_id == channel_id)
            .order_by(ChannelMember.added_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_channels_for_user(self, user_id: int) -> List[Channel]:
        """All channels where the user is a member (not owner)."""
        stmt = (
            select(Channel)
            .join(ChannelMember, ChannelMember.channel_id == Channel.id)
            .where(
                ChannelMember.user_id == user_id,
                Channel.deleted_at.is_(None),
            )
            .order_by(Channel.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def set_member_role(
        self, channel_id: int, user_id: int, role: str
    ) -> ChannelMember:
        if role not in ("editor", "viewer"):
            raise InvalidMemberRoleError(f"Invalid member role: {role}")

        member = await self.get_member(channel_id, user_id)
        if member is None:
            raise ChannelMemberNotFoundError(
                f"User {user_id} is not a member of channel {channel_id}"
            )
        member.role = role
        await self._safe_flush()
        return member

    async def remove_member(self, channel_id: int, user_id: int) -> None:
        result = await self.session.execute(
            delete(ChannelMember)
            .where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
            .returning(ChannelMember.user_id)
        )
        if result.scalar_one_or_none() is None:
            raise ChannelMemberNotFoundError(
                f"User {user_id} is not a member of channel {channel_id}"
            )

    # =================================================================
    # CHANNEL GRANTS — user grants + link grants
    # =================================================================

    async def grant_to_user(
        self,
        *,
        channel_id: int,
        user_id: int,
        granted_by_id: int,
        can_view: bool = True,
        can_download: bool = False,
        can_reshare: bool = False,
        expires_at: Optional[datetime] = None,
    ) -> ChannelGrant:
        if user_id == granted_by_id:
            raise InvalidGranteeError("Cannot grant to self")

        existing = await self.get_active_user_grant(channel_id, user_id)
        if existing is not None:
            raise DuplicateChannelGrantError(
                f"Grant already exists for user {user_id} on channel {channel_id}"
            )

        grant = ChannelGrant(
            channel_id=channel_id,
            grantee_type="user",
            grantee_user_id=user_id,
            granted_by_id=granted_by_id,
            can_view=can_view,
            can_download=can_download,
            can_reshare=can_reshare,
            expires_at=expires_at,
        )
        self.session.add(grant)
        await self._safe_flush()
        await self.session.refresh(grant)
        return grant

    async def grant_via_link(
        self,
        *,
        channel_id: int,
        granted_by_id: int,
        link_token: str,
        can_view: bool = True,
        can_download: bool = False,
        can_reshare: bool = False,
        password_hash: Optional[str] = None,
        max_uses: Optional[int] = None,
        expires_at: Optional[datetime] = None,
    ) -> ChannelGrant:
        grant = ChannelGrant(
            channel_id=channel_id,
            grantee_type="link",
            link_token=link_token,
            granted_by_id=granted_by_id,
            can_view=can_view,
            can_download=can_download,
            can_reshare=can_reshare,
            password_hash=password_hash,
            max_uses=max_uses,
            expires_at=expires_at,
        )
        self.session.add(grant)
        await self._safe_flush()
        await self.session.refresh(grant)
        return grant

    async def revoke_grant(self, grant_id: int) -> None:
        result = await self.session.execute(
            update(ChannelGrant)
            .where(ChannelGrant.id == grant_id, ChannelGrant.revoked_at.is_(None))
            .values(revoked_at=self._now())
            .returning(ChannelGrant.id)
        )
        if result.scalar_one_or_none() is None:
            raise ChannelGrantNotFoundError(
                f"Grant {grant_id} not found or already revoked"
            )

    async def get_active_user_grant(
        self, channel_id: int, user_id: int
    ) -> Optional[ChannelGrant]:
        now = self._now()
        stmt = select(ChannelGrant).where(
            ChannelGrant.channel_id == channel_id,
            ChannelGrant.grantee_type == "user",
            ChannelGrant.grantee_user_id == user_id,
            ChannelGrant.revoked_at.is_(None),
            or_(
                ChannelGrant.expires_at.is_(None),
                ChannelGrant.expires_at > now,
            ),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_grant_by_token(self, link_token: str) -> Optional[ChannelGrant]:
        now = self._now()
        stmt = select(ChannelGrant).where(
            ChannelGrant.grantee_type == "link",
            ChannelGrant.link_token == link_token,
            ChannelGrant.revoked_at.is_(None),
            or_(
                ChannelGrant.expires_at.is_(None),
                ChannelGrant.expires_at > now,
            ),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def increment_grant_use(self, grant_id: int) -> None:
        """
        Atomically increment use_count ONLY if max_uses is not yet reached.
        Raises GrantUsageExhaustedError if already at limit or revoked.
        """
        result = await self.session.execute(
            update(ChannelGrant)
            .where(
                ChannelGrant.id == grant_id,
                ChannelGrant.revoked_at.is_(None),
                or_(
                    ChannelGrant.max_uses.is_(None),
                    ChannelGrant.use_count < ChannelGrant.max_uses,
                ),
            )
            .values(use_count=ChannelGrant.use_count + 1)
            .returning(ChannelGrant.id)
        )
        if result.scalar_one_or_none() is None:
            raise GrantUsageExhaustedError(
                f"Grant {grant_id} is exhausted or revoked"
            )

    async def list_grants_for_channel(self, channel_id: int) -> List[ChannelGrant]:
        stmt = (
            select(ChannelGrant)
            .where(
                ChannelGrant.channel_id == channel_id,
                ChannelGrant.revoked_at.is_(None),
            )
            .order_by(ChannelGrant.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_grants_for_grantee(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[ChannelGrant]:
        """Active grants where `user_id` is the grantee. For 'Shared with me'."""
        now = self._now()
        stmt = (
            select(ChannelGrant)
            .where(
                ChannelGrant.grantee_type == "user",
                ChannelGrant.grantee_user_id == user_id,
                ChannelGrant.revoked_at.is_(None),
                or_(
                    ChannelGrant.expires_at.is_(None),
                    ChannelGrant.expires_at > now,
                ),
            )
            .order_by(ChannelGrant.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_grants_for_grantee(self, user_id: int) -> int:
        now = self._now()
        stmt = (
            select(func.count())
            .select_from(ChannelGrant)
            .where(
                ChannelGrant.grantee_type == "user",
                ChannelGrant.grantee_user_id == user_id,
                ChannelGrant.revoked_at.is_(None),
                or_(
                    ChannelGrant.expires_at.is_(None),
                    ChannelGrant.expires_at > now,
                ),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # CROSS-AGGREGATE READS (read-only lookups, no mutations)
    # =================================================================

    async def list_videos_for_channel(
        self,
        channel_id: int,
        *,
        visibility: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Video]:
        """
        Convenience read for the channel page.
        Mutations to videos belong in VideoRepository — this is read-only.
        """
        stmt = (
            select(Video)
            .where(Video.channel_id == channel_id, Video.deleted_at.is_(None))
            .order_by(
                Video.published_at.desc().nullslast(),
                Video.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        if visibility is not None:
            stmt = stmt.where(Video.visibility == visibility)
        if status is not None:
            stmt = stmt.where(Video.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_videos_for_channel(
        self,
        channel_id: int,
        *,
        visibility: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Video)
            .where(Video.channel_id == channel_id, Video.deleted_at.is_(None))
        )
        if visibility is not None:
            stmt = stmt.where(Video.visibility == visibility)
        if status is not None:
            stmt = stmt.where(Video.status == status)
        return (await self.session.execute(stmt)).scalar_one()

    async def list_subscriptions_for_channel(
        self,
        channel_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Subscription]:
        """
        Subscribers of this channel. Read-only.
        Mutations belong in SubscriptionRepository.
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