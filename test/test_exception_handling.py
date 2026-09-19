"""
Exception handling test suite.

Uses REAL Postgres (via App.core.Connector.database) through the
db_session fixture in test/conftest.py. No SQLite shims.

Run:
    uv run pytest test/test_exception_handling.py -v
"""
import pytest
import pytest_asyncio

from App.core.exceptions import (
    DomainError, InfrastructureError, ValidationError, PermissionDeniedError,
    UserNotFoundError, DuplicateEmailError, CategoryNotFoundError,
    VideoNotFoundError, PlaylistNotFoundError, CommentNotFoundError,
    NotificationNotFoundError, ChannelNotFoundError, TagNotFoundError,
    DuplicateCategoryError, InvalidCategoryParentError,
    DuplicateTagError, InvalidTagSlugError,
    PlaylistAlreadyDeletedError, DuplicatePlaylistVideoError,
    PlaylistVideoNotFoundError, CommentAlreadyDeletedError,
    InvalidVideoStatusError, InvalidVideoVisibilityError,
    InvalidMemberRoleError, CannotAddOwnerAsMemberError,
    InvalidVideoReactionError, InvalidImpressionError,
    WatchHistoryNotFoundError, InvalidWatchPositionError,
    InvalidNotificationTypeError,
)
from App.repository.UserRepository import UserRepository
from App.repository.CategoryRepository import CategoryRepository
from App.repository.TagRepository import TagRepository
from App.repository.ChannelRepository import ChannelRepository
from App.repository.PlaylistRepository import PlaylistRepository
from App.repository.CommentRepository import CommentRepository
from App.repository.NotificationRepository import NotificationRepository
from App.repository.ImpressionRepository import ImpressionRepository
from App.repository.WatchHistoryRepository import WatchHistoryRepository
from App.repository.VideoRepository import VideoRepository
from App.services.category_service import CategoryService
from App.services.tag_service import TagService
from App.services.notification_service import NotificationService
from App.services.video_reaction_service import VideoReactionService


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def admin():
    return {
        "id": 1, "role": "admin", "user_role": "admin",
        "email": "admin@test.com", "permissions": {},
    }


@pytest.fixture
def no_perm_user():
    return {
        "id": 3, "role": "user", "user_role": "user",
        "email": "noperm@test.com", "permissions": {},
    }


@pytest_asyncio.fixture
async def user_and_channel(db_session):
    """Create a user + channel for tests that need FK parents."""
    user = await UserRepository(db_session).create({
        "name": "Test User", "email": "test-uc@test.com",
        "password_hash": "h", "user_role": "user", "is_active": True,
    })
    channel = await ChannelRepository(db_session).create(
        owner_id=user.id, handle="test-uc-ch", display_name="Test UC CH"
    )
    return {"user": user, "channel": channel}


@pytest_asyncio.fixture
async def user_and_video(db_session, user_and_channel):
    """User + channel + video — for comment and video tests."""
    video = await VideoRepository(db_session).create(
        channel_id=user_and_channel["channel"].id,
        title="Test Video",
        storage_bucket="b", storage_prefix="p",
    )
    return {**user_and_channel, "video": video}


# ============================================================================
# 1. EXCEPTION HIERARCHY
# ============================================================================

