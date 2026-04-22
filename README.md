# RSS Full-Text Email Pipeline

通用 RSS 全文抓取 → 翻译 → 邮件推送 Pipeline。

## 功能

- 支持配置多个 RSS feed（`config/feeds.json`）
- **中文 feed** → 提取全文 → 直接发邮件
- **外文 feed** → 提取全文 → 百度翻译 → 发邮件
- 每个 feed 单独一封邮件
- 自动去重（基于 `data/processed_urls.json`）
- 定时运行（北京时间每天 14:35）

## Feed 配置

编辑 `config/feeds.json`：

```json
[
  {
    "name": "XXX",
    "url": "https://www.XXX.org/feed",
    "lang": "zh",
    "max_daily": 10,
    "enabled": true
  },
  {
    "name": "YYYAffairs",
    "url": "https://www.YYY.com/rss.xml",
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
| `lang` | 语言 `zh`（中文）或 `en`（外文，自动翻译） |
| `max_daily` | 每日最多处理篇数 |
| `enabled` | 是否启用 |

## 添加新 Feed 步骤

1. 编辑 `config/feeds.json`，添加新 feed 配置
2. Push 到 GitHub（手动触发或等次日定时运行）

## GitHub Secrets 配置

| Secret | 说明 |
|--------|------|
| `BAIDU_APPID` | 百度翻译 APPID |
| `BAIDU_API_KEY` | 百度翻译 API Key |
| `EMAIL_TO` | 收件邮箱 |
| `EMAIL_FROM` | 发件邮箱 |
| `SMTP_HOST` | SMTP 服务器 |
| `SMTP_PORT` | SMTP 端口 |
| `SMTP_USER` | SMTP 用户名 |
| `SMTP_PASS` | SMTP 密码 |

## 本地测试

```bash
pip install -r requirements.txt
python daily_task.py
```

## 技术栈

- **全文提取**: [trafilatura](https://github.com/adbar/trafilatura) — 无需 API Key 的文章提取库
- **RSS 解析**: feedparser
- **翻译**: 百度大模型文本翻译 API（Bearer Token）
- **邮件**: SMTP (AgentMail)
