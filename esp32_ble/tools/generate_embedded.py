#!/usr/bin/env python3
"""
Generate embedded_files.h from data/ directory.
Converts all files to C byte arrays for direct inclusion in firmware.
"""
import os, sys, re, zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(PROJECT_DIR, "data")
OUTPUT = os.path.join(PROJECT_DIR, "main", "embedded_files.h")

def mangle(path):
    """Convert file path to valid C identifier."""
    name = path.replace("/", "_").replace(".", "_").replace("-", "_")
    name = re.sub(r'^_+', '', name)  # remove leading underscores
    return "data_" + name

def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    if path.endswith('.css.gz'):  return "text/css"
    if path.endswith('.js.gz'):   return "application/javascript"
    if ext == '.html':   return "text/html"
    if ext == '.css':    return "text/css"
    if ext == '.js':     return "application/javascript"
    if ext == '.png':    return "image/png"
    if ext == '.webp':   return "image/webp"
    if ext == '.jpg' or ext == '.jpeg': return "image/jpeg"
    if ext == '.svg':    return "image/svg+xml"
    if ext == '.ico':    return "image/x-icon"
    if ext == '.json':   return "application/json"
    if ext == '.gz':     return "application/octet-stream"
    return "application/octet-stream"

def encoding(path):
    if path.endswith('.gz'):
        return "gzip"
    return None

def process():
    files = []
    for root, dirs, fnames in os.walk(DATA_DIR):
        for fname in sorted(fnames):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, DATA_DIR)
            # Skip .gz files if original exists (we'll create both entries)
            if fname.endswith('.gz'):
                orig = fpath[:-3]
                if os.path.exists(orig):
                    continue  # handle via the non-.gz entry
            files.append((rel, fpath))

    lines = []
    lines.append("#ifndef EMBEDDED_FILES_H")
    lines.append("#define EMBEDDED_FILES_H")
    lines.append("")
    lines.append("#include <stddef.h>")
    lines.append("#include <stdint.h>")
    lines.append("")
    lines.append("typedef struct { const char *path; const char *content_type; const char *encoding; const unsigned char *data; size_t size; uint32_t crc; } EmbeddedFile;")
    lines.append("")

    entries = []
    for rel, fpath in files:
        with open(fpath, 'rb') as f:
            data = f.read()
        var = mangle(rel)
        crc = zlib.crc32(data) & 0xFFFFFFFF
        lines.append(f"static const unsigned char {var}[] = {{")
        for i in range(0, len(data), 16):
            chunk = data[i:i+16]
            hexvals = ', '.join(f"0x{b:02x}" for b in chunk)
            lines.append(f"  {hexvals},")
        lines.append("};")
        lines.append("")
        entries.append((rel, content_type(rel), encoding(rel), var, len(data), crc))

        # Also create a .gz alias if this is a .gz file
        if rel.endswith('.gz'):
            alias = rel[:-3]
            entries.append((alias, content_type(alias), "gzip", var, len(data), crc))

    lines.append("static const EmbeddedFile embedded_files[] = {")
    for rel, ct, enc, var, size, crc in entries:
        enc_str = f'"{enc}"' if enc else "NULL"
        lines.append(f'  {{"/{rel}", "{ct}", {enc_str}, {var}, {size}, 0x{crc:08x}u}},')
    lines.append("  {NULL, NULL, NULL, NULL, 0, 0}")
    lines.append("};")
    lines.append("#endif")

    with open(OUTPUT, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"Generated {OUTPUT} with {len(entries)} file entries")

if __name__ == '__main__':
    process()
