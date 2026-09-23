# App/services/video_service.py
"""
Video CRUD + upload orchestration.

Storage policy:
    - Service generates MinIO keys and presigned URLs.
    - Repositories never touch MinIO.
    - Actual byte transfer happens via presigned URLs (client ↔ MinIO).

Transcode policy:
    - This service does NOT run ffmpeg.
    - Videos start in status='processing'.
    - A separate worker (TranscodeService) picks them up.
    - This service only reads the result (via DB).
"""
import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from App.services.base import BaseService
from App.repository.VideoRepository import VideoRepository
from App.repository.ChannelRepository import ChannelRepository
from App.repository.TagRepository import TagRepository
from App.repository.CategoryRepository import CategoryRepository
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger
from App.core.exceptions import (
    VideoNotFoundError,
    ChannelNotFoundError,
    VideoAccessDeniedError,
    VideoNotReadyError,
    PermissionDeniedError,
    ValidationError,
    MinIOObjectNotFoundError,
)
from App.storage.minio_storage import minio_storage
from App.storage import paths as storage_paths


logger = get_core_logger(__name__)


class VideoService(BaseService):
    """
    Video CRUD and attachments.

    Permission keys:
      - video.create.self / video.update.self / video.delete.self / video.publish.self
      - admin.video.update / admin.video.delete / admin.video.publish
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.videos = VideoRepository(session)
        self.channels = ChannelRepository(session)
        self.tags = TagRepository(session)
        self.categories = CategoryRepository(session)

    # =====================================================================
    # EXISTING HELPERS
    # =====================================================================

    @staticmethod
    def _normalize_permissions(p: Any) -> Dict[str, Any]:
        if not p:
            return {}
        if isinstance(p, str):
            try:
                return json.loads(p)
            except (TypeError, ValueError):
                return {}
        return p

    def _ctx(self, u: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": u.get("id"),
            "role": u.get("role") or u.get("user_role"),
            "permissions": self._normalize_permissions(u.get("permissions")),
        }

    def _assert_can_modify(
        self, ctx: Dict[str, Any], channel_owner_id: int, *, admin_perm: str, self_perm: str
    ) -> None:
        if ctx["role"] == "admin" or ctx["permissions"].get(admin_perm, False):
            return
        if ctx["id"] == channel_owner_id and ctx["permissions"].get(self_perm, False):
            return
        raise PermissionDeniedError("You don't have permission to modify this video")

    async def _get_channel_for_video(self, video_id: int):
        video = await self.videos.get_by_id(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        channel = await self.channels.get_by_id(video.channel_id)
        if channel is None:
            raise ChannelNotFoundError(f"Channel {video.channel_id} not found")
        return video, channel

    # =====================================================================
    # UPLOAD FLOW (NEW)
    # =====================================================================

    async def init_video_upload(
        self,
        current_user: Dict[str, Any],
        *,
        channel_id: int,
        title: str,
        filename: str,
        content_type: str,
        description: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Step 1 of video upload:
          - Create DB row (status='processing')
          - Return a presigned PUT URL for direct client → MinIO upload.
        """
        ctx = self._ctx(current_user)
        channel = await self.channels.get_by_id(channel_id)
        if channel is None:
            raise ChannelNotFoundError(f"Channel {channel_id} not found")
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.create.self",
        )

        # Validation
        if not title or not title.strip():
            raise ValidationError("Title is required")

        if file_size and file_size > settings.MINIO_MAX_VIDEO_SIZE_BYTES:
            max_gb = settings.MINIO_MAX_VIDEO_SIZE_BYTES / (1024 ** 3)
            raise ValidationError(f"Video too large (max {max_gb:.1f} GB)")

        if not content_type or not content_type.startswith("video/"):
            raise ValidationError(f"Invalid content type: {content_type!r}")

        # Keys
        video_uuid = uuid.uuid4()
        prefix = storage_paths.video_prefix(channel.handle, title, video_uuid)
        original_key = storage_paths.video_original_key(prefix)

        # DB row
        video = await self.videos.create(
            channel_id=channel_id,
            title=title,
            description=description,
            storage_bucket=settings.MINIO_DEFAULT_BUCKET,
            storage_prefix=prefix,
            master_playlist_key=storage_paths.RELATIVE_HLS_MASTER,
        )
        await self.channels.increment_video_count(channel_id, by=1)

        # MinIO
        await minio_storage.ensure_bucket(video.storage_bucket)
        upload_url = await minio_storage.presigned_put_url(
            bucket=video.storage_bucket,
            key=original_key,
            expiry_sec=settings.MINIO_UPLOAD_URL_EXPIRY_SEC,
        )

        logger.info(
            f"Initiated upload for video {video.id} "
            f"key={original_key} size={file_size or 'unknown'}"
        )

        return {
            "video_id": video.id,
            "upload_url": upload_url,
            "object_key": original_key,
            "expires_in": settings.MINIO_UPLOAD_URL_EXPIRY_SEC,
            "content_type": content_type,
        }

    async def complete_video_upload(
        self,
        video_id: int,
        current_user: Dict[str, Any],
    ):
        """
        Step 2 of video upload: verify MinIO has the object, then queue for transcode.

        Status transitions:
            pending    → processing   (success — worker can now pick it up)
            processing → processing   (idempotent — already queued)
            ready      → ready        (idempotent — terminal)
            failed     → failed       (idempotent — client must re-init to retry)
        """
        ctx = self._ctx(current_user)
        video, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.create.self",
        )

        # Terminal or already-queued: return as-is, don't re-check MinIO
        if video.status in ("ready", "failed", "processing"):
            return video

        # Anything else (including NULL) is invalid
        if video.status != "pending":
            raise ValidationError(
                f"Cannot complete upload from status={video.status!r}"
            )

        # Verify the object actually landed in MinIO
        original_key = storage_paths.video_original_key(video.storage_prefix)
        try:
            stat = await minio_storage.stat_object(video.storage_bucket, original_key)
        except MinIOObjectNotFoundError:
            raise ValidationError(
                "Upload not found in storage. Did the client PUT succeed?"
            )

        if stat["size"] == 0:
            raise ValidationError("Uploaded file is empty")

        # Only NOW flip to processing — this is what makes the worker see it
        await self.videos.set_status(video_id, "processing")

        logger.info(
            f"Video {video_id} upload verified "
            f"({video.storage_bucket}/{original_key}, {stat['size']} bytes) — queued for transcode"
        )
        return video

    async def get_video_playback_url(
        self,
        video_id: int,
        current_user: Optional[Dict[str, Any]],
        *,
        format: str = "hls",
    ) -> Dict[str, Any]:
        """
        Step 3: return a presigned URL for the manifest. Access control
        is enforced BEFORE generating the URL.
        """
        video = await self.videos.get_by_id(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")

        if video.status != "ready":
            raise VideoNotReadyError(
                f"Video {video_id} is not ready (status={video.status})"
            )

        # Access control
        if video.visibility == "private":
            if not current_user:
                raise VideoAccessDeniedError("Authentication required")
            channel = await self.channels.get_by_id(video.channel_id)
            is_owner = channel is not None and channel.owner_id == current_user.get("id")
            if not is_owner:
                grant = await self.videos.get_active_user_grant(
                    video_id, current_user.get("id")
                )
                if grant is None:
                    raise VideoAccessDeniedError("You don't have access to this video")

        # Pick manifest based on format
        fmt = (format or "hls").lower()
        if fmt == "dash":
            manifest_key = storage_paths.video_dash_manifest_key(video.storage_prefix)
            content_type = "application/dash+xml"
        elif fmt == "hls":
            manifest_key = storage_paths.video_hls_master_key(video.storage_prefix)
            content_type = "application/vnd.apple.mpegurl"
        else:
            raise ValidationError(f"Unknown format: {format!r}. Use 'hls' or 'dash'.")

        url = await minio_storage.presigned_get_url(
            bucket=video.storage_bucket,
            key=manifest_key,
            expiry_sec=settings.MINIO_PLAYBACK_URL_EXPIRY_SEC,
        )

        return {
            "playback_url": url,
            "format": fmt,
            "content_type": content_type,
            "expires_in": settings.MINIO_PLAYBACK_URL_EXPIRY_SEC,
        }

    # =====================================================================
    # THUMBNAIL UPLOAD
    # =====================================================================

    async def update_thumbnail(
        self,
        video_id: int,
        current_user: Dict[str, Any],
        *,
        file: UploadFile,
    ):
        """Upload a custom thumbnail (small, direct through FastAPI)."""
        ctx = self._ctx(current_user)
        video, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )

        if not file.content_type or not file.content_type.startswith("image/"):
            raise ValidationError("Thumbnail must be an image")

        data = await file.read()
        if len(data) == 0:
            raise ValidationError("Thumbnail is empty")
        if len(data) > 10 * 1024 * 1024:
            raise ValidationError("Thumbnail too large (max 10 MB)")

        key = storage_paths.video_thumbnail_key(video.storage_prefix)

        await minio_storage.ensure_bucket(video.storage_bucket)
        await minio_storage.put_object(
            bucket=video.storage_bucket,
            key=key,
            data=data,
            content_type=file.content_type or "image/jpeg",
        )

        return await self.videos.update_metadata(
            video_id, thumbnail_key=storage_paths.THUMBNAIL_FILENAME
        )

    # =====================================================================
    # DELETE with MinIO cleanup
    # =====================================================================

    async def delete_video(self, video_id: int, current_user: Dict[str, Any]) -> None:
        ctx = self._ctx(current_user)
        video, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.delete",
            self_perm="video.delete.self",
        )

        # Snapshot before soft-delete
        bucket = video.storage_bucket
        prefix = video.storage_prefix
        vid = video.id

        await self.videos.soft_delete(video_id)
        await self.channels.increment_video_count(video.channel_id, by=-1)

        # Fire-and-forget MinIO cleanup
        task = asyncio.create_task(self._cleanup_minio_prefix(bucket, prefix, vid))
        task.add_done_callback(self._log_cleanup_result)

    @staticmethod
    def _log_cleanup_result(task: asyncio.Task) -> None:
        try:
            n = task.result()
            logger.info(f"MinIO cleanup: deleted {n} objects")
        except Exception as exc:
            logger.error(f"MinIO cleanup failed: {exc}")

    async def _cleanup_minio_prefix(
        self, bucket: str, prefix: str, video_id: int
    ) -> int:
        """Delete all objects under a prefix. Returns count deleted."""
        try:
            keys = await minio_storage.list_objects(
                bucket, prefix=prefix, recursive=True
            )
            deleted = 0
            for key in keys:
                try:
                    await minio_storage.delete_object(bucket, key)
                    deleted += 1
                except Exception:
                    logger.warning(f"Failed to delete {bucket}/{key}")
            logger.info(f"Cleaned up {deleted}/{len(keys)} objects for video {video_id}")
            return deleted
        except Exception:
            logger.exception(f"MinIO cleanup failed for video {video_id}")
            return 0

    # =====================================================================
    # EXISTING READ METHODS
    # =====================================================================

    async def get_video(self, video_id: int):
        video = await self.videos.get_by_id(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        return video

    async def get_watch_page(self, video_id: int):
        video = await self.videos.get_with_relations(video_id)
        if video is None:
            raise VideoNotFoundError(f"Video {video_id} not found")
        return video

    async def list_public_videos(
        self,
        *,
        tag_id: Optional[int] = None,
        category_id: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List:
        """
        Public feed of ready+public videos.
        Optionally filter by tag_id or category_id.
        """
        if tag_id is not None:
            return await self.videos.list_by_tag(tag_id, limit=limit, offset=offset)
        if category_id is not None:
            return await self.videos.list_by_category(category_id, limit=limit, offset=offset)
        return await self.videos.list_public_feed(limit=limit, offset=offset)

    async def list_channel_videos(
        self,
        channel_id: int,
        *,
        visibility: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List:
        return await self.videos.list_by_channel(
            channel_id, visibility=visibility, status=status, limit=limit, offset=offset
        )

    # =====================================================================
    # EXISTING WRITE METHODS
    # =====================================================================

    async def update_metadata(
        self,
        video_id: int,
        current_user: Dict[str, Any],
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_key: Optional[str] = None,
    ):
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        return await self.videos.update_metadata(
            video_id, title=title, description=description, thumbnail_key=thumbnail_key
        )

    async def set_status(self, video_id: int, current_user: Dict[str, Any], status: str):
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        return await self.videos.set_status(video_id, status)

    async def set_visibility(self, video_id: int, current_user: Dict[str, Any], visibility: str):
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.publish",
            self_perm="video.publish.self",
        )
        return await self.videos.set_visibility(video_id, visibility)

    async def set_tags(self, video_id: int, current_user: Dict[str, Any], tag_ids: List[int]) -> None:
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        added, removed = await self.tags.set_for_video(video_id, tag_ids)
        for tid in added:
            await self.tags.increment_usage_count(tid, by=1)
        for tid in removed:
            await self.tags.increment_usage_count(tid, by=-1)

    async def set_categories(self, video_id: int, current_user: Dict[str, Any], category_ids: List[int]) -> None:
        ctx = self._ctx(current_user)
        _, channel = await self._get_channel_for_video(video_id)
        self._assert_can_modify(
            ctx, channel.owner_id,
            admin_perm="admin.video.update",
            self_perm="video.update.self",
        )
        await self.categories.set_for_video(video_id, category_ids)