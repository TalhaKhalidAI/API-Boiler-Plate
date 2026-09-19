# App/repository/TagRepository.py

import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List, Tuple
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    TagNotFoundError,
    DuplicateTagError,
    InvalidTagSlugError,
    InfrastructureError,
)
from App.api.databases.MigrateTable import (
    Tag,
    VideoTag,
    Video,
)


class TagRepository:
    """
    Repository for the Tag aggregate.

    Owns: tags, video_tags.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Slug policy:
        `slug` is the canonical identifier — lowercase, URL-safe,
        hyphen-separated. `name` is the display form (may preserve
        case, spaces, etc.). Two tags with different names can share
        a slug ONLY if the service normalizes before insert — but
        the DB unique constraint on slug means the second insert
        fails. So the rule is: slug must be globally unique, and
        slug should be derived from name via `slugify`.

    Counter policy:
        `tags.usage_count` is denormalized (COUNT of video_tags
        per tag). The service must call `increment_usage_count(+1)`
        after `attach_to_video` and `(-1)` after
        `detach_from_video`. This repo does NOT auto-update the
        counter — that keeps the attach/detach path cheap for bulk
        operations. A reconciliation method is provided.

    Get-or-create pattern:
        `get_or_create` is the common entry point. It slugs the
        input name, returns an existing tag if the slug is taken,
        or creates a new one. Case-insensitive — "Python" and
        "python" map to the same slug `python`.
    """

    # Slug pattern: lowercase, alphanumeric, hyphens only. Must start
    # and end with alphanumeric. 1-50 chars. No leading/trailing/double
    # hyphens.
    SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

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

    async def _get_or_raise(self, tag_id: int) -> Tag:
        stmt = select(Tag).where(Tag.id == tag_id)
        result = await self.session.execute(stmt)
        tag = result.scalar_one_or_none()
        if tag is None:
            raise TagNotFoundError(f"Tag {tag_id} not found")
        return tag

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def slugify(name: str) -> str:
        """
        Convert a display name to a slug:
          - lowercase
          - strip
          - non-alphanumeric → hyphen
          - collapse multiple hyphens
          - trim leading/trailing hyphens
          - truncate to 50 chars

        "  Python 3.11  " → "python-3-11"
        "C++ & Rust"     → "c-rust"
        """
        s = name.strip().lower()
        # Replace anything not a-z0-9 with a hyphen
        s = re.sub(r"[^a-z0-9]+", "-", s)
        # Collapse multiple hyphens
        s = re.sub(r"-+", "-", s)
        # Trim leading/trailing hyphens
        s = s.strip("-")
        # Truncate
        s = s[:50].rstrip("-")
        return s

    @classmethod
    def _validate_slug(cls, slug: str) -> None:
        if not slug:
            raise InvalidTagSlugError("Slug cannot be empty")
        if len(slug) > 50:
            raise InvalidTagSlugError(f"Slug too long: {len(slug)} > 50")
        if not cls.SLUG_PATTERN.match(slug):
            raise InvalidTagSlugError(
                f"Invalid slug: {slug!r}. Must match {cls.SLUG_PATTERN.pattern}"
            )

    # =================================================================
    # TAG — READ
    # =================================================================

    async def get_by_id(self, tag_id: int) -> Optional[Tag]:
        stmt = select(Tag).where(Tag.id == tag_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[Tag]:
        stmt = select(Tag).where(Tag.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Optional[Tag]:
        """
        Case-insensitive lookup by display name. Slugifies the input
        and looks up by slug — this is how "Python" and "python" and
        "PYTHON" all map to the same tag.
        """
        return await self.get_by_slug(self.slugify(name))

    async def get_many_by_ids(self, tag_ids: List[int]) -> List[Tag]:
        if not tag_ids:
            return []
        stmt = select(Tag).where(Tag.id.in_(tag_ids))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_popular(
        self, *, limit: int = 50, offset: int = 0
    ) -> List[Tag]:
        """
        Tags with the highest usage_count, tie-broken by name.
        For tag cloud / discovery pages.
        """
        stmt = (
            select(Tag)
            .order_by(Tag.usage_count.desc(), Tag.name.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_all(
        self, *, limit: int = 100, offset: int = 0
    ) -> List[Tag]:
        """All tags, alphabetical. For admin views."""
        stmt = (
            select(Tag)
            .order_by(Tag.name.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_by_name(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Tag]:
        """
        Case-insensitive substring match on name. For tag autocomplete.
        Pair with a pg_trgm GIN index on tags.name for large tables.
        """
        pattern = f"%{query}%"
        stmt = (
            select(Tag)
            .where(Tag.name.ilike(pattern))
            .order_by(Tag.usage_count.desc(), Tag.name.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_all(self) -> int:
        stmt = select(func.count()).select_from(Tag)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_search_by_name(self, query: str) -> int:
        pattern = f"%{query}%"
        stmt = (
            select(func.count())
            .select_from(Tag)
            .where(Tag.name.ilike(pattern))
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # TAG — WRITE
    # =================================================================

    async def create(self, *, name: str, slug: Optional[str] = None) -> Tag:
        """
        Create a new tag. If `slug` is not provided, it's derived
        from `name` via `slugify`.

        Raises:
          - InvalidTagSlugError if the slug is malformed
          - DuplicateTagError if the slug already exists
        """
        if slug is None:
            slug = self.slugify(name)
        self._validate_slug(slug)

        if await self.get_by_slug(slug) is not None:
            raise DuplicateTagError(f"Tag slug '{slug}' already exists")

        tag = Tag(
            slug=slug,
            name=name,
            usage_count=0,
        )
        self.session.add(tag)
        await self._safe_flush()
        await self.session.refresh(tag)
        return tag

    async def get_or_create(self, name: str) -> Tuple[Tag, bool]:
        """
        Return (tag, created) where created=True if the tag was
        just inserted, False if it already existed.

        Idempotent. Safe to call from concurrent requests — the
        race is handled by catching the unique constraint violation
        and re-fetching.
        """
        slug = self.slugify(name)
        self._validate_slug(slug)

        # Fast path: look up first
        existing = await self.get_by_slug(slug)
        if existing is not None:
            return existing, False

        # Race path: try to insert; on conflict, refetch
        stmt = (
            pg_insert(Tag)
            .values({"slug": slug, "name": name, "usage_count": 0})
            .on_conflict_do_nothing(index_elements=["slug"])
            .returning(Tag)
        )
        result = await self.session.execute(stmt)
        inserted = result.scalar_one_or_none()

        if inserted is not None:
            await self.session.refresh(inserted)
            return inserted, True

        # Someone else won the race — fetch their row
        winner = await self.get_by_slug(slug)
        if winner is None:
            # Extremely unlikely — the row was inserted then deleted
            # between our insert attempt and this fetch. Retry once.
            raise InfrastructureError(
                f"Tag '{slug}' vanished between insert and refetch"
            )
        return winner, False

    async def update_name(self, tag_id: int, name: str) -> Tag:
        """
        Update a tag's display name. The slug is NOT changed — slug
        is permanent once created. Renaming a tag doesn't break
        references or URLs.
        """
        tag = await self._get_or_raise(tag_id)
        tag.name = name
        await self._safe_flush()
        return tag

    async def delete(self, tag_id: int) -> None:
        """
        Hard-delete a tag. All `video_tags` rows pointing at it are
        removed via ON DELETE CASCADE. The `usage_count` on the tag
        is meaningless after this since the row is gone.
        """
        result = await self.session.execute(
            delete(Tag)
            .where(Tag.id == tag_id)
            .returning(Tag.id)
        )
        if result.scalar_one_or_none() is None:
            raise TagNotFoundError(f"Tag {tag_id} not found")

    async def delete_unused(self) -> int:
        """
        Delete every tag with usage_count = 0.
        Returns the number of tags removed. Useful as a cleanup job.
        """
        result = await self.session.execute(
            delete(Tag)
            .where(Tag.usage_count == 0)
            .returning(Tag.id)
        )
        return len(result.all())

    # =================================================================
    # TAG — COUNTERS
    # =================================================================

    async def increment_usage_count(self, tag_id: int, by: int = 1) -> None:
        """Atomic increment/decrement of a tag's usage_count."""
        await self.session.execute(
            update(Tag)
            .where(Tag.id == tag_id)
            .values(usage_count=Tag.usage_count + by)
        )

    async def recompute_usage_count(self, tag_id: int) -> int:
        """
        Recompute `tags.usage_count` from the actual `video_tags`
        rows. Returns the fresh count.
        """
        stmt = (
            select(func.count())
            .select_from(VideoTag)
            .where(VideoTag.tag_id == tag_id)
        )
        actual = (await self.session.execute(stmt)).scalar_one()
        await self.session.execute(
            update(Tag)
            .where(Tag.id == tag_id)
            .values(usage_count=actual)
        )
        return actual

    # =================================================================
    # VIDEO ↔ TAG — attachments
    # =================================================================

    async def attach_to_video(
        self, video_id: int, tag_ids: List[int]
    ) -> int:
        """
        Attach tags to a video. Idempotent-safe.
        Returns the number of NEW attachments created.

        Caller must increment each attached tag's usage_count by
        the corresponding delta.
        """
        if not tag_ids:
            return 0

        # Validate all tags exist first
        result = await self.session.execute(
            select(Tag.id).where(Tag.id.in_(tag_ids))
        )
        existing = {row[0] for row in result.all()}
        missing = set(tag_ids) - existing
        if missing:
            raise TagNotFoundError(f"Tags not found: {sorted(missing)}")

        stmt = (
            pg_insert(VideoTag)
            .values(
                [{"video_id": video_id, "tag_id": tid} for tid in tag_ids]
            )
            .on_conflict_do_nothing(index_elements=["video_id", "tag_id"])
            .returning(VideoTag.tag_id)
        )
        result = await self.session.execute(stmt)
        return len(result.all())

    async def detach_from_video(
        self, video_id: int, tag_ids: List[int]
    ) -> int:
        """
        Remove tags from a video. Idempotent-safe.
        Returns the number of rows actually removed.

        Caller must decrement each removed tag's usage_count.
        """
        if not tag_ids:
            return 0

        result = await self.session.execute(
            delete(VideoTag)
            .where(
                VideoTag.video_id == video_id,
                VideoTag.tag_id.in_(tag_ids),
            )
            .returning(VideoTag.tag_id)
        )
        return len(result.all())

    async def set_for_video(
        self, video_id: int, tag_ids: List[int]
    ) -> Tuple[List[int], List[int]]:
        """
        Replace the entire tag set for a video.

        Validates all tag IDs first. Then deletes existing rows not
        in the new set, and inserts new rows not in the existing set.

        Returns (added_tag_ids, removed_tag_ids) so the caller can
        adjust usage_counts by the correct deltas.

        Preserves rows that are in BOTH old and new sets — they are
        not touched, so no usage_count change is needed for them.
        """
        if tag_ids:
            result = await self.session.execute(
                select(Tag.id).where(Tag.id.in_(tag_ids))
            )
            existing_tag_ids = {row[0] for row in result.all()}
            missing = set(tag_ids) - existing_tag_ids
            if missing:
                raise TagNotFoundError(f"Tags not found: {sorted(missing)}")

        # What's currently attached?
        result = await self.session.execute(
            select(VideoTag.tag_id).where(VideoTag.video_id == video_id)
        )
        current_ids = {row[0] for row in result.all()}

        new_ids = set(tag_ids)

        to_add = new_ids - current_ids
        to_remove = current_ids - new_ids

        # Delete rows that are going away
        if to_remove:
            await self.session.execute(
                delete(VideoTag).where(
                    VideoTag.video_id == video_id,
                    VideoTag.tag_id.in_(to_remove),
                )
            )

        # Insert rows that are new
        if to_add:
            stmt = (
                pg_insert(VideoTag)
                .values(
                    [{"video_id": video_id, "tag_id": tid} for tid in to_add]
                )
                .on_conflict_do_nothing(index_elements=["video_id", "tag_id"])
            )
            await self.session.execute(stmt)

        return sorted(to_add), sorted(to_remove)

    async def list_tags_for_video(self, video_id: int) -> List[Tag]:
        """Tags attached to a video, alphabetical by name."""
        stmt = (
            select(Tag)
            .join(VideoTag, VideoTag.tag_id == Tag.id)
            .where(VideoTag.video_id == video_id)
            .order_by(Tag.name.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_tag_ids_for_video(self, video_id: int) -> List[int]:
        """Just the tag IDs for a video. For batch "does this video have tag X?" checks."""
        stmt = (
            select(VideoTag.tag_id)
            .where(VideoTag.video_id == video_id)
        )
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def list_video_ids_for_tag(
        self,
        tag_id: int,
        *,
        only_public: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> List[int]:
        """
        Video IDs having a given tag. Public filter joins to `videos`
        to exclude non-public/deleted videos.
        """
        stmt = (
            select(VideoTag.video_id)
            .join(Video, Video.id == VideoTag.video_id)
            .where(VideoTag.tag_id == tag_id)
            .order_by(Video.published_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        if only_public:
            stmt = stmt.where(
                Video.deleted_at.is_(None),
                Video.visibility == "public",
                Video.status == "ready",
            )
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def list_videos_for_tag(
        self,
        tag_id: int,
        *,
        only_public: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Video]:
        """
        Videos with a given tag, newest first.
        Read-only cross-aggregate helper — mutations to videos stay
        in `VideoRepository`.
        """
        stmt = (
            select(Video)
            .join(VideoTag, VideoTag.video_id == Video.id)
            .where(VideoTag.tag_id == tag_id)
            .order_by(Video.published_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        if only_public:
            stmt = stmt.where(
                Video.deleted_at.is_(None),
                Video.visibility == "public",
                Video.status == "ready",
            )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_videos_for_tag(
        self, tag_id: int, *, only_public: bool = True
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(VideoTag)
            .join(Video, Video.id == VideoTag.video_id)
            .where(VideoTag.tag_id == tag_id)
        )
        if only_public:
            stmt = stmt.where(
                Video.deleted_at.is_(None),
                Video.visibility == "public",
                Video.status == "ready",
            )
        return (await self.session.execute(stmt)).scalar_one()