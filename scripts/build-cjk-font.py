#!/usr/bin/env python3
"""构建思源宋体（Noto Serif SC 可变字体）分片 woff2。

策略：全集不裁剪字符，按常用度与 Unicode 区段切分为多个 woff2，
由 CSS unicode-range 让浏览器按需下载。拉丁区段留给 EB Garamond。

输入：fonts-build/NotoSerifSC-VF.ttf
输出：web/fonts/NotoSerifSC-<片名>.woff2、fonts-build/cjk-faces.css
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "fonts-build" / "NotoSerifSC-VF.ttf"
OUT_DIR = ROOT / "web" / "fonts"
CSS_OUT = ROOT / "fonts-build" / "cjk-faces.css"

# 交由 MA Latin（EB Garamond）接管的区段，CJK 分片不再包含
LATIN_RANGES = [
    (0x0000, 0x024F),  # 基本拉丁、拉丁补充、扩展 A/B
    (0x1E00, 0x1EFF),  # 拉丁扩展附加
    (0x2000, 0x206F),  # 通用标点（弯引号、破折号等，维持现状由 EB Garamond 渲染）
    (0x20A0, 0x20CF),  # 货币符号
    (0x2100, 0x214F),  # 字母式符号
    (0x2190, 0x21FF),  # 箭头
]


def gb2312_chars(hi_start: int, hi_end: int) -> set[int]:
    codepoints: set[int] = set()
    for hi in range(hi_start, hi_end + 1):
        for lo in range(0xA1, 0xFF):
            try:
                ch = bytes([hi, lo]).decode("gb2312")
            except UnicodeDecodeError:
                continue
            codepoints.add(ord(ch))
    return codepoints


def in_ranges(cp: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def to_unicode_range(codepoints: set[int]) -> str:
    """码位集合 → CSS unicode-range 区间串。"""
    if not codepoints:
        return ""
    points = sorted(codepoints)
    parts: list[str] = []
    start = prev = points[0]
    for cp in points[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        parts.append(f"U+{start:X}" if start == prev else f"U+{start:X}-{prev:X}")
        start = prev = cp
    parts.append(f"U+{start:X}" if start == prev else f"U+{start:X}-{prev:X}")
    return ",".join(parts)


def build_slice(name: str, codepoints: set[int], cmap: set[int]) -> dict | None:
    points = sorted(codepoints & cmap)
    if not points:
        return None
    out = OUT_DIR / f"NotoSerifSC-{name}.woff2"
    text = "".join(chr(cp) for cp in points)
    subset_input = ROOT / "fonts-build" / f"{name}.txt"
    subset_input.write_text("".join(f"{cp:X}\n" for cp in points), encoding="utf-8")
    cmd = [
        sys.executable, "-m", "fontTools.subset", str(SRC),
        f"--unicodes-file={subset_input}",
        "--layout-features=*",
        "--flavor=woff2",
        f"--output-file={out}",
    ]
    print(f"[build] {name}: {len(points)} 字符 → {out.name}", flush=True)
    subprocess.run(cmd, check=True)
    size = out.stat().st_size
    print(f"[build] {name}: {size/1024/1024:.2f} MB", flush=True)
    return {"name": name, "file": out.name, "ranges": to_unicode_range(set(points)), "size": size}


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"缺少源字体：{SRC}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    font = TTFont(SRC, lazy=True)
    cmap = set(font.getBestCmap().keys())
    font.close()
    print(f"[build] 源字体 cmap：{len(cmap)} 个码位", flush=True)

    core = gb2312_chars(0xB0, 0xD7)      # GB2312 一级（常用）
    gb2 = gb2312_chars(0xD8, 0xF7)       # GB2312 二级（次常用）
    basic = {cp for cp in cmap if 0x4E00 <= cp <= 0x9FFF}
    exta = {cp for cp in cmap if 0x3400 <= cp <= 0x4DBF}
    extb = {cp for cp in cmap if cp >= 0x20000}
    symbols = {
        cp for cp in cmap
        if in_ranges(cp, [(0x2E80, 0x2EFF), (0x3000, 0x303F), (0x3040, 0x30FF),
                          (0x3100, 0x312F), (0x31C0, 0x31EF), (0x3200, 0x32FF),
                          (0xFE10, 0xFE1F), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF)])
    }

    planned = core | gb2 | basic | exta | extb | symbols
    misc = {
        cp for cp in cmap
        if not in_ranges(cp, LATIN_RANGES) and cp not in planned
    }

    slices = [
        ("core", core),
        ("gb2", gb2),
        ("basic-rest", basic - core - gb2),
        ("exta", exta),
        ("extb", extb),
        ("symbols", symbols),
        ("misc", misc),
    ]

    results = []
    for name, points in slices:
        built = build_slice(name, points, cmap)
        if built:
            results.append(built)

    faces = []
    for item in results:
        faces.append(
            '@font-face{font-family:"MA CJK";'
            f'src:url("/fonts/{item["file"]}") format("woff2-variations");'
            'font-weight:200 900;font-style:normal;font-display:swap;'
            f'unicode-range:{item["ranges"]}}}'
        )
    CSS_OUT.write_text("\n".join(faces) + "\n", encoding="utf-8")
    total = sum(item["size"] for item in results)
    print(f"[build] 完成：{len(results)} 片，合计 {total/1024/1024:.2f} MB → {CSS_OUT}", flush=True)


if __name__ == "__main__":
    main()
