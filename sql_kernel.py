"""Tkinter 可视化工作台：SQL 输入、结果、执行计划、库内数据浏览。

启动：python sql_kernel.py [数据目录]（默认 .kernel_db，空库自动灌样板）
"""

import sys
import time
import tkinter as tk
from tkinter import ttk

from kernel_exec import *  # noqa: F401,F403  引擎、样板数据、核心类型

# ========== 可视化工作台 ==========
BG = "#EEF2F4"
PANEL = "#FFFFFF"
INK = "#16202A"
INK2 = "#3A4A57"
LINE = "#CBD6DD"
ACCENT = "#638BD6"
ACCENT_DEEP = "#2B4E7E"
MUTED = "#6B7B87"
SUCCESS = "#2E7D32"
FAIL = "#B4602F"
CODE = "#101A24"
MONO = ("Cascadia Code", 10)
MONO_LG = ("Cascadia Code", 11)
SANS = ("Microsoft YaHei UI", 10)
SANS_SM = ("Microsoft YaHei UI", 9)
SANS_BOLD = ("Microsoft YaHei UI", 10, "bold")
MAX_BROWSE_ROWS = 500
def plan_text(sql, engine):
    """只绑不定，把计划树取出来给「执行计划」页签用。"""
    stmt = Parser(sql).parse_statement()
    query = stmt
    if not isinstance(query, Query): return None
    return engine.binder.bind_query(query).plan.tree()
def table_rows(engine, table_key, limit=MAX_BROWSE_ROWS):
    table = engine.catalog.tables.get(table_key)
    heap = engine.heaps.get(table_key)
    if table is None or heap is None: return None
    rows = [row for _rid, row in heap.scan()]
    return [c.name for c in table.columns], rows[:limit], len(rows), heap.page_count
