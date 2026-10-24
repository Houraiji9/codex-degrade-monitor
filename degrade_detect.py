#!/usr/bin/env python3
"""Codex 降智检测器（独立单文件版，零依赖，Python 3.9+，只读不写）。

用法（本机用 py 启动）：
    py degrade_detect.py                     # 默认扫最近 7 个自然日（含今天）
    py degrade_detect.py --days 3            # 最近 3 天
    py degrade_detect.py --since 2026-10-01 --until 2026-10-24
    py degrade_detect.py --all               # 全部历史
    py degrade_detect.py --model gpt-6-astra # 只看一个模型
    py degrade_detect.py --session 01a16d00  # 按会话ID前缀看逐回合明细

判定口径（与每5分钟巡检完全一致）：
    只统计 gpt-6-astra / gpt-6.1-sol 的回合；
    回合第一包 token_usage_record 的 reasoning_output_tokens > 0 => 降智；
    用量未落盘时，开口之前已出现 reasoning 条目 => 也算降智。

日期按本机时区自然日，含首尾。扫描 ~/.codex/sessions 与 archived_sessions
（可用环境变量 CODEX_HOME 改根目录）。旧会话文件跨天续聊时，回合按其
自身时间戳归入当天。本程序不写任何文件，不影响巡检水位线。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

TARGET_MODELS = ("gpt-6-astra", "gpt-6.1-sol")
BEIJING = timezone(timedelta(hours=8))
MARKERS = (
    '"task_started"',
    '"token_usage_record"',
    '"turn_context"',
    '"session_meta"',
    '"type":"response_item"',
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Codex 降智检测器（独立版，只读）")
    ap.add_argument("--days", type=int, help="扫最近 N 个自然日（含今天），默认 7")
    ap.add_argument("--since", help="起始日 YYYY-MM-DD（本地时区，含当天）")
    ap.add_argument("--until", help="结束日 YYYY-MM-DD（缺省为今天）")
    ap.add_argument("--all", action="store_true", help="扫描全部历史")
    ap.add_argument("--model", choices=TARGET_MODELS, help="只看一个模型（默认两个都看）")
    ap.add_argument("--session", help="会话ID前缀或 jsonl 路径：输出该会话逐回合明细")
    return ap.parse_args()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%m-%d %H:%M:%S")


def fmt_bj(dt: datetime) -> str:
    return dt.astimezone(BEIJING).strftime("%m-%d %H:%M")


def file_day(path: Path) -> date | None:
    # rollout-2026-10-24T01-22-08-<uuid>.jsonl
    name = path.name
    if name.startswith("rollout-") and len(name) >= 18:
        try:
            return datetime.strptime(name[8:18], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def scan_file(path: Path) -> list[dict]:
    """返回该 jsonl 内全部回合（未按模型/时间过滤）。"""
    turns: dict[str, dict] = {}
    order: list[str] = []
    current = None
    last_model = None
    last_cwd = None
    session_id = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(m in line for m in MARKERS):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
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
                        "ts": ts,
                        "model": last_model,
                        "cwd": last_cwd,
                        "first_rsn": None,
                        "reasoning_first": False,
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
                item_type = payload.get("type")
                if item_type == "reasoning" and not turn["saw_assistant"]:
                    turn["reasoning_first"] = True
                elif item_type == "message" and payload.get("role") == "assistant":
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
        rows.append(
            {
                "session_id": session_id or turn_id,
                "turn_id": turn_id,
                "ts": turn["ts"],
                "model": turn["model"] or "",
                "cwd": turn["cwd"] or "",
                "first_rsn": turn["first_rsn"],
                "label": label,
                "path": str(path),
            }
        )
    return rows


def collect_files(home: Path, since: date | None, until: date) -> list[Path]:
    files: list[Path] = []
    for sub in ("sessions", "archived_sessions"):
        base = home / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*.jsonl"):
            d = file_day(p)
            if since is None or d is None:
                files.append(p)  # 全量，或无法解析日期的宁多勿漏
            elif since - timedelta(days=7) <= d <= until:
                # 多回溯 7 天：旧会话文件可能跨天续聊产生新回合
                files.append(p)
    return sorted(files)


def short_project(cwd: str) -> str:
    if not cwd:
        return "（未记录）"
    parts = Path(cwd).parts
    if len(parts) >= 2:
        return parts[-2] + "\\" + parts[-1]
    return cwd


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "-"


def resolve_window(args: argparse.Namespace):
    local_tz = datetime.now().astimezone().tzinfo
    today = datetime.now().astimezone().date()
    until = date.fromisoformat(args.until) if args.until else today
    if args.all:
        return None, until
    if args.since:
        since = date.fromisoformat(args.since)
    else:
        n = args.days if args.days and args.days > 0 else 7
        since = today - timedelta(days=n - 1)
    if since > until:
        raise SystemExit(f"起始日 {since} 晚于结束日 {until}，请检查参数。")
    return since, until


def run_session_mode(args: argparse.Namespace) -> None:
    key = args.session
    paths: list[Path]
    if os.path.isfile(key):
        paths = [Path(key)]
    else:
        home = codex_home()
        paths = []
        for sub in ("sessions", "archived_sessions"):
            base = home / sub
            if base.is_dir():
                paths.extend(sorted(base.rglob(f"*{key}*.jsonl")))
    if not paths:
        raise SystemExit(f"没找到匹配 '{key}' 的会话文件。")

    for path in paths:
        rows = scan_file(path)
        rows.sort(key=lambda r: r["ts"] or "")
        sid = rows[0]["session_id"] if rows else "?"
        print(f"══ 会话 {sid} ══  文件 {path}")
        print(f"共 {len(rows)} 回合；标记〖降智〗= 首包 reasoning_output_tokens>0 或开口前已有推理条目")
        for r in rows:
            t = parse_utc(r["ts"]) if r["ts"] else None
            when = f"{fmt_local(t)}（北京 {fmt_bj(t)}）" if t else "（无时间戳）"
            if r["model"] not in TARGET_MODELS:
                verdict = "非目标模型，不统计"
            elif r["label"] == "reasoned":
                verdict = "〖降智〗"
            elif r["label"] == "zero":
                verdict = "正常"
            else:
                verdict = "无判定"
            rsn = "用量未落盘" if r["first_rsn"] is None else f"首包推理={r['first_rsn']}"
            print(f"  {when}  {r['model']:<13} {rsn:<14} {verdict}")
        print()


def run_report_mode(args: argparse.Namespace) -> None:
    since, until = resolve_window(args)
    home = codex_home()
    local_tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(since, time.min, tzinfo=local_tz) if since else None
    end = datetime.combine(until, time.max, tzinfo=local_tz)
    models = (args.model,) if args.model else TARGET_MODELS

    files = collect_files(home, since, until)
    rows: list[dict] = []
    for path in files:
        try:
            found = scan_file(path)
        except OSError as e:
            print(f"（跳过读不了的文件 {path}：{e}）", file=sys.stderr)
            continue
        for r in found:
            if r["model"] not in models or not r["ts"]:
                continue
            t = parse_utc(r["ts"])
            if t > end:
                continue
            if start and t < start:
                continue
            r["_t"] = t.astimezone()
            rows.append(r)

    n_all = len(rows)
    bad = sorted((r for r in rows if r["label"] == "reasoned"), key=lambda r: r["_t"])

    win_txt = f"{since} ~ {until}" if since else f"全部历史 ~ {until}"
    print("════════ Codex 降智检测报告 ════════")
    print(f"窗口：{win_txt}（本地自然日，含首尾）｜模型：{', '.join(models)}｜扫描文件 {len(files)} 个")
    print(f"总回合 {n_all}｜降智 {len(bad)}｜降智率 {pct(len(bad), n_all)}")
    if n_all == 0:
        print("窗口内没有目标模型的回合。")
        return

    by_model: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_proj: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        hit = 1 if r["label"] == "reasoned" else 0
        by_model[r["model"]][0] += 1
        by_model[r["model"]][1] += hit
        d = r["_t"].strftime("%m-%d")
        by_day[d][0] += 1
        by_day[d][1] += hit
        by_proj[r["cwd"] or "（未记录）"][0] += 1
        by_proj[r["cwd"] or "（未记录）"][1] += hit

    print("\n按模型：")
    for m, (total, hit) in sorted(by_model.items()):
        print(f"  {m:<14} {total} 回合，降智 {hit}（{pct(hit, total)}）")

    print("\n按日：")
    print("  日期      回合   降智   率")
    for d in sorted(by_day):
        total, hit = by_day[d]
        print(f"  {d:<10}{total:>5}{hit:>7}   {pct(hit, total):>5}")

    warned = {p: v for p, v in by_proj.items() if v[1] > 0}
    if warned:
        print("\n按项目（有降智回合的项目预警，这些回合写出的代码按口径视为可能带 Bug）：")
        for p, (total, hit) in sorted(warned.items(), key=lambda kv: -kv[1][1]):
            print(f"  ⚠ {p}  {hit}/{total}（{pct(hit, total)}）")

    if bad:
        print("\n降智回合明细：")
        for r in bad:
            print(f"- {fmt_local(r['_t'])}｜北京 {fmt_bj(r['_t'])}｜{r['model']}｜会话 {r['session_id'][:8]}｜{r['cwd'] or '（未记录）'}")
            print(f"  {r['path']}")
    else:
        print("\n本窗口无降智回合。")


def main() -> None:
    args = parse_args()
    if args.session:
        run_session_mode(args)
    else:
        run_report_mode(args)


if __name__ == "__main__":
    main()
