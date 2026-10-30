#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sub-merger — 多机场 Clash 订阅合并器

合并多个 Clash 订阅：节点池合并 + 主订阅规则 + 精简策略组。
输出一份 merged.yaml，FlClash / mihomo 直接可用。

    python merger.py -c merger.yaml                          # 生成 merged.yaml
    python merger.py -c merger.yaml --pick                   # 交互式挑选节点
    python merger.py -c merger.yaml --serve 127.0.0.1:8899   # 本地订阅服务
    python merger.py -c merger.yaml --offline                # 用缓存，不联网
    python merger.py -c merger.yaml --check                  # 只校验已有输出
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

import singbox
import v2ray

# ---------------------------------------------------------------- 常量

BUILTIN_TARGETS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}
RESOLVE_TAILS = {"no-resolve", "resolve"}

TARGET_ALIASES = {
    "直连": "DIRECT",
    "DIRECT": "DIRECT",
    "拒绝": "REJECT",
    "拦截": "REJECT",
    "REJECT": "REJECT",
    "REJECT-DROP": "REJECT-DROP",
    "PASS": "PASS",
    "GLOBAL": "GLOBAL",
    "全局": "GLOBAL",
}

US_REGEX = (
    r"(美国|🇺🇸|United\s?States|UnitedStates|\bUSA?\b|US[-_]?\d|美国\d"
    r"|洛杉矶|Los\s?Angeles|圣何塞|San\s?Jose|西雅图|Seattle|纽约|New\s?York"
    r"|达拉斯|Dallas|硅谷|Silicon\s?Valley|凤凰城|Phoenix|阿什本|Ashburn"
    r"|迈阿密|Miami|芝加哥|Chicago|亚特兰大|Atlanta|波特兰|Portland"
    r"|丹佛|Denver|休斯顿|Houston|波士顿|Boston|华盛顿|Washington"
    r"|拉斯维加斯|Las\s?Vegas|夏威夷|Hawaii|盐湖城|Salt\s?Lake)"
)

DEFAULTS: Dict[str, Any] = {
    "network": {
        "proxy": "",
        "timeout": 30,
        "user_agent": "clash.meta/1.19.0",
        "insecure": False,
    },
    "output": {
        "path": "",
        "singbox": True,
        "clash": True,
        "v2ray": False,
        "v2ray_path": "",
        "singbox_path": "",
        "group_auto": "自动选择",
        "group_manual": "手动选择",
        "group_entry": "PROXY",
        "url_test": {
            "url": "https://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        },
        "rule_mode": "primary",
        "dedupe": "server",
        "include_dns": False,
        "tun_stack": "system",
        "keep_rule_providers": True,
        "target_map": [
            {"match": r"(?i)^(自动|自动选择|auto|url-test|urltest|最快|延迟|负载|fallback|故障转移)", "to": "自动选择"},
            {"match": r"(?i)^(手动|手动选择|节点选择|select|选择|proxy|主要|入口)", "to": "手动选择"},
            {"match": r"(?i)(美国|United\s?States|\bUSA?\b|US\s?节点)", "to": "自动选择"},
        ],
    },
    "defaults": {"filter": "", "exclude": "", "drop_info_nodes": False},
    "keep_file": "keep.txt",
    "drop_file": "drop.txt",
    "cache_dir": ".cache",
    "refresh_seconds": 1800,
}


class MergerError(Exception):
    pass


# ---------------------------------------------------------------- 工具

def sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "x"


def mask_url(url: str) -> str:
    """日志里把订阅 token 打码，避免贴屏/贴群泄露。"""
    return re.sub(r"((?:token|key|sub|auth|password)=)[^&]+", r"\1***", url)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise MergerError(f"配置文件不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise MergerError("配置文件顶层必须是 mapping")
    cfg = deep_merge(DEFAULTS, data)
    cfg["subscriptions"] = data.get("subscriptions") or []
    if not cfg["subscriptions"]:
        raise MergerError("配置里没有 subscriptions")
    return cfg


def split_top_level(text: str) -> List[str]:
    """按顶层逗号切分，保留 ((...)) 逻辑规则里的逗号。"""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts]


def normalize_target(target: str) -> Optional[str]:
    t = target.strip()
    if not t:
        return None
    up = t.upper()
    if up in BUILTIN_TARGETS:
        return up
    return TARGET_ALIASES.get(t) or TARGET_ALIASES.get(t.upper())


def strip_leading_noise(text: str) -> str:
    """去掉组名前缀的 emoji / 符号，🚀节点选择 -> 节点选择。"""
    stripped = re.sub(r"^[^\w一-鿿]+", "", text)
    return stripped or text


def map_target(target: str, cfg: Dict[str, Any]) -> str:
    out = cfg["output"]
    if target in (out["group_auto"], out["group_manual"], out["group_entry"]):
        return target
    builtin = normalize_target(target)
    if builtin:
        return builtin
    core = strip_leading_noise(target)
    candidates = [target] if core == target else [target, core]
    for item in out.get("target_map") or []:
        if isinstance(item, str):
            continue
        pattern = item.get("match")
        to = item.get("to")
        if not pattern or not to:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise MergerError(f"target_map 正则不合法: {pattern} ({exc})")
        if any(compiled.search(c) for c in candidates):
            return to
    return out["group_entry"]


def looks_like_v2ray_b64(text: str) -> bool:
    stripped = "".join(text.split())
    if len(stripped) < 64 or not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", stripped):
        return False
    try:
        raw = base64.b64decode(stripped + "=" * (-len(stripped) % 4), validate=False)
    except Exception:
        return False
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return "://" in decoded


def load_globs(path: Path) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def glob_hit(name: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) or name == p for p in patterns)


