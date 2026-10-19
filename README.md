# Douyin API

基于 FastAPI 的视频、图集与实况资源解析服务，提供原生平台解析、统一媒体数据和流式下载。

## 功能

- 抖音原画探测、全部转码画质、实况图片/视频配对及作品统计。
- 小红书多编码画质与实况整理，快手原生响应及页面数据提取。
- 哔哩哔哩完整流、DASH 音轨、分 P 选择与多段资源处理。
- 备用接口按平台路由，统一时长、文件大小、作者、画质和错误结果。
- 下载支持平台 Referer、Range / 206、完整性检查、真实进度和取消。
- 图集 ZIP、音视频合并、多段拼接和未加密点播 HLS 合并。
- 服务器端媒体地址、跳转和资源上限校验；运行数据不进入版本库。

## 本地运行

需要 Python 3.9+。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python server.py
```

页面：<http://localhost:8848>。接口说明：<http://localhost:8848/docs>。

可选运行参数使用 `.env.example` 作为模板，本地填写后保存为 `.env`。不要提交实际会话、运行配置或下载文件。

音视频与 HLS 合并使用本地 FFmpeg。Linux 使用系统包管理器安装，并通过 `FFMPEG_PATH` 指定系统程序；Windows 可使用运行依赖提供的程序。当前下载任务状态位于进程内，部署时使用单 worker。

## 接口

```text
GET    /api/parse?url=<分享链接>
GET    /api/download?url=<媒体链接>&filename=<文件名>
POST   /api/download/jobs
GET    /api/download/jobs/{id}
DELETE /api/download/jobs/{id}
GET    /api/download/jobs/{id}/file
GET    /api/status
```

`/api/test` 和 `/api/debug` 默认关闭，只用于本地开发排查。

## 模块

| 文件 | 职责 |
|---|---|
| `server.py` | 请求入口、策略调度、下载任务接口 |
| `douyin_parser.py` | 抖音媒体、画质、实况及统计数据 |
| `bilibili_parser.py` | 哔哩哔哩作品、分 P、视频与音轨 |
| `platform_parsers.py` | 小红书、快手原生解析 |
| `public_api.py` / `media_normalizer.py` | 备用路由与统一字段适配 |
| `media_downloads.py` | 流式下载、校验、任务、打包与合并 |
| `cookie_manager.py` / `browser_parser.py` | 本地会话与浏览器备用策略 |
| `index.html` | 资源选择、下载进度、取消和结果展示 |

## 开发测试

```bash
pip install -r requirements-dev.txt
playwright install chromium
python -m pytest -q
```

测试包括原生响应、接口适配、下载任务、真实小型媒体合并和 Chromium 交互。Linux 环境安装浏览器时可使用 `playwright install --with-deps chromium`。

原理和测试记录见 [解析原理与开发测试](docs/解析原理与开发测试.md)，接口用法见 [使用说明](docs/使用说明.md)，运行说明见 [部署指南](docs/部署指南.md)。

## License

Apache License 2.0。已有组件的许可证与署名保留在对应源码及 [NOTICE](NOTICE) 中。
