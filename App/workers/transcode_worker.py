# App/workers/transcode_worker.py
"""
Standalone transcode worker.

Run:
    python -m App.workers.transcode_worker

Deploy as a separate container with the same image as the API.
"""
import asyncio
import shutil
import signal
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import database
from App.core.LoggingInit import setup_core_logging, get_core_logger
from App.core import ffmpeg_helper
from App.services.transcode_service import TranscodeService
from App.api.databases.MigrateTable import Video

logger = get_core_logger("worker.transcode")


POLL_INTERVAL_SEC = 15
BATCH_SIZE = 1

# Anything stuck in 'processing' longer than this is assumed dead.
# Must exceed the longest expected transcode time with margin.
MAX_TRANSCODE_AGE = timedelta(hours=2)


class TranscodeWorker:
    def __init__(self) -> None:
        self._running = True
        self._current_task: Optional[asyncio.Task] = None

    def _handle_signal(self, *_: object) -> None:
        logger.info("Shutdown signal received")
        self._running = False

    async def run(self) -> None:
        # Signal handlers must be installed in the main thread.
        # add_signal_handler is the asyncio-correct way to do this.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                # Windows fallback
                signal.signal(sig, self._handle_signal)

        self._cleanup_stale_temp_dirs()

        if not await ffmpeg_helper.check_ffmpeg_available(use_cache=False):
            logger.critical("ffmpeg/ffprobe not on PATH — worker cannot start")
            sys.exit(1)

        await database.connect()
        logger.info("Transcode worker started")

        # Recover zombies BEFORE entering the loop.
        await self._recover_zombies()

        while self._running:
            try:
                processed = await self._drain_once()
                if processed == 0:
                    await self._sleep(POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                logger.info("Worker loop cancelled")
                break
            except Exception:
                logger.exception("Worker iteration failed")
                await self._sleep(POLL_INTERVAL_SEC)

        # Give in-flight transcode a chance to be cancelled cleanly.
        if self._current_task and not self._current_task.done():
            logger.info("Cancelling in-flight transcode...")
            self._current_task.cancel()
            try:
                await self._current_task
            except (asyncio.CancelledError, Exception):
                pass

        await database.disconnect()
        logger.info("Transcode worker stopped")

    async def _sleep(self, seconds: float) -> None:
        """Sleep, waking early if shutdown was requested."""
        # Poll the running flag every 0.5s so Ctrl+C takes effect quickly
        # without needing a separate event object.
        elapsed = 0.0
        step = 0.5
        while elapsed < seconds and self._running:
            await asyncio.sleep(step)
            elapsed += step
    # ---------- Zombie recovery ----------

    async def _recover_zombies(self) -> None:
        """
        Requeue rows that have been 'processing' longer than MAX_TRANSCODE_AGE.
        A crashed worker can leave a row in 'processing' forever — this
        sweep picks those up again on startup.

        Requires columns: processing_started_at, failure_reason (see migration).
        """
        cutoff = datetime.now(timezone.utc) - MAX_TRANSCODE_AGE
        async with database.session() as session:
            stmt = (
                update(Video)
                .where(
                    Video.status == "processing",
                    Video.deleted_at.is_(None),
                    Video.processing_started_at.is_not(None),
                    Video.processing_started_at < cutoff,
                )
                .values(
                    processing_started_at=func.now(),
                    updated_at=func.now(),
                    failure_reason="requeued: worker crash recovery",
                )
                .returning(Video.id)
            )
            result = await session.execute(stmt)
            ids = [row[0] for row in result.all()]
            await session.commit()

        if ids:
            logger.warning(
                f"Recovered {len(ids)} zombie videos "
                f"(stuck > {MAX_TRANSCODE_AGE}): {ids}"
            )
        else:
            logger.info("No zombie videos to recover")

    # ---------- Cleanup ----------

    @staticmethod
    def _cleanup_stale_temp_dirs() -> None:
        tmp = Path(tempfile.gettempdir())
        for path in tmp.glob("transcode_*"):
            if path.is_dir():
                try:
                    shutil.rmtree(path)
                    logger.info(f"Cleaned stale temp dir: {path}")
                except Exception:
                    logger.exception(f"Failed to clean {path}")

    # ---------- Main loop ----------

    async def _drain_once(self) -> int:
        """
        Claim ONE video and transcode it.

        Claim strategy:
            - SELECT ... FOR UPDATE SKIP LOCKED (pick a candidate)
            - UPDATE the row to mark it as claimed with processing_started_at=now()
            - COMMIT (releases lock; claim marker prevents double-processing)
            - Run transcode in a FRESH session, unlocked
        """
        video_id = await self._claim_one()
        if video_id is None:
            return 0

        # Run in a fresh session — long work must NOT hold the claim session.
        task = asyncio.create_task(self._transcode_in_fresh_session(video_id))
        self._current_task = task
        try:
            await task
        except asyncio.CancelledError:
            logger.warning(f"Transcode cancelled for video {video_id}")
            raise
        except Exception:
            logger.exception(f"Transcode failed for video {video_id}")
        finally:
            self._current_task = None

        return 1

    async def _claim_one(self) -> Optional[int]:
        """
        Atomically claim one 'processing' video.

        Uses SKIP LOCKED to avoid two workers picking the same row.
        After the claim, processing_started_at is bumped so the recovery
        sweep won't reap us while we work.
        """
        async with database.session() as session:
            stmt = (
                select(Video.id)
                .where(
                    Video.status == "processing",
                    Video.deleted_at.is_(None),
                )
                .order_by(Video.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            row = result.first()
            if row is None:
                return None

            video_id = row[0]

            # Mark as claimed by touching processing_started_at.
            # This is what prevents another worker from picking it up
            # AFTER this session commits.
            await session.execute(
                update(Video)
                .where(Video.id == video_id)
                .values(
                    processing_started_at=func.now(),
                    updated_at=func.now(),
                )
            )
            await session.commit()
            return video_id

    async def _transcode_in_fresh_session(self, video_id: int) -> None:
        """
        Run the transcode pipeline in its own session.

        The pipeline handles its own status writes (ready/failed) and its
        own commit. Do NOT rollback here — that would discard the
        status='failed' write on error.
        """
        async with database.session() as session:
            service = TranscodeService(session)
            await service.transcode_video(video_id)


async def main() -> None:
    setup_core_logging()
    worker = TranscodeWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())