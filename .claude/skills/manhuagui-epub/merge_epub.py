#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把多本 make_epub.py 输出格式的图片型漫画 EPUB，按给定顺序合并成一本。

每本源 EPUB 只取正文页（html/page-*.html + image/img-*），重新编号（全局递增，
天然避免跨本命名冲突），拼进一本新 EPUB；封面/titlepage/样式表只保留一份
（默认取第一本的，缺失则回退到最后一本）。toc.ncx 里每本原书一个顶层
navPoint（章名取自文件名），其下挂原书的页级 navPoint。

用法:
  python3 merge_epub.py <epub1> <epub2> ... -o 输出.epub [-t 标题] [-a 作者]
                        [--cover-index 0] [--cover-fallback-index -1]

  书名/章名默认从文件名 "书名 - 章节.epub" 的 "章节" 部分推导；
  也可用 "path::自定义标签" 显式指定。
"""
import argparse, re, sys, uuid, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

OPF_NS = {'opf': 'http://www.idpf.org/2007/opf', 'dc': 'http://purl.org/dc/elements/1.1/'}

def label_from_filename(path):
    stem = path.rsplit('/', 1)[-1]
    if stem.lower().endswith('.epub'):
        stem = stem[:-5]
    if ' - ' in stem:
        return stem.rsplit(' - ', 1)[-1]
    return stem

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

def parse_book(path, label):
    z = zipfile.ZipFile(path)
    bad = z.testzip()
    if bad is not None:
        sys.exit(f'{path}: 损坏的成员 {bad}')

    opf = z.read('content.opf').decode('utf-8')
    root = ET.fromstring(opf)
    manifest = {item.get('id'): item for item in root.find('opf:manifest', OPF_NS)}
    spine = root.find('opf:spine', OPF_NS)
    creator_el = root.find('.//dc:creator', OPF_NS)
    creator = creator_el.text if creator_el is not None else '佚名'

    # 索引 -> image href/media-type（按文件名里的数字）
    img_by_idx = {}
    for item in manifest.values():
        href = item.get('href')
        m = re.match(r'image/img-(\d+)\.\w+$', href or '')
        if m:
            img_by_idx[int(m.group(1))] = (href, item.get('media-type'))

    pages = []  # [{img_bytes, media_type, ext}] 按 spine 阅读顺序
    for itemref in spine.findall('opf:itemref', OPF_NS):
        idref = itemref.get('idref')
        if idref in ('titlepage', 'Page_cover'):
            continue
        item = manifest[idref]
        href = item.get('href')  # html/page-0001.html
        m = re.match(r'html/page-(\d+)\.html$', href)
        if not m:
            sys.exit(f'{path}: 无法识别的 spine 页面 {href}')
        idx = int(m.group(1))
        if idx not in img_by_idx:
            sys.exit(f'{path}: 第 {idx} 页找不到对应图片')
        img_href, media_type = img_by_idx[idx]
        ext = '.' + img_href.rsplit('.', 1)[-1]
        pages.append({
            'img_bytes': z.read(img_href),
            'media_type': media_type,
            'ext': ext,
        })

    cover_bytes = z.read('cover.jpeg') if 'cover.jpeg' in z.namelist() else None

    return {
        'path': path,
        'label': label,
        'creator': creator,
        'pages': pages,
        'cover_bytes': cover_bytes,
        'titlepage': z.read('titlepage.xhtml'),
        'cover_html': z.read('html/cover.html'),
        'stylesheet': z.read('stylesheet.css'),
        'page_styles': z.read('page_styles.css'),
        'container_xml': z.read('META-INF/container.xml'),
    }

def build_opf(meta, books):
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
    m.append('    <opf:meta name="primary-writing-mode" content="horizontal-rl" />')
    m.append('  </opf:metadata>')
    m.append('  <opf:manifest>')
    m.append('    <opf:item id="titlepage" href="titlepage.xhtml" media-type="application/xhtml+xml" />')
    m.append('    <opf:item id="Page_cover" href="html/cover.html" media-type="application/xhtml+xml" />')
    for b in books:
        for p in b['pages']:
            m.append(f'    <opf:item id="{p["pid"]}" href="html/{p["html"]}" media-type="application/xhtml+xml" />')
    m.append('    <opf:item id="cover" href="cover.jpeg" media-type="image/jpeg" />')
    m.append('    <opf:item id="css" href="stylesheet.css" media-type="text/css" />')
    m.append('    <opf:item id="page_css" href="page_styles.css" media-type="text/css" />')
    m.append('    <opf:item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />')
    for b in books:
        for p in b['pages']:
            m.append(f'    <opf:item id="{p["img_id"]}" href="image/{p["img_href"]}" media-type="{p["media_type"]}" />')
    m.append('  </opf:manifest>')
    m.append('  <opf:spine toc="ncx" page-progression-direction="rtl">')
    m.append('    <opf:itemref idref="titlepage" />')
    m.append('    <opf:itemref idref="Page_cover" />')
    for b in books:
        for p in b['pages']:
            m.append(f'    <opf:itemref idref="{p["pid"]}" />')
    m.append('  </opf:spine>')
    m.append('  <opf:guide>')
    m.append('    <opf:reference type="cover" href="titlepage.xhtml" title="Cover" />')
    m.append('  </opf:guide>')
    m.append('</opf:package>')
    return '\n'.join(m)

def build_ncx(meta, books):
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
    play_order = 1
    for b in books:
        if not b['pages']:
            continue
        n.append(f'    <navPoint id="Book_{b["book_id"]}" playOrder="{play_order}" class="chapter">')
        n.append('      <navLabel>')
        n.append(f'        <text>{b["label"]}</text>')
        n.append('      </navLabel>')
        n.append(f'      <content src="html/{b["pages"][0]["html"]}" />')
        play_order += 1
        for i, p in enumerate(b['pages'], start=1):
            n.append(f'      <navPoint id="{p["pid"]}" playOrder="{play_order}" class="other">')
            n.append('        <navLabel>')
            n.append(f'          <text>{b["label"]} 第 {i:03d} 頁</text>')
            n.append('        </navLabel>')
            n.append(f'        <content src="html/{p["html"]}" />')
            n.append('      </navPoint>')
            play_order += 1
        n.append('    </navPoint>')
    n.append('  </navMap>')
    n.append('</ncx>')
    return '\n'.join(n)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sources', nargs='+', help='源 EPUB 路径，按合并顺序传入；可用 path::标签 指定章名')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('-t', '--title', required=True)
    ap.add_argument('-a', '--author')
    ap.add_argument('--publisher', default='Kox.moe')
    ap.add_argument('--language', default='zh')
    ap.add_argument('--cover-index', type=int, default=0, help='封面取自第几本源书（默认第一本）')
    ap.add_argument('--cover-fallback-index', type=int, default=-1, help='首选本没有封面时的备用本（默认最后一本）')
    args = ap.parse_args()

    specs = []
    for s in args.sources:
        if '::' in s:
            path, label = s.split('::', 1)
        else:
            path, label = s, label_from_filename(s)
        specs.append((path, label))

    books_raw = [parse_book(path, label) for path, label in specs]

    cover_src = books_raw[args.cover_index]
    if cover_src['cover_bytes'] is None:
        cover_src = books_raw[args.cover_fallback_index]
    if cover_src['cover_bytes'] is None:
        sys.exit('所有候选源书都没有 cover.jpeg')

    author = args.author or books_raw[0]['creator']
    now = datetime.now(timezone.utc)
    meta = {
        'title': args.title, 'author': author, 'publisher': args.publisher,
        'language': args.language, 'uuid': str(uuid.uuid4()),
        'date': now.strftime('%Y-%m-%dT00:00:00+00:00'),
        'timestamp': now.strftime('%Y-%m-%dT%H:%M:%S.%f+00:00'),
    }

    books = []
    g = 0  # 全局页计数器，天然跨本偏移，不会重名
    for bi, b in enumerate(books_raw):
        pages = []
        for p in b['pages']:
            g += 1
            pages.append({
                'pid': f'Page_{g+1}',
                'html': f'page-{g:04d}.html',
                'img_id': f'img_{g}',
                'img_href': f'img-{g:04d}{p["ext"]}',
                'media_type': p['media_type'],
                'img_bytes': p['img_bytes'],
            })
        books.append({'book_id': bi, 'label': b['label'], 'pages': pages})

    total_pages = sum(len(b['pages']) for b in books)
    if total_pages == 0:
        sys.exit('没有任何正文页可合并')

    with zipfile.ZipFile(args.output, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', cover_src['container_xml'], compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('content.opf', build_opf(meta, books), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('toc.ncx', build_ncx(meta, books), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('stylesheet.css', cover_src['stylesheet'], compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('page_styles.css', cover_src['page_styles'], compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('titlepage.xhtml', cover_src['titlepage'], compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('html/cover.html', cover_src['cover_html'], compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('cover.jpeg', cover_src['cover_bytes'], compress_type=zipfile.ZIP_STORED)
        for b in books:
            for p in b['pages']:
                z.writestr(f'html/{p["html"]}', page_html(p['pid'], f'../image/{p["img_href"]}'),
                           compress_type=zipfile.ZIP_DEFLATED)
                z.writestr(f'image/{p["img_href"]}', p['img_bytes'], compress_type=zipfile.ZIP_STORED)

    print(f'已生成: {args.output}')
    print(f'  标题: {meta["title"]} / 作者: {meta["author"]}')
    print(f'  合并 {len(books)} 本，共 {total_pages} 页')
    for b in books:
        print(f'    - {b["label"]}: {len(b["pages"])} 页')
    print(f'  封面来源: {cover_src["path"]}')

if __name__ == '__main__':
    main()
