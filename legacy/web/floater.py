"""桌面置顶悬浮窗：显示兑换清单。

由后端启动（单实例，端口互斥）：重复打开时只更新数据文件，
已有窗口每 800ms 检测数据变化并自动刷新。
"""
import json
import socket

import paths

BASE_DIR = paths.data_dir()
DATA_PATH = BASE_DIR / "floater_data.json"
LOCK_PORT = 45678

BG = "#0d1422"
PANEL = "#111827"
GOLD = "#e8b04b"
GREEN = "#5bd88a"
RED = "#ff9a9a"
FG = "#d7e1ef"
DIM = "#7d8ba1"


def fmt(n):
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    neg = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{neg}{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{neg}{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{neg}{v / 1e3:.1f}K"
    return f"{neg}{int(v):,}"


def main():
    # 单实例互斥：端口被占用说明已有窗口在运行，直接退出
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return

    import tkinter as tk

    root = tk.Tk()
    root.title("EVE LP 兑换清单")
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    root.resizable(False, False)

    W, H = 440, 420
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{sw - W - 24}+{sh - H - 64}")

    title_var = tk.StringVar(value="兑换清单")
    tk.Label(
        root, textvariable=title_var, bg=BG, fg=GOLD,
        font=("Microsoft YaHei", 11, "bold"), anchor="w",
    ).pack(fill="x", padx=12, pady=(10, 0))
    tk.Label(
        root, text="置顶悬浮窗 · 数据自动刷新 · 可拖动窗口边缘调整",
        bg=BG, fg=DIM, font=("Microsoft YaHei", 8), anchor="w",
    ).pack(fill="x", padx=12)

    txt = tk.Text(
        root, bg=PANEL, fg=FG, insertbackground=FG, bd=0,
        font=("Consolas", 10), padx=10, pady=8, relief="flat",
    )
    txt.tag_configure("dim", foreground=DIM)
    txt.tag_configure("gold", foreground=GOLD)
    txt.tag_configure("green", foreground=GREEN)
    txt.tag_configure("red", foreground=RED)
    txt.tag_configure("line", foreground="#3a465e")  # 条目间的浅色分割线
    txt.pack(fill="both", expand=True, padx=10, pady=8)
    txt.configure(state="disabled")

    tk.Button(
        root, text="✕  关 闭", command=root.destroy,
        bg="#5c1f1f", fg=RED, activebackground="#7a2626",
        activeforeground=RED, bd=0, relief="flat",
        font=("Microsoft YaHei", 10), cursor="hand2",
    ).pack(pady=(0, 10))

    last_mtime = None

    def load_data():
        nonlocal last_mtime
        if not DATA_PATH.exists():
            return
        mtime = DATA_PATH.stat().st_mtime
        if mtime == last_mtime:
            return
        last_mtime = mtime
        try:
            data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        title_var.set(data.get("title", "兑换清单"))
        rows = data.get("rows", [])
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", "物品", "gold")
        txt.insert("end", f"{'份':>6}{'数量':>12}{'净利润':>16}\n")
        txt.insert("end", "─" * 46 + "\n", "line")
        w_name = 20
        for i, r in enumerate(rows):
            if i > 0:
                txt.insert("end", "─" * 46 + "\n", "line")  # 分割线
            name = str(r.get("itemName", ""))
            disp = name if len(name) <= w_name else name[: w_name - 1] + "…"
            copies = int(r.get("copies", 1) or 1)
            qty = fmt(r.get("totalQuantity", 0))
            net = fmt(r.get("netProfit", r.get("profit", 0)))
            txt.insert("end", f"{disp:<20} {copies:>5} {qty:>12} {net:>15}\n")
        s_ = data.get("summary", {})
        txt.insert("end", "─" * 46 + "\n", "line")
        txt.insert("end", f"总 LP {fmt(s_.get('totalLp', 0)):>9}   ISK 投入 {fmt(s_.get('totalSpend', 0)):>13}\n")
        txt.insert("end", "净利润 ", "gold")
        net_total = fmt(s_.get("netProfit", s_.get("totalProfit", 0)))
        tag = "green" if not str(net_total).startswith("-") else "red"
        txt.insert("end", f"{net_total:>15}\n", tag)
        txt.configure(state="disabled")

    def poll():
        load_data()
        root.after(800, poll)

    load_data()
    root.after(800, poll)
    root.mainloop()
    try:
        s.close()
    except OSError:
        pass


if __name__ == "__main__":
    main()
