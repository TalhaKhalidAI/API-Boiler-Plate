"""
Test suite for CategoryRepository.

Assumes conftest.py defines: `db_session` (with session-scoped loop).

Run:
    uv run pytest test/vrt/test_category_repository.py -v
"""

import uuid
from datetime import datetime, timezone

import pytest

from App.repository.CategoryRepository import CategoryRepository
from App.core.exceptions import (
    CategoryNotFoundError,
    DuplicateCategoryError,
    InvalidCategoryParentError,
)
from App.api.databases.MigrateTable import (
    User,
    Channel,
    Category,
    Video,
    VideoCategory,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def repo(db_session):
    return CategoryRepository(db_session)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

async def _make_user(db_session):
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


async def _make_channel(db_session):
    """Create a real User + Channel pair."""
    u = await _make_user(db_session)
    c = Channel(
        owner_id=u.id,
        handle=f"chan_{u.id}",
        display_name=f"Channel {u.id}",
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


async def _make_video(db_session, channel_id, *, title="Test Video",
                      visibility="public", status="ready"):
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


async def _make_category(db_session, *, slug=None, name=None,
                          parent_id=None, is_active=True, display_order=0):
    c = Category(
        slug=slug or f"cat_{uuid.uuid4().hex[:8]}",
        name=name or "Test Category",
        parent_id=parent_id,
        display_order=display_order,
        is_active=is_active,
    )
    db_session.add(c)
    await db_session.flush()
    await db_session.refresh(c)
    return c


# =====================================================================
# CATEGORY — READ
# =====================================================================

class TestCategoryRead:

    async def test_get_by_id_returns_category(self, repo, db_session):
        c = await _make_category(db_session)
        found = await repo.get_by_id(c.id)
        assert found is not None
        assert found.id == c.id

    async def test_get_by_id_missing_returns_none(self, repo):
        assert await repo.get_by_id(999999) is None

    async def test_get_by_slug(self, repo, db_session):
        c = await _make_category(db_session, slug="my-slug")
        found = await repo.get_by_slug("my-slug")
        assert found is not None
        assert found.id == c.id

    async def test_get_by_slug_missing_returns_none(self, repo):
        assert await repo.get_by_slug("does-not-exist") is None

    async def test_get_with_children_loads_children(self, repo, db_session):
        parent = await _make_category(db_session)
        await _make_category(db_session, parent_id=parent.id)
        await _make_category(db_session, parent_id=parent.id)
        found = await repo.get_with_children(parent.id)
        assert found is not None
        assert len(found.children) == 2

    async def test_get_with_parent_loads_parent(self, repo, db_session):
        parent = await _make_category(db_session)
        child = await _make_category(db_session, parent_id=parent.id)
        found = await repo.get_with_parent(child.id)
        assert found is not None
        assert found.parent is not None
        assert found.parent.id == parent.id

    async def test_list_roots_returns_only_parentless(self, repo, db_session):
        root1 = await _make_category(db_session)
        root2 = await _make_category(db_session)
        await _make_category(db_session, parent_id=root1.id)
        rows = await repo.list_roots(active_only=False)
        ids = {r.id for r in rows}
        assert root1.id in ids
        assert root2.id in ids

    async def test_list_roots_active_only_filters(self, repo, db_session):
        await _make_category(db_session, is_active=True)
        await _make_category(db_session, is_active=False)
        rows = await repo.list_roots(active_only=True)
        assert all(r.is_active for r in rows)

    async def test_list_roots_ordered_by_display_order(self, repo, db_session):
        c3 = await _make_category(db_session, display_order=3)
        c1 = await _make_category(db_session, display_order=1)
        c2 = await _make_category(db_session, display_order=2)
        rows = await repo.list_roots()
        # Locate our 3 by display_order
        ordered = [r for r in rows if r.id in {c1.id, c2.id, c3.id}]
        assert [r.display_order for r in ordered] == [1, 2, 3]

    async def test_list_children(self, repo, db_session):
        parent = await _make_category(db_session)
        await _make_category(db_session, parent_id=parent.id)
        await _make_category(db_session, parent_id=parent.id)
        rows = await repo.list_children(parent.id, active_only=False)
        assert len(rows) == 2

    async def test_list_children_active_only(self, repo, db_session):
        parent = await _make_category(db_session)
        await _make_category(db_session, parent_id=parent.id, is_active=True)
        await _make_category(db_session, parent_id=parent.id, is_active=False)
        rows = await repo.list_children(parent.id, active_only=True)
        assert all(r.is_active for r in rows)

    async def test_list_children_empty(self, repo, db_session):
        parent = await _make_category(db_session)
        rows = await repo.list_children(parent.id)
        assert rows == []

    async def test_list_all(self, repo, db_session):
        await _make_category(db_session)
        await _make_category(db_session)
        rows = await repo.list_all(active_only=False, limit=100, offset=0)
        assert len(rows) >= 2

    async def test_list_all_pagination(self, repo, db_session):
        for _ in range(5):
            await _make_category(db_session)
        page1 = await repo.list_all(limit=2, offset=0, active_only=False)
        page2 = await repo.list_all(limit=2, offset=2, active_only=False)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id

    async def test_count_all(self, repo, db_session):
        before = await repo.count_all(active_only=False)
        await _make_category(db_session)
        await _make_category(db_session)
        after = await repo.count_all(active_only=False)
        assert after == before + 2

    async def test_count_all_active_only(self, repo, db_session):
        before = await repo.count_all(active_only=True)
        await _make_category(db_session, is_active=False)
        after = await repo.count_all(active_only=True)
        assert after == before  # inactive not counted


# =====================================================================
# CATEGORY — TREE HELPERS
# =====================================================================

class TestCategoryTreeHelpers:

    async def test_get_descendant_ids_direct_children(self, repo, db_session):
        root = await _make_category(db_session)
        c1 = await _make_category(db_session, parent_id=root.id)
        c2 = await _make_category(db_session, parent_id=root.id)
        descendants = await repo.get_descendant_ids(root.id)
        assert descendants == {c1.id, c2.id}

    async def test_get_descendant_ids_nested(self, repo, db_session):
        root = await _make_category(db_session)
        c1 = await _make_category(db_session, parent_id=root.id)
        g1 = await _make_category(db_session, parent_id=c1.id)
        g2 = await _make_category(db_session, parent_id=c1.id)
        descendants = await repo.get_descendant_ids(root.id)
        assert descendants == {c1.id, g1.id, g2.id}

    async def test_get_descendant_ids_empty(self, repo, db_session):
        root = await _make_category(db_session)
        assert await repo.get_descendant_ids(root.id) == set()

    async def test_get_descendant_ids_excludes_self(self, repo, db_session):
        root = await _make_category(db_session)
        await _make_category(db_session, parent_id=root.id)
        descendants = await repo.get_descendant_ids(root.id)
        assert root.id not in descendants

    async def test_get_ancestor_ids(self, repo, db_session):
        root = await _make_category(db_session)
        c1 = await _make_category(db_session, parent_id=root.id)
        g1 = await _make_category(db_session, parent_id=c1.id)
        ancestors = await repo.get_ancestor_ids(g1.id)
        assert ancestors == {c1.id, root.id}

    async def test_get_ancestor_ids_root_returns_empty(self, repo, db_session):
        root = await _make_category(db_session)
        assert await repo.get_ancestor_ids(root.id) == set()


# =====================================================================
# CATEGORY — WRITE
# =====================================================================

class TestCategoryWrite:

    async def test_create_root(self, repo):
        slug = f"root_{uuid.uuid4().hex[:8]}"
        c = await repo.create(slug=slug, name="New Root")
        assert c.id is not None
        assert c.parent_id is None
        assert c.slug == slug
        assert c.is_active is True
        assert c.display_order == 0

    async def test_create_with_parent(self, repo, db_session):
        parent = await _make_category(db_session)
        slug = f"child_{uuid.uuid4().hex[:8]}"
        child = await repo.create(
            slug=slug, name="Child", parent_id=parent.id
        )
        assert child.parent_id == parent.id

    async def test_create_duplicate_slug_raises(self, repo, db_session):
        existing = await _make_category(db_session, slug="taken-slug")
        with pytest.raises(DuplicateCategoryError):
            await repo.create(slug="taken-slug", name="Dup")

    async def test_create_missing_parent_raises(self, repo):
        with pytest.raises(InvalidCategoryParentError):
            await repo.create(
                slug=f"orphan_{uuid.uuid4().hex[:8]}",
                name="Orphan",
                parent_id=999999,
            )

    async def test_create_exceeding_max_depth_raises(self, repo, db_session):
        # Build a chain of depth == MAX_DEPTH (3)
        root = await _make_category(db_session)
        c1 = await _make_category(db_session, parent_id=root.id)
        c2 = await _make_category(db_session, parent_id=c1.id)
        # c2 depth = 2. Adding a child at depth 3 is the last allowed.
        # Adding a grandchild at depth 4 must fail.
        c3 = await repo.create(
            slug=f"d3_{uuid.uuid4().hex[:8]}", name="D3", parent_id=c2.id
        )
        # c3 depth = 3 = MAX_DEPTH. Adding another must fail:
        with pytest.raises(InvalidCategoryParentError):
            await repo.create(
                slug=f"d4_{uuid.uuid4().hex[:8]}",
                name="D4",
                parent_id=c3.id,
            )

    async def test_update_fields(self, repo, db_session):
        c = await _make_category(db_session)
        updated = await repo.update(
            c.id,
            name="Renamed",
            description="desc",
            icon_key="icon.png",
            display_order=5,
        )
        assert updated.name == "Renamed"
        assert updated.description == "desc"
        assert updated.icon_key == "icon.png"
        assert updated.display_order == 5

    async def test_update_partial(self, repo, db_session):
        c = await _make_category(db_session, name="Original")
        await repo.update(c.id, display_order=7)
        refreshed = await repo.get_by_id(c.id)
        assert refreshed.name == "Original"
        assert refreshed.display_order == 7

    async def test_update_missing_raises(self, repo):
        with pytest.raises(CategoryNotFoundError):
            await repo.update(999999, name="X")

    async def test_update_slug(self, repo, db_session):
        c = await _make_category(db_session, slug="old-slug")
        updated = await repo.update_slug(c.id, "new-slug")
        assert updated.slug == "new-slug"

    async def test_update_slug_same_is_noop(self, repo, db_session):
        c = await _make_category(db_session, slug="same-slug")
        same = await repo.update_slug(c.id, "same-slug")
        assert same.slug == "same-slug"

    async def test_update_slug_duplicate_raises(self, repo, db_session):
        await _make_category(db_session, slug="taken")
        c = await _make_category(db_session, slug="mine")
        with pytest.raises(DuplicateCategoryError):
            await repo.update_slug(c.id, "taken")

    async def test_set_parent_to_root(self, repo, db_session):
        parent = await _make_category(db_session)
        child = await _make_category(db_session, parent_id=parent.id)
        updated = await repo.set_parent(child.id, None)
        assert updated.parent_id is None

    async def test_set_parent_to_new_parent(self, repo, db_session):
        p1 = await _make_category(db_session)
        p2 = await _make_category(db_session)
        child = await _make_category(db_session, parent_id=p1.id)
        updated = await repo.set_parent(child.id, p2.id)
        assert updated.parent_id == p2.id

    async def test_set_parent_to_self_raises(self, repo, db_session):
        c = await _make_category(db_session)
        with pytest.raises(InvalidCategoryParentError):
            await repo.set_parent(c.id, c.id)

    async def test_set_parent_to_missing_raises(self, repo, db_session):
        c = await _make_category(db_session)
        with pytest.raises(InvalidCategoryParentError):
            await repo.set_parent(c.id, 999999)

    async def test_set_parent_to_descendant_raises(self, repo, db_session):
        # A -> B -> C. Trying to make A's parent = C must fail (cycle).
        a = await _make_category(db_session)
        b = await _make_category(db_session, parent_id=a.id)
        c = await _make_category(db_session, parent_id=b.id)
        with pytest.raises(InvalidCategoryParentError):
            await repo.set_parent(a.id, c.id)

    async def test_set_parent_exceeding_max_depth_raises(self, repo, db_session):
        # Two chains: A -> B -> C (depth 2 under A) and D (isolated).
        # Moving A under D would give A depth = 1 + descendants depth.
        # Simpler: build a chain of depth MAX_DEPTH, then try to nest it
        # under another category.
        r1 = await _make_category(db_session)
        c1 = await _make_category(db_session, parent_id=r1.id)
        c2 = await _make_category(db_session, parent_id=c1.id)   # depth 2
        c3 = await _make_category(db_session, parent_id=c2.id)   # depth 3
        # Now try to make c3 have a child -> should have failed at create.
        # But if we set_parent(r1 -> c3's subtree), r1's depth would balloon.
        # Instead: try to reparent r1 under c3. r1 depth 0 -> would become 4.
        with pytest.raises(InvalidCategoryParentError):
            await repo.set_parent(r1.id, c3.id)

    async def test_set_active(self, repo, db_session):
        c = await _make_category(db_session)
        updated = await repo.set_active(c.id, False)
        assert updated.is_active is False

    async def test_delete_root(self, repo, db_session):
        c = await _make_category(db_session)
        await repo.delete(c.id)
        assert await repo.get_by_id(c.id) is None

    async def test_delete_missing_raises(self, repo):
        with pytest.raises(CategoryNotFoundError):
            await repo.delete(999999)

    async def test_delete_parent_promotes_children_to_root(self, repo, db_session):
        parent = await _make_category(db_session)
        child = await _make_category(db_session, parent_id=parent.id)
        await repo.delete(parent.id)
        await db_session.refresh(child)
        assert child.parent_id is None

    async def test_delete_cascades_video_categories(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        category = await _make_category(db_session)
        await repo.attach_to_video(video.id, [category.id])

        await repo.delete(category.id)

        # The junction row must be gone
        from sqlalchemy import select
        result = await db_session.execute(
            select(VideoCategory).where(VideoCategory.category_id == category.id)
        )
        assert result.scalar_one_or_none() is None


# =====================================================================
# VIDEO ↔ CATEGORY ATTACHMENTS
# =====================================================================

class TestVideoCategoryAttachments:

    async def test_attach_to_video(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c1 = await _make_category(db_session)
        c2 = await _make_category(db_session)

        await repo.attach_to_video(video.id, [c1.id, c2.id])

        cats = await repo.list_categories_for_video(video.id)
        assert {c.id for c in cats} == {c1.id, c2.id}

    async def test_attach_empty_list_is_noop(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        await repo.attach_to_video(video.id, [])
        assert await repo.list_categories_for_video(video.id) == []

    async def test_attach_missing_category_raises(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        with pytest.raises(CategoryNotFoundError):
            await repo.attach_to_video(video.id, [999999])

    async def test_attach_is_idempotent(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c = await _make_category(db_session)
        await repo.attach_to_video(video.id, [c.id])
        await repo.attach_to_video(video.id, [c.id])  # second call — no-op
        cats = await repo.list_categories_for_video(video.id)
        assert len(cats) == 1

    async def test_detach_from_video(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c1 = await _make_category(db_session)
        c2 = await _make_category(db_session)
        await repo.attach_to_video(video.id, [c1.id, c2.id])

        await repo.detach_from_video(video.id, [c1.id])

        cats = await repo.list_categories_for_video(video.id)
        assert {c.id for c in cats} == {c2.id}

    async def test_detach_empty_list_is_noop(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c = await _make_category(db_session)
        await repo.attach_to_video(video.id, [c.id])
        await repo.detach_from_video(video.id, [])
        cats = await repo.list_categories_for_video(video.id)
        assert len(cats) == 1

    async def test_set_for_video_replaces(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c1 = await _make_category(db_session)
        c2 = await _make_category(db_session)

        await repo.set_for_video(video.id, [c1.id])
        await repo.set_for_video(video.id, [c2.id])

        cats = await repo.list_categories_for_video(video.id)
        assert {c.id for c in cats} == {c2.id}

    async def test_set_for_video_empty_clears(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c = await _make_category(db_session)
        await repo.set_for_video(video.id, [c.id])
        await repo.set_for_video(video.id, [])
        assert await repo.list_categories_for_video(video.id) == []

    async def test_set_for_video_validates_first(self, repo, db_session):
        """Bad input must not delete the existing attachment state."""
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c = await _make_category(db_session)
        await repo.set_for_video(video.id, [c.id])

        with pytest.raises(CategoryNotFoundError):
            await repo.set_for_video(video.id, [c.id, 999999])

        # Original state preserved
        cats = await repo.list_categories_for_video(video.id)
        assert len(cats) == 1


# =====================================================================
# CROSS-AGGREGATE READS
# =====================================================================

class TestCrossAggregateReads:

    async def test_list_videos_in_category(self, repo, db_session):
        channel = await _make_channel(db_session)
        v1 = await _make_video(db_session, channel.id, title="V1")
        v2 = await _make_video(db_session, channel.id, title="V2")
        c = await _make_category(db_session)
        await repo.attach_to_video(v1.id, [c.id])
        await repo.attach_to_video(v2.id, [c.id])

        videos = await repo.list_videos_in_category(c.id)
        assert len(videos) == 2

    async def test_list_videos_in_category_excludes_deleted(self, repo, db_session):
        channel = await _make_channel(db_session)
        v = await _make_video(db_session, channel.id)
        c = await _make_category(db_session)
        await repo.attach_to_video(v.id, [c.id])
        v.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()

        videos = await repo.list_videos_in_category(c.id)
        assert videos == []

    async def test_list_videos_in_category_only_public(self, repo, db_session):
        channel = await _make_channel(db_session)
        v_pub = await _make_video(db_session, channel.id, title="Pub",
                                   visibility="public", status="ready")
        v_priv = await _make_video(db_session, channel.id, title="Priv",
                                    visibility="private", status="ready")
        c = await _make_category(db_session)
        await repo.attach_to_video(v_pub.id, [c.id])
        await repo.attach_to_video(v_priv.id, [c.id])

        videos = await repo.list_videos_in_category(c.id, only_public=True)
        assert len(videos) == 1
        assert videos[0].visibility == "public"

    async def test_list_videos_include_subcategories(self, repo, db_session):
        channel = await _make_channel(db_session)
        v1 = await _make_video(db_session, channel.id, title="V1")
        v2 = await _make_video(db_session, channel.id, title="V2")

        parent = await _make_category(db_session)
        child = await _make_category(db_session, parent_id=parent.id)

        await repo.attach_to_video(v1.id, [parent.id])
        await repo.attach_to_video(v2.id, [child.id])

        # Without subcategories — only V1
        videos_flat = await repo.list_videos_in_category(
            parent.id, include_subcategories=False
        )
        assert {v.id for v in videos_flat} == {v1.id}

        # With subcategories — both
        videos_deep = await repo.list_videos_in_category(
            parent.id, include_subcategories=True
        )
        assert {v.id for v in videos_deep} == {v1.id, v2.id}

    async def test_count_videos_in_category(self, repo, db_session):
        channel = await _make_channel(db_session)
        v1 = await _make_video(db_session, channel.id)
        v2 = await _make_video(db_session, channel.id)
        c = await _make_category(db_session)
        await repo.attach_to_video(v1.id, [c.id])
        await repo.attach_to_video(v2.id, [c.id])

        count = await repo.count_videos_in_category(c.id)
        assert count == 2

    async def test_count_videos_include_subcategories(self, repo, db_session):
        channel = await _make_channel(db_session)
        v1 = await _make_video(db_session, channel.id)
        v2 = await _make_video(db_session, channel.id)

        parent = await _make_category(db_session)
        child = await _make_category(db_session, parent_id=parent.id)
        await repo.attach_to_video(v1.id, [parent.id])
        await repo.attach_to_video(v2.id, [child.id])

        assert await repo.count_videos_in_category(parent.id) == 1
        assert await repo.count_videos_in_category(
            parent.id, include_subcategories=True
        ) == 2

    async def test_list_categories_for_video(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        c1 = await _make_category(db_session, display_order=2)
        c2 = await _make_category(db_session, display_order=1)
        await repo.attach_to_video(video.id, [c1.id, c2.id])

        cats = await repo.list_categories_for_video(video.id)
        assert [c.id for c in cats] == [c2.id, c1.id]  # ordered by display_order

    async def test_list_categories_for_video_empty(self, repo, db_session):
        channel = await _make_channel(db_session)
        video = await _make_video(db_session, channel.id)
        assert await repo.list_categories_for_video(video.id) == []