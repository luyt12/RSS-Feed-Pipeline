#!/usr/bin/env python3
"""
每日任务主流程：
1. 读取 config/feeds.json 中所有启用的 feed
2. 对每个 feed：
   a. 抓取当日 RSS 文章
   b. 过滤掉已处理过的文章
   c. 提取全文
   d. 中文 feed → 直接发邮件；外文 feed → 翻译后发邮件
3. 更新 processed_urls.json
4. 提交变更到 GitHub
"""

import json
import logging
import sys
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timezone
import feedparser
import trafilatura
import re
import time
import urllib.request
import urllib.parse

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ─── 环境变量 ───
SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM', '')
EMAIL_TO = os.environ.get('EMAIL_TO', '')
BAIDU_APPID = os.environ.get('BAIDU_APPID', '')
BAIDU_API_KEY = os.environ.get('BAIDU_API_KEY', '')

STATE_FILE = Path('data/processed_urls.json')


def load_feeds():
    """加载 feeds.json 配置"""
    config_file = Path('config/feeds.json')
    if not config_file.exists():
        logger.error(f"配置文件不存在: {config_file}")
        sys.exit(1)

    with open(config_file, 'r', encoding='utf-8') as f:
        feeds = json.load(f)

    enabled = [f for f in feeds if f.get('enabled', True)]
    logger.info(f"共加载 {len(feeds)} 个 feed，{len(enabled)} 个已启用")
    return enabled


