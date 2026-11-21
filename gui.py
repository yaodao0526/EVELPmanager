"""EVE LP 兑换规划器 —— Windows 桌面版（tkinter）。

单进程、无 HTTP 服务、无浏览器：数据直接用 starweb 的公开接口拉，
规划在本地跑 planner，界面是原生 ttk 控件。

数据流：
    势力 / 公司清单  ->  starweb.factions()          （启动时拉一次，缓存 1 小时）
    某公司 LP 商店   ->  starweb.offers()            （点「更新价格」时按公司拉）
    价格曲线 / 标记  ->  enrich.enrich()             （多 scope 采样，4 线程）
    兑换计划         ->  planner.build_plan()

本地文件（都在程序目录）：
    gui_state.json   本金、档位、高级参数、各公司 LP、LP 成本、排除项、存档计划
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import traceback
from datetime import datetime
from tkinter import messagebox, ttk

import enrich
import planner
import starweb
from paths import data_path

REGION_ID = 10000002          # 伏尔戈（The Forge）
APP_TITLE = "EVE LP 兑换规划器"
STATE_FILE = data_path("gui_state.json")

FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")
FONT_BIG = ("Microsoft YaHei UI", 16, "bold")

BG = "#f2f4f7"
CARD = "#ffffff"
LINE = "#d8dee6"
TEXT = "#1d2733"
DIM = "#6b7785"
ACCENT = "#1f6feb"
GOOD = "#17803d"
BAD = "#c0392b"

# 风格档位：只换「排序口径」，卖完天数已取消——LP 默认全部吃满
PRESETS = {
    "lp": {"label": "LP 优先", "sort": "lp",
           "max_lp_share": 40, "min_items": 3, "min_daily": 0.5},
    "balanced": {"label": "均衡", "sort": "balanced",
                 "max_lp_share": 25, "min_items": 5, "min_daily": 1.0},
    "speed": {"label": "速度优先", "sort": "speed",
              "max_lp_share": 25, "min_items": 8, "min_daily": 2.0},
}

PRESET_HINT = {
    "lp": "每个 LP 扣掉成本后赚最多（薄盘口可能吃不下全部 LP）",
    "speed": "优先大市场：每 LP 日成交额最高，出货最快",
    "balanced": "净利与市场吞吐各占一半",
}

DEFAULT_STATE = {
    "isk": 20.0,              # 亿 ISK
    "preset": "balanced",
    "max_lp_share": 25,
    "min_items": 5,
    "min_daily": 1.0,
    "impact": 10,
    "tax": 4.5,
    "broker": 2.5,
    "listing": False,
    "corps": {},
    "lp_costs": {},
    "lp_ignore_cost": True,
    "excluded": [],
    "plans": [],
}

RISK_LABEL = {
    "price_spike": "高于均价",
    "price_dip": "低于均价",
    "slow_sale": "出货慢",
    "tiny_market": "成交量小",
    "no_curve": "无曲线",
}


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
def fmt_money(v: float) -> str:
    v = float(v or 0)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}{v / 1e9:,.2f}B"
    if v >= 1e6:
        return f"{sign}{v / 1e6:,.1f}M"
    if v >= 1e3:
        return f"{sign}{v / 1e3:,.1f}K"
    return f"{sign}{v:,.0f}"


def fmt_int(v) -> str:
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return "-"


def num(text: str, default: float = 0.0) -> float:
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def risk_text(flags) -> str:
    return " ".join(RISK_LABEL.get(f.get("code", ""), f.get("label", "")) for f in (flags or []))


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = self._load_state()
        self.factions: list[dict] = []
        self.offers: dict[int, list] = {}      # corp_id -> [Offer]
        self.curves_info: dict = {}
        self.result: dict | None = None
        self.busy = False
        self._q: "queue.Queue" = queue.Queue()

        self._setup_style()
        self._build_ui()
        self._load_saved_lp_costs()
        self.root.after(80, self._startup)
        self.root.after(120, self._poll_queue)

    # ---------------- 状态 ---------------- #
    def _load_state(self) -> dict:
        st = dict(DEFAULT_STATE)
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))  # 记事本另存会带 BOM
            if isinstance(raw, dict):
                st.update({k: v for k, v in raw.items() if k in DEFAULT_STATE})
        except (OSError, ValueError):
            pass
        st["corps"] = {str(k): int(v) for k, v in (st.get("corps") or {}).items()}
        st["lp_costs"] = {str(k): float(v) for k, v in (st.get("lp_costs") or {}).items()}
        st["excluded"] = [int(x) for x in (st.get("excluded") or [])]
        if st.get("preset") not in PRESETS:      # 旧版本的 fast / full 档位已废弃
            st["preset"] = "balanced"
        return st

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as ex:
            self._status(f"保存设置失败：{ex}")

    def _load_saved_lp_costs(self) -> None:
        if self.state["lp_costs"]:
            return
        try:
            raw = json.loads(data_path("lp_costs.json").read_text(encoding="utf-8-sig"))
            self.state["lp_costs"] = {str(k): float(v) for k, v in raw.items()}
        except (OSError, ValueError):
            pass

    # ---------------- 样式 ---------------- #
    def _setup_style(self) -> None:
        self.root.title(APP_TITLE)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 锁屏/远程会话下 winfo_screenwidth 可能返回很小的值，给个下限免得窗口变成一条标题栏
        w, h = max(1280, sw), max(820, sh - 60)
        self.root.geometry(f"{w}x{h}+0+0")
        self.root.minsize(1040, 640)
        try:
            self.root.state("zoomed")          # 桌面软件习惯：直接最大化
        except tk.TclError:
            pass
        self.root.configure(bg=BG)

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            style.theme_use("clam")

        style.configure(".", font=FONT, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD, relief="solid", borderwidth=1)
        style.configure("Inner.TFrame", background=CARD)
        style.configure("Card2.TFrame", background=BG, relief="solid", borderwidth=1)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD)
        style.configure("Dim.TLabel", background=CARD, foreground=DIM, font=("Microsoft YaHei UI", 8))
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=FONT_B)
        style.configure("Big.TLabel", background=CARD, foreground=TEXT, font=FONT_BIG)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
        style.configure("Good.TLabel", background=CARD, foreground=GOOD, font=FONT_BIG)
        style.configure("TButton", padding=(10, 4))
        style.configure("Accent.TButton", padding=(14, 6), font=FONT_B)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 7), font=FONT)
        style.map("TNotebook.Tab", background=[("selected", CARD)])
        style.configure("Treeview", font=FONT, rowheight=24, fieldbackground=CARD, background=CARD)
        style.configure("Treeview.Heading", font=FONT_B, padding=(4, 5))
        style.configure("TRadiobutton", background=CARD)
        style.configure("TCheckbutton", background=CARD)
        style.configure("TLabelframe", background=CARD)
        style.configure("TLabelframe.Label", background=CARD, foreground=DIM, font=FONT)
        style.configure("Horizontal.TProgressbar", troughcolor=BG)

    # ---------------- 界面 ---------------- #
    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        ttk.Label(top, text="EVE LP 兑换规划器", style="Title.TLabel").pack(side="left")
        self.badge = tk.Label(
            top, text="  市场 伏尔戈 · 数据源 eve.starweb.cc  ",
            bg="#e8f0fe", fg=ACCENT, font=("Microsoft YaHei UI", 8), padx=8, pady=3,
        )
        self.badge.pack(side="left", padx=(12, 0))

        self.btn_plan = ttk.Button(top, text="开始规划", style="Accent.TButton", command=self.run_plan)
        self.btn_plan.pack(side="right")
        self.btn_refresh = ttk.Button(top, text="更新价格", command=self.refresh_prices)
        self.btn_refresh.pack(side="right", padx=(0, 8))
        self.lbl_time = ttk.Label(top, text="价格尚未拉取", foreground=DIM)
        self.lbl_time.pack(side="right", padx=(0, 16))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.tab_plan = ttk.Frame(self.nb, padding=(0, 8))
        self.tab_all = ttk.Frame(self.nb, padding=(0, 8))
        self.tab_saved = ttk.Frame(self.nb, padding=(0, 8))
        self.tab_cfg = ttk.Frame(self.nb, padding=(0, 8))
        self.nb.add(self.tab_plan, text="兑换规划")
        self.nb.add(self.tab_all, text="全部候选")
        self.nb.add(self.tab_saved, text="计划存档")
        self.nb.add(self.tab_cfg, text="设置")

        self._build_plan_tab()
        self._build_all_tab()
        self._build_saved_tab()
        self._build_cfg_tab()

        bar = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        bar.pack(fill="x")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.lbl_status = ttk.Label(bar, text="就绪", foreground=DIM)
        self.lbl_status.pack(side="left")
        self.progress.pack(side="left", padx=10)

    # ---- 兑换规划 ---- #
    def _build_plan_tab(self) -> None:
        body = ttk.Frame(self.tab_plan)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, width=302)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        # --- 本金 ---
        c1 = ttk.Frame(left, style="Card.TFrame", padding=10)
        c1.pack(fill="x")
        ttk.Label(c1, text="我的本金", style="CardTitle.TLabel").pack(anchor="w")
        row = ttk.Frame(c1, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.var_isk = tk.StringVar(value=f"{self.state['isk']:g}")
        ttk.Entry(row, textvariable=self.var_isk, width=12).pack(side="left")
        ttk.Label(row, text="亿 ISK", style="Card.TLabel").pack(side="left", padx=(6, 0))
        self.lbl_isk = ttk.Label(c1, text="", style="Dim.TLabel")
        self.lbl_isk.pack(anchor="w", pady=(4, 0))

        # --- 公司 LP ---
        c2 = ttk.Frame(left, style="Card.TFrame", padding=10)
        c2.pack(fill="both", expand=True, pady=(10, 0))
        head = ttk.Frame(c2, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="军团 LP", style="CardTitle.TLabel").pack(side="left")
        self.lbl_lp_total = ttk.Label(head, text="", style="Dim.TLabel")
        self.lbl_lp_total.pack(side="right")
        srow = ttk.Frame(c2, style="Card.TFrame")
        srow.pack(fill="x", pady=(6, 4))
        self.var_filter = tk.StringVar()
        ent = ttk.Entry(srow, textvariable=self.var_filter)
        ent.pack(fill="x")
        ent.bind("<KeyRelease>", lambda _e: self._fill_corps())
        btns = ttk.Frame(c2, style="Card.TFrame")
        btns.pack(fill="x", pady=(0, 6))
        ttk.Button(btns, text="清空 LP", width=8, command=self._clear_lp).pack(side="left")
        ttk.Button(btns, text="收起", width=5, command=self._collapse_all).pack(side="right")
        ttk.Button(btns, text="展开", width=5, command=self._expand_all).pack(side="right", padx=(0, 4))

        self.tree_corps = ttk.Treeview(
            c2, columns=("lp",), show="tree headings", height=11, selectmode="browse"
        )
        self.tree_corps.heading("#0", text="势力 / 军团")
        self.tree_corps.heading("lp", text="LP")
        self.tree_corps.column("#0", width=196, anchor="w")
        self.tree_corps.column("lp", width=76, anchor="e")
        self.tree_corps.tag_configure("fac", font=(FONT[0], FONT[1], "bold"))
        self.tree_corps.tag_configure("haslp", foreground=ACCENT)
        vs = ttk.Scrollbar(c2, orient="vertical", command=self.tree_corps.yview)
        self.tree_corps.configure(yscrollcommand=vs.set)
        self.tree_corps.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree_corps.bind("<Double-1>", self._edit_lp)
        ttk.Label(
            c2, text="双击军团填 LP；双击势力可折叠 / 展开", style="Dim.TLabel"
        ).pack(anchor="w", pady=(4, 0))

        # --- 当前设定摘要（详细参数在「设置」页） --- #
        c3 = ttk.Frame(left, style="Card.TFrame", padding=10)
        c3.pack(fill="x", pady=(10, 0))
        ttk.Label(c3, text="当前设定", style="CardTitle.TLabel").pack(anchor="w")
        self.lbl_summary_card = ttk.Label(c3, text="", style="Card.TLabel", justify="left")
        self.lbl_summary_card.pack(anchor="w", pady=(4, 0))
        ttk.Label(c3, text="档位 / 卖出方式 / 高级参数 →「设置」页", style="Dim.TLabel").pack(anchor="w")

        # --- 右侧概览 --- #
        hero = ttk.Frame(right, style="Card.TFrame", padding=12)
        hero.pack(fill="x")
        self.hero_vars = {}
        for i, (key, label) in enumerate(
            [("net", "净利润"), ("gross", "总利润"), ("lp", "用掉 LP"), ("spend", "投入 ISK")]
        ):
            cell = ttk.Frame(hero, style="Inner.TFrame")
            cell.grid(row=0, column=i, sticky="w", padx=(0, 30))
            ttk.Label(cell, text=label, style="Dim.TLabel").pack(anchor="w")
            v = ttk.Label(cell, text="—", style="Big.TLabel")
            v.pack(anchor="w")
            sub = ttk.Label(cell, text="", style="Dim.TLabel")
            sub.pack(anchor="w")
            self.hero_vars[key] = (v, sub)

        mini = ttk.Frame(right, style="Card.TFrame", padding=(12, 0, 12, 10))
        mini.pack(fill="x")
        self.mini_vars = {}
        for i, (key, label) in enumerate(
            [("bottleneck", "瓶颈"), ("shadow", "ISK 边际收益"), ("avg", "平均 ISK/LP"),
             ("perlp", "投入 ISK/LP"), ("thru", "每 LP 日成交额"), ("items", "商品数")]
        ):
            cell = ttk.Frame(mini, style="Inner.TFrame")
            cell.grid(row=0, column=i, sticky="w", padx=(0, 30))
            ttk.Label(cell, text=label, style="Dim.TLabel").pack(anchor="w")
            v = ttk.Label(cell, text="—", style="Card.TLabel")
            v.pack(anchor="w")
            self.mini_vars[key] = v

        self.lbl_hint = ttk.Label(
            right, text="先在左侧填 LP 和本金，点右上角「更新价格」再「开始规划」。",
            style="TLabel", foreground=DIM, wraplength=880, justify="left",
        )
        self.lbl_hint.pack(fill="x", pady=(8, 4))

        tw = ttk.Frame(right, style="Card.TFrame", padding=1)
        tw.pack(fill="both", expand=True, pady=(0, 0))
        cols = [
            ("corporationName", "公司", 86, "w"),
            ("itemName", "商品", 200, "w"),
            ("copies", "份数", 50, "e"),
            ("totalQuantity", "数量", 66, "e"),
            ("lpCost", "LP", 70, "e"),
            ("spend", "花费", 78, "e"),
            ("unitPrice", "现价", 70, "e"),
            ("netProfit", "净利润", 84, "e"),
            ("profit", "利润", 84, "e"),
            ("unitProfit", "ISK/LP", 62, "e"),
            ("velocity", "日销", 60, "e"),
            ("risk", "风险", 96, "w"),
        ]
        self.tree_plan = self._make_tree(tw, cols)
        self._pack_tree(tw, self.tree_plan)
        self.tree_plan.bind("<Button-3>", self._plan_menu)

    def _pack_tree(self, parent, tree) -> None:
        """给表格套上纵横两个滚动条。"""
        tree.configure(show="headings")
        vs = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        hs = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

    def _make_tree(self, parent, cols):
        tree = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings")
        for key, title, width, anchor in cols:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor=anchor, stretch=False)
        return tree

    # ---- 全部候选 ---- #
    def _build_all_tab(self) -> None:
        bar = ttk.Frame(self.tab_all)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="按「每 LP 边际收益」排序；右键可排除某个物品（跨公司生效）",
                  foreground=DIM).pack(side="left")
        ttk.Button(bar, text="恢复全部排除", command=self._restore_all).pack(side="right")

        wrap = ttk.Frame(self.tab_all, style="Card.TFrame", padding=1)
        wrap.pack(fill="both", expand=True)
        cols = [
            ("corporationName", "公司", 86, "w"),
            ("itemName", "商品", 220, "w"),
            ("state", "状态", 62, "w"),
            ("perCopyQuantity", "每份", 66, "e"),
            ("lpCost", "LP/份", 74, "e"),
            ("iskCost", "ISK/份", 88, "e"),
            ("unitPrice", "现价", 76, "e"),
            ("marginIsPerLp", "边际 ISK/LP", 92, "e"),
            ("dailyFlow", "日销额", 80, "e"),
            ("risk", "风险", 110, "w"),
        ]
        self.tree_all = self._make_tree(wrap, cols)
        self._pack_tree(wrap, self.tree_all)
        self.tree_all.bind("<Button-3>", self._all_menu)

    # ---- 计划存档 ---- #
    def _build_saved_tab(self) -> None:
        bar = ttk.Frame(self.tab_saved)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="保存当前计划", command=self._save_plan).pack(side="left")
        ttk.Button(bar, text="删除选中", command=self._delete_plan).pack(side="left", padx=8)
        ttk.Label(bar, text="计划存在 gui_state.json 里，换电脑复制整个程序目录即可",
                  foreground=DIM).pack(side="left", padx=12)

        wrap = ttk.Frame(self.tab_saved, style="Card.TFrame", padding=1)
        wrap.pack(fill="both", expand=True)
        cols = [
            ("ts", "时间", 130, "w"),
            ("label", "说明", 220, "w"),
            ("items", "商品数", 70, "e"),
            ("totalProfit", "总利润", 100, "e"),
            ("netProfit", "净利润", 100, "e"),
            ("totalLp", "用掉 LP", 100, "e"),
            ("totalSpend", "投入 ISK", 100, "e"),
            ("avgIsPerLp", "平均 ISK/LP", 100, "e"),
        ]
        self.tree_saved = self._make_tree(wrap, cols)
        self._pack_tree(wrap, self.tree_saved)

    # ---- 设置 ---- #
    def _build_settings_card(self, parent) -> None:
        """档位 + 卖出方式 + 高级参数（放在「设置」页）。"""
        c = ttk.Frame(parent, style="Card.TFrame", padding=12)
        c.pack(fill="x", pady=(0, 10))
        ttk.Label(c, text="交易设定", style="CardTitle.TLabel").pack(anchor="w")

        ttk.Label(c, text="风格档位", style="Dim.TLabel").pack(anchor="w", pady=(8, 0))
        self.var_preset = tk.StringVar(value=self.state["preset"])
        prow = ttk.Frame(c, style="Card.TFrame")
        prow.pack(fill="x")
        for key, meta in PRESETS.items():
            ttk.Radiobutton(
                prow, text=meta["label"], value=key, variable=self.var_preset,
                command=lambda k=key: self._apply_preset(k),
            ).pack(side="left", padx=(0, 16))
        self.lbl_preset = ttk.Label(c, text="", style="Dim.TLabel")
        self.lbl_preset.pack(anchor="w")

        ttk.Label(c, text="卖出方式", style="Dim.TLabel").pack(anchor="w", pady=(10, 0))
        self.var_listing = tk.StringVar(value="listing" if self.state["listing"] else "direct")
        lrow = ttk.Frame(c, style="Card.TFrame")
        lrow.pack(fill="x")
        ttk.Radiobutton(
            lrow, text="直接卖买单", value="direct", variable=self.var_listing, command=self._on_listing
        ).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(
            lrow, text="挂卖单", value="listing", variable=self.var_listing, command=self._on_listing
        ).pack(side="left")

        adv = ttk.Frame(c, style="Card.TFrame")
        adv.pack(fill="x", pady=(10, 0))
        self.var_adv = {}
        fields = [
            ("tax", "销售税 %", self.state["tax"]),
            ("broker", "经纪人费 %", self.state["broker"]),
            ("min_daily", "最低日销售额（亿/天）", self.state["min_daily"]),
            ("max_lp_share", "单品 LP 上限 %", self.state["max_lp_share"]),
            ("min_items", "最少品种数", self.state["min_items"]),
            ("impact", "无曲线时价格冲击 %", self.state["impact"]),
        ]
        for i, (key, label, val) in enumerate(fields):
            r, col = divmod(i, 4)
            cell = ttk.Frame(adv, style="Card.TFrame")
            cell.grid(row=r, column=col, sticky="w", padx=(0, 14), pady=4)
            ttk.Label(cell, text=label, style="Dim.TLabel").pack(anchor="w")
            var = tk.StringVar(value=f"{val:g}")
            e = ttk.Entry(cell, textvariable=var, width=12)
            e.pack(anchor="w")
            e.bind("<KeyRelease>", lambda _e: self._on_adv_change())
            self.var_adv[key] = var

        ttk.Label(
            c, text="改动会立刻存到本机；高级参数被改动时档位显示「自定义」。",
            style="Dim.TLabel",
        ).pack(anchor="w", pady=(10, 0))

    def _build_cfg_tab(self) -> None:
        self._build_settings_card(self.tab_cfg)

        ttk.Label(
            self.tab_cfg,
            text="LP 获取成本：把 1 点 LP 折算成 ISK（做任务的时间成本），只影响「净利润」怎么显示。"
                 "下面这个开关决定它要不要参与筛选。",
            foreground=DIM, wraplength=960, justify="left",
        ).pack(anchor="w", pady=(0, 6))

        self.var_ignore_cost = tk.BooleanVar(value=bool(self.state.get("lp_ignore_cost", True)))
        ttk.Checkbutton(
            self.tab_cfg,
            text="LP 视为已有（推荐）：按毛利挑选商品，尽量把 LP 用光；成本只用于折算净利润",
            variable=self.var_ignore_cost,
            command=self._toggle_ignore_cost,
        ).pack(anchor="w", pady=(0, 8))

        wrap = ttk.Frame(self.tab_cfg, style="Card.TFrame", padding=1)
        wrap.pack(fill="both", expand=True)
        cols = [("name", "军团 / 公司", 240, "w"), ("cost", "ISK / LP", 120, "e")]
        self.tree_cfg = self._make_tree(wrap, cols)
        self._pack_tree(wrap, self.tree_cfg)
        self.tree_cfg.bind("<Double-1>", self._edit_cost)

        bar = ttk.Frame(self.tab_cfg)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="添加军团", width=10, command=self._add_corp_cost).pack(side="left")
        ttk.Button(bar, text="移除选中", width=10, command=self._remove_cost).pack(side="left", padx=6)
        ttk.Button(bar, text="保存", width=8, command=self._save_costs).pack(side="left")
        ttk.Label(bar, text="双击填写价格；留空表示 0（不计成本）", foreground=DIM).pack(side="left", padx=12)

    # ------------------------------------------------------------------ #
    # 启动 / 后台任务
    # ------------------------------------------------------------------ #
    def _startup(self) -> None:
        self._refresh_corp_list()
        self._fill_corps()
        self._fill_cfg()
        self._render_saved()
        self._apply_preset(self.state["preset"], save=False)
        self._load_offers_if_any()
        self._status("正在拉取军团列表…")
        self._run_bg(self._fetch_factions, self._on_factions)

    def _load_offers_if_any(self) -> None:
        corps = {int(k): int(v) for k, v in self.state["corps"].items() if int(v) > 0}
        if corps:
            self._status(f"上次有 {len(corps)} 个军团的 LP 记录，点「更新价格」即可开算")
            self.lbl_time.configure(text="价格尚未拉取")

    def _run_bg(self, fn, on_done=None, retry: int = 0) -> None:
        """后台线程跑 fn，结果放进队列，由主线程轮询取回（tkinter 不允许跨线程调用）。"""
        if self.busy:
            if retry < 40:                    # 最多等 40 秒再试，避免点击被静默丢弃
                self._status("上一个任务还在跑，稍后自动继续…")
                self.root.after(1000, lambda: self._run_bg(fn, on_done, retry + 1))
            else:
                self._status("上一个任务太久没结束，请重试")
            return
        self.busy = True
        self._set_buttons(False)
        self.progress.start(12)

        def worker():
            try:
                out, err = fn(), None
            except Exception as ex:  # noqa: BLE001
                out, err = None, ex
                traceback.print_exc()
            self._q.put((out, err, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                out, err, on_done = self._q.get_nowait()
                self._bg_done(out, err, on_done)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _bg_done(self, out, err, on_done) -> None:
        self.busy = False
        self.progress.stop()
        self._set_buttons(True)
        if err is not None:
            self._status(f"出错：{err}")
            messagebox.showerror(APP_TITLE, f"{err}")
            return
        if on_done:
            on_done(out)

    def _set_buttons(self, enabled: bool) -> None:
        st = "normal" if enabled else "disabled"
        self.btn_refresh.configure(state=st)
        self.btn_plan.configure(state=st)

    def _status(self, text: str) -> None:
        self.lbl_status.configure(text=text)

    # ---- 拉军团列表 ---- #
    def _fetch_factions(self):
        return starweb.factions()

    def _on_factions(self, data) -> None:
        self.factions = data or []
        self._refresh_corp_list()
        self._fill_corps()
        self._fill_cfg()
        self._status("军团列表已就绪；填好 LP 后点「更新价格」")

    def _refresh_corp_list(self) -> None:
        self.corp_meta = {}
        for fac in self.factions:
            for c in fac.get("Corporations") or []:
                cid = int(c.get("CorporationId") or 0)
                if cid:
                    self.corp_meta[cid] = (str(c.get("CorporationName") or ""), str(fac.get("FactionName") or ""))

    def _fill_corps(self) -> None:
        """按「势力 → 军团」两级展示；填写了 LP 的势力默认展开。"""
        kw = self.var_filter.get().strip().lower()
        opened = {iid for iid in self.tree_corps.get_children("") if self.tree_corps.item(iid, "open")}
        first = not opened
        self.tree_corps.delete(*self.tree_corps.get_children())

        for fi, fac in enumerate(self.factions or []):
            fname = str(fac.get("FactionName") or "")
            corps = fac.get("Corporations") or []
            rows = []
            for c in corps:
                cid = int(c.get("CorporationId") or 0)
                if not cid:
                    continue
                name = str(c.get("CorporationName") or cid)
                if kw and kw not in fname.lower() and kw not in name.lower():
                    continue
                rows.append((cid, name, int(self.state["corps"].get(str(cid), 0))))
            if not rows:
                continue
            fac_lp = sum(r[2] for r in rows)
            fid = f"f{fi}"
            is_open = (fid in opened) or (first and fac_lp > 0) or bool(kw)
            self.tree_corps.insert(
                "", "end", iid=fid, text=fname,
                values=(fmt_int(fac_lp) if fac_lp else ""),
                open=bool(is_open), tags=("fac",),
            )
            rows.sort(key=lambda x: (-x[2], x[1]))
            for cid, name, lp in rows:
                self.tree_corps.insert(
                    fid, "end", iid=f"c{cid}", text="   " + name,
                    values=(fmt_int(lp) if lp else ""),
                    tags=("haslp",) if lp else (),
                )
        total = sum(int(v) for v in self.state["corps"].values())
        self.lbl_lp_total.configure(text=f"合计 {fmt_int(total)} LP" if total else "还没填 LP")

    def _clear_lp(self) -> None:
        if not messagebox.askyesno(APP_TITLE, "清空所有军团的 LP？"):
            return
        self.state["corps"] = {}
        self._save_state()
        self._fill_corps()

    def _expand_all(self) -> None:
        for iid in self.tree_corps.get_children(""):
            self.tree_corps.item(iid, open=True)

    def _collapse_all(self) -> None:
        for iid in self.tree_corps.get_children(""):
            self.tree_corps.item(iid, open=False)

    def _edit_lp(self, event) -> None:
        row = self.tree_corps.identify_row(event.y)
        if not row:
            return
        if row.startswith("f"):  # 势力行：折叠 / 展开
            self.tree_corps.item(row, open=not self.tree_corps.item(row, "open"))
            return
        self._prompt_lp(int(row[1:]))

    def _prompt_lp(self, cid: int) -> None:
        name, _ = getattr(self, "corp_meta", {}).get(cid, (str(cid), ""))
        cur = self.state["corps"].get(str(cid), 0)
        from tkinter import simpledialog

        got = simpledialog.askinteger(APP_TITLE, f"{name}\n填写这个军团现有的 LP：", initialvalue=int(cur), minvalue=0)
        if got is None:
            return
        if got > 0:
            self.state["corps"][str(cid)] = int(got)
        else:
            self.state["corps"].pop(str(cid), None)
        self._save_state()
        self._fill_corps()

    # ------------------------------------------------------------------ #
    # 价格
    # ------------------------------------------------------------------ #
    def _current_corps(self) -> dict[int, int]:
        return {int(k): int(v) for k, v in self.state["corps"].items() if int(v) > 0}

    def refresh_prices(self) -> None:
        corps = self._current_corps()
        if not corps:
            messagebox.showinfo(APP_TITLE, "先在左侧给至少一个军团填上 LP。")
            return
        if self.busy:
            return
        self._read_inputs()
        self._status(f"正在拉取 {len(corps)} 个军团的报价与价格曲线…")
        self.lbl_time.configure(text="拉取中…")

        def work():
            out = {}
            info = {}
            # 挂卖单 = 按卖单价成交；直接卖买单 = 按买单价成交。两边差得很远（薄盘口尤其明显）
            basis = "sell" if self.state["listing"] else "buy"
            for cid in corps:
                name = getattr(self, "corp_meta", {}).get(cid, (str(cid), ""))[0]
                data = starweb.offers(
                    REGION_ID, cid, days=7, tax=self.state["tax"],
                    product_price=basis, material_price="sell",
                )
                out[cid] = [planner.Offer.from_api(d, cid, name, days=7) for d in (data or [])]
            all_offers = [o for lst in out.values() for o in lst]
            if all_offers:
                info = enrich.enrich(
                    all_offers, region_id=REGION_ID, days=7, tax=self.state["tax"],
                    product_price=basis, material_price="sell",
                )
            return out, info, basis

        self._run_bg(work, self._on_prices)

    def _on_prices(self, payload) -> None:
        out, info, basis = payload
        self.offers = out
        self.offers_basis = basis
        self.curves_info = info or {}
        stamp = datetime.now().strftime("%H:%M:%S")
        n_curve = self.curves_info.get("curves", 0)
        n_off = self.curves_info.get("offers", 0)
        mode = "卖单价" if basis == "sell" else "买单价"
        self.lbl_time.configure(
            text=f"价格更新于 {stamp} · {mode} · {n_off} 个物品 · {n_curve} 个有价格曲线"
        )
        region = self.curves_info.get("regionName") or "伏尔戈"
        self.badge.configure(text=f"  市场 {region} · 数据源 eve.starweb.cc  ")
        self._status("价格已更新，点「开始规划」出方案")
        errs = self.curves_info.get("errors") or []
        if errs:
            self._status("部分价格曲线拉取失败：" + errs[0][:60])

    # ------------------------------------------------------------------ #
    # 规划
    # ------------------------------------------------------------------ #
    def _read_inputs(self) -> None:
        self.state["isk"] = num(self.var_isk.get(), self.state["isk"])
        self.state["tax"] = num(self.var_adv["tax"].get(), 4.5)
        self.state["broker"] = num(self.var_adv["broker"].get(), 2.5)
        self.state["min_daily"] = num(self.var_adv["min_daily"].get(), 0.5)
        self.state["max_lp_share"] = num(self.var_adv["max_lp_share"].get(), 25)
        self.state["min_items"] = int(num(self.var_adv["min_items"].get(), 5) or 5)
        self.state["impact"] = num(self.var_adv["impact"].get(), 10)
        self.state["listing"] = self.var_listing.get() == "listing"

    def run_plan(self) -> None:
        if self.busy:
            return
        corps = self._current_corps()
        if not corps:
            messagebox.showinfo(APP_TITLE, "先在左侧给至少一个军团填上 LP。")
            return
        missing = [c for c in corps if c not in self.offers]
        if missing:
            if messagebox.askyesno(APP_TITLE, "还有军团的报价没拉取，先更新价格？"):
                self.refresh_prices()
            return
        self._read_inputs()
        self._save_state()
        offers = [o for lst in self.offers.values() for o in lst]
        cfg = dict(
            market_share=1.0,  # 100% 吃下日成交量
            sort_mode=PRESETS.get(self.state["preset"], {}).get("sort", "lp"),
            broker_fee=self.state["broker"] / 100.0,
            listing=self.state["listing"],
            min_daily_sales=self.state["min_daily"] * 1e8,
            exclude_item_ids=self.state["excluded"],
            max_lp_share=self.state["max_lp_share"] / 100.0,
            min_items=self.state["min_items"],
            impact=self.state["impact"] / 100.0,
            lp_unit_costs={int(k): float(v) for k, v in self.state["lp_costs"].items()},
            lp_cost_in_ranking=not self.state.get("lp_ignore_cost", True),
        )
        isk = self.state["isk"] * 1e8
        self._status("正在求解…")

        def work():
            return planner.build_plan(offers, corps, isk, **cfg)

        self._run_bg(work, self._on_plan)

    def _on_plan(self, result) -> None:
        self.result = result
        rows = result.get("rows") or []
        summary = result.get("summary")
        if not rows or not summary:
            self._status("没算出方案：可能本金/ LP 太少，或过滤条件太严")
            self._clear_result()
            return
        self._render_summary(summary)
        self._render_plan(rows)
        self._render_candidates(result.get("candidates") or [])
        self._render_hints(result)
        self._status(f"完成：{len(rows)} 个商品，用时 {datetime.now().strftime('%H:%M:%S')}")

    def _clear_result(self) -> None:
        for v, sub in self.hero_vars.values():
            v.configure(text="—")
            sub.configure(text="")
        for v in self.mini_vars.values():
            v.configure(text="—")
        self.tree_plan.delete(*self.tree_plan.get_children())

    def _render_summary(self, s: dict) -> None:
        net = s.get("profitAfterLpCost", 0.0)
        gross = s.get("totalProfit", 0.0)
        self.hero_vars["net"][0].configure(text=fmt_money(net))
        self.hero_vars["net"][1].configure(text=f"扣 LP 成本 {fmt_money(s.get('lpUnitCostTotal', 0))}")
        self.hero_vars["gross"][0].configure(text=fmt_money(gross))
        self.hero_vars["gross"][1].configure(
            text="已扣销售税" + ("、经纪人费" if self.state["listing"] else "")
        )
        self.hero_vars["lp"][0].configure(text=fmt_int(s.get("totalLp", 0)))
        self.hero_vars["lp"][1].configure(text=f"已用 {s.get('lpUsedRatio', 0) * 100:.1f}%")
        self.hero_vars["spend"][0].configure(text=fmt_money(s.get("totalSpend", 0)))
        self.hero_vars["spend"][1].configure(text=f"已用 {s.get('iskUsedRatio', 0) * 100:.1f}% 本金")

        bn = {"ISK": "卡 ISK", "LP": "卡 LP", "BOTH": "两边都满", "NONE": "都够"}.get(
            s.get("bottleneck"), "—"
        )
        self.mini_vars["bottleneck"].configure(text=bn)
        lam = s.get("iskShadowPrice", 0) or 0
        self.mini_vars["shadow"].configure(
            text=f"{fmt_money(lam * 1e8)} / 亿" if lam > 0 else "充足"
        )
        self.mini_vars["avg"].configure(text=fmt_int(s.get("avgIsPerLp", 0)))
        self.mini_vars["perlp"].configure(text=fmt_int(s.get("avgSpendPerLp", 0)))
        self.mini_vars["thru"].configure(text=fmt_money(s.get("avgThroughputPerLp", 0)))
        self.mini_vars["items"].configure(text=str(s.get("items", 0)))

        isk = self.state["isk"]
        self.lbl_isk.configure(text=f"{isk:g} 亿 ISK 可用")

    def _render_plan(self, rows: list[dict]) -> None:
        self.tree_plan.delete(*self.tree_plan.get_children())
        self._plan_rows = rows
        for idx, r in enumerate(rows):
            self.tree_plan.insert(
                "", "end", iid=str(idx),
                values=(
                    r.get("corporationName", ""),
                    r.get("itemName", ""),
                    fmt_int(r.get("copies", 0)),
                    fmt_int(r.get("totalQuantity", 0)),
                    fmt_int(r.get("lpCost", 0)),
                    fmt_money(r.get("spend", 0)),
                    fmt_int(r.get("unitPrice", 0)),
                    fmt_money(r.get("netProfit", r.get("profit", 0))),
                    fmt_money(r.get("profit", 0)),
                    fmt_int(r.get("unitProfit", 0)),
                    fmt_int(r.get("velocity", 0)),
                    risk_text(r.get("flags")),
                ),
            )

    def _render_candidates(self, cands: list[dict]) -> None:
        self.tree_all.delete(*self.tree_all.get_children())
        for c in cands:
            state = "已选中" if c.get("chosen") else ("已排除" if c.get("excluded") else "未入选")
            self.tree_all.insert(
                "", "end",
                values=(
                    c.get("corporationName", ""),
                    c.get("itemName", ""),
                    state,
                    fmt_int(c.get("perCopyQuantity", 0)),
                    fmt_int(c.get("lpCost", 0)),
                    fmt_money(c.get("iskCost", 0)),
                    fmt_int(c.get("unitPrice", 0)),
                    fmt_int(c.get("marginIsPerLp", 0)),
                    fmt_money(c.get("dailyFlow", 0)),
                    risk_text(c.get("flags")),
                ),
            )

    def _render_hints(self, result: dict) -> None:
        s = result.get("summary") or {}
        hints = list(s.get("warnings") or [])
        diag = result.get("diagnostics") or {}
        hints.extend(diag.get("hints") or [])
        self.lbl_hint.configure(text="\n".join(hints[:4]) if hints else "方案看起来没毛病。")

    def _plan_menu(self, event) -> None:
        """右键计划表某一行：弹出「剔除并重算」。"""
        iid = self.tree_plan.identify_row(event.y)
        if not iid:
            return
        self.tree_plan.selection_set(iid)      # 先选中，视觉上确认操作对象
        rows = getattr(self, "_plan_rows", [])
        try:
            row = rows[int(iid)]
        except (ValueError, IndexError):
            return
        item_id = int(row.get("itemId") or 0)
        name = row.get("itemName", "")

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(
            label=f"剔除「{name}」并重算",
            command=lambda: self._set_excluded(item_id, True),
        )
        if self.state.get("excluded"):
            menu.add_separator()
            menu.add_command(
                label=f"恢复全部被剔除的商品（{len(self.state['excluded'])} 个）",
                command=self._restore_all,
            )
        try:
            menu.tk_popup(event.x_root + 2, event.y_root + 2)
        finally:
            menu.grab_release()

    def _all_menu(self, event) -> None:
        iid = self.tree_all.identify_row(event.y)
        if not iid:
            return
        idx = self.tree_all.index(iid)
        cands = (self.result or {}).get("candidates") or []
        if idx >= len(cands):
            return
        cand = cands[idx]
        menu = tk.Menu(self.root, tearoff=0)
        item_id = int(cand.get("itemId") or 0)
        if cand.get("excluded"):
            menu.add_command(
                label=f"恢复「{cand.get('itemName')}」",
                command=lambda: self._set_excluded(item_id, False),
            )
        else:
            menu.add_command(
                label=f"排除「{cand.get('itemName')}」",
                command=lambda: self._set_excluded(item_id, True),
            )
        menu.tk_popup(event.x_root, event.y_root)

    def _set_excluded(self, item_id: int, excluded: bool) -> None:
        cur = set(int(x) for x in self.state["excluded"])
        if excluded:
            cur.add(item_id)
        else:
            cur.discard(item_id)
        self.state["excluded"] = sorted(cur)
        self._save_state()
        if self.result:
            self._replan()

    def _restore_all(self) -> None:
        self.state["excluded"] = []
        self._save_state()
        if self.result:
            self._replan()

    def _replan(self, tries: int = 0) -> None:
        """剔除/恢复后自动重算：把腾出来的 LP 与 ISK 重新分配。"""
        if self.busy:
            if tries < 40:                      # 最多等 24 秒
                self._status("正在求解，稍后自动重算…")
                self.root.after(600, lambda: self._replan(tries + 1))
            return
        n = len(self.state.get("excluded") or [])
        self._status(f"已剔除 {n} 个商品，正在重新分配 LP / ISK…" if n else "已恢复全部商品，正在重算…")
        self.run_plan()

    # ------------------------------------------------------------------ #
    # 档位 / 成本 / 存档
    # ------------------------------------------------------------------ #
    def _update_summary_card(self) -> None:
        """左侧「当前设定」摘要，随档位 / 卖出方式 / 税费变化。"""
        if not hasattr(self, "lbl_summary_card"):
            return
        meta = PRESETS.get(self.state["preset"], {})
        label = meta.get("label", "自定义")
        listing = "挂卖单" if self.state["listing"] else "直接卖买单"
        self.lbl_summary_card.configure(
            text=(
                f"{label} · 吃满全部 LP" + "\n"
                + f"{listing} · 税 {self.state['tax']:g}% / 经纪费 {self.state['broker']:g}%"
            )
        )

    def _apply_preset(self, key: str, save: bool = True) -> None:
        meta = PRESETS.get(key)
        if not meta:
            return
        self.var_preset.set(key)
        self.state["preset"] = key
        self.state["max_lp_share"] = meta["max_lp_share"]
        self.state["min_items"] = meta["min_items"]
        self.state["min_daily"] = meta["min_daily"]
        for k in ("max_lp_share", "min_items", "min_daily"):
            self.var_adv[k].set(f"{self.state[k]:g}")
        self.lbl_preset.configure(text=f"{meta['label']}：{PRESET_HINT[meta['sort']]}")
        self._update_summary_card()
        if save:
            self._save_state()

    def _on_adv_change(self) -> None:
        self._read_inputs()
        cur = self.state["preset"]
        meta = PRESETS.get(cur, {})
        if meta and (
            self.state["max_lp_share"] != meta["max_lp_share"]
            or self.state["min_items"] != meta["min_items"]
            or self.state["min_daily"] != meta["min_daily"]
        ):
            self.lbl_preset.configure(text="自定义（高级参数已改动）")
        self.lbl_isk.configure(text=f"{self.state['isk']:g} 亿 ISK 可用")
        self._update_summary_card()

    def _on_listing(self) -> None:
        was = self.state["listing"]
        self.state["listing"] = self.var_listing.get() == "listing"
        self._update_summary_card()
        self._save_state()
        if was != self.state["listing"]:
            # 买卖价差很大：切换后旧报价作废，必须按新的价格口径重拉
            self.offers = {}
            self._status(
                f"已切换为{'挂卖单（按卖单价）' if self.state['listing'] else '直接卖买单（按买单价）'}，"
                "点「更新价格」按新口径重拉报价"
            )

    def _fill_cfg(self) -> None:
        self.tree_cfg.delete(*self.tree_cfg.get_children())
        costs = self.state.get("lp_costs") or {}
        ids = {int(k) for k in costs} | set(self._current_corps())
        meta = getattr(self, "corp_meta", {})

        def sort_key(cid: int):
            name, fac = meta.get(cid, (str(cid), ""))
            return (fac, name)

        for cid in sorted(ids, key=sort_key):
            name, fac = meta.get(cid, (str(cid), ""))
            cost = costs.get(str(cid))
            label = f"{name}（{fac}）" if fac else name
            self.tree_cfg.insert(
                "", "end", iid=str(cid), values=(label, f"{cost:g}" if cost else "")
            )

    def _add_corp_cost(self) -> None:
        """弹窗里按「势力 / 军团」搜索，选一个军团加进 LP 成本表。"""
        if not getattr(self, "corp_meta", None):
            messagebox.showinfo(APP_TITLE, "军团列表还没加载完，稍等一下再试")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("添加军团")
        dlg.transient(self.root)
        dlg.geometry("640x560")
        dlg.configure(bg=BG)
        dlg.grab_set()

        top = ttk.Frame(dlg, padding=(12, 10, 12, 0))
        top.pack(fill="x")
        ttk.Label(top, text="搜索势力 / 军团名称", foreground=DIM).pack(anchor="w")
        var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=var)
        ent.pack(fill="x", pady=(4, 0))

        wrap = ttk.Frame(dlg, style="Card.TFrame", padding=1)
        wrap.pack(fill="both", expand=True, padx=12, pady=8)
        cols = [
            ("fac", "势力", 150, "w"),
            ("corp", "军团 / 公司", 300, "w"),
            ("cost", "已设成本", 100, "e"),
        ]
        tree = self._make_tree(wrap, cols)
        self._pack_tree(wrap, tree)

        def fill(*_a) -> None:
            kw = var.get().strip().lower()
            tree.delete(*tree.get_children())
            rows = []
            for cid, (name, fac) in self.corp_meta.items():
                if kw and kw not in name.lower() and kw not in fac.lower():
                    continue
                rows.append((fac, name, cid))
            for fac, name, cid in sorted(rows):
                cost = self.state["lp_costs"].get(str(cid))
                tree.insert("", "end", iid=str(cid), values=(fac, name, f"{cost:g}" if cost else ""))

        var.trace_add("write", fill)
        fill()

        bar = ttk.Frame(dlg, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        self._add_result = None

        def ok(*_a) -> None:
            sel = tree.selection()
            if sel:
                self._add_result = int(sel[0])
                dlg.destroy()

        ttk.Button(bar, text="添加", width=10, command=ok).pack(side="right")
        ttk.Button(bar, text="取消", width=10, command=dlg.destroy).pack(side="right", padx=(0, 6))
        ttk.Label(bar, text="双击一行也可以直接添加", foreground=DIM).pack(side="left")
        tree.bind("<Double-1>", ok)
        ent.focus_set()

        self.root.wait_window(dlg)
        cid = self._add_result
        if not cid:
            return
        self.state["lp_costs"].setdefault(str(cid), 0.0)
        self._save_state()
        self._fill_cfg()
        self.tree_cfg.selection_set(str(cid))
        self.tree_cfg.see(str(cid))
        self._prompt_cost(cid)

    def _toggle_ignore_cost(self) -> None:
        self.state["lp_ignore_cost"] = bool(self.var_ignore_cost.get())
        self._save_state()
        self._status(
            "已切换：LP 视为已有，按毛利挑选（LP 会用得更满）"
            if self.state["lp_ignore_cost"]
            else "已切换：LP 成本参与筛选（每 LP 赚不过成本就不做）"
        )

    def _remove_cost(self) -> None:
        sel = self.tree_cfg.selection()
        if not sel:
            return
        meta = getattr(self, "corp_meta", {})
        names = [meta.get(int(r), (r, ""))[0] for r in sel]
        if not messagebox.askyesno(APP_TITLE, "从 LP 成本表里移除：\n" + "\n".join(names) + "\n\n（填了 LP 的军团仍会保留，只是成本清空）"):
            return
        for r in sel:
            self.state["lp_costs"].pop(str(r), None)
        self._save_state()
        self._fill_cfg()
        self._status("已移除 %d 个军团的成本" % len(sel))

    def _edit_cost(self, event) -> None:
        row = self.tree_cfg.identify_row(event.y)
        if row:
            self._prompt_cost(int(row))

    def _prompt_cost(self, cid: int) -> None:
        from tkinter import simpledialog

        name = getattr(self, "corp_meta", {}).get(cid, (str(cid), ""))[0]
        cur = self.state["lp_costs"].get(str(cid), 0)
        got = simpledialog.askfloat(
            APP_TITLE, f"{name}\n1 点 LP 折算多少 ISK？", initialvalue=float(cur), minvalue=0.0
        )
        if got is None:
            return
        if got > 0:
            self.state["lp_costs"][str(cid)] = float(got)
        else:
            self.state["lp_costs"].pop(str(cid), None)
        self._save_state()
        self._fill_cfg()

    def _save_costs(self) -> None:
        self._save_state()
        try:
            data_path("lp_costs.json").write_text(
                json.dumps({k: v for k, v in self.state["lp_costs"].items() if v > 0},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        self._status("LP 成本已保存")
        if self.result:
            self.run_plan()

    def _save_plan(self) -> None:
        if not self.result or not self.result.get("rows"):
            messagebox.showinfo(APP_TITLE, "先算出一份计划再保存。")
            return
        s = self.result["summary"]
        label = datetime.now().strftime("%m-%d %H:%M")
        self.state["plans"].append({
            "ts": label,
            "items": s.get("items", 0),
            "totalProfit": s.get("totalProfit", 0),
            "netProfit": s.get("profitAfterLpCost", 0),
            "totalLp": s.get("totalLp", 0),
            "totalSpend": s.get("totalSpend", 0),
            "avgIsPerLp": s.get("avgIsPerLp", 0),
            "isk": self.state["isk"],
            "preset": self.state["preset"],
        })
        self._save_state()
        self._render_saved()
        self._status("计划已存档")

    def _render_saved(self) -> None:
        self.tree_saved.delete(*self.tree_saved.get_children())
        for i, p in enumerate(self.state.get("plans") or []):
            self.tree_saved.insert(
                "", "end", iid=str(i),
                values=(
                    p.get("ts", ""),
                    f"{PRESETS.get(p.get('preset'), {}).get('label', '自定义')} · 本金 {p.get('isk', 0):g} 亿",
                    p.get("items", 0),
                    fmt_money(p.get("totalProfit", 0)),
                    fmt_money(p.get("netProfit", 0)),
                    fmt_int(p.get("totalLp", 0)),
                    fmt_money(p.get("totalSpend", 0)),
                    fmt_int(p.get("avgIsPerLp", 0)),
                ),
            )

    def _delete_plan(self) -> None:
        sel = self.tree_saved.selection()
        if not sel:
            return
        idx = int(sel[0])
        plans = self.state.get("plans") or []
        if 0 <= idx < len(plans):
            if not messagebox.askyesno(APP_TITLE, "删除这条存档？"):
                return
            plans.pop(idx)
            self._save_state()
            self._render_saved()


def _single_instance_guard() -> "socket.socket | None":
    """端口占位式单实例：重复双击时只提示，不再开第二个窗口抢 gui_state.json。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47615))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def main() -> None:
    guard = _single_instance_guard()
    if guard is None:
        hidden = tk.Tk()
        hidden.withdraw()
        messagebox.showinfo(APP_TITLE, "程序已经在运行了，切到任务栏看一下吧")
        hidden.destroy()
        return

    root = tk.Tk()
    try:
        from paths import resource_path

        ico = resource_path("assets", "eve-lp.ico")
        if not ico.exists():
            ico = data_path("assets") / "eve-lp.ico"
        if ico.exists():
            root.iconbitmap(str(ico))
    except (tk.TclError, OSError):
        pass
    try:
        App(root)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        messagebox.showerror(APP_TITLE, "启动失败，详情见 logs\\gui.log")
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
