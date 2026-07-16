#!/usr/bin/env node
// 列出 manhuagui 某部漫画的所有章节，按阅读顺序（第1话在前）输出。
// 用法: node list_chapters.js <comicId>
// 输出: 每行  <序号>\t<章节ID>\t<标题>\t<章节URL>
const https = require('https');
const comicId = process.argv[2];
if (!comicId) { console.error('用法: node list_chapters.js <comicId>'); process.exit(1); }
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1';

function get(url) {
  return new Promise((res, rej) => {
    https.get(url, { headers: { 'User-Agent': UA } }, (r) => {
      if (r.statusCode >= 300 && r.statusCode < 400 && r.headers.location)
        return res(get(new URL(r.headers.location, url).href));
      const c = []; r.on('data', (d) => c.push(d)); r.on('end', () => res(Buffer.concat(c).toString('utf8')));
    }).on('error', rej);
  });
}

(async () => {
  const url = `https://m.manhuagui.com/comic/${comicId}/`;
  const html = await get(url);
  // 页面为倒序（最新在前），抓 链接+标题
  const re = /href="\/comic\/\d+\/(\d+)\.html"[^>]*>\s*<b>([^<]*)<\/b>/g;
  let m, list = [];
  while ((m = re.exec(html))) list.push({ id: m[1], title: m[2].trim() });
  if (!list.length) { console.error('未解析到章节，页面结构可能变化'); process.exit(2); }
  list.reverse(); // 变成阅读顺序（第1话在前）
  list.forEach((c, i) => {
    console.log(`${i + 1}\t${c.id}\t${c.title}\thttps://m.manhuagui.com/comic/${comicId}/${c.id}.html`);
  });
})().catch((e) => { console.error('错误:', e.message); process.exit(1); });
