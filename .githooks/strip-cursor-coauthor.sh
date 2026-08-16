#!/bin/sh
# Remove Cursor co-author trailers that editors/agents inject.
set -e
msg_file=$1
if [ ! -f "$msg_file" ]; then
  exit 0
fi
tmp=${msg_file}.cursor-strip
# GNU and BSD sed both accept this BRE for a fixed email line.
sed '/^Co-authored-by: Cursor <cursoragent@cursor.com>[[:space:]]*$/d' "$msg_file" | \
  sed '/^Co-authored-by: Cursor[[:space:]]*$/d' > "$tmp"
mv "$tmp" "$msg_file"
