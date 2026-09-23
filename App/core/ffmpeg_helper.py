# App/core/ffmpeg_helper.py
"""
Async FFmpeg wrapper for HLS transcoding.

Design:
    - FFmpeg runs as a subprocess via asyncio.create_subprocess_exec.
    - The subprocess is put in its own process group (preexec_fn=os.setsid)
      so that on timeout we can SIGKILL the whole tree, not just the parent.
    - Input/output are LOCAL FILESYSTEM PATHS. MinIO integration is the
      caller's job (see App/services/transcode_service.py).
    - Progress is parsed from ffmpeg's stdout (-progress pipe:1). FFmpeg's
      regular logs stay on stderr, so the two streams never mix.
    - Timeout applies to the process itself (proc.wait), not to the stderr
      reader, so a slow-writing ffmpeg can't hang us past the deadline.

ABR ladder:
    Multi-bitrate HLS (master.m3u8 + per-quality playlists).
    H.264 + AAC. Universally supported. fMP4 segments.

Why HLS and not DASH:
    HLS works everywhere without MSE shims. DASH can be added later as a
    second output in the same ffmpeg invocation.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)


# ---------------------------------------------------------------------------
# ABR LADDER
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rendition:
    height: int
    video_bitrate_kbps: int
    audio_bitrate_kbps: int
    maxrate_kbps: int
    bufsize_kbps: int

    @property
    def name(self) -> str:
        return f"{self.height}p"


# Ordered high-to-low. Filtered at runtime by source height.
ABR_LADDER: list[Rendition] = [
    Rendition(height=1080, video_bitrate_kbps=5000, audio_bitrate_kbps=128, maxrate_kbps=5350, bufsize_kbps=7500),
    Rendition(height=720,  video_bitrate_kbps=2500, audio_bitrate_kbps=128, maxrate_kbps=2675, bufsize_kbps=3750),
    Rendition(height=480,  video_bitrate_kbps=1000, audio_bitrate_kbps=96,  maxrate_kbps=1070, bufsize_kbps=1500),
    Rendition(height=360,  video_bitrate_kbps=600,  audio_bitrate_kbps=96,  maxrate_kbps=642,  bufsize_kbps=900),
    Rendition(height=240,  video_bitrate_kbps=300,  audio_bitrate_kbps=64,  maxrate_kbps=321,  bufsize_kbps=450),
]


def pick_ladder(source_height: int) -> list[Rendition]:
    """Return the subset of the ABR ladder at or below the source height."""
    ladder = [r for r in ABR_LADDER if r.height <= source_height]
    if not ladder:
        ladder = [ABR_LADDER[-1]]
    return ladder


# ---------------------------------------------------------------------------
# PROBE
# ---------------------------------------------------------------------------

@dataclass
class MediaInfo:
    duration_sec: float
    width: int
    height: int
    video_codec: str
    audio_codec: Optional[str]
    bitrate_kbps: int
    has_audio: bool


async def probe(path: Path, *, timeout: int = 30) -> MediaInfo:
    """
    Run ffprobe on a media file. Returns structured info.
    Raises RuntimeError if ffprobe fails or times out.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        await proc.wait()
        raise RuntimeError(f"ffprobe timed out after {timeout}s on {path.name}")

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (rc={proc.returncode}) on {path.name}: "
            f"{stderr.decode('utf-8', errors='replace')[:500]}"
        )

    try:
        data = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe produced invalid JSON: {e}") from e

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise RuntimeError(f"No video stream found in {path.name}")

    return MediaInfo(
        duration_sec=float(fmt.get("duration", 0.0)),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        video_codec=video.get("codec_name", "unknown"),
        audio_codec=audio.get("codec_name") if audio else None,
        bitrate_kbps=int(int(fmt.get("bit_rate", 0)) / 1000),
        has_audio=audio is not None,
    )


# ---------------------------------------------------------------------------
# HLS TRANSCODE
# ---------------------------------------------------------------------------

@dataclass
class TranscodeResult:
    output_dir: Path
    master_playlist: Path
    renditions: list[Rendition] = field(default_factory=list)
    duration_sec: float = 0.0


ProgressCallback = Callable[[float], None]

