#!/usr/bin/env python3
"""
单源 RSS 处理脚本 - 从环境变量读取 feed 配置
"""
import os
import json
import feedparser
import requests
import trafilatura
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
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = trafilatura.extract(response.content, include_comments=False)
        return content if content else ''
    except Exception as e:
        print(f"提取全文失败: {e}")
        return ''


def translate_with_kimi(text, max_retries=3):
    """使用 Kimi API 翻译"""
    if not text or not KIMI_API_KEY:
        return text

    prompt = f"""请将以下英文内容翻译成中文，保持原文的结构和语气：

{text[:4000]}

只输出翻译结果，不要添加任何解释或评论。"""

    headers = {
        'Authorization': f'Bearer {KIMI_API_KEY}',
        'Content-Type': 'application/json'
    }

    payload = {
        'model': 'moonshotai/kimi-k2.5',
        'messages': [
            {'role': 'user', 'content': prompt}
        ],
        'temperature': 0.3,
        'max_tokens': 4096
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(KIMI_API_URL, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            result = response.json()
            return result['choices'][0]['message']['content']
        except Exception as e:
            print(f"翻译失败 (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                return text

    return text


def send_email(articles, feed_name):
    """发送邮件"""
    if not articles:
        print(f"[{feed_name}] 无新文章，不发送邮件")
        return False

    subject = f"[{feed_name}] RSS 订阅 - {datetime.now().strftime('%Y-%m-%d')}"

    # 构建 HTML 正文
    html_parts = [f"<h1>{feed_name} - 今日更新</h1>"]
    html_parts.append(f"<p>共 {len(articles)} 篇文章</p><hr>")

    for i, article in enumerate(articles, 1):
        html_parts.append(f"<h2>{i}. {article['title']}</h2>")
        html_parts.append(f"<p><a href=\"{article['link']}\">原文链接</a></p>")
        html_parts.append(f"<div style=\"margin: 20px 0;\">{article['content']}</div>")
        html_parts.append("<hr>")

    html_content = '\n'.join(html_parts)
    text_content = f"{feed_name} - 今日更新\n共 {len(articles)} 篇文章\n\n" + \
                   '\n\n'.join([f"{i}. {a['title']}\n{a['link']}" for i, a in enumerate(articles, 1)])

    # 创建邮件
    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg['Subject'] = subject
    msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    # 发送邮件
    try:
        ctx = ssl.create_default_context()
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
    print(f"跳过 trafilatura: {SKIP_TRAFILATURA}")

    # 加载已处理的 URL
    processed_urls = load_processed_urls()
    feed_processed = processed_urls.get(FEED_NAME, [])

    # 获取 feed
    feed = fetch_feed(FEED_URL)
    if not feed or not feed.entries:
        print(f"[{FEED_NAME}] 无法获取 feed 或无文章")
        return

    # 筛选新文章
    today = datetime.now(timezone.utc).date()
    new_articles = []

    for entry in feed.entries:
        link = entry.get('link', '')
        if not link or link in feed_processed:
            continue

        # 检查发布日期（可选：只处理今天的文章）
        published = entry.get('published_parsed')
        if published:
            pub_date = datetime(*published[:6], tzinfo=timezone.utc).date()
            # 如果不是今天的文章，跳过（可配置为处理所有未处理的文章）

        if len(new_articles) >= MAX_DAILY:
            break

        # 提取内容（动态判断：RSS 有全文则用，没有再抓取）
        title = entry.get('title', '无标题')
        summary = entry.get('summary', '') or entry.get('description', '')
        rss_len = len(summary)
        
        # 检查 RSS 是否包含全文（>2000字符认为是全文）
        if rss_len > 2000:
            content = summary
            print(f"[{FEED_NAME}] RSS 已含全文 ({rss_len} 字符)，直接使用")
        else:
            # RSS 没有全文，用 trafilatura 抓取
            print(f"[{FEED_NAME}] RSS 仅 {rss_len} 字符，尝试抓取全文...")
            full_content = extract_content(link)
            if full_content and len(full_content) > rss_len:
                content = full_content
                print(f"[{FEED_NAME}] 抓取成功: {len(full_content)} 字符")
            else:
                content = summary
                print(f"[{FEED_NAME}] 抓取失败，使用 RSS 摘要")

        # 翻译（非中文内容）
        if FEED_LANG != 'zh' and content:
            print(f"[{FEED_NAME}] 翻译: {title[:50]}...")
            content = translate_with_kimi(content)
            title = translate_with_kimi(title)

        new_articles.append({
            'title': title,
            'link': link,
            'content': content
        })

        # 标记为已处理
        feed_processed.append(link)

    print(f"[{FEED_NAME}] 新文章: {len(new_articles)} 篇")

    # 发送邮件
    if new_articles:
        send_email(new_articles, FEED_NAME)

    # 保存已处理的 URL
    processed_urls[FEED_NAME] = feed_processed
    save_processed_urls(processed_urls)
    print(f"[{FEED_NAME}] 处理完成")


if __name__ == '__main__':
    main()
