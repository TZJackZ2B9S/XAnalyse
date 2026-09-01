"""X/Twitter 推文解析、自动链接检测和订阅推送。"""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from gsuid_core.sv import SV
from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.server import on_core_start, on_core_shutdown
from gsuid_core.segment import MessageSegment
from gsuid_core.subscribe import gs_subscribe

from .api import (
    USER_AGENT,
    is_tweet_url,
    normalize_url,
    is_profile_url,
    get_http_client,
    fetch_tweet_data,
    close_http_client,
    fetch_latest_tweet,
    build_tweet_message,
    extract_candidate_links,
    handle_from_profile_url,
)
from .media import download_media, media_to_message
from .models import TweetData
from .screenshot import (
    ScreenshotResult,
    render_tweet_card,
)
from .translation import translate_text
from ..utils.database import XAnalyseTweet
from ..xanalyse_config import BloggerConfig, XAnalyseSettings, get_settings

sv = SV("XAnalyse 推特解析", priority=4)

_subscription_job_id = "XAnalyse:subscription-check"
_subscription_lock = asyncio.Lock()
_processing_semaphore = asyncio.Semaphore(1)


@dataclass(frozen=True)
class TweetContent:
    """API 已取得的推文内容；媒体下载延后到发送阶段。"""

    tweet: TweetData
    translated: str
    screenshot: ScreenshotResult
    retweet: bool = False


@dataclass(frozen=True)
class TweetContentResult:
    content: TweetContent | None = None
    not_found: bool = False
    invalid_url: bool = False
    error: str = ""


def _translation_input(tweet: TweetData) -> str:
    alt_texts = [item.alt_text for item in tweet.media if item.alt_text]
    if not alt_texts:
        return tweet.text
    alt_lines = [
        f"[图片{index if len(alt_texts) > 1 else ''}描述: {value}]" for index, value in enumerate(alt_texts, start=1)
    ]
    return f"{tweet.text}\n\n" + "\n".join(alt_lines)


