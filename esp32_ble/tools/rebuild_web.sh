#!/usr/bin/env bash
# ============================================================================
# rebuild_web.sh — 重建 ESP32 内嵌 Web 资源
#
# 用途: 修改前端后一键完成 压缩 + 重新生成嵌入头文件
#   - 将 data/ 下未压缩的 *.html 以 gzip -9 压缩（输出 *.html.gz 并删除原件）
#   - 将 data/ 下所有 *.css / *.js 压缩（tools/gzip_compress.py，就地压缩删除原件）
#   - 重新生成 main/embedded_files.h（tools/generate_embedded.py）
#
# 日常编辑流程:
#   1. zcat data/phone.html.gz > data/phone.html        # 解压出可编辑源文件
#   2. 编辑 data/phone.html / data/static/phone.js 等
#   3. bash tools/rebuild_web.sh                          # 压缩并重建头文件
#   4. idf.py build
#
# 注意: 此脚本会删除 data/ 下的未压缩原件，使其与仓库既有约定一致
#       （data/ 仅保存 .gz 文件，由 generate_embedded.py 嵌入固件）。
#       请勿在 data/ 中同时保留 原件 + .gz（会导致原件被嵌入、体积翻倍）。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

DATA_DIR="data"

# 1) HTML: gzip -9（HTML 不走 gzip_compress.py）
for f in "$DATA_DIR"/*.html; do
    [ -e "$f" ] || continue
    gzip -9 -c "$f" > "$f.gz"
    rm "$f"
    echo "gzip: ${f#$DATA_DIR/}.gz ($(stat -c%s "$f.gz") bytes)"
done

# 2) CSS/JS: 就地压缩并删除原始文件
echo "gzip: static css/js ..."
python3 tools/gzip_compress.py

# 3) 重新生成嵌入式头文件
echo "generate: main/embedded_files.h ..."
python3 tools/generate_embedded.py

echo "Done. 请执行 idf.py build 验证固件。"