def load_state() -> dict:
    """加载已处理状态"""
    if STATE_FILE.exists():
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """保存已处理状态"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_today_articles(url: str, max_items: int = 10):
    """抓取 RSS，返回当日文章列表"""
    logger.info(f"抓取 RSS: {url}")
    feed = feedparser.parse(url)

    if feed.bozo and feed.bozo_exception:
        logger.warning(f"RSS 解析异常: {feed.bozo_exception}")

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    articles = []

    for entry in feed.entries[:max_items]:
        # 解析发布时间
        published = None
        if entry.get('published_parsed'):
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            published = dt.strftime('%Y-%m-%d')
        elif entry.get('updated_parsed'):
            dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
            published = dt.strftime('%Y-%m-%d')

        if not published:
            logger.warning(f"  跳过无发布时间: {entry.get('title', '')[:50]}")
            continue

        if published != today:
            continue

        # 提取内容
        content = ''
        if hasattr(entry, 'content') and entry.content:
            content = entry.content[0].value
        elif getattr(entry, 'summary', None):
            content = entry.summary
        elif getattr(entry, 'description', None):
            content = entry.description

        content = re.sub(r'<[^>]+>', '', content).strip()

        articles.append({
            'title': entry.get('title', 'NO_TITLE'),
            'link': entry.get('link', ''),
            'published': published,
            'summary': content[:500]
        })
        logger.info(f"  ✓ [{published}] {articles[-1]['title'][:60]}")

    logger.info(f"今日新文章: {len(articles)} 篇")
    return articles


def extract_full_text(url: str):
    """使用 trafilatura 提取全文"""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None

        text = trafilatura.extract(downloaded,
                                    include_comments=False,
                                    include_tables=True,
                                    output_format='markdown',
                                    favor_precision=True)
        if not text or len(text) < 200:
            return None
        return text
    except Exception as e:
        logger.warning(f"  提取失败 [{url}]: {e}")
        return None


# ─── 百度翻译（Bearer Token）───
_ACCESS_TOKEN = None
_TOKEN_EXPIRE = 0


def _get_access_token():
    global _ACCESS_TOKEN, _TOKEN_EXPIRE
    if not BAIDU_APPID or not BAIDU_API_KEY:
        return None

    now = time.time()
    if _ACCESS_TOKEN and now < _TOKEN_EXPIRE:
        return _ACCESS_TOKEN

    logger.info("获取百度 Access Token...")
    params = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': BAIDU_APPID,
        'client_secret': BAIDU_API_KEY
    })
    req = urllib.request.Request(f"https://aip.baidubbs.com/oauth/2.0/token?{params}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    if 'access_token' not in data:
        logger.error(f"Token 获取失败: {data}")
        return None

    _ACCESS_TOKEN = data['access_token']
    _TOKEN_EXPIRE = now + 7100
    logger.info("Token 获取成功")
    return _ACCESS_TOKEN


def _translate_text(text: str, token: str) -> str:
    """翻译单段文本"""
    if len(text) > 5500:
        mid = len(text) // 2
        split = text.rfind('。', max(0, mid - 500), mid + 500)
        if split == -1:
            split = mid
        return _translate_text(text[:split + 1], token) + _translate_text(text[split + 1:], token)

    payload = json.dumps({'text': text, 'from': 'auto', 'to': 'zh'}).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    req = urllib.request.Request(
        'https://aip.baidubbs.com/ait/api/aiTextTranslate',
        data=payload, headers=headers, method='POST'
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())

    err = result.get('error_code') or result.get('code')
    if err in ('52001', '52002', '52003', '54001', '54000'):
        logger.error(f"百度翻译失败 [{err}]: {result}")
        return ''
    return result.get('data', {}).get('translateResult', '') or text


def translate_articles(articles: list, feed_name: str) -> list:
    """翻译文章列表中的全文"""
    token = _get_access_token()
    if not token:
        logger.warning("百度翻译未配置，跳过翻译")
        return articles

    for art in articles:
        content = art.get('content', '')
        if not content:
            continue

        logger.info(f"翻译: {art['title'][:50]}...")
        translated = _translate_text(content, token)
        if translated:
            art['content'] = translated
            logger.info(f"  ✓ 翻译完成 ({len(translated)} 字符)")
        else:
            logger.warning(f"  ✗ 翻译失败，保留原文")

        time.sleep(0.5)  # 避免请求过快

    return articles


def build_html(feed_name: str, articles: list, is_translated: bool):
    """构建 HTML 邮件"""
    hdr_bg = '#1b4332' if not is_translated else '#1a237e'
    badge_bg = '#40916c' if not is_translated else '#1565c0'
    badge_txt = '🌐 英文原文' if not is_translated else '🔄 中文翻译'

    body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{font-family:-apple-system,'Microsoft YaHei',Arial,sans-serif;margin:0;padding:20px;background:#f0f2f5}}
  .wrap{{max-width:720px;margin:0 auto;background:white;border-radius:10px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.08)}}
  .hdr{{background:{hdr_bg};color:white;padding:28px 32px}}
  .badge{{display:inline-block;background:{badge_bg};color:white;padding:4px 12px;border-radius:12px;font-size:11px;margin-bottom:8px}}
  .hdr h1{{margin:0;font-size:22px;font-weight:700}}
  .hdr .sub{{opacity:0.8;margin-top:6px;font-size:13px}}
  .bar{{background:#f8f9fa;padding:14px 32px;font-size:13px;color:#555;border-bottom:1px solid #eee}}
  .art{{padding:22px 32px;border-bottom:1px solid #f0f0f0}}
  .art:last-child{{border-bottom:none}}
  .art h2{{font-size:16px;font-weight:600;color:#1a1a1a;margin:0 0 8px 0;line-height:1.4}}
  .art h2 a{{color:inherit;text-decoration:none}}
  .meta{{font-size:11px;color:#aaa;margin-bottom:12px}}
  .txt{{font-size:14px;line-height:1.85;color:#333}}
  .txt p{{margin:0 0 10px 0}}
  .ft{{padding:14px 32px;background:#f8f9fa;text-align:center;font-size:11px;color:#bbb}}
</style>
</head><body>
<div class="wrap">
  <div class="hdr">
    <div class="badge">{badge_txt}</div>
    <h1>{feed_name}</h1>
    <div class="sub">{len(articles)} 篇文章 · {datetime.now().strftime('%Y-%m-%d')} 自动推送</div>
  </div>
  <div class="bar">📡 今日新文章 <strong>{len(articles)}</strong> 篇</div>
"""

    for i, art in enumerate(articles, 1):
        title = art.get('title', '无标题')
        link = art.get('link', '#')
        pub = art.get('published', '')
        content = (art.get('content') or art.get('summary', '（无内容）'))
        # markdown → HTML
        content = (content.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
                   .replace('\n\n', '</p><p>').replace('\n', '<br>'))
        body += f"""
  <div class="art">
    <h2><a href="{link}">{i}. {title}</a></h2>
    <div class="meta">📅 {pub}</div>
    <div class="txt"><p>{content}</p></div>
  </div>
"""

    body += f"""
  <div class="ft">AI 助手自动抓取推送</div>
</div></body></html>"""
    return body