class TestExceptionHierarchy:

    def test_infrastructure_error_is_domain(self):
        assert issubclass(InfrastructureError, DomainError)

    def test_validation_error_is_domain(self):
        assert issubclass(ValidationError, DomainError)

    def test_permission_denied_is_domain(self):
        assert issubclass(PermissionDeniedError, DomainError)

    def test_all_not_found_errors_are_domain(self):
        for cls in [
            UserNotFoundError, CategoryNotFoundError, VideoNotFoundError,
            PlaylistNotFoundError, CommentNotFoundError,
            NotificationNotFoundError, ChannelNotFoundError, TagNotFoundError,
            WatchHistoryNotFoundError,
        ]:
            assert issubclass(cls, DomainError), f"{cls.__name__}"

    def test_duplicate_errors_are_domain(self):
        for cls in [
            DuplicateEmailError, DuplicateCategoryError,
            DuplicateTagError, DuplicatePlaylistVideoError,
        ]:
            assert issubclass(cls, DomainError)

    def test_validation_errors_are_domain(self):
        for cls in [
            InvalidCategoryParentError, InvalidTagSlugError,
            InvalidVideoStatusError, InvalidVideoVisibilityError,
            InvalidMemberRoleError,
            InvalidVideoReactionError, InvalidImpressionError,
            InvalidWatchPositionError, InvalidNotificationTypeError,
        ]:
            assert issubclass(cls, DomainError)


# ============================================================================
# 2. REPOSITORY EXCEPTIONS
# ============================================================================

class TestUserRepositoryExceptions:

    async def test_duplicate_email_raises_domain_error(self, db_session):
        repo = UserRepository(db_session)
        await repo.create({
            "name": "A", "email": "dup@test.com",
            "password_hash": "h", "user_role": "user", "is_active": True,
        })
        with pytest.raises(DuplicateEmailError):
            await repo.create({
                "name": "B", "email": "dup@test.com",
                "password_hash": "h", "user_role": "user", "is_active": True,
            })

    async def test_get_missing_user_returns_none(self, db_session):
        repo = UserRepository(db_session)
        assert await repo.get_by_id(99999) is None


class TestCategoryRepositoryExceptions:

    async def test_duplicate_slug_raises_domain_error(self, db_session):
        repo = CategoryRepository(db_session)
        await repo.create(slug="python", name="Python")
        with pytest.raises(DuplicateCategoryError):
            await repo.create(slug="python", name="Python2")

    async def test_invalid_parent_raises_domain_error(self, db_session):
        repo = CategoryRepository(db_session)
        with pytest.raises(InvalidCategoryParentError):
            await repo.create(slug="child", name="Child", parent_id=99999)


class TestTagRepositoryExceptions:

    async def test_invalid_slug_raises_domain_error(self, db_session):
        repo = TagRepository(db_session)
        with pytest.raises(InvalidTagSlugError):
            await repo.create(name="test", slug="")

    async def test_duplicate_tag_raises_domain_error(self, db_session):
        repo = TagRepository(db_session)
        await repo.create(name="Python")
        with pytest.raises(DuplicateTagError):
            await repo.create(name="Python")


class TestPlaylistRepositoryExceptions:

    async def test_invalid_visibility_raises_validation_error(self, db_session, user_and_channel):
        repo = PlaylistRepository(db_session)
        with pytest.raises(ValidationError):
            await repo.create(
                owner_id=user_and_channel["user"].id,
                title="Test", visibility="invalid_mode",
            )

    async def test_invalid_visibility_on_update_raises(self, db_session, user_and_channel):
        repo = PlaylistRepository(db_session)
        pl = await repo.create(
            owner_id=user_and_channel["user"].id, title="Test"
        )
        with pytest.raises(ValidationError):
            await repo.update(pl.id, visibility="invalid_mode")

    async def test_duplicate_video_raises_domain_error(self, db_session, user_and_video):
        repo = PlaylistRepository(db_session)
        pl = await repo.create(
            owner_id=user_and_video["user"].id, title="Test"
        )
        vid = user_and_video["video"].id
        await repo.add_video(pl.id, video_id=vid)
        with pytest.raises(DuplicatePlaylistVideoError):
            await repo.add_video(pl.id, video_id=vid)

    async def test_remove_missing_video_raises_domain_error(self, db_session, user_and_channel):
        repo = PlaylistRepository(db_session)
        pl = await repo.create(
            owner_id=user_and_channel["user"].id, title="Test"
        )
        with pytest.raises(PlaylistVideoNotFoundError):
            await repo.remove_video(pl.id, video_id=99999)

    async def test_soft_delete_twice_raises_domain_error(self, db_session, user_and_channel):
        repo = PlaylistRepository(db_session)
        pl = await repo.create(
            owner_id=user_and_channel["user"].id, title="Test"
        )
        await repo.soft_delete(pl.id)
        with pytest.raises(PlaylistAlreadyDeletedError):
            await repo.soft_delete(pl.id)


