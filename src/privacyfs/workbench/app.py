"""Tk desktop workbench. Native inference remains in ScanController's worker."""
from importlib import metadata, util
from pathlib import Path
from datetime import datetime
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from ..gui.backend import ScanOptions, display_text, DEFAULT_AI_CONTEXT, MAX_AI_CONTEXT
from .model import WorkbenchController

PAGE_SIZE = 80
STATE_LABELS = {"pending":"待复核", "confirmed":"确认隐私", "false_positive":"误报", "defer":"待定"}
SCAN_LABELS = {"running":"正在检查", "cancelling":"正在停止", "complete":"检查完成", "partial":"部分完成",
               "cancelled":"已取消", "failed":"检查失败"}
ERROR_LABELS = {"ai_dependency_missing":"缺少或无法加载本地推理依赖。请安装项目 local-ai 依赖。",
    "ntfs_journal_gap":"预览期间的变更日志已不完整，无法安全复用索引。请重新加载目录后再核验。",
    "ai_model_file_invalid":"请选择可读取的本地 GGUF 模型。", "ai_model_load_failed":"模型不兼容或可用内存不足。",
    "ai_worker_timeout":"模型操作超时。可减少每批数量、调整超时或换用较小模型。",
    "worker_exit":"工作进程异常退出，请检查运行库、模型兼容性与可用内存。",
    "coverage":"有条目未完成检查，请查看未检查、访问失败和排除统计。",
    "result_storage_failed":"临时结果存储失败，请检查磁盘空间和权限。",
    "ntfs_access_denied":"NTFS 卷读取权限不足。请关闭后通过 PrivacyFS-Workbench-Admin.vbs 启动，再扫描；也可手动选择普通目录遍历。",
    "ntfs_scope_not_volume":"快速扫描需要本地 NTFS 盘符路径并开启递归；网络路径请手动选择普通遍历。",
    "ntfs_not_ntfs":"所选路径不是 NTFS 卷，请手动选择普通目录遍历。",
    "ntfs_scope_redirected":"路径经过链接或挂载重定向，不能按当前盘符安全枚举；请选择实际路径。",
    "ntfs_journal_unavailable":"USN 日志不可用；本工具不会创建日志。可手动选择普通目录遍历。"}


