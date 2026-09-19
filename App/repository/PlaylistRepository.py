# App/repository/PlaylistRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    PlaylistNotFoundError,
    PlaylistAlreadyDeletedError,
    DuplicatePlaylistVideoError,
    PlaylistVideoNotFoundError,
    PlaylistPositionConflictError,
    InfrastructureError,
    ValidationError
)
from App.api.databases.MigrateTable import (
    Playlist,
    PlaylistVideo,
    Video,
)


class PlaylistRepository:
    """
    Repository for the Playlist aggregate.

    Owns: playlists, playlist_videos.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Deletion policy:
        Playlists are soft-deleted via `deleted_at`. `get_*` methods
        filter on `deleted_at IS NULL` by default.

    Ordering policy:
        `playlist_videos.position` is a contiguous 0-based index.
        Two invariants are maintained by the methods in this repo:
          1. No two videos in a playlist share the same position
             (enforced by `uq_playlist_videos_position`).
          2. After any add/remove/reorder, positions are renumbered
             to be contiguous from 0 to N-1.

        The renumbering happens in Python (the affected range is
        bounded and small) inside the same transaction. There is no
        DB trigger — the service must call these methods, not raw SQL.

    Counter policy:
        `playlists.video_count` is denormalized. This repo increments
        and decrements it in the same flush as the underlying insert/
        delete. Two operations are idempotent-safe:
          - `add_video` — no-op if the video is already present
          - `remove_video` — no-op if the video is not present
        Both only touch the counter when the underlying row actually
        changed.
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

    async def _get_or_raise(
        self, playlist_id: int, *, include_deleted: bool = False
    ) -> Playlist:
        stmt = select(Playlist).where(Playlist.id == playlist_id)
        if not include_deleted:
            stmt = stmt.where(Playlist.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        playlist = result.scalar_one_or_none()
        if playlist is None:
            raise PlaylistNotFoundError(f"Playlist {playlist_id} not found")
        return playlist

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def _get_entries(self, playlist_id: int) -> List[PlaylistVideo]:
        """All PlaylistVideo rows for a playlist, ordered by position."""
        stmt = (
            select(PlaylistVideo)
            .where(PlaylistVideo.playlist_id == playlist_id)
            .order_by(PlaylistVideo.position.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _renumber(self, playlist_id: int) -> None:
        """
        Renumber all PlaylistVideo rows in a playlist to be contiguous
        from 0 to N-1, preserving their current relative order.

        Called after any mutation that changes the set of entries or
        their positions. In-place, in the current transaction.
        """
        entries = await self._get_entries(playlist_id)
        for idx, entry in enumerate(entries):
            if entry.position != idx:
                entry.position = idx
        await self._safe_flush()

    # =================================================================
    # PLAYLIST — READ
    # =================================================================

    async def get_by_id(
        self, playlist_id: int, *, include_deleted: bool = False
    ) -> Optional[Playlist]:
        stmt = select(Playlist).where(Playlist.id == playlist_id)
        if not include_deleted:
            stmt = stmt.where(Playlist.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_uuid(
        self, playlist_uuid: str, *, include_deleted: bool = False
    ) -> Optional[Playlist]:
        stmt = select(Playlist).where(Playlist.playlist_uuid == playlist_uuid)
        if not include_deleted:
            stmt = stmt.where(Playlist.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_entries(self, playlist_id: int) -> Optional[Playlist]:
        """
        Eager-load the ordered entries (PlaylistVideo rows) and their
        videos. For the playlist detail page.
        """
        stmt = (
            select(Playlist)
            .where(Playlist.id == playlist_id, Playlist.deleted_at.is_(None))
            .options(
                selectinload(Playlist.videos)
                .joinedload(PlaylistVideo.video)
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_owner(
        self,
        owner_id: int,
        *,
        visibility: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Playlist]:
        """A user's playlists, newest first. Optionally filtered by visibility."""
        stmt = (
            select(Playlist)
            .where(
                Playlist.owner_id == owner_id,
                Playlist.deleted_at.is_(None),
            )
            .order_by(Playlist.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if visibility is not None:
            stmt = stmt.where(Playlist.visibility == visibility)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_public(
        self, *, limit: int = 20, offset: int = 0
    ) -> List[Playlist]:
        """Public playlists, newest first. For discovery pages."""
        stmt = (
            select(Playlist)
            .where(
                Playlist.visibility == "public",
                Playlist.deleted_at.is_(None),
            )
            .order_by(Playlist.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_owner(
        self,
        owner_id: int,
        *,
        visibility: Optional[str] = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Playlist)
            .where(
                Playlist.owner_id == owner_id,
                Playlist.deleted_at.is_(None),
            )
        )
        if visibility is not None:
            stmt = stmt.where(Playlist.visibility == visibility)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_public(self) -> int:
        stmt = (
            select(func.count())
            .select_from(Playlist)
            .where(
                Playlist.visibility == "public",
                Playlist.deleted_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # PLAYLIST — WRITE
    # =================================================================

    async def create(
        self,
        *,
        owner_id: int,
        title: str,
        description: Optional[str] = None,
        visibility: str = "private",
        thumbnail_key: Optional[str] = None,
    ) -> Playlist:
        if visibility not in ("private", "unlisted", "public"):
            raise InfrastructureError(f"Invalid visibility: {visibility!r}")


        playlist = Playlist(
            owner_id=owner_id,
            title=title,
            description=description,
            visibility=visibility,
            thumbnail_key=thumbnail_key,
            video_count=0,
        )
        self.session.add(playlist)
        await self._safe_flush()
        await self.session.refresh(playlist)
        return playlist

    async def update(
        self,
        playlist_id: int,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        visibility: Optional[str] = None,
        thumbnail_key: Optional[str] = None,
    ) -> Playlist:
        playlist = await self._get_or_raise(playlist_id)
        if title is not None:
            playlist.title = title
        if description is not None:
            playlist.description = description
        if visibility is not None:
            if visibility not in ("private", "unlisted", "public"):
                raise InfrastructureError(
                    f"Invalid visibility: {visibility!r}"
                )
            playlist.visibility = visibility
        if thumbnail_key is not None:
            playlist.thumbnail_key = thumbnail_key
        await self._safe_flush()
        return playlist

    async def soft_delete(self, playlist_id: int) -> None:
        playlist = await self._get_or_raise(playlist_id, include_deleted=True)
        if playlist.deleted_at is not None:
            raise PlaylistAlreadyDeletedError(
                f"Playlist {playlist_id} already deleted"
            )
        playlist.deleted_at = self._now()
        await self._safe_flush()

    # =================================================================
    # PLAYLIST — ENTRIES (add / remove / reorder)
    # =================================================================

    async def add_video(
        self, playlist_id: int, video_id: int
    ) -> PlaylistVideo:
        """
        Append a video to the end of a playlist. Idempotent-safe:
        raises DuplicatePlaylistVideoError if the video is already in
        the playlist.

        Bumps `playlists.video_count` in the same flush.
        """
        playlist = await self._get_or_raise(playlist_id)

        # Check for duplicate
        existing = await self.get_entry(playlist_id, video_id)
        if existing is not None:
            raise DuplicatePlaylistVideoError(
                f"Video {video_id} already in playlist {playlist_id}"
            )

        # Find next position = current max + 1 (or 0 if empty)
        stmt = select(func.coalesce(func.max(PlaylistVideo.position), -1)).where(
            PlaylistVideo.playlist_id == playlist_id
        )
        max_position = (await self.session.execute(stmt)).scalar_one()
        next_position = max_position + 1

        entry = PlaylistVideo(
            playlist_id=playlist_id,
            video_id=video_id,
            position=next_position,
        )
        self.session.add(entry)

        playlist.video_count = playlist.video_count + 1

        await self._safe_flush()
        await self.session.refresh(entry)
        return entry

    async def remove_video(self, playlist_id: int, video_id: int) -> None:
        """
        Remove a video from a playlist. Renumbers positions so they
        stay contiguous. Decrements `playlists.video_count`.

        Raises PlaylistVideoNotFoundError if the video isn't present.
        """
        playlist = await self._get_or_raise(playlist_id)

        result = await self.session.execute(
            delete(PlaylistVideo)
            .where(
                PlaylistVideo.playlist_id == playlist_id,
                PlaylistVideo.video_id == video_id,
            )
            .returning(PlaylistVideo.id)
        )
        if result.scalar_one_or_none() is None:
            raise PlaylistVideoNotFoundError(
                f"Video {video_id} not in playlist {playlist_id}"
            )

        playlist.video_count = max(0, playlist.video_count - 1)

        await self._renumber(playlist_id)
        await self._safe_flush()

    async def get_entry(
        self, playlist_id: int, video_id: int
    ) -> Optional[PlaylistVideo]:
        stmt = select(PlaylistVideo).where(
            PlaylistVideo.playlist_id == playlist_id,
            PlaylistVideo.video_id == video_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def has_video(self, playlist_id: int, video_id: int) -> bool:
        stmt = select(PlaylistVideo.id).where(
            PlaylistVideo.playlist_id == playlist_id,
            PlaylistVideo.video_id == video_id,
        ).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def reorder_video(
        self, playlist_id: int, video_id: int, new_position: int
    ) -> None:
        """
        Move a video to a new position in the playlist.

        Other videos shift to fill the gap. Positions stay contiguous
        and 0-based after the operation.

        Raises:
          - PlaylistVideoNotFoundError if the video isn't in the playlist
          - ValueError if new_position is out of [0, N-1]
        """
        await self._get_or_raise(playlist_id)

        entries = await self._get_entries(playlist_id)
        n = len(entries)

        if new_position < 0 or new_position >= n:
            raise ValidationError(
                f"new_position {new_position} out of range [0, {n - 1}]"
            )

        # Find the moving entry and its current index
        current_index = next(
            (i for i, e in enumerate(entries) if e.video_id == video_id),
            None,
        )
        if current_index is None:
            raise PlaylistVideoNotFoundError(
                f"Video {video_id} not in playlist {playlist_id}"
            )

        if current_index == new_position:
            return  # no-op

        # Reorder the list in memory
        moving = entries.pop(current_index)
        entries.insert(new_position, moving)

        # Renumber
        for idx, entry in enumerate(entries):
            entry.position = idx

        await self._safe_flush()

    async def list_entries(
        self,
        playlist_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[PlaylistVideo]:
        """
        Paginated PlaylistVideo rows for a playlist, ordered by position.
        """
        stmt = (
            select(PlaylistVideo)
            .where(PlaylistVideo.playlist_id == playlist_id)
            .order_by(PlaylistVideo.position.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_videos(
        self,
        playlist_id: int,
        *,
        only_available: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Video]:
        """
        Videos in a playlist, ordered by position.

        If `only_available=True` (default), excludes deleted or
        non-public videos — used for the public playlist view.
        Set to False for the owner's own view (which sees everything).
        """
        stmt = (
            select(Video)
            .join(PlaylistVideo, PlaylistVideo.video_id == Video.id)
            .where(PlaylistVideo.playlist_id == playlist_id)
            .order_by(PlaylistVideo.position.asc())
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

    async def count_entries(self, playlist_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(PlaylistVideo)
            .where(PlaylistVideo.playlist_id == playlist_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # PLAYLIST — COUNTERS (kept in sync by add_video / remove_video)
    # =================================================================

    async def recompute_video_count(self, playlist_id: int) -> int:
        """
        Recompute `playlists.video_count` from the actual entry rows.
        Used by reconciliation jobs. Returns the fresh count.
        """
        actual = await self.count_entries(playlist_id)
        await self.session.execute(
            update(Playlist)
            .where(Playlist.id == playlist_id)
            .values(video_count=actual)
        )
        return actual