class TestImpressionRepositoryExceptions:

    async def test_invalid_days_raises_validation_error(self, db_session):
        repo = ImpressionRepository(db_session)
        with pytest.raises(ValidationError):
            await repo.delete_older_than(0)

    async def test_invalid_identity_raises_domain_error(self, db_session):
        repo = ImpressionRepository(db_session)
        with pytest.raises(InvalidImpressionError):
            await repo.create(video_id=1)


class TestWatchHistoryRepositoryExceptions:

    async def test_invalid_days_raises_validation_error(self, db_session):
        repo = WatchHistoryRepository(db_session)
        with pytest.raises(ValidationError):
            await repo.delete_older_than(0)

    async def test_negative_position_raises_domain_error(self, db_session):
        repo = WatchHistoryRepository(db_session)
        with pytest.raises(InvalidWatchPositionError):
            await repo.upsert(
                user_id=1, video_id=1, channel_id=1,
                last_position_sec=-5,
            )

    async def test_delete_missing_raises_domain_error(self, db_session):
        repo = WatchHistoryRepository(db_session)
        with pytest.raises(WatchHistoryNotFoundError):
            await repo.delete(user_id=999, video_id=999)


class TestNotificationRepositoryExceptions:

    async def test_invalid_days_raises_validation_error(self, db_session):
        repo = NotificationRepository(db_session)
        with pytest.raises(ValidationError):
            await repo.delete_older_than(0)

    async def test_invalid_type_raises_domain_error(self, db_session):
        repo = NotificationRepository(db_session)
        with pytest.raises(InvalidNotificationTypeError):
            await repo.create(
                recipient_id=1, notification_type="unknown_type"
            )


class TestChannelRepositoryExceptions:

    async def test_invalid_member_role_raises_domain_error(self, db_session, user_and_channel):
        repo = ChannelRepository(db_session)
        with pytest.raises(InvalidMemberRoleError):
            await repo.add_member(
                channel_id=user_and_channel["channel"].id,
                user_id=user_and_channel["user"].id,
                role="invalid_role",
            )

    async def test_cannot_add_owner_as_member(self, db_session, user_and_channel):
        repo = ChannelRepository(db_session)
        with pytest.raises(CannotAddOwnerAsMemberError):
            await repo.add_member(
                channel_id=user_and_channel["channel"].id,
                user_id=user_and_channel["user"].id,
                role="editor",
            )


class TestVideoRepositoryExceptions:

    async def test_invalid_status_raises_domain_error(self, db_session, user_and_video):
        repo = VideoRepository(db_session)
        with pytest.raises(InvalidVideoStatusError):
            await repo.set_status(user_and_video["video"].id, "invalid_status")

    async def test_invalid_visibility_raises_domain_error(self, db_session, user_and_video):
        repo = VideoRepository(db_session)
        with pytest.raises(InvalidVideoVisibilityError):
            await repo.set_visibility(
                user_and_video["video"].id, "invalid_visibility"
            )


class TestCommentRepositoryExceptions:

    async def test_soft_delete_twice_raises_domain_error(self, db_session, user_and_video):
        repo = CommentRepository(db_session)
        c = await repo.create(
            video_id=user_and_video["video"].id,
            user_id=user_and_video["user"].id,
            content="test",
        )
        await repo.soft_delete(c.id)
        with pytest.raises(CommentAlreadyDeletedError):
            await repo.soft_delete(c.id)


# ============================================================================
# 3. SERVICE PERMISSION EXCEPTIONS
# ============================================================================

