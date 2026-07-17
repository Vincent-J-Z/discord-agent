#!/usr/bin/env bash
# 批量把 manhuagui 某部漫画的连续若干话打包成 EPUB（每话一本）。
#
# 用法:
#   ./batch.sh <comicId> <起始话> <结束话> <书名> <作者> [输出根目录]
# 例:
#   ./batch.sh 49036 10 20 "被追放的转生重骑士用游戏知识开无双" "武六甲理衣、猫子"
#
# 依赖: node (含本目录 node_modules/lz-string), python3
set -euo pipefail
cd "$(dirname "$0")"

COMIC_ID="${1:?需要 comicId}"
START="${2:?需要起始话序号}"
END="${3:?需要结束话序号}"
BOOK="${4:?需要书名}"
AUTHOR="${5:?需要作者}"
OUTROOT="${6:-./out}"

WORK="$OUTROOT/_images/$BOOK"
EPUBDIR="$OUTROOT/epub"
mkdir -p "$WORK" "$EPUBDIR"

# 官方封面图（g = 大图变体）：https://cf.hamreus.com/cpic/g/<comicId>.jpg
COVER_FILE="$OUTROOT/cover_${COMIC_ID}.jpg"
COVER_ARGS=()
echo "== 获取官方封面 (comic $COMIC_ID) =="
if curl -fsSL -A "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1" \
     -e "https://m.manhuagui.com/" \
     -o "$COVER_FILE" "https://cf.hamreus.com/cpic/g/${COMIC_ID}.jpg" \
   && [ -s "$COVER_FILE" ]; then
  echo "封面已下载: $COVER_FILE ($(wc -c < "$COVER_FILE") 字节)"
  COVER_ARGS=(--cover "$COVER_FILE")
else
  echo "警告: 官方封面下载失败，回退为用第一张内页当封面（旧行为）"
  rm -f "$COVER_FILE"
fi

echo "== 获取章节列表 (comic $COMIC_ID) =="
CHAPTERS="$OUTROOT/chapters_${COMIC_ID}.tsv"
node list_chapters.js "$COMIC_ID" > "$CHAPTERS"
TOTAL=$(wc -l < "$CHAPTERS" | tr -d ' ')
echo "共 $TOTAL 话，处理第 $START ~ $END 话"

ok=0; fail=0
for ((n=START; n<=END; n++)); do
  line=$(awk -F'\t' -v k="$n" '$1==k' "$CHAPTERS")
  if [ -z "$line" ]; then echo "[$n] 跳过：无此话"; continue; fi
  cid=$(echo "$line" | cut -f2)
  title=$(echo "$line" | cut -f3)
  url=$(echo "$line" | cut -f4)
  imgdir="$WORK/$title"
  echo ""
  echo "== [$n/$END] $title ($url) =="

  # 1) 下载整话图片（失败自动重试一次）
  if ! node download_chapter.js "$url" "$imgdir" ; then
    echo "  首次下载失败，重试..."
    sleep 2
    node download_chapter.js "$url" "$imgdir" || { echo "  [$title] 下载失败，跳过"; fail=$((fail+1)); continue; }
  fi

  # 2) 打包 EPUB
  out="$EPUBDIR/${BOOK} - ${title}.epub"
  python3 make_epub.py "$imgdir" -o "$out" -t "${BOOK} - ${title}" -a "$AUTHOR" "${COVER_ARGS[@]}"
  ok=$((ok+1))

  # 3) 轻微限速，别把站点打爆
  sleep 1
done

echo ""
echo "== 批处理完成：成功 $ok 本，失败 $fail 本 =="
echo "EPUB 输出目录: $EPUBDIR"
ls -1 "$EPUBDIR" 2>/dev/null || true
