"""fxtwitter API、链接识别和推文文案格式化。"""

from __future__ import annotations

import re
import sys
import time
import asyncio
from typing import TypedDict, TypeGuard
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import SplitResult, quote, urlsplit, parse_qsl, urlencode, urlunsplit
from collections.abc import Mapping

import httpx

from gsuid_core.logger import logger

from .models import MediaItem, MediaType, TweetData, TweetFetchResult
from ..xanalyse_config import XAnalyseSettings

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _error_detail(error: BaseException) -> str:
    """异常文本为空时回退到类型名，避免日志只显示一个空冒号。"""

    detail = str(error).strip()
    error_type = error.__class__.__name__
    if not detail:
        return error_type
    if detail == error_type:
        return detail
    return f"{detail}（{error_type}）"


class _SharedHttpState(TypedDict):
    client: httpx.AsyncClient | None
    proxy: str | None
    lock: asyncio.Lock


_HTTP_STATE_KEY = "_xanalyse_http_state_v1"


def _is_shared_http_state(value: object) -> TypeGuard[_SharedHttpState]:
    if not isinstance(value, dict):
        return False
    required = ("client", "proxy", "lock")
    if any(key not in value for key in required):
        return False
    client = value["client"]
    proxy = value["proxy"]
    return (
        isinstance(value["lock"], asyncio.Lock)
        and (client is None or isinstance(client, httpx.AsyncClient))
        and (proxy is None or isinstance(proxy, str))
    )


def _new_shared_http_state() -> _SharedHttpState:
    return {"client": None, "proxy": None, "lock": asyncio.Lock()}


def _shared_http_state() -> _SharedHttpState:
    """取得插件共享的 HTTP 客户端状态。"""

    core_package = sys.modules["gsuid_core"]
    package_globals = core_package.__dict__
    raw_state = package_globals[_HTTP_STATE_KEY] if _HTTP_STATE_KEY in package_globals else None
    if _is_shared_http_state(raw_state):
        return raw_state
    state = _new_shared_http_state()
    package_globals[_HTTP_STATE_KEY] = state
    return state