class TestCategoryServicePermissions:

    async def test_no_auth_raises_permission_denied(self, db_session):
        service = CategoryService(db_session)
        with pytest.raises(PermissionDeniedError):
            await service.create_category(
                {"id": None, "role": "user", "permissions": {}},
                slug="x", name="X",
            )

    async def test_no_permission_raises_permission_denied(self, db_session, no_perm_user):
        service = CategoryService(db_session)
        with pytest.raises(PermissionDeniedError):
            await service.create_category(
                no_perm_user, slug="x", name="X"
            )

    async def test_admin_bypasses_permission_check(self, db_session, admin):
        service = CategoryService(db_session)
        cat = await service.create_category(
            admin, slug="admin-created", name="Admin Created"
        )
        assert cat.id is not None


class TestTagServicePermissions:

    async def test_no_auth_raises_permission_denied(self, db_session):
        service = TagService(db_session)
        with pytest.raises(PermissionDeniedError):
            await service.get_or_create("Python", {"id": None})

    async def test_delete_unused_requires_admin(self, db_session, no_perm_user):
        service = TagService(db_session)
        with pytest.raises(PermissionDeniedError):
            await service.delete_unused(no_perm_user)


class TestNotificationServicePermissions:

    async def test_list_requires_auth(self, db_session):
        service = NotificationService(db_session)
        with pytest.raises(PermissionDeniedError):
            await service.list_my_notifications({"id": None})

    async def test_broadcast_requires_admin(self, db_session, no_perm_user):
        service = NotificationService(db_session)
        with pytest.raises(PermissionDeniedError):
            await service.broadcast_new_video(
                no_perm_user,
                video_id=1, channel_id=1, actor_id=1, payload={},
            )


class TestVideoReactionServicePermissions:

    async def test_reaction_requires_auth(self, db_session):
        service = VideoReactionService(db_session)
        with pytest.raises(PermissionDeniedError):
            await service.set_reaction(1, {"id": None}, "like")


# ============================================================================
# 4. NO BUILTIN EXCEPTIONS LEAK
# ============================================================================

class TestNoBuiltinExceptionsLeak:

    async def test_playlist_does_not_raise_builtin_valueerror(self, db_session, user_and_channel):
        repo = PlaylistRepository(db_session)
        try:
            await repo.create(
                owner_id=user_and_channel["user"].id,
                title="T", visibility="bad",
            )
            pytest.fail("Expected ValidationError")
        except ValidationError:
            pass
        except ValueError:
            pytest.fail("Builtin ValueError leaked!")

    async def test_impression_does_not_raise_builtin_valueerror(self, db_session):
        repo = ImpressionRepository(db_session)
        try:
            await repo.delete_older_than(0)
            pytest.fail("Expected ValidationError")
        except ValidationError:
            pass
        except ValueError:
            pytest.fail("Builtin ValueError leaked!")

    async def test_watch_history_does_not_raise_builtin_valueerror(self, db_session):
        repo = WatchHistoryRepository(db_session)
        try:
            await repo.delete_older_than(0)
            pytest.fail("Expected ValidationError")
        except ValidationError:
            pass
        except ValueError:
            pytest.fail("Builtin ValueError leaked!")

    async def test_notification_does_not_raise_builtin_valueerror(self, db_session):
        repo = NotificationRepository(db_session)
        try:
            await repo.delete_older_than(0)
            pytest.fail("Expected ValidationError")
        except ValidationError:
            pass
        except ValueError:
            pytest.fail("Builtin ValueError leaked!")

    async def test_service_does_not_raise_builtin_permissionerror(self, db_session):
        service = CategoryService(db_session)
        try:
            await service.create_category(
                {"id": None, "role": "user", "permissions": {}},
                slug="x", name="X",
            )
            pytest.fail("Expected PermissionDeniedError")
        except PermissionDeniedError:
            pass
        except PermissionError:
            pytest.fail("Builtin PermissionError leaked!")