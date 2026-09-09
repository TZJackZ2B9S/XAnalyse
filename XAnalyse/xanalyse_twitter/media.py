"""推文媒体下载、FFmpeg 洗白和消息段转换。"""

from __future__ import annotations

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
from gsuid_core.server import on_core_start
from gsuid_core.segment import MessageSegment

from .api import USER_AGENT
from .models import MediaItem, MediaType
from ..xanalyse_config import XAnalyseSettings
from ..utils.resource.RESOURCE_PATH import CACHE_PATH

_MEDIA_TIMEOUT = httpx.Timeout(30.0, connect=8.0, pool=8.0)
_PROBE_TIMEOUT = 8.0
_FFMPEG_TIMEOUT = 120.0
_MAX_MEDIA_DOWNLOAD_BYTES = 512 * 1024 * 1024
_VIDEO_CACHE_TTL = 1800.0
_VIDEO_CACHE_PREFIX = "xanalyse_video_"

_GIF_QUALITY_PRESETS: dict[str, tuple[int, int, int, str]] = {
    "low": (10, 480, 128, "bayer:bayer_scale=5"),
    "medium": (15, 720, 192, "sierra2_4a"),
    "high": (20, 1080, 256, "sierra2_4a"),
}


@on_core_start
async def _check_media_tools() -> None:
    missing = tuple(tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None)
    if missing:
        missing_text = "、".join(missing)
        logger.warning(
            f"[XAnalyse] 未找到系统命令：{missing_text}。"
            "视频/图片洗白、GIF 合成或音轨判断将不可用，但插件仍会正常加载并回退发送原始媒体。"
            "请在 Core 所在系统或容器安装 ffmpeg（通常同时包含 ffprobe）后重启。"
        )
    await _cleanup_video_cache()


@dataclass(frozen=True)
class PreparedMedia:
    """已下载并处理好的媒体。"""

    data: bytes
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


async def _has_audio_stream(data: bytes) -> bool | None:
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
            "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout=_PROBE_TIMEOUT)
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


async def _write_file(path: Path, data: bytes) -> None:
    async with aiofiles.open(path, "wb") as file:
        await file.write(data)


async def _read_file(path: Path) -> bytes:
    async with aiofiles.open(path, "rb") as file:
        return await file.read()


async def _remove_file(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink)
    except FileNotFoundError:
        return


async def _cleanup_video_cache() -> None:
    for path in CACHE_PATH.glob(f"{_VIDEO_CACHE_PREFIX}*"):
        await _remove_file(path)


async def _remove_video_later(path: Path) -> None:
    await asyncio.sleep(_VIDEO_CACHE_TTL)
    await _remove_file(path)


async def _ffmpeg_transform(data: bytes, args: list[str], input_suffix: str, output_suffix: str) -> bytes | None:
    token = secrets.token_hex(8)
    input_path = CACHE_PATH / f"xanalyse_input_{token}{input_suffix}"
    output_path = CACHE_PATH / f"xanalyse_output_{token}{output_suffix}"
    try:
        await _write_file(input_path, data)
        resolved_args = [
            str(input_path) if arg == "INPUT" else str(output_path) if arg == "OUTPUT" else arg for arg in args
        ]
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
            return None
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            logger.debug(f"[XAnalyse] FFmpeg 处理失败：{detail[-300:]}")
            return None
        return await _read_file(output_path)
    except OSError as error:
        logger.debug(f"[XAnalyse] FFmpeg 不可用，跳过媒体处理：{error}")
        return None
    finally:
        await _remove_file(input_path)
        await _remove_file(output_path)


async def _convert_video_to_gif(data: bytes, quality: str) -> bytes | None:
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
    return await _ffmpeg_transform(
        data,
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
        ".mp4",
        ".gif",
    )


async def wash_media(data: bytes, media_type: MediaType) -> bytes:
    """在不改变分辨率的前提下洗白媒体。"""

    if media_type == "video" and is_mp4_bytes(data):
        result = await _ffmpeg_transform(
            data,
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
            ".mp4",
            ".mp4",
        )
        return result or data
    if media_type == "image" and not is_animated_image_bytes(data) and is_image_bytes(data):
        if data.startswith(b"\xff\xd8\xff"):
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
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
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
        result = await _ffmpeg_transform(data, args, ".img", output_suffix)
        return result or data
    return data


async def prepare_media(
    item: MediaItem,
    data: bytes,
    gif_quality: str = "medium",
    convert_gif: bool = True,
) -> PreparedMedia:
    """按文件头纠正媒体类型，并保持媒体原始分辨率。"""

    actual_type: MediaType
    if is_mp4_bytes(data):
        actual_type = "video"
    elif is_image_bytes(data):
        actual_type = "image"
    else:
        actual_type = "video" if item.type == "video" else "image"

    if actual_type == "video" and convert_gif:
        has_audio = await _has_audio_stream(data)
        if item.type == "animated_gif" or has_audio is False:
            gif_data = await _convert_video_to_gif(data, gif_quality)
            if gif_data is not None:
                return PreparedMedia(data=gif_data, type="animated_gif")

    washed_data = await wash_media(data, actual_type)
    return PreparedMedia(data=washed_data, type=actual_type)


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
        try:
            async with client.stream("GET", item.url, headers=headers, timeout=_MEDIA_TIMEOUT) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if (
                    max_media_bytes is not None
                    and content_length is not None
                    and content_length.isdigit()
                    and int(content_length) > max_media_bytes
                ):
                    logger.warning(
                        f"[XAnalyse] 媒体超过 {max_media_bytes / 1024 / 1024:.0f} MB 安全上限，跳过：{item.url}"
                    )
                    return None
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if max_media_bytes is not None and len(chunks) + len(chunk) > max_media_bytes:
                        logger.warning(
                            f"[XAnalyse] 媒体超过 {max_media_bytes / 1024 / 1024:.0f} MB 安全上限，跳过：{item.url}"
                        )
                        return None
                    chunks.extend(chunk)
                data = bytes(chunks)
                del chunks
            if not data:
                raise ValueError("媒体响应为空")
            prepared = await prepare_media(item, data, settings.gif_quality, settings.convert_gif)
            del data
            if max_media_bytes is not None and len(prepared.data) > max_media_bytes:
                logger.warning(
                    f"[XAnalyse] 处理后媒体超过 {max_media_bytes / 1024 / 1024:.0f} MB 安全上限，跳过：{item.url}"
                )
                return None
            return prepared
        except (httpx.HTTPError, ValueError, OSError) as error:
            if attempt + 1 >= settings.fetch_retries:
                logger.warning(f"[XAnalyse] 媒体下载失败：{item.url}：{error}")
                return None
            await asyncio.sleep(min(float(attempt + 1), 5.0))
    return None


async def media_to_message(media: PreparedMedia, *, video_send_type: str = "base64") -> Message:
    """把已处理媒体转换为消息段；file 模式下视频落盘后以 file:// 发送。"""

    if media.type != "video":
        return MessageSegment.image(media.data)

    if video_send_type != "file":
        return MessageSegment.video(media.data)

    path = CACHE_PATH / f"{_VIDEO_CACHE_PREFIX}{secrets.token_hex(8)}.mp4"
    await _write_file(path, media.data)
    asyncio.create_task(_remove_video_later(path), name=f"XAnalyse:remove-video:{path.name}")
    return Message(type="video", data=path.as_uri())
