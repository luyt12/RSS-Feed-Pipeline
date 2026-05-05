#!/usr/bin/env python3
"""
单源 RSS 处理脚本 - 从环境变量读取 feed 配置
对齐 daily_task.py 逻辑：历史文章回退、精美邮件格式、翻译优化
增加：SMTP失败后使用AgentMail HTTP API发送
更新：多模型 fallback + 长文分段翻译 + 模型标注
"""
import os
import json
import feedparser
import requests
import time
import trafilatura
import re
import base64
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

# AgentMail HTTP API 配置（作为SMTP失败后的fallback）
AGENTMAIL_API_KEY = os.environ.get('AGENTMAIL_API_KEY', '')
AGENTMAIL_INBOX_ID = os.environ.get('AGENTMAIL_INBOX_ID', '')

# API 配置
KIMI_API_KEY = os.environ.get('KIMI_API_KEY', '')
KIMI_API_URL = os.environ.get('KIMI_API_URL', 'https://integrate.api.nvidia.com/v1/chat/completions')

# 模型列表（按优先级排列）
MODEL_LIST = [
    'minimaxai/minimax-m2.7',
    'qwen/qwen3-coder-480b-a35b-instruct',
    'stepfun-ai/step-3.5-flash',
    'google/gemma-3n-e2b-it',
    'mistralai/mistral-nemotron'
]

# 路径配置
DATA_DIR = 'data'
PROCESSED_URLS_FILE = os.path.join(DATA_DIR, 'processed_urls.json')

# 翻译 Prompt
TRANSLATE_PROMPT = """You are a professional translator. Translate the English article into Chinese following these rules:

## CRITICAL Word Count Rule (MUST FOLLOW)
The Chinese output MUST have approximately 80% of the English word count in Chinese characters.
- English 1000 words → Chinese ~800 characters
- English 2000 words → Chinese ~1600 characters
- English 3000 words → Chinese ~2400 characters
- If your output is too short, expand with context, background, and details from the original

## Translation Approach
1. Translate key arguments and evidence in detail
2. Keep ALL important facts: names, dates, institutions, data, quotes
3. Include context and background that helps understanding
4. Do NOT aggressively compress - preserve information density
5. Stay neutral on controversial topics

## Output Format
Output Chinese text directly. No meta-comments. No "This article discusses..." filler."""


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
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=30)
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


def count_words(text):
    """计算英文单词数"""
    # 简单按空格分割计算单词数
    words = text.split()
    return len(words)


def split_by_paragraphs(text, max_words=2000):
    """
    按自然段落分割文章，每部分不超过 max_words 词
    
    返回: [(part_text, word_count), ...]
    """
    # 按双换行分割段落
    paragraphs = re.split(r'\n\n+', text)
    
    parts = []
    current_part = []
    current_words = 0
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        
        para_words = count_words(para)
        
        # 如果当前段落本身就超过 max_words，需要进一步分割
        if para_words > max_words:
            # 先保存当前部分
            if current_part:
                part_text = '\n\n'.join(current_part)
                parts.append((part_text, current_words))
                current_part = []
                current_words = 0
            
            # 按句子分割长段落
            sentences = re.split(r'(?<=[.!?])\s+', para)
            sub_part = []
            sub_words = 0
            
            for sent in sentences:
                sent_words = count_words(sent)
                
                if sub_words + sent_words <= max_words:
                    sub_part.append(sent)
                    sub_words += sent_words
                else:
                    # 保存当前子部分
                    if sub_part:
                        parts.append((' '.join(sub_part), sub_words))
                    sub_part = [sent]
                    sub_words = sent_words
            
            # 保存最后的子部分
            if sub_part:
                parts.append((' '.join(sub_part), sub_words))
        
        elif current_words + para_words <= max_words:
            # 当前段落可以加入当前部分
            current_part.append(para)
            current_words += para_words
        else:
            # 当前部分已满，保存并开始新部分
            if current_part:
                part_text = '\n\n'.join(current_part)
                parts.append((part_text, current_words))
            current_part = [para]
            current_words = para_words
    
    # 保存最后的部分
    if current_part:
        part_text = '\n\n'.join(current_part)
        parts.append((part_text, current_words))
    
    return parts


