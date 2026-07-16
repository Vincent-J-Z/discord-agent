#!/usr/bin/env node
// 下载 manhuagui (看漫画) 手机版单话全部图片
// 用法: node download_chapter.js <章节URL> [输出目录]
// 例:   node download_chapter.js https://m.manhuagui.com/comic/49036/702785.html

const fs = require('fs');
const path = require('path');
const https = require('https');
const LZString = require('lz-string');

const CHAPTER_URL = process.argv[2] || 'https://m.manhuagui.com/comic/49036/702785.html';
const IMG_HOST = 'https://i.hamreus.com';
const MOBILE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) ' +
  'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1';

function fetch(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': MOBILE_UA, ...headers } }, (res) => {
      // 跟随重定向
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        return resolve(fetch(new URL(res.headers.location, url).href, headers));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks)));
    });
    req.on('error', reject);
    req.setTimeout(30000, () => req.destroy(new Error('timeout: ' + url)));
  });
}

// Dean Edwards packer 的解包：把 eval 换成返回字符串
function unpack(html) {
  const m = html.match(/}\('(.*?)',(\d+),(\d+),'(.*?)'\['\\x73\\x70\\x6c\\x69\\x63'\]\('\\x7c'\)/s)
        || html.match(/}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)/s);
  if (!m) throw new Error('未找到打包的 imgData 脚本');
  let [, p, a, c, kStr] = m;
  a = +a; c = +c;
  // manhuagui: 字典先经 LZString 压缩再 split('|')
  const k = LZString.decompressFromBase64(kStr).split('|');
  const e = (cc) =>
    (cc < a ? '' : e(Math.floor(cc / a))) +
    ((cc = cc % a) > 35 ? String.fromCharCode(cc + 29) : cc.toString(36));
  const d = {};
  let i = c;
  while (i--) d[e(i)] = k[i] || e(i);
  p = p.replace(/\\'/g, "'").replace(/\\\\/g, '\\');
  return p.replace(/\b\w+\b/g, (w) => d[w] === undefined ? w : d[w]);
}

(async () => {
  console.log('抓取页面:', CHAPTER_URL);
  const html = (await fetch(CHAPTER_URL)).toString('utf8');

  const decoded = unpack(html);
  const jm = decoded.match(/SMH\.reader\((\{.*\})\)\.preInit/s)
          || decoded.match(/SMH\.reader\((\{.*\})\)/s);
  if (!jm) throw new Error('解包后未找到 reader JSON');
  const data = JSON.parse(jm[1]);

  const { bookName, chapterTitle, images, sl } = data;
  console.log(`《${bookName}》 ${chapterTitle} — 共 ${images.length} 页`);

  const outDir = process.argv[3] ||
    path.join(__dirname, 'download', `${bookName}`, `${chapterTitle}`);
  fs.mkdirSync(outDir, { recursive: true });

  const query = `?e=${sl.e}&m=${sl.m}`;
  let ok = 0, fail = 0;
  for (let i = 0; i < images.length; i++) {
    const url = IMG_HOST + images[i] + query;
    // images[i] 形如 .../01.jpg.webp — 取最后的真实扩展名
    const ext = (images[i].match(/\.(webp|jpe?g|png|gif|bmp)$/i) || ['.webp'])[0];
    const outFile = path.join(outDir, String(i + 1).padStart(3, '0') + ext);
    try {
      const buf = await fetch(url, { Referer: 'https://m.manhuagui.com/' });
      fs.writeFileSync(outFile, buf);
      ok++;
      process.stdout.write(`\r下载中 ${ok + fail}/${images.length} (成功 ${ok}, 失败 ${fail})`);
    } catch (e) {
      fail++;
      console.error(`\n第 ${i + 1} 页失败: ${e.message}`);
    }
  }
  console.log(`\n完成。输出目录: ${outDir}  (成功 ${ok} / 失败 ${fail})`);
})().catch((e) => {
  console.error('错误:', e.message);
  process.exit(1);
});
