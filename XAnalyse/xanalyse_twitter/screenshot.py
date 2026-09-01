"""使用 Pillow 绘制 X/Twitter 风格推文卡片。"""

from __future__ import annotations

import re
import math
import asyncio
from io import BytesIO
from functools import lru_cache
from dataclasses import dataclass
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
from importlib.resources import files

import httpx
from PIL import Image, ImageOps, ImageDraw, ImageFont

from gsuid_core.logger import logger
from gsuid_core.utils.fonts.fonts import core_font

from .api import USER_AGENT, format_number, format_tweet_time
from .models import MediaItem, MediaType, TweetData


@dataclass(frozen=True)
class ScreenshotResult:
    """PIL 卡片渲染结果。"""

    data: bytes | None


@dataclass(frozen=True)
class MediaPreview:
    """卡片内嵌的轻量媒体预览；原始媒体仍由发送流程单独处理。"""

    type: MediaType
    data: bytes | None = None
    aspect_ratio: float | None = None


@dataclass(frozen=True)
class _MediaSlot:
    """媒体在卡片中的位置和尺寸。"""

    index: int
    left: int
    top: int
    width: int
    height: int


_CARD_WIDTH = 1_080
_CARD_MARGIN = 64
_CARD_MAX_HEIGHT = 3_000
_CARD_MAX_BYTES = 4 * 1024 * 1024
_CARD_LINE_HEIGHT = 52
_CARD_BODY_MAX_LINES = 27
_MAX_AVATAR_BYTES = 1 * 1024 * 1024
_AVATAR_TIMEOUT = httpx.Timeout(4.0, connect=2.0, pool=2.0)
_MAX_PREVIEW_ITEMS = 4
_PREVIEW_TIMEOUT = httpx.Timeout(4.0, connect=2.0, pool=2.0)
_PREVIEW_SIZE = (1_200, 1_200)
_PREVIEW_JPEG_QUALITY = 88
_MAX_SOURCE_DIMENSION = 4_096
_MAX_SOURCE_PIXELS = 16_000_000
_SINGLE_MEDIA_MAX_HEIGHT = 1_800
_SINGLE_MEDIA_MIN_HEIGHT = 260
_MULTI_MEDIA_MAX_HEIGHT = 1_800
_MULTI_MEDIA_MIN_HEIGHT = 220
_MEDIA_RADIUS = 20
_MEDIA_GAP = 16
_QUOTE_PADDING = 28
_QUOTE_TEXT_LINE_HEIGHT = 44
_QUOTE_TEXT_MAX_LINES = 8
_QUOTE_MEDIA_SIZE = 192
_QUOTE_MEDIA_GAP = 4
_QUOTE_MEDIA_RADIUS = 18
_QUOTE_BODY_GAP = 16

_BG = (247, 249, 250)
_WHITE = (255, 255, 255)
_BORDER = (207, 217, 222)
_TEXT = (15, 20, 25)
_SECONDARY = (83, 100, 113)
_MUTED = (113, 118, 123)
_BLUE = (29, 155, 240)
_BLUE_DARK = (15, 120, 190)
_MEDIA_BACKGROUND = (239, 243, 244)

_CORE_EMOJI_FONT = files("gsuid_core.utils.fonts").joinpath("TwemojiMozilla-colr.woff2")
_COLOR_EMOJI_FONT_PATHS = ("NotoColorEmoji.ttf", files("gsuid_core.utils.fonts").joinpath("NotoColorEmoji.ttf"))
_CJK_FALLBACK_FONT_NAMES = ("NotoSansCJK-Regular.ttc", "SourceHanSansCN-Regular.ttc")
_UNICODE_FALLBACK_FONT_NAMES = ("unifont_upper.otf", "unifont.otf")
_SYMBOL_EMOJI_FONT_NAMES = ("Symbola_hint.ttf", "DejaVuSans.ttf")
_COLOR_EMOJI_NATIVE_HEIGHT = 128


@lru_cache(maxsize=1)
def _color_emoji_font() -> ImageFont.FreeTypeFont | None:
    for path in (*_COLOR_EMOJI_FONT_PATHS, _CORE_EMOJI_FONT):
        try:
            emoji_font = ImageFont.truetype(path, size=109)
        except OSError:
            continue
        if emoji_font.getmask("❤️").getbbox() is not None:
            return emoji_font
    return None


@lru_cache(maxsize=32)
def _symbol_emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _SYMBOL_EMOJI_FONT_NAMES:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return None


@lru_cache(maxsize=32)
def _cjk_fallback_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _CJK_FALLBACK_FONT_NAMES:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return None


@lru_cache(maxsize=32)
def _unicode_fallback_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ...]:
    fonts: list[ImageFont.FreeTypeFont] = []
    for path in _UNICODE_FALLBACK_FONT_NAMES:
        try:
            fonts.append(ImageFont.truetype(path, size=size))
        except OSError:
            continue
    return tuple(fonts)


@lru_cache(maxsize=64)
def _missing_glyph_signatures(
    font: ImageFont.FreeTypeFont,
) -> tuple[tuple[tuple[int, int], bytes], ...]:
    return tuple((missing.size, bytes(missing)) for missing in (font.getmask("\ufffd"), font.getmask("\U0010ffff")))


@lru_cache(maxsize=8_192)
def _font_has_glyph(font: ImageFont.FreeTypeFont, char: str) -> bool:
    glyph = font.getmask(char)
    signature = (glyph.size, bytes(glyph))
    return signature not in _missing_glyph_signatures(font)


def _normal_font_for_char(font: ImageFont.FreeTypeFont, char: str) -> ImageFont.FreeTypeFont:
    if _font_has_glyph(font, char):
        return font
    fallback = _cjk_fallback_font(font.size)
    if fallback is not None and _font_has_glyph(fallback, char):
        return fallback
    for fallback in _unicode_fallback_fonts(font.size):
        if _font_has_glyph(fallback, char):
            return fallback
    symbol_font = _symbol_emoji_font(font.size)
    if symbol_font is not None and _font_has_glyph(symbol_font, char):
        return symbol_font
    return font