def name_hit(sub: str, orig: str, patterns: List[str]) -> bool:
    """支持两种写法：裸节点名，或 订阅名::节点名（精确到某一家）。"""
    return glob_hit(orig, patterns) or glob_hit(f"{sub}::{orig}", patterns)


# ---------------------------------------------------------------- 订阅获取

def ascii_url(url: str) -> str:
    """把中文域名转成 punycode，否则 urllib 会报 latin-1 编码错。"""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        host.encode("ascii")
        return url
    except UnicodeEncodeError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except Exception:
        return url
    netloc = ascii_host
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        auth = parts.username
        if parts.password:
            auth = f"{auth}:{parts.password}"
        netloc = f"{auth}@{netloc}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def http_fetch(url: str, cfg: Dict[str, Any]) -> str:
    net = cfg["network"]
    handlers: List[Any] = []
    proxy = net.get("proxy") or os.environ.get("SUBMERGER_PROXY", "")
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if net.get("insecure"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        ascii_url(url),
        headers={
            "User-Agent": net.get("user_agent", "clash.meta/1.19.0"),
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with opener.open(req, timeout=net.get("timeout", 30)) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        hint = ""
        text = f"{type(exc).__name__}: {exc}"
        if "idna" in text.lower() or "latin-1" in text.lower():
            hint = "  （链接域名含中文，程序已尝试转码；仍失败就去机场复制纯英文/punycode 的订阅链接）"
        elif "certificate" in text.lower() or "SSL" in text:
            hint = "  （证书校验失败：确认机场域名没写错，或在配置里把 network.insecure 设为 true）"
        elif "timed out" in text.lower() or "Timeout" in text or "URLError" in text:
            hint = "  （连不上：机场域名可能被墙，在「拉订阅代理」填 http://127.0.0.1:7890 再试）"
        raise MergerError(f"下载失败 {mask_url(url)}: {text}{hint}")


def read_sub_text(name: str, spec: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    if spec.get("path"):
        path = Path(spec["path"])
        if not path.is_absolute():
            path = ctx["base_dir"] / path
        if not path.exists():
            raise MergerError(f"[{name}] 本地订阅不存在: {path}")
        return path.read_text(encoding="utf-8-sig")

    url = spec.get("url")
    if not url:
        raise MergerError(f"[{name}] 缺少 url 或 path")

    cache = ctx["cache_dir"] / f"{sanitize(name)}-{hashlib.sha256(url.encode()).hexdigest()[:10]}.yaml"
    if ctx["offline"]:
        if not cache.exists():
            raise MergerError(f"[{name}] 离线模式但没有缓存: {cache}")
        return cache.read_text(encoding="utf-8-sig")

    text = http_fetch(url, ctx["cfg"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


def parse_clash(name: str, text: str) -> Dict[str, Any]:
    if looks_like_v2ray_b64(text):
        raise MergerError(
            f"[{name}] 这是 v2ray 分享链接（base64）格式，不是 Clash 配置。\n"
            f"   请在机场后台复制【Clash】格式订阅链接（常见做法：链接后加 &flag=clash），\n"
            f"   或用 sb 转换器转一次再填进 merger.yaml。"
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MergerError(f"[{name}] YAML 解析失败: {exc}")
    if not isinstance(data, dict):
        raise MergerError(f"[{name}] 订阅内容不是 Clash 配置（顶层不是 mapping）")
    if not data.get("proxies") and not data.get("proxy-providers"):
        raise MergerError(
            f"[{name}] 订阅里既没有 proxies 也没有 proxy-providers，"
            f"可能不是 Clash 格式（找机场要 Clash 订阅链接）"
        )
    return data


def collect_provider_nodes(
    sub_name: str, prov_name: str, prov: Dict[str, Any], ctx: Dict[str, Any]
) -> List[Dict[str, Any]]:
    if not isinstance(prov, dict) or not prov.get("url"):
        return []
    text = http_fetch(prov["url"], ctx["cfg"])
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise MergerError(f"[{sub_name}/provider:{prov_name}] YAML 解析失败: {exc}")
    nodes = data.get("proxies") or []
    if not nodes:
        return []
    filt = prov.get("filter")
    if filt:
        try:
            nodes = [n for n in nodes if re.search(filt, str(n.get("name", "")), re.I)]
        except re.error as exc:
            raise MergerError(f"[{sub_name}/provider:{prov_name}] filter 正则不合法: {exc}")
    override = prov.get("override") or {}
    prefix = str(override.get("additional-prefix") or "")
    suffix = str(override.get("additional-suffix") or "")
    out: List[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict) or not node.get("name"):
            continue
        item = dict(node)
        item["name"] = f"{prefix}{item['name']}{suffix}"
        out.append(item)
    return out


# ---------------------------------------------------------------- 合并

def node_key(node: Dict[str, Any]) -> str:
    server = str(node.get("server", "")).lower()
    port = str(node.get("port", ""))
    secret = str(
        node.get("uuid")
        or node.get("password")
        or node.get("psk")
        or node.get("auth")
        or node.get("token")
        or ""
    )
    return f"{node.get('type')}|{server}|{port}|{secret}"


def collect(cfg: Dict[str, Any], base_dir: Path, offline: bool) -> Dict[str, Any]:
    ctx = {
        "cfg": cfg,
        "base_dir": base_dir,
        "offline": offline,
        "cache_dir": (base_dir / cfg.get("cache_dir", ".cache")).resolve(),
    }
    keep = load_globs(_resolve_path(cfg.get("keep_file", "keep.txt"), base_dir))
    drop = load_globs(_resolve_path(cfg.get("drop_file", "drop.txt"), base_dir))

    subs: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {
        "subs": [],
        "deduped": 0,
        "retargeted": {},
        "match_dropped": 0,
        "invalid_rules": 0,
        "info_dropped": 0,
    }

    for spec in cfg["subscriptions"]:
        if spec.get("enabled", True) is False:
            continue
        name = str(spec.get("name") or f"sub{len(subs) + 1}")
        text = read_sub_text(name, spec, ctx)
        data = parse_clash(name, text)

        raw: List[Dict[str, Any]] = []
        for node in data.get("proxies") or []:
            if isinstance(node, dict) and node.get("name"):
                raw.append(dict(node))
        for prov_name, prov in (data.get("proxy-providers") or {}).items():
            raw.extend(collect_provider_nodes(name, prov_name, prov, ctx))

        pattern = spec.get("filter") or cfg.get("defaults", {}).get("filter") or ""
        exclude = spec.get("exclude") or cfg.get("defaults", {}).get("exclude") or ""
        drop_info = bool(cfg.get("defaults", {}).get("drop_info_nodes"))
        info_pattern = r"(剩余流量|套餐到期|到期时间|流量重置|过期时间)"
        prefix = spec.get("prefix")
        if prefix is None:
            prefix = f"[{name}] "

        kept: List[Dict[str, Any]] = []
        for node in raw:
            orig = str(node["name"])
            if pattern:
                try:
                    if not re.search(pattern, orig, re.I):
                        continue
                except re.error as exc:
                    raise MergerError(f"[{name}] filter 正则不合法: {exc}")
            if exclude:
                try:
                    if re.search(exclude, orig, re.I):
                        continue
                except re.error as exc:
                    raise MergerError(f"[{name}] exclude 正则不合法: {exc}")
            if drop_info and re.search(info_pattern, orig):
                stats["info_dropped"] += 1
                continue
            if keep and not name_hit(name, orig, keep):
                continue
            if drop and name_hit(name, orig, drop):
                continue
            item = dict(node)
            item["name"] = f"{prefix}{orig}"
            item["_sub"] = name
            item["_orig"] = orig
            kept.append(item)

        stats["subs"].append(
            {
                "name": name,
                "fetched": len(raw),
                "kept": len(kept),
                "rules": len(data.get("rules") or []),
                "providers": len(data.get("rule-providers") or {}),
            }
        )
        subs.append({"name": name, "data": data, "primary": bool(spec.get("primary"))})
        nodes.extend(kept)

    if not nodes:
        raise MergerError(
            "过滤后一个节点都不剩。检查 filter 正则，或用 --list 看节点名长什么样。"
        )

    # 跨订阅去重
    deduped: List[Dict[str, Any]] = []
    seen: set = set()
    for node in nodes:
        if cfg["output"].get("dedupe", "server") == "none":
            deduped.append(node)
            continue
        key = node_key(node)
        if key in seen:
            stats["deduped"] += 1
            continue
        seen.add(key)
        deduped.append(node)

    # 同名节点加序号
    used: Dict[str, int] = {}
    for node in deduped:
        name = node["name"]
        if name in used:
            used[name] += 1
            node["name"] = f"{name} #{used[name]}"
        else:
            used[name] = 1

    return {"subs": subs, "nodes": deduped, "stats": stats}


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


EXPORT_DIR_NAME = "订阅导出"


def default_export_dir() -> Path:
    """默认导出到「下载/订阅导出」，找不到下载目录就放到用户主目录下。"""
    home = Path.home()
    for name in ("Downloads", "下载"):
        candidate = home / name
        if candidate.is_dir():
            return candidate / EXPORT_DIR_NAME
    return home / EXPORT_DIR_NAME


def resolve_output(cfg: Dict[str, Any], base_dir: Path) -> Path:
    """output.path 为空时用「下载/订阅导出/merged.yaml」。"""
    value = str((cfg.get("output") or {}).get("path") or "").strip()
    if not value:
        return default_export_dir() / "merged.yaml"
    return _resolve_path(value, base_dir)


def pick_nodes(nodes: List[Dict[str, Any]], cfg: Dict[str, Any], base_dir: Path) -> List[Dict[str, Any]]:
    width = max((len(n["name"]) for n in nodes), default=10)
    print(f"\n共 {len(nodes)} 个节点（过滤后）：")
    for idx, node in enumerate(nodes, 1):
        print(
            f" [{idx:>3}] {node['name']:<{width}}  {str(node.get('type', '')):<9} "
            f"{node.get('server', '')}:{node.get('port', '')}"
        )
    raw = input("\n输入要保留的编号（如 1,3,5-9；回车=全部保留；q=退出）: ").strip()
    if raw.lower() in ("q", "quit", "exit"):
        raise SystemExit(0)
    if not raw:
        return nodes

    chosen: List[Dict[str, Any]] = []
    for chunk in raw.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                raise MergerError(f"编号不合法: {chunk}")
            chosen.extend(i for i in range(lo_i, hi_i + 1) if 1 <= i <= len(nodes))
        else:
            try:
                i = int(chunk)
            except ValueError:
                raise MergerError(f"编号不合法: {chunk}")
            if 1 <= i <= len(nodes):
                chosen.append(i)

    picked_ids = sorted(set(chosen))
    if not picked_ids:
        raise MergerError("没有选中任何节点")
    picked = [nodes[i - 1] for i in picked_ids]

    keep_path = _resolve_path(cfg.get("keep_file", "keep.txt"), base_dir)
    lines = ["# 由 --pick 生成，按节点原名匹配；改名前请重新生成"]
    lines.extend(f'{n["_sub"]}::{n["_orig"]}' for n in picked)
    keep_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已保留 {len(picked)} 个节点，名单写入 {keep_path}")
    return picked


def build_groups(nodes: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = cfg["output"]
    auto = out["group_auto"]
    manual = out["group_manual"]
    entry = out["group_entry"]
    ut = out["url_test"]
    names = [n["name"] for n in nodes]
    return [
        {"name": entry, "type": "select", "proxies": [auto, manual, "DIRECT"]},
        {
            "name": auto,
            "type": "url-test",
            "url": ut["url"],
            "interval": int(ut["interval"]),
            "tolerance": int(ut["tolerance"]),
            "lazy": True,
            "proxies": names,
        },
        {"name": manual, "type": "select", "proxies": [auto] + names + ["DIRECT"]},
    ]


def build_rules(
    subs: List[Dict[str, Any]], cfg: Dict[str, Any], stats: Dict[str, Any]
) -> Tuple[List[str], Dict[str, Any]]:
    out = cfg["output"]
    mode = str(out.get("rule_mode", "primary")).lower()
    primaries = [s for s in subs if s.get("primary")]
    primary = primaries[0] if primaries else subs[0]
    ordered = [primary] + [s for s in subs if s is not primary]
    if mode != "union":
        ordered = [primary]

    rules: List[str] = []
    providers: Dict[str, Any] = {}
    seen: set = set()

    for sub in ordered:
        is_primary = sub is primary
        sub_key = sanitize(sub["name"])
        data = sub["data"]
        sub_provs = data.get("rule-providers") or {}

        for raw in data.get("rules") or []:
            text = str(raw).strip()
            if not text or text.startswith("#"):
                continue
            parts = split_top_level(text)
            if not parts:
                continue
            head = parts[0].upper()
            if head == "MATCH":
                stats["match_dropped"] += 1
                continue

            tail = ""
            if len(parts) >= 2 and parts[-1].lower() in RESOLVE_TAILS:
                tail = parts[-1].lower()
                tgt_idx = len(parts) - 2
            else:
                tgt_idx = len(parts) - 1
            if tgt_idx < 1:
                stats["invalid_rules"] += 1
                continue

            payload = parts[1:tgt_idx]
            old_target = parts[tgt_idx]
            new_target = map_target(old_target, cfg)
            if new_target != old_target:
                stats["retargeted"][old_target] = new_target

            if head == "RULE-SET" and payload:
                key = payload[0]
                if key not in sub_provs:
                    raise MergerError(
                        f"[{sub['name']}] 规则引用了不存在的 rule-provider: {key}"
                    )
                if not is_primary:
                    new_key = f"{sub_key}-{key}"
                    prov = dict(sub_provs[key])
                    path = str(prov.get("path") or "")
                    if path and not path.startswith("/"):
                        prov["path"] = f"./ruleset/{new_key}.yaml"
                    providers[new_key] = prov
                    payload = [new_key] + payload[1:]
                else:
                    if out.get("keep_rule_providers", True):
                        providers[key] = dict(sub_provs[key])

            if not payload:
                stats["invalid_rules"] += 1
                continue

            rebuilt = ",".join([parts[0]] + payload + [new_target] + ([tail] if tail else []))
            if rebuilt in seen:
                continue
            seen.add(rebuilt)
            rules.append(rebuilt)

    if not rules:
        raise MergerError("没有可用规则，检查主订阅是否带 rules")

    rules.append(f"MATCH,{out['group_entry']}")
    return rules, providers


def assemble(
    collected: Dict[str, Any], cfg: Dict[str, Any], nodes: List[Dict[str, Any]]
) -> Dict[str, Any]:
    stats = collected["stats"]
    groups = build_groups(nodes, cfg)
    rules, providers = build_rules(collected["subs"], cfg, stats)

    out: Dict[str, Any] = {}
    if cfg["output"].get("include_dns"):
        primary = next((s for s in collected["subs"] if s.get("primary")), collected["subs"][0])
        dns = primary["data"].get("dns")
        if dns:
            out["dns"] = dns

    out["proxies"] = [{k: v for k, v in n.items() if not k.startswith("_")} for n in nodes]
    out["proxy-groups"] = groups
    if providers:
        out["rule-providers"] = providers
    out["rules"] = rules
    return out


def validate(config: Dict[str, Any], nodes: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    node_names = [n["name"] for n in nodes]
    if len(set(node_names)) != len(node_names):
        errors.append("节点名有重复")

    groups = config.get("proxy-groups") or []
    group_names = [g["name"] for g in groups]
    if len(set(group_names)) != len(group_names):
        errors.append("策略组名有重复")
    known = set(node_names) | set(group_names) | BUILTIN_TARGETS

    for group in groups:
        members = group.get("proxies") or []
        if not members:
            errors.append(f"策略组 {group['name']} 是空的")
        for member in members:
            if member not in known:
                errors.append(f"策略组 {group['name']} 引用了不存在的 {member}")

    providers = set((config.get("rule-providers") or {}).keys())
    match_count = 0
    rules = config.get("rules") or []
    for idx, raw in enumerate(rules):
        text = str(raw)
        parts = split_top_level(text)
        if not parts:
            errors.append(f"第 {idx + 1} 条规则为空")
            continue
        if parts[0].upper() == "MATCH":
            match_count += 1
            if idx != len(rules) - 1:
                errors.append("MATCH 不在最后一行")
            if len(parts) != 2:
                errors.append(f"MATCH 格式异常: {text}")
            elif parts[1] not in known:
                errors.append(f"MATCH 指向不存在的 {parts[1]}")
            continue
        tail = ""
        if len(parts) >= 2 and parts[-1].lower() in RESOLVE_TAILS:
            tail = parts[-1].lower()
            tgt_idx = len(parts) - 2
        else:
            tgt_idx = len(parts) - 1
        if tgt_idx < 1:
            errors.append(f"第 {idx + 1} 条规则格式异常: {text}")
            continue
        target = parts[tgt_idx]
        if target not in known:
            errors.append(f"第 {idx + 1} 条规则指向不存在的 {target}: {text}")
        if parts[0].upper() == "RULE-SET" and parts[1] not in providers:
            errors.append(f"第 {idx + 1} 条规则引用了不存在的 rule-provider {parts[1]}")

    if match_count != 1:
        errors.append(f"MATCH 规则数量为 {match_count}，应恰好 1 条")
    return errors


def dump_yaml(config: Dict[str, Any], nodes: List[Dict[str, Any]], path: Path) -> None:
    header = (
        f"# sub-merger 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# 节点 {len(nodes)} · 策略组 {len(config.get('proxy-groups') or [])} · "
        f"规则 {len(config.get('rules') or [])}\n"
        f"# 端口 / TUN / external-controller 由 FlClash 自己管理，这里不写\n"
    )
    body = yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        width=10**6,
        default_flow_style=False,
        default_style=None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    os.replace(tmp, path)


def write_report(collected: Dict[str, Any], config: Dict[str, Any], path: Path) -> None:
    stats = collected["stats"]
    lines = [f"# sub-merger 报告", "", f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    lines.append("| 订阅 | 抓取节点 | 保留 | 规则 | rule-providers |")
    lines.append("|---|---|---|---|---|")
    for s in stats["subs"]:
        lines.append(f"| {s['name']} | {s['fetched']} | {s['kept']} | {s['rules']} | {s['providers']} |")
    lines.append("")
    lines.append(f"- 跨订阅去重：{stats['deduped']} 个")
    lines.append(f"- 丢弃 MATCH：{stats['match_dropped']} 条")
    if stats["retargeted"]:
        lines.append("- 规则目标重映射：")
        for old, new in stats["retargeted"].items():
            lines.append(f"  - `{old}` → `{new}`")
    lines.append("")
    groups = config.get("proxy-groups") or []
    rules = config.get("rules") or []
    lines.append(f"- 最终策略组：{', '.join(g['name'] for g in groups)}")
    lines.append(f"- 最终规则数：{len(rules)}")
    lines.append("")
    lines.append("## 保留的节点")
    lines.append("")
    for node in collected["nodes"]:
        lines.append(f"- {node['name']}  `{node.get('type')}` {node.get('server')}:{node.get('port')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(config: Dict[str, Any], collected: Dict[str, Any], out_path: Path) -> None:
    stats = collected["stats"]
    print("\n合并结果")
    for s in stats["subs"]:
        print(f"  {s['name']:<10} 抓取 {s['fetched']:>4}  保留 {s['kept']:>4}  规则 {s['rules']:>3}")
    print(f"  去重 {stats['deduped']} 个，最终节点 {len(collected['nodes'])} 个")
    groups = config.get("proxy-groups") or []
    rules = config.get("rules") or []
    print(f"  策略组 {', '.join(g['name'] for g in groups)}")
    print(f"  规则 {len(rules)} 条，末尾 {rules[-1] if rules else '-'}")
    if stats["retargeted"]:
        mapping = "；".join(f"{k}→{v}" for k, v in stats["retargeted"].items())
        print(f"  目标重映射：{mapping}")
    print(f"  输出 {out_path}")


# ---------------------------------------------------------------- 本地订阅服务

class SubHandler(BaseHTTPRequestHandler):
    server_version = "sub-merger/1.0"

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?")[0].rstrip("/")
        if route not in ("/sub", "/sub.yaml", "/sub.json", "/sub.txt"):
            self.send_error(404, "try /sub, /sub.json or /sub.txt")
            return
        try:
            if route == "/sub.json":
                body = self.server.get_json_text()  # type: ignore[attr-defined]
                ctype = "application/json; charset=utf-8"
            elif route == "/sub.txt":
                body = self.server.get_v2ray_text()  # type: ignore[attr-defined]
                ctype = "text/plain; charset=utf-8"
            else:
                body = self.server.get_config_text()  # type: ignore[attr-defined]
                ctype = "text/yaml; charset=utf-8"
        except Exception:
            self.send_error(500, "merge failed", explain=traceback.format_exc())
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[serve] {self.address_string()} {fmt % args}")


class SubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: Tuple[str, int], cfg: Dict[str, Any], base_dir: Path):
        super().__init__(addr, SubHandler)
        self.cfg = cfg
        self.base_dir = base_dir
        self.ttl = int(cfg.get("refresh_seconds", 1800))
        self._lock = threading.Lock()
        self._stamp = 0.0
        self._text = ""

    def get_config_text(self) -> str:
        with self._lock:
            if time.time() - self._stamp < self.ttl and self._text:
                return self._text
            collected = collect(self.cfg, self.base_dir, offline=False)
            config = assemble(collected, self.cfg, collected["nodes"])
            errors = validate(config, collected["nodes"])
            if errors:
                raise MergerError("校验失败：\n  " + "\n  ".join(errors))
            out_path = resolve_output(self.cfg, self.base_dir)
            dump_yaml(config, collected["nodes"], out_path)
            write_report(collected, config, out_path.with_name(f"{out_path.stem}-report.md"))
            self._text = out_path.read_text(encoding="utf-8")
            self._stamp = time.time()
            print(f"[serve] 已刷新 {out_path}（{len(collected['nodes'])} 个节点）")
            return self._text


def _build_json_text(self) -> str:
    self.get_config_text()
    data = yaml.safe_load(self._text) or {}
    cfg, _skipped = singbox.build(data, self.cfg)
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def _build_v2ray_text(self) -> str:
    self.get_config_text()
    data = yaml.safe_load(self._text) or {}
    links, _skipped = v2ray.links(data.get("proxies") or [])
    return v2ray.subscription(links)


SubServer.get_json_text = _build_json_text
SubServer.get_v2ray_text = _build_v2ray_text


# ---------------------------------------------------------------- CLI

def cmd_check(path: Path) -> int:
    if not path.exists():
        print(f"文件不存在: {path}")
        return 2
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    nodes = data.get("proxies") or []
    errors = validate(data, nodes)
    if errors:
        print("校验失败：")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"校验通过：{len(nodes)} 节点 / {len(data.get('proxy-groups') or [])} 组 / "
          f"{len(data.get('rules') or [])} 规则")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="多机场 Clash 订阅合并器")
    parser.add_argument("-c", "--config", default="merger.yaml", help="配置文件路径")
    parser.add_argument("-o", "--output", help="覆盖输出路径")
    parser.add_argument("--pick", action="store_true", help="交互式挑选节点，写入 keep.txt")
    parser.add_argument("--list", action="store_true", help="只列出过滤后的节点")
    parser.add_argument("--serve", metavar="ADDR", help="本地订阅服务，如 127.0.0.1:8899")
    parser.add_argument("--offline", action="store_true", help="用缓存，不联网")
    parser.add_argument("--check", action="store_true", help="只校验已生成的配置")
    parser.add_argument("--report", help="报告输出路径")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    base_dir = config_path.parent

    try:
        if args.check:
            if args.output:
                return cmd_check(Path(args.output))
            if config_path.exists():
                check_cfg = load_config(config_path)
                return cmd_check(resolve_output(check_cfg, base_dir))
            return cmd_check(Path("merged.yaml"))

        cfg = load_config(config_path)
        if args.output:
            cfg["output"]["path"] = str(Path(args.output).resolve())

        if args.serve:
            host, _, port = args.serve.partition(":")
            server = SubServer((host or "127.0.0.1", int(port or 8899)), cfg, base_dir)
            url = f"http://{host or '127.0.0.1'}:{port or 8899}/sub"
            print(f"本地订阅服务已启动: {url}")
            print("在 FlClash 里『配置 → 新建 → 从 URL 导入』填这个地址，")
            print("再把自动更新间隔设成 30 分钟。Ctrl+C 停止。")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止")
            finally:
                server.server_close()
            return 0

        collected = collect(cfg, base_dir, offline=args.offline)

        if args.list:
            for node in collected["nodes"]:
                print(f"{node['name']}  {node.get('type')}  {node.get('server')}:{node.get('port')}")
            return 0

        if args.pick:
            collected["nodes"] = pick_nodes(collected["nodes"], cfg, base_dir)

        config = assemble(collected, cfg, collected["nodes"])
        errors = validate(config, collected["nodes"])
        if errors:
            raise MergerError("校验失败，未写出配置：\n  " + "\n  ".join(errors))

        out_path = resolve_output(cfg, base_dir)
        if cfg["output"].get("clash", True):
            dump_yaml(config, collected["nodes"], out_path)
        else:
            print("  跳过 .yaml（导出方式里只要 sing-box）")
        sb_path = None
        sb_skipped = []
        if cfg["output"].get("singbox", True):
            sb_cfg, sb_skipped = singbox.build(config, cfg)
            problems = singbox.validate(sb_cfg)
            if problems:
                raise MergerError("sing-box 校验失败：" + chr(10) + "  " + (chr(10) + "  ").join(problems))
            sb_path = _resolve_path(
                cfg["output"].get("singbox_path") or f"{out_path.stem}-singbox.json",
                out_path.parent,
            )
            singbox.dump(sb_cfg, sb_path)
        v2_path = None
        v2_skipped = []
        if cfg["output"].get("v2ray", False):
            v2_links, v2_skipped = v2ray.links(collected["nodes"])
            problems = v2ray.validate(v2_links)
            if problems:
                raise MergerError("V2Ray 链接校验失败：" + chr(10) + "  " + (chr(10) + "  ").join(problems))
            v2_path = _resolve_path(
                cfg["output"].get("v2ray_path") or f"{out_path.stem}-v2ray.txt",
                out_path.parent,
            )
            v2ray.dump(v2_links, v2_path)
        report_path = Path(args.report) if args.report else out_path.with_name(f"{out_path.stem}-report.md")
        write_report(collected, config, report_path)
        print_summary(config, collected, out_path)
        print(f"  报告 {report_path}")
        if sb_path:
            print(f"  sing-box {sb_path}")
        if v2_path:
            print(f"  v2ray 订阅 {v2_path}（{len(v2_links)} 条链接）")
        if sb_skipped:
            print(f"  sing-box 跳过 {len(sb_skipped)} 个不支持的节点")
            for item in sb_skipped[:5]:
                print(f"    - {item}")
        return 0
    except MergerError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
