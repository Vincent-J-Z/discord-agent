#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把一个图片文件夹打包成漫画 EPUB —— 复刻 Kox.moe / calibre 生成的图片型漫画格式。
特点：右往左翻页 (rtl)、每页一张整图、结构与参考文件完全一致。

用法:
  python3 make_epub.py <图片目录> [-o 输出.epub] [-t 标题] [-a 作者]
                       [--publisher 出版方] [--ltr] [--cover 封面图片]

  <图片目录>   按文件名排序
  --cover      指定封面图片路径（如漫画官方封面）；给了此参数时，
               目录内全部图片都作为正文内页（不再丢第一张）。
               不给时保持旧行为：第一张图当封面，其余为内页。
  --ltr        改为西式左往右翻页（默认 rtl，适合日漫）
"""
import argparse, os, sys, uuid, zipfile, mimetypes
from datetime import datetime, timezone

IMG_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')

STYLESHEET = """.calibre {
  background: #FFF;
  color: #000;
}
.calibre1 {
  display: block;
  font-size: 1em;
  line-height: 1.2;
  margin: 0 5pt;
  padding: 0;
}
.calibre2 {
  display: block;
  text-align: center;
  vertical-align: top;
  white-space: nowrap;
  margin: 0;
  padding: 0;
  border: currentColor none 0;
}
.fs {
  display: block;
  text-align: center;
  vertical-align: top;
  margin: 0;
  padding: 0;
  border: currentColor none 0;
}
.singlepage {
  height: auto;
  max-height: 100%;
  max-width: 100%;
  text-align: center;
  vertical-align: top;
  width: auto;
  margin: 0;
  border: currentColor none 0;
}
"""

PAGE_STYLES = """@page {
  margin-bottom: 5pt;
  margin-top: 5pt;
}
"""

CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
   <rootfiles>
      <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>

   </rootfiles>
</container>
"""

def page_html(title, img_rel):
    return f"""<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml" class="calibre">
  <head>
    <title>{title}</title>
    <meta name="viewport" content="width=960, height=1280"/>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
  <link rel="stylesheet" type="text/css" href="../stylesheet.css"/>
<link rel="stylesheet" type="text/css" href="../page_styles.css"/>
</head>
  <body class="calibre1">
<div class="fs">
  <div class="calibre2">
    <img src="{img_rel}" alt="{title}" class="singlepage" kmoetag="rotate:0"/>
  </div>
</div>
</body>
</html>
"""

def cover_html():
    return """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml" class="calibre">
  <head>
    <title>封面</title>
    <meta name="viewport" content="width=960, height=1280"/>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
  <link rel="stylesheet" type="text/css" href="../stylesheet.css"/>
<link rel="stylesheet" type="text/css" href="../page_styles.css"/>
</head>
  <body class="calibre1">
<div class="fs">
  <div class="calibre2">
    <img src="../cover.jpeg" alt="Book Cover" class="singlepage"/>
  </div>
</div>
</body>
</html>
"""

def titlepage_xhtml():
    return """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
    <head>
        <meta http-equiv="Content-Type" content="text/html; charset=UTF-8"/>
        <meta name="calibre:cover" content="true"/>
        <title>Cover</title>
        <style type="text/css" title="override_css">
            @page {padding: 0pt; margin:0pt}
            body { text-align: center; padding:0pt; margin: 0pt; }
        </style>
    </head>
    <body>
        <div>
            <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 800 1200" preserveAspectRatio="none">
                <image width="800" height="1200" xlink:href="cover.jpeg"/>
            </svg>
        </div>
    </body>
</html>
"""

