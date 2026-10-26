#!/usr/bin/env node
/**
 * 全量语法体检：web/ 下**每一个**要发到浏览器的脚本都必须能被解析。
 *
 * 为什么需要它：这批前端没有任何构建步骤。其它 *_test.js 套件只会加载"用到"的
 * 文件（smoke_test 的 index/phone 链、device_bench_test 的 device-store/device-bench…），
 * 于是 web/static 下没进任何一条链的 .js、以及 web/*.html 里的内联 <script>，
 * 写错一个括号也没人管 —— 浏览器里的表现是**整段脚本静默失效**（白屏、某个按钮
 * 永远没反应、下拉菜单点不开），控制台之外没有任何反馈，CI 更是全绿。
 *
 * 这里只做 `vm.Script` 解析（不执行），成本可忽略，但把"语法错"从"上线后猜"
 * 变成"提交时红"。
 *
 * Usage: node tests/js/syntax_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.resolve(__dirname, '../../web');

/**
 * 第三方压缩产物不在守护范围内：`static/chart.umd.min.js` 是 Chart.js 官方
 * UMD 包（`chart-loader.js` 从 CDN 取，本地这份只是离线兜底）。它是压缩产物、
 * 不由本仓库编辑，而 vendor 包一旦改用 ESM 语法（`import()` / 顶层 `await`），
 * `vm.Script` 会解析失败——那是一次和本仓库改动无关的常红，反而教人跳过这个测试。
 * 我们真正要守的是"自己写的脚本有没有语法错"。
 */
const VENDOR = /[\\/](?:[^\\/]+[.-]min|[^\\/]+[.-]bundle)[^\\/]*\.js$/;

/** 只解析真正的 JS：`type="application/json"` / `text/template` 这类块故意不是代码
 *  （JSON 里一个裸 `{...}` 会被当成代码块解析然后报错，同样是无关的常红）。 */
const JS_TYPE = /^(?:|module|text\/javascript|application\/javascript|text\/ecmascript|application\/ecmascript)$/i;

let failed = 0;
let passed = 0;
function ok(cond, label, detail) {
    if (cond) { passed++; console.log(`  ok   ${label}`); }
    else { failed++; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}

/** 解析失败时把首行错误摘出来，整段栈对"哪一行错了"没有帮助反而淹掉结论。 */
function parseError(src, filename) {
    try {
        new vm.Script(src, { filename });
        return null;
    } catch (e) {
        return String((e && e.message) || e).split('\n')[0];
    }
}

function walk(dir, out = []) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(p, out);
        else out.push(p);
    }
    return out;
}

console.log('── JS 文件 ──');
const skipped = [];
const jsFiles = walk(WEB).filter((p) => {
    if (!p.endsWith('.js')) return false;
    if (VENDOR.test(p)) { skipped.push(path.relative(WEB, p)); return false; }
    return true;
});
if (skipped.length) console.log(`  --   跳过第三方产物：${skipped.join(', ')}`);
ok(jsFiles.length > 0, `找到 ${jsFiles.length} 个 .js`, 'web/ 下没有脚本？路径不对');
for (const file of jsFiles) {
    const rel = path.relative(WEB, file);
    const err = parseError(fs.readFileSync(file, 'utf8'), rel);
    ok(err === null, `${rel} 可解析`, err);
}

console.log('\n── HTML 内联脚本 ──');
const htmlFiles = walk(WEB).filter((p) => p.endsWith('.html'));
ok(htmlFiles.length > 0, `找到 ${htmlFiles.length} 个 .html`);
for (const file of htmlFiles) {
    const rel = path.relative(WEB, file);
    const html = fs.readFileSync(file, 'utf8');
    // 只取**没有 src 属性**的 <script>（外链的由上面逐个解析，重复解也没意义）
    const inline = [...html.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/g)]
        .filter((m) => {
            const type = /\btype\s*=\s*["']?([^"'\s>]+)/i.exec(m[1]);
            return !type || JS_TYPE.test(type[1]);      // 只认 JS（含 module）
        })
        .map((m) => m[2]);        // 只要脚本正文
    if (inline.length === 0) {
        console.log(`  --   ${rel}（无内联脚本）`);
        continue;
    }
    inline.forEach((src, i) => {
        ok(typeof src === 'string' && src.length > 0,
           `${rel} 内联脚本 #${i + 1} 取到正文`, `拿到 ${typeof src}`);
        const err = parseError(src, `${rel}#inline${i + 1}`);
        ok(err === null, `${rel} 内联脚本 #${i + 1} 可解析`, err);
    });
}

console.log(failed === 0
    ? `\nAll ${passed} syntax checks passed.`
    : `\n${failed}/${passed + failed} syntax checks FAILED.`);
process.exit(failed === 0 ? 0 : 1);
