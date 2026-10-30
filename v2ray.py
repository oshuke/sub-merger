"""把合并后的节点转成 v2ray 分享链接（base64 订阅字符串）。

输入是 mihomo 风格的 proxy dict 列表（跟 merged.yaml 里 proxies 一样），
输出是 v2rayN / nekobox / 小火箭都认的链接列表，再整体 base64 就是订阅内容。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import quote

SUPPORTED = {"vmess", "vless", "trojan", "ss", "hysteria2", "tuic", "socks5", "http"}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _name(node: Dict[str, Any]) -> str:
    return quote(str(node.get("name") or "node"), safe="")


def _net(node: Dict[str, Any]) -> str:
    return str(node.get("network") or "tcp")


def _transport_query(node: Dict[str, Any]) -> List[Tuple[str, str]]:
    """生成 type/host/path/serviceName 这类传输层参数。"""
    net = _net(node)
    out: List[Tuple[str, str]] = [("type", net)]
    if net == "ws":
        ws = node.get("ws-opts") or {}
        path = ws.get("path")
        host = (ws.get("headers") or {}).get("Host")
        if path:
            out.append(("path", str(path)))
        if host:
            out.append(("host", str(host)))
    elif net == "grpc":
        grpc = node.get("grpc-opts") or {}
        service = grpc.get("grpc-service-name")
        if service:
            out.append(("serviceName", str(service)))
    elif net in ("h2", "http"):
        h2 = node.get("h2-opts") or node.get("http-opts") or {}
        hosts = h2.get("host")
        if isinstance(hosts, list) and hosts:
            out.append(("host", str(hosts[0])))
        elif isinstance(hosts, str) and hosts:
            out.append(("host", hosts))
        path = h2.get("path")
        if isinstance(path, list) and path:
            out.append(("path", str(path[0])))
        elif isinstance(path, str) and path:
            out.append(("path", path))
    return out


def _tls_query(node: Dict[str, Any], security: str) -> List[Tuple[str, str]]:
    """TLS / REALITY 相关参数。"""
    out: List[Tuple[str, str]] = [("security", security)]
    sni = node.get("servername") or node.get("sni")
    if sni:
        out.append(("sni", str(sni)))
    if node.get("client-fingerprint"):
        out.append(("fp", str(node["client-fingerprint"])))
    if node.get("skip-cert-verify"):
        out.append(("allowInsecure", "1"))
    alpn = node.get("alpn")
    if isinstance(alpn, list) and alpn:
        out.append(("alpn", ",".join(str(x) for x in alpn)))
    reality = node.get("reality-opts") or {}
    if reality.get("public-key"):
        out.append(("pbk", str(reality["public-key"])))
    if reality.get("short-id"):
        out.append(("sid", str(reality["short-id"])))
    return out


def _query(pairs: Iterable[Tuple[str, str]]) -> str:
    parts = [f"{k}={quote(str(v), safe=',')}" for k, v in pairs if v not in (None, "")]
    return "&".join(parts)


def _vmess(node: Dict[str, Any]) -> str:
    net = _net(node)
    ws = node.get("ws-opts") or {}
    grpc = node.get("grpc-opts") or {}
    tls = "tls" if node.get("tls") else ""
    payload = {
        "v": "2",
        "ps": str(node.get("name") or "node"),
        "add": str(node.get("server")),
        "port": str(node.get("port")),
        "id": str(node.get("uuid")),
        "aid": str(node.get("alterId") or 0),
        "scy": str(node.get("cipher") or "auto"),
        "net": net,
        "type": "none",
        "host": (ws.get("headers") or {}).get("Host", "") if net == "ws" else "",
        "path": str(ws.get("path") or "") if net == "ws" else str(grpc.get("grpc-service-name") or ""),
        "tls": tls,
        "sni": str(node.get("servername") or node.get("sni") or ""),
        "alpn": ",".join(node.get("alpn") or []),
        "fp": str(node.get("client-fingerprint") or ""),
        "skip-cert-verify": bool(node.get("skip-cert-verify")),
    }
    return "vmess://" + _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _vless(node: Dict[str, Any]) -> str:
    security = "none"
    if node.get("reality-opts"):
        security = "reality"
    elif node.get("tls"):
        security = "tls"
    pairs = [("encryption", "none")] + _tls_query(node, security) + _transport_query(node)
    if node.get("flow"):
        pairs.append(("flow", str(node["flow"])))
    return ("vless://" + quote(str(node.get("uuid")), safe="") + "@" +
            f"{node.get('server')}:{node.get('port')}?" + _query(pairs) + "#" + _name(node))


def _trojan(node: Dict[str, Any]) -> str:
    pairs = _tls_query(node, "tls") + _transport_query(node)
    return ("trojan://" + quote(str(node.get("password")), safe="") + "@" +
            f"{node.get('server')}:{node.get('port')}?" + _query(pairs) + "#" + _name(node))


def _ss(node: Dict[str, Any]) -> str:
    method = str(node.get("cipher") or "aes-128-gcm")
    userinfo = _b64(f"{method}:{node.get('password')}").rstrip("=")
    base = f"ss://{userinfo}@{node.get('server')}:{node.get('port')}"
    query: List[str] = []
    plugin = node.get("plugin")
    if plugin:
        opts = node.get("plugin-opts") or {}
        bits = [str(plugin)]
        if plugin in ("obfs", "simple-obfs"):
            if opts.get("mode"):
                bits.append("obfs=" + str(opts["mode"]))
            if opts.get("host"):
                bits.append("obfs-host=" + str(opts["host"]))
        if opts.get("tls"):
            bits.append("tls")
        query.append("plugin=" + quote(";".join(bits), safe=""))
    if query:
        base += "?" + "&".join(query)
    return base + "#" + _name(node)


def _hysteria2(node: Dict[str, Any]) -> str:
    pairs = _tls_query(node, "tls")
    obfs = node.get("obfs")
    if obfs:
        pairs.append(("obfs", str(obfs)))
        if node.get("obfs-password"):
            pairs.append(("obfs-password", str(node["obfs-password"])))
    auth = str(node.get("password") or node.get("auth") or "")
    return ("hysteria2://" + quote(auth, safe="") + "@" +
            f"{node.get('server')}:{node.get('port')}?" + _query(pairs) + "#" + _name(node))


def _tuic(node: Dict[str, Any]) -> str:
    pairs = _tls_query(node, "tls")
    if node.get("congestion-controller"):
        pairs.append(("congestion_control", str(node["congestion-controller"])))
    if node.get("udp-relay-mode"):
        pairs.append(("udp_relay_mode", str(node["udp-relay-mode"])))
    user = quote(str(node.get("uuid")), safe="")
    pwd = quote(str(node.get("password")), safe="")
    return (f"tuic://{user}:{pwd}@{node.get('server')}:{node.get('port')}?" +
            _query(pairs) + "#" + _name(node))


def _plain(node: Dict[str, Any]) -> str:
    scheme = "socks5" if node.get("type") == "socks5" else "http"
    auth = ""
    if node.get("username"):
        auth = quote(str(node["username"]), safe="")
        if node.get("password"):
            auth += ":" + quote(str(node["password"]), safe="")
        auth += "@"
    return f"{scheme}://{auth}{node.get('server')}:{node.get('port')}#" + _name(node)


_BUILDERS = {
    "vmess": _vmess,
    "vless": _vless,
    "trojan": _trojan,
    "ss": _ss,
    "hysteria2": _hysteria2,
    "tuic": _tuic,
    "socks5": _plain,
    "http": _plain,
}


def links(nodes: Iterable[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """返回 (链接列表, 跳过的节点名列表)。"""
    out: List[str] = []
    skipped: List[str] = []
    for node in nodes:
        kind = str(node.get("type") or "")
        builder = _BUILDERS.get(kind)
        if builder is None:
            skipped.append(f"{node.get('name')}（{kind or '未知类型'}）")
            continue
        try:
            out.append(builder(node))
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{node.get('name')}（{exc}）")
    return out, skipped


def subscription(links_list: Iterable[str]) -> str:
    """v2rayN 那种 base64 订阅内容。"""
    text = "\n".join(links_list)
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def dump(links_list: List[str], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(subscription(links_list), encoding="ascii")
    os.replace(tmp, path)


def dump_plain(links_list: List[str], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(links_list) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def validate(links_list: List[str]) -> List[str]:
    problems: List[str] = []
    for link in links_list:
        if "://" not in link:
            problems.append(f"不是合法链接：{link[:40]}")
            continue
        scheme, _, rest = link.partition("://")
        if scheme not in {"vmess", "vless", "trojan", "ss", "hysteria2", "tuic", "socks5", "http"}:
            problems.append(f"不认识的协议：{scheme}")
        if scheme != "vmess" and "@" not in rest:
            problems.append(f"缺少账号信息：{link[:60]}")
        if "#" not in link:
            problems.append(f"缺少节点名：{link[:60]}")
    return problems
