"""XAnalyse 运行时数据模型。"""

from __future__ import annotations

from typing import Literal
from dataclasses import dataclass

MediaType = Literal["image", "video", "animated_gif"]


@dataclass(frozen=True)
class MediaItem:
    """推文中的一个媒体资源。"""

    url: str
    type: MediaType
    alt_text: str = ""
    thumbnail_url: str = ""
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class TweetData:
    """由 FxTwitter API 得到的推文信息。"""

    text: str
    author_name: str
    media: tuple[MediaItem, ...]
    created_at: str | None = None
    views: float | None = None
    likes: float | None = None
    replies: float | None = None
    retweets: float | None = None
    author_handle: str = ""
    verified: bool = False
    is_retweet: bool = False
    avatar_url: str = ""
    quote: TweetData | None = None


@dataclass(frozen=True)
class TweetFetchResult:
    """保留 HTTP 状态语义，便于区分删除和暂时失败。"""

    tweet: TweetData | None
    not_found: bool = False
    error: str = ""