def build_opf(meta, pages):
    """pages: list of dicts {pid, html, img_id, img_href, media_type}"""
    ppd = 'rtl' if meta['rtl'] else 'ltr'
    pwm = 'horizontal-rl' if meta['rtl'] else 'horizontal-lr'
    m = []
    m.append("<?xml version='1.0' encoding='UTF-8'?>")
    m.append('<opf:package xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:opf="http://www.idpf.org/2007/opf" version="2.0" '
             'unique-identifier="uuid_id">')
    m.append('  <opf:metadata>')
    m.append(f'    <dc:title>{meta["title"]}</dc:title>')
    m.append(f'    <dc:creator opf:role="aut" opf:file-as="{meta["author"]}">{meta["author"]}</dc:creator>')
    m.append('    <dc:contributor opf:role="bkp">calibre (9.4.0) [https://calibre-ebook.com]</dc:contributor>')
    m.append(f'    <dc:publisher>{meta["publisher"]}</dc:publisher>')
    m.append(f'    <dc:identifier id="uuid_id" opf:scheme="uuid">{meta["uuid"]}</dc:identifier>')
    m.append(f'    <dc:date>{meta["date"]}</dc:date>')
    m.append(f'    <dc:language>{meta["language"]}</dc:language>')
    m.append(f'    <dc:identifier opf:scheme="calibre">{meta["uuid"]}</dc:identifier>')
    m.append(f'    <opf:meta name="calibre:timestamp" content="{meta["timestamp"]}" />')
    m.append(f'    <opf:meta name="calibre:title_sort" content="{meta["title"]}" />')
    m.append('    <opf:meta name="cover" content="cover" />')
    m.append(f'    <opf:meta name="primary-writing-mode" content="{pwm}" />')
    m.append('  </opf:metadata>')
    m.append('  <opf:manifest>')
    m.append('    <opf:item id="titlepage" href="titlepage.xhtml" media-type="application/xhtml+xml" />')
    m.append('    <opf:item id="Page_cover" href="html/cover.html" media-type="application/xhtml+xml" />')
    for p in pages:
        m.append(f'    <opf:item id="{p["pid"]}" href="html/{p["html"]}" media-type="application/xhtml+xml" />')
    m.append('    <opf:item id="cover" href="cover.jpeg" media-type="image/jpeg" />')
    m.append('    <opf:item id="css" href="stylesheet.css" media-type="text/css" />')
    m.append('    <opf:item id="page_css" href="page_styles.css" media-type="text/css" />')
    m.append('    <opf:item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />')
    for p in pages:
        m.append(f'    <opf:item id="{p["img_id"]}" href="image/{p["img_href"]}" media-type="{p["media_type"]}" />')
    m.append('    </opf:manifest>')
    m.append(f'  <opf:spine toc="ncx" page-progression-direction="{ppd}">')
    m.append('    <opf:itemref idref="titlepage" />')
    m.append('    <opf:itemref idref="Page_cover" />')
    for p in pages:
        m.append(f'    <opf:itemref idref="{p["pid"]}" />')
    m.append('  </opf:spine>')
    m.append('  <opf:guide>')
    m.append('    <opf:reference type="cover" href="titlepage.xhtml" title="Cover" />')
    m.append('  </opf:guide>')
    m.append('</opf:package>')
    return '\n'.join(m)

