#!/usr/bin/env python3
"""Post-i18n verification for the CUKTECH ble_server web UI.

1. Loads zh-CN / en locale packs via Node (same mechanism as the browser) and
   flattens their keys.
2. Scans HTML/JS for i18n key usage (t() calls + data-i18n* attributes) and
   cross-checks every used key against both packs.
3. Detects hardcoded CJK user-visible text (HTML text nodes without a
   data-i18n* attribute; JS string literals outside comments), so nothing
   visible is left untranslated.
"""
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
LOCALE_DIR = WEB / "static" / "locales"
NODE = "/vol1/@appcenter/nodejs_v22/bin/node"

CJK = re.compile(r"[\u4e00-\u9fff]")
# keys from t() calls: I18N.t('a.b') / t('a.b'), but not word-embedded "t("
# and not string concatenations like t('scene.' + x)
T_CALL = re.compile(r"(?:I18N\.)?\bt\(\s*['\"]([A-Za-z0-9_.\-]+)['\"](?!\s*\+)")
# data-i18n* attribute values that carry a translation key
KEY_ATTRS = ("data-i18n", "data-i18n-placeholder", "data-i18n-title", "data-i18n-html", "data-i18n-aria-label")

EXCLUDE_FILES = {"chart.umd.min.js", "zh-CN.js", "en.js", "i18n.js"}

# Known intentional CJK in code (not UI copy):
#  - server-contract error text comparison (backend returns Chinese errors)
#  - the charger device's own display-language labels (device setting values)
#  - native name "中文" in the language <select> options (language lists are
#    conventionally shown in their own language and are not translated)
KNOWN_CJK_LINES = re.compile(r"data\.error\.includes\('等待扫码'\)|label: '中文'|value=\"zh-CN\">中文")

# Keys that are bound at runtime via config tables / string concatenation and
# therefore do not appear as literal t('...') calls (settings.*, scene.desc*).
DYNAMIC_KEY_PREFIXES = ("settings.", "scene.desc")


def node_locale_keys():
    script = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const dir=process.env.LOCALE_DIR;
const sandbox={console};
sandbox.window=sandbox;
const ctx=vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(dir,'zh-CN.js'),'utf8'),ctx);
vm.runInContext(fs.readFileSync(path.join(dir,'en.js'),'utf8'),ctx);
function flatten(d,p=''){const o={};for(const k in d){const key=p?p+'.'+k:k;
 const v=d[k];
 if(v&&typeof v==='object'&&!Array.isArray(v)){
   // plural forms {one,other,...} are one logical key
   const cats=v.one||v.other||v.few||v.many||v.zero||v.two;
   if(cats!==undefined){o[key]=true;} else {Object.assign(o,flatten(v,key));}
 } else o[key]=true;}return o;}
const zh=ctx.I18N_RESOURCES['zh-CN'], en=ctx.I18N_RESOURCES['en'];
process.stdout.write(JSON.stringify({zhKeys:Object.keys(flatten(zh)),enKeys:Object.keys(flatten(en))}));
"""
    r = subprocess.run(
        [NODE, "-e", script],
        env={**__import__("os").environ, "LOCALE_DIR": str(LOCALE_DIR)},
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)


class HtmlScan(HTMLParser):
    """Collects: key usage, and CJK text nodes lacking data-i18n* attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.cjk_hits = []
        self.stack = []  # (tag, attrs_dict)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.scan_text_context(a)
        self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        self.scan_text_context(dict(attrs))

    def handle_endtag(self, tag):
        if self.stack:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break

    def handle_data(self, data):
        if not data or not CJK.search(data):
            return
        # inside <script>/<style>: handled separately as JS/CSS, not HTML text
        for tag, _ in self.stack:
            if tag in ("script", "style", "title"):
                return
        # native language names inside the web-language <select> are intentionally
        # untranslated (language lists are conventionally shown in their own language)
        if data.strip().rstrip('。：') in ("中文",) and any(
            tag == "select" and attrs.get("id") == "web_language" for tag, attrs in self.stack
        ):
            return
        for tag, attrs in self.stack:
            if any(k.startswith("data-i18n") for k in attrs):
                return
        line = self.getpos()[0] if hasattr(self, "getpos") else "?"
        self.cjk_hits.append((line, data.strip()[:100]))

    def scan_text_context(self, attrs):
        # record key usage from attributes (order-independent of data-i18n)
        for k, v in attrs.items():
            if k in KEY_ATTRS and v:
                self.keys_used.append(v)


