# App/repository/ImpressionRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, or_, func
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from datetime import datetime, timedelta, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    ImpressionNotFoundError,
    InvalidImpressionError,
    InfrastructureError,
    ValidationError
    
)
from App.api.databases.MigrateTable import (
    VideoImpression,
    Video,
)


class ImpressionRepository:
    """
    Repository for the VideoImpression aggregate.

    Owns: video_impressions.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Identity policy:
        An impression is identified by EITHER a `user_id` (logged-in
        viewer) OR a `session_id` (anonymous viewer), never both and
        never neither. The DB CHECK constraint enforces this; this
        repo also validates in app code for clearer errors.

    Counting policy:
        An impression has a boolean `counted` flag. The worker that
        aggregates views flips `counted=True` once it has incremented
        `videos.view_count`. This repo exposes:
          - `list_uncounted` — what the worker pulls
          - `mark_counted` — how the worker commits progress
        The repository does NOT increment `videos.view_count` itself;
        that belongs to `VideoRepository.increment_view_count`.

    Retention policy:
        Impressions are append-only and grow fast. `delete_older_than`
        exists for retention jobs (e.g. purge after 90 days). The
        service layer decides the retention window.
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

    async def _get_or_raise(self, impression_id: int) -> VideoImpression:
        stmt = select(VideoImpression).where(VideoImpression.id == impression_id)
        result = await self.session.execute(stmt)
        impression = result.scalar_one_or_none()
        if impression is None:
            raise ImpressionNotFoundError(
                f"Impression {impression_id} not found"
            )
        return impression

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _validate_identity(
        user_id: Optional[int], session_id: Optional[str]
    ) -> None:
        """
        Exactly one of user_id / session_id must be provided.
        Enforced by DB CHECK `ck_video_impressions_user_or_session`; we
        validate in app code for a clearer error.
        """
        if user_id is None and session_id is None:
            raise InvalidImpressionError(
                "Impression requires either user_id or session_id"
            )
        if user_id is not None and session_id is not None:
            raise InvalidImpressionError(
                "Impression must have user_id OR session_id, not both"
            )

    # =================================================================
    # IMPRESSION — READ
    # =================================================================

    async def get_by_id(self, impression_id: int) -> Optional[VideoImpression]:
        stmt = select(VideoImpression).where(VideoImpression.id == impression_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_video(self, impression_id: int) -> Optional[VideoImpression]:
        """Eager-load the video for worker / admin views."""
        stmt = (
            select(VideoImpression)
            .where(VideoImpression.id == impression_id)
            .options(joinedload(VideoImpression.video))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_video(
        self,
        video_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[VideoImpression]:
        """All impressions for a video, newest first."""
        stmt = (
            select(VideoImpression)
            .where(VideoImpression.video_id == video_id)
            .order_by(VideoImpression.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[VideoImpression]:
        """All impressions recorded for a logged-in user."""
        stmt = (
            select(VideoImpression)
            .where(VideoImpression.user_id == user_id)
            .order_by(VideoImpression.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_session(
        self,
        session_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[VideoImpression]:
        """All impressions recorded for an anonymous session."""
        stmt = (
            select(VideoImpression)
            .where(VideoImpression.session_id == session_id)
            .order_by(VideoImpression.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_for_video(self, video_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(VideoImpression)
            .where(VideoImpression.video_id == video_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_for_user(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(VideoImpression)
            .where(VideoImpression.user_id == user_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_for_session(self, session_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(VideoImpression)
            .where(VideoImpression.session_id == session_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # IMPRESSION — WORKER QUEUE
    # =================================================================

    async def list_uncounted(
        self, *, limit: int = 500
    ) -> List[VideoImpression]:
        """
        Impressions the view-count worker has not yet processed.
        Ordered oldest-first so the worker drains the queue in
        chronological order and does not starve old rows.
        """
        stmt = (
            select(VideoImpression)
            .where(VideoImpression.counted.is_(False))
            .order_by(VideoImpression.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_uncounted(self) -> int:
        stmt = (
            select(func.count())
            .select_from(VideoImpression)
            .where(VideoImpression.counted.is_(False))
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # IMPRESSION — WRITE
    # =================================================================

    async def create(
        self,
        *,
        video_id: int,
        user_id: Optional[int] = None,
        session_id: Optional[str] = None,
        watched_seconds: int = 0,
    ) -> VideoImpression:
        """
        Record a new impression. Exactly one of user_id / session_id
        must be provided. `counted` always starts False — the worker
        flips it after updating the video's view_count.
        """
        self._validate_identity(user_id, session_id)

        impression = VideoImpression(
            video_id=video_id,
            user_id=user_id,
            session_id=session_id,
            watched_seconds=watched_seconds,
            counted=False,
        )
        self.session.add(impression)
        await self._safe_flush()
        await self.session.refresh(impression)
        return impression

    async def update_watched_seconds(
        self, impression_id: int, watched_seconds: int
    ) -> VideoImpression:
        """
        Update the watched-seconds total for an impression. Used when
        a player sends periodic heartbeats during playback.
        """
        impression = await self._get_or_raise(impression_id)
        impression.watched_seconds = watched_seconds
        await self._safe_flush()
        return impression

    async def mark_counted(self, impression_id: int) -> VideoImpression:
        """
        Flip `counted=True`. Called by the worker AFTER it has
        successfully incremented the video's view_count.

        This is not idempotent-safe by itself — if the worker crashes
        between increment_view_count and mark_counted, the impression
        stays uncounted and will be counted again on the next pass.
        To make it safe, the caller must increment and mark inside
        the SAME transaction.
        """
        impression = await self._get_or_raise(impression_id)
        if impression.counted:
            # Already counted — no-op, return current state.
            return impression
        impression.counted = True
        await self._safe_flush()
        return impression

    async def mark_counted_bulk(self, impression_ids: List[int]) -> int:
        """
        Bulk version of mark_counted. Returns the number of rows
        actually updated. Idempotent — already-counted rows are
        skipped by the WHERE clause.
        """
        if not impression_ids:
            return 0
        result = await self.session.execute(
            update(VideoImpression)
            .where(
                VideoImpression.id.in_(impression_ids),
                VideoImpression.counted.is_(False),
            )
            .values(counted=True)
            .returning(VideoImpression.id)
        )
        return len(result.all())

    async def delete(self, impression_id: int) -> None:
        """Hard-delete an impression."""
        result = await self.session.execute(
            delete(VideoImpression)
            .where(VideoImpression.id == impression_id)
            .returning(VideoImpression.id)
        )
        if result.scalar_one_or_none() is None:
            raise ImpressionNotFoundError(
                f"Impression {impression_id} not found"
            )

    async def delete_older_than(self, days: int) -> int:
        """
        Retention helper. Deletes every impression older than `days`.
        Returns the number of rows removed. Only deletes impressions
        that have already been counted — uncounted ones stay for the
        worker.
        """
        if days <= 0:
            raise ValidationError("days must be positive")
        cutoff = self._now() - timedelta(days=days)
        result = await self.session.execute(
            delete(VideoImpression)
            .where(
                VideoImpression.created_at < cutoff,
                VideoImpression.counted.is_(True),
            )
            .returning(VideoImpression.id)
        )
        return len(result.all())

    async def delete_older_than_including_uncounted(self, days: int) -> int:
        """
        Aggressive retention. Deletes every impression older than
        `days`, counted or not. Use only when you know the worker has
        already drained the queue.
        """
        if days <= 0:
            raise ValidationError("days must be positive")

        cutoff = self._now() - timedelta(days=days)
        result = await self.session.execute(
            delete(VideoImpression)
            .where(VideoImpression.created_at < cutoff)
            .returning(VideoImpression.id)
        )
        return len(result.all())

    # =================================================================
    # DE-DUPLICATION HELPERS
    # =================================================================

    async def has_recent_impression_for_user(
        self,
        *,
        video_id: int,
        user_id: int,
        within_seconds: int,
    ) -> bool:
        """
        True if this user has an impression on this video created
        within the last `within_seconds`. Used to debounce view counts
        (e.g. "same user watching in 30 min = 1 view").
        """
        cutoff = self._now() - timedelta(seconds=within_seconds)
        stmt = select(VideoImpression.id).where(
            VideoImpression.video_id == video_id,
            VideoImpression.user_id == user_id,
            VideoImpression.created_at >= cutoff,
        ).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def has_recent_impression_for_session(
        self,
        *,
        video_id: int,
        session_id: str,
        within_seconds: int,
    ) -> bool:
        cutoff = self._now() - timedelta(seconds=within_seconds)
        stmt = select(VideoImpression.id).where(
            VideoImpression.video_id == video_id,
            VideoImpression.session_id == session_id,
            VideoImpression.created_at >= cutoff,
        ).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # =================================================================
    # AGGREGATION (read-only, for analytics endpoints)
    # =================================================================

    async def count_by_day_for_video(
        self,
        video_id: int,
        *,
        since: Optional[datetime] = None,
    ) -> List[tuple]:
        """
        Returns [(date, impression_count), ...] grouped by calendar day.

        Note: this aggregates from `created_at`. It is a coarse metric;
        for high-traffic analytics, use a materialized daily table.
        """
        stmt = (
            select(
                func.date_trunc("day", VideoImpression.created_at).label("day"),
                func.count().label("count"),
            )
            .where(VideoImpression.video_id == video_id)
            .group_by("day")
            .order_by("day")
        )
        if since is not None:
            stmt = stmt.where(VideoImpression.created_at >= since)
        result = await self.session.execute(stmt)
        return [(row.day, row.count) for row in result.all()]

    async def count_unique_viewers_for_video(self, video_id: int) -> int:
        """
        Distinct users + distinct sessions that have an impression on
        this video. Each distinct anonymous session counts as one
        "viewer" for this metric.
        """
        # Distinct user_ids
        user_stmt = (
            select(func.count(func.distinct(VideoImpression.user_id)))
            .where(
                VideoImpression.video_id == video_id,
                VideoImpression.user_id.isnot(None),
            )
        )
        users = (await self.session.execute(user_stmt)).scalar_one()

        # Distinct session_ids
        session_stmt = (
            select(func.count(func.distinct(VideoImpression.session_id)))
            .where(
                VideoImpression.video_id == video_id,
                VideoImpression.session_id.isnot(None),
            )
        )
        sessions = (await self.session.execute(session_stmt)).scalar_one()

        return int(users) + int(sessions)