def _write_master_playlist(
    *,
    master_path: Path,
    output_dir: Path,
    renditions: list[Rendition],
    has_audio: bool,
) -> None:
    """
    Manually write an HLS master.m3u8 that references each rendition.

    FFmpeg's -master_pl_name is unreliable in FFmpeg 8.x when used with
    multiple HLS outputs. This function builds the master playlist from
    the renditions we actually produced.

    Standard HLS master format:
        #EXTM3U
        #EXT-X-VERSION:3
        #EXT-X-STREAM-INF:BANDWIDTH=...,RESOLUTION=...,CODECS="..."
        <rendition>/index.m3u8
        ...
    """
    if not renditions:
        raise ValueError("Cannot write master playlist with no renditions")

    lines: list[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
    ]

    # H.264 Main profile, level 4.1, AAC-LC.
    # These strings must match what we actually encoded in _build_ffmpeg_cmd.
    if has_audio:
        codecs = "avc1.4d401f,mp4a.40.2"   # H.264 Main L4.1 + AAC-LC
    else:
        codecs = "avc1.4d401f"              # H.264 Main L4.1 only

    for rend in renditions:
        # Total bitrate: video + audio, in bits per second
        total_kbps = rend.video_bitrate_kbps
        if has_audio:
            total_kbps += rend.audio_bitrate_kbps
        bandwidth = total_kbps * 1000

        # 16:9 aspect ratio approximation.
        # Actual width depends on source aspect; scale=-2 preserves it.
        width = int(round(rend.height * 16 / 9))
        # Ensure width is even (H.264 requirement)
        if width % 2 != 0:
            width -= 1

        lines.append(
            f"#EXT-X-STREAM-INF:"
            f"BANDWIDTH={bandwidth},"
            f"RESOLUTION={width}x{rend.height},"
            f'CODECS="{codecs}"'
        )
        lines.append(f"{rend.name}/index.m3u8")

    # Trailing newline is standard
    master_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