def _iter_normal_font_runs(text: str, font: ImageFont.FreeTypeFont) -> list[tuple[ImageFont.FreeTypeFont, str]]:
    runs: list[tuple[ImageFont.FreeTypeFont, str]] = []
    current_font: ImageFont.FreeTypeFont | None = None
    current_text: list[str] = []
    for char in text:
        selected_font = _normal_font_for_char(font, char)
        if current_font is not selected_font:
            if current_font is not None and current_text:
                runs.append((current_font, "".join(current_text)))
            current_font = selected_font
            current_text = []
        current_text.append(char)
    if current_font is not None and current_text:
        runs.append((current_font, "".join(current_text)))
    return runs


def _is_emoji_base(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2300 <= codepoint <= 0x23FF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x2B00 <= codepoint <= 0x2BFF
        or codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x24C2, 0x3030, 0x303D, 0x3297, 0x3299}
    )


def _consume_emoji_cluster(text: str, start: int) -> tuple[str, int] | None:
    char = text[start]
    if not _is_emoji_base(char) and not (
        char in "#*0123456789" and start + 1 < len(text) and text[start + 1] in "\ufe0e\ufe0f\u20e3"
    ):
        return None

    index = start + 1
    cluster = [char]
    codepoint = ord(char)
    if 0x1F1E6 <= codepoint <= 0x1F1FF and index < len(text):
        next_codepoint = ord(text[index])
        if 0x1F1E6 <= next_codepoint <= 0x1F1FF:
            cluster.append(text[index])
            index += 1

    while index < len(text):
        current = text[index]
        current_codepoint = ord(current)
        if current in "\ufe0e\ufe0f\u20e3" or 0x1F3FB <= current_codepoint <= 0x1F3FF:
            cluster.append(current)
            index += 1
            continue
        if 0xE0020 <= current_codepoint <= 0xE007F:
            cluster.append(current)
            index += 1
            continue
        if current == "\u200d" and index + 1 < len(text) and _is_emoji_base(text[index + 1]):
            cluster.extend((current, text[index + 1]))
            index += 2
            continue
        break
    return "".join(cluster), index


def _iter_text_runs(text: str) -> list[tuple[bool, str]]:
    runs: list[tuple[bool, str]] = []
    normal: list[str] = []
    index = 0
    while index < len(text):
        cluster = _consume_emoji_cluster(text, index)
        if cluster is None:
            normal.append(text[index])
            index += 1
            continue
        if normal:
            runs.append((False, "".join(normal)))
            normal.clear()
        value, index = cluster
        runs.append((True, value))
    if normal:
        runs.append((False, "".join(normal)))
    return runs


def _emoji_target_height(font: ImageFont.FreeTypeFont) -> int:
    bbox = font.getbbox("中")
    return max(1, bbox[3] - bbox[1])


def _emoji_run_width(value: str, font: ImageFont.FreeTypeFont) -> float:
    color_font = _color_emoji_font()
    if color_font is not None:
        return color_font.getlength(value) * _emoji_target_height(font) / _COLOR_EMOJI_NATIVE_HEIGHT
    symbol_font = _symbol_emoji_font(font.size)
    if symbol_font is not None:
        return symbol_font.getlength(value)
    return font.getlength(value)


def _text_length(text: str, font: ImageFont.FreeTypeFont) -> float:
    return sum(
        _emoji_run_width(value, font)
        if is_emoji
        else sum(run_font.getlength(run_text) for run_font, run_text in _iter_normal_font_runs(value, font))
        for is_emoji, value in _iter_text_runs(text)
    )


def _load_card_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return core_font(size, weight=680 if bold else 430)


