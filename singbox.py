#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把合并后的 Clash 节点池导出成 sing-box 配置。

只做“节点 + 精简策略组”，分流规则仍是 sing-box 自己的 final，
因为 Clash 的 RULE-SET / GEOIP 语法和 sing-box 不通用。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

UTLS = {"chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random"}

UNSUPPORTED = {
    "ssr": "sing-box 已移除 shadowsocksr",
    "hysteria": "hysteria v1 已移除，建议用 hysteria2",
    "wireguard": "wireguard 要写成 endpoint，暂不导出",
    "snell": "snell 需要版本号等额外字段，暂不导出",
}


def _tls(proxy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    reality = proxy.get("reality-opts") or {}
    if not proxy.get("tls") and not reality.get("public-key"):
        return None
    tls: Dict[str, Any] = {"enabled": True}
    sni = proxy.get("servername") or proxy.get("sni") or proxy.get("server")
    if sni:
        tls["server_name"] = sni
    if proxy.get("skip-cert-verify"):
        tls["insecure"] = True
    alpn = proxy.get("alpn")
    if alpn:
        tls["alpn"] = alpn
    fp = str(proxy.get("client-fingerprint") or "").lower()
    if fp in UTLS:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    if reality.get("public-key"):
        tls["reality"] = {"enabled": True, "public_key": reality["public-key"]}
        if reality.get("short-id"):
            tls["reality"]["short_id"] = str(reality["short-id"])
    return tls


def _transport(proxy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    net = str(proxy.get("network") or "").lower()
    if net == "ws":
        opts = proxy.get("ws-opts") or {}
        tr: Dict[str, Any] = {"type": "ws", "path": opts.get("path") or "/"}
        headers = opts.get("headers") or {}
        host = headers.get("Host") or headers.get("host")
        if host:
            tr["headers"] = {"Host": host}
        if opts.get("max-early-data"):
            tr["max_early_data"] = opts["max-early-data"]
        if opts.get("early-data-header-name"):
            tr["early_data_header_name"] = opts["early-data-header-name"]
        return tr
    if net == "grpc":
        opts = proxy.get("grpc-opts") or {}
        return {"type": "grpc", "service_name": opts.get("grpc-service-name") or ""}
    if net in ("h2", "http"):
        opts = proxy.get("h2-opts") or proxy.get("http-opts") or {}
        tr = {"type": "http", "path": opts.get("path") or "/"}
        host = opts.get("host")
        if isinstance(host, str) and host:
            tr["host"] = [host]
        elif isinstance(host, list) and host:
            tr["host"] = host
        return tr
    return None


def _plugin_opts(opts: Any) -> str:
    if isinstance(opts, str):
        return opts
    if not isinstance(opts, dict):
        return ""
    parts = []
    for key, val in opts.items():
        parts.append(str(key) if val is True else f"{key}={val}")
    return ";".join(parts)


def _mbps(value: Any) -> Optional[int]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.findall(r"\d+", str(value))
    return int(digits[0]) if digits else None


def node(proxy: Dict[str, Any], tag: str) -> Dict[str, Any]:
    """Clash proxy → sing-box outbound。不支持的抛 ValueError。"""
    kind = str(proxy.get("type") or "").lower()
    server = proxy.get("server")
    port = proxy.get("port")
    if not server or not port:
        raise ValueError("缺少服务器或端口")
    if kind in UNSUPPORTED:
        raise ValueError(UNSUPPORTED[kind])

    out: Dict[str, Any] = {"type": kind, "tag": tag, "server": server, "server_port": int(port)}
    tls = _tls(proxy)
    tr = _transport(proxy)

    if kind == "ss":
        out["type"] = "shadowsocks"
        out["method"] = proxy.get("cipher")
        out["password"] = proxy.get("password")
        if proxy.get("plugin"):
            out["plugin"] = proxy["plugin"]
            opts = _plugin_opts(proxy.get("plugin-opts"))
            if opts:
                out["plugin_opts"] = opts
    elif kind == "vmess":
        out["type"] = "vmess"
        out["uuid"] = proxy.get("uuid")
        out["security"] = proxy.get("cipher") or "auto"
        out["alter_id"] = int(proxy.get("alterId") or 0)
    elif kind == "vless":
        out["type"] = "vless"
        out["uuid"] = proxy.get("uuid")
        if proxy.get("flow"):
            out["flow"] = proxy["flow"]
    elif kind == "trojan":
        out["type"] = "trojan"
        out["password"] = proxy.get("password")
    elif kind in ("hysteria2", "hy2"):
        out["type"] = "hysteria2"
        out["password"] = proxy.get("password") or proxy.get("auth")
        if proxy.get("obfs"):
            out["obfs"] = {"type": proxy["obfs"], "password": proxy.get("obfs-password") or ""}
        up = _mbps(proxy.get("up"))
        if up:
            out["up_mbps"] = up
        down = _mbps(proxy.get("down"))
        if down:
            out["down_mbps"] = down
    elif kind == "tuic":
        out["type"] = "tuic"
        out["uuid"] = proxy.get("uuid")
        out["password"] = proxy.get("password")
        if proxy.get("congestion-controller"):
            out["congestion_control"] = proxy["congestion-controller"]
        if proxy.get("udp-relay-mode"):
            out["udp_relay_mode"] = proxy["udp-relay-mode"]
    elif kind in ("socks5", "socks"):
        out["type"] = "socks"
        if proxy.get("username"):
            out["username"] = proxy["username"]
        if proxy.get("password"):
            out["password"] = proxy["password"]
    elif kind == "http":
        out["type"] = "http"
        if proxy.get("username"):
            out["username"] = proxy["username"]
        if proxy.get("password"):
            out["password"] = proxy["password"]
    else:
        raise ValueError(f"未支持的节点类型 {kind}")

    if kind == "trojan" and tls is None:
        tls = {"enabled": True, "server_name": proxy.get("sni") or server}
    if kind == "hysteria2":
        if tls is None:
            tls = {"enabled": True, "server_name": proxy.get("sni") or server}
        if not proxy.get("skip-cert-verify"):
            tls.setdefault("alpn", ["h3"])
    if tls is not None and proxy.get("skip-cert-verify"):
        tls["insecure"] = True
    if tls:
        out["tls"] = tls
    if tr:
        out["transport"] = tr
    return out


def build(config: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """返回 (sing-box 配置, 被跳过的节点说明)。"""
    out_cfg = cfg.get("output") or {}
    auto = out_cfg.get("group_auto") or "自动选择"
    manual = out_cfg.get("group_manual") or "手动选择"
    entry = out_cfg.get("group_entry") or "PROXY"
    ut = out_cfg.get("url_test") or {}
    interval = int(ut.get("interval") or 300)

    nodes: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for proxy in config.get("proxies") or []:
        name = str(proxy.get("name") or "")
        try:
            nodes.append(node(proxy, name))
        except ValueError as exc:
            skipped.append(f"{name} — {exc}")
    if not nodes:
        raise ValueError("没有可导出的节点")

    tags = [item["tag"] for item in nodes]
    urltest = {
        "type": "urltest",
        "tag": auto,
        "outbounds": tags,
        "url": ut.get("url") or "https://www.gstatic.com/generate_204",
        "interval": f"{interval}s",
        "tolerance": int(ut.get("tolerance") or 50),
    }
    sel_manual = {"type": "selector", "tag": manual, "outbounds": [auto] + tags + ["direct"], "default": auto}
    sel_entry = {"type": "selector", "tag": entry, "outbounds": [auto, manual, "direct"], "default": auto}

    outbounds: List[Dict[str, Any]] = [sel_entry, sel_manual, urltest] + nodes
    outbounds.append({"type": "direct", "tag": "direct"})

    # 官方部分构建没带 gVisor：mixed/gvisor 栈会 FATAL（gVisor is not included in this build）。
    # 因此默认 system 栈，需要时用 output.tun_stack 覆盖。
    tun_stack = str(out_cfg.get("tun_stack") or "system").strip().lower()
    if tun_stack not in ("system", "gvisor", "mixed"):
        tun_stack = "system"

    result: Dict[str, Any] = {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
                "auto_route": True,
                "strict_route": True,
                "stack": tun_stack,
            },
            {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 2080},
        ],
        "outbounds": outbounds,
        "route": {
            "rules": [{"ip_is_private": True, "outbound": "direct"}],
            "final": entry,
            "auto_detect_interface": True,
        },
    }
    if out_cfg.get("include_dns"):
        # sing-box 1.12+ 的新 DNS 写法（旧 address 形式 1.14 起已移除）
        result["dns"] = {
            "servers": [
                {"type": "udp", "tag": "dns-direct", "server": "223.5.5.5"},
                {"type": "https", "tag": "dns-remote", "server": "1.1.1.1",
                 "path": "/dns-query", "detour": entry},
            ],
            "final": "dns-remote",
        }
        # 1.12 起必须显式给域名解析器，否则 1.14 会 FATAL
        result["route"]["default_domain_resolver"] = "dns-direct"
    return result, skipped


def dump(config: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def validate(config: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    tags = set()
    for ob in config.get("outbounds") or []:
        tag = ob.get("tag")
        if not tag:
            problems.append("有 outbound 缺少 tag")
            continue
        if tag in tags:
            problems.append(f"outbound tag 重复: {tag}")
        tags.add(tag)
    for ob in config.get("outbounds") or []:
        for ref in ob.get("outbounds") or []:
            if ref not in tags:
                problems.append(f"{ob.get('tag')} 引用了不存在的 {ref}")
        default = ob.get("default")
        if default and default not in tags:
            problems.append(f"{ob.get('tag')} 的 default 指向不存在的 {default}")
    final = (config.get("route") or {}).get("final")
    if final and final not in tags:
        problems.append(f"route.final 指向不存在的 {final}")
    return problems