def build_ncx(meta, pages):
    n = []
    n.append("<?xml version='1.0' encoding='UTF-8'?>")
    n.append('<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zho">')
    n.append('  <head>')
    n.append(f'    <meta name="dtb:uid" content="{meta["uuid"]}" />')
    n.append('    <meta name="dtb:depth" content="2" />')
    n.append('    <meta name="dtb:generator" content="calibre (9.4.0)" />')
    n.append('    <meta name="dtb:totalPageCount" content="0" />')
    n.append('    <meta name="dtb:maxPageNumber" content="0" />')
    n.append('  </head>')
    n.append('  <docTitle>')
    n.append(f'    <text>{meta["title"]}</text>')
    n.append('  </docTitle>')
    n.append('  <navMap>')
    for i, p in enumerate(pages, start=2):
        n.append(f'    <navPoint id="{p["pid"]}" playOrder="{i}" class="other">')
        n.append('      <navLabel>')
        n.append(f'        <text>第 {i-1:03d} 頁</text>')
        n.append('      </navLabel>')
        n.append(f'      <content src="html/{p["html"]}" />')
        n.append('    </navPoint>')
    n.append('  </navMap>')
    n.append('</ncx>')
    return '\n'.join(n)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('image_dir')
    ap.add_argument('-o', '--output')
    ap.add_argument('-t', '--title', default='未命名漫画')
    ap.add_argument('-a', '--author', default='佚名')
    ap.add_argument('--publisher', default='Kox.moe')
    ap.add_argument('--language', default='zh')
    ap.add_argument('--ltr', action='store_true', help='左往右翻页（默认右往左）')
    ap.add_argument('--cover', help='封面图片路径（不给则用第一张内页凑数，向后兼容）')
    args = ap.parse_args()

    imgs = sorted(f for f in os.listdir(args.image_dir)
                  if f.lower().endswith(IMG_EXTS))
    if not imgs:
        sys.exit(f'目录里没有图片: {args.image_dir}')

    out = args.output or (args.title + '.epub')
    now = datetime.now(timezone.utc)
    meta = {
        'title': args.title, 'author': args.author, 'publisher': args.publisher,
        'language': args.language, 'uuid': str(uuid.uuid4()),
        'date': now.strftime('%Y-%m-%dT00:00:00+00:00'),
        'timestamp': now.strftime('%Y-%m-%dT%H:%M:%S.%f+00:00'),
        'rtl': not args.ltr,
    }

    if args.cover:
        # 独立封面图（如官方封面）：全部下载页都进正文，不丢第一页
        cover_src = args.cover
        body_imgs = imgs
    else:
        # 向后兼容：第一张作封面，其余是内页
        cover_src = os.path.join(args.image_dir, imgs[0])
        body_imgs = imgs[1:] if len(imgs) > 1 else imgs

    pages = []
    for idx, fn in enumerate(body_imgs, start=1):
        ext = os.path.splitext(fn)[1].lower()
        media = mimetypes.types_map.get(ext, 'image/jpeg')
        if ext == '.webp':
            media = 'image/webp'
        img_href = f'img-{idx:04d}{ext}'
        pages.append({
            'pid': f'Page_{idx+1}',           # Page_2 起（Page_cover 占前）
            'html': f'page-{idx:04d}.html',
            'title': f'第 {idx+1} 页',
            'img_id': f'img_{idx}',
            'img_href': img_href,
            'src': os.path.join(args.image_dir, fn),
            'media_type': media,
        })

    with zipfile.ZipFile(out, 'w') as z:
        # 1) mimetype 必须第一个且不压缩
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        # 2) 元数据/样式
        z.writestr('META-INF/container.xml', CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('content.opf', build_opf(meta, pages), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('toc.ncx', build_ncx(meta, pages), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('stylesheet.css', STYLESHEET, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('page_styles.css', PAGE_STYLES, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('titlepage.xhtml', titlepage_xhtml(), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('html/cover.html', cover_html(), compress_type=zipfile.ZIP_DEFLATED)
        # 3) 封面图（统一存成 cover.jpeg 名，内容按原样）
        with open(cover_src, 'rb') as f:
            z.writestr('cover.jpeg', f.read(), compress_type=zipfile.ZIP_STORED)
        # 4) 每页 html + 图片
        for p in pages:
            z.writestr(f'html/{p["html"]}', page_html(p['title'], f'../image/{p["img_href"]}'),
                       compress_type=zipfile.ZIP_DEFLATED)
            with open(p['src'], 'rb') as f:
                z.writestr(f'image/{p["img_href"]}', f.read(), compress_type=zipfile.ZIP_STORED)

    print(f'已生成: {out}')
    print(f'  标题: {meta["title"]} / 作者: {meta["author"]}')
    print(f'  翻页方向: {"右往左 (rtl)" if meta["rtl"] else "左往右 (ltr)"}')
    print(f'  页数: 封面 + {len(pages)} 内页 = {len(pages)+1} 图')

if __name__ == '__main__':
    main()