def translate_with_model(text, model_name, max_retries=3):
    """
    使用指定模型翻译
    
    返回: (translated_text, success)
    """
    if not text or not KIMI_API_KEY:
        return text, False

    headers = {
        'Authorization': f'Bearer {KIMI_API_KEY}',
        'Content-Type': 'application/json'
    }

    payload = {
        'model': model_name,
        'messages': [
            {'role': 'system', 'content': TRANSLATE_PROMPT},
            {'role': 'user', 'content': text}
        ],
        'temperature': 0.7,
        'max_tokens': 16000
    }

    for attempt in range(max_retries):
        try:
            print(f"    {model_name} 翻译请求 (attempt {attempt + 1}/{max_retries})...")
            response = requests.post(KIMI_API_URL, headers=headers, json=payload, timeout=300)
            response.raise_for_status()
            result = response.json()
            if result.get('choices') and result['choices'][0]:
                translated = result['choices'][0]['message']['content']
                if translated and len(translated) > 50:  # 确保有实质性输出
                    return translated, True
                else:
                    print(f"    {model_name} 返回内容过短，可能失败")
            else:
                print(f"    API 响应异常: {result.get('error', result)}")
        except Exception as e:
            print(f"    {model_name} 翻译失败 (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                wait = 10 * (2 ** attempt)
                print(f"    等待 {wait}s 后重试...")
                time.sleep(wait)
    
    return text, False


def translate_article(text):
    """
    翻译文章，支持长文分段和多模型 fallback
    
    返回: (translated_text, model_used, was_split)
    """
    if not text:
        return text, None, False
    
    word_count = count_words(text)
    print(f"    原文 {word_count} 词")
    
    # 短文直接翻译
    if word_count <= 2000:
        for model in MODEL_LIST:
            translated, success = translate_with_model(text, model)
            if success:
                return translated, model, False
            print(f"    {model} 失败，尝试下一个模型...")
        
        # 所有模型都失败
        print(f"    所有模型均失败，使用原文")
        return text, 'failed', False
    
    # 长文：分段翻译
    print(f"    长文分段处理...")
    parts = split_by_paragraphs(text, max_words=2000)
    print(f"    分为 {len(parts)} 部分: {[p[1] for p in parts]} 词")
    
    translated_parts = []
    successful_model = None
    
    for i, (part_text, part_words) in enumerate(parts):
        print(f"    翻译第 {i+1}/{len(parts)} 部分 ({part_words} 词)...")
        
        # 如果已有成功模型，优先使用
        if successful_model:
            translated, success = translate_with_model(part_text, successful_model)
            if success:
                translated_parts.append(translated)
                continue
            else:
                print(f"    {successful_model} 在第 {i+1} 部分失败，尝试其他模型...")
        
        # 尝试所有模型
        part_translated = False
        for model in MODEL_LIST:
            if model == successful_model:
                continue  # 已经试过了
            translated, success = translate_with_model(part_text, model)
            if success:
                translated_parts.append(translated)
                successful_model = model
                part_translated = True
                break
            print(f"    {model} 失败，尝试下一个模型...")
        
        if not part_translated:
            # 这部分所有模型都失败，使用原文
            print(f"    第 {i+1} 部分翻译失败，保留原文")
            translated_parts.append(part_text)
    
    # 拼接所有部分
    final_text = '\n\n'.join(translated_parts)
    # 只要有一段成功翻译，就不算完全失败
    if successful_model:
        return final_text, successful_model, True
    else:
        return text, 'failed', False


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
  body{{font-family:-apple-system,'Microsoft YaHei',Arial,sans-serif;margin:0;padding:4px;background:#f0f2f5}}
  .wrap{{max-width:100%;margin:0 auto;background:white;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
  .hdr{{background:{hdr_bg};color:white;padding:10px 8px}}
  .badge{{display:inline-block;background:{badge_bg};color:white;padding:3px 10px;border-radius:10px;font-size:11px;margin-bottom:6px}}
  .hdr h1{{margin:0;font-size:18px;font-weight:700}}
  .hdr .sub{{opacity:0.8;margin-top:4px;font-size:12px}}
  .bar{{background:#f8f9fa;padding:8px 8px;font-size:12px;color:#555;border-bottom:1px solid #eee}}
  .art{{padding:10px 8px;border-bottom:1px solid #f0f0f0}}
  .art:last-child{{border-bottom:none}}
  .art h2{{font-size:17px;font-weight:700;color:#1a1a1a;margin:0 0 6px 0;line-height:1.4;border-left:3px solid #40916c;padding:4px 6px;border-radius:0 4px 4px 0}}
  .art h2 a{{color:inherit;text-decoration:none}}
  .meta{{font-size:11px;color:#aaa;margin-bottom:8px}}
  .txt{{font-size:14px;line-height:1.7;color:#333}}
  .fail-tag{{font-size:11px;color:#c0392b;font-style:italic;margin-top:8px;padding-top:8px;border-top:1px dashed #e0e0e0}}
  .model-tag{{font-size:11px;color:#1565c0;font-style:italic;margin-top:8px;padding-top:8px;border-top:1px dashed #e0e0e0}}
  .ft{{padding:8px 8px;background:#f8f9fa;text-align:center;font-size:11px;color:#bbb}}
  @media (max-width:480px){{
    body{{padding:2px}}
    .hdr{{padding:8px 6px}}
    .hdr h1{{font-size:16px}}
    .art{{padding:8px 6px}}
    .art h2{{font-size:15px;padding:3px 4px}}
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
        model_used = art.get('model_used', None)
        
        # HTML 转义
        title_esc = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        content = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        content = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
        
        was_split = art.get('was_split', False)
        
        # 模型标注
        model_tag = ''
        if model_used == 'failed':
            model_tag = '<div class="fail-tag">⚠️ 大模型翻译失败，保留原文</div>'
        elif model_used:
            model_display = model_used.split('/')[-1] if '/' in model_used else model_used
            split_note = '（经分段处理）' if was_split else ''
            model_tag = f'<div class="model-tag">🤖 翻译模型: {model_display}{split_note}</div>'
        
        body += f"""
  <div class="art">
    <h2><span style="color:#40916c;margin-right:6px">{i}.</span><a href="{link}">{title_esc}</a></h2>
    <div class="meta">📅 {pub}</div>
    <div class="txt"><p>{content}</p></div>
    {model_tag}
  </div>
"""

    body += f"""
  <div class="ft">AI 助手自动抓取推送</div>
</div></body></html>"""
    return body


def send_email_via_smtp(articles, feed_name, is_translated, is_today, max_retries=5):
    """通过SMTP发送邮件（带重试机制）"""
    if not articles:
        print(f"[{feed_name}] 无新文章，不发送邮件")
        return False, "无新文章"

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
        model_used = a.get('model_used', None)
        was_split = a.get('was_split', False)
        if model_used == "failed":
            model_tag = "\n[⚠️ 大模型翻译失败，保留原文]"
        elif model_used:
            split_note = "（经分段处理）" if was_split else ""
            model_tag = f"\n[翻译模型: {model_used}{split_note}]"
        text_content += f"\n{'='*60}\n📰 {i}. {title}\n🔗 {link}\n\n{content[:1000]}\n{model_tag}\n"

    # HTML 版本
    html_content = build_html(feed_name, articles, is_translated, is_today)

    # 创建邮件
    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg['Subject'] = subject
    msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    last_error = None
    
    # 发送邮件（带重试机制）
    for attempt in range(max_retries):
        try:
            print(f"[{feed_name}] SMTP发送 (attempt {attempt + 1}/{max_retries})...")
            ctx = ssl.create_default_context()
            
            if SMTP_PORT == 465:
                # SSL 连接
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as server:
                    server.login(SMTP_USER, SMTP_PASS)
                    server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
            else:
                # STARTTLS 连接
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                    server.starttls(context=ctx)
                    server.login(SMTP_USER, SMTP_PASS)
                    server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
            
            print(f"[{feed_name}] SMTP邮件发送成功: {subject}")
            return True, None
            
        except Exception as e:
            last_error = str(e)
            print(f"[{feed_name}] SMTP发送失败 (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                wait_time = min(30 * (2 ** attempt), 300)  # 最大等待5分钟
                print(f"[{feed_name}] 等待 {wait_time}s 后重试...")
                time.sleep(wait_time)
    
    return False, last_error


def send_email_via_http_api(articles, feed_name, is_translated, is_today):
    """通过AgentMail HTTP API发送邮件（作为SMTP失败后的fallback）"""
    if not AGENTMAIL_API_KEY or not AGENTMAIL_INBOX_ID:
        print(f"[{feed_name}] AgentMail API配置缺失，跳过HTTP API发送")
        return False, "AgentMail API配置缺失"
    
    if not articles:
        print(f"[{feed_name}] 无新文章，不发送邮件")
        return False, "无新文章"

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
        model_used = a.get('model_used', None)
        was_split = a.get('was_split', False)
        if model_used == "failed":
            model_tag = "\n[⚠️ 大模型翻译失败，保留原文]"
        elif model_used:
            split_note = "（经分段处理）" if was_split else ""
            model_tag = f"\n[翻译模型: {model_used}{split_note}]"
        text_content += f"\n{'='*60}\n📰 {i}. {title}\n🔗 {link}\n\n{content[:1000]}\n{model_tag}\n"

    # HTML 版本
    html_content = build_html(feed_name, articles, is_translated, is_today)

    try:
        print(f"[{feed_name}] 尝试通过AgentMail HTTP API发送...")
        
        # AgentMail HTTP API endpoint
        api_url = f"https://api.agentmail.to/v0/inboxes/{AGENTMAIL_INBOX_ID}/messages/send"
        
        headers = {
            'Authorization': f'Bearer {AGENTMAIL_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'to': EMAIL_TO,
            'subject': subject,
            'text': text_content,
            'html': html_content
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        print(f"[{feed_name}] HTTP API邮件发送成功: {subject}")
        print(f"[{feed_name}] 响应: {result}")
        return True, None
        
    except Exception as e:
        error_msg = str(e)
        print(f"[{feed_name}] HTTP API发送失败: {error_msg}")
        return False, error_msg


def send_email_with_fallback(articles, feed_name, is_translated, is_today):
    """发送邮件，SMTP失败后尝试HTTP API"""
    # 首先尝试SMTP
    success, error = send_email_via_smtp(articles, feed_name, is_translated, is_today)
    
    if success:
        return True
    
    # SMTP失败，尝试HTTP API
    print(f"[{feed_name}] SMTP发送失败，尝试HTTP API...")
    success, error = send_email_via_http_api(articles, feed_name, is_translated, is_today)
    
    if success:
        return True
    
    print(f"[{feed_name}] 所有发送方式均失败")
    return False


def main():
    print(f"开始处理 feed: {FEED_NAME}")
    print(f"URL: {FEED_URL}")
    print(f"语言: {FEED_LANG}")
    print(f"每日上限: {MAX_DAILY}")
    print(f"跳过全文提取: {SKIP_TRAFILATURA}")
    print(f"AgentMail HTTP API可用: {bool(AGENTMAIL_API_KEY and AGENTMAIL_INBOX_ID)}")
    print(f"模型列表: {MODEL_LIST}")

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

    # 逻辑：优先用今日文章，不够5个则补充历史未处理文章
    candidates = []
    is_today = False

    if today_articles:
        candidates = list(today_articles[:MAX_DAILY])
        is_today = True
        remaining = MAX_DAILY - len(candidates)
        if remaining > 0:
            # 今日文章不足5个，补充历史未处理文章
            history_candidates = [a for a in all_articles if a['link'] not in feed_processed and a not in today_articles]
            history_candidates = history_candidates[:remaining]
            candidates.extend(history_candidates)
            if history_candidates:
                print(f"  → 今日文章 {len(today_articles)} 篇（不足 {MAX_DAILY}），补充历史未处理 {len(history_candidates)} 篇")
            else:
                print(f"  → 使用今日文章: {len(candidates)} 篇")
        else:
            print(f"  → 使用今日文章: {len(candidates)} 篇")
    else:
        # 今日无文章，全部从历史未处理中取
        candidates = [a for a in all_articles if a['link'] not in feed_processed]
        is_today = False
        if candidates:
            print(f"  → 今日无文章，回退到历史未处理: {len(candidates)} 篇")
        else:
            print(f"  → 无新文章")
            return

    # 最终限制数量
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
        model_used = None
        if FEED_LANG != 'zh' and content:
            print(f"  翻译正文: {title[:50]}...")
            translated, model_used, was_split = translate_article(content)
            if translated and translated != content:
                content = translated
                print(f"    ✓ 翻译完成 (模型: {model_used})")
            else:
                print(f"    ✗ 翻译失败，使用原文")

        new_articles.append({
            'title': title,
            'link': link,
            'published': art.get('published', ''),
            'content': content,
            'summary': art.get('summary', ''),
            'model_used': model_used,
            'was_split': was_split
        })

        # 只在翻译成功时标记为已处理，失败则下次重试
        if model_used != 'failed':
            feed_processed.add(link)

    print(f"[{FEED_NAME}] 新文章: {len(new_articles)} 篇")

    # 发送邮件（使用带fallback的版本）
    is_translated = FEED_LANG != 'zh'
    if new_articles:
        send_email_with_fallback(new_articles, FEED_NAME, is_translated, is_today)

    # 保存已处理的 URL
    processed_urls[FEED_NAME] = list(feed_processed)
    save_processed_urls(processed_urls)
    print(f"[{FEED_NAME}] 处理完成")


if __name__ == '__main__':
    main()