class Workbench(tk.Tk):
    def __init__(self, engine, db_path):
        super().__init__()
        self.engine = engine
        self.title("MYSQL · 可视化工作台")
        self.geometry("1240x800")
        self.minsize(980, 620)
        self.configure(bg=BG)
        self._style()
        self._build()
        self._refresh_tables()
        self.stat("已连接 %s" % db_path)
    def _style(self):
        st = ttk.Style(self)
        try: st.theme_use("clam")
        except tk.TclError: pass
        st.configure(".", background=BG, foreground=INK, font=SANS)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=SANS_SM)
        st.configure("TButton", background="#F7FAFB", foreground=INK, bordercolor=LINE,
                     focusthickness=0, padding=(10, 5))
        st.map("TButton", background=[("active", "#E6EEF8")])
        st.configure("Go.TButton", background=ACCENT_DEEP, foreground="#FFFFFF")
        st.map("Go.TButton", background=[("active", ACCENT)])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background="#DFE7EC", foreground=INK2, padding=(18, 7))
        st.map("TNotebook.Tab", background=[("selected", PANEL)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=INK,
                     rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background="#F1F5F8", foreground=MUTED, font=SANS_SM)
    def _build(self):
        head = tk.Frame(self, bg=CODE, height=52)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="MYSQL", bg=CODE, fg="#FFFFFF", font=MONO_LG).pack(side="left", padx=(16, 8))
        tk.Label(head, text="可视化工作台", bg=CODE, fg="#8FA6B8", font=SANS_SM).pack(side="left")
        self.stat_label = tk.Label(head, text="", bg=CODE, fg="#8FA6B8", font=SANS_SM)
        self.stat_label.pack(side="right", padx=14)
        main = tk.PanedWindow(self, orient="horizontal", bg=BG, sashwidth=6, sashrelief="flat", bd=0)
        main.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        left = tk.Frame(main, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        right = tk.Frame(main, bg=BG)
        main.add(left, minsize=360, width=470)
        main.add(right, minsize=520)
        tk.Label(left, text="SQL", bg=PANEL, fg=ACCENT_DEEP, font=MONO_LG, anchor="w").pack(
            fill="x", padx=12, pady=(10, 4))
        self.sql_text = tk.Text(left, wrap="word", bg=PANEL, fg=INK, font=MONO_LG, bd=0,
                                insertbackground=ACCENT_DEEP, padx=10, pady=8, undo=True)
        self.sql_text.pack(fill="both", expand=True, padx=8)
        self.sql_text.insert("1.0", "SELECT * FROM Students WHERE Sdept = 'CS';")
        self.sql_text.bind("<Control-Return>", lambda e: (self.run_sql(), "break")[1])
        bar = tk.Frame(left, bg=PANEL)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="执行  Ctrl+Enter", style="Go.TButton", command=self.run_sql).pack(side="left")
        ttk.Button(bar, text="解释计划", command=self.run_explain).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=lambda: self.sql_text.delete("1.0", "end")).pack(side="right")
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self._build_result_tab()
        self._build_plan_tab()
        self._build_data_tab()
        foot = tk.Frame(self, bg="#F1F5F8")
        foot.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(foot, textvariable=self.status_var, bg="#F1F5F8", fg=INK2, font=SANS_SM,
                 anchor="w", padx=12, pady=5).pack(fill="x")
    def _tree(self, parent):
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        tree = ttk.Treeview(wrap, show="headings", selectmode="browse")
        tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(wrap, orient="vertical", command=tree.yview).grid(row=0, column=1, sticky="ns")
        ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview).grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        return tree
    def _build_result_tab(self):
        frame = tk.Frame(self.nb, bg=PANEL)
        self.nb.add(frame, text="  结果  ")
        head = tk.Frame(frame, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(10, 0))
        self.res_title = tk.Label(head, text="尚未执行", bg=PANEL, fg=ACCENT_DEEP,
                                  font=SANS_BOLD, anchor="w")
        self.res_title.pack(side="left")
        self.res_state = tk.Label(head, text="—", bg=PANEL, fg=MUTED, font=SANS_BOLD)
        self.res_state.pack(side="right")
        self.res_meta = tk.Label(head, text="", bg=PANEL, fg=MUTED, font=MONO)
        self.res_meta.pack(side="right", padx=(0, 12))
        self.result_tree = self._tree(frame)
        self.msg_line = tk.Label(frame, text="", bg="#F1F5F8", fg=INK2, font=SANS_SM,
                                 anchor="w", padx=12, pady=6)
        self.msg_line.pack(fill="x", side="bottom")
    def _build_plan_tab(self):
        frame = tk.Frame(self.nb, bg=PANEL)
        self.nb.add(frame, text="  执行计划  ")
        head = tk.Frame(frame, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(head, text="执行计划", bg=PANEL, fg=ACCENT_DEEP, font=SANS_BOLD).pack(side="left")
        tk.Label(head, text="根节点在上，缩进即子节点", bg=PANEL, fg=MUTED,
                 font=SANS_SM).pack(side="right")
        self.plan_view = tk.Text(frame, wrap="word", bg=PANEL, fg=INK, font=MONO_LG, bd=0,
                                 padx=12, pady=10, state="disabled")
        self.plan_view.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    def _build_data_tab(self):
        frame = tk.Frame(self.nb, bg=PANEL)
        self.nb.add(frame, text="  数据  ")
        head = tk.Frame(frame, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(head, text="表", bg=PANEL, fg=MUTED, font=SANS_SM).pack(side="left")
        self.table_box = ttk.Combobox(head, state="readonly", width=28)
        self.table_box.pack(side="left", padx=6)
        self.table_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_data())
        ttk.Button(head, text="刷新", command=self._refresh_tables).pack(side="left")
        self.data_info = tk.Label(head, text="", bg=PANEL, fg=MUTED, font=SANS_SM)
        self.data_info.pack(side="right")
        self.data_tree = self._tree(frame)
    def stat(self, text):
        self.status_var.set(text)
    def _refresh_tables(self):
        keys = sorted(self.engine.catalog.tables)
        self.table_box.configure(values=keys)
        if keys and not self.table_box.get(): self.table_box.set(keys[0])
        self.refresh_data()
    def refresh_data(self):
        self.data_tree.delete(*self.data_tree.get_children())
        key = self.table_box.get()
        if not key:
            self.data_info.configure(text="库里还没有表")
            return
        data = table_rows(self.engine, key)
        if data is None:
            self.data_info.configure(text="这张表不在了，点刷新")
            return
        cols, rows, total, pages = data
        ids = ["c%d" % i for i in range(len(cols))]
        self.data_tree.configure(columns=ids)
        for i, name in enumerate(cols):
            self.data_tree.heading(ids[i], text=name)
            self.data_tree.column(ids[i], width=max(90, min(220, 14 * len(name) + 40)),
                                  anchor="w", stretch=True)
        for row in rows: self.data_tree.insert("", "end", values=[format_value(v) for v in row])
        shown = "%d 行" % total if len(rows) >= total else "显示 %d / %d 行" % (len(rows), total)
        self.data_info.configure(text="%s · %d 页 · %s" % (key, pages, shown))
    def run_explain(self):
        sql = self.sql_text.get("1.0", "end").strip().rstrip(";")
        if not sql: return
        try: lines = plan_text(sql, self.engine)
        except SQLError as exc:
            self.show_plan("无法生成计划：%s" % exc)
            self.stat("解释失败：%s" % exc)
            self.nb.select(1)
            return
        self.show_plan("\n".join(lines) if lines else "这条语句不是查询，没有执行计划。")
        self.stat("计划已生成 · %d 个节点" % len(lines or []))
        self.nb.select(1)
    def run_sql(self):
        statements = split_statements(self.sql_text.get("1.0", "end"))
        if not statements:
            self.stat("没有可执行的语句")
            return
        done, total, last = 0, 0.0, None
        for sql in statements:
            ok, result, elapsed = self._execute_one(sql)
            total += elapsed
            done += 1
            if not ok: break
            last = result
        self._refresh_tables()
        if last is not None: self.show_result(last, total)
        if last is not None and done == len(statements): self.stat("成功 · %d 条语句 · 用时 %.1f ms" % (done, total))
        elif last is not None: self.stat("失败 · 第 %d 条出错" % done)
        self.nb.select(0)
    def _execute_one(self, sql):
        start = time.perf_counter()
        try: result = self.engine.execute(sql)
        except SQLError as exc:
            self.show_error(str(exc), (time.perf_counter() - start) * 1000)
            return False, None, (time.perf_counter() - start) * 1000
        except Exception as exc:            # 一条语句出错不该把窗口带崩
            self.show_error("%s: %s" % (type(exc).__name__, exc),
                            (time.perf_counter() - start) * 1000)
            return False, None, (time.perf_counter() - start) * 1000
        return True, result, (time.perf_counter() - start) * 1000
    def _fill(self, tree, columns, rows):
        tree.delete(*tree.get_children())
        ids = ["c%d" % i for i in range(len(columns))]
        tree.configure(columns=ids)
        for i, name in enumerate(columns):
            tree.heading(ids[i], text=name)
            tree.column(ids[i], width=max(90, min(240, 14 * len(name) + 40)), anchor="w", stretch=True)
        for row in rows: tree.insert("", "end", values=[format_value(v) for v in row])
    def show_result(self, result, elapsed):
        cols, rows = result.columns or [], result.rows or []
        if cols: self._fill(self.result_tree, cols, rows)
        else: self._fill(self.result_tree, ["结果"], [])
        self.res_title.configure(text="查询结果" if cols else "执行结果")
        self.res_state.configure(text="成功", fg=SUCCESS)
        self.res_meta.configure(text="用时 %.1f ms · %d 行" % (elapsed, len(rows)) if cols
                                else "用时 %.1f ms" % elapsed)
        self.msg_line.configure(text=result.message or "")
    def show_error(self, message, elapsed):
        self._fill(self.result_tree, ["错误"], [(message,)])
        self.res_title.configure(text="执行失败")
        self.res_state.configure(text="失败", fg=FAIL)
        self.res_meta.configure(text="用时 %.1f ms" % elapsed)
        self.msg_line.configure(text=message)
        self.show_plan("这条语句没有通过，没有生成执行计划。")
        self.stat("失败 · %s" % message)
    def show_plan(self, text):
        self.plan_view.configure(state="normal")
        self.plan_view.delete("1.0", "end")
        self.plan_view.insert("1.0", text or "")
        self.plan_view.configure(state="disabled")
def launch(db_path, auto_demo=True):
    engine = Engine(db_path)
    if auto_demo and not engine.catalog.tables: load_demo(engine)
    Workbench(engine, str(db_path)).mainloop()
if __name__ == "__main__": launch(sys.argv[1] if len(sys.argv) > 1 else ".gui_db")