#!/usr/bin/env node
/**
 * RSS Feed EPUB Generator + Feishu Delivery
 * Reads article JSON files from data/articles/ → generates EPUB → sends to Feishu
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const JSZip = require('jszip');

const WORKSPACE = process.env.GITHUB_WORKSPACE || path.join(__dirname, '..');
const ARTICLES_DIR = path.join(WORKSPACE, 'data', 'articles');
const OUTPUT_DIR = path.join(WORKSPACE, 'data', 'epubs');

// Feishu credentials (from environment or defaults)
const FEISHU_APP_ID = process.env.FEISHU_APP_ID || 'cli_a93257284678dcd5';
const FEISHU_APP_SECRET = process.env.FEISHU_APP_SECRET || '';
const FEISHU_RECEIVE_ID = process.env.FEISHU_RECEIVE_ID || 'ou_c99d5eaa47be753c6c5092688731b6aa';

// ─── Utility ───────────────────────────────────────────

function escXml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function formatDate() {
  const d = new Date();
  const offset = 8; // Beijing time UTC+8
  d.setHours(d.getHours() + offset);
  return d.toISOString().slice(0, 10);
}

// ─── Load Articles ─────────────────────────────────────

function loadArticles() {
  const files = fs.readdirSync(ARTICLES_DIR).filter(f => f.endsWith('.json'));
  const allFeeds = [];

  for (const f of files) {
    const raw = fs.readFileSync(path.join(ARTICLES_DIR, f), 'utf8');
    const data = JSON.parse(raw);
    allFeeds.push(data);
  }

  // Sort feeds by name for consistent ordering
  allFeeds.sort((a, b) => a.feed_name.localeCompare(b.feed_name));
  return allFeeds;
}

// ─── Generate EPUB ─────────────────────────────────────

async function generateEpub(feeds) {
  const zip = new JSZip();
  const dateStr = formatDate();
  const bookId = `rss-digest-${dateStr}`;
  const totalArticles = feeds.reduce((sum, f) => sum + f.articles.length, 0);
  const title = `RSS 日报 ${dateStr}`;
  const allSpineItems = [];

  // ── mimetype (must be first, uncompressed) ──
  zip.file('mimetype', 'application/epub+zip', { compression: null });

  // ── META-INF/container.xml ──
  zip.file('META-INF/container.xml',
    `<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oepbs-package+xml"/>
  </rootfiles>
</container>`
  );

  // ── CSS ──
  const css = `
body { font-family: "Microsoft YaHei", -apple-system, Arial, sans-serif; margin: 1em; padding: 0; background: #fff; color: #333; line-height: 1.8; }
h1 { font-size: 1.5em; color: #1a237e; border-bottom: 2px solid #1a237e; padding-bottom: 0.3em; }
h2 { font-size: 1.3em; color: #1b4332; margin-top: 1.5em; border-left: 3px solid #40916c; padding-left: 0.5em; }
h3 { font-size: 1.1em; color: #333; margin-top: 1em; }
.feed-header { background: #1a237e; color: white; padding: 0.8em; margin-bottom: 0.5em; border-radius: 4px; }
.feed-header .badge { display: inline-block; background: #1565c0; padding: 0.2em 0.6em; border-radius: 10px; font-size: 0.8em; }
.feed-header h2 { color: white; margin: 0; border: none; padding: 0; }
.article { padding: 0.8em 0; border-bottom: 1px solid #f0f0f0; }
.article:last-child { border-bottom: none; }
.meta { font-size: 0.85em; color: #aaa; }
.model-tag { font-size: 0.85em; color: #1565c0; font-style: italic; margin-top: 0.5em; border-top: 1px dashed #e0e0e0; padding-top: 0.3em; }
.content { font-size: 1em; line-height: 1.7; }
.content p { margin: 0.5em 0; }
footer { text-align: center; font-size: 0.8em; color: #bbb; margin-top: 2em; padding-top: 1em; border-top: 1px solid #eee; }
`;
  zip.file('OEBPS/style.css', css);

  // ── Build chapters ──
  const manifestItems = [];
  let chapterNum = 0;

  // Cover page
  const coverHtml = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh">
<head><meta charset="UTF-8"/><title>${escXml(title)}</title>
<link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
<h1>${escXml(title)}</h1>
<p>共 ${feeds.length} 个源 · ${totalArticles} 篇文章</p>
${feeds.map(f => `<p>• ${escHtml(f.feed_name)} (${f.articles.length} 篇)</p>`).join('\n')}
<footer>AI 助手自动抓取推送</footer>
</body></html>`;
  zip.file('OEBPS/cover.xhtml', coverHtml);
  manifestItems.push({ id: 'cover', href: 'cover.xhtml', type: 'application/xhtml+xml' });
  allSpineItems.push('cover');

  // Per-feed chapters
  for (const feed of feeds) {
    chapterNum++;
    const fileName = `feed-${chapterNum}.xhtml`;
    const badge = feed.is_translated ? '🔄 中文翻译' : '🌐 原文';
    const articlesHtml = feed.articles.map((art, i) => {
      const titleEsc = escHtml(art.title || '无标题');
      const content = (art.content || art.summary || '（无内容）')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      // Convert newlines to paragraphs
      const paragraphs = content.split(/\n\n+/).map(p => `<p>${p.replace(/\n/g, '<br/>')}</p>`).join('\n');
      
      let metaLine = '';
      if (art.published) metaLine = `<div class="meta">📅 ${escHtml(art.published)}</div>`;
      
      let countLine = '';
      if (art.en_words > 0 && art.zh_chars > 0) countLine = `<div class="meta">📊 英文约 ${art.en_words} 词 → 中文约 ${art.zh_chars} 字</div>`;
      
      let modelLine = '';
      if (art.model_used === 'failed') modelLine = `<div class="model-tag">⚠️ 翻译失败，保留原文</div>`;
      else if (art.model_used) {
        const m = art.model_used.split('/').pop();
        const splitNote = art.was_split ? '（分段处理）' : '';
        modelLine = `<div class="model-tag">🤖 翻译模型: ${escHtml(m)}${splitNote}</div>`;
      }

      return `
<div class="article">
  <h3>${i + 1}. ${titleEsc}</h3>
  ${metaLine}
  ${countLine}
  <div class="content">
    ${paragraphs}
  </div>
  ${modelLine}
</div>`;
    }).join('\n');

    const feedHtml = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh">
<head><meta charset="UTF-8"/><title>${escXml(feed.feed_name)}</title>
<link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
<div class="feed-header">
  <div class="badge">${badge}</div>
  <h2>${escXml(feed.feed_name)}</h2>
  <div style="opacity:0.8;font-size:0.85em">${feed.articles.length} 篇文章</div>
</div>
${articlesHtml}
</body></html>`;
    zip.file(`OEBPS/${fileName}`, feedHtml);
    manifestItems.push({ id: `feed-${chapterNum}`, href: fileName, type: 'application/xhtml+xml' });
    allSpineItems.push(`feed-${chapterNum}`);
  }

  // ── toc.ncx ──
  const navPoints = manifestItems.filter(m => m.id !== 'cover').map((m, i) => {
    const feed = feeds[i - 0]; // offset by 0 since cover is first
    const label = feed ? feed.feed_name : 'Cover';
    return `    <navPoint id="np-${i + 1}" playOrder="${i + 1}">
      <navLabel><text>${escXml(label)}</text></navLabel>
      <content src="${m.href}"/>
    </navPoint>`;
  }).join('\n');

  zip.file('OEBPS/toc.ncx',
    `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh">
  <head>
    <meta name="dtb:uid" content="${bookId}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>${escXml(title)}</text></docTitle>
  <navMap>
    <navPoint id="np-cover" playOrder="1">
      <navLabel><text>${escXml(title)}</text></navLabel>
      <content src="cover.xhtml"/>
    </navPoint>
${navPoints}
  </navMap>
</ncx>`
  );

  manifestItems.push({ id: 'ncx', href: 'toc.ncx', type: 'application/x-dtbncx+xml' });
  manifestItems.push({ id: 'css', href: 'style.css', type: 'text/css' });

  // ── content.opf ──
  const manifestXml = manifestItems.map(m =>
    `    <item id="${m.id}" href="${m.href}" media-type="${m.type}"/>`
  ).join('\n');

  const spineXml = allSpineItems.map(id =>
    `    <itemref idref="${id}"/>`
  ).join('\n');

  zip.file('OEBPS/content.opf',
    `<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">${bookId}</dc:identifier>
    <dc:title>${escXml(title)}</dc:title>
    <dc:language>zh</dc:language>
    <dc:creator>AI RSS Digest</dc:creator>
    <dc:date>${dateStr}</dc:date>
    <meta property="dcterms:modified">${dateStr}T00:00:00Z</meta>
  </metadata>
  <manifest>
${manifestXml}
  </manifest>
  <spine toc="ncx">
${spineXml}
  </spine>
</package>`
  );

  const epubBuf = await zip.generateAsync({ type: 'nodebuffer' });
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  const outputPath = path.join(OUTPUT_DIR, `rss-digest-${dateStr}.epub`);
  fs.writeFileSync(outputPath, epubBuf);
  console.log(`✅ EPUB generated: ${outputPath} (${(epubBuf.length / 1024).toFixed(1)} KB)`);
  return { outputPath, epubBuf, dateStr };
}

// ─── Feishu API ────────────────────────────────────────

async function getFeishuToken() {
  const body = JSON.stringify({
    app_id: FEISHU_APP_ID,
    app_secret: FEISHU_APP_SECRET
  });

  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'open.feishu.cn',
      path: '/open-apis/auth/v3/app_access_token/internal',
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' }
    }, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try {
          const obj = JSON.parse(data);
          if (obj.code === 0) resolve(obj.app_access_token);
          else reject(new Error(`Feishu auth failed: ${obj.msg}`));
        } catch(e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

async function uploadFeishuFile(token, epubBuf, dateStr) {
  // Upload as file via Feishu im/v1/files API
  const filename = `rss-digest-${dateStr}.epub`;
  
  // Build multipart form data
  const boundary = '----FormBoundary' + Date.now();
  const parts = [];
  
  // file_type field
  parts.push(`--${boundary}\r\nContent-Disposition: form-data; name="file_type"\r\n\r\nstream`);
  // file_name field
  parts.push(`--${boundary}\r\nContent-Disposition: form-data; name="file_name"\r\n\r\n${filename}`);
  // file content
  const fileHeader = `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: application/epub+zip\r\n\r\n`;
  
  const headerBuf = Buffer.from(fileHeader, 'utf8');
  const footerBuf = Buffer.from(`\r\n--${boundary}--\r\n`, 'utf8');
  const fieldBuf = Buffer.from(parts.join('\r\n') + '\r\n', 'utf8');
  
  const fullBuf = Buffer.concat([fieldBuf, headerBuf, epubBuf, footerBuf]);

  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'open.feishu.cn',
      path: '/open-apis/im/v1/files',
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': `multipart/form-data; boundary=${boundary}`
      }
    }, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try {
          const obj = JSON.parse(data);
          if (obj.code === 0) resolve(obj.data.file_key);
          else reject(new Error(`File upload failed: code=${obj.code} msg=${obj.msg}`));
        } catch(e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.write(fullBuf);
    req.end();
  });
}

async function sendFeishuMessage(token, fileKey, dateStr) {
  const body = JSON.stringify({
    receive_id: FEISHU_RECEIVE_ID,
    msg_type: 'file',
    content: JSON.stringify({ file_key: fileKey })
  });

  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'open.feishu.cn',
      path: '/open-apis/im/v1/messages?receive_id_type=open_id',
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json; charset=utf-8'
      }
    }, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try {
          const obj = JSON.parse(data);
          if (obj.code === 0) resolve(obj.data);
          else reject(new Error(`Send message failed: code=${obj.code} msg=${obj.msg}`));
        } catch(e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ─── Main ──────────────────────────────────────────────

async function main() {
  console.log('=== RSS Feed EPUB Generator ===');

  // 1. Load articles
  if (!fs.existsSync(ARTICLES_DIR)) {
    console.error('No articles directory found. Nothing to generate.');
    process.exit(0);
  }

  const feeds = loadArticles();
  if (feeds.length === 0) {
    console.log('No article JSON files found. Nothing to generate.');
    process.exit(0);
  }

  const totalArticles = feeds.reduce((sum, f) => sum + f.articles.length, 0);
  console.log(`Found ${feeds.length} feeds, ${totalArticles} total articles`);

  // 2. Generate EPUB
  const { outputPath, epubBuf, dateStr } = await generateEpub(feeds);

  // 3. Send to Feishu
  if (FEISHU_APP_SECRET) {
    console.log('Sending EPUB to Feishu...');
    try {
      const token = await getFeishuToken();
      console.log('  ✅ Feishu auth OK');
      
      const fileKey = await uploadFeishuFile(token, epubBuf, dateStr);
      console.log(`  ✅ File uploaded: ${fileKey}`);
      
      const result = await sendFeishuMessage(token, fileKey, dateStr);
      console.log(`  ✅ Message sent: ${result.message_id}`);
    } catch(e) {
      console.error(`  ❌ Feishu send failed: ${e.message}`);
      // Don't fail the whole workflow - EPUB is still saved locally
    }
  } else {
    console.log('No Feishu APP_SECRET configured, skipping Feishu delivery.');
  }

  // 4. Clean up article JSONs (optional - keep for debugging)
  // fs.readdirSync(ARTICLES_DIR).forEach(f => fs.unlinkSync(path.join(ARTICLES_DIR, f)));

  console.log('\n=== Done ===');
}

main().catch(e => { console.error('FATAL:', e); process.exit(1); });