# App/repository/WatchHistoryRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, or_
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    WatchHistoryNotFoundError,
    InvalidWatchPositionError,
    InfrastructureError,
    ValidationError
)
from App.api.databases.MigrateTable import (
    WatchHistory,
    Video,
    Channel,
)


class WatchHistoryRepository:
    """
    Repository for the WatchHistory aggregate.

    Owns: watch_history.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Identity policy:
        One row per (user_id, video_id) — enforced by the unique
        constraint `uq_watch_history_user_video`. The main write
        path is `upsert`, which is idempotent and race-safe.

    Upsert policy:
        `upsert` is the primary entry point — called every time the
        player sends a heartbeat. It:
          1. Inserts the row if it doesn't exist.
          2. Updates `last_position_sec`, `watched_seconds`,
             `last_watched_at` if it does.
          3. Preserves `completed=True` once set (never unsets it
             by a stray heartbeat).

    Counter policy:
        This repo does NOT touch `videos.view_count`. That's a
        worker-side operation driven by `VideoImpression`, not by
        `watch_history`. Watch history is user state; view counts
        are aggregate metrics. Two different tables, two different
        lifecycles.

    Retention policy:
        `delete_older_than` exists for GDPR / "clear my history"
        flows. The service layer decides when to call it.

    Pagination policy:
        History lists are cursor-friendly by `last_watched_at`,
        but this repo exposes offset pagination for consistency
        with the other repos. If you need cursor pagination later,
        add a `list_continue_watching_cursor` method.
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

    async def _get_or_raise(
        self, user_id: int, video_id: int
    ) -> WatchHistory:
        stmt = select(WatchHistory).where(
            WatchHistory.user_id == user_id,
            WatchHistory.video_id == video_id,
        )
        result = await self.session.execute(stmt)
        entry = result.scalar_one_or_none()
        if entry is None:
            raise WatchHistoryNotFoundError(
                f"Watch history for user {user_id} / video {video_id} not found"
            )
        return entry

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # =================================================================
    # WATCH HISTORY — READ
    # =================================================================

    async def get(
        self, user_id: int, video_id: int
    ) -> Optional[WatchHistory]:
        stmt = select(WatchHistory).where(
            WatchHistory.user_id == user_id,
            WatchHistory.video_id == video_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def has_watched(self, user_id: int, video_id: int) -> bool:
        """True if there's any watch history row for this pair."""
        stmt = (
            select(WatchHistory.id)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.video_id == video_id,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def has_completed(self, user_id: int, video_id: int) -> bool:
        """True if the user finished this video."""
        stmt = (
            select(WatchHistory.completed)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.video_id == video_id,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is True

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> List[WatchHistory]:
        """
        Full watch history for a user, newest first.
        Returns junction rows — use `list_videos_for_user` if you
        need the Video objects.
        """
        stmt = (
            select(WatchHistory)
            .where(WatchHistory.user_id == user_id)
            .order_by(WatchHistory.last_watched_at.desc())
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
        Videos the user has watched, newest first.
        Filters out deleted videos by default. Also filters out
        non-ready videos unless you're the owner (which is beyond
        this repo's scope — the service decides).

        This is a read-only cross-aggregate helper. Mutations to
        videos belong to `VideoRepository`.
        """
        stmt = (
            select(Video)
            .join(WatchHistory, WatchHistory.video_id == Video.id)
            .where(WatchHistory.user_id == user_id)
            .order_by(WatchHistory.last_watched_at.desc())
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

    async def list_continue_watching(
        self,
        user_id: int,
        *,
        min_watched_seconds: int = 10,
        limit: int = 10,
        offset: int = 0,
    ) -> List[WatchHistory]:
        """
        Videos the user started but hasn't finished. For the
        "Continue watching" shelf.

        Excludes:
          - completed=True rows
          - rows with watched_seconds below `min_watched_seconds`
            (so a 3-second peek doesn't pollute the shelf)

        Ordered by `last_watched_at` desc — most recently touched
        unfinished video first.
        """
        stmt = (
            select(WatchHistory)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.completed.is_(False),
                WatchHistory.watched_seconds >= min_watched_seconds,
            )
            .order_by(WatchHistory.last_watched_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_completed_for_user(
        self,
        user_id: int,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> List[WatchHistory]:
        """Videos the user has finished watching."""
        stmt = (
            select(WatchHistory)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.completed.is_(True),
            )
            .order_by(WatchHistory.last_watched_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_channel_for_user(
        self,
        user_id: int,
        channel_id: int,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> List[WatchHistory]:
        """
        Watch history for a user limited to one channel.
        For "your history with this creator" views.
        """
        stmt = (
            select(WatchHistory)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.channel_id == channel_id,
            )
            .order_by(WatchHistory.last_watched_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_for_user(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(WatchHistory)
            .where(WatchHistory.user_id == user_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_completed_for_user(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(WatchHistory)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.completed.is_(True),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_for_video(self, video_id: int) -> int:
        """How many distinct users have watched this video."""
        stmt = (
            select(func.count())
            .select_from(WatchHistory)
            .where(WatchHistory.video_id == video_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # WATCH HISTORY — WRITE
    # =================================================================

    async def upsert(
        self,
        *,
        user_id: int,
        video_id: int,
        channel_id: int,
        last_position_sec: int = 0,
        watched_seconds: int = 0,
        completed: bool = False,
    ) -> WatchHistory:
        """
        Insert or update a watch history row. Called on every
        player heartbeat and on pause/seek/finish events.

        Rules:
          - `completed=True` is STICKY — once set, a later upsert
            with `completed=False` will not un-set it. Prevents a
            stray heartbeat after finishing from reviving the row
            into "continue watching."
          - `last_watched_at` is always bumped to now.
          - `watched_seconds` is monotonically non-decreasing —
            a heartbeat with a lower value than the current row is
            IGNORED (protects against out-of-order heartbeats from
            slow clients).

        Raises InvalidWatchPositionError for negative values.
        """
        if last_position_sec < 0 or watched_seconds < 0:
            raise InvalidWatchPositionError(
                "last_position_sec and watched_seconds must be non-negative"
            )

        now = self._now()

        # Fast path: does the row exist?
        existing = await self.get(user_id, video_id)

        if existing is not None:
            # Monotonic watched_seconds: never go backwards
            new_watched = max(existing.watched_seconds, watched_seconds)
            # Sticky completed: never un-complete
            new_completed = existing.completed or completed

            existing.last_position_sec = last_position_sec
            existing.watched_seconds = new_watched
            existing.completed = new_completed
            existing.last_watched_at = now
            # channel_id doesn't change for a given video_id

            await self._safe_flush()
            return existing

        # Race path: insert; if another request won, retry as update
        stmt = (
            pg_insert(WatchHistory)
            .values(
                user_id=user_id,
                video_id=video_id,
                channel_id=channel_id,
                last_position_sec=last_position_sec,
                watched_seconds=watched_seconds,
                completed=completed,
                last_watched_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "video_id"]
            )
            .returning(WatchHistory.id)
        )
        result = await self.session.execute(stmt)
        inserted_id = result.scalar_one_or_none()

        if inserted_id is None:
            # Lost the race — someone else inserted. Re-fetch and update.
            return await self.upsert(
                user_id=user_id,
                video_id=video_id,
                channel_id=channel_id,
                last_position_sec=last_position_sec,
                watched_seconds=watched_seconds,
                completed=completed,
            )

        # Re-fetch the inserted row
        row = await self.get(user_id, video_id)
        if row is None:
            raise InfrastructureError(
                f"Watch history row vanished after insert"
            )
        return row

    async def mark_completed(
        self, user_id: int, video_id: int, *, watched_seconds: Optional[int] = None
    ) -> WatchHistory:
        """
        Explicitly mark a video as completed. Used when the player
        fires an "ended" event or the user clicks "mark as watched."

        Optionally bumps `watched_seconds` if provided (and higher
        than the current value).
        """
        entry = await self._get_or_raise(user_id, video_id)
        entry.completed = True
        if watched_seconds is not None:
            if watched_seconds < 0:
                raise InvalidWatchPositionError(
                    "watched_seconds must be non-negative"
                )
            entry.watched_seconds = max(entry.watched_seconds, watched_seconds)
        entry.last_watched_at = self._now()
        await self._safe_flush()
        return entry

    async def clear_position(
        self, user_id: int, video_id: int
    ) -> WatchHistory:
        """
        Reset the playback position to 0 without deleting the row.
        Used for "restart from beginning" in the UI.
        """
        entry = await self._get_or_raise(user_id, video_id)
        entry.last_position_sec = 0
        entry.completed = False
        entry.last_watched_at = self._now()
        await self._safe_flush()
        return entry

    async def delete(
        self, user_id: int, video_id: int
    ) -> None:
        """Remove a single entry from a user's history."""
        result = await self.session.execute(
            delete(WatchHistory)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.video_id == video_id,
            )
            .returning(WatchHistory.id)
        )
        if result.scalar_one_or_none() is None:
            raise WatchHistoryNotFoundError(
                f"Watch history for user {user_id} / video {video_id} not found"
            )

    async def clear_for_user(self, user_id: int) -> int:
        """
        Delete every watch-history entry for a user.
        Returns the number of rows removed.

        This is the "clear my watch history" feature.
        """
        result = await self.session.execute(
            delete(WatchHistory)
            .where(WatchHistory.user_id == user_id)
            .returning(WatchHistory.id)
        )
        return len(result.all())

    async def clear_for_user_by_channel(
        self, user_id: int, channel_id: int
    ) -> int:
        """
        Delete watch history for a user scoped to a single channel.
        For "stop showing me this creator in my feed" preferences.
        """
        result = await self.session.execute(
            delete(WatchHistory)
            .where(
                WatchHistory.user_id == user_id,
                WatchHistory.channel_id == channel_id,
            )
            .returning(WatchHistory.id)
        )
        return len(result.all())

    async def delete_older_than(self, days: int) -> int:
        """
        Retention helper. Deletes every watch-history row older than
        `days`. Returns the number of rows removed.
        """
        if days <= 0:
            raise ValidationError("days must be positive")

        from datetime import timedelta
        cutoff = self._now() - timedelta(days=days)
        result = await self.session.execute(
            delete(WatchHistory)
            .where(WatchHistory.last_watched_at < cutoff)
            .returning(WatchHistory.id)
        )
        return len(result.all())