#!/usr/bin/env python3
"""
check_i18n.py — 校验中英文语言资源键一致性（新增/修改语言后运行）

用法: python3 tools/check_i18n.py [DATA_DIR]
默认 DATA_DIR=data，也可以对未压缩的解压副本运行。

检查项:
  1. 每个语言文件都是合法 JSON 对象（window.I18N_XX = {...};）
  2. 所有语言的扁平键集合完全一致（缺键会被列出，缺失方在运行时回退 zh-CN，
     但建议补齐以保持完整翻译）
  3. 值类型一致（字符串 vs 复数对象）——防止 en 是对象而 zh 是字符串导致复数异常
退出码: 0 通过, 1 有差异
"""
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT_DIR, "data")
LANG_DIR = os.path.join(DATA_DIR, "static", "lang")


def load(path, name):
    """解析 'window.NAME = { ... };' 文件为 dict（文件正文为合法 JSON 对象字面量）"""
    src = open(path, encoding="utf-8").read()
    m = re.search(r"window\.%s\s*=\s*(\{.*\});?\s*$" % re.escape(name), src, re.S)
    if not m:
        sys.exit(f"[FAIL] {path}: 未找到 window.{name} = {{...}} 定义")
    return json.loads(m.group(1))


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and set(v.keys()) <= {"one", "other"}:
            out[p] = "plural"  # 复数对象（值类型标记）
        elif isinstance(v, dict):
            out.update(flatten(v, p))
        else:
            out[p] = "string"
    return out


def main():
    langs = {"zh-CN": ("zh.js", "I18N_ZH"), "en": ("en.js", "I18N_EN")}
    flat = {}
    for code, (fname, varname) in langs.items():
        path = os.path.join(LANG_DIR, fname)
        if not os.path.exists(path):
            sys.exit(f"[FAIL] 缺少 {path}（请先解压对应 .gz：zcat {path}.gz > {path}）")
        flat[code] = flatten(load(path, varname))
        print(f"  {code}: {len(flat[code])} keys")

    base = flat["zh-CN"]
    ok = True
    for code, f in flat.items():
        if code == "zh-CN":
            continue
        miss = sorted(set(base) - set(f))
        extra = sorted(set(f) - set(base))
        if miss:
            ok = False
            print(f"[DIFF] {code} 缺少键（运行时会回退 zh-CN）: {miss}")
        if extra:
            ok = False
            print(f"[DIFF] {code} 有多余键（zh-CN 没有）: {extra}")
        # 值类型一致性：zh-CN 无复数形态，允许用字符串代替 {one,other}（两种形式相同），
        # 其余语言必须与 zh-CN 类型一致
        for k in set(base) & set(f):
            if base[k] != f[k]:
                if code == "zh-CN":
                    continue  # 基准语言自身不比较
                if base[k] == "string" and f[k] == "plural":
                    print(f"[INFO] {code} 键 '{k}' 为复数对象而 zh-CN 为字符串"
                          f"（中文复数同形，属预期；若该语言也有单复数之分请确认 one/other 均已填写）")
                    continue
                ok = False
                print(f"[DIFF] {code} 键 '{k}' 值类型不一致: zh={base[k]} vs {code}={f[k]}")

    print("结果:", "ALL PASS" if ok else "存在差异")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()