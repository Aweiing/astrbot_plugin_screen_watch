#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盯住一个窗口，画面有变化就写一条任务到 task/wait。

只管"变了没有"，不管变的是什么。写出的 json 交给事件桥，桥再把它投给我。

用法：
    python watch.py --window "Gay" --task qq --interval 3 --threshold 0.02
    python watch.py --list-windows          # 列出所有可见窗口标题
    python watch.py --window "Gay" --once   # 只看一次，打印当前差异，不写文件
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

TASK_DIR = Path(os.environ.get("ASTRBOT_TASK_DIR", "")) if os.environ.get("ASTRBOT_TASK_DIR") else None


def resolve_task_dir(explicit: str | None = None) -> Path:
    """确定任务目录：命令行 > 环境变量 > AstrBot 数据目录 > 当前目录下的 task。"""
    if explicit:
        return Path(explicit)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / "task"
    except Exception:
        pass
    for guess in (r"D:\AstrBot\data\task", str(Path.cwd() / "task")):
        if Path(guess).parent.exists():
            return Path(guess)
    return Path.cwd() / "task"


TASK_DIR = resolve_task_dir()
WAIT_DIR = TASK_DIR / "wait"

def say(msg: str) -> None:
    """输出到控制台。没有控制台时也不至于把进程搞崩。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


# ---------------- 窗口枚举 ----------------
def list_windows() -> list[tuple[int, str]]:
    result = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.strip():
            result.append((hwnd, buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    return result


def find_window(title: str) -> int | None:
    """按标题找窗口：先完全匹配，再子串匹配（取面积最大的那个）。"""
    wins = list_windows()
    for hwnd, t in wins:
        if t == title:
            return hwnd
    cands = [(hwnd, t) for hwnd, t in wins if title.lower() in t.lower()]
    if not cands:
        return None
    best, best_area = None, -1
    for hwnd, _ in cands:
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area > best_area:
            best, best_area = hwnd, area
    return best


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


# ---------------- 截图 ----------------
def grab_by_printwindow(hwnd: int):
    """PrintWindow：即便窗口被遮挡也能拿到画面。"""
    from PIL import Image

    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = user32.GetWindowDC(hwnd)
    mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mfc_dc, bmp)

    # 2 = PW_RENDERFULLCONTENT，能抓到部分 GPU 渲染的内容
    ok = user32.PrintWindow(hwnd, mfc_dc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h  # 负数表示自上而下
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfc_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    if not ok:
        return None
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


def grab_by_screen(hwnd: int):
    """兜底：直接截屏幕对应区域，窗口被挡住时拿到的是挡它的东西。"""
    from PIL import ImageGrab

    left, top, right, bottom = window_rect(hwnd)
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)


def capture(hwnd: int):
    img = None
    try:
        img = grab_by_printwindow(hwnd)
    except Exception:
        img = None
    if img is None or img.getbbox() is None:
        try:
            img = grab_by_screen(hwnd)
        except Exception:
            return None
    return img


# ---------------- 比对 ----------------
def change_ratio(img_a, img_b, pix_threshold: int = 25) -> float:
    """两张原尺寸图直接比：统计“差异超过阈值的像素”占全图多少。

    用 OpenCV 的绝对差 + 二值化，比“平均差”更直观：这就是“变了多少面积”。
    """
    try:
        import cv2
        import numpy as np

        a = cv2.cvtColor(np.array(img_a), cv2.COLOR_RGB2GRAY)
        b = cv2.cvtColor(np.array(img_b), cv2.COLOR_RGB2GRAY)
        if a.shape != b.shape:
            return 1.0
        d = cv2.absdiff(a, b)
        changed = int(np.count_nonzero(d > pix_threshold))
        return changed / float(d.size)
    except Exception:
        # 没有 opencv 时退回平均差，能用但没那么直观
        g_a = img_a.convert("L").tobytes()
        g_b = img_b.convert("L").tobytes()
        if len(g_a) != len(g_b):
            return 1.0
        total = sum(abs(x - y) for x, y in zip(g_a, g_b))
        return total / (255.0 * len(g_a))


# ---------------- 写任务 ----------------
def write_task(
    task: str,
    ratio: float,
    window: str,
    notify: str,
    extra: str = "",
    purpose: str = "",
) -> Path:
    WAIT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    name = f"{now.strftime('%Y%m%d_%H%M%S')}_{task}.json"
    payload = {
        "task": task,
        "text": f"窗口「{window}」有变化，约 {ratio:.1%}",
        "ratio": round(ratio, 4),
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "notify": notify,
    }
    if extra:
        payload["extra"] = extra
    if purpose:
        payload["purpose"] = purpose
    # 同一任务只保留最新一份：写之前把旧的清掉，队列永远不积压
    for old in WAIT_DIR.glob(f"*_{task}.json"):
        try:
            old.unlink()
        except Exception:
            pass

    path = WAIT_DIR / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def pid_file(task: str) -> Path:
    return TASK_DIR / f"watch_{task}.pid"


def read_state(task: str) -> dict | None:
    """读任务状态。兼容老格式（文件里只有一个进程号）。"""
    p = pid_file(task)
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return None
    # 老格式：纯进程号
    try:
        return {"task": task, "pid": int(raw), "status": "running"}
    except Exception:
        return None


def write_state(task: str, state: dict) -> None:
    p = pid_file(task)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def pid_alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        errors="ignore",
    ).stdout
    return str(pid) in out


def stop_task(task: str) -> bool:
    state = read_state(task)
    if not state:
        say(f"没有找到任务 {task} 的记录")
        return False
    pid = state.get("pid")
    if not pid:
        say(f"任务 {task} 的记录里没有 pid")
        return False
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    # 停完就删，不留墓碑，免得跟"还在跑"混在一起
    try:
        pid_file(task).unlink(missing_ok=True)
    except Exception:
        pass
    say(f"已停止任务 {task}（pid {pid}）")
    return True


def list_tasks() -> None:
    files = sorted(TASK_DIR.glob("watch_*.pid"))
    if not files:
        say("当前没有监听任务")
        return
    for f in files:
        name = f.stem.replace("watch_", "")
        st = read_state(name)
        if not st:
            say(f"{name:14} 记录损坏: {f.name}")
            continue
        pid = st.get("pid", 0)
        alive = pid_alive(pid) if pid else False
        if st.get("status") == "stopped" and not alive:
            flag = "已停止"
        elif alive:
            flag = "运行中"
        else:
            flag = "已退出"
        extra = []
        if st.get("purpose"):
            extra.append(f"用途={st['purpose']}")
        if st.get("window"):
            extra.append(f"窗口={st['window']}")
        if st.get("threshold") is not None:
            extra.append(f"阈值={st['threshold']}")
        if st.get("started_at"):
            extra.append(f"起于={st['started_at']}")
        if st.get("report_count"):
            extra.append(f"上报={st['report_count']}次")
        if st.get("last_change_at"):
            extra.append(f"最近={st['last_change_at']}")
        say(f"{name:14} pid={pid:<8} {flag:5} " + " ".join(extra))


def main() -> int:
    ap = argparse.ArgumentParser(description="窗口变化监听")
    ap.add_argument("--window", help="窗口标题（完全匹配优先，其次子串）")
    ap.add_argument("--task", default="", help="任务名，写进事件 json，默认取窗口标题")
    ap.add_argument("--interval", type=float, default=3.0, help="截图间隔秒，默认 3")
    ap.add_argument("--threshold", type=float, default=0.0, help="差异阈值，默认 0：只要有变化就写。想少打扰再给个 0.02 之类")
    ap.add_argument("--notify", default="auto", choices=["auto", "yes", "no"], help="写进事件的 notify")
    ap.add_argument("--cooldown", type=float, default=10.0, help="两次上报的最小间隔秒，默认 10")
    ap.add_argument("--pix-threshold", type=int, default=25, help="单像素差多少才算“变了”，默认 25")
    ap.add_argument("--extra", default="", help="附带的说明文字")
    ap.add_argument("--purpose", default="", help="必填（真正开监听时）：这个任务要干什么，桥会拿它当行动目标")
    ap.add_argument("--once", action="store_true", help="只检测一次，不写文件，打印差异")
    ap.add_argument("--list-windows", action="store_true", help="列出可见窗口")
    ap.add_argument("--list-tasks", action="store_true", help="列出在跑的监听任务")
    ap.add_argument("--stop", metavar="TASK", default=None, help="停止指定任务的监听进程")
    ap.add_argument("--task-dir", default=None, help="任务目录，默认自动探测 AstrBot 的 data/task")
    args = ap.parse_args()

    global WAIT_DIR
    WAIT_DIR = resolve_task_dir(args.task_dir) / "wait"
    globals()["TASK_DIR"] = resolve_task_dir(args.task_dir)

    if args.list_tasks:
        list_tasks()
        return 0
    if args.stop:
        return 0 if stop_task(args.stop) else 1

    if args.list_windows:
        for hwnd, title in list_windows():
            print(f"{hwnd}\t{title}")
        return 0

    if not args.window:
        print("需要 --window，或先用 --list-windows 看看有哪些窗口", file=sys.stderr)
        return 2

    hwnd = find_window(args.window)
    if not hwnd:
        print(f"找不到标题包含「{args.window}」的窗口", file=sys.stderr)
        return 3

    if not args.purpose.strip():
        say("缺少 --purpose：必须写清这个任务要干什么，否则不予启动", )
        return 2

    task = args.task or args.window
    say(f"[watch] 盯上窗口: {args.window} (hwnd={hwnd})，任务名 {task}")

    prev = None
    if args.once:
        img = capture(hwnd)
        if img is None:
            print("截不到画面", file=sys.stderr)
            return 4
        time.sleep(0.6)
        nxt = capture(hwnd) or img
        print(f"画面 {img.width}x{img.height}，前后两帧差异 {change_ratio(img, nxt):.2%}")
        return 0

    last_report = 0.0
    state = {
        "task": task,
        "pid": os.getpid(),
        "window": args.window,
        "hwnd": hwnd,
        "interval": args.interval,
        "threshold": args.threshold,
        "pix_threshold": args.pix_threshold,
        "cooldown": args.cooldown,
        "notify": args.notify,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": args.purpose,
        "status": "running",
        "report_count": 0,
        "last_change_at": "",
        "last_ratio": 0.0,
    }
    try:
        write_state(task, state)
    except Exception as e:
        say(f"[watch] 写状态文件失败: {e}")
    while True:
        try:
            img = capture(hwnd)
            if img is None:
                print("[watch] 截不到画面，稍后重试", flush=True)
                time.sleep(args.interval)
                continue
            if prev is not None:
                ratio = change_ratio(prev, img, args.pix_threshold)
                if ratio > args.threshold and (time.time() - last_report) >= args.cooldown:
                    path = write_task(
                        task, ratio, args.window, args.notify, args.extra, args.purpose
                    )
                    last_report = time.time()
                    state["report_count"] = int(state.get("report_count", 0)) + 1
                    state["last_change_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    state["last_ratio"] = round(ratio, 4)
                    try:
                        write_state(task, state)
                    except Exception:
                        pass
                    say(f"[watch] 变化 {ratio:.1%} -> {path.name}")
            prev = img
        except KeyboardInterrupt:
            say("[watch] 退出")
            return 0
        except Exception as e:
            say(f"[watch] 异常: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
