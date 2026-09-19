"""
Test suite for VideoRepository.

Assumptions:
  - pytest-asyncio installed; `asyncio_mode = auto` in pytest.ini (or use
    @pytest.mark.asyncio on each test).
  - `db_session` fixture yields an AsyncSession bound to a test DB.
  - `user_factory`, `channel_factory` fixtures create rows and return ORM objects.
  - Fixtures are per-test transactional (rolled back at the end).

If your fixture names differ, adjust the `@pytest.fixture` args at the top
of each test — the test bodies don't change.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from App.repository.VideoRepository import VideoRepository
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from App.repository.VideoRepository import VideoRepository

from App.core.exceptions import (
    VideoNotFoundError,
    VideoAlreadyDeletedError,
    VideoNotReadyError,
    DuplicateVideoGrantError,
    InvalidGranteeError,
    InvalidVideoStatusError,
    InvalidVideoVisibilityError,
    InvalidVideoReactionError,
    VideoVariantNotFoundError,
    VideoGrantNotFoundError,
    TagNotFoundError,
    CategoryNotFoundError,
    GrantUsageExhaustedError,
)
from App.api.databases.MigrateTable import (
    Video, VideoVariant, VideoGrant, VideoTag, VideoCategory,
    VideoReaction, Tag, Category,
)


# ---------------------------------------------------------------------
# Fixtures (adjust to match your conftest)
# ---------------------------------------------------------------------

@pytest.fixture
def repo(db_session):
    return VideoRepository(db_session)


@pytest.fixture
async def user(db_session):
    """Minimal user — no channel."""
    from App.api.databases.MigrateTable import User
    u = User(
        name="Test User",
        email="test@example.com",
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
    from App.api.databases.MigrateTable import User
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
async def channel(db_session, user):
    from App.api.databases.MigrateTable import Channel
    c = Channel(
        owner_id=user.id,
        handle=f"chan_{user.id}",
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


@pytest.fixture
async def other_channel(db_session, other_user):
    from App.api.databases.MigrateTable import Channel
    c = Channel(
        owner_id=other_user.id,
        handle=f"chan_{other_user.id}",
        display_name="Other Channel",
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


async def _make_video(
    db_session, channel, *, title="Test Video", status="processing",
    visibility="private", published_at=None,
):
    """Helper — creates a video row directly (bypasses repo for setup)."""
    v = Video(
        channel_id=channel.id,
        title=title,
        description=None,
        storage_bucket="test-bucket",
        storage_prefix=f"videos/{title}",
        master_playlist_key="master.m3u8",
        status=status,
        visibility=visibility,
        published_at=published_at,
    )
    db_session.add(v)
    await db_session.flush()
    await db_session.refresh(v)
    return v


async def _make_tag(db_session, slug="t1", name="Tag 1"):
    t = Tag(slug=slug, name=name, usage_count=0)
    db_session.add(t)
    await db_session.flush()
    await db_session.refresh(t)
    return t


async def _make_category(db_session, slug="c1", name="Cat 1"):
    c = Category(slug=slug, name=name, display_order=0, is_active=True)
    db_session.add(c)
    await db_session.flush()
    await db_session.refresh(c)
    return c


# =====================================================================
# VIDEO — READ
# =====================================================================

class TestVideoRead:

    async def test_get_by_id_returns_video(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        result = await repo.get_by_id(v.id)
        assert result is not None
        assert result.id == v.id

    async def test_get_by_id_excludes_deleted(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        v.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        result = await repo.get_by_id(v.id)
        assert result is None

    async def test_get_by_id_include_deleted(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        v.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        result = await repo.get_by_id(v.id, include_deleted=True)
        assert result is not None

    async def test_get_by_id_missing_returns_none(self, repo):
        assert await repo.get_by_id(999999) is None

    async def test_get_by_uuid(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        result = await repo.get_by_uuid(str(v.video_uuid))
        assert result is not None
        assert result.id == v.id

    async def test_get_by_uuid_excludes_deleted(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        v.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        assert await repo.get_by_uuid(str(v.video_uuid)) is None

    async def test_get_with_relations_loads_variants_tags_categories(
        self, repo, db_session, channel
    ):
        v = await _make_video(db_session, channel)
        await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264",
            playlist_key="p.m3u8", width=1280, height=720, status="ready",
        )
        t = await _make_tag(db_session)
        await repo.attach_tags(v.id, [t.id])
        c = await _make_category(db_session)
        await repo.attach_categories(v.id, [c.id])

        result = await repo.get_with_relations(v.id)
        assert result is not None
        assert len(result.variants) == 1
        assert len(result.tags) == 1
        assert len(result.categories) == 1

    async def test_get_with_grants(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        result = await repo.get_with_grants(v.id)
        assert result is not None
        assert len(result.grants) == 1

    async def test_list_by_channel_filters_visibility(
        self, repo, db_session, channel
    ):
        await _make_video(db_session, channel, visibility="public", status="ready")
        await _make_video(db_session, channel, visibility="private", status="ready")
        rows = await repo.list_by_channel(channel.id, visibility="public")
        assert len(rows) == 1
        assert rows[0].visibility == "public"

    async def test_list_by_channel_excludes_deleted(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        v.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        rows = await repo.list_by_channel(channel.id)
        assert rows == []

    async def test_list_by_channel_pagination(self, repo, db_session, channel):
        for i in range(5):
            await _make_video(db_session, channel, title=f"V{i}")
        page1 = await repo.list_by_channel(channel.id, limit=2, offset=0)
        page2 = await repo.list_by_channel(channel.id, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id

    async def test_list_public_feed_excludes_non_ready(self, repo, db_session, channel):
        await _make_video(db_session, channel, visibility="public", status="ready")
        await _make_video(db_session, channel, visibility="public", status="processing")
        rows = await repo.list_public_feed()
        assert len(rows) == 1
        assert rows[0].status == "ready"

    async def test_list_public_feed_excludes_private(self, repo, db_session, channel):
        await _make_video(db_session, channel, visibility="public", status="ready")
        await _make_video(db_session, channel, visibility="private", status="ready")
        rows = await repo.list_public_feed()
        assert len(rows) == 1

    async def test_list_by_tag(self, repo, db_session, channel):
        v = await _make_video(db_session, channel, visibility="public", status="ready")
        t = await _make_tag(db_session)
        await repo.attach_tags(v.id, [t.id])
        rows = await repo.list_by_tag(t.id)
        assert len(rows) == 1
        assert rows[0].id == v.id

    async def test_list_by_category(self, repo, db_session, channel):
        v = await _make_video(db_session, channel, visibility="public", status="ready")
        c = await _make_category(db_session)
        await repo.attach_categories(v.id, [c.id])
        rows = await repo.list_by_category(c.id)
        assert len(rows) == 1

    async def test_search_by_title_matches_substring(self, repo, db_session, channel):
        await _make_video(db_session, channel, title="Python Tutorial", visibility="public", status="ready")
        await _make_video(db_session, channel, title="Golang Basics", visibility="public", status="ready")
        rows = await repo.search_by_title("python")
        assert len(rows) == 1
        assert rows[0].title == "Python Tutorial"

    async def test_search_by_title_case_insensitive(self, repo, db_session, channel):
        await _make_video(db_session, channel, title="Python Tutorial", visibility="public", status="ready")
        rows = await repo.search_by_title("PYTHON")
        assert len(rows) == 1

    async def test_search_by_title_public_only(self, repo, db_session, channel):
        await _make_video(db_session, channel, title="Python Private", visibility="private", status="ready")
        rows = await repo.search_by_title("python", public_only=True)
        assert rows == []


# =====================================================================
# VIDEO — COUNTS
# =====================================================================

class TestVideoCounts:

    async def test_count_by_channel(self, repo, db_session, channel):
        await _make_video(db_session, channel)
        await _make_video(db_session, channel)
        assert await repo.count_by_channel(channel.id) == 2

    async def test_count_by_channel_excludes_deleted(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        v.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()
        assert await repo.count_by_channel(channel.id) == 0

    async def test_count_public_feed(self, repo, db_session, channel):
        await _make_video(db_session, channel, visibility="public", status="ready")
        await _make_video(db_session, channel, visibility="private", status="ready")
        assert await repo.count_public_feed() == 1

    async def test_count_by_tag(self, repo, db_session, channel):
        v = await _make_video(db_session, channel, visibility="public", status="ready")
        t = await _make_tag(db_session)
        await repo.attach_tags(v.id, [t.id])
        assert await repo.count_by_tag(t.id) == 1

    async def test_count_by_category(self, repo, db_session, channel):
        v = await _make_video(db_session, channel, visibility="public", status="ready")
        c = await _make_category(db_session)
        await repo.attach_categories(v.id, [c.id])
        assert await repo.count_by_category(c.id) == 1

    async def test_count_search_by_title(self, repo, db_session, channel):
        await _make_video(db_session, channel, title="Python 1", visibility="public", status="ready")
        await _make_video(db_session, channel, title="Python 2", visibility="public", status="ready")
        assert await repo.count_search_by_title("python") == 2


# =====================================================================
# VIDEO — WRITE
# =====================================================================

class TestVideoWrite:

    async def test_create_sets_defaults(self, repo, channel):
        v = await repo.create(
            channel_id=channel.id,
            title="New",
            storage_bucket="b",
            storage_prefix="p",
        )
        assert v.status == "processing"
        assert v.visibility == "private"
        assert v.published_at is None

    async def test_update_metadata(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        updated = await repo.update_metadata(v.id, title="New Title")
        assert updated.title == "New Title"

    async def test_update_metadata_partial(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        v.description = "old desc"
        await db_session.flush()
        updated = await repo.update_metadata(v.id, title="New Title")
        assert updated.title == "New Title"
        assert updated.description == "old desc"

    async def test_set_status_valid(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        result = await repo.set_status(v.id, "ready")
        assert result.status == "ready"

    async def test_set_status_invalid_raises(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        with pytest.raises(InvalidVideoStatusError):
            await repo.set_status(v.id, "bogus")

    async def test_set_visibility_to_public_requires_ready(
        self, repo, db_session, channel
    ):
        v = await _make_video(db_session, channel, status="processing")
        with pytest.raises(VideoNotReadyError):
            await repo.set_visibility(v.id, "public")

    async def test_set_visibility_sets_published_at(
        self, repo, db_session, channel
    ):
        v = await _make_video(db_session, channel, status="ready")
        result = await repo.set_visibility(v.id, "public")
        assert result.published_at is not None

    async def test_set_visibility_does_not_overwrite_published_at(
        self, repo, db_session, channel
    ):
        old = datetime(2024, 1, 1, tzinfo=timezone.utc)
        v = await _make_video(db_session, channel, status="ready", published_at=old)
        result = await repo.set_visibility(v.id, "public")
        assert result.published_at == old

    async def test_set_visibility_invalid_raises(self, repo, db_session, channel):
        v = await _make_video(db_session, channel, status="ready")
        with pytest.raises(InvalidVideoVisibilityError):
            await repo.set_visibility(v.id, "bogus")

    async def test_soft_delete(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.soft_delete(v.id)
        await db_session.refresh(v)
        assert v.deleted_at is not None
        assert v.status == "deleted"

    async def test_soft_delete_twice_raises(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.soft_delete(v.id)
        with pytest.raises(VideoAlreadyDeletedError):
            await repo.soft_delete(v.id)

    async def test_soft_delete_missing_raises(self, repo):
        with pytest.raises(VideoNotFoundError):
            await repo.soft_delete(999999)


# =====================================================================
# VIDEO — COUNTERS
# =====================================================================

class TestVideoCounters:

    async def test_increment_view_count(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.increment_view_count(v.id, by=5)
        await db_session.refresh(v)
        assert v.view_count == 5

    async def test_increment_like_count(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.increment_like_count(v.id, by=3)
        await db_session.refresh(v)
        assert v.like_count == 3

    async def test_increment_dislike_count(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.increment_dislike_count(v.id)
        await db_session.refresh(v)
        assert v.dislike_count == 1

    async def test_increment_comment_count(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.increment_comment_count(v.id)
        await db_session.refresh(v)
        assert v.comment_count == 1

    async def test_increment_save_count(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.increment_save_count(v.id)
        await db_session.refresh(v)
        assert v.save_count == 1

    async def test_increment_share_count(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.increment_share_count(v.id)
        await db_session.refresh(v)
        assert v.share_count == 1

    async def test_update_total_size(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.update_total_size(v.id, 1234567)
        await db_session.refresh(v)
        assert v.total_size_bytes == 1234567


# =====================================================================
# VIDEO VARIANTS
# =====================================================================

class TestVideoVariants:

    async def test_add_variant(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        variant = await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264",
            playlist_key="p.m3u8", width=1280, height=720,
        )
        assert variant.id is not None
        assert variant.status == "pending"

    async def test_get_variant(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264", playlist_key="p.m3u8"
        )
        found = await repo.get_variant(v.id, "720p", "h264")
        assert found is not None

    async def test_get_variant_by_id(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        variant = await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264", playlist_key="p.m3u8"
        )
        found = await repo.get_variant_by_id(variant.id)
        assert found is not None

    async def test_list_ready_variants_only_ready(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264",
            playlist_key="p.m3u8", status="ready",
        )
        await repo.add_variant(
            video_id=v.id, quality="480p", codec="h264",
            playlist_key="p.m3u8", status="pending",
        )
        ready = await repo.list_ready_variants(v.id)
        assert len(ready) == 1
        assert ready[0].quality == "720p"

    async def test_list_all_variants(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264",
            playlist_key="p.m3u8", status="ready",
        )
        await repo.add_variant(
            video_id=v.id, quality="480p", codec="h264",
            playlist_key="p.m3u8", status="failed",
        )
        all_v = await repo.list_all_variants(v.id)
        assert len(all_v) == 2

    async def test_set_variant_status(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        variant = await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264", playlist_key="p.m3u8"
        )
        await repo.set_variant_status(variant.id, "ready", size_bytes=1000)
        await db_session.refresh(variant)
        assert variant.status == "ready"
        assert variant.size_bytes == 1000

    async def test_set_variant_status_invalid(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        variant = await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264", playlist_key="p.m3u8"
        )
        with pytest.raises(InvalidVideoStatusError):
            await repo.set_variant_status(variant.id, "bogus")

    async def test_set_variant_status_missing(self, repo):
        with pytest.raises(VideoVariantNotFoundError):
            await repo.set_variant_status(999999, "ready")

    async def test_delete_variant(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        variant = await repo.add_variant(
            video_id=v.id, quality="720p", codec="h264", playlist_key="p.m3u8"
        )
        await repo.delete_variant(variant.id)
        assert await repo.get_variant_by_id(variant.id) is None

    async def test_delete_variant_missing(self, repo):
        with pytest.raises(VideoVariantNotFoundError):
            await repo.delete_variant(999999)


# =====================================================================
# VIDEO GRANTS
# =====================================================================

class TestVideoGrants:

    async def test_grant_to_user(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        grant = await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        assert grant.id is not None
        assert grant.grantee_type == "user"

    async def test_grant_to_self_raises(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        with pytest.raises(InvalidGranteeError):
            await repo.grant_to_user(
                video_id=v.id, user_id=channel.owner_id,
                granted_by_id=channel.owner_id,
            )

    async def test_grant_to_user_duplicate_raises(
        self, repo, db_session, channel, other_user
    ):
        v = await _make_video(db_session, channel)
        await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        with pytest.raises(DuplicateVideoGrantError):
            await repo.grant_to_user(
                video_id=v.id, user_id=other_user.id,
                granted_by_id=channel.owner_id,
            )

    async def test_grant_via_link(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        grant = await repo.grant_via_link(
            video_id=v.id, granted_by_id=channel.owner_id,
            link_token="tok-123",
        )
        assert grant.grantee_type == "link"

    async def test_revoke_grant(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        grant = await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        await repo.revoke_grant(grant.id)
        assert await repo.get_active_user_grant(v.id, other_user.id) is None

    async def test_revoke_grant_twice_raises(
        self, repo, db_session, channel, other_user
    ):
        v = await _make_video(db_session, channel)
        grant = await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        await repo.revoke_grant(grant.id)
        with pytest.raises(VideoGrantNotFoundError):
            await repo.revoke_grant(grant.id)

    async def test_get_active_user_grant_expired_returns_none(
        self, repo, db_session, channel, other_user
    ):
        v = await _make_video(db_session, channel)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id,
            granted_by_id=channel.owner_id, expires_at=past,
        )
        assert await repo.get_active_user_grant(v.id, other_user.id) is None

    async def test_get_grant_by_token(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.grant_via_link(
            video_id=v.id, granted_by_id=channel.owner_id, link_token="tok-x",
        )
        found = await repo.get_grant_by_token("tok-x")
        assert found is not None

    async def test_get_grant_by_token_expired_returns_none(
        self, repo, db_session, channel
    ):
        v = await _make_video(db_session, channel)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.grant_via_link(
            video_id=v.id, granted_by_id=channel.owner_id,
            link_token="tok-exp", expires_at=past,
        )
        assert await repo.get_grant_by_token("tok-exp") is None

    async def test_increment_grant_use(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        grant = await repo.grant_via_link(
            video_id=v.id, granted_by_id=channel.owner_id,
            link_token="tok-inc", max_uses=3,
        )
        await repo.increment_grant_use(grant.id)
        await db_session.refresh(grant)
        assert grant.use_count == 1

    async def test_increment_grant_use_exhausted_raises(
        self, repo, db_session, channel
    ):
        v = await _make_video(db_session, channel)
        grant = await repo.grant_via_link(
            video_id=v.id, granted_by_id=channel.owner_id,
            link_token="tok-ex", max_uses=1,
        )
        await repo.increment_grant_use(grant.id)
        with pytest.raises(GrantUsageExhaustedError):
            await repo.increment_grant_use(grant.id)

    async def test_increment_grant_use_revoked_raises(
        self, repo, db_session, channel
    ):
        v = await _make_video(db_session, channel)
        grant = await repo.grant_via_link(
            video_id=v.id, granted_by_id=channel.owner_id,
            link_token="tok-rev", max_uses=5,
        )
        await repo.revoke_grant(grant.id)
        with pytest.raises(GrantUsageExhaustedError):
            await repo.increment_grant_use(grant.id)

    async def test_list_grants_for_video(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        grants = await repo.list_grants_for_video(v.id)
        assert len(grants) == 1

    async def test_list_grants_for_grantee(self, repo, db_session, channel, other_user):
        v1 = await _make_video(db_session, channel, title="V1")
        v2 = await _make_video(db_session, channel, title="V2")
        await repo.grant_to_user(
            video_id=v1.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        await repo.grant_to_user(
            video_id=v2.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        grants = await repo.list_grants_for_grantee(other_user.id)
        assert len(grants) == 2

    async def test_count_grants_for_grantee(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        await repo.grant_to_user(
            video_id=v.id, user_id=other_user.id, granted_by_id=channel.owner_id,
        )
        assert await repo.count_grants_for_grantee(other_user.id) == 1


# =====================================================================
# VIDEO TAGS
# =====================================================================

class TestVideoTags:

    async def test_attach_tags(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        t = await _make_tag(db_session)
        await repo.attach_tags(v.id, [t.id])
        tags = await repo.list_video_tags(v.id)
        assert len(tags) == 1

    async def test_attach_tags_missing_raises(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        with pytest.raises(TagNotFoundError):
            await repo.attach_tags(v.id, [999999])

    async def test_attach_tags_empty_is_noop(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.attach_tags(v.id, [])
        assert await repo.list_video_tags(v.id) == []

    async def test_attach_tags_idempotent(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        t = await _make_tag(db_session)
        await repo.attach_tags(v.id, [t.id])
        await repo.attach_tags(v.id, [t.id])
        assert len(await repo.list_video_tags(v.id)) == 1

    async def test_detach_tags(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        t = await _make_tag(db_session)
        await repo.attach_tags(v.id, [t.id])
        await repo.detach_tags(v.id, [t.id])
        assert await repo.list_video_tags(v.id) == []

    async def test_set_tags_replaces(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        t1 = await _make_tag(db_session, slug="t1", name="T1")
        t2 = await _make_tag(db_session, slug="t2", name="T2")
        await repo.set_tags(v.id, [t1.id])
        await repo.set_tags(v.id, [t2.id])
        tags = await repo.list_video_tags(v.id)
        assert len(tags) == 1
        assert tags[0].id == t2.id

    async def test_set_tags_validates_first(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        t1 = await _make_tag(db_session, slug="t1")
        await repo.set_tags(v.id, [t1.id])
        with pytest.raises(TagNotFoundError):
            await repo.set_tags(v.id, [t1.id, 999999])
        # Original state preserved
        assert len(await repo.list_video_tags(v.id)) == 1


# =====================================================================
# VIDEO CATEGORIES
# =====================================================================

class TestVideoCategories:

    async def test_attach_categories(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        c = await _make_category(db_session)
        await repo.attach_categories(v.id, [c.id])
        cats = await repo.list_video_categories(v.id)
        assert len(cats) == 1

    async def test_attach_categories_missing_raises(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        with pytest.raises(CategoryNotFoundError):
            await repo.attach_categories(v.id, [999999])

    async def test_attach_categories_empty_noop(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        await repo.attach_categories(v.id, [])
        assert await repo.list_video_categories(v.id) == []

    async def test_set_categories_replaces(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        c1 = await _make_category(db_session, slug="c1", name="C1")
        c2 = await _make_category(db_session, slug="c2", name="C2")
        await repo.set_categories(v.id, [c1.id])
        await repo.set_categories(v.id, [c2.id])
        cats = await repo.list_video_categories(v.id)
        assert len(cats) == 1
        assert cats[0].id == c2.id

    async def test_set_categories_validates_first(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        c1 = await _make_category(db_session, slug="c1")
        await repo.set_categories(v.id, [c1.id])
        with pytest.raises(CategoryNotFoundError):
            await repo.set_categories(v.id, [c1.id, 999999])
        assert len(await repo.list_video_categories(v.id)) == 1


# =====================================================================
# VIDEO REACTIONS
# =====================================================================

class TestVideoReactions:

    async def test_upsert_reaction_created(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        action, previous = await repo.upsert_reaction(v.id, other_user.id, "like")
        assert action == "created"
        assert previous is None

    async def test_upsert_reaction_unchanged(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        await repo.upsert_reaction(v.id, other_user.id, "like")
        action, previous = await repo.upsert_reaction(v.id, other_user.id, "like")
        assert action == "unchanged"
        assert previous is None

    async def test_upsert_reaction_updated(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        await repo.upsert_reaction(v.id, other_user.id, "like")
        action, previous = await repo.upsert_reaction(v.id, other_user.id, "dislike")
        assert action == "updated"
        assert previous == "like"

    async def test_upsert_reaction_invalid(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        with pytest.raises(InvalidVideoReactionError):
            await repo.upsert_reaction(v.id, other_user.id, "bogus")

    async def test_delete_reaction(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        await repo.upsert_reaction(v.id, other_user.id, "like")
        previous = await repo.delete_reaction(v.id, other_user.id)
        assert previous == "like"

    async def test_delete_reaction_none_exists(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        assert await repo.delete_reaction(v.id, other_user.id) is None

    async def test_get_reaction(self, repo, db_session, channel, other_user):
        v = await _make_video(db_session, channel)
        await repo.upsert_reaction(v.id, other_user.id, "like")
        r = await repo.get_reaction(v.id, other_user.id)
        assert r is not None
        assert r.reaction == "like"


# =====================================================================
# MEDIA INFO (worker)
# =====================================================================

class TestMediaInfo:

    async def test_update_media_info(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        result = await repo.update_media_info(
            v.id, duration_seconds=120.5, total_size_bytes=999_999_999,
        )
        assert result.duration_seconds == 120.5
        assert result.total_size_bytes == 999_999_999

    async def test_update_media_info_partial(self, repo, db_session, channel):
        v = await _make_video(db_session, channel)
        v.duration_seconds = 100
        await db_session.flush()
        result = await repo.update_media_info(v.id, total_size_bytes=500)
        assert result.duration_seconds == 100
        assert result.total_size_bytes == 500