async def transcode_to_hls(
    *,
    input_path: Path,
    output_dir: Path,
    source_height: int,
    source_duration_sec: float,
    has_audio: bool = True,
    on_progress: Optional[ProgressCallback] = None,
    timeout_sec: Optional[int] = None,
    hls_segment_seconds: int = 6,
    preset: str = "medium",
    segment_type: str = "mpegts",
) -> TranscodeResult:
    """
    Transcode a local file into a multi-bitrate HLS output.

    Output structure (mpegts):
        output_dir/
            master.m3u8
            720p/
                index.m3u8
                segment_00001.ts
                ...
            480p/
                ...

    NOTE: master.m3u8 is written by THIS function, not by ffmpeg.
    FFmpeg 8.x has a known bug where -master_pl_name is silently
    ignored in multi-output HLS mode. We build it ourselves instead.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    # Timeout policy
    if timeout_sec is None:
        timeout_sec = max(600, int(source_duration_sec * 6))

    # Subdirs must exist BEFORE ffmpeg writes into them.
    output_dir.mkdir(parents=True, exist_ok=True)
    ladder = pick_ladder(source_height)
    for rend in ladder:
        (output_dir / rend.name).mkdir(parents=True, exist_ok=True)

    logger.info(
        f"Transcoding {input_path.name} → HLS | "
        f"source_height={source_height} renditions={[r.name for r in ladder]} "
        f"preset={preset} segments={segment_type} timeout={timeout_sec}s"
    )

    cmd = _build_ffmpeg_cmd(
        input_path=input_path,
        output_dir=output_dir,
        ladder=ladder,
        has_audio=has_audio,
        hls_segment_seconds=hls_segment_seconds,
        preset=preset,
        segment_type=segment_type,
    )

    logger.info(f"ffmpeg cmd: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,   # progress lines
        stderr=asyncio.subprocess.PIPE,   # logs
        preexec_fn=os.setsid,             # own process group
    )

    # Two concurrent readers. Progress from stdout, log capture from stderr.
    progress_task = asyncio.create_task(
        _consume_progress(proc.stdout, source_duration_sec, on_progress)
    )
    stderr_task = asyncio.create_task(_consume_stderr(proc.stderr))

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.error(f"ffmpeg timed out after {timeout_sec}s — killing process group")
        _kill_process_group(proc)
        await proc.wait()
        # Drain the readers so we don't leave tasks pending
        await asyncio.gather(progress_task, stderr_task, return_exceptions=True)
        raise RuntimeError(f"Transcode timed out after {timeout_sec}s")

    # Process exited cleanly (or with non-zero rc). Drain readers.
    await asyncio.gather(progress_task, stderr_task, return_exceptions=True)

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (rc={proc.returncode}) for {input_path.name}"
        )

    # =================================================================
    # Verify at least one rendition playlist was produced
    # =================================================================
    rendered_renditions = [
        r for r in ladder
        if (output_dir / r.name / "index.m3u8").exists()
    ]

    if not rendered_renditions:
        # List what IS in the output dir to help debugging
        found = []
        for p in output_dir.rglob("*"):
            if p.is_file():
                found.append(str(p.relative_to(output_dir)))
        raise RuntimeError(
            f"ffmpeg produced no rendition playlists in {output_dir}. "
            f"Files present: {found[:20]}"
        )

    if len(rendered_renditions) < len(ladder):
        missing = [r.name for r in ladder if r not in rendered_renditions]
        logger.warning(
            f"Some renditions missing from output: {missing}. "
            f"Only {[r.name for r in rendered_renditions]} will be in master.m3u8"
        )

    # =================================================================
    # Manually write master.m3u8
    # FFmpeg 8.x silently ignores -master_pl_name in multi-output mode.
    # =================================================================
    master = output_dir / "master.m3u8"
    _write_master_playlist(
        master_path=master,
        output_dir=output_dir,
        renditions=rendered_renditions,
        has_audio=has_audio,
    )

    if not master.exists():
        raise RuntimeError(f"Failed to write master playlist: {master}")

    logger.info(
        f"Transcode complete: {master} "
        f"({len(rendered_renditions)} renditions)"
    )

    return TranscodeResult(
        output_dir=output_dir,
        master_playlist=master,
        renditions=rendered_renditions,
        duration_sec=source_duration_sec,
    )

# async def transcode_to_hls(
#     *,
#     input_path: Path,
#     output_dir: Path,
#     source_height: int,
#     source_duration_sec: float,
#     has_audio: bool = True,
#     on_progress: Optional[ProgressCallback] = None,
#     timeout_sec: Optional[int] = None,
#     hls_segment_seconds: int = 6,
#     preset: str = "medium",
#     segment_type: str = "mpegts",
# ) -> TranscodeResult:
#     """
#     Transcode a local file into a multi-bitrate HLS output.

#     Output structure (fmp4):
#         output_dir/
#             master.m3u8
#             720p/
#                 index.m3u8
#                 init.mp4
#                 segment_00001.m4s
#                 ...
#             480p/
#                 ...

#     Args:
#         input_path: Local path to the source video.
#         output_dir: Local dir to write HLS output. Created if missing.
#         source_height: Source video height. Determines renditions.
#         source_duration_sec: Duration in seconds.
#         has_audio: If False, produces video-only streams.
#         on_progress: Sync callback 0.0..1.0. Keep it fast — it runs in the
#                      event loop.
#         timeout_sec: Hard kill after this many seconds. If None, scaled
#                      from duration (default policy: max(600, duration * 6)).
#         hls_segment_seconds: Target segment length. 6s is the standard.
#         preset: x264 preset. "medium" is a good quality/speed trade-off.
#         segment_type: "fmp4" (modern) or "mpegts" (legacy).

#     Raises:
#         FileNotFoundError: input missing.
#         RuntimeError: ffmpeg failed or timed out.
#     """
#     if not input_path.exists():
#         raise FileNotFoundError(f"Input not found: {input_path}")

#     # Timeout policy
#     if timeout_sec is None:
#         timeout_sec = max(600, int(source_duration_sec * 6))

#     # Subdirs must exist BEFORE ffmpeg writes into them.
#     output_dir.mkdir(parents=True, exist_ok=True)
#     ladder = pick_ladder(source_height)
#     for rend in ladder:
#         (output_dir / rend.name).mkdir(parents=True, exist_ok=True)

