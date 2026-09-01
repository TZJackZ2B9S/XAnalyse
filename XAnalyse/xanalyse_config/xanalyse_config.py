"""XAnalyse 配置实例和类型化读取。"""

from dataclasses import dataclass

from gsuid_core.utils.plugins_config.models import (
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsListStrConfig,
    GsRepeatGroupConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_default import CONFIG_DEFAULT
from ..utils.resource.RESOURCE_PATH import CONFIG_PATH

XAnalyseConfig = StringConfig("XAnalyse", CONFIG_PATH, CONFIG_DEFAULT)


@dataclass(frozen=True)
class BloggerConfig:
    """一个订阅博主配置。"""

    id: str
    group_ids: tuple[str, ...]
    blacklist: tuple[str, ...] = ()


@dataclass(frozen=True)
class XAnalyseSettings:
    """运行时使用的不可变配置快照。"""

    account: str
    platform: str
    enable_screenshot: bool
    update_interval: int
    fetch_retries: int
    max_media_size_mb: int
    translate_enabled: bool
    api_key: str
    api_url: str
    model: str
    prompt: str
    translate_retries: int
    bloggers: tuple[BloggerConfig, ...]
    output_logs: bool
    detect_x_links: bool
    proxy: str = ""
    gif_quality: str = "medium"


def _str(name: str) -> str:
    item = XAnalyseConfig.get_config(name)
    if not isinstance(item, GsStrConfig):
        raise TypeError(f"XAnalyse 配置 {name} 类型错误")
    return item.data


def _bool(name: str) -> bool:
    item = XAnalyseConfig.get_config(name)
    if not isinstance(item, GsBoolConfig):
        raise TypeError(f"XAnalyse 配置 {name} 类型错误")
    return item.data


def _int(name: str) -> int:
    item = XAnalyseConfig.get_config(name)
    if not isinstance(item, GsIntConfig):
        raise TypeError(f"XAnalyse 配置 {name} 类型错误")
    return item.data


def _bloggers() -> tuple[BloggerConfig, ...]:
    item = XAnalyseConfig.get_config("bloggers")
    if not isinstance(item, GsRepeatGroupConfig):
        raise TypeError("XAnalyse 配置 bloggers 类型错误")

    result: list[BloggerConfig] = []
    for raw in item.data:
        blogger_id = raw["id"]
        group_ids = raw["groupID"]
        blacklist = raw["blacklist"]
        if not isinstance(blogger_id, GsStrConfig):
            raise TypeError("XAnalyse 配置 bloggers.id 类型错误")
        if not isinstance(group_ids, GsListStrConfig):
            raise TypeError("XAnalyse 配置 bloggers.groupID 类型错误")
        if not isinstance(blacklist, GsListStrConfig):
            raise TypeError("XAnalyse 配置 bloggers.blacklist 类型错误")
        clean_id = blogger_id.data.strip().lstrip("@")
        if clean_id:
            result.append(
                BloggerConfig(
                    id=clean_id,
                    group_ids=tuple(str(value).strip() for value in group_ids.data if str(value).strip()),
                    blacklist=tuple(str(value) for value in blacklist.data if str(value)),
                )
            )
    return tuple(result)


def get_settings() -> XAnalyseSettings:
    """读取当前配置；每次调用都会反映 WebConsole 的最新值。"""

    return XAnalyseSettings(
        account=_str("account").strip(),
        platform=_str("platform").strip(),
        enable_screenshot=_bool("enableScreenshot"),
        update_interval=max(1, _int("updateInterval")),
        fetch_retries=max(1, _int("fetchRetries")),
        max_media_size_mb=max(0, _int("maxMediaSize")),
        gif_quality=_str("gifQuality").strip().lower(),
        translate_enabled=_bool("whe_translate"),
        api_key=_str("apiKey").strip(),
        api_url=_str("apiurl").strip(),
        model=_str("model").strip(),
        prompt=_str("prompt"),
        translate_retries=max(1, _int("translateRetries")),
        bloggers=_bloggers(),
        output_logs=_bool("outputLogs"),
        detect_x_links=_bool("detectXLinks"),
        proxy=_str("proxy").strip(),
    )