class Workbench:
    def __init__(self, show=True):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("PrivacyFS 工作台 · 本地目录隐私检查")
        width = max(760, min(1180, self.root.winfo_screenwidth()-80))
        height = max(480, min(820, self.root.winfo_screenheight()-100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(760, 480)
        self.controller = WorkbenchController()
        self.page, self.visible, self.selected = 0, {}, None
        self.filter_pending = self.note_dirty = self.rendering = False
        self.after_save = None
        self._closed = False
        self._tick_id = None
        self._was_running = False
        self.config_widgets, self.review_buttons = [], []
        self.inventory_widgets = []
        self.root_path = tk.StringVar(value=str(Path.home()))
        self.model_path = tk.StringVar()
        self.recursive = tk.BooleanVar(value=True)
        self.excludes = tk.BooleanVar(value=True)
        self.enumeration = tk.StringVar(value="NTFS 快速扫描（失败停止）")
        self.batch = tk.StringVar(value="32")
        self.context = tk.StringVar(value=str(DEFAULT_AI_CONTEXT))
        self.threads = tk.StringVar(value=str(min(4, os.cpu_count() or 4)))
        self.budget = tk.StringVar(value="6000")
        self.timeout = tk.StringVar(value="300")
        self.query = tk.StringVar()
        self.type_filter = tk.StringVar(value="全部类型")
        self.state_filter = tk.StringVar(value="全部候选")
        self.category_filter = tk.StringVar(value="全部类别")
        self.export_format = tk.StringVar(value="JSON（保留原值）")
        self.export_scope = tk.StringVar(value="全部候选及复核状态")
        self.message = tk.StringVar(value="选择目录与本地 GGUF，按步骤完成检查、复核和导出。")
        self.status = tk.StringVar(value="准备就绪")
        self.phase = tk.StringVar(value="模型在独立工作进程中加载，无需服务器。")
        self.coverage = tk.StringVar(value="只检查名称及相对目录关系，不读取文档正文。")
        self.review_summary = tk.StringVar()
        self.saved_state = tk.StringVar(value="尚未开始")
        self.page_text = tk.StringVar(value="暂无候选")
        self.preflight_text = tk.StringVar(value="环境检查不会加载模型，也不开始扫描。")
        self.metrics = {key:tk.StringVar(value="0") for key in ("visited", "checked", "unchecked", "hits")}
        self._style()
        self._build()
        self.root.report_callback_exception = lambda *args: self.message.set("操作未完成，请检查输入或当前状态；已有文件未被覆盖。")
        self.root.protocol("WM_DELETE_WINDOW", self.close_request)
        self.root.bind("<Control-s>", lambda event: self.save())
        self.root.bind("<Control-o>", lambda event: self.open())
        self.root.bind("<Control-z>", lambda event: self.undo())
        self.refresh()
        if show:
            self.root.deiconify()
        self._tick_id = self.root.after(100, self.tick)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(background="#f2f5f7")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f2f5f7", foreground="#203a48")
        style.configure("TButton", padding=(12, 7))
        style.configure("Primary.TButton", background="#146b62", foreground="white", padding=(15, 8))
        style.map("Primary.TButton", background=[("active", "#1e8074"), ("disabled", "#a5b9b6")])
        style.configure("TNotebook.Tab", padding=(18, 9))
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=32, borderwidth=0)
        style.configure("Treeview.Heading", padding=7, font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#d8ece7")], foreground=[("selected", "#183d35")])
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Metric.TLabel", font=("Microsoft YaHei UI", 23, "bold"), foreground="#146b62")
        style.configure("Muted.TLabel", foreground="#627785")

    def _button(self, parent, text, command, primary=False):
        return ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton")

    def _build(self):
        shell = ttk.Frame(self.root, padding=16)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x")
        ttk.Label(header, text="PrivacyFS 工作台", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="本地模型 · 目录上下文 · 人工复核", style="Muted.TLabel").pack(side="left", padx=18)
        toolbar = ttk.Frame(shell)
        toolbar.pack(fill="x", pady=(12, 10))
        self.start_button = self._button(toolbar, "加载目录结构", self.start, True)
        self.ai_button = self._button(toolbar, "开始 AI 审阅", self.begin_review, True)
        self.stop_button = self._button(toolbar, "停止检查", self.stop)
        self.open_button = self._button(toolbar, "打开会话", self.open)
        self.save_button = self._button(toolbar, "保存会话", self.save)
        for button in (self.start_button, self.ai_button, self.stop_button, self.open_button, self.save_button):
            button.pack(side="left", padx=(0, 7))
        ttk.Label(toolbar, textvariable=self.saved_state, style="Muted.TLabel").pack(side="right")
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True)
        self.pages = [ttk.Frame(self.tabs, padding=14) for _ in range(4)]
        for frame, title in zip(self.pages, ("1  配置", "2  目录结构与检查", "3  复核", "4  导出")):
            self.tabs.add(frame, text=title)
        self._setup_page()
        self._scan_page()
        self._review_page()
        self._export_page()
        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.message, wraplength=850, style="Muted.TLabel").pack(side="left", fill="x", expand=True)
        self.cancel_io = self._button(footer, "取消文件操作", self.cancel_file_job)
        self.cancel_io.pack(side="right", padx=(8, 0))

    def _setup_page(self):
        outer = self.pages[0]
        canvas = tk.Canvas(outer, highlightthickness=0, background="#f2f5f7")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        form.columnconfigure(1, weight=1)
        for index, (label, variable, kind) in enumerate((
            ("检查目录", self.root_path, "directory"), ("本地 GGUF", self.model_path, "model"))):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky="w", padx=(0, 12), pady=8)
            entry = ttk.Entry(form, textvariable=variable)
            entry.grid(row=index, column=1, sticky="ew", pady=8)
            button = self._button(form, "选择…", lambda kind=kind: self.browse(kind))
            button.grid(row=index, column=2, padx=(8, 0))
            self.config_widgets.extend([entry, button])
            if kind == "directory":
                self.inventory_widgets.extend([entry, button])
        switches = ttk.Frame(form)
        switches.grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        for label, variable in (("递归检查子目录", self.recursive), ("使用默认排除项", self.excludes)):
            widget = ttk.Checkbutton(switches, text=label, variable=variable)
            widget.pack(side="left", padx=(0, 18))
            self.config_widgets.append(widget)
            self.inventory_widgets.append(widget)
        drive = self._button(switches, "选择 C 盘", self.choose_drive)
        drive.pack(side="left")
        self.config_widgets.append(drive)
        self.inventory_widgets.append(drive)
        settings = ttk.LabelFrame(form, text="推理与分批", padding=14)
        settings.grid(row=3, column=0, columnspan=3, sticky="ew", pady=14)
        fields = [("每批最多条目", self.batch, 1, 128), ("CPU 线程", self.threads, 1, 64),
                  ("上下文长度", self.context, 2048, MAX_AI_CONTEXT), ("清单字节预算", self.budget, 512, 32768),
                  ("单次操作超时（秒）", self.timeout, 5, 3600)]
        for index, (label, variable, low, high) in enumerate(fields):
            line, col = divmod(index, 2)
            ttk.Label(settings, text=label).grid(row=line, column=col*2, sticky="w", padx=(0, 12), pady=8)
            spin = ttk.Spinbox(settings, textvariable=variable, from_=low, to=high, width=10)
            spin.grid(row=line, column=col*2+1, sticky="w", padx=(0, 24))
            self.config_widgets.append(spin)
        ttk.Label(settings, text="文件列表来源").grid(row=2, column=2, sticky="w", padx=(0,12), pady=8)
        enumeration = ttk.Combobox(settings, textvariable=self.enumeration, state="readonly",
            values=["NTFS 快速扫描（失败停止）", "自动（卷根优先 NTFS）", "普通目录遍历"], width=23)
        enumeration.grid(row=2, column=3, sticky="w", padx=(0,24))
        self.config_widgets.append(enumeration)
        self.inventory_widgets.append(enumeration)
        ttk.Label(form, text="小目录整批检查；较长清单按目录边界和长度分批。模型每轮只加载一次。\n排除项、无权限目录及未返回判断的条目会单独统计。", wraplength=740,
                  style="Muted.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=8)
        preflight = self._button(form, "检查运行环境", self.preflight)
        preflight.grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        self.config_widgets.append(preflight)
        ttk.Label(form, textvariable=self.preflight_text, wraplength=740, justify="left").grid(row=6, column=0, columnspan=3, sticky="w", pady=8)

    def _scan_page(self):
        page = self.pages[1]
        ttk.Label(page, textvariable=self.status, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(page, textvariable=self.phase, wraplength=900).pack(anchor="w", pady=12)
        self.progress = ttk.Progressbar(page, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 18))
        metrics = ttk.Frame(page)
        metrics.pack(fill="x")
        self.metric_cards = {}
        for column, (key, title) in enumerate((("visited","已遍历"),("checked","已有判断"),("unchecked","尚未检查"),("hits","候选条目"))):
            metrics.columnconfigure(column, weight=1)
            card = ttk.LabelFrame(metrics, text=title, padding=12)
            self.metric_cards[key] = card
            card.grid(row=0, column=column, sticky="ew", padx=(0, 9))
            ttk.Label(card, textvariable=self.metrics[key], style="Metric.TLabel").pack(anchor="w")
        ttk.Label(page, textvariable=self.coverage, wraplength=900, justify="left").pack(anchor="w", pady=20)
        ttk.Label(page, text="“完成”表示已取得所声明范围的判断，不代表隐私认证。模型结果需要人工复核。",
                  style="Muted.TLabel", wraplength=900).pack(anchor="w", pady=8)
        self._button(page, "查看候选并复核 →", lambda: self.tabs.select(2), True).pack(anchor="w", pady=4)
        self.folder, self.folder_page, self.structure_rows = ".", 0, {}
        self.structure_path = tk.StringVar(value="目录结构尚未加载")
        nav = ttk.Frame(page)
        nav.pack(fill="x")
        self._button(nav, "上一级", self.folder_up).pack(side="left")
        self._button(nav, "上一页", lambda: self.folder_move(-1)).pack(side="left")
        self._button(nav, "下一页", lambda: self.folder_move(1)).pack(side="left")
        ttk.Label(nav, textvariable=self.structure_path).pack(side="left", padx=8)
        self.structure = ttk.Treeview(page, columns=("state", "reason"), show="tree headings", height=6)
        self.structure.heading("#0", text="名称（双击目录展开）")
        self.structure.heading("state", text="AI 标记")
        self.structure.heading("reason", text="依据")
        self.structure.column("#0", width=280)
        self.structure.column("state", width=130)
        self.structure.column("reason", width=380)
        self.structure.tag_configure("marked", foreground="#9c301b", background="#fff0e6")
        scroll = ttk.Scrollbar(page, orient="vertical", command=self.structure.yview)
        self.structure.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.structure.pack(fill="both", expand=True)
        self.structure.bind("<Double-1>", self.folder_open)

    def _review_page(self):
        page = self.pages[2]
        page.rowconfigure(2, weight=1)
        page.columnconfigure(0, weight=1)
        ttk.Label(page, textvariable=self.review_summary).grid(row=0, column=0, sticky="w", pady=(0, 10))
        filters = ttk.Frame(page)
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        filters.columnconfigure(0, weight=1)
        search = ttk.Entry(filters, textvariable=self.query)
        search.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        search.bind("<KeyRelease>", lambda event: self.filter_changed())
        for column, (variable, values, width) in enumerate((
            (self.type_filter, ["全部类型", "仅文件", "仅目录"], 9),
            (self.state_filter, ["全部候选", *STATE_LABELS.values()], 11),
            (self.category_filter, ["全部类别", "人物", "联系方式", "机构", "身份与敏感词", "AI待复核"], 12)), 1):
            combo = ttk.Combobox(filters, textvariable=variable, values=values, state="readonly", width=width)
            combo.grid(row=0, column=column, padx=(0, 6))
            combo.bind("<<ComboboxSelected>>", lambda event: self.filter_changed())
        body = ttk.Panedwindow(page, orient="horizontal")
        body.grid(row=2, column=0, sticky="nsew")
        table_frame, detail = ttk.Frame(body), ttk.LabelFrame(body, text="当前条目", padding=10)
        body.add(table_frame, weight=3)
        body.add(detail, weight=2)
        self.tree = ttk.Treeview(table_frame, columns=("name", "review", "category"), show="headings", selectmode="browse")
        for key, title, width in (("name","名称 / 相对路径",300),("review","人工复核",92),("category","类别",94)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=70, stretch=key == "name")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.detail = tk.Text(detail, height=8, width=30, wrap="word", relief="flat", font=("Microsoft YaHei UI", 10), state="disabled")
        self.detail.pack(fill="both", expand=True)
        ttk.Label(detail, text="复核备注（最多 2000 字）").pack(anchor="w", pady=(8, 4))
        self.note = tk.Text(detail, height=3, width=30, wrap="word", relief="solid", borderwidth=1, font=("Microsoft YaHei UI", 10))
        self.note.pack(fill="x")
        self.note.bind("<<Modified>>", self.note_changed)
        actions = ttk.Frame(detail)
        actions.pack(fill="x", pady=(8, 0))
        for index, state in enumerate(("confirmed", "false_positive", "defer", "pending")):
            button = self._button(actions, STATE_LABELS[state], lambda state=state: self.mark(state))
            button.grid(row=index//2, column=index%2, sticky="ew", padx=2, pady=2)
            actions.columnconfigure(index%2, weight=1)
            self.review_buttons.append(button)
        save_note = self._button(detail, "保存备注", self.flush_note)
        save_note.pack(fill="x", pady=(6, 0))
        self.review_buttons.append(save_note)
        pager = ttk.Frame(page)
        pager.grid(row=3, column=0, sticky="ew", pady=8)
        ttk.Label(pager, textvariable=self.page_text, style="Muted.TLabel").pack(side="left")
        self.next_button = self._button(pager, "下一页", lambda: self.change_page(1))
        self.prev_button = self._button(pager, "上一页", lambda: self.change_page(-1))
        self.next_button.pack(side="right")
        self.prev_button.pack(side="right", padx=6)
        batchbar = ttk.Frame(page)
        batchbar.grid(row=4, column=0, sticky="ew")
        self.bulk_button = self._button(batchbar, "本页标为误报", self.mark_page_false)
        self.bulk_button.pack(side="left")
        self.undo_button = self._button(batchbar, "撤销最近复核", self.undo)
        self.undo_button.pack(side="left", padx=8)
        self._button(batchbar, "打开所在目录", self.reveal).pack(side="right")

    def _export_page(self):
        page = self.pages[3]
        ttk.Label(page, text="保存工作进度，或生成复核报告", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=8)
        ttk.Label(page, textvariable=self.review_summary).pack(anchor="w", pady=8)
        ttk.Label(page, text="会话文件用于重新打开、继续复核；Windows 下使用当前账户 DPAPI 保护。\nJSON / CSV 是含真实路径的私有报告，不是可以直接公开的脱敏文件。",
                  wraplength=880, justify="left", style="Muted.TLabel").pack(anchor="w", pady=12)
        self._button(page, "保存可继续复核的会话…", self.save, True).pack(anchor="w", pady=10)
        options = ttk.LabelFrame(page, text="报告导出", padding=16)
        options.pack(fill="x", pady=14)
        for row_index, (label, variable, values) in enumerate((
            ("文件格式", self.export_format, ["JSON（保留原值）", "CSV（表格查看）"]),
            ("导出范围", self.export_scope, ["全部候选及复核状态", "仅人工确认隐私的条目"]))):
            ttk.Label(options, text=label).grid(row=row_index, column=0, padx=(0, 16), pady=8)
            ttk.Combobox(options, textvariable=variable, values=values, state="readonly", width=32).grid(row=row_index, column=1, sticky="w")
        self.export_button = self._button(options, "导出报告…", self.export, True)
        self.export_button.grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(page, text="误报标记不会删除原文件或覆盖模型原判断。导出保留扫描覆盖状态；取消和部分完成不会变成完整成功。",
                  wraplength=880, style="Muted.TLabel").pack(anchor="w", pady=10)

    def enumeration_mode(self):
        return {"NTFS 快速扫描（失败停止）":"ntfs", "普通目录遍历":"walk"}.get(self.enumeration.get(), "auto")

    def options(self):
        return ScanOptions(self.root_path.get().strip(), directory_ai=True, workflow=True, ai_backend="embedded",
            ai_model_path=self.model_path.get().strip(), recursive=self.recursive.get(), use_excludes=self.excludes.get(),
            enumeration_mode=self.enumeration_mode(),
            ai_batch_items=int(self.batch.get()), ai_context=int(self.context.get()), ai_threads=int(self.threads.get()),
            ai_input_bytes=int(self.budget.get()), ai_worker_timeout=int(self.timeout.get()))

    def browse(self, kind):
        preview = self.controller.scan.running and self.controller.report.get("stage") == "preview"
        if self.controller.busy and not (preview and kind == "model"):
            return
        if kind == "directory":
            path = filedialog.askdirectory(parent=self.root, title="选择检查目录", mustexist=True)
        else:
            path = filedialog.askopenfilename(parent=self.root, title="选择本地模型", filetypes=[("GGUF模型", "*.gguf")])
        if path:
            (self.root_path if kind == "directory" else self.model_path).set(path)
            if kind == "directory":
                self.start()

    def choose_drive(self):
        self.root_path.set("C:\\" if os.name == "nt" else "/")
        self.start()

    def preflight(self):
        try:
            options = self.options()
            if not Path(options.root).is_dir() or not Path(options.ai_model_path).is_file():
                raise ValueError("missing path")
            with Path(options.ai_model_path).open("rb") as stream:
                if stream.read(4) != b"GGUF":
                    raise ValueError("invalid GGUF")
            if util.find_spec("llama_cpp") is None:
                raise ValueError("missing runtime")
            version = metadata.version("llama-cpp-python")
            self.preflight_text.set(f"✓ 目录可访问   ✓ GGUF 文件头有效   ✓ llama-cpp-python {version}\n"
                f"CPU {options.ai_threads} 线程 · 上下文 {options.ai_context} · 每批最多 {options.ai_batch_items} 条\n"
                "模型架构兼容性、内存是否足够与识别效果仍需实际运行确认。")
            return True
        except Exception:
            self.preflight_text.set("请检查目录、GGUF 模型路径、数字设置和 local-ai 依赖；尚未开始扫描。")
            self.tabs.select(0)
            return False

    def replace_current(self, action):
        if not self.flush_note():
            return
        if not self.controller.dirty:
            action()
            return
        answer = messagebox.askyesnocancel("保存当前会话", "当前结果或复核尚未保存。\n是否先保存会话再继续？", parent=self.root)
        if answer is True:
            self.save(after=action)
        elif answer is False:
            action()

    def start(self):
        if self.controller.busy:
            return
        options = ScanOptions(self.root_path.get().strip(), workflow=True,
            recursive=self.recursive.get(), use_excludes=self.excludes.get(),
            enumeration_mode=self.enumeration_mode())
        if not Path(options.root).is_dir():
            self.message.set("请选择可访问的目录。")
            return
        def begin():
            self.controller.start(options, discard=True)
            self.folder, self.folder_page = ".", 0
            self.selected, self.note_dirty, self.page = None, False, 0
            self.reset_filters()
            self.tabs.select(1)
            self.message.set("正在加载目录结构；完成后可浏览目录，再点击开始 AI 审阅。")
            self.refresh()
        self.replace_current(begin)

    def begin_review(self):
        if self.controller.report.get("stage") != "preview" or not self.preflight():
            return
        try:
            self.controller.scan.begin_review(self.options())
        except ValueError:
            self.message.set("目录已改变，请停止当前预览并重新加载目录结构。")
            return
        self.tabs.select(1)
        self.message.set("AI 将调用 mark_privacy 标记疑似隐私，界面随每批工具调用更新。")
        self.refresh()

    def render_structure(self):
        store = self.controller.scan.inventory
        selection = self.structure.selection()
        scroll = self.structure.yview()
        self.structure.delete(*self.structure.get_children())
        self.structure_rows = {}
        if store is None:
            self.structure_path.set("历史会话仅保存候选；完整目录结构请重新加载。")
            return
        rows = store.children(self.folder, self.folder_page, PAGE_SIZE+1)
        self.folder_next = int(rows[PAGE_SIZE-1]["entry_id"][1:]) if len(rows)>PAGE_SIZE else None
        if getattr(store, "lazy", False):
            note = " · 正在读取当前页…" if store.pending else " · 当前页已加载"
            if store.error:
                note = " · 此目录无法读取或已被过滤"
            elif not store.online:
                note = " · 仅保留已缓存页面"
            self.structure_path.set(display_text(self.folder) + f" · 卷级索引 {len(store):,} 条" + note)
        else:
            self.structure_path.set(display_text(self.folder) + f" · 共 {len(store):,} 条")
        for row in rows[:PAGE_SIZE]:
            identifier, verdict = row["entry_id"], row["verdict"]
            label = "未检查" if verdict is None else ("本批未发现线索" if verdict["label"] == "NONE" else "⚑ " + verdict["label"])
            self.structure_rows[identifier] = row
            self.structure.insert("", "end", iid=identifier,
                text=("📁 " if row["is_dir"] else "  ") + display_text(row["name"]),
                values=(label, display_text(verdict["reason"]) if verdict else ""),
                tags=("marked",) if verdict and verdict["label"] != "NONE" else ())
        if selection and selection[0] in self.structure_rows:
            self.structure.selection_set(selection[0])
        if scroll:
            self.structure.yview_moveto(scroll[0])

    def folder_open(self, event=None):
        selection = self.structure.selection()
        row = self.structure_rows.get(selection[0]) if selection else None
        if row and row["is_dir"]:
            self.folder, self.folder_page = row["relative_path"], 0
            self.render_structure()

    def folder_up(self):
        from pathlib import PurePosixPath
        self.folder, self.folder_page = str(PurePosixPath(self.folder).parent), 0
        self.render_structure()

    def folder_move(self, direction):
        if direction < 0:
            store = self.controller.scan.inventory
            self.folder_page = store.previous(self.folder, self.folder_page) if store is not None else 0
        elif getattr(self, "folder_next", None) is not None:
            self.folder_page = self.folder_next
        self.render_structure()

    def stop(self):
        self.controller.scan.cancel()
        self.message.set("正在停止，已收到的候选会保留，状态将是不完整检查。")
        self.refresh()

    def save(self, after=None):
        if self.controller.busy or not self.controller.report:
            return
        if not self.flush_note():
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="保存私有会话", defaultextension=".pfsreview",
                                          initialfile="review-"+datetime.now().strftime("%Y%m%d-%H%M%S")+".pfsreview", confirmoverwrite=False,
                                          filetypes=[("PrivacyFS会话", "*.pfsreview")])
        if path:
            self.controller.save(path)
            self.after_save = after
            self.refresh()

    def open(self):
        if self.controller.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="打开私有会话", filetypes=[("PrivacyFS会话", "*.pfsreview")])
        if path:
            def begin():
                self.controller.open(path, discard=True)
                self.selected, self.note_dirty = None, False
                self.refresh()
            self.replace_current(begin)

    def export(self):
        if self.controller.busy or not self.controller.report:
            return
        if not self.flush_note():
            return
        format = "json" if self.export_format.get().startswith("JSON") else "csv"
        scope = "all" if self.export_scope.get().startswith("全部") else "confirmed"
        path = filedialog.asksaveasfilename(parent=self.root, title="导出私有复核报告", defaultextension="."+format,
                                          initialfile="report-"+datetime.now().strftime("%Y%m%d-%H%M%S")+"."+format, confirmoverwrite=False,
                                          filetypes=[(format.upper(), "*."+format)])
        if path:
            self.controller.export(path, format, scope)
            self.refresh()

    def cancel_file_job(self):
        self.controller.cancel_file_job()
        self.after_save = None
        self.message.set(self.controller.message)
        self.refresh()

    def filter_changed(self):
        if not self.flush_note():
            self.filter_pending = False
            return
        self.page = 0
        self.render_page()

    def reset_filters(self):
        self.query.set("")
        self.type_filter.set("全部类型")
        self.state_filter.set("全部候选")
        self.category_filter.set("全部类别")
        self.page = 0

    def change_page(self, delta):
        if not self.flush_note():
            return
        self.page += delta
        self.render_page()

    def render_page(self):
        state = next((key for key,value in STATE_LABELS.items() if value == self.state_filter.get()), "all")
        categories = {"人物":("PERSON","NAME_LIST","EN_NAME","PINYIN"), "联系方式":("PATTERN","REGEX"),
            "机构":("ORG",), "身份与敏感词":("IDENTITY","KEYWORD","PROFESSION"), "AI待复核":("UNCERTAIN",)}
        rows, count, self.filter_pending, self.page = self.controller.rows.review_page(self.page, PAGE_SIZE,
            self.query.get(), {"仅文件":1,"仅目录":2}.get(self.type_filter.get(),0),
            categories.get(self.category_filter.get(),()), state)
        self.rendering = True
        scroll = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        self.visible = {row["_review_id"]:row for row in rows}
        for row in rows:
            category = " / ".join(sorted({s.get("label", s["category"]) for s in row["signals"]}))
            self.tree.insert("", "end", iid=str(row["_review_id"]), values=(display_text(row["relative_path"]),
                STATE_LABELS[row["human_review"]["state"]], category))
        if self.selected in self.visible:
            self.tree.selection_set(str(self.selected))
        else:
            self.selected = None
            self.show_details(None)
        if scroll:
            self.tree.yview_moveto(scroll[0])
        self.rendering = False
        self.page_text.set(f"筛选 {count:,} / 全部 {len(self.controller.rows):,} · 第 {self.page+1} 页" + (" · 筛选中" if self.filter_pending else ""))
        self.prev_button.configure(state="normal" if self.page else "disabled")
        self.next_button.configure(state="normal" if (self.page+1)*PAGE_SIZE < count else "disabled")
        self.update_actions()

    def select(self, event=None):
        if self.rendering:
            return
        selection = self.tree.selection()
        identifier = int(selection[0]) if selection else None
        if identifier == self.selected:
            return
        if not self.flush_note():
            if self.selected is not None:
                self.tree.selection_set(str(self.selected))
            return
        self.selected = identifier
        self.show_details(self.visible.get(identifier))
        self.update_actions()

    def show_details(self, row):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if row:
            text = display_text(row["path"]) + "\n\n"
            for signal in row["signals"]:
                text += f'{display_text(signal.get("label", signal["category"]))} · {display_text(signal["source"])}\n'
                text += display_text(signal.get("reason", signal["surface"])) + "\n"
            text += "\n人工复核：" + STATE_LABELS[row["human_review"]["state"]]
            self.detail.insert("end", text)
        else:
            self.detail.insert("end", "选择一个候选，查看真实路径、模型理由并记录复核结果。")
        self.detail.configure(state="disabled")
        self.note.configure(state="normal")
        self.note.delete("1.0", "end")
        if row:
            self.note.insert("1.0", row["human_review"]["note"])
        self.note.edit_modified(False)
        self.note_dirty = False

    def note_changed(self, event=None):
        if self.note.edit_modified():
            self.note_dirty = True
            self.note.edit_modified(False)

    def flush_note(self):
        if self.note_dirty and self.selected is not None and not self.controller.busy:
            note = self.note.get("1.0", "end-1c")
            if len(note) > 2000:
                self.message.set("备注超过2000字，请缩短后保存。")
                return False
            self.controller.mark([self.selected], self.controller.rows.state(self.selected), note)
            self.note_dirty = False
        return True

    def mark(self, state):
        if self.selected is None or self.controller.busy:
            return
        note = self.note.get("1.0", "end-1c")
        if len(note) > 2000:
            self.message.set("备注超过2000字，请缩短后再复核。")
            return
        self.controller.mark([self.selected], state, note)
        self.note_dirty = False
        self.refresh()
        if self.selected in self.visible:
            self.show_details(self.visible[self.selected])

    def mark_page_false(self):
        if self.controller.busy or not self.visible:
            return
        if not self.flush_note():
            return
        self.controller.mark(list(self.visible), "false_positive", None)
        self.refresh()

    def undo(self):
        if self.controller.busy:
            return
        if not self.flush_note():
            return
        self.controller.undo()
        self.refresh()
        if self.selected in self.visible:
            self.show_details(self.visible[self.selected])

    def reveal(self):
        row = self.visible.get(self.selected)
        if row:
            parent = Path(row["path"]).parent
            if os.name == "nt" and parent.is_dir():
                os.startfile(str(parent))
            else:
                self.message.set("所在目录不可用，历史结果中的路径可能已经变化。")

    def update_actions(self):
        busy, running = self.controller.busy, self.controller.scan.running
        preview = self.controller.report.get("stage") == "preview" and running
        unverified = self.controller.report.get("inventory_verified") is False
        self.ai_button.configure(state="normal" if preview else "disabled",
                                 text="核验并开始 AI 审阅" if unverified else "开始 AI 审阅")
        for widget in self.config_widgets:
            widget.configure(state="disabled" if busy and not preview else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
        if busy:
            for widget in self.inventory_widgets:
                widget.configure(state="disabled")
        for button in (self.start_button, self.open_button):
            button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        for button in (self.save_button, self.export_button):
            button.configure(state="normal" if self.controller.report and not busy else "disabled")
        self.cancel_io.configure(state="normal" if self.controller.job else "disabled")
        for button in self.review_buttons:
            button.configure(state="normal" if self.selected is not None and not busy else "disabled")
        self.note.configure(state="normal" if self.selected is not None and not busy else "disabled")
        self.bulk_button.configure(state="normal" if self.visible and not busy else "disabled")
        self.undo_button.configure(state="normal" if self.controller.rows.can_undo and not busy else "disabled")

    def refresh(self):
        report, stats = self.controller.report, self.controller.report.get("stats", {})
        self.status.set(SCAN_LABELS.get(report.get("status"), "准备就绪") + (" · 历史会话" if self.controller.reopened else ""))
        if self.controller.scan.running and report.get("stage") == "preview":
            self.status.set("目录预览就绪 · 等待启动 AI")
        self.phase.set(display_text(report.get("phase", "选择目录与模型后开始检查。")))
        if report.get("inventory_verified") is False:
            self.phase.set(self.phase.get() + " · 当前结构是未核验预览，可能缺少硬链接名称")
        preparing = report.get("enumeration_backend") == "ntfs_preparing"
        lazy_browsing = report.get("lazy_preview") and not report.get("inventory_verified") and not preparing
        if lazy_browsing:
            values = (("visited", "已读 NTFS 记录", stats.get("ntfs_records",0)),
                      ("checked", "已响应页面查询", stats.get("preview_pages",0)),
                      ("unchecked", "全目录尚未核验", None), ("hits", "AI 尚未启动", None))
        elif preparing:
            preview_dirs = bool(stats.get("ntfs_preliminary") or stats.get("ntfs_preview_directories"))
            values = (("visited", "已读 NTFS 记录", stats.get("ntfs_records",0)),
                      ("checked", "已整理预览目录" if preview_dirs else "已核验目录",
                       stats.get("ntfs_preview_directories",0) if preview_dirs else stats.get("ntfs_directories",0)),
                      ("unchecked", "已补齐文件名", stats.get("ntfs_link_names",0)),
                      ("hits", "AI 尚未启动", None))
        else:
            values = (("visited", "已遍历", stats.get("visited",0)),
                      ("checked", "已有判断", stats.get("ai_checked",0)),
                      ("unchecked", "尚未检查", stats.get("ai_unchecked",0)),
                      ("hits", "候选条目", len(self.controller.rows)))
        for key, title, value in values:
            self.metric_cards[key].configure(text=title)
            self.metrics[key].set("—" if value is None else f"{value:,}")
        enumeration = {"ntfs":"NTFS 名称索引", "ntfs_preparing":"正在建立 NTFS 索引", "walk":"普通目录遍历", "ntfs_unavailable":"NTFS 不可用（未改用普通遍历）"}.get(report.get("enumeration_backend"),"尚未开始")
        fallback = {"access_denied":"卷读取权限不足，已回退普通遍历", "not_ntfs":"非NTFS卷", "scope_not_volume":"当前选择不是递归卷根范围",
                    "journal_gap":"USN日志发生变化或缺口", "journal_unavailable":"USN日志不可用"}.get(report.get("enumeration_fallback"), "")
        if report.get("enumeration_fallback") and not fallback:
            fallback = "快速枚举不可用，已回退普通遍历"
        if report.get("enumeration_backend") == "ntfs_unavailable":
            fallback = "请查看下方原因；可手动选择普通目录遍历"
        self.coverage.set(f'枚举：{enumeration}'  + (f'（{fallback}）' if fallback else "") + "\n" +
            f'排除 {stats.get("excluded",0):,} · 跳过链接 {stats.get("reparse",0):,} · '
            f'私有状态 {stats.get("protected",0):,} · 访问失败 {stats.get("errors",0):,}\n'
            f'工具调用 {stats.get("ai_tool_calls",0):,} · 批次 {stats.get("ai_batches",0):,} · 不完整批次 {stats.get("ai_failed_batches",0):,} · '
            f'超长条目 {stats.get("ai_oversized",0):,} · 用时 {report.get("elapsed",0):.1f}s\n'
            + ERROR_LABELS.get(report.get("reason"), ""))
        if preparing:
            self.coverage.set(self.coverage.get() + "\n当前是索引准备阶段；NTFS 记录数是卷级元数据数量，不是所选目录条目数或 AI 检查数。")
        if str(report.get("reason", "")).startswith("ntfs_") and report.get("reason") not in ERROR_LABELS:
            self.coverage.set(self.coverage.get() + "\nNTFS 索引未能建立（" + display_text(report["reason"]) + "）；未自动改用普通遍历。")
        if stats.get("ntfs_late_changes"):
            self.coverage.set(self.coverage.get() + f'\n索引准备期间又有 {stats["ntfs_late_changes"]} 条结构变更；结果不是静态快照。')
        counts = self.controller.rows.counts()
        self.review_summary.set("   ·   ".join(f"{STATE_LABELS[state]} {counts[state]:,}" for state in STATE_LABELS))
        self.saved_state.set("有未保存的会话变更" if self.controller.dirty else ("会话已保存" if report else "尚未开始"))
        if self.controller.scan.running and not self._was_running:
            self.progress.start(12)
        elif not self.controller.scan.running:
            self.progress.stop()
        self._was_running = self.controller.scan.running
        self.render_page()
        self.render_structure()

    def tick(self):
        try:
            was_running = self.controller.scan.running
            previous_job = self.controller.job.kind if self.controller.job else None
            if self.controller.poll():
                if previous_job:
                    self.message.set(self.controller.message)
                if previous_job == "open" and not self.controller.job and not self.controller.last_error:
                    self.reset_filters()
                    report = self.controller.report
                    self.root_path.set(report.get("root", ""))
                    settings = report.get("settings", {})
                    self.enumeration.set({"ntfs":"NTFS 快速扫描（失败停止）", "walk":"普通目录遍历"}.get(settings.get("enumeration_mode"), "自动（卷根优先 NTFS）"))
                    self.model_path.set(str(settings.get("ai_model_path") or ""))
                    for variable, key in ((self.batch,"ai_batch_items"), (self.context,"ai_context"),
                                          (self.threads,"ai_threads"), (self.budget,"ai_input_bytes"),
                                          (self.timeout,"ai_worker_timeout")):
                        if type(settings.get(key)) is int:
                            variable.set(str(settings[key]))
                    for variable, key in ((self.recursive,"recursive"), (self.excludes,"use_excludes")):
                        if type(settings.get(key)) is bool:
                            variable.set(settings[key])
                    self.tabs.select(2)
                if was_running and not self.controller.scan.running:
                    self.tabs.select(2)
                    self.message.set("检查已结束，请查看覆盖状态，再逐条复核候选。")
                self.refresh()
                if previous_job == "save" and not self.controller.job and self.after_save:
                    action, self.after_save = self.after_save, None
                    if not self.controller.last_error:
                        action()
            elif self.filter_pending:
                self.render_page()
            if getattr(self.controller.scan.inventory, "pending", False):
                self.render_structure()
        except Exception:
            self.message.set("界面操作失败，未确认完成；当前结果尚未作为完整报告发布。")
        if not self._closed:
            self._tick_id = self.root.after(100, self.tick)

    def close_request(self):
        if self.controller.busy:
            if messagebox.askyesno("停止并退出", "当前操作尚未结束。\n停止并退出将丢弃尚未保存的结果，是否继续？", parent=self.root):
                self.shutdown()
            return
        if not self.flush_note():
            return
        if self.controller.dirty:
            answer = messagebox.askyesnocancel("退出前保存", "是否保存会话后退出？", parent=self.root)
            if answer is None:
                return
            if answer:
                self.save(after=self.shutdown)
                return
        self.shutdown()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        if self._tick_id:
            self.root.after_cancel(self._tick_id)
        self.controller.close()
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            if not self._closed:
                self.shutdown()


def main():
    Workbench().run()


if __name__ == "__main__":
    main()
