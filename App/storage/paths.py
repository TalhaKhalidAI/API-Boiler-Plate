# App/storage/paths.py
"""
Central place for MinIO storage path conventions.

Layout:
    channels/{handle}/
        banner.jpg
        avatar.jpg
        videos/{slug}-{short_uuid}/
            original/
                video_upload.mp4
            final/
                hls/
                    master.m3u8
                    720p/...
                dash/
                    manifest.mpd
                    720p/...
            thumbnail.jpg
    users/{user_id}/
        profile_pic.jpg
    playlists/{playlist_id}/
        thumbnail.jpg
"""

import re
import uuid as _uuid


# ---------------------------------------------------------------------------
# Filenames / directory names
# ---------------------------------------------------------------------------

ORIGINAL_DIR = "original"
ORIGINAL_FILENAME = "video_upload.mp4"
FINAL_DIR = "final"
HLS_SUBDIR = "hls"
DASH_SUBDIR = "dash"
THUMBNAIL_FILENAME = "thumbnail.jpg"

BANNER_FILENAME = "banner.jpg"
AVATAR_FILENAME = "avatar.jpg"
PROFILE_PIC_FILENAME = "profile_pic.jpg"

# Relative keys stored in the DB (compat with existing master_playlist_key column)
RELATIVE_HLS_MASTER = f"{FINAL_DIR}/{HLS_SUBDIR}/master.m3u8"
RELATIVE_DASH_MANIFEST = f"{FINAL_DIR}/{DASH_SUBDIR}/manifest.mpd"


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def slugify(text: str, max_length: int = 50) -> str:
    """Convert a title to a URL-safe slug. Never returns empty string."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")[:max_length].rstrip("-")
    return s or "untitled"


def short_uuid(u: _uuid.UUID | None = None) -> str:
    """8-char hex prefix of a UUID. Collision-resistant for our scale."""
    u = u or _uuid.uuid4()
    return str(u).replace("-", "")[:8]


# ---------------------------------------------------------------------------
# Video paths
# ---------------------------------------------------------------------------

def video_prefix(channel_handle: str, video_title: str, video_uuid: _uuid.UUID) -> str:
    """channels/{handle}/videos/{slug}-{short_uuid}/"""
    slug = slugify(video_title)
    suid = short_uuid(video_uuid)
    return f"channels/{channel_handle}/videos/{slug}-{suid}/"


def video_original_key(prefix: str) -> str:
    return f"{prefix}{ORIGINAL_DIR}/{ORIGINAL_FILENAME}"


def video_hls_master_key(prefix: str) -> str:
    return f"{prefix}{FINAL_DIR}/{HLS_SUBDIR}/master.m3u8"


def video_dash_manifest_key(prefix: str) -> str:
    return f"{prefix}{FINAL_DIR}/{DASH_SUBDIR}/manifest.mpd"


def video_hls_prefix(prefix: str) -> str:
    return f"{prefix}{FINAL_DIR}/{HLS_SUBDIR}/"


def video_dash_prefix(prefix: str) -> str:
    return f"{prefix}{FINAL_DIR}/{DASH_SUBDIR}/"


def video_thumbnail_key(prefix: str) -> str:
    return f"{prefix}{THUMBNAIL_FILENAME}"


# ---------------------------------------------------------------------------
# Channel / user / playlist paths
# ---------------------------------------------------------------------------

def channel_banner_key(handle: str) -> str:
    return f"channels/{handle}/{BANNER_FILENAME}"


def channel_avatar_key(handle: str) -> str:
    return f"channels/{handle}/{AVATAR_FILENAME}"


def user_profile_pic_key(user_id: int) -> str:
    return f"users/{user_id}/{PROFILE_PIC_FILENAME}"


def playlist_thumbnail_key(playlist_id: int) -> str:
    return f"playlists/{playlist_id}/{THUMBNAIL_FILENAME}"