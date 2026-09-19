from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, or_, text, func
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List, Tuple
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
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
    InfrastructureError,
    GrantUsageExhaustedError,
)
from App.api.databases.MigrateTable import (
    Video,
    VideoVariant,
    VideoGrant,
    VideoTag,
    VideoCategory,
    VideoReaction,
    Tag,
    Category,
)


class VideoRepository:
    """
    Repository for the Video aggregate.

    Owns: videos, video_variants, video_grants, video_tags,
          video_categories, video_reactions.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries. Callers must call `await session.commit()`
        (or use a unit-of-work) after all desired mutations.

    Storage policy:
        This repository NEVER touches MinIO or any external storage.
        Existence checks and presigning live in the service layer.

    Deletion policy:
        Soft-delete is tracked by `deleted_at IS NULL`. `status='deleted'`
        is informational only and is set alongside `deleted_at`. Queries
        filter on `deleted_at` consistently — never on `status='deleted'`.
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

    async def _get_video_or_raise(
        self, video_id: int, *, include_deleted: bool = False
    ) -> Video:
        stmt = select(Video).where(Video.id == video_id)
        if not include_deleted:
            stmt = stmt.where(Video.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        video = result.scalar_one_or_none()
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        return video

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # =================================================================
    # VIDEO — READ
    # =================================================================

    async def get_by_id(
        self, video_id: int, *, include_deleted: bool = False
    ) -> Optional[Video]:
        stmt = select(Video).where(Video.id == video_id)
        if not include_deleted:
            stmt = stmt.where(Video.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_uuid(
        self, video_uuid: str, *, include_deleted: bool = False
    ) -> Optional[Video]:
        stmt = select(Video).where(Video.video_uuid == video_uuid)
        if not include_deleted:
            stmt = stmt.where(Video.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_relations(self, video_id: int) -> Optional[Video]:
        """Eager-load variants, tags, and categories for the watch page."""
        stmt = (
            select(Video)
            .where(Video.id == video_id, Video.deleted_at.is_(None))
            .options(
                selectinload(Video.variants),
                selectinload(Video.tags).joinedload(VideoTag.tag),
                selectinload(Video.categories).joinedload(VideoCategory.category),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_grants(self, video_id: int) -> Optional[Video]:
        """Eager-load grants for share-management views."""
        stmt = (
            select(Video)
            .where(Video.id == video_id, Video.deleted_at.is_(None))
            .options(selectinload(Video.grants))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_channel(
        self,
        channel_id: int,
        *,
        visibility: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Video]:
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

    async def list_public_feed(
        self, *, limit: int = 20, offset: int = 0
    ) -> List[Video]:
        stmt = (
            select(Video)
            .where(
                Video.visibility == "public",
                Video.status == "ready",
                Video.deleted_at.is_(None),
            )
            .order_by(Video.published_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_tag(
        self, tag_id: int, *, limit: int = 20, offset: int = 0
    ) -> List[Video]:
        stmt = (
            select(Video)
            .join(VideoTag, VideoTag.video_id == Video.id)
            .where(
                VideoTag.tag_id == tag_id,
                Video.visibility == "public",
                Video.status == "ready",
                Video.deleted_at.is_(None),
            )
            .order_by(Video.published_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_category(
        self, category_id: int, *, limit: int = 20, offset: int = 0
    ) -> List[Video]:
        stmt = (
            select(Video)
            .join(VideoCategory, VideoCategory.video_id == Video.id)
            .where(
                VideoCategory.category_id == category_id,
                Video.visibility == "public",
                Video.status == "ready",
                Video.deleted_at.is_(None),
            )
            .order_by(Video.published_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_by_title(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        public_only: bool = True,
    ) -> List[Video]:
        """
        Case-insensitive prefix/substring match on title.
        Uses ILIKE — pair with a pg_trgm GIN index on videos.title
        for large tables.
        """
        pattern = f"%{query}%"
        stmt = (
            select(Video)
            .where(Video.title.ilike(pattern), Video.deleted_at.is_(None))
            .order_by(Video.published_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        if public_only:
            stmt = stmt.where(
                Video.visibility == "public",
                Video.status == "ready",
            )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # =================================================================
    # VIDEO — COUNTS (for pagination metadata)
    # =================================================================

    async def count_by_channel(
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

    async def count_public_feed(self) -> int:
        stmt = (
            select(func.count())
            .select_from(Video)
            .where(
                Video.visibility == "public",
                Video.status == "ready",
                Video.deleted_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_by_tag(self, tag_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Video)
            .join(VideoTag, VideoTag.video_id == Video.id)
            .where(
                VideoTag.tag_id == tag_id,
                Video.visibility == "public",
                Video.status == "ready",
                Video.deleted_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_by_category(self, category_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Video)
            .join(VideoCategory, VideoCategory.video_id == Video.id)
            .where(
                VideoCategory.category_id == category_id,
                Video.visibility == "public",
                Video.status == "ready",
                Video.deleted_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_search_by_title(
        self, query: str, *, public_only: bool = True
    ) -> int:
        pattern = f"%{query}%"
        stmt = (
            select(func.count())
            .select_from(Video)
            .where(Video.title.ilike(pattern), Video.deleted_at.is_(None))
        )
        if public_only:
            stmt = stmt.where(
                Video.visibility == "public",
                Video.status == "ready",
            )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # VIDEO — WRITE
    # =================================================================

    async def create(
        self,
        *,
        channel_id: int,
        title: str,
        storage_bucket: str,
        storage_prefix: str,
        description: Optional[str] = None,
        thumbnail_key: Optional[str] = None,
        master_playlist_key: str = "master.m3u8",
    ) -> Video:
        video = Video(
            channel_id=channel_id,
            title=title,
            description=description,
            thumbnail_key=thumbnail_key,
            storage_bucket=storage_bucket,
            storage_prefix=storage_prefix,
            master_playlist_key=master_playlist_key,
            status="processing",
            visibility="private",
        )
        self.session.add(video)
        await self._safe_flush()
        await self.session.refresh(video)
        return video

    async def update_metadata(
        self,
        video_id: int,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_key: Optional[str] = None,
    ) -> Video:
        video = await self._get_video_or_raise(video_id)
        if title is not None:
            video.title = title
        if description is not None:
            video.description = description
        if thumbnail_key is not None:
            video.thumbnail_key = thumbnail_key
        await self._safe_flush()
        return video

    async def set_status(self, video_id: int, status: str) -> Video:
        if status not in ("processing", "ready", "failed", "deleted"):
            raise InvalidVideoStatusError(f"Invalid status: {status}")
        video = await self._get_video_or_raise(video_id, include_deleted=True)
        video.status = status
        await self._safe_flush()
        return video

    async def set_visibility(self, video_id: int, visibility: str) -> Video:
        if visibility not in ("private", "unlisted", "public"):
            raise InvalidVideoVisibilityError(f"Invalid visibility: {visibility}")

        video = await self._get_video_or_raise(video_id)

        if visibility in ("public", "unlisted") and video.status != "ready":
            raise VideoNotReadyError(
                f"Cannot set visibility='{visibility}' while status='{video.status}'"
            )

        video.visibility = visibility
        if visibility in ("public", "unlisted") and video.published_at is None:
            video.published_at = self._now()
        await self._safe_flush()
        return video

    async def soft_delete(self, video_id: int) -> None:
        video = await self._get_video_or_raise(video_id, include_deleted=True)
        if video.deleted_at is not None:
            raise VideoAlreadyDeletedError(f"Video {video_id} already deleted")
        video.deleted_at = self._now()
        video.status = "deleted"
        await self._safe_flush()

    # =================================================================
    # VIDEO — COUNTERS (atomic, worker-only)
    # =================================================================

    async def increment_view_count(self, video_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(view_count=Video.view_count + by)
        )

    async def increment_like_count(self, video_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(like_count=Video.like_count + by)
        )

    async def increment_dislike_count(self, video_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(dislike_count=Video.dislike_count + by)
        )

    async def increment_comment_count(self, video_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(comment_count=Video.comment_count + by)
        )

    async def increment_save_count(self, video_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(save_count=Video.save_count + by)
        )

    async def increment_share_count(self, video_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(share_count=Video.share_count + by)
        )

    async def update_total_size(self, video_id: int, size_bytes: int) -> None:
        await self.session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(total_size_bytes=size_bytes)
        )

    # =================================================================
    # VIDEO VARIANTS
    # =================================================================

    async def add_variant(
        self,
        *,
        video_id: int,
        quality: str,
        codec: str,
        playlist_key: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        bitrate_kbps: Optional[int] = None,
        segment_count: Optional[int] = None,
        size_bytes: Optional[int] = None,
        status: str = "pending",
    ) -> VideoVariant:
        variant = VideoVariant(
            video_id=video_id,
            quality=quality,
            codec=codec,
            playlist_key=playlist_key,
            width=width,
            height=height,
            bitrate_kbps=bitrate_kbps,
            segment_count=segment_count,
            size_bytes=size_bytes,
            status=status,
        )
        self.session.add(variant)
        await self._safe_flush()
        await self.session.refresh(variant)
        return variant

    async def get_variant(
        self, video_id: int, quality: str, codec: str
    ) -> Optional[VideoVariant]:
        stmt = select(VideoVariant).where(
            VideoVariant.video_id == video_id,
            VideoVariant.quality == quality,
            VideoVariant.codec == codec,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_variant_by_id(self, variant_id: int) -> Optional[VideoVariant]:
        stmt = select(VideoVariant).where(VideoVariant.id == variant_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_ready_variants(self, video_id: int) -> List[VideoVariant]:
        stmt = (
            select(VideoVariant)
            .where(
                VideoVariant.video_id == video_id,
                VideoVariant.status == "ready",
            )
            .order_by(VideoVariant.height.desc().nullslast())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_all_variants(self, video_id: int) -> List[VideoVariant]:
        """All variants regardless of status — admin / debug / re-transcode."""
        stmt = (
            select(VideoVariant)
            .where(VideoVariant.video_id == video_id)
            .order_by(
                VideoVariant.height.desc().nullslast(),
                VideoVariant.created_at.asc(),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def set_variant_status(
        self,
        variant_id: int,
        status: str,
        *,
        size_bytes: Optional[int] = None,
        segment_count: Optional[int] = None,
    ) -> None:
        if status not in ("pending", "processing", "ready", "failed"):
            raise InvalidVideoStatusError(f"Invalid variant status: {status}")

        values = {"status": status}
        if size_bytes is not None:
            values["size_bytes"] = size_bytes
        if segment_count is not None:
            values["segment_count"] = segment_count

        result = await self.session.execute(
            update(VideoVariant)
            .where(VideoVariant.id == variant_id)
            .values(**values)
            .returning(VideoVariant.id)
        )
        if result.scalar_one_or_none() is None:
            raise VideoVariantNotFoundError(f"Variant {variant_id} not found")

    async def delete_variant(self, variant_id: int) -> None:
        """Hard-delete a variant row (e.g. cleanup after failed transcode)."""
        result = await self.session.execute(
            delete(VideoVariant)
            .where(VideoVariant.id == variant_id)
            .returning(VideoVariant.id)
        )
        if result.scalar_one_or_none() is None:
            raise VideoVariantNotFoundError(f"Variant {variant_id} not found")

    # =================================================================
    # VIDEO GRANTS — user grants + link grants
    # =================================================================

    async def grant_to_user(
        self,
        *,
        video_id: int,
        user_id: int,
        granted_by_id: int,
        can_view: bool = True,
        can_download: bool = False,
        can_reshare: bool = False,
        expires_at: Optional[datetime] = None,
    ) -> VideoGrant:
        if user_id == granted_by_id:
            raise InvalidGranteeError("Cannot grant to self")

        existing = await self.get_active_user_grant(video_id, user_id)
        if existing is not None:
            raise DuplicateVideoGrantError(
                f"Grant already exists for user {user_id} on video {video_id}"
            )

        grant = VideoGrant(
            video_id=video_id,
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
        video_id: int,
        granted_by_id: int,
        link_token: str,
        can_view: bool = True,
        can_download: bool = False,
        can_reshare: bool = False,
        password_hash: Optional[str] = None,
        max_uses: Optional[int] = None,
        expires_at: Optional[datetime] = None,
    ) -> VideoGrant:
        grant = VideoGrant(
            video_id=video_id,
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
            update(VideoGrant)
            .where(VideoGrant.id == grant_id, VideoGrant.revoked_at.is_(None))
            .values(revoked_at=self._now())
            .returning(VideoGrant.id)
        )
        if result.scalar_one_or_none() is None:
            raise VideoGrantNotFoundError(
                f"Grant {grant_id} not found or already revoked"
            )

    async def get_active_user_grant(
        self, video_id: int, user_id: int
    ) -> Optional[VideoGrant]:
        now = self._now()
        stmt = select(VideoGrant).where(
            VideoGrant.video_id == video_id,
            VideoGrant.grantee_type == "user",
            VideoGrant.grantee_user_id == user_id,
            VideoGrant.revoked_at.is_(None),
            or_(
                VideoGrant.expires_at.is_(None),
                VideoGrant.expires_at > now,
            ),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_grant_by_token(self, link_token: str) -> Optional[VideoGrant]:
        now = self._now()
        stmt = select(VideoGrant).where(
            VideoGrant.grantee_type == "link",
            VideoGrant.link_token == link_token,
            VideoGrant.revoked_at.is_(None),
            or_(
                VideoGrant.expires_at.is_(None),
                VideoGrant.expires_at > now,
            ),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def increment_grant_use(self, grant_id: int) -> None:
        """
        Atomically increment use_count ONLY if max_uses is not yet reached.
        Raises GrantUsageExhaustedError if already at limit.
        """
        result = await self.session.execute(
            update(VideoGrant)
            .where(
                VideoGrant.id == grant_id,
                VideoGrant.revoked_at.is_(None),
                or_(
                    VideoGrant.max_uses.is_(None),
                    VideoGrant.use_count < VideoGrant.max_uses,
                ),
            )
            .values(use_count=VideoGrant.use_count + 1)
            .returning(VideoGrant.id)
        )
        if result.scalar_one_or_none() is None:
            raise GrantUsageExhaustedError(
                f"Grant {grant_id} is exhausted or revoked"
            )

    async def list_grants_for_video(self, video_id: int) -> List[VideoGrant]:
        stmt = (
            select(VideoGrant)
            .where(
                VideoGrant.video_id == video_id,
                VideoGrant.revoked_at.is_(None),
            )
            .order_by(VideoGrant.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_grants_for_grantee(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[VideoGrant]:
        """
        Active grants where `user_id` is the grantee.
        For the "Shared with me" view.
        """
        now = self._now()
        stmt = (
            select(VideoGrant)
            .where(
                VideoGrant.grantee_type == "user",
                VideoGrant.grantee_user_id == user_id,
                VideoGrant.revoked_at.is_(None),
                or_(
                    VideoGrant.expires_at.is_(None),
                    VideoGrant.expires_at > now,
                ),
            )
            .order_by(VideoGrant.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_grants_for_grantee(self, user_id: int) -> int:
        now = self._now()
        stmt = (
            select(func.count())
            .select_from(VideoGrant)
            .where(
                VideoGrant.grantee_type == "user",
                VideoGrant.grantee_user_id == user_id,
                VideoGrant.revoked_at.is_(None),
                or_(
                    VideoGrant.expires_at.is_(None),
                    VideoGrant.expires_at > now,
                ),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # VIDEO TAGS — attachments
    # =================================================================

    async def attach_tags(self, video_id: int, tag_ids: List[int]) -> None:
        if not tag_ids:
            return
        result = await self.session.execute(
            select(Tag.id).where(Tag.id.in_(tag_ids))
        )
        existing_ids = {row[0] for row in result.all()}
        missing = set(tag_ids) - existing_ids
        if missing:
            raise TagNotFoundError(f"Tags not found: {sorted(missing)}")

        stmt = (
            pg_insert(VideoTag)
            .values([{"video_id": video_id, "tag_id": tid} for tid in tag_ids])
            .on_conflict_do_nothing(index_elements=["video_id", "tag_id"])
        )
        await self.session.execute(stmt)

    async def detach_tags(self, video_id: int, tag_ids: List[int]) -> None:
        if not tag_ids:
            return
        await self.session.execute(
            delete(VideoTag).where(
                VideoTag.video_id == video_id,
                VideoTag.tag_id.in_(tag_ids),
            )
        )

    async def set_tags(self, video_id: int, tag_ids: List[int]) -> None:
        """Replace all tags for a video — validated before mutation."""
        if tag_ids:
            result = await self.session.execute(
                select(Tag.id).where(Tag.id.in_(tag_ids))
            )
            existing_ids = {row[0] for row in result.all()}
            missing = set(tag_ids) - existing_ids
            if missing:
                raise TagNotFoundError(f"Tags not found: {sorted(missing)}")

        await self.session.execute(
            delete(VideoTag).where(VideoTag.video_id == video_id)
        )
        if tag_ids:
            stmt = (
                pg_insert(VideoTag)
                .values(
                    [{"video_id": video_id, "tag_id": tid} for tid in tag_ids]
                )
                .on_conflict_do_nothing(index_elements=["video_id", "tag_id"])
            )
            await self.session.execute(stmt)

    async def list_video_tags(self, video_id: int) -> List[Tag]:
        stmt = (
            select(Tag)
            .join(VideoTag, VideoTag.tag_id == Tag.id)
            .where(VideoTag.video_id == video_id)
            .order_by(Tag.name.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # =================================================================
    # VIDEO CATEGORIES — attachments
    # =================================================================

    async def attach_categories(
        self, video_id: int, category_ids: List[int]
    ) -> None:
        if not category_ids:
            return
        result = await self.session.execute(
            select(Category.id).where(Category.id.in_(category_ids))
        )
        existing_ids = {row[0] for row in result.all()}
        missing = set(category_ids) - existing_ids
        if missing:
            raise CategoryNotFoundError(
                f"Categories not found: {sorted(missing)}"
            )

        stmt = (
            pg_insert(VideoCategory)
            .values(
                [{"video_id": video_id, "category_id": cid} for cid in category_ids]
            )
            .on_conflict_do_nothing(index_elements=["video_id", "category_id"])
        )
        await self.session.execute(stmt)

    async def set_categories(
        self, video_id: int, category_ids: List[int]
    ) -> None:
        """
        Replace all categories. Validates BEFORE mutating so a bad input
        can't leave the session with a deleted-and-not-reinserted state.
        """
        if category_ids:
            result = await self.session.execute(
                select(Category.id).where(Category.id.in_(category_ids))
            )
            existing_ids = {row[0] for row in result.all()}
            missing = set(category_ids) - existing_ids
            if missing:
                raise CategoryNotFoundError(
                    f"Categories not found: {sorted(missing)}"
                )

        await self.session.execute(
            delete(VideoCategory).where(VideoCategory.video_id == video_id)
        )
        if category_ids:
            stmt = (
                pg_insert(VideoCategory)
                .values(
                    [
                        {"video_id": video_id, "category_id": cid}
                        for cid in category_ids
                    ]
                )
                .on_conflict_do_nothing(index_elements=["video_id", "category_id"])
            )
            await self.session.execute(stmt)

    async def list_video_categories(self, video_id: int) -> List[Category]:
        stmt = (
            select(Category)
            .join(VideoCategory, VideoCategory.category_id == Category.id)
            .where(VideoCategory.video_id == video_id)
            .order_by(Category.display_order.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # =================================================================
    # VIDEO REACTIONS
    # =================================================================

    async def upsert_reaction(
        self, video_id: int, user_id: int, reaction: str
    ) -> Tuple[str, Optional[str]]:
        """
        Set or change a user's reaction to a video.

        Returns (action, previous_reaction):
            - ('created',   None)            → new reaction row
            - ('updated',   'like'|'dislike') → reaction changed
            - ('unchanged', None)            → no-op (same reaction already set)

        Caller adjusts video.like_count / dislike_count based on this.
        """
        if reaction not in ("like", "dislike"):
            raise InvalidVideoReactionError(f"Invalid reaction: {reaction}")

        stmt = select(VideoReaction).where(
            VideoReaction.video_id == video_id,
            VideoReaction.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is None:
            self.session.add(
                VideoReaction(
                    video_id=video_id, user_id=user_id, reaction=reaction
                )
            )
            await self._safe_flush()
            return ("created", None)

        if existing.reaction == reaction:
            return ("unchanged", None)

        previous = existing.reaction
        existing.reaction = reaction
        await self._safe_flush()
        return ("updated", previous)

    async def delete_reaction(
        self, video_id: int, user_id: int
    ) -> Optional[str]:
        """Returns the previous reaction ('like'|'dislike') or None."""
        stmt = select(VideoReaction).where(
            VideoReaction.video_id == video_id,
            VideoReaction.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is None:
            return None
        previous = existing.reaction
        await self.session.delete(existing)
        await self._safe_flush()
        return previous

    async def get_reaction(
        self, video_id: int, user_id: int
    ) -> Optional[VideoReaction]:
        stmt = select(VideoReaction).where(
            VideoReaction.video_id == video_id,
            VideoReaction.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_media_info(
        self,
        video_id: int,
        *,
        duration_seconds: Optional[float] = None,
        total_size_bytes: Optional[int] = None,
    ) -> Video:
        """Called by the transcoding worker after ffmpeg completes."""
        video = await self._get_video_or_raise(video_id, include_deleted=True)
        if duration_seconds is not None:
            video.duration_seconds = duration_seconds
        if total_size_bytes is not None:
            video.total_size_bytes = total_size_bytes
        await self._safe_flush()
        return video