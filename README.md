# Douyin API

一个基于 FastAPI 的短视频链接解析服务，支持抖音、小红书、快手等平台，并提供流式下载代理。

## 特性

- 抖音多策略解析：免 Cookie 详情优先，签名 API、Playwright 浏览器和公开 API 兜底
- 抖音原画入口与 CDN 探测、全部转码画质及备用链接、可选播放量
- 网页按画质分别下载，展示分辨率、帧率、编码、码率和文件大小
- 小红书和快手原生页面/API 提取
- 图集、视频元数据和无水印直链统一返回
- 下载代理带公网地址校验，避免把服务变成 SSRF 入口
- Cookie 通过环境变量或本地缓存配置，不进入版本库
- 自动生成 OpenAPI 文档：`/docs`

## 快速开始

需要 Python 3.9 或更高版本。

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python server.py
```

打开 <http://localhost:8848>，或访问 <http://localhost:8848/docs> 查看 API。

如需配置 Cookie，复制 `.env.example` 为 `.env` 并填写 `DOUYIN_COOKIE`。服务会自动读取该文件；也可以直接设置同名环境变量。服务还会在首次请求时尝试通过 Playwright 自动获取短期 Cookie。

## API 示例

```bash
curl "http://localhost:8848/api/parse?url=https://v.douyin.com/example/"
curl "http://localhost:8848/api/status"
```

`/api/test` 和 `/api/debug` 默认关闭。仅在本地排障时设置 `ENABLE_DEBUG_ENDPOINTS=1`。

## 项目结构

```text
.
├── server.py              # FastAPI 应用和路由
├── douyin_parser.py       # 抖音详情、转码画质及精选页播放量格式化
├── platform_parsers.py    # 小红书、快手解析器
├── browser_parser.py      # Playwright 浏览器解析器
├── cookie_manager.py      # Cookie 获取和缓存
├── abogus.py / xbogus.py  # 抖音签名算法
├── index.html             # 内置 Web 界面
├── tests/                 # 自动化测试
├── requirements.txt       # 运行依赖
└── docs/                  # 使用和部署文档
```

更多说明见 [docs/使用说明.md](docs/使用说明.md) 和 [docs/部署指南.md](docs/部署指南.md)。
抖音接口依据、字段含义与核验结果见 [docs/抖音解析接口.md](docs/抖音解析接口.md)。

## 测试

```bash
pip install -r requirements-dev.txt
playwright install chromium
python -m pytest -q
```

测试包含实际 Chromium 页面交互；Linux CI 会同时安装浏览器所需的系统依赖。

## 免责声明

请遵守各平台服务条款、版权和隐私法规。仅对你有权访问和下载的内容使用本项目。

## License

Apache License 2.0。详见 [LICENSE](LICENSE)。
