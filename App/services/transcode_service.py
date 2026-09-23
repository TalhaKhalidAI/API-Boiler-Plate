# App/services/transcode_service.py
"""
Background transcode pipeline.

Flow:
    1. Fetch video row (status='processing')
    2. Download original from MinIO → local temp
    3. ffprobe → MediaInfo
    4. ffmpeg transcode → local HLS + DASH output
    5. Upload HLS + DASH → MinIO
    6. Extract + upload thumbnail
    7. Update DB: status='ready', variants, media info

On failure: status='failed'

Design:
    - Uses tempfile.TemporaryDirectory (auto-cleanup on exit).
    - Compensation: on any failure, sets status='failed'.
    - Idempotent: skips videos already 'ready'.
    - Concurrency: safe to run multiple worker instances (SKIP LOCKED).
"""
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from App.core.LoggingInit import get_core_logger
from App.core import ffmpeg_helper
 
from App.core.settings import settings

from App.repository.VideoRepository import VideoRepository
from App.storage.minio_storage import minio_storage
from App.storage import paths as storage_paths

logger = get_core_logger(__name__)


class TranscodeService:
    """
    Worker service. No permission checks — workers are trusted system actors.
    """

    # For memory vs streaming decision
    STREAMING_THRESHOLD_MB = 100

    # Concurrency for segment uploads
    UPLOAD_CONCURRENCY = 20

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.videos = VideoRepository(session)