async def _expand_short_link(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        response = await client.get(url, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as error:
        logger.debug(f"[XAnalyse] t.co 展开失败：{error}")
        return None
    if "location" not in response.headers:
        return None
    return normalize_url(urljoin("https://x.com", response.headers["location"]))


async def _fetch_tweet_content(
    url: str,
    settings: XAnalyseSettings,
    *,
    retweet: bool = False,
) -> TweetContentResult:
    """解析链接并准备推文卡片。"""

    started = time.perf_counter()
    client = await get_http_client(settings.proxy)
    normalized = normalize_url(url)
    if normalized and normalized.lower().startswith("https://t.co/"):
        normalized = await _expand_short_link(client, normalized)
    if normalized is None or (not is_tweet_url(normalized) and not is_profile_url(normalized)):
        return TweetContentResult(invalid_url=True)

    if is_profile_url(normalized):
        handle = handle_from_profile_url(normalized)
        if handle is None:
            return TweetContentResult(invalid_url=True)
        latest = await fetch_latest_tweet(handle, settings, client)
        if latest is None:
            return TweetContentResult(error=f"无法获取 @{handle} 的最新推文")
        normalized, tweet = latest
        retweet = retweet or tweet.is_retweet
    else:
        result = await fetch_tweet_data(normalized, settings, client)
        if result.not_found:
            return TweetContentResult(not_found=True)
        tweet = result.tweet
        if tweet is None:
            return TweetContentResult(error=result.error)

    translated = await translate_text(_translation_input(tweet), settings, client)
    screenshot = ScreenshotResult(data=None)
    if settings.enable_screenshot:
        screenshot = await render_tweet_card(tweet, translated, normalized, client=client)
    if settings.output_logs:
        logger.info(f"[XAnalyse] API 文案处理完成（{time.perf_counter() - started:.2f}s）")
    return TweetContentResult(
        content=TweetContent(
            tweet=tweet,
            translated=translated,
            screenshot=screenshot,
            retweet=retweet or tweet.is_retweet,
        )
    )


async def _build_tweet_messages(
    url: str,
    settings: XAnalyseSettings,
    *,
    retweet: bool = False,
) -> tuple[Message, ...]:
    """构造订阅推送消息。"""

    result = await _fetch_tweet_content(url, settings, retweet=retweet)
    if result.invalid_url:
        return ()
    if result.not_found:
        return ()
    content = result.content
    if content is None:
        return ()

    screenshot = content.screenshot
    if screenshot.data is not None:
        messages: list[Message] = [MessageSegment.image(screenshot.data)]
    else:
        messages = [
            MessageSegment.text(
                build_tweet_message(
                    content.tweet,
                    content.translated,
                    retweet=content.retweet,
                )
            )
        ]

    client = await get_http_client(settings.proxy)
    media_messages: list[Message] = []
    for item in content.tweet.media:
        prepared = await download_media(client, item, settings)
        if prepared is not None:
            media_messages.append(media_to_message(prepared))
    if len(media_messages) == 1:
        messages.append(media_messages[0])
    elif media_messages:
        messages.append(MessageSegment.node(media_messages))
    return tuple(messages)


async def _send_tweet(
    bot: Bot,
    url: str,
    settings: XAnalyseSettings,
    *,
    notify: bool,
    retweet: bool = False,
) -> bool:
    await _processing_semaphore.acquire()
    try:
        return await _send_tweet_unbounded(bot, url, settings, notify=notify, retweet=retweet)
    finally:
        _processing_semaphore.release()


async def _send_tweet_unbounded(
    bot: Bot,
    url: str,
    settings: XAnalyseSettings,
    *,
    notify: bool,
    retweet: bool = False,
) -> bool:
    if notify:
        await bot.send("正在获取推文内容...")
    result = await _fetch_tweet_content(url, settings, retweet=retweet)
    if result.invalid_url:
        await bot.send("未识别到有效的 X/Twitter 推文或用户主页链接。")
        return False
    if result.not_found:
        await bot.send("该推文已被删除或不存在，无法获取内容。")
        return False
    content = result.content
    if content is None:
        if result.error:
            await bot.send(f"获取推文内容失败（{result.error}），请检查网络代理设置后重试。")
        else:
            await bot.send("获取推文内容失败（可能是网络问题），请检查网络代理设置后重试。")
        return False

    tweet = content.tweet
    translated = content.translated
    screenshot = content.screenshot
    is_retweet = content.retweet
    del content
    del result

    if screenshot.data is not None:
        await bot.send(MessageSegment.image(screenshot.data))
    else:
        await bot.send(
            MessageSegment.text(
                build_tweet_message(
                    tweet,
                    translated,
                    retweet=is_retweet,
                )
            )
        )
    del screenshot

    client = await get_http_client(settings.proxy)
    media_messages: list[Message] = []
    for item in tweet.media:
        prepared = await download_media(client, item, settings)
        if prepared is None:
            continue
        media_message = media_to_message(prepared)
        media_messages.append(media_message)
        del prepared
    if len(media_messages) == 1:
        await bot.send(media_messages[0])
    elif media_messages:
        await bot.send(MessageSegment.node(media_messages))
    del media_messages
    return True


@sv.on_command(
    ("twitter", "x"),
    block=True,
    to_ai="""解析 X/Twitter 推文或用户主页链接，返回正文、统计数据和图片/视频媒体。

当用户发送「twitter <链接>」「x <链接>」或询问推文内容时调用；输入用户主页时返回该用户最新推文。
Args:
    text: X/Twitter 推文或用户主页 URL。
""",
    covers=["X/Twitter 推文解析", "推文图片视频下载", "推特翻译"],
    aliases=["推特·解析链接", "X·查看推文", "Twitter·获取推文"],
)
async def twitter_command(bot: Bot, ev: Event) -> None:
    await _send_tweet(bot, ev.text.strip(), get_settings(), notify=True)


@sv.on_command("tt", block=True)
async def check_command(bot: Bot, _ev: Event) -> None:
    settings = get_settings()
    if not settings.bloggers:
        await bot.send("当前没有配置订阅博主。")
        return
    await bot.send("正在检查订阅更新...")
    await check_subscriptions(settings)


@sv.on_command("cs", block=True)
async def check_status(bot: Bot, _ev: Event) -> None:
    await bot.send("XAnalyse 已加载。")


@sv.on_message()
async def detect_links(bot: Bot, ev: Event) -> None:
    settings = get_settings()
    if not settings.detect_x_links:
        return
    text = ev.raw_text.strip()
    if not text or text.lower().startswith(("twitter ", "twitter\n", "x ", "x\n")):
        return

    candidates = extract_candidate_links(text)
    if not candidates:
        return
    for candidate in candidates:
        await _send_tweet(bot, candidate, settings, notify=False)


def _bot_identity(ws_bot_id: str, route_bot_id: str) -> tuple[str, str]:
    route = route_bot_id.strip()
    if ":" in route:
        platform, account = route.split(":", 1)
    else:
        platform, account = route, ""
    if not platform:
        logger.warning(f"[XAnalyse] WS 连接 {ws_bot_id} 没有可用的平台 ID")
    return platform, account


def _target_event(
    ws_bot_id: str,
    platform: str,
    account: str,
    blogger: BloggerConfig,
    group_id: str,
) -> Event:
    return Event(
        bot_id=platform,
        bot_self_id=account,
        msg_id="",
        user_type="group",
        group_id=group_id,
        user_id=f"xanalyse:{blogger.id}:{group_id}",
        sender={},
        user_pm=0,
        WS_BOT_ID=ws_bot_id,
        real_bot_id=platform,
    )


async def _ensure_config_subscriptions(settings: XAnalyseSettings) -> None:
    """把 bloggers 配置映射为 GsCore 持久化订阅。"""

    for ws_bot_id, active_bot in list(gss.active_bot.items()):
        route_platform, route_account = _bot_identity(ws_bot_id, active_bot.bot_id)
        platform = settings.platform or route_platform
        account = settings.account or route_account
        if not platform or not account:
            logger.warning(
                f"[XAnalyse] 订阅需要配置 platform/account，跳过连接 {ws_bot_id}；"
                "请在插件配置中填写平台 ID 和机器人账号。"
            )
            continue
        if settings.platform and platform != settings.platform:
            continue
        if settings.account and account != settings.account:
            continue
        for blogger in settings.bloggers:
            task_name = f"XAnalyse:{blogger.id}"
            existing = await gs_subscribe.get_subscribe(
                task_name,
                WS_BOT_ID=ws_bot_id,
            )
            existing_groups = {
                row.group_id
                for row in existing
                if row.group_id and row.bot_id == platform and row.bot_self_id == account
            }
            desired_groups = set(blogger.group_ids)
            for group_id in existing_groups - desired_groups:
                stale_event = _target_event(ws_bot_id, platform, account, blogger, group_id)
                await gs_subscribe.delete_subscribe(
                    "session",
                    task_name,
                    stale_event,
                    WS_BOT_ID=ws_bot_id,
                )
            for group_id in blogger.group_ids:
                if group_id in existing_groups:
                    continue
                event = _target_event(ws_bot_id, platform, account, blogger, group_id)
                await gs_subscribe.add_subscribe("session", task_name, event)


@gss.on_bot_connect
async def _register_subscriptions_on_bot_connect() -> None:
    settings = get_settings()
    if settings.bloggers:
        await _ensure_config_subscriptions(settings)


async def _send_subscription_update(
    blogger: BloggerConfig,
    latest: tuple[str, TweetData],
    settings: XAnalyseSettings,
) -> None:
    await _processing_semaphore.acquire()
    try:
        subscriptions = await gs_subscribe.get_subscribe(f"XAnalyse:{blogger.id}")
        if not subscriptions:
            return
        link, tweet = latest
        messages = await _build_tweet_messages(link, settings, retweet=tweet.is_retweet)
        if not messages:
            return
        for subscription in subscriptions:
            await subscription.send(reply=list(messages))
    finally:
        _processing_semaphore.release()


async def check_subscriptions(settings: XAnalyseSettings) -> None:
    """轮询 bloggers，首次只建立基线，之后通过 gs_subscribe 推送更新。"""

    if not settings.bloggers:
        return
    async with _subscription_lock:
        await _ensure_config_subscriptions(settings)
        for blogger in settings.bloggers:
            latest = await fetch_latest_tweet(blogger.id, settings)
            if latest is None:
                if settings.output_logs:
                    logger.info(f"[XAnalyse] 暂未取得博主 {blogger.id} 的最新推文")
                continue
            link, tweet = latest
            previous = await XAnalyseTweet.get_by_blogger(blogger.id)
            if previous is None:
                await XAnalyseTweet.save_latest(blogger.id, link, tweet.text)
                continue
            if previous.link == link:
                continue
            await XAnalyseTweet.save_latest(blogger.id, link, tweet.text)
            if any(word and word in tweet.text for word in blogger.blacklist):
                continue

            await _send_subscription_update(blogger, latest, settings)


@on_core_start
async def _start_subscription_job() -> None:
    settings = get_settings()
    if not settings.bloggers:
        return

    async def _run_once() -> None:
        try:
            await check_subscriptions(get_settings())
        except Exception as error:  # APScheduler 任务必须自行记录外部网络/数据库错误
            logger.exception(f"[XAnalyse] 订阅检查失败：{error}")

    asyncio.create_task(_run_once(), name="XAnalyse:subscription-initial")
    scheduler.add_job(
        _run_once,
        trigger="interval",
        minutes=settings.update_interval,
        id=_subscription_job_id,
        name="XAnalyse 订阅检查",
        replace_existing=True,
    )
    logger.info("[XAnalyse] 订阅轮询已启用")


@on_core_shutdown
async def _stop_subscription_job() -> None:
    job = scheduler.get_job(_subscription_job_id)
    if job is not None:
        scheduler.remove_job(_subscription_job_id)
    await close_http_client()


__all__ = [
    "check_subscriptions",
    "detect_links",
    "twitter_command",
]