#     logger.info(
#         f"Transcoding {input_path.name} → HLS | "
#         f"source_height={source_height} renditions={[r.name for r in ladder]} "
#         f"preset={preset} segments={segment_type} timeout={timeout_sec}s"
#     )

#     cmd = _build_ffmpeg_cmd(
#         input_path=input_path,
#         output_dir=output_dir,
#         ladder=ladder,
#         has_audio=has_audio,
#         hls_segment_seconds=hls_segment_seconds,
#         preset=preset,
#         segment_type=segment_type,
#     )

#     logger.info(f"ffmpeg cmd: {' '.join(cmd)}")


#     proc = await asyncio.create_subprocess_exec(
#         *cmd,
#         stdout=asyncio.subprocess.PIPE,   # progress lines
#         stderr=asyncio.subprocess.PIPE,   # logs
#         preexec_fn=os.setsid,             # own process group
#     )

#     # Two concurrent readers. Progress from stdout, log capture from stderr.
#     progress_task = asyncio.create_task(
#         _consume_progress(proc.stdout, source_duration_sec, on_progress)
#     )
#     stderr_task = asyncio.create_task(_consume_stderr(proc.stderr))

#     try:
#         await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
#     except asyncio.TimeoutError:
#         logger.error(f"ffmpeg timed out after {timeout_sec}s — killing process group")
#         _kill_process_group(proc)
#         await proc.wait()
#         # Drain the readers so we don't leave tasks pending
#         await asyncio.gather(progress_task, stderr_task, return_exceptions=True)
#         raise RuntimeError(f"Transcode timed out after {timeout_sec}s")

#     # Process exited cleanly (or with non-zero rc). Drain readers.
#     await asyncio.gather(progress_task, stderr_task, return_exceptions=True)

#     if proc.returncode != 0:
#         raise RuntimeError(
#             f"ffmpeg failed (rc={proc.returncode}) for {input_path.name}"
#         )

#     master = output_dir / "master.m3u8"
#     if not master.exists():
#         raise RuntimeError(f"ffmpeg succeeded but {master} is missing")

#     logger.info(f"Transcode complete: {master}")

#     return TranscodeResult(
#         output_dir=output_dir,
#         master_playlist=master,
#         renditions=ladder,
#         duration_sec=source_duration_sec,
#     )


# ---------------------------------------------------------------------------
# COMMAND BUILDER
# ---------------------------------------------------------------------------

def _build_ffmpeg_cmd(
    *,
    input_path: Path,
    output_dir: Path,
    ladder: list[Rendition],
    has_audio: bool,
    hls_segment_seconds: int,
    preset: str,
    segment_type: str,
) -> list[str]:
    """
    Build the ffmpeg command. Each rendition gets its own output. The
    first output carries -master_pl_name so ffmpeg writes master.m3u8.
    """
    seg_ext = "m4s" if segment_type == "fmp4" else "ts"

    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-progress", "pipe:1",
        "-nostats",
        "-y",
        "-i", str(input_path),
        "-filter_complex", _build_filter_complex(ladder),
    ]

    # DO NOT add -map here. It belongs per-output (inside the loop).

    hls_common = [
        "-f", "hls",
        "-hls_time", str(hls_segment_seconds),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", segment_type,
    ]

    for idx, rend in enumerate(ladder):
        # Input mapping for THIS output
        cmd += ["-map", f"[v{idx}]"]
        if has_audio:
            cmd += ["-map", "0:a:0?"]

        # Video codec
        cmd += [
            "-c:v", "libx264",
            "-preset", preset,
            "-profile:v", "main",
            "-level", "4.1",
            "-pix_fmt", "yuv420p",
            "-b:v", f"{rend.video_bitrate_kbps}k",
            "-maxrate", f"{rend.maxrate_kbps}k",
            "-bufsize", f"{rend.bufsize_kbps}k",
            "-g", str(hls_segment_seconds * 30),
            "-keyint_min", str(hls_segment_seconds * 30),
            "-sc_threshold", "0",
        ]

        # Audio codec
        if has_audio:
            cmd += [
                "-c:a", "aac",
                "-b:a", f"{rend.audio_bitrate_kbps}k",
                "-ac", "2",
                "-ar", "48000",
            ]
        else:
            cmd += ["-an"]

        # HLS output
        cmd += hls_common + [
            "-hls_segment_filename",
            str(output_dir / rend.name / f"segment_%05d.{seg_ext}"),
        ]

        # Master playlist ONLY on first output
        # if idx == 0:
        #     cmd += ["-master_pl_name", "master.m3u8"]

        cmd += [str(output_dir / rend.name / "index.m3u8")]

    return cmd


