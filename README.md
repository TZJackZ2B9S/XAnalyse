# XAnalyse

GsCore 的 X/Twitter 推文解析插件。支持推文、用户主页、引用推文和媒体，卡片使用 Pillow 绘制。

## 安装

在 GsCore 的 `gsuid_core/plugins/` 目录执行：

```bash
git clone --depth 1 https://github.com/TZJackZ2B9S/XAnalyse.git XAnalyse
```

视频处理和 GIF 判断需要 `ffmpeg`、`ffprobe`。插件启动时会检查它们，缺少时仍可加载，但视频洗白和 GIF 转换不可用。

Debian/Ubuntu：

```bash
sudo apt update
sudo apt install -y ffmpeg
```

Docker 部署的 GsCore 要在容器内装，宿主机装了容器里也用不到：

```bash
docker exec -it <容器名> apt update
docker exec -it <容器名> apt install -y ffmpeg
```

容器重启后依然有效；重建容器则需要在镜像的 Dockerfile 里加上 `RUN apt-get update && apt-get install -y ffmpeg`。

## 使用

发送以下命令：

```text
twitter <X/Twitter 链接>
x <X/Twitter 链接>
```

也可以直接发送用户主页链接，解析该用户最新一条推文。开启 `detectXLinks` 后，普通消息中的 X/Twitter 链接也会自动处理。

`tt` 用于手动检查订阅更新。

## 配置

配置文件：`data/XAnalyse/config.json`

| 配置项 | 说明 |
| --- | --- |
| `enableScreenshot` | 是否发送 Pillow 卡片 |
| `detectXLinks` | 是否自动解析普通消息中的链接 |
| `proxy` | API 和媒体请求使用的代理，如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080` |
| `grokTranslation` | 是否使用 Grok 翻译；外层推文和引用推文都会尝试翻译 |
| `commentParsing` | 是否解析并在卡片右侧绘制评论区；默认关闭 |
| `convertGif` | 是否把无音轨视频转换为 GIF |
| `gifQuality` | GIF 质量：`low`、`medium`、`high` |
| `maxMediaSize` | 单个媒体大小上限，单位 MB；`0` 使用 512 MB 安全上限，避免异常媒体耗尽内存 |
| `fetchRetries` | API 请求失败后的重试次数 |
| `outputLogs` | 是否输出详细日志 |
| `account` / `platform` | 订阅推送使用的账号和平台 |
| `bloggers` | 订阅用户、推送群号和屏蔽词 |

订阅需要填写 `account`、`platform`，并在 `bloggers` 中配置用户和 `groupID`。

## 说明

- 图片和视频默认按原分辨率处理，不主动压缩；媒体超过 `maxMediaSize` 或 512 MB 安全上限时跳过。
- 图片、视频会进行洗白处理。多个媒体会以合并转发发送。
- X 的 GIF 通常以无音轨 MP4 返回，开启 `convertGif` 后会按 `gifQuality` 转为 GIF。
- 临时媒体只在处理期间使用，处理结束后会清理。
