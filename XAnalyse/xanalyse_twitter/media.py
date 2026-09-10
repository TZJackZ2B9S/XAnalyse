"""推文媒体下载、FFmpeg 洗白和消息段转换。"""

from __future__ import annotations

import time
import shutil
import asyncio
import secrets
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass

import httpx
import aiofiles

from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.server import on_core_start, on_core_start_before
from gsuid_core.segment import MessageSegment

from .api import USER_AGENT
from .models import MediaItem, MediaType
from ..xanalyse_config import XAnalyseSettings
from ..utils.resource.RESOURCE_PATH import CACHE_PATH

_MEDIA_TIMEOUT = httpx.Timeout(30.0, connect=8.0, pool=8.0)
_PROBE_TIMEOUT = 8.0
_FFMPEG_TIMEOUT = 120.0
_MAX_MEDIA_DOWNLOAD_BYTES = 512 * 1024 * 1024
_GIF_SOURCE_MAX_BYTES = 30 * 1024 * 1024
_VIDEO_CACHE_TTL = 1800.0
_MEDIA_CACHE_PREFIX = "xanalyse_media_"
_HEADER_BYTES = 64 * 1024
# 后三个是旧版本命名，保留清理以便升级后回收残留文件。
_CACHE_SWEEP_PREFIXES = (
    _MEDIA_CACHE_PREFIX,
    "xanalyse_input_",
    "xanalyse_output_",
    "xanalyse_video_",
)
_media_cleanup_tasks: set[asyncio.Task[None]] = set()

_GIF_QUALITY_PRESETS: dict[str, tuple[int, int, int, str]] = {
    "low": (10, 480, 128, "bayer:bayer_scale=5"),
    "medium": (15, 720, 192, "sierra2_4a"),
    "high": (20, 1080, 256, "sierra2_4a"),
}


@on_core_start
def _check_media_tools() -> None:
    missing = tuple(tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None)
    if missing:
        missing_text = "、".join(missing)
        logger.warning(
            f"[XAnalyse] 未找到系统命令：{missing_text}。"
            "视频/图片洗白、GIF 合成或音轨判断将不可用，但插件仍会正常加载并回退发送原始媒体。"
            "请在 Core 所在系统或容器安装 ffmpeg（通常同时包含 ffprobe）后重启。"
        )


@dataclass(frozen=True)
class PreparedMedia:
    """已下载并处理好的媒体；始终以文件形式存在，避免整份驻留内存。"""

    path: Path
    type: MediaType


def is_gif_bytes(data: bytes) -> bool:
    return data.startswith((b"GIF87a", b"GIF89a"))


def is_mp4_bytes(data: bytes) -> bool:
    return len(data) >= 12 and b"ftyp" in data[:32]


