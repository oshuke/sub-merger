#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sub-merger 图形界面

    python gui.py

跟 merger.py 共用 merger.yaml 与同一套合并逻辑；配置改完点“生成配置”即可。
"""

from __future__ import annotations

import io
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import merger  # noqa: E402
import singbox  # noqa: E402
import v2ray  # noqa: E402

CFG_PATH = APP_DIR / "merger.yaml"

UI_FONT = ("Microsoft YaHei UI", 10)
MONO_FONT = ("Consolas", 9)

REGION_PRESETS: Dict[str, str] = {
    "美国": "(?i)" + merger.US_REGEX,
    "日本": r"(?i)(日本|🇯🇵|Japan|JP[-_]?\d|东京|大阪|Tokyo|Osaka)",
    "香港": r"(?i)(香港|🇭🇰|Hong\s?Kong|HK[-_]?\d|港)",
    "新加坡": r"(?i)(新加坡|🇸🇬|Singapore|SG[-_]?\d|狮城)",
    "台湾": r"(?i)(台湾|🇹🇼|Taiwan|TW[-_]?\d|台北)",
    "自定义": "",
    "全部（不筛）": "",
}
CUSTOM_PRESET = "自定义"
ALL_PRESET = "全部（不筛）"
UNION_LABEL = "全部拼接（union）"
EXPORT_BOTH = "Clash + sing-box"
EXPORT_ALL = "Clash + sing-box + V2Ray（全要）"
EXPORT_CLASH = "只要 Clash（FlClash）"
EXPORT_SINGBOX = "只要 sing-box"
EXPORT_V2RAY = "只要 V2Ray"
EXPORT_CHOICES = [EXPORT_BOTH, EXPORT_ALL, EXPORT_CLASH, EXPORT_SINGBOX, EXPORT_V2RAY]


def default_cfg() -> Dict[str, Any]:
    cfg = merger.deep_merge(merger.DEFAULTS, {})
    cfg["network"]["proxy"] = "http://127.0.0.1:7890"
    cfg["subscriptions"] = []
    cfg["output"]["path"] = str(merger.resolve_output({}, APP_DIR))
    return cfg


def region_label(pattern: str) -> str:
    """把过滤正则粗分类成地区名，仅用于界面显示。"""
    if not pattern:
        return "全部"
    for label, needles in (
        ("美国", ("美国", "🇺🇸", "USA", "US[")),
        ("日本", ("日本", "🇯🇵", "Japan")),
        ("香港", ("香港", "🇭🇰", "Hong")),
        ("新加坡", ("新加坡", "🇸🇬", "Singapore")),
        ("台湾", ("台湾", "🇹🇼", "Taiwan")),
    ):
        if any(n in pattern for n in needles):
            return label
    return "自定义"


class StreamProxy(io.TextIOBase):
    """把 print / traceback 的输出接到界面的日志框。"""

    def __init__(self, sink):
        self._sink = sink

    def write(self, text: str) -> int:
        if text:
            self._sink(text.rstrip("\n"))
        return len(text)

    def flush(self) -> None:
        return None


class SubDialog(tk.Toplevel):
    """新增 / 编辑一个订阅。"""

    def __init__(self, master: "App", spec: Optional[Dict[str, Any]] = None):
        super().__init__(master)
        self.title("编辑订阅" if spec else "添加订阅")
        self.transient(master)
        self.resizable(False, False)
        self.result: Optional[Dict[str, Any]] = None

        spec = dict(spec or {})
        self.var_name = tk.StringVar(value=spec.get("name", f"订阅{len(master.subs) + 1}"))
        self.var_src = tk.StringVar(value=spec.get("url") or spec.get("path") or "")
        self.var_filter = tk.StringVar(value=spec.get("filter", REGION_PRESETS["美国"]))
        self.var_exclude = tk.StringVar(value=spec.get("exclude", ""))
        self.var_preset = tk.StringVar(value=self._guess_preset())

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="名称").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.var_name, width=58).grid(row=0, column=1, sticky="we", pady=4)

        ttk.Label(body, text="订阅链接").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.var_src, width=58).grid(row=1, column=1, sticky="we", pady=4)
        ttk.Label(body, text="填机场给的 Clash 订阅链接（http/https），节点会随订阅自动更新",
                  foreground="#777", font=(UI_FONT[0], 8)).grid(row=2, column=1, sticky="w")

        ttk.Label(body, text="地区预设").grid(row=3, column=0, sticky="w", pady=4)
        preset = ttk.Combobox(body, textvariable=self.var_preset, values=list(REGION_PRESETS),
                              state="readonly", width=20)
        preset.grid(row=3, column=1, sticky="w", pady=4)
        preset.bind("<<ComboboxSelected>>", self.on_preset)

        ttk.Label(body, text="过滤正则").grid(row=4, column=0, sticky="nw", pady=4)
        self.entry_filter = ttk.Entry(body, textvariable=self.var_filter, width=58)
        self.entry_filter.grid(row=4, column=1, sticky="we", pady=4)
        self.hint_filter = ttk.Label(body, text="留空=全部保留；选「自定义」可直接写正则", foreground="#777",
                                     font=(UI_FONT[0], 8))
        self.hint_filter.grid(row=5, column=1, sticky="w")

        ttk.Label(body, text="排除正则").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.var_exclude, width=58).grid(row=6, column=1, sticky="we", pady=4)

        ttk.Label(body, text="分流规则用哪家，回主界面「分流规则」下拉里选",
                  foreground="#777", font=(UI_FONT[0], 8)).grid(row=7, column=1, sticky="w", pady=(8, 0))

        btns = ttk.Frame(body)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="确定", command=self.ok).pack(side="right")

        self.update_idletasks()
        width = max(self.winfo_reqwidth(), 640)
        height = self.winfo_reqheight()
        pos_x = master.winfo_rootx() + (master.winfo_width() - width) // 2
        pos_y = master.winfo_rooty() + 70
        self.geometry(f"{width}x{height}+{max(0, pos_x)}+{max(0, pos_y)}")
        self.grab_set()
        self.bind("<Return>", lambda _e: self.ok())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _guess_preset(self) -> str:
        pattern = self.var_filter.get().strip()
        if not pattern:
            return ALL_PRESET
        for label, value in REGION_PRESETS.items():
            if label in (CUSTOM_PRESET, ALL_PRESET):
                continue
            if value and value == pattern:
                return label
        return CUSTOM_PRESET

    def on_preset(self, _event=None) -> None:
        name = self.var_preset.get()
        if name == CUSTOM_PRESET:
            self.entry_filter.focus_set()
            self.hint_filter.configure(text="自己写正则，例如 (?i)(美国|洛杉矶|圣何塞)")
            return
        self.var_filter.set(REGION_PRESETS[name])
        self.hint_filter.configure(text="留空=全部保留；选「自定义」可直接写正则")

    def ok(self) -> None:
        name = self.var_name.get().strip()
        src = self.var_src.get().strip()
        if not name:
            messagebox.showwarning("缺少名称", "给这份订阅起个名字，比如 A、JC。", parent=self)
            return
        if not src:
            messagebox.showwarning("缺少地址", "填订阅 URL 或选本地 yaml 文件。", parent=self)
            return
        if not src.lower().startswith(("http://", "https://")):
            messagebox.showwarning("链接不对", "只支持 http/https 的 Clash 订阅链接。", parent=self)
            return
        spec: Dict[str, Any] = {
            "name": name,
            "url": src,
            "filter": self.var_filter.get().strip(),
            "exclude": self.var_exclude.get().strip(),
        }
        self.result = spec
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("sub-merger — 多机场订阅合并")
        self.geometry("1150x940")
        self.minsize(1120, 760)

        self.subs: List[Dict[str, Any]] = []
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.server: Optional[merger.SubServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.last_out: Optional[Path] = None
        self.busy = False
        self._done_evt = threading.Event()

        self.var_out = tk.StringVar(value=str(merger.resolve_output({}, APP_DIR)))
        self.var_rule = tk.StringVar(value="")
        self._want_union = False
        self.var_dedupe = tk.StringVar(value="server")
        self.var_proxy = tk.StringVar(value="http://127.0.0.1:7890")
        self.var_info = tk.BooleanVar(value=True)
        self.var_export = tk.StringVar(value=EXPORT_BOTH)
        self.var_offline = tk.BooleanVar(value=False)
        self.var_dns = tk.BooleanVar(value=False)
        self.var_port = tk.StringVar(value="8899")
        self.var_serve_hint = tk.StringVar(value="未启动")

        self._build_style()
        self._build_ui()
        self._load_cfg()
        self._redirect_streams()
        self.after(120, self._drain_log)

    # ------------------------------------------------------------ 外观

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure(".", font=UI_FONT)
        style.configure("Treeview", rowheight=24)
        style.configure("Head.TLabel", font=(UI_FONT[0], 15, "bold"))
        style.configure("Sub.TLabel", foreground="#666")
        style.configure("Accent.TButton", font=(UI_FONT[0], 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=(14, 12, 14, 10))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        head = ttk.Frame(root)
        head.grid(row=0, column=0, sticky="we")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="订阅合并器", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="多个机场订阅 → 只留你要的地区 → 一次生成 FlClash / sing-box 两份配置",
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Button(head, text="使用说明", command=self.show_help).grid(row=0, column=1, rowspan=2, sticky="e")

        # 订阅列表
        sub_box = ttk.LabelFrame(root, text=" 订阅 ", padding=10)
        sub_box.grid(row=1, column=0, sticky="nsew", pady=(12, 8))
        sub_box.columnconfigure(0, weight=1)

        columns = ("name", "src", "region", "primary")
        self.tree = ttk.Treeview(sub_box, columns=columns, show="headings", height=3, selectmode="browse")
        for col, text, width, anchor in (
            ("name", "名称", 110, "w"),
            ("src", "地址", 520, "w"),
            ("region", "地区", 80, "center"),
            ("primary", "规则", 60, "center"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "src"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _e: self.edit_sub())

        bar = ttk.Frame(sub_box)
        bar.grid(row=1, column=0, sticky="we", pady=(8, 0))
        ttk.Button(bar, text="添加", command=self.add_sub).pack(side="left")
        ttk.Button(bar, text="编辑", command=self.edit_sub).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="删除", command=self.del_sub).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="双击可编辑；「分流规则」下拉里选哪家，就用哪家的规则",
                  style="Sub.TLabel").pack(side="left", padx=(14, 0))
        ttk.Button(bar, text="↓", width=3, command=lambda: self.move_sub(1)).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="↑", width=3, command=lambda: self.move_sub(-1)).pack(side="right", padx=(4, 0))
        ttk.Label(bar, text="顺序", style="Sub.TLabel").pack(side="right", padx=(12, 0))

        # 选项
        opt = ttk.LabelFrame(root, text=" 输出 ", padding=10)
        opt.grid(row=2, column=0, sticky="we", pady=(0, 8))
        opt.columnconfigure(1, weight=1)

        ttk.Label(opt, text="保存到").grid(row=0, column=0, sticky="w")
        out_row = ttk.Frame(opt)
        out_row.grid(row=0, column=1, columnspan=3, sticky="we", padx=(8, 0))
        out_row.columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.var_out).grid(row=0, column=0, sticky="we")
        ttk.Button(out_row, text="另存为…", command=self.browse_out).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(opt, text="分流规则").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cmb_rule = ttk.Combobox(opt, textvariable=self.var_rule, state="readonly", width=18,
                                     values=[])
        self.cmb_rule.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        self.cmb_rule.bind("<<ComboboxSelected>>", self._on_rule_change)
        ttk.Label(opt, text="选某家=只用它自己的规则；选「全部拼接」=各家规则按顺序叠加",
                  style="Sub.TLabel").grid(row=1, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 0))

        ttk.Label(opt, text="节点去重").grid(row=2, column=0, sticky="w", pady=(8, 0))
        dedupe = ttk.Combobox(opt, textvariable=self.var_dedupe, state="readonly", width=10,
                              values=["server", "name"])
        dedupe.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(opt, text="server=同一服务器视为重复，name=同名视为重复",
                  style="Sub.TLabel").grid(row=2, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 0))

        ttk.Label(opt, text="拉订阅代理").grid(row=3, column=0, sticky="w", pady=(8, 0))
        proxy_row = ttk.Frame(opt)
        proxy_row.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Entry(proxy_row, textvariable=self.var_proxy, width=28).pack(side="left")
        ttk.Label(proxy_row, text="留空=直连，本地订阅不需要",
                  style="Sub.TLabel").pack(side="left", padx=(8, 0))
        extra = ttk.Frame(opt)
        extra.grid(row=3, column=2, columnspan=2, sticky="w", padx=(16, 0), pady=(8, 0))
        ttk.Checkbutton(extra, text="保留“剩余流量/到期”节点", variable=self.var_info).pack(side="left")
        ttk.Checkbutton(extra, text="带 DNS 配置", variable=self.var_dns).pack(side="left", padx=(12, 0))

        ttk.Label(opt, text="导出").grid(row=4, column=0, sticky="w", pady=(8, 0))
        exp_row = ttk.Frame(opt)
        exp_row.grid(row=4, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(8, 0))
        self.cmb_export = ttk.Combobox(exp_row, textvariable=self.var_export, state="readonly",
                                       width=24, values=EXPORT_CHOICES)
        self.cmb_export.pack(side="left")
        ttk.Label(exp_row, text="V2Ray 是 base64 订阅链接，给 v2rayN / 小火箭",
                  style="Sub.TLabel").pack(side="left", padx=(10, 0))
        ttk.Checkbutton(exp_row, text="离线（用上次缓存）", variable=self.var_offline).pack(side="left", padx=(16, 0))

        # 操作 + 日志
        act = ttk.Frame(root)
        act.grid(row=3, column=0, sticky="nsew")
        act.columnconfigure(0, weight=1)
        act.rowconfigure(2, weight=1)

        buttons = ttk.Frame(act)
        buttons.grid(row=0, column=0, sticky="we")
        self.btn_preview = ttk.Button(buttons, text="预览节点", command=self.do_preview)
        self.btn_preview.pack(side="left")
        self.btn_generate = ttk.Button(buttons, text="生成配置", style="Accent.TButton", command=self.do_generate)
        self.btn_generate.pack(side="left", padx=(6, 0))
        self.btn_check = ttk.Button(buttons, text="校验", command=self.do_check)
        self.btn_check.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="打开所在文件夹", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="配置文件", command=self.open_cfg).pack(side="left", padx=(6, 0))
        self.btn_import = ttk.Button(buttons, text="复制导入路径", command=self.copy_out_path)
        self.btn_import.pack(side="left", padx=(6, 0))
        ttk.Label(buttons, text="F5 生成 / F6 预览 / F7 校验",
                  style="Sub.TLabel").pack(side="right")

        serve = ttk.Frame(act)
        serve.grid(row=1, column=0, sticky="we", pady=(8, 0))
        self.btn_serve = ttk.Button(serve, text="启动本地订阅服务", command=self.toggle_serve)
        self.btn_serve.pack(side="left")
        ttk.Label(serve, text="端口").pack(side="left", padx=(10, 4))
        ttk.Entry(serve, textvariable=self.var_port, width=8).pack(side="left")
        ttk.Label(serve, textvariable=self.var_serve_hint, style="Sub.TLabel").pack(side="left", padx=(12, 0))

        log_box = ttk.LabelFrame(act, text=" 日志 ", padding=8)
        log_box.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, height=10, wrap="word", font=MONO_FONT,
                                background="#1f1f1f", foreground="#d8d8d8",
                                insertbackground="#d8d8d8", relief="flat", padx=10, pady=8)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.log_menu = tk.Menu(self, tearoff=0)
        self.log_menu.add_command(label="复制全部", command=self.copy_log)
        self.log_menu.add_command(label="清空日志", command=self.clear_log)
        self.log_text.bind("<Button-3>", self._show_log_menu)

        self.bind_all("<F1>", lambda _e: self.show_help())
        self.bind_all("<F5>", lambda _e: self.do_generate())
        self.bind_all("<F6>", lambda _e: self.do_preview())
        self.bind_all("<F7>", lambda _e: self.do_check())

    # ------------------------------------------------------------ 日志

    def _redirect_streams(self) -> None:
        proxy = StreamProxy(self.log)
        sys.stdout = proxy
        sys.stderr = proxy

    def log(self, text: str) -> None:
        self.log_queue.put(text)

    def _drain_log(self) -> None:
        chunks: List[str] = []
        try:
            while True:
                chunks.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            self.log_text.configure(state="normal")
            for chunk in chunks:
                self.log_text.insert("end", chunk + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        if self._done_evt.is_set():
            self._done_evt.clear()
            self._set_busy(False)
        self.after(120, self._drain_log)

    # ------------------------------------------------------------ 数据

    def _load_cfg(self) -> None:
        if not CFG_PATH.exists():
            self.refresh_tree()
            self.log("提示：还没有 merger.yaml，先点“添加”，填机场的 Clash 订阅链接。")
            return
        try:
            cfg = merger.load_config(CFG_PATH)
        except Exception as exc:  # noqa: BLE001
            self.log(f"读取 merger.yaml 失败：{exc}")
            self.refresh_tree()
            return
        self.subs = [dict(s) for s in cfg.get("subscriptions") or []]
        out = cfg.get("output") or {}
        self.var_out.set(out.get("path") or str(merger.resolve_output({}, APP_DIR)))
        self._want_union = out.get("rule_mode", "primary") == "union"
        self.var_dedupe.set(out.get("dedupe", "server"))
        self.var_proxy.set((cfg.get("network") or {}).get("proxy", ""))
        self.var_info.set(not bool((cfg.get("defaults") or {}).get("drop_info_nodes", False)))
        self.var_dns.set(bool(out.get("include_dns", False)))
        want_clash = out.get("clash", True) is not False
        want_sb = out.get("singbox", True) is not False
        want_v2 = bool(out.get("v2ray", False))
        if want_clash and want_sb and want_v2:
            self.var_export.set(EXPORT_ALL)
        elif want_clash and want_sb:
            self.var_export.set(EXPORT_BOTH)
        elif want_clash:
            self.var_export.set(EXPORT_CLASH)
        elif want_sb:
            self.var_export.set(EXPORT_SINGBOX)
        else:
            self.var_export.set(EXPORT_V2RAY)
        self.refresh_tree()
        self.log(f"已载入 {CFG_PATH.name}：{len(self.subs)} 份订阅")

    def build_cfg(self) -> Dict[str, Any]:
        if not self.subs:
            raise merger.MergerError("还没有订阅，先添加一个。")
        self._apply_rule_choice()
        rule_mode = "union" if self.var_rule.get() == UNION_LABEL else "primary"
        cfg = merger.deep_merge(merger.DEFAULTS, {
            "network": {"proxy": self.var_proxy.get().strip()},
            "output": {
                "path": self.var_out.get().strip() or str(merger.resolve_output({}, APP_DIR)),
                "rule_mode": rule_mode,
                "clash": self.var_export.get() in (EXPORT_BOTH, EXPORT_ALL, EXPORT_CLASH),
                "singbox": self.var_export.get() in (EXPORT_BOTH, EXPORT_ALL, EXPORT_SINGBOX),
                "v2ray": self.var_export.get() in (EXPORT_ALL, EXPORT_V2RAY),
                "dedupe": self.var_dedupe.get().strip() or "server",
                "include_dns": bool(self.var_dns.get()),
            },
            "defaults": {"drop_info_nodes": not self.var_info.get()},
        })
        cfg["subscriptions"] = [dict(s) for s in self.subs]
        return cfg

    def save_cfg(self, cfg: Dict[str, Any]) -> None:
        import yaml

        keep = {
            "network": cfg["network"],
            "output": {k: v for k, v in cfg["output"].items() if k != "target_map"},
            "subscriptions": cfg["subscriptions"],
            "defaults": cfg["defaults"],
            "cache_dir": cfg.get("cache_dir", ".cache"),
            "refresh_seconds": cfg.get("refresh_seconds", 1800),
        }
        text = yaml.safe_dump(keep, allow_unicode=True, sort_keys=False, default_flow_style=False)
        tmp = CFG_PATH.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, CFG_PATH)

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for spec in self.subs:
            src = spec.get("url") or spec.get("path") or ""
            region = region_label(spec.get("filter") or "")
            self.tree.insert("", "end", values=(
                spec.get("name", ""),
                merger.mask_url(src) if src.lower().startswith("http") else src,
                region,
                "★" if spec.get("primary") else "",
            ))
        self.refresh_rule_choices()

    def refresh_rule_choices(self) -> None:
        names = [spec.get("name", "") for spec in self.subs if spec.get("name")]
        values = names + [UNION_LABEL]
        self.cmb_rule.configure(values=values, state="readonly" if names else "disabled")
        if self.var_rule.get() not in values:
            fallback = UNION_LABEL if self._want_union else (
                next((s.get("name") for s in self.subs if s.get("primary")), names[0] if names else UNION_LABEL))
            self.var_rule.set(fallback)
        self._apply_rule_choice()

    def _apply_rule_choice(self) -> None:
        choice = self.var_rule.get()
        if not self.subs:
            return
        if choice == UNION_LABEL:
            for i, spec in enumerate(self.subs):
                spec["primary"] = i == 0
            return
        hit = False
        for spec in self.subs:
            spec["primary"] = (spec.get("name") == choice)
            hit = hit or spec["primary"]
        if not hit:
            self.subs[0]["primary"] = True

    def _on_rule_change(self, _event=None) -> None:
        self._want_union = self.var_rule.get() == UNION_LABEL
        self._apply_rule_choice()
        self.refresh_tree()

    def selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.index(sel[0])

    # ------------------------------------------------------------ 订阅操作

    def add_sub(self) -> None:
        dlg = SubDialog(self)
        self.wait_window(dlg)
        if not dlg.result:
            return
        if any(s["name"] == dlg.result["name"] for s in self.subs):
            messagebox.showwarning("名称重复", "换一个名字，前缀会拼在节点名前面。")
            return
        self.subs.append(dlg.result)
        if self.var_rule.get() not in [x.get("name") for x in self.subs]:
            self.var_rule.set(dlg.result["name"])
        self.refresh_tree()

    def edit_sub(self) -> None:
        idx = self.selected_index()
        if idx is None:
            messagebox.showinfo("未选中", "先在列表里点一份订阅。")
            return
        dlg = SubDialog(self, self.subs[idx])
        self.wait_window(dlg)
        if not dlg.result:
            return
        old_name = self.subs[idx].get("name")
        self.subs[idx] = dlg.result
        if self.var_rule.get() == old_name:
            self.var_rule.set(dlg.result.get("name", old_name))
        self.refresh_tree()

    def del_sub(self) -> None:
        idx = self.selected_index()
        if idx is None:
            return
        name = self.subs[idx].get("name")
        if not messagebox.askyesno("删除", f"删掉订阅「{name}」？"):
            return
        self.subs.pop(idx)
        self.refresh_tree()

    # ------------------------------------------------------------ 动作

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._done_evt.clear() if busy else None
        state = "disabled" if busy else "normal"
        for btn in (self.btn_preview, self.btn_generate, self.btn_check):
            btn.configure(state=state)
        self.configure(cursor="watch" if busy else "")

    def _run(self, title: str, fn) -> None:
        if self.busy:
            return
        try:
            cfg = self.build_cfg()
            self.save_cfg(cfg)
        except merger.MergerError as exc:
            messagebox.showwarning("配置不完整", str(exc))
            return
        self._set_busy(True)
        self.log(f"— {title} —")

        def work() -> None:
            try:
                fn(cfg)
            except merger.MergerError as exc:
                self.log(f"失败：{exc}")
            except Exception:  # noqa: BLE001
                self.log("出错：\n" + traceback.format_exc())
            finally:
                self._done_evt.set()

        threading.Thread(target=work, daemon=True).start()

    def _check_links(self) -> bool:
        if not self.subs:
            self.log('还没有订阅，点「添加」填一个 Clash 订阅链接。')
            return False
        missing = [str(sub.get('name') or '?') for sub in self.subs
                   if not str(sub.get('url') or '').strip()]
        if missing:
            self.log('这些订阅还没填链接：' + '、'.join(missing) + '  —— 双击那一行填上 http/https 链接再生成。')
            return False
        return True

    def do_preview(self) -> None:
        if not self._check_links():
            return

        def work(cfg: Dict[str, Any]) -> None:
            collected = merger.collect(cfg, APP_DIR, offline=bool(self.var_offline.get()))
            nodes = collected["nodes"]
            for s in collected["stats"]["subs"]:
                self.log(f"  {s['name']}: 抓取 {s['fetched']} → 保留 {s['kept']}，规则 {s['rules']}")
            self.log(f"  去重 {collected['stats']['deduped']} 个，合计 {len(nodes)} 个节点")
            for node in nodes[:40]:
                self.log(f"    {node['name']}  {node.get('type')}")
            if len(nodes) > 40:
                self.log(f"    …… 其余 {len(nodes) - 40} 个略")

        self._run("预览节点", work)

    def do_generate(self) -> None:
        if not self._check_links():
            return

        def work(cfg: Dict[str, Any]) -> None:
            collected = merger.collect(cfg, APP_DIR, offline=bool(self.var_offline.get()))
            config = merger.assemble(collected, cfg, collected["nodes"])
            errors = merger.validate(config, collected["nodes"])
            if errors:
                raise merger.MergerError("校验失败：\n  " + "\n  ".join(errors))
            out_path = merger._resolve_path(cfg["output"]["path"], APP_DIR)
            want_clash = cfg["output"].get("clash", True)
            if want_clash:
                merger.dump_yaml(config, collected["nodes"], out_path)
            merger.write_report(collected, config,
                                out_path.with_name(f"{out_path.stem}-report.md"))
            if cfg["output"].get("singbox", True):
                try:
                    sb_cfg, skipped = singbox.build(config, cfg)
                    problems = singbox.validate(sb_cfg)
                    if problems:
                        raise merger.MergerError("请修一下：" + chr(10) + "  " + (chr(10) + "  ").join(problems))
                    sb_path = out_path.with_name(f"{out_path.stem}-singbox.json")
                    singbox.dump(sb_cfg, sb_path)
                    self.log(f"  sing-box {sb_path.name}：{len(sb_cfg['outbounds'])} 个 outbound")
                    if skipped:
                        self.log(f"  跳过 {len(skipped)} 个不支持的节点（如 ssr/旧 hysteria）")
                except ValueError as exc:
                    self.log(f"  sing-box 未生成：{exc}")
            v2_path = None
            if cfg["output"].get("v2ray", False):
                try:
                    v2_links, v2_skipped = v2ray.links(collected["nodes"])
                    problems = v2ray.validate(v2_links)
                    if problems:
                        raise merger.MergerError("；".join(problems[:3]))
                    v2_path = out_path.with_name(f"{out_path.stem}-v2ray.txt")
                    v2ray.dump(v2_links, v2_path)
                    self.log(f"  v2ray {v2_path.name}：{len(v2_links)} 条链接（base64 订阅）")
                    if v2_skipped:
                        self.log(f"  跳过 {len(v2_skipped)} 个不支持的节点")
                except (ValueError, merger.MergerError) as exc:
                    self.log(f"  v2ray 未生成：{exc}")
            self.last_out = out_path
            merger.print_summary(config, collected, out_path)
            self.log("")
            self.log("✔ 生成成功")
            if v2_path:
                self.log(f"   V2Ray：把 {v2_path.name} 当订阅导入 v2rayN / 小火箭")
            if want_clash:
                self.log(f"   FlClash：配置 → + → 从文件导入 → {out_path.name}")
            if cfg["output"].get("singbox", True):
                self.log(f"   sing-box：用同目录的 {out_path.stem}-singbox.json")
            self.log(f"   {out_path.parent}")
            self.log("   → 已打开导出文件夹")
            try:
                os.startfile(str(out_path.parent))  # noqa: S606
            except OSError as exc:
                self.log(f"   （打开文件夹失败：{exc}，手动去上面那个目录拿文件）")
            if all((sub.get("path") or "") for sub in cfg["subscriptions"]):
                self.log("注：当前全部来自本机已下载的配置，节点不会自己更新；"
                         "换成订阅 URL 才会跟着机场刷新。")

        self._run("生成配置", work)

    def do_check(self) -> None:
        def work(cfg: Dict[str, Any]) -> None:
            out_path = self.last_out or merger.resolve_output(cfg, APP_DIR)
            code = merger.cmd_check(out_path)
            self.log("校验通过。" if code == 0 else f"校验未通过（exit {code}）。")

        self._run("校验", work)

    def browse_out(self) -> None:
        current = Path(self.var_out.get().strip() or str(merger.resolve_output({}, APP_DIR)))
        path = filedialog.asksaveasfilename(title="保存为", defaultextension=".yaml",
                                            initialdir=str(current.parent),
                                            initialfile=current.name,
                                            filetypes=[("YAML", "*.yaml")])
        if path:
            self.var_out.set(path)

    def current_out(self) -> Path:
        return self.last_out or Path(self.var_out.get().strip() or str(merger.resolve_output({}, APP_DIR)))

    def move_sub(self, delta: int) -> None:
        idx = self.selected_index()
        if idx is None:
            messagebox.showinfo("未选中", "先在列表里点一份订阅。")
            return
        new = idx + delta
        if not 0 <= new < len(self.subs):
            return
        self.subs[idx], self.subs[new] = self.subs[new], self.subs[idx]
        self.refresh_tree()
        children = self.tree.get_children()
        if new < len(children):
            self.tree.selection_set(children[new])
            self.tree.focus(children[new])

    def open_cfg(self) -> None:
        if not CFG_PATH.exists():
            messagebox.showinfo("还没有配置", "先点一次「生成配置」，会写出 merger.yaml。")
            return
        os.startfile(str(CFG_PATH))  # noqa: S606

    def _show_log_menu(self, event) -> None:
        self.log_menu.tk_popup(event.x_root, event.y_root)

    def copy_log(self) -> None:
        text = self.log_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def open_folder(self) -> None:
        out_path = self.current_out()
        folder = out_path if out_path.is_dir() else out_path.parent
        if not folder.exists():
            messagebox.showinfo("还没生成", "先生成一次配置。")
            return
        os.startfile(str(folder))  # noqa: S606

    def copy_out_path(self) -> None:
        out_path = self.current_out()
        self.clipboard_clear()
        self.clipboard_append(str(out_path))
        self.log(f"已复制路径：{out_path}")
        if not out_path.exists():
            self.log("（这个文件还不存在，先点“生成配置”）")

    # ------------------------------------------------------------ 本地服务

    def toggle_serve(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.btn_serve.configure(text="启动本地订阅服务")
            self.var_serve_hint.set("未启动")
            self.log("本地订阅服务已停止。")
            return
        try:
            cfg = self.build_cfg()
            self.save_cfg(cfg)
            port = int(self.var_port.get().strip() or "8899")
        except (merger.MergerError, ValueError) as exc:
            messagebox.showwarning("无法启动", str(exc))
            return
        try:
            self.server = merger.SubServer(("127.0.0.1", port), cfg, APP_DIR)
        except OSError as exc:
            messagebox.showerror("端口被占用", f"{exc}\n换个端口试试。")
            return
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.btn_serve.configure(text="停止本地订阅服务")
        url = f"http://127.0.0.1:{port}/sub"
        self.var_serve_hint.set(f"FlClash 导入：{url}")
        self.clipboard_clear()
        self.clipboard_append(url)
        self.log(f"本地订阅服务已启动：{url}（已复制）")
        self.log(f"  FlClash 用 /sub；sing-box 用 /sub.json；V2Ray/小火箭用 /sub.txt")
        self.log("改完订阅记得“停止 → 再启动”，服务会重新读配置。")

    # ------------------------------------------------------------ 帮助

    def show_help(self) -> None:
        lines = [
            '三步走',
            '',
            '1. 加订阅',
            '   · 点「添加」，填机场给的 Clash 订阅链接（http/https；v2ray/base64 订阅不支持）。',
            '   · 「地区预设」选美国=只留美区；选「自定义」自己写正则；留空=全留。',
            '   · 「排除正则」用来扔掉不想看到的节点，例如 (剩余流量|官网|群组)。',
            '   · 生成时会重新下载，节点跟着机场更新；订阅打不开时勾「离线（用上次缓存）」。',
            '',
            '2. 调好选项，再生成',
            '   · 分流规则：选某家=只用它的规则集，节点池仍是所有订阅合并后的；',
            '     选「全部拼接」=各家规则按顺序叠加，命中即停（顺序用订阅栏的 ↑↓ 调）。',
            '   · 节点去重：server=同一服务器算重复；name=同名算重复。',
            '   · 拉订阅代理：本机 FlClash 开着就填 http://127.0.0.1:7890，留空=直连。',
            '   · 「导出」下拉里挑要哪种：Clash / sing-box / V2Ray（可组合），各自生成对应文件。',
            '   · 点「生成配置」（F5）：得到 merged.yaml、merged-singbox.json、merged-report.md，',
            '     默认放在「下载/订阅导出」并自动打开文件夹（改「保存到」或点「另存为…」可换位置）。',
            '',
            '3. 导入客户端',
            '   FlClash：配置 → 右上角 + → 从文件导入 → merged.yaml',
            '            想自动更新：点「启动本地订阅服务」，再从 URL 导入 http://127.0.0.1:8899/sub',
            '            sing-box 用同端口的 /sub.json，V2Ray / 小火箭用 /sub.txt。',
            '   V2Ray：merged-v2ray.txt 是 base64 订阅内容，导给 v2rayN / 小火箭 / nekobox。',
            '   sing-box：桌面端把 merged-singbox.json 当配置文件用即可。',
            '            它自带 tun + mixed(127.0.0.1:2080) 入站；规则只有「局域网直连 + final=PROXY」，',
            '            要在 sing-box 里分流，得自己写 route.rules。',
            '',
            '小提示',
            '   · 双击订阅行可改名/改地区；↑↓ 调整顺序。',
            '   · 「配置文件」直接打开 merger.yaml；命令行也能跑：python merger.py -c merger.yaml',
            '   · 日志区右键可以复制全部 / 清空。',
            '   · 快捷键：F5 生成 / F6 预览 / F7 校验 / F1 说明。',
            '   · 产物含节点凭据，别外传；订阅链接本身就等于账号密码。',
        ]
        text = chr(10).join(lines)

        win = tk.Toplevel(self)
        win.title("使用说明")
        win.transient(self)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=text, font=UI_FONT, justify="left").pack(anchor="w")
        ttk.Button(body, text="知道了", command=win.destroy).pack(anchor="e", pady=(12, 0))
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + 60
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.bind("<Escape>", lambda _e: win.destroy())
        win.bind("<Return>", lambda _e: win.destroy())

    def destroy(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
        super().destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