def strip_js_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for line in text.splitlines():
        out.append(re.sub(r"//[^\n]*$", "", line))
    return "\n".join(out)


def main():
    loc = node_locale_keys()
    zh_keys, en_keys = set(loc["zhKeys"]), set(loc["enKeys"])
    print(f"locale packs: zh-CN ({len(zh_keys)} keys), en ({len(en_keys)} keys)")
    diff = sorted(zh_keys - en_keys)
    if diff:
        print(f"  keys in zh-CN missing from en: {diff}")
    diff2 = sorted(en_keys - zh_keys)
    if diff2:
        print(f"  keys in en missing from zh-CN: {diff2}")

    used = {}
    html_cjk = []
    js_cjk = []

    def scan_js_into(rel, raw, used_map, cjk_list, stripped=None):
        for m in T_CALL.finditer(raw):
            used_map.setdefault(m.group(1), set()).add(rel)
        src = stripped if stripped is not None else raw
        for i, line in enumerate(src.splitlines(), 1):
            if CJK.search(line) and not KNOWN_CJK_LINES.search(line):
                cjk_list.append((rel, i, line.strip()[:110]))

    for path in sorted(WEB.rglob("*")):
        if not path.is_file() or path.suffix not in (".html", ".js"):
            continue
        if path.name in EXCLUDE_FILES or "locales" in path.parts:
            continue
        rel = str(path.relative_to(WEB))
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".html":
            parser = HtmlScan()
            parser.keys_used = []
            parser.feed(text)
            parser.close()
            for k in parser.keys_used:
                if k:
                    used.setdefault(k, set()).add(rel)
            html_cjk.extend(
                (rel, ln, sn) for ln, sn in parser.cjk_hits
                if not KNOWN_CJK_LINES.search(sn)
            )
            # inline <script> bodies also get the JS treatment
            for m in re.finditer(r"<script[^>]*>(.*?)</script>", text, flags=re.S):
                body = strip_js_comments(m.group(1))
                scan_js_into(rel, body, used, js_cjk)
        elif path.suffix == ".js":
            for m in T_CALL.finditer(text):
                used.setdefault(m.group(1), set()).add(rel)
            stripped = strip_js_comments(text)
            scan_js_into(rel, text, used, js_cjk, stripped=stripped)

    print("\n── key usage vs packs ──")
    missing = {k: v for k, v in sorted(used.items()) if k not in zh_keys}
    if missing:
        print("USED KEYS MISSING from packs:")
        for k, files in missing.items():
            print(f"  {k}   <- {', '.join(sorted(files))}")
    else:
        print(f"All {len(used)} used keys are defined in both packs. ✓")
    unused = sorted(
        k for k in (zh_keys - set(used))
        if not k.startswith(DYNAMIC_KEY_PREFIXES)
    )
    print(f"Unused keys ({len(unused)}): {unused if len(unused) <= 60 else unused[:60] + ['…']}")

    print("\n── hardcoded CJK (HTML text without data-i18n, JS literals) ──")
    all_hits = sorted((*h, "html") for h in html_cjk) + sorted((*h, "js") for h in js_cjk)
    if not all_hits:
        print("None. ✓")
    for rel, line, sn, kind in all_hits:
        print(f"  [{kind}] {rel}:{line}: {sn}")
    bad = bool(missing) or bool(all_hits) or bool(diff)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()