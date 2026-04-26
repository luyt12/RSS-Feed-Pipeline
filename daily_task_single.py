#!/usr/bin/env python3
"""
单源 RSS 处理脚本 - 从环境变量读取 feed 配置
对齐 daily_task.py 逻辑：历史文章回退、精美邮件格式、翻译优化
"""
import os
import json
import feedparser
import requests
import time
import trafilatura
import re
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import ssl

# 从环境变量读取配置
FEED_NAME = os.environ.get('FEED_NAME', '')
FEED_URL = os.environ.get('FEED_URL', '')
FEED_LANG = os.environ.get('FEED_LANG', 'en')
MAX_DAILY = int(os.environ.get('MAX_DAILY', '5'))
SKIP_TRAFILATURA = os.environ.get('SKIP_TRAFILATURA', 'false').lower() == 'true'

# SMTP 配置
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.agentmail.to')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM', '')
EMAIL_TO = os.environ.get('EMAIL_TO', '')

# Kimi API 配置
KIMI_API_KEY = os.environ.get('KIMI_API_KEY', '')
KIMI_API_URL = os.environ.get('KIMI_API_URL', 'https://integrate.api.nvidia.com/v1/chat/completions')

# 路径配置
DATA_DIR = 'data'
PROCESSED_URLS_FILE = os.path.join(DATA_DIR, 'processed_urls.json')

# Kimi 翻译 Prompt（与 daily_task.py 相同）
KIMI_PROMPT = """You are a professional translator and editor. Please complete the following task on the article below:

## Task: Extract and Summarize
Extract key points from the English article and write a Chinese summary with these requirements:
1. No need to translate the full text - extract key points directly
2. High information density - cover main points, background, and significance  
3. Keep key details (names, institutions, data)
4. Word count rules (based on original English word count):
   - > 8000 words: Chinese summary ~4000 characters
   - > 4000 words: Chinese summary ~3000 characters
   - > 2000 words: Chinese summary ~2000 characters
   - ≤ 2000 words: Chinese summary ~80% of original word count in characters
5. Concise style, avoid "This article discusses..." filler
6. Stay neutral on controversial topics

## Output Format
Output the Chinese summary directly, no introductions or meta-comments."""