def _wrap_card_text(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        current = ""
        units = [unit for is_emoji, value in _iter_text_runs(paragraph) for unit in ([value] if is_emoji else value)]
        for unit in units:
            candidate = current + unit
            if current and _text_length(candidate, font) > max_width:
                lines.append(current)
                current = unit
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


def _ellipsize(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    if _text_length(text, font) <= max_width:
        return text
    suffix = "…"
    units = [unit for is_emoji, value in _iter_text_runs(text) for unit in ([value] if is_emoji else value)]
    while units and _text_length("".join(units) + suffix, font) > max_width:
        units.pop()
    return "".join(units) + suffix


def _limit_lines(lines: list[str], font: ImageFont.FreeTypeFont, max_width: float, limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    result = lines[:limit]
    result[-1] = _ellipsize(result[-1], font, max_width)
    return result


def _short_link(link: str) -> str:
    parsed = urlsplit(link)
    if parsed.hostname and parsed.path:
        return f"{parsed.hostname}{parsed.path}"
    return link or "x.com"


def _handle_from_link(link: str) -> str:
    path_parts = [part for part in urlsplit(link).path.split("/") if part]
    if path_parts and path_parts[0].lower() not in {"i", "status"}:
        return path_parts[0]
    return ""


async def _download_avatar(client: httpx.AsyncClient | None, avatar_url: str) -> bytes | None:
    """下载小尺寸头像；失败时继续使用首字母占位图。"""

    if client is None or not avatar_url:
        return None
    parsed = urlsplit(avatar_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    try:
        async with client.stream(
            "GET",
            avatar_url,
            headers={"User-Agent": USER_AGENT},
            timeout=_AVATAR_TIMEOUT,
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length is not None and content_length.isdigit() and int(content_length) > _MAX_AVATAR_BYTES:
                return None
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > _MAX_AVATAR_BYTES:
                    return None
                chunks.extend(chunk)
            return bytes(chunks) if chunks else None
    except (httpx.HTTPError, ValueError, OSError) as error:
        logger.debug(f"[XAnalyse] 头像下载失败：{avatar_url}：{error}")
        return None


def _thumbnail_media(data: bytes) -> bytes | None:
    """把图片预览压缩成小 JPEG，避免把原图长期留在卡片渲染线程中。"""

    try:
        with BytesIO(data) as stream:
            media = Image.open(stream)
            if (
                media.width > _MAX_SOURCE_DIMENSION
                or media.height > _MAX_SOURCE_DIMENSION
                or media.width * media.height > _MAX_SOURCE_PIXELS
            ):
                return None
            media.draft("RGB", _PREVIEW_SIZE)
            media.load()
            media = media.convert("RGB")
            media.thumbnail(_PREVIEW_SIZE, Image.Resampling.LANCZOS)
            output = BytesIO()
            media.save(output, format="JPEG", quality=_PREVIEW_JPEG_QUALITY, optimize=True)
            return output.getvalue()
    except (OSError, ValueError, SyntaxError):
        return None


def _preview_url(url: str) -> str:
    """优先请求 X 图床的 medium 变体，兼顾清晰度和卡片渲染开销。"""

    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() != "pbs.twimg.com":
        return url
    query = parse_qsl(parsed.query, keep_blank_values=True)
    for index, (key, _value) in enumerate(query):
        if key == "name":
            query[index] = (key, "medium")
            break
    else:
        query.append(("name", "medium"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


async def _download_media_preview(client: httpx.AsyncClient, item: MediaItem) -> bytes | None:
    """下载单个媒体预览；视频优先使用 API 提供的封面，不下载视频本体。"""

    preview_url = item.url if item.type == "image" else item.thumbnail_url
    if not preview_url:
        return None
    preview_url = _preview_url(preview_url)
    parsed = urlsplit(preview_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    try:
        async with client.stream(
            "GET",
            preview_url,
            headers={"User-Agent": USER_AGENT},
            timeout=_PREVIEW_TIMEOUT,
        ) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
            if not chunks:
                return None
            raw_data = bytes(chunks)
            del chunks
        preview_data = await asyncio.to_thread(_thumbnail_media, raw_data)
        if preview_data is None:
            logger.warning(f"[XAnalyse] 媒体预览解码失败：{preview_url}")
        return preview_data
    except (httpx.HTTPError, ValueError, OSError) as error:
        detail = str(error).strip() or error.__class__.__name__
        logger.debug(f"[XAnalyse] 媒体预览下载失败：{preview_url}：{detail}")
        return None


async def _download_media_previews(
    client: httpx.AsyncClient | None,
    media: tuple[MediaItem, ...],
) -> tuple[MediaPreview, ...]:
    if client is None or not media:
        return ()
    items = media[:_MAX_PREVIEW_ITEMS]
    preview_data = await asyncio.gather(*(_download_media_preview(client, item) for item in items))
    previews: list[MediaPreview] = []
    for item, data in zip(items, preview_data, strict=True):
        aspect_ratio = None
        if item.width is not None and item.height is not None and item.width > 0 and item.height > 0:
            aspect_ratio = item.width / item.height
        previews.append(MediaPreview(type=item.type, data=data, aspect_ratio=aspect_ratio))
    return tuple(previews)


def _prepare_avatar(data: bytes | None, size: int) -> Image.Image | None:
    if not data:
        return None
    try:
        with BytesIO(data) as stream:
            avatar = Image.open(stream)
            if avatar.width > 2048 or avatar.height > 2048:
                return None
            avatar.load()
            avatar = avatar.convert("RGB")
    except (OSError, ValueError, SyntaxError):
        return None
    try:
        return ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return None


def _prepare_media_image(data: bytes | None, size: tuple[int, int]) -> Image.Image | None:
    if not data:
        return None
    try:
        with BytesIO(data) as stream:
            media = Image.open(stream)
            if media.width > 2048 or media.height > 2048 or media.width * media.height > 4_000_000:
                return None
            media.load()
            media = media.convert("RGB")
    except (OSError, ValueError, SyntaxError):
        return None
    source_aspect = media.width / media.height
    target_aspect = size[0] / size[1]
    if abs(source_aspect / target_aspect - 1.0) > 0.01:
        return ImageOps.fit(media, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    return ImageOps.contain(media, size, method=Image.Resampling.LANCZOS)


def _media_aspect(data: bytes | None) -> float | None:
    if not data:
        return None
    try:
        with BytesIO(data) as stream:
            media = Image.open(stream)
            if media.width <= 0 or media.height <= 0:
                return None
            return media.width / media.height
    except (OSError, ValueError, SyntaxError):
        return None


def _preview_aspect(preview: MediaPreview) -> float | None:
    aspect = preview.aspect_ratio
    if aspect is None or aspect <= 0:
        aspect = _media_aspect(preview.data)
    return aspect if aspect is not None and aspect > 0 else None


def _is_vertical_mosaic(previews: tuple[MediaPreview, ...]) -> bool:
    """判断 3/4 张媒体是否适合 X 风格的“皿”形排版。"""

    if len(previews) not in {3, 4}:
        return False
    aspects = tuple(_preview_aspect(preview) for preview in previews)
    if any(aspect is None or aspect > 0.85 for aspect in aspects):
        return False
    valid_aspects = tuple(aspect for aspect in aspects if aspect is not None)
    if not valid_aspects:
        return False
    return max(valid_aspects) / min(valid_aspects) <= 1.18


def _media_panel_size_for_width(
    preview: MediaPreview,
    panel_width: int,
    *,
    single: bool,
    max_panel_height: int | None = None,
) -> tuple[int, int]:
    aspect = _preview_aspect(preview)
    if aspect is None:
        return panel_width, 320 if single else 300

    max_height = _SINGLE_MEDIA_MAX_HEIGHT if single else _MULTI_MEDIA_MAX_HEIGHT
    min_height = _SINGLE_MEDIA_MIN_HEIGHT if single else _MULTI_MEDIA_MIN_HEIGHT
    if max_panel_height is not None:
        max_height = min(max_height, max_panel_height)
    panel_height = round(panel_width / aspect)
    if panel_height > max_height:
        panel_height = max_height
    elif panel_height < min_height:
        panel_height = min_height
    return panel_width, max(1, panel_height)


def _media_rows(previews: tuple[MediaPreview, ...]) -> tuple[tuple[int, ...], ...]:
    if _is_vertical_mosaic(previews):
        # 3/4 张相近竖图横向并排，一行铺满内容区，避免退化成“田”字网格。
        return (tuple(range(len(previews))),)
    if len(previews) == 1:
        return ((0,),)
    return tuple(tuple(range(start, min(start + 2, len(previews)))) for start in range(0, len(previews), 2))


def _media_layout(
    previews: tuple[MediaPreview, ...],
    max_width: int,
    *,
    max_panel_height: int | None = None,
) -> tuple[tuple[_MediaSlot, ...], int]:
    """计算媒体网格位置，所有行都在同一个内容宽度内对齐。"""

    if not previews:
        return (), 0

    rows = _media_rows(previews)
    mosaic = _is_vertical_mosaic(previews)
    slots: list[_MediaSlot] = []
    row_top = 0
    for row_indices in rows:
        column_count = len(row_indices)
        full_width = mosaic or len(previews) == 1 or column_count > 1
        if full_width:
            available_width = max_width
            gap_count = column_count - 1
            base_width, remainder = divmod(
                max(1, available_width - _MEDIA_GAP * gap_count),
                column_count,
            )
            column_widths = tuple(base_width + (index < remainder) for index in range(column_count))
        else:
            column_widths = (max(1, (max_width - _MEDIA_GAP) // 2),)

        panel_sizes = tuple(
            _media_panel_size_for_width(
                previews[index],
                column_widths[column],
                single=full_width and column_count == 1,
                max_panel_height=max_panel_height,
            )
            for column, index in enumerate(row_indices)
        )
        row_height = max(panel_height for _panel_width, panel_height in panel_sizes)
        row_width = sum(column_widths) + _MEDIA_GAP * (column_count - 1)
        row_left = max(0, (max_width - row_width) // 2)
        column_left = row_left
        for column, (index, (_panel_width, panel_height)) in enumerate(zip(row_indices, panel_sizes, strict=True)):
            slots.append(
                _MediaSlot(
                    index=index,
                    left=column_left,
                    top=row_top,
                    width=_panel_width,
                    height=panel_height,
                )
            )
            column_left += column_widths[column] + _MEDIA_GAP
        row_top += row_height + _MEDIA_GAP

    return tuple(slots), max(0, row_top - _MEDIA_GAP)


def _media_section_height(
    previews: tuple[MediaPreview, ...],
    max_width: int,
    *,
    max_panel_height: int | None = None,
) -> int:
    return _media_layout(previews, max_width, max_panel_height=max_panel_height)[1]


def _draw_media_previews(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    previews: tuple[MediaPreview, ...],
    top: int,
    max_width: int,
    *,
    origin_x: int = _CARD_MARGIN,
    max_panel_height: int | None = None,
) -> None:
    if not previews:
        return
    slots, _section_height = _media_layout(previews, max_width, max_panel_height=max_panel_height)

    for slot in slots:
        preview = previews[slot.index]
        panel_width, panel_height = slot.width, slot.height
        left = origin_x + slot.left
        panel_top = slot.top
        panel = _render_media_tile(preview, (panel_width, panel_height))

        mask = Image.new("L", (panel_width, panel_height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, panel_width - 1, panel_height - 1),
            radius=_MEDIA_RADIUS,
            fill=255,
        )
        image.paste(panel, (left, top + panel_top), mask)
        draw.rounded_rectangle(
            (left, top + panel_top, left + panel_width, top + panel_top + panel_height),
            radius=_MEDIA_RADIUS,
            outline=_BORDER,
            width=2,
        )


def _draw_play_icon(panel_draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    center = (width // 2, height // 2)
    panel_draw.ellipse(
        (center[0] - 34, center[1] - 34, center[0] + 34, center[1] + 34),
        fill=_BLUE,
    )
    panel_draw.polygon(
        (
            (center[0] - 8, center[1] - 14),
            (center[0] - 8, center[1] + 14),
            (center[0] + 15, center[1]),
        ),
        fill=_WHITE,
    )


def _render_media_tile(
    preview: MediaPreview,
    size: tuple[int, int],
    *,
    round_image: bool = True,
) -> Image.Image:
    width, height = size
    panel = Image.new("RGB", size, _MEDIA_BACKGROUND)
    panel_draw = ImageDraw.Draw(panel)
    media_image = _prepare_media_image(preview.data, size)
    if media_image is not None:
        image_left = (width - media_image.width) // 2
        image_top = (height - media_image.height) // 2
        if round_image:
            media_mask = Image.new("L", media_image.size, 0)
            ImageDraw.Draw(media_mask).rounded_rectangle(
                (0, 0, media_image.width - 1, media_image.height - 1),
                radius=min(_MEDIA_RADIUS, media_image.width // 2, media_image.height // 2),
                fill=255,
            )
            panel.paste(media_image, (image_left, image_top), media_mask)
        else:
            panel.paste(media_image, (image_left, image_top))
    elif preview.type in {"video", "animated_gif"}:
        _draw_play_icon(panel_draw, width, height)
    else:
        _draw_centered_text(
            panel,
            panel_draw,
            (width / 2, height / 2),
            "图片预览不可用",
            _load_card_font(26),
            _MUTED,
        )

    if media_image is not None and preview.type in {"video", "animated_gif"}:
        _draw_play_icon(panel_draw, width, height)
    return panel


def _media_summary(tweet: TweetData) -> str:
    if not tweet.media:
        return "无媒体"
    image_count = sum(item.type == "image" for item in tweet.media)
    video_count = len(tweet.media) - image_count
    parts = [f"媒体 {len(tweet.media)} 项"]
    if image_count:
        parts.append(f"图片 {image_count}")
    if video_count:
        parts.append(f"视频 {video_count}")
    return " · ".join(parts)


def _avatar_color(author: str) -> tuple[int, int, int]:
    palette = (
        (29, 155, 240),
        (120, 86, 255),
        (0, 150, 136),
        (232, 93, 117),
        (245, 166, 35),
    )
    index = sum(ord(char) for char in author) % len(palette)
    return palette[index]


def _draw_card_text(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    x, y = xy
    baseline_bbox = font.getbbox("中")
    emoji_top = y + baseline_bbox[1]
    target_height = _emoji_target_height(font)
    color_font = _color_emoji_font()
    symbol_font = _symbol_emoji_font(font.size) if color_font is None else None
    for is_emoji, value in _iter_text_runs(text):
        if not is_emoji:
            baseline = y + font.getmetrics()[0]
            for run_font, run_text in _iter_normal_font_runs(value, font):
                if run_font is font:
                    draw.text((x, y), run_text, font=run_font, fill=fill)
                else:
                    draw.text((x, baseline), run_text, font=run_font, fill=fill, anchor="ls")
                x += run_font.getlength(run_text)
            continue

        if color_font is not None:
            native_width = max(1, math.ceil(color_font.getlength(value)))
            emoji_layer = Image.new("RGBA", (native_width + 2, _COLOR_EMOJI_NATIVE_HEIGHT), (0, 0, 0, 0))
            ImageDraw.Draw(emoji_layer).text(
                (0, 0),
                value,
                font=color_font,
                fill=(255, 255, 255, 255),
                embedded_color=True,
            )
            scaled_width = max(1, round(native_width * target_height / _COLOR_EMOJI_NATIVE_HEIGHT))
            emoji_layer = emoji_layer.resize((scaled_width, target_height), Image.Resampling.LANCZOS)
            image.paste(emoji_layer, (round(x), round(emoji_top)), emoji_layer)
            x += scaled_width
        elif symbol_font is not None:
            draw.text((x, y), value, font=symbol_font, fill=fill)
            x += symbol_font.getlength(value)
        else:
            draw.text((x, y), value, font=font, fill=fill)
            x += font.getlength(value)


def _draw_centered_text(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    bbox = font.getbbox(text) if not any(is_emoji for is_emoji, _value in _iter_text_runs(text)) else font.getbbox("中")
    width = _text_length(text, font)
    height = bbox[3] - bbox[1]
    _draw_card_text(
        image,
        draw,
        (xy[0] - width / 2 - bbox[0], xy[1] - height / 2 - bbox[1]),
        text,
        font,
        fill,
    )


def _action_items(tweet: TweetData) -> list[tuple[str, str | None]]:
    """返回有数据的统计操作项。"""

    items: list[tuple[str, str | None]] = []
    if tweet.replies is not None:
        items.append(("reply", format_number(tweet.replies)))
    if tweet.retweets is not None:
        items.append(("repost", format_number(tweet.retweets)))
    if tweet.likes is not None:
        items.append(("like", format_number(tweet.likes)))
    if tweet.views is not None:
        items.append(("views", format_number(tweet.views)))
    return items


_X_ICON_PATHS: dict[str, str] = {
    "reply": (
        "M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 "
        "0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01z "
        "m8.005-6c-3.317 0-6.005 2.69-6.005 6 0 3.37 2.77 6.08 6.138 6.01l.351-.01h1.761v2.3 "
        "l5.087-2.81c1.951-1.08 3.163-3.13 3.163-5.36 0-3.39-2.744-6.13-6.129-6.13H9.756z"
    ),
    "repost": (
        "M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5 "
        "c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88z "
        "M16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14 "
        "-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"
    ),
    "like": (
        "M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09 "
        "C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 "
        "1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 "
        "1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69 "
        "c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67 "
        "-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 "
        "1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 "
        "1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z"
    ),
    "views": "M8.75 21V3h2v18h-2zM18 21V8.5h2V21h-2zM4 21l.004-10h2L6 21H4zm9.248 0v-7h2v7h-2z",
}
_SVG_TOKEN_RE = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_SVG_COMMAND_ARITY = {"M": 2, "m": 2, "L": 2, "l": 2, "H": 1, "h": 1, "V": 1, "v": 1, "C": 6, "c": 6}


def _parse_icon_path(path: str) -> tuple[tuple[tuple[float, float], ...], ...]:
    tokens = _SVG_TOKEN_RE.findall(path)
    contours: list[list[tuple[float, float]]] = []
    contour: list[tuple[float, float]] = []
    command: str | None = None
    token_index = 0
    current = (0.0, 0.0)
    start = (0.0, 0.0)

    while token_index < len(tokens):
        token = tokens[token_index]
        if token.isalpha():
            command = token
            token_index += 1
            if command in "Zz":
                if contour and contour[-1] != start:
                    contour.append(start)
                if contour:
                    contours.append(contour)
                contour = []
                current = start
                command = None
                continue
        if command is None or command not in _SVG_COMMAND_ARITY:
            break

        arity = _SVG_COMMAND_ARITY[command]
        if token_index + arity > len(tokens):
            break
        values = tokens[token_index : token_index + arity]
        if any(value.isalpha() for value in values):
            break
        numbers = tuple(float(value) for value in values)
        token_index += arity
        relative = command.islower()
        operation = command.upper()

        if operation == "M":
            if contour:
                contours.append(contour)
                contour = []
            point = (
                numbers[0] + current[0] if relative else numbers[0],
                numbers[1] + current[1] if relative else numbers[1],
            )
            contour.append(point)
            current = point
            start = point
            command = "l" if relative else "L"
            continue

        if operation == "L":
            point = (
                numbers[0] + current[0] if relative else numbers[0],
                numbers[1] + current[1] if relative else numbers[1],
            )
            contour.append(point)
            current = point
            continue

        if operation == "H":
            current = (numbers[0] + current[0] if relative else numbers[0], current[1])
            contour.append(current)
            continue

        if operation == "V":
            current = (current[0], numbers[0] + current[1] if relative else numbers[0])
            contour.append(current)
            continue

        control_1 = (
            numbers[0] + current[0] if relative else numbers[0],
            numbers[1] + current[1] if relative else numbers[1],
        )
        control_2 = (
            numbers[2] + current[0] if relative else numbers[2],
            numbers[3] + current[1] if relative else numbers[3],
        )
        endpoint = (
            numbers[4] + current[0] if relative else numbers[4],
            numbers[5] + current[1] if relative else numbers[5],
        )
        origin = current
        for step in range(1, 13):
            progress = step / 12
            inverse = 1 - progress
            contour.append(
                (
                    inverse**3 * origin[0]
                    + 3 * inverse**2 * progress * control_1[0]
                    + 3 * inverse * progress**2 * control_2[0]
                    + progress**3 * endpoint[0],
                    inverse**3 * origin[1]
                    + 3 * inverse**2 * progress * control_1[1]
                    + 3 * inverse * progress**2 * control_2[1]
                    + progress**3 * endpoint[1],
                )
            )
        current = endpoint

    if contour:
        contours.append(contour)
    return tuple(tuple(points) for points in contours)


def _point_in_polygon(point: tuple[float, float], polygon: tuple[tuple[float, float], ...]) -> bool:
    point_x, point_y = point
    inside = False
    for (x_1, y_1), (x_2, y_2) in zip(polygon, polygon[1:], strict=False):
        if (y_1 > point_y) != (y_2 > point_y):
            intersection_x = (x_2 - x_1) * (point_y - y_1) / (y_2 - y_1) + x_1
            if point_x < intersection_x:
                inside = not inside
    return inside


@lru_cache(maxsize=16)
def _action_icon_mask(kind: str, size: int) -> Image.Image:
    contours = _parse_icon_path(_X_ICON_PATHS[kind])
    raster_scale = 4
    raster_size = size * raster_scale
    mask = Image.new("L", (raster_size, raster_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    depths = tuple(
        sum(
            _point_in_polygon(contour[0], other)
            for other_index, other in enumerate(contours)
            if other_index != contour_index
        )
        for contour_index, contour in enumerate(contours)
    )
    coordinate_scale = raster_size / 24
    for contour_index in sorted(range(len(contours)), key=lambda index: depths[index]):
        contour = contours[contour_index]
        points = [(round(x * coordinate_scale), round(y * coordinate_scale)) for x, y in contour]
        fill = 0 if depths[contour_index] % 2 else 255
        mask_draw.polygon(points, fill=fill)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _draw_action_icon(
    image: Image.Image,
    kind: str,
    left: float,
    top: float,
    size: int,
    color: tuple[int, int, int],
) -> None:
    """绘制统计图标。"""

    icon_mask = _action_icon_mask(kind, size)
    image.paste(color, (round(left), round(top)), icon_mask)


def _draw_action_bar(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    tweet: TweetData,
    top: int,
    max_width: int,
    font: ImageFont.FreeTypeFont,
) -> None:
    items = _action_items(tweet)
    if not items:
        return

    icon_size = 44
    item_width = max_width / len(items)
    for index, (kind, count) in enumerate(items):
        slot_left = _CARD_MARGIN + index * item_width
        label_width = _text_length(count, font) if count else 0
        content_width = icon_size + (14 + label_width if count else 0)
        content_left = slot_left + (item_width - content_width) / 2
        icon_top = top + 8
        _draw_action_icon(image, kind, content_left, icon_top, icon_size, _SECONDARY)
        if count:
            _draw_card_text(
                image,
                draw,
                (content_left + icon_size + 14, top + 14),
                count,
                font,
                _SECONDARY,
            )


def _quote_text_lines(quote: TweetData, text_font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    clean_text = quote.text.strip()
    if not clean_text:
        return []
    return _limit_lines(
        _wrap_card_text(clean_text, text_font, max_width),
        text_font,
        max_width,
        _QUOTE_TEXT_MAX_LINES,
    )


def _quote_text_width(media_previews: tuple[MediaPreview, ...], inner_width: int) -> int:
    if not media_previews:
        return inner_width
    return max(1, inner_width - _QUOTE_MEDIA_SIZE - _QUOTE_BODY_GAP)


def _quote_body_layout(
    quote: TweetData,
    media_previews: tuple[MediaPreview, ...],
    inner_width: int,
) -> tuple[list[str], int]:
    text_font = _load_card_font(30)
    text_width = _quote_text_width(media_previews, inner_width)
    text_lines = _quote_text_lines(quote, text_font, text_width)
    text_height = len(text_lines) * _QUOTE_TEXT_LINE_HEIGHT
    media_height = _QUOTE_MEDIA_SIZE if media_previews else 0
    return text_lines, max(text_height, media_height)


def _quote_media_cells(count: int, size: int) -> tuple[tuple[int, int, int, int, int], ...]:
    visible_count = min(max(count, 0), 4)
    if visible_count == 0:
        return ()
    if visible_count == 1:
        return ((0, 0, size, size, 0),)

    half_width = (size - _QUOTE_MEDIA_GAP) // 2
    right_width = size - _QUOTE_MEDIA_GAP - half_width
    if visible_count == 2:
        return (
            (0, 0, half_width, size, 0),
            (half_width + _QUOTE_MEDIA_GAP, 0, right_width, size, 1),
        )

    half_height = (size - _QUOTE_MEDIA_GAP) // 2
    bottom_height = size - _QUOTE_MEDIA_GAP - half_height
    cells = [
        (0, 0, half_width, half_height, 0),
        (half_width + _QUOTE_MEDIA_GAP, 0, right_width, half_height, 1),
    ]
    if visible_count == 3:
        cells.append((0, half_height + _QUOTE_MEDIA_GAP, size, bottom_height, 2))
    else:
        cells.extend(
            (
                (0, half_height + _QUOTE_MEDIA_GAP, half_width, bottom_height, 2),
                (half_width + _QUOTE_MEDIA_GAP, half_height + _QUOTE_MEDIA_GAP, right_width, bottom_height, 3),
            )
        )
    return tuple(cells)


def _render_quote_media_thumbnail(previews: tuple[MediaPreview, ...]) -> Image.Image:
    visible_previews = previews[:4]
    thumbnail = Image.new("RGB", (_QUOTE_MEDIA_SIZE, _QUOTE_MEDIA_SIZE), _MEDIA_BACKGROUND)
    for left, top, width, height, index in _quote_media_cells(len(visible_previews), _QUOTE_MEDIA_SIZE):
        tile = _render_media_tile(
            visible_previews[index],
            (width, height),
            round_image=False,
        )
        thumbnail.paste(tile, (left, top))
    return thumbnail


def _quote_section_height(
    quote: TweetData | None,
    media_previews: tuple[MediaPreview, ...],
    max_width: int,
) -> int:
    if quote is None:
        return 0
    inner_width = max(1, max_width - _QUOTE_PADDING * 2)
    _, body_height = _quote_body_layout(quote, media_previews, inner_width)
    content_height = 60
    if body_height:
        content_height += 14 + body_height
    return _QUOTE_PADDING * 2 + content_height


def _draw_avatar(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    data: bytes | None,
    left: int,
    top: int,
    size: int,
    author: str,
) -> None:
    avatar_box = (left, top, left + size - 1, top + size - 1)
    avatar_image = _prepare_avatar(data, size)
    if avatar_image is None:
        draw.ellipse(avatar_box, fill=_avatar_color(author))
        _draw_centered_text(
            image,
            draw,
            (left + size / 2, top + size / 2),
            author[:1] or "X",
            _load_card_font(max(18, round(size * 0.42)), bold=True),
            _WHITE,
        )
        return
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    image.paste(avatar_image, (left, top), mask)
    draw.ellipse(avatar_box, outline=_BORDER, width=2)


def _draw_quote_card(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    quote: TweetData,
    quote_avatar_data: bytes | None,
    quote_media_previews: tuple[MediaPreview, ...],
    top: int,
    max_width: int,
) -> None:
    """绘制接近 X 原生样式的嵌套引用推文。"""

    quote_height = _quote_section_height(quote, quote_media_previews, max_width)
    left = _CARD_MARGIN
    draw.rounded_rectangle(
        (left, top, left + max_width, top + quote_height),
        radius=22,
        fill=_WHITE,
        outline=_BORDER,
        width=2,
    )

    inner_left = left + _QUOTE_PADDING
    inner_top = top + _QUOTE_PADDING
    inner_width = max(1, max_width - _QUOTE_PADDING * 2)
    avatar_size = 60
    author = quote.author_name.strip() or "未知用户"
    _draw_avatar(image, draw, quote_avatar_data, inner_left, inner_top, avatar_size, author)

    author_font = _load_card_font(30, bold=True)
    handle_font = _load_card_font(22)
    author_left = inner_left + avatar_size + 16
    author_label = _ellipsize(author, author_font, max(120, inner_width - avatar_size - 120))
    _draw_card_text(image, draw, (author_left, inner_top + 1), author_label, author_font, _TEXT)
    if quote.verified:
        badge_left = round(author_left + _text_length(author_label, author_font) + 8)
        draw.ellipse((badge_left, inner_top + 5, badge_left + 22, inner_top + 27), fill=_BLUE)
        draw.line(
            (badge_left + 5, inner_top + 16, badge_left + 9, inner_top + 20, badge_left + 17, inner_top + 11),
            fill=_WHITE,
            width=2,
        )
    quote_handle = quote.author_handle.strip().lstrip("@")
    quote_subline = f"@{quote_handle}" if quote_handle else "X/Twitter"
    quote_timestamp = format_tweet_time(quote.created_at)
    if quote_timestamp:
        quote_subline = f"{quote_subline} · {quote_timestamp}"
    _draw_card_text(
        image,
        draw,
        (author_left, inner_top + 36),
        _ellipsize(quote_subline, handle_font, inner_width - avatar_size - 16),
        handle_font,
        _SECONDARY,
    )

    text_font = _load_card_font(30)
    text_lines, body_height = _quote_body_layout(quote, quote_media_previews, inner_width)
    if body_height:
        body_top = inner_top + avatar_size + 14
        if quote_media_previews:
            thumbnail = _render_quote_media_thumbnail(quote_media_previews)
            thumbnail_mask = Image.new("L", thumbnail.size, 0)
            ImageDraw.Draw(thumbnail_mask).rounded_rectangle(
                (0, 0, _QUOTE_MEDIA_SIZE - 1, _QUOTE_MEDIA_SIZE - 1),
                radius=_QUOTE_MEDIA_RADIUS,
                fill=255,
            )
            image.paste(thumbnail, (inner_left, body_top), thumbnail_mask)
            draw.rounded_rectangle(
                (inner_left, body_top, inner_left + _QUOTE_MEDIA_SIZE, body_top + _QUOTE_MEDIA_SIZE),
                radius=_QUOTE_MEDIA_RADIUS,
                outline=_BORDER,
                width=2,
            )
            text_left = inner_left + _QUOTE_MEDIA_SIZE + _QUOTE_BODY_GAP
        else:
            text_left = inner_left
        for line_index, line in enumerate(text_lines):
            text_top = body_top + line_index * _QUOTE_TEXT_LINE_HEIGHT
            _draw_card_text(image, draw, (text_left, text_top), line, text_font, _TEXT)


def _render_tweet_card_sync(
    tweet: TweetData,
    text: str,
    link: str,
    avatar_data: bytes | None = None,
    media_previews: tuple[MediaPreview, ...] = (),
    quote_avatar_data: bytes | None = None,
    quote_media_previews: tuple[MediaPreview, ...] = (),
) -> bytes:
    body_font = _load_card_font(34)
    small_font = _load_card_font(25)
    handle_font = _load_card_font(26)
    author_font = _load_card_font(38, bold=True)
    max_width = _CARD_WIDTH - _CARD_MARGIN * 2

    clean_text = text.strip()
    body_lines = _wrap_card_text(clean_text, body_font, max_width) if clean_text else []
    timestamp = format_tweet_time(tweet.created_at)
    handle = tweet.author_handle.strip().lstrip("@") or _handle_from_link(link)
    author = tweet.author_name.strip() or "未知用户"
    footer_height = 190
    media_gap = 18 if media_previews else 0
    quote_height = _quote_section_height(tweet.quote, quote_media_previews, max_width)
    quote_gap = 18 if tweet.quote is not None else 0
    outer_media_max_height = _SINGLE_MEDIA_MAX_HEIGHT
    if tweet.quote is not None:
        outer_media_max_height = max(
            _SINGLE_MEDIA_MIN_HEIGHT,
            _CARD_MAX_HEIGHT - 166 - _CARD_LINE_HEIGHT - media_gap - quote_gap - quote_height - 18 - footer_height,
        )
    media_height = _media_section_height(
        media_previews,
        max_width,
        max_panel_height=outer_media_max_height,
    )
    max_body_lines = max(
        1,
        (_CARD_MAX_HEIGHT - 166 - media_gap - media_height - quote_gap - quote_height - 18 - footer_height)
        // _CARD_LINE_HEIGHT,
    )
    body_lines = _limit_lines(
        body_lines,
        body_font,
        max_width,
        min(_CARD_BODY_MAX_LINES, max_body_lines),
    )
    body_bottom = 166 + len(body_lines) * _CARD_LINE_HEIGHT
    media_top = body_bottom + media_gap
    media_bottom = media_top + media_height
    quote_top = media_bottom + quote_gap
    divider_y = quote_top + quote_height + 18
    height = max(520, min(_CARD_MAX_HEIGHT, divider_y + footer_height))

    image = Image.new("RGB", (_CARD_WIDTH, height), _BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (20, 20, _CARD_WIDTH - 20, height - 20),
        radius=28,
        fill=_WHITE,
        outline=_BORDER,
        width=2,
    )

    _draw_avatar(image, draw, avatar_data, _CARD_MARGIN, 50, 84, author)

    author_x = _CARD_MARGIN + 104
    author_label = _ellipsize(author, author_font, 530)
    _draw_card_text(image, draw, (author_x, 52), author_label, author_font, _TEXT)
    if tweet.verified:
        badge_x = author_x + _text_length(author_label, author_font) + 12
        draw.ellipse((badge_x, 61, badge_x + 28, 89), fill=_BLUE)
        draw.line((badge_x + 7, 75, badge_x + 12, 80), fill=_WHITE, width=3)
        draw.line((badge_x + 12, 80, badge_x + 21, 69), fill=_WHITE, width=3)
    subline = f"@{handle}" if handle else "X/Twitter"
    if timestamp:
        subline = f"{subline} · {timestamp}"
    _draw_card_text(
        image,
        draw,
        (author_x, 102),
        _ellipsize(subline, handle_font, 700),
        handle_font,
        _SECONDARY,
    )
    _draw_centered_text(image, draw, (_CARD_WIDTH - 86, 76), "···", _load_card_font(30, bold=True), _SECONDARY)

    body_y = 166
    for line in body_lines:
        _draw_card_text(image, draw, (_CARD_MARGIN, body_y), line, body_font, _TEXT)
        body_y += _CARD_LINE_HEIGHT

    _draw_media_previews(
        image,
        draw,
        media_previews,
        media_top,
        max_width,
        max_panel_height=outer_media_max_height,
    )
    if tweet.quote is not None:
        _draw_quote_card(
            image,
            draw,
            tweet.quote,
            quote_avatar_data,
            quote_media_previews,
            quote_top,
            max_width,
        )
    draw.line((_CARD_MARGIN, divider_y, _CARD_WIDTH - _CARD_MARGIN, divider_y), fill=_BORDER, width=2)
    _draw_action_bar(image, draw, tweet, divider_y, max_width, small_font)
    media_text = _media_summary(tweet)
    _draw_card_text(image, draw, (_CARD_MARGIN, divider_y + 72), media_text, small_font, _MUTED)
    _draw_card_text(
        image,
        draw,
        (_CARD_MARGIN, divider_y + 112),
        _ellipsize(_short_link(link), small_font, max_width),
        small_font,
        _BLUE_DARK,
    )

    output = BytesIO()
    image.save(output, format="JPEG", quality=86, optimize=True)
    return output.getvalue()


async def _download_quote_assets(
    client: httpx.AsyncClient | None,
    quote: TweetData | None,
) -> tuple[bytes | None, tuple[MediaPreview, ...]]:
    if quote is None:
        return None, ()
    return await asyncio.gather(
        _download_avatar(client, quote.avatar_url),
        _download_media_previews(client, quote.media),
    )


async def render_tweet_card(
    tweet: TweetData,
    text: str,
    link: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ScreenshotResult:
    """在线程池绘制卡片，避免 PIL 阻塞 Core 事件循环。"""

    avatar_data, media_previews, quote_assets = await asyncio.gather(
        _download_avatar(client, tweet.avatar_url),
        _download_media_previews(client, tweet.media),
        _download_quote_assets(client, tweet.quote),
    )
    quote_avatar_data, quote_media_previews = quote_assets
    try:
        if avatar_data is None and not media_previews and quote_avatar_data is None and not quote_media_previews:
            data = await asyncio.to_thread(_render_tweet_card_sync, tweet, text, link)
        else:
            data = await asyncio.to_thread(
                _render_tweet_card_sync,
                tweet,
                text,
                link,
                avatar_data,
                media_previews,
                quote_avatar_data,
                quote_media_previews,
            )
    except (OSError, ValueError) as error:
        logger.warning(f"[XAnalyse] PIL 卡片生成失败：{error}")
        return ScreenshotResult(data=None)
    finally:
        del avatar_data
        del media_previews
        del quote_avatar_data
        del quote_media_previews
    if len(data) > _CARD_MAX_BYTES:
        logger.warning(f"[XAnalyse] PIL 卡片过大（{len(data) / 1024 / 1024:.1f} MiB），跳过发送")
        return ScreenshotResult(data=None)
    return ScreenshotResult(data=data)


__all__ = ["MediaPreview", "ScreenshotResult", "render_tweet_card"]