def send_mail(feed_name: str, articles: list, is_translated: bool) -> bool:
    """发送邮件"""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO]):
        logger.error("SMTP 环境变量未配置")
        return False

    if not articles:
        logger.warning("没有文章，跳过")
        return False

    prefix = '🔄' if is_translated else '🌐'
    subject = f"{prefix} [{feed_name}] 今日更新 · {len(articles)} 篇"

    plain = '\n\n'.join(
        f"{i}. {a.get('title','')}\n{a.get('link','')}\n{a.get('content','')[:500]}"
        for i, a in enumerate(articles, 1)
    )
    html = build_html(feed_name, articles, is_translated)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg.attach(MIMEText(plain, 'plain', 'utf-8'))
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

        logger.info(f"✅ 邮件已发送: {subject}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def main():
    logger.info("=" * 50)
    logger.info("RSS Full-Text Email Pipeline 开始")
    logger.info("=" * 50)

    feeds = load_feeds()
    state = load_state()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    total_sent = 0

    for feed in feeds:
        name = feed.get('name', '未知源')
        url = feed.get('url', '')
        lang = feed.get('lang', 'zh')
        max_daily = feed.get('max_daily', 10)

        if not url:
            logger.warning(f"Feed 无 URL，跳过: {name}")
            continue

        logger.info(f"\n{'─' * 40}")
        logger.info(f"处理 Feed: {name} (语言={lang})")

        # 1. 抓取 RSS
        articles = fetch_today_articles(url, max_daily)
        if not articles:
            logger.info(f"  没有今日新文章，跳过")
            continue

        # 2. 去重
        processed = set(state.get(url, []))
        new_articles = [a for a in articles if a['link'] not in processed]
        skipped = len(articles) - len(new_articles)
        if skipped:
            logger.info(f"  跳过 {skipped} 篇已处理文章")

        if not new_articles:
            logger.info("  所有文章均已处理，跳过")
            continue

        # 3. 提取全文
        for art in new_articles:
            logger.info(f"  提取全文: {art['title'][:50]}...")
            text = extract_full_text(art['link'])
            if text:
                art['content'] = text
                logger.info(f"    ✓ {len(text)} 字符")
            else:
                art['content'] = art.get('summary', '')
                logger.warning(f"    ✗ 全文提取失败，使用摘要")

        # 4. 翻译（非中文 feed）
        is_translated = False
        if lang != 'zh':
            logger.info(f"  → 非中文 Feed，执行翻译...")
            new_articles = translate_articles(new_articles, name)
            is_translated = True

        # 5. 发送邮件（每 feed 一封）
        if send_mail(name, new_articles, is_translated):
            # 更新状态
            new_links = set(a['link'] for a in new_articles)
            state[url] = list(processed | new_links)
            save_state(state)
            logger.info(f"  ✅ {name} 处理完成，{len(new_articles)} 篇已发送")
            total_sent += 1
        else:
            logger.error(f"  ❌ {name} 邮件发送失败")

    logger.info(f"\n{'=' * 50}")
    logger.info(f"处理完成，共发送 {total_sent} 封邮件")
    logger.info("=" * 50)

    if total_sent > 0:
        logger.info("准备提交变更...")
        import subprocess
        subprocess.run(['git', 'config', 'user.name', 'GitHub Actions'], check=False)
        subprocess.run(['git', 'config', 'user.email', 'actions@github.com'], check=False)
        subprocess.run(['git', 'add', 'data/processed_urls.json'], check=False)
        r = subprocess.run(['git', 'diff', '--staged', '--quiet'], capture_output=True)
        if r.returncode != 0:
            subprocess.run(['git', 'commit', '-m', f'Update processed_urls [{today}]'], check=False)
            subprocess.run(['git', 'push'], check=False)
            logger.info("✅ 已提交并推送状态更新")
        else:
            logger.info("没有变更需要提交")


if __name__ == '__main__':
    main()