_TWEET_PATH_RE = re.compile(r"^/[^/\s]+/status/\d+(?:/)?$", re.IGNORECASE)
_PROFILE_PATH_RE = re.compile(
    r"^/([^/\s?#]+)(?:/(?:with_replies|media|likes|followers|following))?/?$",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_RESERVED_PROFILE_HANDLES = frozenset(
    {
        "about",
        "compose",
        "explore",
        "home",
        "i",
        "intent",
        "jobs",
        "login",
        "messages",
        "notifications",
        "privacy",
        "search",
        "settings",
        "signup",
        "tos",
    }
)
# FxTwitter 偶尔会在代理切换节点时重置连接。连接超时只影响失败请求，
# 正常响应仍按实际耗时返回；较长的 pool 超时避免被媒体预览短暂占满时误报失败。
_API_TIMEOUT = httpx.Timeout(12.0, connect=8.0, pool=8.0)
_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/\s'\"<>]+/status/\d+(?![A-Za-z0-9_])"
    r"(?:[/?#][^\s'\"<>,!?;:，。？！；：、]*)?"
    r"|(?:https?://)?t\.co/[^\s'\"<>,!?;:，。？！；：、]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,!?;:)]}>\"'，。？！；：）】》」』、"


def _object(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _value(data: Mapping[str, object], key: str) -> object | None:
    if key in data:
        return data[key]
    return None


def _string(data: Mapping[str, object], key: str, default: str = "") -> str:
    value = _value(data, key)
    return value if isinstance(value, str) else default


def _number(data: Mapping[str, object], key: str) -> float | None:
    value = _value(data, key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _is_x_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    return normalized in {
        "x.com",
        "twitter.com",
        "t.co",
        "mobile.twitter.com",
        "www.twitter.com",
        "mobile.x.com",
        "www.x.com",
    }


def _split_url(value: str) -> SplitResult | None:
    candidate = value.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    return parsed


def normalize_url(value: str) -> str | None:
    """清理链接并统一为 HTTPS；非 X/Twitter 域名返回 ``None``。"""

    parsed = _split_url(value.rstrip(_TRAILING_PUNCTUATION))
    if parsed is None or not _is_x_host(parsed.hostname or ""):
        return None
    return urlunsplit(
        (
            "https",
            parsed.netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def is_tweet_url(value: str) -> bool:
    """判断链接是否指向 ``/<user>/status/<id>``。"""

    parsed = _split_url(value)
    return bool(parsed and parsed.hostname and _is_x_host(parsed.hostname) and _TWEET_PATH_RE.fullmatch(parsed.path))


def handle_from_profile_url(value: str) -> str | None:
    """从 X 用户主页（或主页标签页）提取用户名。"""

    parsed = _split_url(value)
    if parsed is None or not parsed.hostname or not _is_x_host(parsed.hostname):
        return None
    match = _PROFILE_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    handle = match.group(1)
    if handle.lower() in _RESERVED_PROFILE_HANDLES or not _HANDLE_RE.fullmatch(handle):
        return None
    return handle


def is_profile_url(value: str) -> bool:
    """判断链接是否指向一个 X 用户主页，而不是具体推文。"""

    return handle_from_profile_url(value) is not None


def extract_candidate_links(text: str) -> list[str]:
    """提取 X/Twitter 推文候选链接，包含待展开的 ``t.co`` 短链。"""

    links: list[str] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(text):
        normalized = normalize_url(match.group(0))
        if normalized is None or normalized in seen:
            continue
        if not is_tweet_url(normalized) and not urlsplit(normalized).hostname == "t.co":
            continue
        seen.add(normalized)
        links.append(normalized)
    return links


def api_url_for_tweet(value: str) -> str:
    """把 X/Twitter 推文链接映射到 fxtwitter API。"""

    parsed = _split_url(value)
    if parsed is None or not _is_x_host(parsed.hostname or "") or parsed.hostname == "t.co":
        raise ValueError("无效的 X/Twitter 链接")
    return urlunsplit(("https", "api.fxtwitter.com", parsed.path, parsed.query, ""))


def api_url_for_profile(handle: str) -> str:
    """构造 FxTwitter v2 用户时间线地址。"""

    clean_handle = handle.strip().lstrip("@")
    if not _HANDLE_RE.fullmatch(clean_handle):
        raise ValueError("无效的 X/Twitter 用户名")
    return f"https://api.fxtwitter.com/2/profile/{quote(clean_handle, safe='')}/statuses"


def build_http_client(proxy: str, timeout: float = 10.0) -> httpx.AsyncClient:
    """构造统一 HTTP 客户端。"""

    timeout_config = httpx.Timeout(
        timeout,
        connect=min(timeout, 10.0),
        pool=min(timeout, 8.0),
    )
    limits = httpx.Limits(
        max_connections=8,
        max_keepalive_connections=4,
        # 代理软件切换节点后可能主动关闭空闲连接，缩短复用时间可避免复用陈旧连接。
        keepalive_expiry=10.0,
    )

    transport = httpx.AsyncHTTPTransport(
        proxy=proxy or None,
        retries=1,
        limits=limits,
        trust_env=False,
    )
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        timeout=timeout_config,
        headers={"User-Agent": USER_AGENT},
        trust_env=False,
    )


async def get_http_client(proxy: str) -> httpx.AsyncClient:
    """获取按代理地址复用的 HTTP 客户端。"""

    state = _shared_http_state()
    async with state["lock"]:
        client = state["client"]
        if client is None or client.is_closed or state["proxy"] != proxy:
            if client is not None and not client.is_closed:
                await client.aclose()
            client = build_http_client(proxy)
            state["client"] = client
            state["proxy"] = proxy
        return client


async def close_http_client() -> None:
    """在 Core 关闭时释放共享连接池。"""

    state = _shared_http_state()
    async with state["lock"]:
        client = state["client"]
        state["client"] = None
        state["proxy"] = None
        if client is not None and not client.is_closed:
            await client.aclose()


def _media_type(value: str) -> MediaType:
    normalized = value.lower()
    if normalized in {"gif", "animated_gif", "animated-gif"} or "gif" in normalized:
        return "animated_gif"
    if normalized in {"video", "mp4"} or "video" in normalized:
        return "video"
    return "image"


def _video_url(item: Mapping[str, object], fallback: str) -> str:
    formats = _value(item, "formats")
    if not isinstance(formats, list):
        return fallback
    variants: list[tuple[float, str]] = []
    for raw_format in formats:
        variant = _object(raw_format)
        if variant is None or _string(variant, "container").lower() != "mp4":
            continue
        url = _string(variant, "url")
        if not url:
            continue
        bitrate = _number(variant, "bitrate") or 0.0
        variants.append((bitrate, url))
    if not variants:
        return fallback

    positive = [variant for variant in variants if variant[0] > 0]
    if not positive:
        return fallback
    selected = max(positive, key=lambda variant: variant[0])
    return selected[1]


def _dimension(data: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        value = _number(data, key)
        if value is not None and value > 0:
            return round(value)
    return None


def _dimensions_from_url(url: str) -> tuple[int | None, int | None]:
    match = re.search(r"/(\d{2,5})x(\d{2,5})/", url)
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _original_image_url(url: str) -> str:
    """把 X 图床图片请求固定到 ``orig`` 规格，避免误用缩略图。"""

    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() != "pbs.twimg.com":
        return url
    query = parse_qsl(parsed.query, keep_blank_values=True)
    for index, (key, _value) in enumerate(query):
        if key == "name":
            query[index] = (key, "orig")
            break
    else:
        query.append(("name", "orig"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _append_media(media: list[MediaItem], seen: set[str], raw: object, *, article: bool = False) -> None:
    item = _object(raw)
    if item is None:
        return

    if article:
        info = _object(_value(item, "media_info"))
        if info is None:
            return
        url = _string(info, "original_img_url") or _string(info, "url")
        type_name = _string(info, "__typename")
        alt_text = _string(info, "altText") or _string(item, "alt_text")
        thumbnail_url = ""
        media_type = _media_type(type_name)
        width = _dimension(info, "width", "original_width", "original_img_width")
        height = _dimension(info, "height", "original_height", "original_img_height")
    else:
        url = _string(item, "url")
        raw_type = _string(item, "type")
        media_type = _media_type(raw_type)
        alt_text = _string(item, "altText") or _string(item, "alt_text")
        thumbnail_url = _string(item, "thumbnail_url") or _string(item, "thumbnail")
        width = _dimension(item, "width")
        height = _dimension(item, "height")
        if media_type in {"video", "animated_gif"} and url:
            url = _video_url(item, url)
            if width is None or height is None:
                url_width, url_height = _dimensions_from_url(url)
                width = width or url_width
                height = height or url_height
        if not url:
            formats = _object(_value(item, "formats"))
            if formats is not None:
                url = _string(formats, "jpeg") or _string(formats, "webp")

    if media_type == "image" and url:
        url = _original_image_url(url)
    if url and url not in seen:
        seen.add(url)
        media.append(
            MediaItem(
                url=url,
                type=media_type,
                alt_text=alt_text.strip(),
                thumbnail_url=thumbnail_url.strip(),
                width=width,
                height=height,
            )
        )


def _tweet_data_from_mapping(tweet: Mapping[str, object], *, quote_depth: int = 0) -> TweetData:
    author = _object(_value(tweet, "author"))
    author_name = _string(author, "name", "未知用户") if author is not None else "未知用户"
    author_handle = _string(author, "screen_name") if author is not None else ""
    avatar_url = ""
    if author is not None:
        avatar_url = _string(author, "avatar_url")
        if not avatar_url:
            avatar_url = _string(author, "profile_image_url_https")
        if not avatar_url:
            avatar_url = _string(author, "profile_image_url")
    verified = False
    if author is not None:
        verified_value = _value(author, "verified")
        if isinstance(verified_value, bool):
            verified = verified_value
        verification = _object(_value(author, "verification"))
        if not verified and verification is not None:
            verification_value = _value(verification, "verified")
            verified = verification_value if isinstance(verification_value, bool) else False
    text = _string(tweet, "text")
    media: list[MediaItem] = []
    seen_urls: set[str] = set()

    media_obj = _object(_value(tweet, "media"))
    if media_obj is not None:
        media_all = _value(media_obj, "all")
        if isinstance(media_all, list):
            for raw_media in media_all:
                _append_media(media, seen_urls, raw_media)

    article = _object(_value(tweet, "article"))
    if article is not None:
        title = _string(article, "title", "无标题")
        preview = _string(article, "preview_text")
        text = f"【📝长文章】\n标题：{title}\n预览：{preview}"

        cover_media = _object(_value(article, "cover_media"))
        if cover_media is not None:
            _append_media(media, seen_urls, cover_media, article=True)

        entities = _value(article, "media_entities")
        if isinstance(entities, list):
            for raw_entity in entities:
                _append_media(media, seen_urls, raw_entity, article=True)

    retweets = _number(tweet, "retweets")
    if retweets is None:
        retweets = _number(tweet, "reposts")
    quote: TweetData | None = None
    if quote_depth == 0:
        raw_quote = _object(_value(tweet, "quote"))
        if raw_quote is not None:
            quote = _tweet_data_from_mapping(raw_quote, quote_depth=quote_depth + 1)
    return TweetData(
        text=text,
        author_name=author_name or "未知用户",
        media=tuple(media),
        created_at=_string(tweet, "created_at") or None,
        views=_number(tweet, "views"),
        likes=_number(tweet, "likes"),
        replies=_number(tweet, "replies"),
        retweets=retweets,
        author_handle=author_handle,
        verified=verified,
        is_retweet=_object(_value(tweet, "reposted_by")) is not None,
        avatar_url=avatar_url.strip(),
        quote=quote,
    )


def parse_tweet_payload(payload: object) -> TweetFetchResult:
    """解析 fxtwitter JSON；该函数不进行网络 I/O，便于离线测试。"""

    root = _object(payload)
    if root is None:
        return TweetFetchResult(tweet=None)

    code = _value(root, "code")
    code_name = code.upper() if isinstance(code, str) else ""
    if code in {401, 404, "401", "404"} or code_name in {"PRIVATE_TWEET", "NOT_FOUND"}:
        return TweetFetchResult(tweet=None, not_found=True)

    tweet = _object(_value(root, "tweet"))
    if tweet is None:
        tweet = _object(_value(root, "status"))
    if tweet is None:
        return TweetFetchResult(tweet=None)
    return TweetFetchResult(tweet=_tweet_data_from_mapping(tweet))


def parse_latest_profile_payload(payload: object) -> tuple[str, TweetData] | None:
    """从 FxTwitter v2 用户时间线中取第一条有效推文。"""

    root = _object(payload)
    if root is None:
        return None
    code = _value(root, "code")
    if code not in {200, "200"}:
        return None
    results = _value(root, "results")
    if not isinstance(results, list):
        return None
    for raw_status in results:
        status = _object(raw_status)
        if status is None:
            continue
        status_type = _string(status, "type")
        if status_type and status_type != "status":
            continue
        link = normalize_url(_string(status, "url"))
        if link is None or not is_tweet_url(link):
            continue
        return link, _tweet_data_from_mapping(status)
    return None


async def fetch_tweet_data(
    url: str,
    settings: XAnalyseSettings,
    client: httpx.AsyncClient | None = None,
) -> TweetFetchResult:
    """请求 fxtwitter，按配置重试并区分删除/私密推文。"""

    api_url = api_url_for_tweet(url)
    active_client = client or await get_http_client(settings.proxy)
    headers: dict[str, str] = {"User-Agent": USER_AGENT}

    last_error = ""
    for attempt in range(settings.fetch_retries):
        try:
            request_started = time.perf_counter()
            if settings.output_logs:
                label = "开始请求" if attempt == 0 else f"第 {attempt + 1}/{settings.fetch_retries} 次重试"
                logger.info(f"[XAnalyse] {label} API: {api_url}")
            response = await active_client.get(api_url, headers=headers, timeout=_API_TIMEOUT)
            if settings.output_logs:
                logger.info(
                    f"[XAnalyse] API 响应 HTTP {response.status_code}（{time.perf_counter() - request_started:.2f}s）"
                )
            if response.status_code in {401, 404}:
                return TweetFetchResult(tweet=None, not_found=True)
            if response.status_code in {403, 429}:
                last_error = f"API 暂不可用（HTTP {response.status_code}）"
                logger.warning(f"[XAnalyse] {last_error}")
                return TweetFetchResult(tweet=None, error=last_error)
            response.raise_for_status()
            result = parse_tweet_payload(response.json())
            if result.not_found:
                return result
            if result.tweet is not None:
                return result
            last_error = "API 返回中缺少 tweet 字段"
            logger.warning(f"[XAnalyse] {last_error}，跳过重试")
            return TweetFetchResult(tweet=None, error=last_error)
        except (httpx.HTTPError, ValueError) as error:
            last_error = _error_detail(error)
            if attempt + 1 >= settings.fetch_retries:
                logger.warning(f"[XAnalyse] API 请求失败：{last_error}")
                break
            await asyncio.sleep(min(0.5 * (attempt + 1), 2.0))
    return TweetFetchResult(tweet=None, error=last_error)


async def fetch_latest_tweet(
    handle: str,
    settings: XAnalyseSettings,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, TweetData] | None:
    """通过 FxTwitter v2 时间线获取用户最新一条推文。"""

    api_url = api_url_for_profile(handle)
    active_client = client or await get_http_client(settings.proxy)
    headers: dict[str, str] = {"User-Agent": USER_AGENT}

    for attempt in range(settings.fetch_retries):
        try:
            request_started = time.perf_counter()
            response = await active_client.get(
                api_url,
                params={"count": "1", "with_replies": "false", "groupthreads": "false"},
                headers=headers,
                timeout=_API_TIMEOUT,
            )
            if settings.output_logs:
                logger.info(
                    f"[XAnalyse] 用户时间线 HTTP {response.status_code}（"
                    f"{time.perf_counter() - request_started:.2f}s）：{handle}"
                )
            if response.status_code in {204, 404}:
                return None
            if response.status_code in {403, 429}:
                logger.warning(f"[XAnalyse] 用户时间线暂不可用（HTTP {response.status_code}）：{handle}")
                return None
            response.raise_for_status()
            return parse_latest_profile_payload(response.json())
        except (httpx.HTTPError, ValueError) as error:
            detail = _error_detail(error)
            if attempt + 1 >= settings.fetch_retries:
                logger.warning(f"[XAnalyse] 获取用户最新推文失败：{handle}：{detail}")
                break
            await asyncio.sleep(min(0.5 * (attempt + 1), 2.0))
    return None


def format_number(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value >= 10000:
        return f"{int(value // 1000) / 10:.1f}万"
    if value.is_integer():
        return str(int(value))
    return str(value)


def format_tweet_time(created_at: str | None) -> str:
    if not created_at:
        return ""
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(created_at)
        except (TypeError, ValueError, OverflowError):
            return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y/%m/%d %H:%M:%S")


def _visual_width(value: str) -> int:
    width = 0
    for char in value:
        codepoint = ord(char)
        width += 1 if codepoint < 0x80 or 0x2000 <= codepoint <= 0x206F else 2
    return width


def build_tweet_stats(tweet: TweetData) -> str:
    lines: list[str | tuple[str, str]] = []
    if tweet.created_at:
        formatted = format_tweet_time(tweet.created_at)
        if formatted:
            lines.append(f"发布时间：{formatted}")
    if tweet.views is not None or tweet.likes is not None:
        lines.append(
            (
                f"浏览量：{format_number(tweet.views)}" if tweet.views is not None else "",
                f"点赞数：{format_number(tweet.likes)}" if tweet.likes is not None else "",
            )
        )
    if tweet.replies is not None or tweet.retweets is not None:
        lines.append(
            (
                f"回复数：{format_number(tweet.replies)}" if tweet.replies is not None else "",
                f"转帖数：{format_number(tweet.retweets)}" if tweet.retweets is not None else "",
            )
        )
    if not lines:
        return ""

    max_width = max(
        (_visual_width(line[0]) for line in lines if isinstance(line, tuple) and line[0]),
        default=0,
    )
    rendered: list[str] = []
    for line in lines:
        if isinstance(line, tuple):
            left, right = line
            padding = " " * max(0, max_width - _visual_width(left))
            rendered.append(f"{left}{padding}    {right}")
        else:
            rendered.append(line)
    return "\n" + "\n".join(rendered)


def build_tweet_message(
    tweet: TweetData,
    translated_text: str,
    *,
    retweet: bool = False,
) -> str:
    retweet = retweet or tweet.is_retweet
    is_video = any(item.type in {"video", "animated_gif"} for item in tweet.media)
    kind = "视频" if is_video else "图片"
    alt_lines = [
        f"[图片{index if len(tweet.media) > 1 else ''}描述原文: {item.alt_text}]"
        for index, item in enumerate(tweet.media, start=1)
        if item.alt_text
    ]
    suffix = "\n" + "\n".join(alt_lines) if alt_lines else ""
    retweet_suffix = "\n[提醒：这是一条转发推文]" if retweet else ""
    header = f"【{tweet.author_name or '未知用户'}】 获取了一条{kind}推文："
    if not translated_text.strip():
        return f"{header}{suffix}{build_tweet_stats(tweet)}{retweet_suffix}"
    return f"{header}\n{translated_text}{suffix}{build_tweet_stats(tweet)}{retweet_suffix}"
