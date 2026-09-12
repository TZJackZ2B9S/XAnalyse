"""XAnalyse 配置项。"""

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsListStrConfig,
    GsRepeatGroupConfig,
)

CONFIG_DEFAULT: dict[str, GSC] = {
    "account": GsStrConfig(
        title="机器人账号",
        desc="用于订阅推送的机器人账号（bot_self_id）；仅手动解析时可留空。",
        data="",
    ),
    "platform": GsStrConfig(
        title="机器人平台",
        desc="用于订阅推送的平台标识，例如 onebot；仅手动解析时可留空。",
        data="",
    ),
    "enableScreenshot": GsBoolConfig(
        title="推文卡片",
        desc="开启后使用 Pillow 绘制 X 风格推文卡片；关闭可减少处理开销。",
        data=True,
    ),
    "updateInterval": GsIntConfig(
        title="检查推文更新间隔（分钟）",
        desc="订阅轮询间隔；每增加两个订阅建议增加约一分钟。",
        data=5,
        max_value=1440,
    ),
    "fetchRetries": GsIntConfig(
        title="抓取失败重试次数",
        desc="请求推文或媒体失败时的最大尝试次数。",
        data=3,
        max_value=10,
    ),
    "maxMediaSize": GsIntConfig(
        title="媒体大小拦截（MB）",
        desc="单个图片或视频超过此大小时跳过；填写 0 使用 512 MB 安全上限，避免异常媒体耗尽内存。",
        data=256,
        max_value=4096,
    ),
    "convertGif": GsBoolConfig(
        title="GIF 媒体转为 GIF",
        desc="开启后把 API 标记为 GIF 的媒体按 GIF 质量合成；关闭时洗白后按视频发送。",
        data=True,
    ),
    "gifQuality": GsStrConfig(
        title="GIF 合成质量",
        desc="GIF 媒体合成时的帧率、尺寸和颜色质量。可选 low、medium、high。",
        data="medium",
        options=["low", "medium", "high"],
    ),
    "videoSendType": GsStrConfig(
        title="视频发送方式",
        desc=(
            "base64：由 GsCore 编码后发送，兼容性最好；"
            "file：视频落盘后以 file:// 发送，可避免 base64 内存放大，"
            "但需要 Bot 端与 Core 能访问同一路径。"
        ),
        data="base64",
        options=["base64", "file"],
    ),
    "grokTranslation": GsBoolConfig(
        title="是否开启 Grok 翻译",
        desc="请求 FxTwitter v2 的 Grok 翻译；关闭时保留推文原文。",
        data=False,
    ),
    "commentParsing": GsBoolConfig(
        title="开启评论区解析",
        desc="开启后请求推文评论并绘制到卡片右侧；关闭可减少 API 请求和内存占用。",
        data=False,
    ),
    "bloggers": GsRepeatGroupConfig(
        title="订阅的博主列表",
        desc="每项填写 X 用户名、推送群号和可选屏蔽词。",
        data=[],
        template={
            "id": GsStrConfig(
                title="Twitter 博主用户名",
                desc="填写 @ 后的用户名，不要包含 @。",
                data="",
            ),
            "groupID": GsListStrConfig(
                title="推送群号",
                desc="需要推送的群号列表。",
                data=[],
            ),
            "blacklist": GsListStrConfig(
                title="屏蔽词",
                desc="推文正文包含任一词时不推送。",
                data=[],
            ),
        },
    ),
    "outputLogs": GsBoolConfig(
        title="日志调试模式",
        desc="开启后记录请求、媒体处理和链接检测的详细日志。",
        data=True,
    ),
    "detectXLinks": GsBoolConfig(
        title="自动检测 X/Twitter 链接",
        desc="在普通消息中发现 X/Twitter 推文链接时自动解析。",
        data=True,
    ),
    "proxy": GsStrConfig(
        title="网络代理",
        desc="X API、媒体和头像请求使用的代理，例如 http://<host>:<port> 或 socks5://<host>:<port>；留空直连。",
        data="",
        secret=True,
    ),
}
