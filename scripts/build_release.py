#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打一个可直接分发的 zip：python scripts/build_release.py [--version v0.1.0]

产物：dist/sub-merger-<version>.zip
"""

import argparse
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = [
    "merger.py",
    "gui.py",
    "singbox.py",
    "v2ray.py",
    "merger.example.yaml",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "启动界面.bat",
]
SKIP_DIRS = {"__pycache__", ".git", ".github", "dist", ".cache"}


def main() -> int:
    parser = argparse.ArgumentParser(description="build a distributable zip")
    parser.add_argument("--version", default="dev", help="版本号，进 zip 文件名")
    parser.add_argument("--out", default="dist", help="输出目录")
    args = parser.parse_args()

    version = args.version.strip() or "dev"
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"sub-merger-{version}.zip"

    missing = [name for name in FILES if not (ROOT / name).exists()]
    if missing:
        raise SystemExit("缺少文件：" + ", ".join(missing))

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in FILES:
            zf.write(ROOT / name, arcname=f"sub-merger/{name}")
        for path in sorted(ROOT.glob("tests/fixtures/*.yaml")):
            zf.write(path, arcname=f"sub-merger/tests/fixtures/{path.name}")

    size = target.stat().st_size / 1024
    print(f"[ok] 打包完成：{target}（{size:.1f} KB，{len(FILES) + 2} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
