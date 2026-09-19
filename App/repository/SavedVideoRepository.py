# App/repository/SavedVideoRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    VideoAlreadySavedError,
    VideoNotSavedError,
    InfrastructureError,
)
from App.api.databases.MigrateTable import (
    SavedVideo,
    Video,
)


class SavedVideoRepository:
    """
    Repository for the SavedVideo aggregate.

    Owns: saved_videos.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Semantics:
        "Save" is a user's private bookmark — akin to YouTube's
        "Save to Watch Later" or "Save to playlist". It is NOT the
        same as a like (which is public and counted) or a share
        (which grants access).

    Counter policy:
        `videos.save_count` is denormalized. The service that calls
        `save` or `unsave` must ALSO call
        `VideoRepository.increment_save_count(+1 / -1)` in the same
        transaction. This repo does NOT touch the video's counter —
        that's a cross-aggregate mutation and belongs in the service.

        Why: if the repo incremented `video.save_count` directly,
        we'd have two repositories mutating the same table
        (`videos`), which breaks the aggregate boundary.

    Idempotency:
        `save` raises VideoAlreadySavedError if the row exists.
        `unsave` raises VideoNotSavedError if it doesn't. Callers
        that want "toggle" behavior should check `is_saved` first.
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
    # SAVED VIDEO — READ
    # =================================================================

    async def is_saved(self, user_id: int, video_id: int) -> bool:
        """True if the user has this video in their saved list."""
        stmt = (
            select(SavedVideo.video_id)
            .where(
                SavedVideo.user_id == user_id,
                SavedVideo.video_id == video_id,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get(
        self, user_id: int, video_id: int
    ) -> Optional[SavedVideo]:
        """Fetch a single saved-video row, if it exists."""
        stmt = select(SavedVideo).where(
            SavedVideo.user_id == user_id,
            SavedVideo.video_id == video_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> List[SavedVideo]:
        """
        SavedVideo rows for a user, newest first.
        Returns the junction rows (not the videos) — caller can eager
        load the video if needed, or use `list_videos_for_user`.
        """
        stmt = (
            select(SavedVideo)
            .where(SavedVideo.user_id == user_id)
            .order_by(SavedVideo.saved_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_videos_for_user(
        self,
        user_id: int,
        *,
        only_available: bool = True,
        limit: int = 30,
        offset: int = 0,
    ) -> List[Video]:
        """
        Videos the user has saved, newest save first.

        If `only_available=True` (default), filters out deleted or
        non-ready videos — used for the public "My Saves" feed.
        Set to False for the user's own admin view.
        """
        stmt = (
            select(Video)
            .join(SavedVideo, SavedVideo.video_id == Video.id)
            .where(SavedVideo.user_id == user_id)
            .order_by(SavedVideo.saved_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if only_available:
            stmt = stmt.where(
                Video.deleted_at.is_(None),
                Video.status == "ready",
            )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_for_user(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(SavedVideo)
            .where(SavedVideo.user_id == user_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_for_video(self, video_id: int) -> int:
        """How many users have saved this video."""
        stmt = (
            select(func.count())
            .select_from(SavedVideo)
            .where(SavedVideo.video_id == video_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_savers(
        self,
        video_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> List[SavedVideo]:
        """
        Who saved this video, newest first.
        For the owner's "who bookmarked my video" analytics view.
        """
        stmt = (
            select(SavedVideo)
            .where(SavedVideo.video_id == video_id)
            .order_by(SavedVideo.saved_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # =================================================================
    # SAVED VIDEO — WRITE
    # =================================================================

    async def save(self, user_id: int, video_id: int) -> SavedVideo:
        """
        Save a video for a user.

        Raises VideoAlreadySavedError if the row already exists.
        Caller must ALSO call VideoRepository.increment_save_count(+1)
        in the same transaction.
        """
        if await self.is_saved(user_id, video_id):
            raise VideoAlreadySavedError(
                f"Video {video_id} already saved by user {user_id}"
            )

        row = SavedVideo(user_id=user_id, video_id=video_id)
        self.session.add(row)
        await self._safe_flush()
        await self.session.refresh(row)
        return row

    async def unsave(self, user_id: int, video_id: int) -> None:
        """
        Remove a video from a user's saved list.

        Raises VideoNotSavedError if the row doesn't exist.
        Caller must ALSO call VideoRepository.increment_save_count(-1)
        in the same transaction.
        """
        result = await self.session.execute(
            delete(SavedVideo)
            .where(
                SavedVideo.user_id == user_id,
                SavedVideo.video_id == video_id,
            )
            .returning(SavedVideo.video_id)
        )
        if result.scalar_one_or_none() is None:
            raise VideoNotSavedError(
                f"Video {video_id} not saved by user {user_id}"
            )

    async def toggle(self, user_id: int, video_id: int) -> bool:
        """
        Flip the save state for a user/video pair.

        Returns:
          - True if the video is NOW saved (i.e. was not saved before)
          - False if the video is NOW unsaved (i.e. was saved before)

        Caller must increment/decrement video.save_count accordingly.
        This is a convenience method for the UI toggle — the caller
        still owns the counter update.
        """
        if await self.is_saved(user_id, video_id):
            await self.unsave(user_id, video_id)
            return False
        else:
            await self.save(user_id, video_id)
            return True

    async def save_bulk(
        self, user_id: int, video_ids: List[int]
    ) -> int:
        """
        Save many videos at once. Idempotent-safe: existing rows are
        skipped (ON CONFLICT DO NOTHING). Returns the number of NEW
        saves actually created.

        Caller must increment video.save_count for each newly saved
        video — or use the returned count and update in bulk via
        VideoRepository.
        """
        if not video_ids:
            return 0

        # Use ON CONFLICT DO NOTHING for idempotency.
        stmt = (
            pg_insert(SavedVideo)
            .values([{"user_id": user_id, "video_id": vid} for vid in video_ids])
            .on_conflict_do_nothing(index_elements=["user_id", "video_id"])
            .returning(SavedVideo.video_id)
        )
        result = await self.session.execute(stmt)
        return len(result.all())

    async def unsave_bulk(
        self, user_id: int, video_ids: List[int]
    ) -> int:
        """
        Unsave many videos at once. Idempotent-safe: missing rows are
        silently skipped. Returns the number of rows actually removed.

        Caller must decrement video.save_count for each removed save.
        """
        if not video_ids:
            return 0

        result = await self.session.execute(
            delete(SavedVideo)
            .where(
                SavedVideo.user_id == user_id,
                SavedVideo.video_id.in_(video_ids),
            )
            .returning(SavedVideo.video_id)
        )
        return len(result.all())

    async def clear_for_user(self, user_id: int) -> int:
        """
        Remove ALL saved videos for a user.
        Returns the number of rows deleted.

        Caller must decrement video.save_count for each affected video
        if the counter matters — or accept drift and reconcile via a
        maintenance job.
        """
        result = await self.session.execute(
            delete(SavedVideo)
            .where(SavedVideo.user_id == user_id)
            .returning(SavedVideo.video_id)
        )
        return len(result.all())