# App/services/transcode_service.py

    async def transcode_video(
        self,
        video_id: int,
    ) -> None:
        """
        Full transcode pipeline for one video.

        Status contract:
            processing → ready    on success
            processing → failed   on permanent error (bad input, ffmpeg crash)
            processing → (unchanged) on CancelledError / shutdown — recovery sweep requeues

        Idempotency:
            - Skips if already 'ready'.
            - Safe to call twice; add_variant is upsert-based.
            - Partial HLS uploads are overwritten on retry.
        """
        video = await self.videos.get_by_id(video_id, include_deleted=False)
        if video is None:
            logger.warning(f"[video {video_id}] Not found — skipping")
            return

        if video.status == "ready":
            logger.info(f"[video {video_id}] Already ready — skipping")
            return

        if video.status != "processing":
            logger.warning(f"[video {video_id}] Unexpected status={video.status} — skipping")
            return

        if not await ffmpeg_helper.check_ffmpeg_available():
            logger.critical("ffmpeg/ffprobe not on PATH — cannot transcode")
            await self.videos.set_status(video_id, "failed", reason="ffmpeg unavailable")
            return

        bucket = video.storage_bucket
        prefix = video.storage_prefix

        with tempfile.TemporaryDirectory(prefix=f"transcode_{video_id}_") as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "original.mp4"
            output_dir = tmp_path / "output"

            try:
                # === STEP 1: Download ===
                original_key = storage_paths.video_original_key(prefix)
                logger.info(
                    f"[video {video_id}] Downloading {bucket}/{original_key} "
                    f"(prefix={prefix!r})"
                )
                await self._download_from_minio(bucket, original_key, input_path)

                # === STEP 2: Probe ===
                info = await ffmpeg_helper.probe(input_path)
                logger.info(
                    f"[video {video_id}] Probe: {info.width}x{info.height}, "
                    f"{info.duration_sec:.1f}s, codec={info.video_codec}, "
                    f"audio={info.has_audio}"
                )

                # === STEP 3: Transcode (HLS) ===
                logger.info(f"[video {video_id}] Transcoding to HLS")

                def on_progress(fraction: float) -> None:
                    pct = int(fraction * 100)
                    if pct % 10 == 0 and pct > 0:
                        logger.info(f"[video {video_id}] {pct}%")

                result = await ffmpeg_helper.transcode_to_hls(
                    input_path=input_path,
                    output_dir=output_dir,
                    source_height=info.height,
                    source_duration_sec=info.duration_sec,
                    has_audio=info.has_audio,
                    preset="medium",
                    on_progress=on_progress,
                )

                # === STEP 4: Upload HLS ===
                hls_prefix = storage_paths.video_hls_prefix(prefix)
                logger.info(f"[video {video_id}] Uploading HLS → {hls_prefix}")
                await self._upload_tree(
                    bucket=bucket,
                    prefix=hls_prefix.rstrip("/") + "/",
                    local_dir=result.output_dir,
                )

                # === STEP 5: Thumbnail (non-fatal) ===
                thumb_local = tmp_path / "thumbnail.jpg"
                try:
                    await ffmpeg_helper.extract_thumbnail(
                        input_path=input_path,
                        output_path=thumb_local,
                        timestamp_sec=min(5.0, info.duration_sec * 0.1),
                        width=1280,
                    )
                    thumb_key = storage_paths.video_thumbnail_key(prefix)
                    await minio_storage.put_object(
                        bucket=bucket,
                        key=thumb_key,
                        data=thumb_local.read_bytes(),
                        content_type="image/jpeg",
                    )
                    logger.info(f"[video {video_id}] Thumbnail uploaded")
                except Exception:
                    logger.exception(f"[video {video_id}] Thumbnail failed (non-fatal)")

                # === STEP 6: Persist variants (idempotent upsert) ===
                # Do this BEFORE flipping status to 'ready', so a crash between
                # the two leaves status='processing' and the retry re-upserts.
                for rend in result.renditions:
                    await self.videos.add_variant(
                        video_id=video_id,
                        quality=rend.name,
                        codec="h264",
                        playlist_key=f"final/hls/{rend.name}/index.m3u8",
                        bitrate_kbps=rend.video_bitrate_kbps,
                        status="ready",
                    )

                # === STEP 7: Finalize ===
                total_size = await self._compute_size(output_dir)
                await self.videos.update_media_info(
                    video_id,
                    duration_seconds=info.duration_sec,
                    total_size_bytes=total_size,
                )
                await self.videos.set_status(video_id, "ready")
                logger.info(f"[video {video_id}] Transcode complete")

            except asyncio.CancelledError:
                # Worker shutting down mid-flight. Do NOT mark failed — this is
                # not a video problem. Leave status='processing' so the recovery
                # sweep requeues it on next startup.
                logger.warning(
                    f"[video {video_id}] Transcode cancelled (worker shutdown) — "
                    f"leaving status='processing' for recovery"
                )
                raise

            except Exception as e:
                logger.exception(f"[video {video_id}] Transcode pipeline failed")
                await self.videos.set_status(
                    video_id, "failed", reason=str(e)[:500]
                )
                return

    # =====================================================================
    # MinIO HELPERS
    # =====================================================================

    async def _download_from_minio(
        self, bucket: str, key: str, dest: Path
    ) -> None:
        """
        Small files: get_object (in-memory).
        Large files: stream_object (chunked, low memory).
        """
        stat = await minio_storage.stat_object(bucket, key)
        size_mb = stat["size"] / (1024 * 1024)

        if size_mb < self.STREAMING_THRESHOLD_MB:
            data = await minio_storage.get_object(bucket, key)
            dest.write_bytes(data)
        else:
            with dest.open("wb") as f:
                async with minio_storage.stream_object(bucket, key) as chunks:
                    async for chunk in chunks:
                        f.write(chunk)

    async def _upload_tree(
        self,
        *,
        bucket: str,
        prefix: str,
        local_dir: Path,
    ) -> None:
        """
        Upload all files under local_dir to MinIO, preserving relative paths.
        Limits concurrency to avoid saturating the connection.
        """
        await minio_storage.ensure_bucket(bucket)

        files = [p for p in local_dir.rglob("*") if p.is_file()]
        logger.info(f"Uploading {len(files)} files to {prefix}")

        semaphore = asyncio.Semaphore(self.UPLOAD_CONCURRENCY)

        async def upload_one(path: Path) -> None:
            async with semaphore:
                rel = path.relative_to(local_dir)
                key = f"{prefix}{rel.as_posix()}"
                content_type = _guess_content_type(path.suffix)
                await minio_storage.put_object(
                    bucket=bucket,
                    key=key,
                    data=path.read_bytes(),
                    content_type=content_type,
                )

        await asyncio.gather(*(upload_one(p) for p in files))

    @staticmethod
    async def _compute_size(root: Path) -> int:
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


# =====================================================================
# Content type mapping (HLS/DASH segments)
# =====================================================================

def _guess_content_type(suffix: str) -> str:
    suffix = suffix.lower()
    return {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".mpd": "application/dash+xml",
        ".ts": "video/mp2t",
        ".m4s": "video/iso.segment",
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".vtt": "text/vtt",
    }.get(suffix, "application/octet-stream")