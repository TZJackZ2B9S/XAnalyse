# GsCore XAnalyse

运行在 [GsCore](https://github.com/Genshin-bots/gsuid-core) 上的 X/Twitter 推文解析与媒体转发插件。
插件通过 FxTwitter API 获取推文，使用 Pillow 绘制卡片；图片、视频和 GIF 会先做容器/元数据洗白，
再以 GsCore 消息段发送。两项及以上媒体会合并为一条转发节点。

## 功能

- `twitter <推文链接>` / `x <推文链接>`：解析正文、作者、头像、统计数据和媒体。
- `twitter <用户主页>`：请求 FxTwitter 用户时间线并解析该用户最新一条推文。
- 开启 `detectXLinks` 后，普通消息中的 X/Twitter 推文链接会自动解析。
- `enableScreenshot` 开启时用 Pillow 绘制 X 风格卡片，卡片内嵌媒体预览和有数据的统计图标。
- `tt` 手动检查订阅；首次检查只建立基线，之后的新推文通过 `gs_subscribe` 推送。
- X GIF 通常以无音轨 MP4 返回；插件会探测音轨并按 `gifQuality` 合成为 GIF。
- 视频选择 API 返回的最高码率 MP4，并修正 H.264 BT.709 色域标记。
- 支持 HTTP 和 SOCKS5 代理；API、媒体、头像和翻译请求共享连接池。

## 安装

将仓库目录复制到 GsCore 的 `gsuid_core/plugins/`，目录名保持为 `XAnalyse`：

```text
gsuid_core/gsuid_core/plugins/XAnalyse/
```

GsCore 会根据 `pyproject.toml` 安装依赖。系统还需要 `ffmpeg` 和 `ffprobe` 才能进行视频/图片洗白及 GIF 判断；找不到时会回退发送 API 返回的原始媒体。
单个媒体响应超过 `maxMediaSize` 会跳过；普通图片和视频低于该阈值不会缩放分辨率，GIF 合成尺寸由 `gifQuality` 决定。

## 配置

配置文件位于 `data/XAnalyse/config.json`，以下为 GsCore 插件配置：

| 字段 | 作用 |
| --- | --- |
| `account` / `platform` | 订阅推送目标机器人的账号和平台标识 |
| `enableScreenshot` | 是否发送 Pillow 推文卡片 |
| `updateInterval` / `fetchRetries` | 订阅轮询间隔和请求重试次数 |
| `maxMediaSize` | 单个媒体大小上限，单位 MB；超过后跳过，`0` 表示不限制 |
| `gifQuality` | 无音轨视频合成 GIF 的质量：`low`、`medium`、`high` |
| `whe_translate`、`apiKey`、`apiurl`、`model`、`prompt`、`translateRetries` | 可选的 OpenAI 兼容翻译 |
| `bloggers` | `id` 用户名、`groupID` 群号列表、`blacklist` 屏蔽词 |
| `outputLogs` / `detectXLinks` | 详细日志和自动链接检测开关 |
| `proxy` | 网络代理，例如 `http://<host>:<port>` 或 `socks5://<host>:<port>` |

订阅至少需要填写 `platform`、`account`，并为每个博主填写 `groupID`。配置变更后重载或重启插件即可；
订阅记录持久化在 GsCore 的订阅表，重连后会自动恢复。

## API 说明

单条推文使用 `https://api.fxtwitter.com/<user>/status/<id>`；用户主页使用
`https://api.fxtwitter.com/2/profile/<user>/statuses`。FxTwitter 受服务端限流和 Cloudflare
影响，若返回 HTTP 403/429，插件会记录明确错误并等待下一次检查；此类错误不是推文格式问题。

## 开发

```bash
uv run ruff check XAnalyse
uv run python -m compileall -q XAnalyse
```

插件采用 GsCore 推荐的嵌套结构、类型化配置、异步 HTTP 和 Pillow 渲染；运行时数据写入 GsCore
的数据目录，不会写入仓库。
