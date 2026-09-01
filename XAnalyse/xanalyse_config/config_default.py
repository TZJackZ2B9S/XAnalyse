"""XAnalyse 配置项。"""

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsListStrConfig,
    GsRepeatGroupConfig,
)

DEFAULT_PROMPT = (
    "翻译成简体中文，直接给出翻译结果，不要有多余输出不要修改标点符号，"
    "如果遇到网址或者空白内容请不要翻译，请翻译: {text}"
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
        desc="单个图片或视频超过此大小时跳过；填写 0 表示不限制。",
        data=256,
        max_value=4096,
    ),
    "convertGif": GsBoolConfig(
        title="无音轨视频转 GIF",
        desc="开启后将无音轨视频按 GIF 质量转换；关闭时洗白后按视频发送。",
        data=True,
    ),
    "gifQuality": GsStrConfig(
        title="GIF 合成质量",
        desc="无音轨视频按 GIF 处理时的帧率、尺寸和颜色质量。可选 low、medium、high。",
        data="medium",
        options=["low", "medium", "high"],
    ),
    "whe_translate": GsBoolConfig(
        title="启用推文翻译",
        desc="通过 OpenAI 兼容接口翻译推文正文和图片描述。",
        data=False,
    ),
    "apiKey": GsStrConfig(
        title="翻译 API Key",
        desc="DeepSeek 或其他 OpenAI 兼容翻译服务的 API Key。",
        data="",
        secret=True,
    ),
    "apiurl": GsStrConfig(
        title="翻译 API 地址",
        desc="OpenAI 兼容 API 根地址，例如 https://api.deepseek.com。",
        data="https://api.deepseek.com",
    ),
    "model": GsStrConfig(
        title="翻译模型",
        desc="翻译接口使用的模型名称。",
        data="deepseek-chat",
    ),
    "prompt": GsStrConfig(
        title="翻译提示词",
        desc="使用 {text} 表示待翻译内容。",
        data=DEFAULT_PROMPT,
    ),
    "translateRetries": GsIntConfig(
        title="翻译失败重试次数",
        desc="翻译接口失败时的最大尝试次数。",
        data=3,
        max_value=10,
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
        desc="X API、媒体和翻译请求使用的代理，例如 http://<host>:<port> 或 socks5://<host>:<port>；留空直连。",
        data="",
        secret=True,
    ),
}