def load_processed_urls():
    """加载已处理的 URL"""
    if os.path.exists(PROCESSED_URLS_FILE):
        with open(PROCESSED_URLS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_processed_urls(processed_urls):
    """保存已处理的 URL"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROCESSED_URLS_FILE, 'w', encoding='utf-8') as f:
        json.dump(processed_urls, f, ensure_ascii=False, indent=2)


def fetch_feed(url):
    """获取 RSS feed"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return feedparser.parse(response.content)
    except Exception as e:
        print(f"获取 feed 失败: {e}")
        return None


def extract_content(url):
    """使用 trafilatura 提取全文"""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ''
        text = trafilatura.extract(downloaded,
                                   include_comments=False,
                                   include_tables=True,
                                   output_format='markdown',
                                   favor_precision=True)
        return text if text and len(text) >= 200 else ''
    except Exception as e:
        print(f"提取全文失败: {e}")
        return ''


def translate_with_kimi(text, max_retries=5):
    """使用 Kimi API 翻译+摘要"""
    if not text or not KIMI_API_KEY:
        return text

    headers = {
        'Authorization': f'Bearer {KIMI_API_KEY}',
        'Content-Type': 'application/json'
    }

    payload = {
        'model': 'moonshotai/kimi-k2.5',
        'messages': [
            {'role': 'system', 'content': KIMI_PROMPT},
            {'role': 'user', 'content': text}
        ],
        'temperature': 0.7,
        'max_tokens': 16000
    }

    for attempt in range(max_retries):
        try:
            print(f"Kimi 翻译请求 (attempt {attempt + 1}/{max_retries})...")
            response = requests.post(KIMI_API_URL, headers=headers, json=payload, timeout=300)
            response.raise_for_status()
            result = response.json()
            if result.get('choices') and result['choices'][0]:
                return result['choices'][0]['message']['content']
            else:
                print(f"API 响应错误: {result}")
                if attempt < max_retries - 1:
                    wait = 30 * (2 ** attempt)
                    print(f"等待 {wait}s 后重试...")
                    time.sleep(wait)
        except Exception as e:
            print(f"翻译失败 (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(30 * (2 ** attempt))
    return text


def build_html(feed_name, articles, is_translated, is_today):
    """构建精美 HTML 邮件"""
    hdr_bg = '#1b4332' if not is_translated else '#1a237e'
    badge_bg = '#40916c' if not is_translated else '#1565c0'
    badge_txt = '🌐 原文' if not is_translated else '🔄 中文翻译'
    date_label = '今日' if is_today else '历史'

    body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body{{font-family:-apple-system,'Microsoft YaHei',Arial,sans-serif;margin:0;padding:12px;background:#f0f2f5}}
  .wrap{{max-width:100%;margin:0 auto;background:white;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
  .hdr{{background:{hdr_bg};color:white;padding:16px 16px}}
  .badge{{display:inline-block;background:{badge_bg};color:white;padding:3px 10px;border-radius:10px;font-size:11px;margin-bottom:6px}}
  .hdr h1{{margin:0;font-size:18px;font-weight:700}}
  .hdr .sub{{opacity:0.8;margin-top:4px;font-size:12px}}
  .bar{{background:#f8f9fa;padding:10px 16px;font-size:12px;color:#555;border-bottom:1px solid #eee}}
  .art{{padding:14px 16px;border-bottom:1px solid #f0f0f0}}
  .art:last-child{{border-bottom:none}}
  .art h2{{font-size:17px;font-weight:700;color:#1a1a1a;margin:0 0 8px 0;line-height:1.4;border-left:3px solid #40916c;padding-left:10px;background:#f0fdf4;padding:6px 10px;border-radius:0 4px 4px 0}}
  .art h2 a{{color:inherit;text-decoration:none}}
  .meta{{font-size:11px;color:#aaa;margin-bottom:8px}}
  .txt{{font-size:14px;line-height:1.7;color:#333}}
  .ft{{padding:12px 16px;background:#f8f9fa;text-align:center;font-size:11px;color:#bbb}}
  @media (max-width:480px){{
    body{{padding:8px}}
    .hdr{{padding:12px 12px}}
    .hdr h1{{font-size:16px}}
    .art{{padding:12px}}
    .art h2{{font-size:15px}}
    .txt{{font-size:13px;line-height:1.6}}
  }}
</style>
</head><body>
<div class="wrap">
  <div class="hdr">
    <div class="badge">{badge_txt}</div>
    <h1>{feed_name}</h1>
    <div class="sub">{len(articles)} 篇文章 · {datetime.now().strftime('%Y-%m-%d')} 自动推送</div>
  </div>
  <div class="bar">📡 {date_label}文章 <strong>{len(articles)}</strong> 篇</div>
"""

    for i, art in enumerate(articles, 1):
        title = art.get('title', '无标题')
        link = art.get('link', '#')
        pub = art.get('published', '')
        content = art.get('content') or art.get('summary', '（无内容）')
        # HTML 转义
        title_esc = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        content = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        content = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
        body += f"""
  <div class="art">
    <h2><span style="color:#40916c;margin-right:6px">{i}.</span><a href="{link}">{title_esc}</a></h2>
    <div class="meta">📅 {pub}</div>
    <div class="txt"><p>{content}</p></div>
  </div>
"""

    body += f"""
  <div class="ft">AI 助手自动抓取推送</div>
</div></body></html>"""
    return body


def send_email(articles, feed_name, is_translated, is_today):
    """发送邮件"""
    if not articles:
        print(f"[{feed_name}] 无新文章，不发送邮件")
        return False

    prefix = '🔄' if is_translated else '🌐'
    date_label = '今日' if is_today else '历史'
    # 邮件主题包含首篇文章标题
    first_title = articles[0].get('title', '')[:50] if articles else ''
    title_suffix = f" · {first_title}..." if len(articles) > 1 else (f" · {first_title}" if first_title else "")
    subject = f"{prefix} [{feed_name}] {date_label}更新{title_suffix} · {len(articles)} 篇"

    # 纯文本版本
    text_content = f"{feed_name} - {date_label}更新\n共 {len(articles)} 篇文章\n\n"
    for i, a in enumerate(articles, 1):
        title = a.get('title', '无标题')
        link = a.get('link', '#')
        content = a.get('content') or a.get('summary', '')
        text_content += f"\n{'='*60}\n📰 {i}. {title}\n🔗 {link}\n\n{content[:1000]}\n"

    # HTML 版本
    html_content = build_html(feed_name, articles, is_translated, is_today)

    # 创建邮件
    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg['Subject'] = subject
    msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    # 发送邮件（自动判断 SSL/STARTTLS）
    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls(context=ctx)
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        print(f"[{feed_name}] 邮件发送成功: {subject}")
        return True
    except Exception as e:
        print(f"[{feed_name}] 邮件发送失败: {e}")
        return False


def main():
    print(f"开始处理 feed: {FEED_NAME}")
    print(f"URL: {FEED_URL}")
    print(f"语言: {FEED_LANG}")
    print(f"每日上限: {MAX_DAILY}")
    print(f"跳过全文提取: {SKIP_TRAFILATURA}")

    # 加载已处理的 URL
    processed_urls = load_processed_urls()
    feed_processed = set(processed_urls.get(FEED_NAME, []))

    # 获取 feed
    feed = fetch_feed(FEED_URL)
    if not feed or not feed.entries:
        print(f"[{FEED_NAME}] 无法获取 feed 或无文章")
        return

    # 解析所有文章
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    all_articles = []
    today_articles = []

    for entry in feed.entries:
        link = entry.get('link', '')
        if not link:
            continue

        # 解析发布日期
        published = ''
        if entry.get('published_parsed'):
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            published = dt.strftime('%Y-%m-%d')
        elif entry.get('updated_parsed'):
            dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
            published = dt.strftime('%Y-%m-%d')

        # 提取 RSS 摘要
        content = ''
        if hasattr(entry, 'content') and entry.content:
            content = entry.content[0].value
        elif entry.get('summary'):
            content = entry.summary
        elif entry.get('description'):
            content = entry.description

        # 清理 HTML 标签
        content = re.sub(r'<[^>]+>', '', content).strip()

        article = {
            'title': entry.get('title', '无标题'),
            'link': link,
            'published': published,
            'summary': content[:500] if content else ''
        }

        all_articles.append(article)
        if published == today_str:
            today_articles.append(article)

    print(f"[{FEED_NAME}] RSS 共 {len(all_articles)} 篇文章，今日 {len(today_articles)} 篇")

    # 优先使用今日文章，无则回退到历史未处理文章
    if today_articles:
        candidates = today_articles
        is_today = True
        print(f"  → 使用今日文章: {len(candidates)} 篇")
    else:
        candidates = [a for a in all_articles if a['link'] not in feed_processed]
        is_today = False
        if candidates:
            print(f"  → 今日无文章，回退到历史未处理: {len(candidates)} 篇")
        else:
            print(f"  → 无新文章")
            return

    # 限制数量
    candidates = candidates[:MAX_DAILY]

    # 处理每篇文章
    new_articles = []
    for art in candidates:
        link = art['link']
        if link in feed_processed:
            continue

        title = art['title']
        rss_content = art.get('summary', '')
        rss_len = len(rss_content)

        # 提取全文（动态判断）
        if SKIP_TRAFILATURA:
            print(f"  [跳过全文] {title[:50]}...")
            content = rss_content
        elif rss_len > 2000:
            print(f"  RSS 已含全文 ({rss_len} 字符)，直接使用")
            content = rss_content
        else:
            print(f"  提取全文: {title[:50]}... (RSS 仅 {rss_len} 字符)")
            full_content = extract_content(link)
            if full_content and len(full_content) > rss_len:
                content = full_content
                print(f"    ✓ 抓取成功: {len(full_content)} 字符")
            else:
                content = rss_content
                print(f"    ✗ 全文提取失败，使用 RSS 摘要")

        # 翻译（仅非中文 feed，只翻译正文，不翻译标题）
        if FEED_LANG != 'zh' and content:
            print(f"  翻译正文: {title[:50]}...")
            # 截断过长内容，避免 API 超时
            if len(content) > 40000:
                content = content[:40000]
                print(f"    ⚠ 原文过长，截取前40000字符")
            translated = translate_with_kimi(content)
            if translated and translated != content:
                content = translated
                print(f"    ✓ 翻译完成")
            else:
                print(f"    ✗ 翻译失败，使用原文")

        new_articles.append({
            'title': title,
            'link': link,
            'published': art.get('published', ''),
            'content': content,
            'summary': art.get('summary', '')
        })

        feed_processed.add(link)

    print(f"[{FEED_NAME}] 新文章: {len(new_articles)} 篇")

    # 发送邮件
    is_translated = FEED_LANG != 'zh'
    if new_articles:
        send_email(new_articles, FEED_NAME, is_translated, is_today)

    # 保存已处理的 URL
    processed_urls[FEED_NAME] = list(feed_processed)
    save_processed_urls(processed_urls)
    print(f"[{FEED_NAME}] 处理完成")


if __name__ == '__main__':
    main()
