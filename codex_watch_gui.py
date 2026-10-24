#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 降智监测台 —— 常驻 GUI 版降智检测器（单文件 EXE）。

功能：
  - 长期驻守：可最小化到系统托盘；托盘图标绿色=最近一轮正常，红色=最近一轮有报警
  - 检测频率可设（1~1440 分钟），自动巡检可随时开/关
  - 「立即检测」按钮手动触发一次
  - 检测内容滚动展示：报警明细红色、静默灰色，新结果在最上面
  - 结果追加写入本目录 巡检日志.txt（超 1MB 自动截尾）；水位线 last_check_gui.txt
判定口径与 check_alert.py / degrade_detect.py 完全一致：
    gpt-6-astra / gpt-6.1-sol 回合第一包 reasoning_output_tokens>0（或用量未落盘
    时开口前已有推理条目）= 降智。独立水位线，与命令行巡检互不干扰。
数据文件全部保存在本程序（EXE）所在目录。--selftest 为无人自检模式。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
TARGET_MODELS = ("gpt-6-astra", "gpt-6.1-sol")
MARKERS = (
    '"task_started"',
    '"token_usage_record"',
    '"turn_context"',
    '"session_meta"',
    '"type":"response_item"',
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DATA = app_dir()
CONFIG_PATH = DATA / "codex_watch_config.json"
STATE_PATH = DATA / "last_check_gui.txt"
LOG_PATH = DATA / "巡检日志.txt"
AUTOSTART_BAT = (
    Path(os.environ.get("APPDATA", ""))
    / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    / "Codex降智监测台自启.bat"
)


# ─────────────────────────── 检测核心（纯逻辑，无 GUI 依赖） ───────────────────────────

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def file_day(path: Path):
    # rollout-2026-10-24T01-22-08-<uuid>.jsonl
    name = path.name
    if name.startswith("rollout-") and len(name) >= 18:
        try:
            return datetime.strptime(name[8:18], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def scan_file(path: Path) -> list:
    turns = {}
    order = []
    current = None
    last_model = None
    last_cwd = None
    session_id = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(m in line for m in MARKERS):
                continue
            import json as _json
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            kind = obj.get("type")
            payload = obj.get("payload") or {}
            ts = obj.get("timestamp") or ""
            if kind == "session_meta":
                session_id = payload.get("session_id") or payload.get("id")
                if payload.get("cwd"):
                    last_cwd = payload.get("cwd")
            elif kind == "event_msg" and payload.get("type") == "task_started":
                turn_id = payload.get("turn_id")
                root = payload.get("root_turn_id")
                if not turn_id or (root and root != turn_id):
                    current = None
                    continue
                current = turn_id
                if turn_id not in turns:
                    turns[turn_id] = {
                        "ts": ts, "model": last_model, "cwd": last_cwd,
                        "first_rsn": None, "reasoning_first": False,
                        "saw_assistant": False,
                    }
                    order.append(turn_id)
            elif kind == "turn_context":
                turn_id = payload.get("turn_id") or current
                model = payload.get("model")
                cwd = payload.get("cwd")
                if model:
                    last_model = model
                if cwd:
                    last_cwd = cwd
                if turn_id and turn_id in turns:
                    if model:
                        turns[turn_id]["model"] = model
                    if cwd:
                        turns[turn_id]["cwd"] = cwd
            elif kind == "token_usage_record":
                turn_id = payload.get("turn_id") or current
                turn = turns.get(turn_id) if turn_id else None
                if turn is None or turn["first_rsn"] is not None:
                    continue
                rsn = (payload.get("usage") or {}).get("reasoning_output_tokens")
                if rsn is None:
                    continue
                turn["first_rsn"] = rsn
                if not turn["ts"]:
                    turn["ts"] = ts
            elif kind == "response_item" and current and current in turns:
                turn = turns[current]
                it = payload.get("type")
                if it == "reasoning" and not turn["saw_assistant"]:
                    turn["reasoning_first"] = True
                elif it == "message" and payload.get("role") == "assistant":
                    turn["saw_assistant"] = True

    rows = []
    for turn_id in order:
        turn = turns[turn_id]
        if turn["first_rsn"] is not None:
            label = "reasoned" if turn["first_rsn"] > 0 else "zero"
        elif turn["reasoning_first"]:
            label = "reasoned"
        elif turn["saw_assistant"]:
            label = "zero"
        else:
            label = None
        rows.append({
            "session_id": session_id or turn_id,
            "ts": turn["ts"], "model": turn["model"] or "",
            "cwd": turn["cwd"] or "", "first_rsn": turn["first_rsn"],
            "label": label, "path": str(path),
        })
    return rows


def detect_once() -> dict:
    """增量检测一轮：水位线水位 = 上次成功扫描时间；首次运行回看 30 分钟。

    注意：rollout jsonl 的 mtime 不随追加更新（Codex 保留原始时间戳），
    不能按 mtime 过滤，只能按文件名日期回看若干天全量解析。
    """
    t0 = time.perf_counter()
    now = datetime.now().astimezone()
    if STATE_PATH.exists():
        try:
            last = datetime.fromisoformat(STATE_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            last = now - timedelta(minutes=30)
    else:
        last = now - timedelta(minutes=30)

    files = []
    home = codex_home()
    today = now.date()
    earliest = (last.date() if last.date() < today else today) - timedelta(days=7)
    for sub in ("sessions", "archived_sessions"):
        base = home / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*.jsonl"):
            d = file_day(p)
            if d is None or earliest <= d <= today:
                files.append(p)

    rows = []
    for p in sorted(files):
        try:
            found = scan_file(p)
        except OSError:
            continue
        for r in found:
            if r["model"] not in TARGET_MODELS or not r["ts"]:
                continue
            try:
                t = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if last < t <= now:
                r["_t"] = t.astimezone()
                rows.append(r)

    STATE_PATH.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    rows.sort(key=lambda r: r["_t"])
    bad = [r for r in rows if r["label"] == "reasoned"]
    win = f"{last.astimezone().strftime('%m-%d %H:%M')} ~ {now.strftime('%m-%d %H:%M')}"
    lines = []
    if not bad:
        lines.append(f"静默：窗口 {win}（本地时间）无新增降智回合，计入 {len(rows)} 回合。")
    else:
        lines.append(f"⚠️ Codex 降智报警：窗口 {win}（本地时间）新增 {len(bad)}/{len(rows)} 回合降智")
        for r in bad:
            lines.append(
                f"- 本地 {r['_t'].strftime('%m-%d %H:%M:%S')}"
                f"｜北京 {r['_t'].astimezone(BEIJING).strftime('%m-%d %H:%M')}"
                f"｜{r['model']}｜会话 {r['session_id'][:8]}｜{r['cwd'] or '（未记录）'}"
            )
            rsn = r["first_rsn"] if r["first_rsn"] is not None else "用量未落盘"
            lines.append(f"  首包推理={rsn}｜文件 {r['path']}")
    return {"text": "\n".join(lines), "bad": len(bad), "total": len(rows),
            "now": now, "files": len(files),
            "secs": round(time.perf_counter() - t0, 2)}


def append_log(text: str) -> None:
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {text}\n")
        if LOG_PATH.stat().st_size > 1_000_000:
            keep = LOG_PATH.read_text(encoding="utf-8", errors="replace")[-400_000:]
            LOG_PATH.write_text("……（旧日志已截断）……\n" + keep, encoding="utf-8")
    except OSError:
        pass



# ─────────────────────────── GUI ───────────────────────────

DEFAULT_CONFIG = {"interval_min": 5, "auto": True, "tray_on_close": True}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def make_icon_pil(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=color)
    d.rounded_rectangle((27, 12, 37, 38), radius=5, fill="white")
    d.ellipse((27, 44, 37, 54), fill="white")
    return img


# 配色（深色主题）
C = {
    "bg": "#0e1116", "card": "#161b22", "card2": "#1c222b", "line": "#262d38",
    "hover": "#232a35", "text": "#e6edf3", "muted": "#8b949e", "dim": "#5c6573",
    "accent": "#4f8cff", "accent_h": "#6a9eff", "ok": "#2ecc8f", "ok_bg": "#11291f",
    "bad": "#ff5c6c", "bad_bg": "#2e1519", "warn": "#f2b84b", "warn_bg": "#2d2410",
    "info": "#7aa7ff", "info_bg": "#141f33",
}
FONT = "Microsoft YaHei UI"
MONO = "Cascadia Mono"


class FlatButton:
    """Label 实现的扁平按钮：悬停高亮、禁用态、回车/空格可触发。"""

    def __init__(self, parent, text, command, kind="ghost", font_size=10,
                 padx=14, pady=7):
        import tkinter as tk
        self.command = command
        self.kind = kind
        self.enabled = True
        pal = {
            "primary": (C["accent"], C["accent_h"], "#ffffff"),
            "ghost": (C["card2"], C["hover"], C["text"]),
            "danger": (C["card2"], C["bad_bg"], C["bad"]),
        }[kind]
        self.bg, self.bg_h, self.fg = pal
        self.w = tk.Label(parent, text=text, bg=self.bg, fg=self.fg, cursor="hand2",
                          font=(FONT, font_size, "bold" if kind == "primary" else "normal"),
                          padx=padx, pady=pady, takefocus=1,
                          highlightthickness=1, highlightbackground=self.bg,
                          highlightcolor=C["accent"])
        self.w.bind("<Enter>", lambda e: self.enabled and self.w.config(bg=self.bg_h, highlightbackground=self.bg_h))
        self.w.bind("<Leave>", lambda e: self.w.config(bg=self.bg, highlightbackground=self.bg))
        self.w.bind("<ButtonRelease-1>", self._click)
        self.w.bind("<Return>", self._click)
        self.w.bind("<space>", self._click)

    def _click(self, _e=None):
        if self.enabled and self.command:
            self.command()

    def set_text(self, text):
        self.w.config(text=text)

    def state(self, flags):
        on = "!disabled" in flags or "disabled" not in flags
        self.enabled = on
        self.w.config(fg=self.fg if on else C["dim"],
                      cursor="hand2" if on else "arrow",
                      bg=self.bg if on else C["card2"])

    def pack(self, **kw):
        self.w.pack(**kw)
        return self


class Toggle:
    """画布绘制的开关 + 文字标签，整体可点击。"""

    def __init__(self, parent, text, variable, command=None, bg=None):
        import tkinter as tk
        bg = bg or C["card"]
        self.var, self.command, self.enabled = variable, command, True
        self.f = tk.Frame(parent, bg=bg, cursor="hand2")
        self.cv = tk.Canvas(self.f, width=38, height=22, bg=bg, highlightthickness=0)
        self.cv.pack(side="left")
        self.lbl = tk.Label(self.f, text=text, bg=bg, fg=C["text"], font=(FONT, 10))
        self.lbl.pack(side="left", padx=(7, 0))
        for w in (self.f, self.cv, self.lbl):
            w.bind("<ButtonRelease-1>", self._click)
        variable.trace_add("write", lambda *a: self.draw())
        self.draw()

    def _click(self, _e=None):
        if not self.enabled:
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def draw(self):
        cv = self.cv
        cv.delete("all")
        on = bool(self.var.get())
        col = (C["accent"] if on else "#3a4250") if self.enabled else "#2a303a"
        r = 11
        cv.create_oval(0, 0, 2 * r, 2 * r, fill=col, outline=col)
        cv.create_oval(38 - 2 * r, 0, 38, 2 * r, fill=col, outline=col)
        cv.create_rectangle(r, 0, 38 - r, 2 * r, fill=col, outline=col)
        x = 38 - r if on else r
        knob = "#ffffff" if self.enabled else "#6b7280"
        cv.create_oval(x - 8, 3, x + 8, 19, fill=knob, outline=knob)

    def state(self, flags):
        self.enabled = "disabled" not in flags
        self.lbl.config(fg=C["text"] if self.enabled else C["dim"])
        self.f.config(cursor="hand2" if self.enabled else "arrow")
        self.draw()

    def pack(self, **kw):
        self.f.pack(**kw)
        return self


class App:
    MAX_ENTRIES = 300

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, messagebox
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root = root
        self.cfg = load_config()
        self.busy = False
        self.after_id = None
        self.alarms_this_run = 0
        self.checks_this_run = 0
        self.last_result = None
        self.next_at = None
        self.tray = None
        self.entries = []          # 信息流条目：(kind, title, body, stamp)
        self.filter = "all"
        self._spin_angle = 0
        self._build_ui()
        self._tick()
        self.root.after(300, self.startup_check)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI ----------
    def _card(self, parent, **kw):
        tk = self.tk
        return tk.Frame(parent, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["line"], **kw)

    def _build_ui(self):
        tk = self.tk
        root = self.root
        root.title("Codex 降智监测台")
        root.geometry("980x700")
        root.minsize(820, 560)
        root.configure(bg=C["bg"])
        self._style_ttk()
        try:
            self._photo = self._pil_photo()
            root.iconphoto(True, self._photo)
        except Exception:
            pass

        wrap = tk.Frame(root, bg=C["bg"])
        wrap.pack(fill="both", expand=True, padx=18, pady=(14, 0))

        # ── 顶栏 ──
        head = tk.Frame(wrap, bg=C["bg"])
        head.pack(fill="x")
        logo = tk.Canvas(head, width=36, height=36, bg=C["bg"], highlightthickness=0)
        logo.pack(side="left")
        self._round_rect(logo, 1, 1, 35, 35, 9, fill=C["accent"])
        logo.create_text(18, 18, text="C", fill="white", font=(FONT, 15, "bold"))
        tt = tk.Frame(head, bg=C["bg"])
        tt.pack(side="left", padx=(10, 0))
        tk.Label(tt, text="Codex 降智监测台", bg=C["bg"], fg=C["text"],
                 font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(tt, text="监测 " + " / ".join(TARGET_MODELS) + " 首包推理异常",
                 bg=C["bg"], fg=C["muted"], font=(FONT, 9)).pack(anchor="w")
        self.pill = tk.Label(head, text="● 启动中", bg=C["info_bg"], fg=C["info"],
                             font=(FONT, 9, "bold"), padx=12, pady=4)
        self.pill.pack(side="right")

        # ── 状态主卡 ──
        hero = self._card(wrap)
        hero.pack(fill="x", pady=(14, 0))
        inner = tk.Frame(hero, bg=C["card"])
        inner.pack(fill="x", padx=18, pady=16)
        self.ring = tk.Canvas(inner, width=72, height=72, bg=C["card"], highlightthickness=0)
        self.ring.pack(side="left")
        txt = tk.Frame(inner, bg=C["card"])
        txt.pack(side="left", padx=(16, 0), fill="x", expand=True)
        self.hero_title = tk.Label(txt, text="正在准备首轮检测", bg=C["card"], fg=C["text"],
                                   font=(FONT, 17, "bold"), anchor="w")
        self.hero_title.pack(anchor="w")
        self.hero_sub = tk.Label(txt, text="稍候片刻……", bg=C["card"], fg=C["muted"],
                                 font=(FONT, 10), anchor="w", justify="left")
        self.hero_sub.pack(anchor="w", pady=(4, 0))
        self.btn_check = FlatButton(inner, "⟳  立即检测", self.manual_check,
                                    kind="primary", font_size=11, padx=22, pady=10)
        self.btn_check.pack(side="right")
        self._draw_ring("idle")

        # ── 指标卡 ──
        stats = tk.Frame(wrap, bg=C["bg"])
        stats.pack(fill="x", pady=(12, 0))
        self.stat = {}
        specs = [("next", "下次巡检", "—"), ("last", "上次检测", "—"),
                 ("alarm", "本次运行报警", "0"), ("scan", "最近一轮", "—")]
        for i, (key, label, val) in enumerate(specs):
            card = self._card(stats)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            stats.grid_columnconfigure(i, weight=1, uniform="s")
            tk.Label(card, text=label, bg=C["card"], fg=C["muted"],
                     font=(FONT, 9)).pack(anchor="w", padx=14, pady=(10, 0))
            v = tk.Label(card, text=val, bg=C["card"], fg=C["text"], font=(FONT, 16, "bold"))
            v.pack(anchor="w", padx=14)
            s = tk.Label(card, text=" ", bg=C["card"], fg=C["dim"], font=(FONT, 8))
            s.pack(anchor="w", padx=14, pady=(0, 10))
            self.stat[key] = (v, s)

        # ── 设置条 ──
        bar = self._card(wrap)
        bar.pack(fill="x", pady=(12, 0))
        bi = tk.Frame(bar, bg=C["card"])
        bi.pack(fill="x", padx=14, pady=10)
        tk.Label(bi, text="检测频率", bg=C["card"], fg=C["muted"],
                 font=(FONT, 10)).pack(side="left")
        self.interval_var = tk.StringVar(value=str(self.cfg.get("interval_min", 5)))
        step = tk.Frame(bi, bg=C["card2"])
        step.pack(side="left", padx=(8, 0))
        FlatButton(step, "−", lambda: self._bump(-1), padx=9, pady=3).pack(side="left")
        self.spn = tk.Entry(step, textvariable=self.interval_var, width=5, justify="center",
                            bg=C["card2"], fg=C["text"], insertbackground=C["text"],
                            relief="flat", font=(FONT, 11, "bold"))
        self.spn.pack(side="left", ipady=3)
        FlatButton(step, "+", lambda: self._bump(1), padx=9, pady=3).pack(side="left")
        tk.Label(bi, text="分钟", bg=C["card"], fg=C["muted"],
                 font=(FONT, 10)).pack(side="left", padx=(6, 0))
        self.spn.bind("<Return>", lambda e: self.on_interval_change())
        self.spn.bind("<FocusOut>", lambda e: self.on_interval_change())
        self.spn.bind("<MouseWheel>", lambda e: self._bump(1 if e.delta > 0 else -1))
        self.spn.bind("<Up>", lambda e: self._bump(1))
        self.spn.bind("<Down>", lambda e: self._bump(-1))

        tk.Frame(bi, bg=C["line"], width=1, height=22).pack(side="left", padx=16)
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto", True)))
        Toggle(bi, "自动巡检", self.auto_var, self.on_auto_toggle).pack(side="left")
        self.boot_var = tk.BooleanVar(value=AUTOSTART_BAT.exists())
        Toggle(bi, "开机自启", self.boot_var, self.on_boot_toggle).pack(side="left", padx=(18, 0))
        self.tray_var = tk.BooleanVar(value=bool(self.cfg.get("tray_on_close", True)))
        self.tray_cb = Toggle(bi, "关闭时驻留托盘", self.tray_var, self.on_tray_pref)
        self.tray_cb.pack(side="left", padx=(18, 0))

        self.btn_quit = FlatButton(bi, "退出", self.quit_app, kind="danger", padx=12, pady=4)
        self.btn_quit.pack(side="right")
        FlatButton(bi, "数据目录", lambda: os.startfile(DATA), padx=12, pady=4).pack(
            side="right", padx=(0, 8))

        # ── 信息流 ──
        feed = self._card(wrap)
        feed.pack(fill="both", expand=True, pady=(12, 0))
        fh = tk.Frame(feed, bg=C["card"])
        fh.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(fh, text="检测记录", bg=C["card"], fg=C["text"],
                 font=(FONT, 11, "bold")).pack(side="left")
        self.feed_count = tk.Label(fh, text="", bg=C["card"], fg=C["dim"], font=(FONT, 9))
        self.feed_count.pack(side="left", padx=(8, 0))
        FlatButton(fh, "清空", self.clear_view, padx=10, pady=3, font_size=9).pack(side="right")
        seg = tk.Frame(fh, bg=C["card2"])
        seg.pack(side="right", padx=(0, 10))
        self.seg_btns = {}
        for key, label in (("all", "全部"), ("alarm", "仅报警"), ("silent", "静默"),
                           ("sys", "系统")):
            b = tk.Label(seg, text=label, font=(FONT, 9), padx=10, pady=3, cursor="hand2")
            b.pack(side="left")
            b.bind("<ButtonRelease-1>", lambda e, k=key: self.set_filter(k))
            self.seg_btns[key] = b
        self._paint_seg()

        body = tk.Frame(feed, bg=C["card"])
        body.pack(fill="both", expand=True, padx=(14, 4), pady=(0, 10))
        self.txt = tk.Text(body, wrap="word", bg=C["card"], fg=C["text"], relief="flat",
                           font=(FONT, 10), padx=4, pady=4, borderwidth=0,
                           highlightthickness=0, cursor="arrow", spacing1=1, spacing3=1,
                           selectbackground="#2b4a7a", insertwidth=0)
        sb = self.ttk.Scrollbar(body, orient="vertical", command=self.txt.yview,
                                style="Dark.Vertical.TScrollbar")
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        for kind in ("alarm", "silent", "sys", "err"):
            fg, bg = {"alarm": (C["bad"], C["bad_bg"]), "silent": (C["ok"], C["ok_bg"]),
                      "sys": (C["info"], C["info_bg"]), "err": (C["warn"], C["warn_bg"])}[kind]
            self.txt.tag_configure(f"badge_{kind}", foreground=fg, background=bg,
                                   font=(FONT, 9, "bold"))
        self.txt.tag_configure("title", font=(FONT, 10, "bold"), foreground=C["text"])
        self.txt.tag_configure("stamp", foreground=C["dim"], font=(FONT, 9))
        self.txt.tag_configure("body", foreground=C["muted"], lmargin1=14, lmargin2=14,
                               font=(FONT, 9), spacing1=2)
        self.txt.tag_configure("bodybad", foreground="#ffb3ba", lmargin1=14, lmargin2=28,
                               font=(FONT, 9), spacing1=3)
        self.txt.tag_configure("path", foreground=C["dim"], lmargin1=28, lmargin2=28,
                               font=(MONO, 8))
        self.txt.tag_configure("sep", font=(FONT, 4))
        self.txt.tag_configure("empty", foreground=C["dim"], justify="center",
                               font=(FONT, 10), spacing1=40)
        self.txt.bind("<Key>", lambda e: None if (e.state & 4) else "break")  # 只读但可复制

        # ── 底栏 ──
        self.status = tk.Label(root, text="启动中……", bg=C["bg"], fg=C["dim"],
                               font=(FONT, 9), anchor="w", padx=20, pady=6)
        self.status.pack(fill="x", side="bottom")
        tk.Label(root, text="F5 立即检测 · Ctrl+L 清空 · Esc 隐藏到托盘", bg=C["bg"],
                 fg=C["dim"], font=(FONT, 9)).place(relx=1.0, rely=1.0, x=-20, y=-6,
                                                    anchor="se")

        root.bind("<F5>", lambda e: self.manual_check())
        root.bind("<Control-r>", lambda e: self.manual_check())
        root.bind("<Control-l>", lambda e: self.clear_view())
        root.bind("<Escape>", lambda e: self.on_close() if self.tray is not None else None)
        self._render_feed()

    def _style_ttk(self):
        st = self.ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            return
        st.configure("Dark.Vertical.TScrollbar", background=C["card2"], troughcolor=C["card"],
                     bordercolor=C["card"], arrowcolor=C["muted"], lightcolor=C["card2"],
                     darkcolor=C["card2"], gripcount=0)
        st.map("Dark.Vertical.TScrollbar", background=[("active", C["hover"])])

    @staticmethod
    def _round_rect(cv, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return cv.create_polygon(pts, smooth=True, **kw)

    def _draw_ring(self, mode):
        cv = self.ring
        cv.delete("all")
        col = {"ok": C["ok"], "bad": C["bad"], "busy": C["accent"],
               "err": C["warn"], "idle": C["dim"]}[mode]
        cv.create_oval(6, 6, 66, 66, outline=C["line"], width=6)
        if mode == "busy":
            cv.create_arc(6, 6, 66, 66, start=self._spin_angle, extent=100,
                          style="arc", outline=col, width=6)
        else:
            cv.create_oval(6, 6, 66, 66, outline=col, width=6)
        glyph = {"ok": "✓", "bad": "!", "busy": "…", "err": "?", "idle": "·"}[mode]
        cv.create_text(36, 36, text=glyph, fill=col, font=(FONT, 20, "bold"))

    def _spin(self):
        if not self.busy:
            return
        self._spin_angle = (self._spin_angle - 12) % 360
        self._draw_ring("busy")
        self.root.after(30, self._spin)

    def _paint_seg(self):
        for k, b in self.seg_btns.items():
            on = k == self.filter
            b.config(bg=C["accent"] if on else C["card2"], fg="#fff" if on else C["muted"])

    def _bump(self, d):
        try:
            v = int(self.interval_var.get())
        except ValueError:
            v = 5
        self.interval_var.set(str(max(1, min(1440, v + d))))
        self.on_interval_change()
        return "break"

    def _pil_photo(self):
        from PIL import ImageTk
        return ImageTk.PhotoImage(make_icon_pil((79, 140, 255, 255)))

    # ---------- 检测调度 ----------
    def startup_check(self):
        self.log_view("监测台已启动，进行首轮检测……", "sys")
        self.start_thread(auto=True)

    def manual_check(self):
        self.start_thread(auto=False)

    def start_thread(self, auto: bool):
        if self.busy:
            self.log_view("上一次检测还没结束，忽略本次触发。", "sys")
            if auto:
                self.schedule_next()
            return
        self.busy = True
        self.btn_check.state(["disabled"])
        self.btn_check.set_text("检测中…")
        self.hero_title.config(text="正在扫描会话日志", fg=C["text"])
        self.hero_sub.config(text=f"读取 {codex_home()} 下的 rollout 记录……")
        self._set_pill("● 检测中", C["info"], C["info_bg"])
        self._spin()
        self.set_status("检测中……")

        def work():
            try:
                res = detect_once()
            except Exception:
                err = traceback.format_exc()
                append_log("检测异常：\n" + err)
                res = {"text": "检测发生异常，详见 巡检日志.txt", "bad": -1,
                       "total": 0, "now": datetime.now().astimezone(),
                       "files": 0, "secs": 0.0}
            self.root.after(0, lambda: self.on_done(res, auto))

        threading.Thread(target=work, daemon=True).start()

    def on_done(self, res, auto: bool):
        self.busy = False
        self.btn_check.state(["!disabled"])
        self.btn_check.set_text("⟳  立即检测")
        try:
            self.checks_this_run += 1
            stamp = res["now"].strftime("%m-%d %H:%M:%S")
            meta = f"扫描 {res['files']} 个文件 · {res['total']} 回合 · {res['secs']}s"
            lines = res["text"].split("\n")
            if res["bad"] < 0:
                kind, title = "err", "检测异常"
                self._draw_ring("err")
                self.hero_title.config(text="检测出错", fg=C["warn"])
                self.hero_sub.config(text="详情已写入 巡检日志.txt，可点击「数据目录」查看")
                self._set_pill("● 异常", C["warn"], C["warn_bg"])
            elif res["bad"] > 0:
                kind, title = "alarm", f"发现 {res['bad']}/{res['total']} 回合降智"
                self._draw_ring("bad")
                self.hero_title.config(text=f"发现降智 · {res['bad']} 回合", fg=C["bad"])
                self.hero_sub.config(text=f"{stamp} · {meta}\n明细见下方检测记录")
                self._set_pill("● 报警", C["bad"], C["bad_bg"])
            else:
                kind, title = "silent", "一切正常"
                self._draw_ring("ok")
                self.hero_title.config(text="运行正常 · 未发现降智", fg=C["ok"])
                self.hero_sub.config(text=f"{stamp} · {meta}")
                self._set_pill("● 正常", C["ok"], C["ok_bg"])
            self.log_view("\n".join(lines), kind, title=title, meta=meta)
            append_log(res["text"])
            if res["bad"] > 0:
                self.alarms_this_run += res["bad"]
                self.set_tray_color(alarm=True)
                self.notify(f"Codex 降智报警：新增 {res['bad']} 回合，点开查看")
                try:
                    self.root.bell()
                except Exception:
                    pass
            else:
                self.set_tray_color(alarm=False)
            self.last_result = res
            v, s = self.stat["last"]
            v.config(text=res["now"].strftime("%H:%M:%S"))
            s.config(text="报警" if res["bad"] > 0 else ("异常" if res["bad"] < 0 else "静默"))
            v, s = self.stat["alarm"]
            v.config(text=str(self.alarms_this_run),
                     fg=C["bad"] if self.alarms_this_run else C["text"])
            s.config(text=f"共检测 {self.checks_this_run} 轮")
            v, s = self.stat["scan"]
            v.config(text=f"{res['total']} 回合")
            s.config(text=f"{res['files']} 文件 · {res['secs']}s")
        except Exception:
            # 窗口版程序会吞掉界面回调异常，必须落盘留痕，且绝不能卡住状态
            append_log("界面更新异常：\n" + traceback.format_exc())
            try:
                self.log_view("界面更新异常，详见 巡检日志.txt", "err")
            except Exception:
                pass
        finally:
            if auto:
                self.schedule_next()
            else:
                self.refresh_status()

    def on_interval_change(self):
        try:
            v = int(self.interval_var.get())
        except ValueError:
            self.interval_var.set(str(self.cfg.get("interval_min", 5)))
            return
        v = max(1, min(1440, v))
        self.interval_var.set(str(v))
        if v == self.cfg.get("interval_min") and self.after_id is not None:
            return
        self.cfg["interval_min"] = v
        save_config(self.cfg)
        if self.auto_var.get() and not self.busy:
            self.schedule_next()
        else:
            self.refresh_status()

    def on_auto_toggle(self):
        self.cfg["auto"] = bool(self.auto_var.get())
        save_config(self.cfg)
        if self.auto_var.get():
            self.schedule_next()
        else:
            self.cancel_timer()
            self.next_at = None
            self.refresh_status()

    def schedule_next(self):
        self.cancel_timer()
        if not self.auto_var.get():
            self.next_at = None
            self.refresh_status()
            return
        minutes = 5
        try:
            minutes = max(1, int(self.interval_var.get()))
        except ValueError:
            pass
        self.next_at = datetime.now() + timedelta(minutes=minutes)
        self.after_id = self.root.after(minutes * 60_000, self.auto_fire)
        self.refresh_status()

    def auto_fire(self):
        self.after_id = None
        self.start_thread(auto=True)

    def cancel_timer(self):
        if self.after_id is not None:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

    def _tick(self):
        """每秒刷新「下次巡检」倒计时。"""
        try:
            v, s = self.stat["next"]
            if self.busy:
                v.config(text="检测中", fg=C["accent"])
                s.config(text=" ")
            elif not self.auto_var.get():
                v.config(text="已暂停", fg=C["dim"])
                s.config(text="自动巡检已关闭")
            elif self.next_at:
                left = max(0, int((self.next_at - datetime.now()).total_seconds()))
                h, rem = divmod(left, 3600)
                m, sec = divmod(rem, 60)
                v.config(text=(f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"),
                         fg=C["text"])
                s.config(text=f"于 {self.next_at.strftime('%H:%M:%S')} · 每 "
                              f"{self.interval_var.get()} 分钟")
        except Exception:
            pass
        self.root.after(1000, self._tick)

    # ---------- 展示 ----------
    def _set_pill(self, text, fg, bg):
        self.pill.config(text=text, fg=fg, bg=bg)

    def log_view(self, text: str, tag: str, title: str = None, meta: str = ""):
        kind = {"head": "sys", "silent": "silent", "alarm": "alarm",
                "err": "err"}.get(tag, "sys")
        if title is None:
            title, text = text.strip(), ""
        self.entries.insert(0, (kind, title, text, meta, datetime.now().strftime("%H:%M:%S")))
        del self.entries[self.MAX_ENTRIES:]
        self._render_feed()

    def set_filter(self, key):
        self.filter = key
        self._paint_seg()
        self._render_feed()

    def _render_feed(self):
        t = self.txt
        t.delete("1.0", "end")
        shown = [e for e in self.entries
                 if self.filter == "all" or e[0] == self.filter
                 or (self.filter == "sys" and e[0] == "err")]
        n_alarm = sum(1 for e in self.entries if e[0] == "alarm")
        self.feed_count.config(text=f"{len(self.entries)} 条 · 报警 {n_alarm}")
        if not shown:
            t.insert("end", "暂无记录", "empty")
            return
        badge = {"alarm": " 报警 ", "silent": " 静默 ", "sys": " 系统 ", "err": " 异常 "}
        for kind, title, body, meta, stamp in shown:
            t.insert("end", badge[kind], f"badge_{kind}")
            t.insert("end", "  " + title, "title")
            t.insert("end", f"   {stamp}" + (f" · {meta}" if meta else "") + "\n", "stamp")
            for ln in body.split("\n") if body else []:
                if not ln.strip():
                    continue
                if ln.startswith("- "):
                    t.insert("end", "• " + ln[2:] + "\n", "bodybad")
                elif ln.startswith("  "):
                    t.insert("end", ln.strip() + "\n", "path")
                else:
                    t.insert("end", ln + "\n", "body")
            t.insert("end", "\n", "sep")

    def clear_view(self):
        self.entries.clear()
        self.log_view("显示已清空（日志文件不受影响）。", "sys")

    def set_status(self, prefix: str):
        auto = "开" if self.auto_var.get() else "关"
        try:
            itv = int(self.interval_var.get())
        except ValueError:
            itv = "?"
        parts = [p for p in (prefix.strip("｜ "), f"自动巡检 {auto}（每 {itv} 分钟）",
                             f"数据目录 {DATA}") if p]
        self.status.config(text="   ·   ".join(parts))

    def refresh_status(self):
        last = ""
        if self.last_result is not None:
            n = self.last_result["bad"]
            last = f"上次 {self.last_result['now'].strftime('%H:%M:%S')} " \
                   f"{'报警 +%d' % n if n > 0 else '静默'}"
        self.set_status(last)

    # ---------- 托盘 ----------
    def start_tray(self):
        try:
            import pystray
        except Exception:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", self.tray_show, default=True),
                pystray.MenuItem("立即检测", lambda: self.root.after(0, self.manual_check)),
                pystray.MenuItem("退出", lambda: self.root.after(0, self.quit_app)),
            )
            self.tray = pystray.Icon(
                "CodexWatch", make_icon_pil((46, 125, 50, 255)),
                "Codex 降智监测台", menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def tray_show(self, *_):
        def _show():
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        self.root.after(0, _show)

    def set_tray_color(self, alarm: bool):
        if self.tray is None:
            return
        try:
            self.tray.icon = make_icon_pil(
                (211, 47, 47, 255) if alarm else (46, 125, 50, 255))
        except Exception:
            pass

    def notify(self, message: str):
        if self.tray is None:
            return
        try:
            self.tray.notify(message, "Codex 降智监测台")
        except Exception:
            pass

    def on_tray_pref(self):
        self.cfg["tray_on_close"] = bool(self.tray_var.get())
        save_config(self.cfg)

    # ---------- 开机自启 ----------
    def on_boot_toggle(self):
        try:
            if self.boot_var.get():
                AUTOSTART_BAT.parent.mkdir(parents=True, exist_ok=True)
                exe = sys.executable if getattr(sys, "frozen", False) else \
                    f'pyw "{Path(__file__).resolve()}"'
                if getattr(sys, "frozen", False):
                    AUTOSTART_BAT.write_text(f'@echo off\nstart "" "{exe}"\n',
                                             encoding="utf-8")
                else:
                    AUTOSTART_BAT.write_text(f'@echo off\n{exe}\n', encoding="utf-8")
                self.log_view("已开启开机自启。", "sys")
            else:
                AUTOSTART_BAT.unlink(missing_ok=True)
                self.log_view("已关闭开机自启。", "sys")
        except OSError as e:
            self.messagebox.showerror("开机自启", f"设置失败：{e}")
            self.boot_var.set(not self.boot_var.get())

    # ---------- 退出 ----------
    def on_close(self):
        if self.tray is not None and self.tray_var.get():
            self.root.withdraw()
            self.notify("仍在后台监测中，点图标恢复窗口")
            return
        self.quit_app()

    def quit_app(self):
        self.cancel_timer()
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--selftest", action="store_true",
                        help="无人自检：跑一轮检测，结果写 selftest_result.txt 后退出")
    args, _ = parser.parse_known_args()
    if args.selftest:
        res = detect_once()
        out = (res["text"]
               + f"\nSELFTEST_OK bad={res['bad']} total={res['total']}"
                 f" files={res['files']} secs={res['secs']}")
        (DATA / "selftest_result.txt").write_text(out, encoding="utf-8")
        try:
            print(out)
        except OSError:
            pass
        return

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    import tkinter as tk
    root = tk.Tk()
    app = App(root)
    app.start_tray()
    if app.tray is None:
        app.log_view("系统托盘不可用：关闭窗口将直接退出程序", "sys")
        app.tray_cb.state(["disabled"])
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        Path(app_dir() / "崩溃日志.txt").write_text(
            traceback.format_exc(), encoding="utf-8")
        raise