def is_image_bytes(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or is_gif_bytes(data)
        or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def is_animated_image_bytes(data: bytes) -> bool:
    """识别常见动图容器。"""

    if is_gif_bytes(data):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return b"acTL" in data
    if len(data) >= 21 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if b"ANIM" in data:
            return True
        return data[12:16] == b"VP8X" and bool(data[20] & 0x02)
    return False


async def _has_audio_stream(path: Path) -> bool | None:
    """返回媒体是否包含音轨；探测失败时返回 None。"""

    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=nw=1:nk=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            logger.debug("[XAnalyse] FFprobe 音轨探测超时")
            return None
    except OSError as error:
        logger.debug(f"[XAnalyse] FFprobe 不可用，跳过 GIF 判断：{error}")
        return None

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        logger.debug(f"[XAnalyse] FFprobe 音轨探测失败：{detail[-300:]}")
        return None
    return bool(stdout.strip())


def _gif_quality_options(quality: str) -> tuple[int, int, int, str]:
    normalized = quality.strip().lower()
    if normalized not in _GIF_QUALITY_PRESETS:
        normalized = "medium"
    return _GIF_QUALITY_PRESETS[normalized]


def _cache_path(suffix: str) -> Path:
    return CACHE_PATH / f"{_MEDIA_CACHE_PREFIX}{secrets.token_hex(8)}{suffix}"


async def _read_head(path: Path) -> bytes:
    async with aiofiles.open(path, "rb") as file:
        return await file.read(_HEADER_BYTES)


async def _read_file(path: Path) -> bytes:
    async with aiofiles.open(path, "rb") as file:
        return await file.read()


async def _remove_file(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink)
    except FileNotFoundError:
        return


async def _stream_to_file(response: httpx.Response, path: Path, max_bytes: int) -> int | None:
    """流式落盘；超过上限时丢弃半成品并返回 None。"""

    total = 0
    exceeded = False
    async with aiofiles.open(path, "wb") as file:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                exceeded = True
                break
            await file.write(chunk)
    if exceeded:
        await _remove_file(path)
        return None
    return total


@on_core_start_before
async def _cleanup_media_cache() -> None:
    """清理上次进程残留的媒体缓存；在 WS 启动前执行，避免与新消息写入竞争。"""

    threshold = time.time() - _VIDEO_CACHE_TTL
    for prefix in _CACHE_SWEEP_PREFIXES:
        for path in CACHE_PATH.glob(f"{prefix}*"):
            stat = await asyncio.to_thread(path.stat)
            if stat.st_mtime < threshold:
                await _remove_file(path)


async def _remove_media_later(path: Path) -> None:
    await asyncio.sleep(_VIDEO_CACHE_TTL)
    await _remove_file(path)


def _schedule_media_cleanup(path: Path) -> None:
    task = asyncio.create_task(_remove_media_later(path), name=f"XAnalyse:remove-media:{path.name}")
    _media_cleanup_tasks.add(task)
    task.add_done_callback(_media_cleanup_tasks.discard)


async def _ffmpeg_transform(input_path: Path, output_path: Path, args: list[str]) -> bool:
    """运行 FFmpeg；args 中的 INPUT/OUTPUT 占位符会替换成真实路径。"""

    resolved_args = [
        str(input_path) if arg == "INPUT" else str(output_path) if arg == "OUTPUT" else arg for arg in args
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *resolved_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_FFMPEG_TIMEOUT)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            logger.warning("[XAnalyse] FFmpeg 处理超时，已终止任务")
            return False
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            logger.debug(f"[XAnalyse] FFmpeg 处理失败：{detail[-300:]}")
            return False
        return True
    except OSError as error:
        logger.debug(f"[XAnalyse] FFmpeg 不可用，跳过媒体处理：{error}")
        return False


async def _convert_video_to_gif(path: Path, quality: str) -> Path | None:
    fps, max_dimension, max_colors, dither = _gif_quality_options(quality)
    scale = (
        f"scale=w='min({max_dimension},iw)':h='min({max_dimension},ih)':"
        "force_original_aspect_ratio=decrease:flags=lanczos"
    )
    filter_complex = (
        f"[0:v]fps={fps},{scale},split[s0][s1];"
        f"[s0]palettegen=max_colors={max_colors}:stats_mode=diff[p];"
        f"[s1][p]paletteuse=dither={dither}[v]"
    )
    output_path = _cache_path(".gif")
    transformed = await _ffmpeg_transform(
        path,
        output_path,
        [
            "ffmpeg",
            "-y",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            "-i",
            "INPUT",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map_metadata",
            "-1",
            "-an",
            "-loop",
            "0",
            "-f",
            "gif",
            "OUTPUT",
        ],
    )
    if not transformed:
        await _remove_file(output_path)
        return None
    return output_path


async def wash_media(path: Path, media_type: MediaType) -> Path:
    """在不改变分辨率的前提下洗白媒体；处理失败时返回原文件。"""

    header = await _read_head(path)
    if media_type == "video" and is_mp4_bytes(header):
        output_path = _cache_path(".mp4")
        transformed = await _ffmpeg_transform(
            path,
            output_path,
            [
                "ffmpeg",
                "-y",
                "-i",
                "INPUT",
                "-map_metadata",
                "-1",
                "-metadata",
                f"creation_time={datetime.now(timezone.utc).isoformat()}",
                "-c",
                "copy",
                "-bsf:v",
                "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
                "OUTPUT",
            ],
        )
        if not transformed:
            await _remove_file(output_path)
            return path
        return output_path
    if media_type == "image" and not is_animated_image_bytes(header) and is_image_bytes(header):
        if header.startswith(b"\xff\xd8\xff"):
            args = [
                "ffmpeg",
                "-y",
                "-i",
                "INPUT",
                "-map_metadata",
                "-1",
                "-frames:v",
                "1",
                "-c:v",
                "mjpeg",
                "-q:v",
                "1",
                "-pix_fmt",
                "yuvj444p",
                "OUTPUT",
            ]
            output_suffix = ".jpg"
        elif header.startswith(b"\x89PNG\r\n\x1a\n"):
            args = [
                "ffmpeg",
                "-y",
                "-i",
                "INPUT",
                "-map_metadata",
                "-1",
                "-frames:v",
                "1",
                "-c:v",
                "png",
                "-compression_level",
                "9",
                "-pred",
                "mixed",
                "OUTPUT",
            ]
            output_suffix = ".png"
        else:
            args = [
                "ffmpeg",
                "-y",
                "-i",
                "INPUT",
                "-map_metadata",
                "-1",
                "-frames:v",
                "1",
                "-c:v",
                "libwebp",
                "-lossless",
                "1",
                "-q:v",
                "100",
                "OUTPUT",
            ]
            output_suffix = ".webp"
        output_path = _cache_path(output_suffix)
        transformed = await _ffmpeg_transform(path, output_path, args)
        if not transformed:
            await _remove_file(output_path)
            return path
        return output_path
    return path


async def prepare_media(
    item: MediaItem,
    path: Path,
    gif_quality: str = "medium",
    convert_gif: bool = True,
) -> PreparedMedia:
    """按文件头纠正媒体类型，并保持媒体原始分辨率。"""

    header = await _read_head(path)
    actual_type: MediaType
    if is_mp4_bytes(header):
        actual_type = "video"
    elif is_image_bytes(header):
        actual_type = "image"
    else:
        actual_type = "video" if item.type == "video" else "image"

    if actual_type == "video" and convert_gif:
        source_size = (await asyncio.to_thread(path.stat)).st_size
        if source_size > _GIF_SOURCE_MAX_BYTES:
            logger.info(f"[XAnalyse] 源视频超过 {_GIF_SOURCE_MAX_BYTES // 1024 // 1024} MB，跳过 GIF 转换：{path.name}")
        else:
            has_audio = await _has_audio_stream(path)
            if item.type == "animated_gif" or has_audio is False:
                gif_path = await _convert_video_to_gif(path, gif_quality)
                if gif_path is not None:
                    return PreparedMedia(path=gif_path, type="animated_gif")

    washed_path = await wash_media(path, actual_type)
    return PreparedMedia(path=washed_path, type=actual_type)


async def download_media(
    client: httpx.AsyncClient,
    item: MediaItem,
    settings: XAnalyseSettings,
) -> PreparedMedia | None:
    headers = {"User-Agent": USER_AGENT}
    configured_limit = settings.max_media_size_mb * 1024 * 1024 if settings.max_media_size_mb > 0 else None
    max_media_bytes = (
        min(configured_limit, _MAX_MEDIA_DOWNLOAD_BYTES) if configured_limit is not None else _MAX_MEDIA_DOWNLOAD_BYTES
    )
    for attempt in range(settings.fetch_retries):
        source_path = _cache_path(".source")
        try:
            async with client.stream("GET", item.url, headers=headers, timeout=_MEDIA_TIMEOUT) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None and content_length.isdigit() and int(content_length) > max_media_bytes:
                    logger.warning(
                        f"[XAnalyse] 媒体超过 {max_media_bytes / 1024 / 1024:.0f} MB 安全上限，跳过：{item.url}"
                    )
                    return None
                written = await _stream_to_file(response, source_path, max_media_bytes)
            if written is None:
                logger.warning(f"[XAnalyse] 媒体超过 {max_media_bytes / 1024 / 1024:.0f} MB 安全上限，跳过：{item.url}")
                return None
            if written == 0:
                raise ValueError("媒体响应为空")
            prepared = await prepare_media(item, source_path, settings.gif_quality, settings.convert_gif)
            if prepared.path != source_path:
                await _remove_file(source_path)
            processed_size = (await asyncio.to_thread(prepared.path.stat)).st_size
            if processed_size > max_media_bytes:
                logger.warning(
                    f"[XAnalyse] 处理后媒体超过 {max_media_bytes / 1024 / 1024:.0f} MB 安全上限，跳过：{item.url}"
                )
                await _remove_file(prepared.path)
                return None
            return prepared
        except (httpx.HTTPError, ValueError, OSError) as error:
            await _remove_file(source_path)
            if attempt + 1 >= settings.fetch_retries:
                logger.warning(f"[XAnalyse] 媒体下载失败：{item.url}：{error}")
                return None
            await asyncio.sleep(min(float(attempt + 1), 5.0))
    return None


async def media_to_message(media: PreparedMedia, *, video_send_type: str = "base64") -> Message:
    """把已处理媒体转换为消息段，并接管媒体文件的清理。"""

    if media.type == "video" and video_send_type == "file":
        _schedule_media_cleanup(media.path)
        return Message(type="video", data=media.path.as_uri())

    try:
        data = await _read_file(media.path)
    finally:
        await _remove_file(media.path)
    if media.type == "video":
        return MessageSegment.video(data)
    return MessageSegment.image(data)
