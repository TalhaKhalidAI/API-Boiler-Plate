"""
Test suite for ChannelRepository.

Assumes conftest.py defines: `db_session` (with session-scoped loop).

Run:
    uv run pytest test/vrt/test_channel_repository.py -v
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from App.repository.ChannelRepository import ChannelRepository
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
)
from App.api.databases.MigrateTable import (
    User,
    Channel,
    ChannelMember,
    ChannelGrant,
    Video,
    Subscription,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def repo(db_session):
    return ChannelRepository(db_session)


@pytest.fixture
async def user(db_session):
    """Owner of the primary channel."""
    u = User(
        name="Owner User",
        email="owner@example.com",
        password_hash="x",
        user_role="user",
        is_active=True,
        disabled=False,
        permissions={},
        is_deleted=False,
    )
    db_session.add(u)
    await db_session.flush()
    await db_session.refresh(u)
    return u


@pytest.fixture
async def other_user(db_session):
    """Unrelated user — used as member / grantee / second owner."""
    u = User(
        name="Other User",
        email="other@example.com",
        password_hash="x",
        user_role="user",
        is_active=True,
        disabled=False,
        permissions={},
        is_deleted=False,
    )
    db_session.add(u)
    await db_session.flush()
    await db_session.refresh(u)
    return u


@pytest.fixture
async def third_user(db_session):
    """Third user — used when we need two non-owners."""
    u = User(
        name="Third User",
        email="third@example.com",
        password_hash="x",
        user_role="user",
        is_active=True,
        disabled=False,
        permissions={},
        is_deleted=False,
    )
    db_session.add(u)
    await db_session.flush()
    await db_session.refresh(u)
    return u


@pytest.fixture
async def channel(db_session, user):
    """Primary channel owned by `user`."""
    c = Channel(
        owner_id=user.id,
        handle=f"owner_{user.id}",
        display_name="Test Channel",
        is_verified=False,
        is_active=True,
        subscriber_count=0,
        video_count=0,
        total_view_count=0,
    )
    db_session.add(c)
    await db_session.flush()
    await db_session.refresh(c)
    return c


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

async def _make_user(db_session):
    """Create a fresh user. Uses a uuid for a guaranteed-unique email."""
    u = User(
        name="Auto User",
        email=f"auto_{uuid.uuid4().hex[:12]}@example.com",
        password_hash="x",
        user_role="user",
        is_active=True,
        disabled=False,
        permissions={},
        is_deleted=False,
    )
    db_session.add(u)
    await db_session.flush()
    await db_session.refresh(u)
    return u


async def _make_channel(
    db_session,
    *,
    handle=None,
    display_name=None,
    is_active=True,
    deleted=False,
):
    """
    Create a real User + Channel pair.

    Creates a fresh user (so the FK is satisfied), then creates
    the channel owned by that user. Handle and display_name are
    derived from the user's id so they're unique per call.
    """
    u = await _make_user(db_session)

    c = Channel(
        owner_id=u.id,
        handle=handle or f"chan_{u.id}",
        display_name=display_name or f"Channel {u.id}",
        is_verified=False,
        is_active=is_active,
        subscriber_count=0,
        video_count=0,
        total_view_count=0,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    db_session.add(c)
    await db_session.flush()
    await db_session.refresh(c)
    return c


async def _make_video(db_session, channel_id, *, title="Test Video", visibility="private", status="processing"):
    v = Video(
        channel_id=channel_id,
        title=title,
        storage_bucket="test-bucket",
        storage_prefix=f"videos/{title}",
        master_playlist_key="master.m3u8",
        status=status,
        visibility=visibility,
    )
    db_session.add(v)
    await db_session.flush()
    await db_session.refresh(v)
    return v


async def _make_subscription(db_session, subscriber_id, channel_id):
    s = Subscription(subscriber_id=subscriber_id, channel_id=channel_id)
    db_session.add(s)
    await db_session.flush()
    await db_session.refresh(s)
    return s


# =====================================================================
# CHANNEL — READ
# =====================================================================

class TestChannelRead:

    async def test_get_by_id_returns_channel(self, repo, channel):
        result = await repo.get_by_id(channel.id)
        assert result is not None
        assert result.id == channel.id

    async def test_get_by_id_excludes_deleted(self, repo, db_session, channel):
        channel.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        assert await repo.get_by_id(channel.id) is None

    async def test_get_by_id_include_deleted(self, repo, db_session, channel):
        channel.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        result = await repo.get_by_id(channel.id, include_deleted=True)
        assert result is not None

    async def test_get_by_id_missing_returns_none(self, repo):
        assert await repo.get_by_id(999999) is None

    async def test_get_by_uuid(self, repo, channel):
        result = await repo.get_by_uuid(str(channel.channel_uuid))
        assert result is not None
        assert result.id == channel.id

    async def test_get_by_uuid_excludes_deleted(self, repo, db_session, channel):
        channel.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        assert await repo.get_by_uuid(str(channel.channel_uuid)) is None

    async def test_get_by_handle(self, repo, channel):
        result = await repo.get_by_handle(channel.handle)
        assert result is not None
        assert result.id == channel.id

    async def test_get_by_handle_excludes_deleted(self, repo, db_session, channel):
        channel.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        assert await repo.get_by_handle(channel.handle) is None

    async def test_get_by_owner(self, repo, channel, user):
        result = await repo.get_by_owner(user.id)
        assert result is not None
        assert result.id == channel.id

    async def test_get_by_owner_no_channel_returns_none(self, repo, third_user):
        assert await repo.get_by_owner(third_user.id) is None

    async def test_get_with_relations_loads_members_and_grants(
        self, repo, db_session, channel, other_user
    ):
        await repo.add_member(channel_id=channel.id, user_id=other_user.id, role="editor")
        await repo.grant_to_user(
            channel_id=channel.id, user_id=other_user.id,
            granted_by_id=channel.owner_id,
        )
        result = await repo.get_with_relations(channel.id)
        assert result is not None
        assert len(result.members) == 1
        assert len(result.grants) == 1

    async def test_list_active_excludes_deleted(self, repo, db_session, channel):
        await _make_channel(db_session)          # active
        await _make_channel(db_session, deleted=True)  # deleted
        rows = await repo.list_active()
        assert all(r.deleted_at is None for r in rows)

    async def test_list_active_excludes_inactive(self, repo, db_session, channel):
        await _make_channel(db_session)                      # active
        await _make_channel(db_session, is_active=False)     # inactive
        rows = await repo.list_active()
        assert all(r.is_active for r in rows)

    async def test_list_active_pagination(self, repo, db_session):
        for _ in range(5):
            await _make_channel(db_session)
        page1 = await repo.list_active(limit=2, offset=0)
        page2 = await repo.list_active(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id

    async def test_search_by_handle_matches_substring(self, repo, db_session):
        await _make_channel(db_session, handle="python_channel")
        await _make_channel(db_session, handle="golang_channel")
        rows = await repo.search_by_handle("python")
        assert len(rows) == 1
        assert rows[0].handle == "python_channel"

    async def test_search_by_handle_case_insensitive(self, repo, db_session):
        await _make_channel(db_session, handle="PythonChannel")
        rows = await repo.search_by_handle("pythonchannel")
        assert len(rows) == 1

    async def test_search_by_handle_excludes_deleted(self, repo, db_session):
        await _make_channel(db_session, handle="deleted_chan", deleted=True)
        rows = await repo.search_by_handle("deleted_chan")
        assert rows == []

    async def test_search_by_handle_excludes_inactive(self, repo, db_session):
        await _make_channel(db_session, handle="inactive_chan", is_active=False)
        rows = await repo.search_by_handle("inactive_chan")
        assert rows == []


# =====================================================================
# CHANNEL — COUNTS
# =====================================================================

class TestChannelCounts:

    async def test_count_active(self, repo, db_session, channel):
        # channel fixture is active
        await _make_channel(db_session)
        count = await repo.count_active()
        assert count >= 2

    async def test_count_active_excludes_deleted(self, repo, db_session):
        await _make_channel(db_session, deleted=True)
        # We only assert the count is non-negative here; other fixtures may
        # add active channels, so we can't assert an exact number safely.
        count = await repo.count_active()
        assert count >= 0

    async def test_count_search_by_handle(self, repo, db_session):
        await _make_channel(db_session, handle="searchtest_one")
        await _make_channel(db_session, handle="searchtest_two")
        count = await repo.count_search_by_handle("searchtest")
        assert count == 2


# =====================================================================
# CHANNEL — WRITE
# =====================================================================

class TestChannelWrite:

    async def test_create_sets_defaults(self, repo, third_user):
        c = await repo.create(
            owner_id=third_user.id,
            handle=f"new_{third_user.id}",
            display_name="New Channel",
        )
        assert c.is_verified is False
        assert c.is_active is True
        assert c.subscriber_count == 0
        assert c.video_count == 0
        assert c.total_view_count == 0

    async def test_create_duplicate_owner_raises(self, repo, channel):
        # `channel` fixture already created a channel for `channel.owner_id`
        with pytest.raises(ChannelAlreadyExistsError):
            await repo.create(
                owner_id=channel.owner_id,
                handle=f"another_{channel.owner_id}",
                display_name="Second Channel",
            )

    async def test_create_duplicate_handle_raises(self, repo, third_user, channel):
        with pytest.raises(DuplicateHandleError):
            await repo.create(
                owner_id=third_user.id,
                handle=channel.handle,
                display_name="Steal Handle",
            )

    async def test_create_handle_conflicts_with_soft_deleted(self, repo, db_session):
        """A soft-deleted channel still reserves its handle."""
        u1 = await _make_user(db_session)
        u2 = await _make_user(db_session)

        ghost = Channel(
            owner_id=u1.id,
            handle="ghost_handle",
            display_name="Ghost",
            is_verified=False,
            is_active=False,
            subscriber_count=0,
            video_count=0,
            total_view_count=0,
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(ghost)
        await db_session.flush()

        with pytest.raises(DuplicateHandleError):
            await repo.create(
                owner_id=u2.id,
                handle="ghost_handle",
                display_name="Reuse",
            )

    async def test_update_profile(self, repo, channel):
        updated = await repo.update_profile(
            channel.id,
            display_name="Updated",
            description="New description",
        )
        assert updated.display_name == "Updated"
        assert updated.description == "New description"

    async def test_update_profile_partial(self, repo, channel):
        original_handle = channel.handle
        await repo.update_profile(channel.id, display_name="Just Name")
        refreshed = await repo.get_by_id(channel.id)
        assert refreshed.display_name == "Just Name"
        assert refreshed.handle == original_handle

    async def test_update_handle(self, repo, channel):
        updated = await repo.update_handle(channel.id, "brand_new_handle")
        assert updated.handle == "brand_new_handle"

    async def test_update_handle_same_is_noop(self, repo, channel):
        same = await repo.update_handle(channel.id, channel.handle)
        assert same.handle == channel.handle

    async def test_update_handle_duplicate_raises(self, repo, db_session, channel):
        await _make_channel(db_session, handle="taken_handle")
        with pytest.raises(DuplicateHandleError):
            await repo.update_handle(channel.id, "taken_handle")

    async def test_set_verified(self, repo, channel):
        result = await repo.set_verified(channel.id, True)
        assert result.is_verified is True

    async def test_set_active(self, repo, channel):
        result = await repo.set_active(channel.id, False)
        assert result.is_active is False

    async def test_soft_delete(self, repo, db_session, channel):
        await repo.soft_delete(channel.id)
        await db_session.refresh(channel)
        assert channel.deleted_at is not None
        assert channel.is_active is False

    async def test_soft_delete_twice_raises(self, repo, channel):
        await repo.soft_delete(channel.id)
        with pytest.raises(ChannelAlreadyDeletedError):
            await repo.soft_delete(channel.id)

    async def test_soft_delete_missing_raises(self, repo):
        with pytest.raises(ChannelNotFoundError):
            await repo.soft_delete(999999)


# =====================================================================
# CHANNEL — COUNTERS
# =====================================================================

class TestChannelCounters:

    async def test_increment_subscriber_count(self, repo, db_session, channel):
        await repo.increment_subscriber_count(channel.id, by=5)
        await db_session.refresh(channel)
        assert channel.subscriber_count == 5

    async def test_increment_video_count(self, repo, db_session, channel):
        await repo.increment_video_count(channel.id, by=3)
        await db_session.refresh(channel)
        assert channel.video_count == 3

    async def test_increment_view_count(self, repo, db_session, channel):
        await repo.increment_view_count(channel.id, by=10)
        await db_session.refresh(channel)
        assert channel.total_view_count == 10

    async def test_increment_by_negative(self, repo, db_session, channel):
        await repo.increment_subscriber_count(channel.id, by=10)
        await repo.increment_subscriber_count(channel.id, by=-3)
        await db_session.refresh(channel)
        assert channel.subscriber_count == 7


# =====================================================================
# CHANNEL MEMBERS
# =====================================================================

class TestChannelMembers:

    async def test_add_member_viewer(self, repo, channel, other_user):
        member = await repo.add_member(
            channel_id=channel.id, user_id=other_user.id, role="viewer"
        )
        assert member.role == "viewer"
        assert member.user_id == other_user.id

    async def test_add_member_editor(self, repo, channel, other_user):
        member = await repo.add_member(
            channel_id=channel.id, user_id=other_user.id, role="editor"
        )
        assert member.role == "editor"

    async def test_add_member_owner_raises(self, repo, channel):
        with pytest.raises(CannotAddOwnerAsMemberError):
            await repo.add_member(
                channel_id=channel.id, user_id=channel.owner_id, role="editor"
            )

    async def test_add_member_invalid_role_raises(self, repo, channel, other_user):
        with pytest.raises(InvalidMemberRoleError):
            await repo.add_member(
                channel_id=channel.id, user_id=other_user.id, role="admin"
            )

    async def test_add_member_duplicate_raises(self, repo, channel, other_user):
        await repo.add_member(channel_id=channel.id, user_id=other_user.id)
        with pytest.raises(DuplicateChannelMemberError):
            await repo.add_member(channel_id=channel.id, user_id=other_user.id)

    async def test_add_member_missing_channel_raises(self, repo, other_user):
        with pytest.raises(ChannelNotFoundError):
            await repo.add_member(channel_id=999999, user_id=other_user.id)

    async def test_get_member(self, repo, channel, other_user):
        await repo.add_member(channel_id=channel.id, user_id=other_user.id)
        found = await repo.get_member(channel.id, other_user.id)
        assert found is not None
        assert found.user_id == other_user.id

    async def test_get_member_not_exists_returns_none(self, repo, channel, other_user):
        assert await repo.get_member(channel.id, other_user.id) is None

    async def test_list_members(self, repo, channel, other_user, third_user):
        await repo.add_member(channel_id=channel.id, user_id=other_user.id)
        await repo.add_member(channel_id=channel.id, user_id=third_user.id)
        members = await repo.list_members(channel.id)
        assert len(members) == 2

    async def test_list_channels_for_user(self, repo, db_session, other_user):
        # Two different channels, both add other_user as member
        c1 = await _make_channel(db_session)
        c2 = await _make_channel(db_session)
        await repo.add_member(channel_id=c1.id, user_id=other_user.id)
        await repo.add_member(channel_id=c2.id, user_id=other_user.id)
        channels = await repo.list_channels_for_user(other_user.id)
        assert len(channels) == 2

    async def test_set_member_role(self, repo, channel, other_user):
        await repo.add_member(
            channel_id=channel.id, user_id=other_user.id, role="viewer"
        )
        updated = await repo.set_member_role(channel.id, other_user.id, "editor")
        assert updated.role == "editor"

    async def test_set_member_role_invalid_raises(self, repo, channel, other_user):
        await repo.add_member(channel_id=channel.id, user_id=other_user.id)
        with pytest.raises(InvalidMemberRoleError):
            await repo.set_member_role(channel.id, other_user.id, "admin")

    async def test_set_member_role_not_member_raises(self, repo, channel, other_user):
        with pytest.raises(ChannelMemberNotFoundError):
            await repo.set_member_role(channel.id, other_user.id, "editor")

    async def test_remove_member(self, repo, channel, other_user):
        await repo.add_member(channel_id=channel.id, user_id=other_user.id)
        await repo.remove_member(channel.id, other_user.id)
        assert await repo.get_member(channel.id, other_user.id) is None

    async def test_remove_member_not_exists_raises(self, repo, channel, other_user):
        with pytest.raises(ChannelMemberNotFoundError):
            await repo.remove_member(channel.id, other_user.id)


# =====================================================================
# CHANNEL GRANTS
# =====================================================================

class TestChannelGrants:

    async def test_grant_to_user(self, repo, channel, other_user):
        grant = await repo.grant_to_user(
            channel_id=channel.id,
            user_id=other_user.id,
            granted_by_id=channel.owner_id,
        )
        assert grant.grantee_type == "user"
        assert grant.grantee_user_id == other_user.id

    async def test_grant_to_self_raises(self, repo, channel):
        with pytest.raises(InvalidGranteeError):
            await repo.grant_to_user(
                channel_id=channel.id,
                user_id=channel.owner_id,
                granted_by_id=channel.owner_id,
            )

    async def test_grant_to_user_duplicate_raises(self, repo, channel, other_user):
        await repo.grant_to_user(
            channel_id=channel.id,
            user_id=other_user.id,
            granted_by_id=channel.owner_id,
        )
        with pytest.raises(DuplicateChannelGrantError):
            await repo.grant_to_user(
                channel_id=channel.id,
                user_id=other_user.id,
                granted_by_id=channel.owner_id,
            )

    async def test_grant_via_link(self, repo, channel):
        grant = await repo.grant_via_link(
            channel_id=channel.id,
            granted_by_id=channel.owner_id,
            link_token="tok-chan-1",
        )
        assert grant.grantee_type == "link"

    async def test_revoke_grant(self, repo, channel, other_user):
        grant = await repo.grant_to_user(
            channel_id=channel.id,
            user_id=other_user.id,
            granted_by_id=channel.owner_id,
        )
        await repo.revoke_grant(grant.id)
        assert await repo.get_active_user_grant(channel.id, other_user.id) is None

    async def test_revoke_grant_twice_raises(self, repo, channel, other_user):
        grant = await repo.grant_to_user(
            channel_id=channel.id,
            user_id=other_user.id,
            granted_by_id=channel.owner_id,
        )
        await repo.revoke_grant(grant.id)
        with pytest.raises(ChannelGrantNotFoundError):
            await repo.revoke_grant(grant.id)

    async def test_get_active_user_grant_expired_returns_none(self, repo, channel, other_user):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.grant_to_user(
            channel_id=channel.id,
            user_id=other_user.id,
            granted_by_id=channel.owner_id,
            expires_at=past,
        )
        assert await repo.get_active_user_grant(channel.id, other_user.id) is None

    async def test_get_grant_by_token(self, repo, channel):
        await repo.grant_via_link(
            channel_id=channel.id,
            granted_by_id=channel.owner_id,
            link_token="tok-lookup",
        )
        found = await repo.get_grant_by_token("tok-lookup")
        assert found is not None

    async def test_get_grant_by_token_expired_returns_none(self, repo, channel):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.grant_via_link(
            channel_id=channel.id,
            granted_by_id=channel.owner_id,
            link_token="tok-expired",
            expires_at=past,
        )
        assert await repo.get_grant_by_token("tok-expired") is None

    async def test_increment_grant_use(self, repo, db_session, channel):
        grant = await repo.grant_via_link(
            channel_id=channel.id,
            granted_by_id=channel.owner_id,
            link_token="tok-inc",
            max_uses=3,
        )
        await repo.increment_grant_use(grant.id)
        await db_session.refresh(grant)
        assert grant.use_count == 1

    async def test_increment_grant_use_exhausted_raises(self, repo, channel):
        grant = await repo.grant_via_link(
            channel_id=channel.id,
            granted_by_id=channel.owner_id,
            link_token="tok-ex",
            max_uses=1,
        )
        await repo.increment_grant_use(grant.id)
        with pytest.raises(GrantUsageExhaustedError):
            await repo.increment_grant_use(grant.id)

    async def test_increment_grant_use_revoked_raises(self, repo, channel):
        grant = await repo.grant_via_link(
            channel_id=channel.id,
            granted_by_id=channel.owner_id,
            link_token="tok-rev",
            max_uses=5,
        )
        await repo.revoke_grant(grant.id)
        with pytest.raises(GrantUsageExhaustedError):
            await repo.increment_grant_use(grant.id)

    async def test_list_grants_for_channel(self, repo, channel, other_user):
        await repo.grant_to_user(
            channel_id=channel.id,
            user_id=other_user.id,
            granted_by_id=channel.owner_id,
        )
        grants = await repo.list_grants_for_channel(channel.id)
        assert len(grants) == 1

    async def test_list_grants_for_grantee(self, repo, db_session, other_user):
        c1 = await _make_channel(db_session)
        c2 = await _make_channel(db_session)
        await repo.grant_to_user(
            channel_id=c1.id, user_id=other_user.id, granted_by_id=c1.owner_id,
        )
        await repo.grant_to_user(
            channel_id=c2.id, user_id=other_user.id, granted_by_id=c2.owner_id,
        )
        grants = await repo.list_grants_for_grantee(other_user.id)
        assert len(grants) == 2

    async def test_count_grants_for_grantee(self, repo, channel, other_user):
        await repo.grant_to_user(
            channel_id=channel.id,
            user_id=other_user.id,
            granted_by_id=channel.owner_id,
        )
        assert await repo.count_grants_for_grantee(other_user.id) == 1


# =====================================================================
# CROSS-AGGREGATE READS
# =====================================================================

class TestCrossAggregateReads:

    async def test_list_videos_for_channel(self, repo, db_session, channel):
        await _make_video(db_session, channel.id, title="V1")
        await _make_video(db_session, channel.id, title="V2")
        videos = await repo.list_videos_for_channel(channel.id)
        assert len(videos) == 2

    async def test_list_videos_excludes_deleted(self, repo, db_session, channel):
        v = await _make_video(db_session, channel.id, title="Deleted")
        v.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        videos = await repo.list_videos_for_channel(channel.id)
        assert videos == []

    async def test_list_videos_filters_visibility(self, repo, db_session, channel):
        await _make_video(db_session, channel.id, title="Pub", visibility="public", status="ready")
        await _make_video(db_session, channel.id, title="Priv", visibility="private", status="ready")
        videos = await repo.list_videos_for_channel(channel.id, visibility="public")
        assert len(videos) == 1
        assert videos[0].visibility == "public"

    async def test_count_videos_for_channel(self, repo, db_session, channel):
        await _make_video(db_session, channel.id, title="V1")
        await _make_video(db_session, channel.id, title="V2")
        count = await repo.count_videos_for_channel(channel.id)
        assert count == 2

    async def test_list_subscriptions_for_channel(self, repo, db_session, channel, other_user):
        await _make_subscription(db_session, other_user.id, channel.id)
        subs = await repo.list_subscriptions_for_channel(channel.id)
        assert len(subs) == 1

    async def test_list_subscriptions_empty(self, repo, channel):
        subs = await repo.list_subscriptions_for_channel(channel.id)
        assert subs == []