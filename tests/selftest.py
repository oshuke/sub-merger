#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sub-merger 自测：覆盖 proxy-providers 内联、base64 报错、含逗号 payload 的规则。"""

import base64
import sys
import shutil
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import merger  # noqa: E402

US = r"(?i)(美国|🇺🇸|\bUSA?\b|US[-_]?\d)"


def make_provider(path: Path) -> None:
    data = {
        "proxies": [
            {"name": "🇺🇸 洛杉矶 A", "type": "ss", "server": "p1.example", "port": 443,
             "cipher": "aes-128-gcm", "password": "x1"},
            {"name": "🇺🇸 西雅图 B", "type": "trojan", "server": "p2.example", "port": 443,
             "password": "x2"},
            {"name": "🇯🇵 东京 C", "type": "ss", "server": "p3.example", "port": 443,
             "cipher": "aes-128-gcm", "password": "x3"},
        ]
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def make_sub(path: Path, provider_url: str) -> None:
    data = {
        "proxy-providers": {
            "p1": {
                "type": "http",
                "url": provider_url,
                "path": "./providers/p1.yaml",
                "filter": "美国|🇺🇸",
                "override": {"additional-prefix": "P-"},
            }
        },
        "rules": [
            "DOMAIN-REGEX,^https?://a,b,节点选择",
            "DOMAIN-SUFFIX,openai.com,自动选择",
            "MATCH,节点选择",
        ],
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def main() -> int:
    tmp_path = Path(__file__).resolve().parent / ".tmp-selftest"
    shutil.rmtree(tmp_path, ignore_errors=True)
    tmp_path.mkdir(parents=True)
    if True:
        make_provider(tmp_path / "provider.yaml")
        make_sub(tmp_path / "sub.yaml", (tmp_path / "provider.yaml").as_uri())
        cfg_path = tmp_path / "merger.yaml"
        cfg_path.write_text(
            yaml.safe_dump(
                {
                    "network": {"proxy": "", "timeout": 5},
                    "defaults": {"filter": US},
                    "subscriptions": [
                        {"name": "S", "path": "sub.yaml", "primary": True}
                    ],
                    "output": {"path": "out.yaml"},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        cfg = merger.load_config(cfg_path)
        collected = merger.collect(cfg, tmp_path, offline=False)
        names = [n["name"] for n in collected["nodes"]]
        assert names == ["[S] P-🇺🇸 洛杉矶 A", "[S] P-🇺🇸 西雅图 B"], names

        config = merger.assemble(collected, cfg, collected["nodes"])
        assert "DOMAIN-REGEX,^https?://a,b,手动选择" in config["rules"], config["rules"]
        assert "DOMAIN-SUFFIX,openai.com,自动选择" in config["rules"], config["rules"]
        assert config["rules"][-1] == "MATCH,PROXY"
        assert merger.validate(config, collected["nodes"]) == []
    shutil.rmtree(tmp_path, ignore_errors=True)

    b64 = base64.b64encode(
        b"ss://YWVzLTEyOC1nY206cGFzcw@1.2.3.4:443#test\nvmess://eyJ2IjoiMiJ9"
    ).decode()
    try:
        merger.parse_clash("X", b64)
    except merger.MergerError as exc:
        assert "v2ray" in str(exc)
    else:
        raise AssertionError("base64 v2ray 订阅应当报错")

    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