def _build_filter_complex(ladder: list[Rendition]) -> str:
    """
    [0:v]split=N[vin0][vin1]...;
    [vin0]scale=-2:H0[v0];
    [vin1]scale=-2:H1[v1]; ...
    """
    n = len(ladder)
    split_inputs = "".join(f"[vin{i}]" for i in range(n))
    parts = [f"[0:v]split={n}{split_inputs}"]
    for i, rend in enumerate(ladder):
        parts.append(f"[vin{i}]scale=-2:{rend.height}[v{i}]")
    return ";".join(parts)


# ---------------------------------------------------------------------------
# PROCESS HELPERS
# ---------------------------------------------------------------------------

def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the entire process group. No-op if the process is gone."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already dead, or we can't signal it. Fall back to kill().
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _consume_progress(
    stdout: asyncio.StreamReader,
    duration_sec: float,
    on_progress: Optional[ProgressCallback],
) -> None:
    """
    Read ffmpeg -progress key=value lines from stdout and invoke callback.
    Keeps the last few lines for diagnostics.
    """
    out_time_re = re.compile(r"^out_time_ms=(\d+)")
    tail: deque[str] = deque(maxlen=20)

    while True:
        line = await stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        tail.append(text)

        m = out_time_re.match(text)
        if m and on_progress and duration_sec > 0:
            current_ms = int(m.group(1))
            fraction = min(current_ms / 1_000_000 / duration_sec, 1.0)
            try:
                on_progress(fraction)
            except Exception:
                logger.exception("Progress callback raised (ignored)")


async def _consume_stderr(stderr: asyncio.StreamReader) -> None:
    """
    Drain stderr. Only the last 20 lines are kept and logged at debug
    level; anything containing 'error' or 'failed' is logged as warning.
    """
    tail: deque[str] = deque(maxlen=20)

    while True:
        line = await stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        tail.append(text)

    if not tail:
        return

    joined = "\n".join(tail)
    lowered = joined.lower()
    if "error" in lowered or "failed" in lowered or "invalid" in lowered:
        logger.warning(f"ffmpeg stderr tail:\n{joined}")
    else:
        logger.debug(f"ffmpeg stderr tail:\n{joined}")


# ---------------------------------------------------------------------------
# FFMPEG AVAILABILITY
# ---------------------------------------------------------------------------

_ffmpeg_checked: Optional[bool] = None


async def check_ffmpeg_available(*, use_cache: bool = True) -> bool:
    """
    Return True if ffmpeg and ffprobe are on PATH.

    Result is cached after the first call. Set use_cache=False to force
    a fresh check (e.g. in health endpoints).
    """
    global _ffmpeg_checked
    if use_cache and _ffmpeg_checked is not None:
        return _ffmpeg_checked

    ok = True
    for binary in ("ffmpeg", "ffprobe"):
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await proc.wait()
            if rc != 0:
                ok = False
                break
        except FileNotFoundError:
            ok = False
            break

    _ffmpeg_checked = ok
    if not ok:
        logger.error("ffmpeg/ffprobe not found on PATH")
    return ok

# App/core/ffmpeg_helper.py — add this function

async def extract_thumbnail(
    *,
    input_path: Path,
    output_path: Path,
    timestamp_sec: float = 5.0,
    width: int = 1280,
) -> None:
    """
    Extract a single frame as a JPEG thumbnail.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(timestamp_sec),
        "-i", str(input_path),
        "-vframes", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "2",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"Thumbnail extraction failed: {stderr.decode()[:500]}"
        )
# ---------------------------------------------------------------------------
# CLEANUP
# ---------------------------------------------------------------------------

def cleanup_dir(path: Path) -> None:
    """Remove a directory tree. Best-effort."""
    try:
        if path.exists():
            shutil.rmtree(path)
    except Exception:
        logger.exception(f"Failed to clean up {path}")