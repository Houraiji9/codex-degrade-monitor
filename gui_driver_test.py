"""无人 GUI 回归测试：真实启动界面（含系统托盘），验证启动首轮与手动检测。

用法：PYTHONIOENCODING=utf-8 python gui_driver_test.py
预期输出包含 tray: OK / on_done ok / ROUND_DONE / MANUAL_DONE，各耗时应 < 3s。
（退出码 124 属正常：驱动脚本未停托盘线程，由 timeout 收尾。）
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_watch_gui as g
import tkinter as tk

orig = g.App.on_done


def spy(self, res, auto):
    print("on_done enter bad=%s files=%s secs=%s" % (
        res.get("bad"), res.get("files"), res.get("secs")), flush=True)
    try:
        orig(self, res, auto)
        print("on_done ok", flush=True)
    except Exception:
        traceback.print_exc()
        print("ON_DONE_RAISED", flush=True)


g.App.on_done = spy

root = tk.Tk()
t0 = time.time()
app = g.App(root)
app.start_tray()
print("tray:", "OK" if app.tray is not None else "unavailable", flush=True)


def poll():
    if app.busy:
        if time.time() - t0 > 25:
            print("BUSY_STUCK after 25s", flush=True)
            root.destroy()
            return
        root.after(300, poll)
        return
    print("ROUND_DONE in %.2fs bad=%s secs=%s" % (
        time.time() - t0,
        None if app.last_result is None else app.last_result["bad"],
        None if app.last_result is None else app.last_result["secs"]), flush=True)
    t1 = time.time()
    app.manual_check()

    def poll2():
        if app.busy:
            if time.time() - t1 > 25:
                print("MANUAL_STUCK after 25s", flush=True)
                root.destroy()
                return
            root.after(300, poll2)
            return
        print("MANUAL_DONE in %.2fs" % (time.time() - t1), flush=True)
        root.after(200, root.destroy)

    root.after(300, poll2)


root.after(400, poll)
root.mainloop()
print("DRIVER_FINISHED busy=%s" % app.busy, flush=True)
