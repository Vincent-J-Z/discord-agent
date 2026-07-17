---
name: manhuagui-epub
description: Download chapters from manhuagui (看漫画 m.manhuagui.com) and package each chapter into a Kox.moe/calibre-style right-to-left manga EPUB. Use when the user wants to grab a manhuagui comic/chapter, batch-download a chapter range, or turn manga images into an EPUB matching that format. Triggers on manhuagui, m.manhuagui.com, 看漫画, comic id + chapter range, "打包成 epub", "漫画下载".
---

# manhuagui → 漫画 EPUB 批量流水线

把 m.manhuagui.com（看漫画手机版）的章节抓下来，每话打包成一本**右往左翻页**的漫画 EPUB，格式对齐 Kox.moe/calibre 生成的图片型 EPUB。

## 这个 skill 里有什么

同目录下的脚本（都已就绪，含 `node_modules/lz-string`）：
- `list_chapters.js` — 列出某部漫画全部章节（按阅读顺序，第1话在前）
- `download_chapter.js` — 下载单话全部图片（含解密逻辑）
- `make_epub.py` — 把一个图片文件夹打包成 EPUB（纯 Python 标准库）
- `batch.sh` — 一条龙：列章节 → 逐话下载 → 逐话打包

## 快速用法

```bash
cd ~/.claude/skills/manhuagui-epub

# 列出全部章节（拿到序号↔ID↔URL）
node list_chapters.js 49036

# 批量做第 10~20 话，每话一本 EPUB
./batch.sh 49036 10 20 "被追放的转生重骑士用游戏知识开无双" "武六甲理衣、猫子" /想要的/输出目录

# 只做单话：先下载再打包
node download_chapter.js "https://m.manhuagui.com/comic/49036/702794.html" ./imgs/第10话
python3 make_epub.py ./imgs/第10话 -o "第10话.epub" -t "书名 - 第10话" -a "作者"
```

`comicId` 就是 URL 里的数字：`m.manhuagui.com/comic/<comicId>/`。
输出：`<输出目录>/epub/<书名> - <话名>.epub`，图片中转在 `<输出目录>/_images/`。

## 关键原理（复现/排障必读）

**1. 章节页图片是加密的，分三层：**
- 页面底部有一段 `eval(function(p,a,c,k,e,d){...}(...))` —— 这是 **Dean Edwards packer**。
- 但 manhuagui 把标准的 `.split('|')` 换成了 `["\x73\x70\x6c\x69\x63"]('\x7c')`（即自定义方法名 `splic`）。真正含义是：**先把字典串用 LZString.decompressFromBase64 解压，再 `split('|')`** 才是 packer 的 k 字典。漏了 LZString 这步是最常见的坑。
- 解包后得到 `SMH.reader({...})`，里面：
  - `images`: 图片相对路径数组（形如 `/ps4/w/w6-21292/xxx/第01话/01.jpg.webp`，已是完整路径，不需要再拼 `path`）
  - `sl`: `{ e: <过期时间戳>, m: <签名> }`
  - `bookName` / `chapterTitle`

**2. 真实图片地址：**
```
https://i.hamreus.com  +  images[i]  +  ?e=<sl.e>&m=<sl.m>
```
请求**必须带** `Referer: https://m.manhuagui.com/`，否则 403。用手机 UA（iPhone Safari）。

**3. 签名有时效**：`sl.e` 是过期时间戳。每次都是现抓页面现下载，别缓存 URL 隔天再用。

**4. 官方封面图**（比拿内页凑数好看，也不会吞掉正文第一页）：
```
https://cf.hamreus.com/cpic/g/<comicId>.jpg   # g = 大图变体
```
同样**必须带** `Referer: https://m.manhuagui.com/` + 手机 UA，否则可能 403。
`batch.sh` 会在开跑前自动拉取这张图并对每话传给 `make_epub.py --cover`；
单独跑 `make_epub.py` 时也可以自己下载后用 `--cover 封面.jpg` 传入。

## EPUB 目标格式（对照 Kox.moe/calibre 图片漫画）

结构与要点：
```
mimetype                      # 首个文件、ZIP_STORED 不压缩，内容 application/epub+zip
META-INF/container.xml        # 指向 content.opf
content.opf                   # opf: 命名空间；spine 带 page-progression-direction="rtl"
                              #   metadata 里 primary-writing-mode = horizontal-rl
toc.ncx                       # navMap，每页一个 navPoint「第 NNN 頁」
titlepage.xhtml               # SVG 封面 (viewBox 0 0 800 1200)
cover.jpeg                    # 封面图（第一张图）
stylesheet.css / page_styles.css   # .fs/.calibre2/.singlepage 等
html/cover.html               # 封面页
html/page-NNNN.html           # 每页一个，<img class="singlepage"> 包一张图
image/img-NNNN.<ext>          # 图片原样嵌入（jpg/png/webp）
```
- **右往左翻页**（日漫）是核心：`rtl` + `horizontal-rl`。西式条漫用 `make_epub.py --ltr`。
- 传了 `--cover 封面图片` 时：该图作封面，目录内**全部**下载页都是正文（不丢页）。
  不传 `--cover` 时保持旧行为：第一张图当封面，其余为内页。`batch.sh` 默认会传 `--cover`
  （自动抓官方封面），除非官方封面下载失败才回退旧行为。

## 校验产物

```bash
python3 -c "
import zipfile,sys
z=zipfile.ZipFile(sys.argv[1]); n=z.namelist()
assert n[0]=='mimetype' and z.getinfo('mimetype').compress_type==zipfile.ZIP_STORED
assert z.testzip() is None
print('OK 图片', len([x for x in n if x.startswith('image/')]))
" 某本.epub
```

## 依赖 / 复现环境
- `node`（本目录 `node_modules/lz-string` 已装；缺失就 `npm install lz-string`）
- `python3`（仅标准库）
- 若在别处复现：把本目录整体拷走即可，或重新 `npm install lz-string`。

## 已知坑
- **解包后找不到 JSON**：多半是 LZString 那步漏了，或站点把函数名从 `SMH.reader` 改了 —— 先 `console.log` 解包后的字符串看结构。
- **图片 403**：缺 `Referer` 或用了桌面 UA。
- **章节列表为空**：`list_chapters.js` 的正则依赖 `<a href=...><b>第N话</b>`，页面改版就调正则；页面是**倒序**的，脚本已 `reverse()` 成阅读序。
- **限速**：`batch.sh` 每话之间 `sleep 1`，别改太激进。
