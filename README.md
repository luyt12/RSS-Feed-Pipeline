# RSS Full-Text Email Pipeline

通用 RSS 全文抓取 → 邮件推送 Pipeline。

## 功能

- 支持配置多个 RSS feed（`config/feeds.json`）
- **中文 feed** → 提取全文 → 直接发邮件
- **外文 feed** → 提取全文 → 百度翻译 → 发邮件
- 每个 feed 单独一封邮件
- 自动去重（基于 `data/processed_urls.json`）
- 定时运行（北京时间每天 14:35）

## Feed 配置

编辑 `config/feeds.json`，添加/启用 feed：

```json
[
  {
    "name": "中国笔会",
    "url": "https://www.chinesepen.org/feed",
    "lang": "zh",
    "max_daily": 10,
    "enabled": true
  },
  {
    "name": "My Foreign Source",
    "url": "https://example.com/rss.xml",
    "lang": "en",
    "max_daily": 5,
    "enabled": true
  }
]
```

| 字段 | 说明 |
|------|------|
| `name` | Feed 名称（邮件标题用） |
| `url` | RSS feed URL |
| `lang` | `zh`=中文（直接发），`en` 或其他=外文（翻译后发） |
| `max_daily` | 每日最多处理篇数 |
| `enabled` | `true`=启用，`false`=跳过 |

## 添加新 Feed

1. 编辑 `config/feeds.json`，添加配置
2. Push 到 GitHub，自动触发 workflow

## GitHub Secrets

| Secret | 说明 |
|--------|------|
| `BAIDU_APPID` | 百度翻译 APPID（外文 feed 需要） |
| `BAIDU_API_KEY` | 百度翻译 API Key |
| `EMAIL_TO` | 收件邮箱 |
| `EMAIL_FROM` | 发件邮箱 |
| `SMTP_HOST` | SMTP 服务器 |
| `SMTP_PORT` | SMTP 端口（465 或 587） |
| `SMTP_USER` | SMTP 用户名 |
| `SMTP_PASS` | SMTP 密码 |

## 技术栈

- **全文提取**: [trafilatura](https://github.com/adbar/trafilatura) — 无需 API Key
- **RSS 解析**: feedparser
- **翻译**: 百度大模型文本翻译 API
- **邮件**: SMTP (AgentMail)