#!/usr/bin/env python3
"""ZCode-like model settings + CCS fetch/dropdown. Closing the window exits."""

from __future__ import annotations

import ctypes
import json
import re
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

from sync import (
    API_TYPES,
    DEFAULT_CONFIG,
    REASONING_LABELS,
    REASONING_LADDER,
    SyncError,
    fetch_models,
    list_provider_summaries,
    load_config,
    save_provider,
    test_connection,
)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

BG = "#161616"
CARD = "#2B2B2B"
SIDE = "#2B2B2B"
INPUT = "#2B2B2B"
LINE = "#414141"
LINE2 = "#414141"
TEXT = "#D4D4D4"
MUTED = "#8d8d8d"
WHITE = "#ffffff"  # R6-13/P2-23：官方白钮纯白（原 #f4f4f4）
WHITE_FG = "#111111"
GREEN = "#46BF72"
OK = "#7ee2a8"
ERR = "#ff5c5c"  # P2-23：官方 destructive
ROW = "#2B2B2B"
HOVER = "#242424"
SEL = "#4B4B4B"
GHOST = "#363636"
ICON_MUTED = "#B9B9B9"  # P2-23/R6-8：行内图标静默色，官方实测 #B9B9B9
CHIP_ON = "#454545"  # R6-7：chips 选中底色（官方实测 (69,69,69)）
CHIP_ON_LINE = "#5A5A5A"  # R6-7：chips 选中描边
POPUP_BG = "#1E1E1E"  # P0-1：弹层 inner 深底，比卡片（#2B2B2B）深一档
COL_FIELD_W = 80
COL_DROP_W = 96
COL_DEL_W = 44  # 图标按钮列（插头/铅笔/垃圾桶，32px 画布）

# R4-P1-9 文案包（findings-ux.md 逐条落地；R6-P1-14/15：统一「推理等级」命名）
NAME_PLACEHOLDER = "输入模型 ID，或点获取模型列表"
CTX_PLACEHOLDER = "如 1000000（≈1M tokens）"
MAX_PLACEHOLDER = "留空 = 默认"
API_TYPE_HINT = "不确定选哪个？大多数 OpenAI 兼容接口、中转站选第一项 Chat Completions；仅直连官方 Anthropic 接口时选最后一项。"
NO_CANDIDATE_TEXT = "还没有候选模型：先点上方「获取模型列表」"
NO_MATCH_TEXT = "没有匹配的模型，换个关键字试试"
REASON_HEAD_TIP = (
    "推理等级：勾选这个模型实际支持的档，不必连续勾；只保存勾上的。"
    "极低=minimal、低=low、中=medium、高=high、极高=xhigh、最高=max、极致=ultra。"
)
DEFAULT_HEAD_TIP = (
    "默认档位：新建会话时用的那一档，直接选。"
    "选了还没在「推理等级」勾选的档，会自动勾上并设为默认。"
    "写在列表末尾，ZCode 读最后一项。"
)
NUM_HEAD_TIP = (
    "上下文：模型一次能读入的最大 token 数（含对话与文件）。"
    "最大输出：单次回复最多生成的 token 数。留空 = 不写入、用模型默认。"
)
FETCH_TIP = "向服务商拉取可用模型作候选；再在每行「模型 ID」下拉里挑选，选中的才会保存。"
ADD_MODEL_TIP = "手动加一行自己填模型 ID；不拉取列表时用。"
RELOAD_TIP = "重新读取 ZCode 配置文件；当前窗口里未保存的修改会被覆盖。"
EMPTY_PROVIDERS_GUIDE = "还没有自定义供应商：点右上角「+ 添加供应商」，填好 Base URL 和 API Key 后获取模型列表。"
# R4-P1-10 [C-12]：模型 ID 输入侧剥离换行/控制字符
MODEL_ID_CTRL_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")

API_TYPE_OPTIONS = (
    ("chat", "Chat Completions (/v1/chat/completions)"),
    ("responses", "Responses (/responses)"),
    # P2-22：与 sync 实际拼接的 URL 一致（anthropic 基址 + /v1/messages）
    ("anthropic", "Anthropic Messages (/v1/messages)"),
)
# 档位中文都从 sync.REASONING_LABELS 来。勾选集合不是连续区间。
REASON_UNSET = "未设置"
DEFAULT_FOLLOW = "随最高勾选"
LABEL_TO_LEVEL = {label: level for level, label in REASONING_LABELS.items() if level}


def valid_positive_int(raw: str) -> bool:
    """A2-3：行内与弹窗共用的数字口径——isdecimal + int>0，
    拒绝 "+5"/"1_000"/"²"（isdecimal 假）与 0/负数；"001"/全角数字按值接受。"""
    if not raw:
        return False
    return raw.isdecimal() and int(raw) > 0


def api_type_short(value: str | None) -> str:
    if value == "openai-responses":
        return "responses"
    if value == "anthropic-messages":
        return "anthropic"
    return "chat"


def api_type_label(short: str) -> str:
    for key, label in API_TYPE_OPTIONS:
        if key == short:
            return label
    return API_TYPE_OPTIONS[0][1]


class Popup:
    """One dark popup at a time."""

    current = None

    @classmethod
    def close(cls) -> None:
        if cls.current is not None:
            try:
                cls.current.destroy()
            except tk.TclError:
                pass
            cls.current = None

    @classmethod
    def close_if(cls, popup) -> None:
        """R4-P1-4 [C-2]：延迟回调只关「发起时那一个」弹层，
        120ms 内新开的弹层不被残留定时器误杀。"""
        if cls.current is popup:
            cls.close()


class Tooltip:
    """R4-P1-9：深色小 tooltip。hover 450ms 后弹出，离开/点击即隐藏。"""

    _current = None  # 全局同时只保留一个

    def __init__(self, widget: tk.Misc, text: str, fonts: dict) -> None:
        self.widget = widget
        self.text = text
        self.fonts = fonts
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", lambda _e: self._hide(), add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(450, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001
                pass
            self._after_id = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
        if Tooltip._current is self:
            Tooltip._current = None

    def _show(self) -> None:
        self._after_id = None
        if Tooltip._current is not None and Tooltip._current is not self:
            Tooltip._current._hide()
        if self._tip is not None or not self.widget.winfo_exists():
            return
        tip = tk.Toplevel(self.widget)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=LINE2)
        inner = tk.Label(
            tip,
            text=self.text,
            bg=POPUP_BG,
            fg=TEXT,
            font=self.fonts["small"],
            justify="left",
            anchor="w",
            padx=8,
            pady=6,
        )
        inner.pack(padx=1, pady=1)
        self.widget.update_idletasks()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip.geometry(f"+{x}+{y}")
        tip.deiconify()
        tip.lift()
        self._tip = tip
        Tooltip._current = self


class Drop(tk.Frame):
    """Dark field that opens a list. Searchable when the catalog is long."""

    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        options: list[str],
        *,
        fonts: dict,
        searchable: bool = False,
        command=None,
        width: int | None = None,
        note_fn=None,
        empty_fn=None,
        placeholder: str = "",
    ) -> None:
        super().__init__(
            parent,
            bg=INPUT,
            highlightbackground=LINE2,
            highlightthickness=1,
        )
        self.variable = variable
        self.options = list(options)
        self.searchable = searchable
        self.command = command
        self.fonts = fonts
        # R4-P1-9：note_fn() 返回弹层顶部说明行文案（思考档位弹层说明）；
        # empty_fn() 返回列表为空时的占位文案（区分「未获取目录」与「搜索无结果」）。
        self.note_fn = note_fn
        self.empty_fn = empty_fn
        self.placeholder = placeholder
        self._enabled = True
        # label 用普通 text，不绑 textvariable：空值可显示 placeholder（占位不算进名字列宽）。
        self.label = tk.Label(
            self,
            text="",
            bg=INPUT,
            fg=TEXT,
            font=fonts["name"] if searchable else fonts["ui"],
            anchor="w",
            padx=10,
        )
        # P1-1/P1-2：label 撑满左侧、箭头贴字段右缘（与基线一致），窄框里不再被裁成残影。
        self.label.pack(side="left", fill="both", expand=True, ipady=6)
        # P1-4：padx 10 + pady 4 扩大命中区；字形/字号与 API 格式下拉本就同款（ui ▾）。
        caret = tk.Label(
            self, text="▾", bg=INPUT, fg=MUTED, font=fonts["ui"], padx=10, pady=4
        )
        caret.pack(side="right", padx=(0, 10))
        self.caret = caret
        for widget in (self, self.label, caret):
            widget.bind("<Button-1>", self._on_click)
        if width:
            self.configure(width=width)
        self.variable.trace_add("write", self._on_var_write)
        self._sync_color()

    def _on_var_write(self, *_args) -> None:
        # 行重建（切换供应商/保存）后旧变量仍可能被 set：控件已销毁时静默跳过。
        try:
            self._sync_color()
        except tk.TclError:
            pass

    def _on_click(self, event) -> None:
        self.open()
        return "break"

    def set_options(self, options: list[str]) -> None:
        self.options = list(options)

    def set_enabled(self, enabled: bool) -> None:
        """False 时 open() 直接 return，label 与 caret 改 MUTED。"""
        self._enabled = bool(enabled)
        self._sync_color()

    def _sync_color(self) -> None:
        value = self.variable.get()
        if not self._enabled:
            shown = value or self.placeholder
            fg = MUTED
        elif value:
            shown = value
            fg = TEXT
        elif self.placeholder:
            shown = self.placeholder
            fg = MUTED
        else:
            shown = ""
            fg = MUTED
        self.label.configure(text=shown, fg=fg)
        self.caret.configure(fg=MUTED)

    def open(self) -> None:
        if not self._enabled:
            return
        Popup.close()
        self.update_idletasks()
        # P0-1(1)：高度自适应——Listbox 原生无行距选项（spacing1/3 是 Text 的），
        # 行实高=字体行距，item_h 取实测行距；公式形状保留：8 + 搜索框 + min(n,8)×item_h + 8，
        # 上限 280。候选超过可视数靠滚轮滚，一律不出滚动条，视觉上零大片空白。
        linespace = int(round(float(self.fonts["ui"].metrics("linespace"))))
        item_h = linespace
        width = max(self.winfo_width(), 280)
        x = self.winfo_rootx()
        field_top = self.winfo_rooty()
        y_below = field_top + self.winfo_height()

        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(bg=LINE2)
        popup.attributes("-topmost", True)
        Popup.current = popup

        # P0-1(3)：inner 深底 #1E1E1E + 外层 1px LINE2 描边近似投影（Tk 做不了真投影）。
        inner = tk.Frame(popup, bg=POPUP_BG, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        # R4-P1-9 [U-1]：思考档位弹层顶部动态说明行（随当前值，每次打开重算）。
        note_label = None
        note_h = 0
        if self.note_fn is not None:
            try:
                note_text = str(self.note_fn() or "")
            except Exception:  # noqa: BLE001
                note_text = ""
            if note_text:
                note_label = tk.Label(
                    inner,
                    text=note_text,
                    bg=POPUP_BG,
                    fg=MUTED,
                    font=self.fonts["small"],
                    anchor="w",
                    justify="left",
                )
                note_label.pack(fill="x", padx=8, pady=(6, 0))
                note_h = note_label.winfo_reqheight() + 6
                width = max(width, min(420, note_label.winfo_reqwidth() + 26))

        query = tk.StringVar()
        search = None
        if self.searchable:
            # P0-1(2)：搜索框可见——INPUT 底 + 1px LINE2 描边。
            search = tk.Entry(
                inner,
                textvariable=query,
                bg=INPUT,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                font=self.fonts["ui"],
                highlightthickness=1,
                highlightbackground=LINE2,
            )
            search.pack(fill="x", padx=6, pady=(6, 4), ipady=5)
        search_h = (search.winfo_reqheight() + 6 + 4) if search is not None else 0

        list_box = tk.Listbox(
            inner,
            bg=POPUP_BG,
            fg=TEXT,
            selectbackground=SEL,
            selectforeground=TEXT,
            activestyle="none",
            highlightthickness=0,
            bd=0,
            font=self.fonts["ui"],
            exportselection=False,
        )
        list_box.pack(fill="both", expand=True, padx=2, pady=(0, 4))

        # P0-1(4)：底缘会超出卡片底缘或屏幕底缘（取小者）则整体向上翻转。
        def formula_height(count: int) -> int:
            visible = min(max(count, 1), 8)  # 空态占位按 1 条计
            return min(8 + note_h + search_h + visible * item_h + 8, 280)

        h0 = formula_height(len(self.options))
        limit = self.winfo_screenheight()
        card = getattr(self.winfo_toplevel(), "card_ref", None)
        if card is not None:
            try:
                limit = min(limit, card.winfo_rooty() + card.winfo_height())
            except tk.TclError:
                pass
        flipped = y_below + h0 > limit

        def filtered() -> list[str]:
            needle = query.get().strip().lower()
            if not needle:
                return self.options
            return [item for item in self.options if needle in item.lower()]

        def apply_geometry(count: int) -> None:
            visible = min(max(count, 1), 8)
            list_box.configure(height=visible)  # req 高同步为可见行数，reqheight≤280 可验证
            height = formula_height(count)
            y = field_top - height if flipped else y_below
            popup.geometry(f"{width}x{height}+{x}+{y}")

        def select_current() -> None:
            # P0-1(5)：选中当前值并 see()；无当前值不选。
            list_box.selection_clear(0, tk.END)
            current = self.variable.get()
            if not current:
                return
            for index, item in enumerate(list_box.get(0, tk.END)):
                if item == current:
                    list_box.selection_set(index)
                    list_box.see(index)
                    return

        # R4-P1-9 [U-5]：空态文案区分——未获取目录 vs 搜索无结果。
        empty_text = ""
        if self.empty_fn is not None:
            try:
                empty_text = str(self.empty_fn() or "")
            except Exception:  # noqa: BLE001
                empty_text = ""
        if not empty_text:
            empty_text = NO_MATCH_TEXT

        def refill(_event=None) -> None:
            list_box.delete(0, tk.END)
            items = filtered()
            for item in items[:400]:
                list_box.insert(tk.END, item)
            if not items:
                list_box.insert(tk.END, empty_text)
            apply_geometry(len(items))
            select_current()

        def pick(_event=None):
            selection = list_box.curselection()
            if not selection:
                return "break"
            value = list_box.get(selection[0])
            if value == empty_text:
                return "break"
            self.variable.set(value)
            Popup.close()
            if self.command:
                self.command(value)
            return "break"

        def move_sel(delta: int):
            size = list_box.size()
            if not size:
                return "break"
            sel = list_box.curselection()
            if sel:
                index = min(max(sel[0] + delta, 0), size - 1)
            else:
                index = 0 if delta > 0 else size - 1
            list_box.selection_clear(0, tk.END)
            list_box.selection_set(index)
            list_box.see(index)
            return "break"

        def on_motion(event) -> None:
            # P0-1(6)：hover 高亮随鼠标；移出由 <Leave> 恢复当前值。
            index = list_box.nearest(event.y)
            if index < 0 or index >= list_box.size():
                return
            list_box.selection_clear(0, tk.END)
            list_box.selection_set(index)

        # P0-1(7)：键盘 ↑/↓ 移动选中、Enter 选中（Esc 已有）。pick 返回 break 防止
        # listbox 与 popup 两级 bindtag 重复触发。
        list_box.bind("<ButtonRelease-1>", pick)
        list_box.bind("<Return>", pick)
        list_box.bind("<Up>", lambda _e: move_sel(-1))
        list_box.bind("<Down>", lambda _e: move_sel(1))
        list_box.bind("<Motion>", on_motion)
        list_box.bind("<Leave>", lambda _e: select_current())
        popup.bind("<Escape>", lambda _e: Popup.close())
        popup.bind("<Return>", pick)
        popup.bind("<Up>", lambda _e: move_sel(-1))
        popup.bind("<Down>", lambda _e: move_sel(1))
        popup.bind("<FocusOut>", self._on_focus_out)
        popup.bind("<Button-1>", lambda _e: None)
        if search is not None:
            query.trace_add("write", lambda *_: refill())
            search.bind("<Return>", pick)
            search.bind("<Up>", lambda _e: move_sel(-1))
            search.bind("<Down>", lambda _e: move_sel(1))

        refill()
        popup.deiconify()
        popup.lift()
        popup.focus_force()
        if search is not None:
            search.focus_set()

    def _on_focus_out(self, _event=None) -> None:
        # R4-P1-4/P1-5 [C-2/C-3]：焦点离开弹层 120ms 后复查——新焦点仍在弹层
        # 子树内（弹层内部交互/键盘循环）才保留；移到主窗口其他控件或应用外
        # 即关闭。回调闭包捕获发起时的 popup，只关自己那一个（定时器不误杀新弹层）。
        popup = Popup.current
        if popup is None:
            return
        self.after(120, lambda: self._close_if_focus_outside(popup))

    @staticmethod
    def _close_if_focus_outside(popup) -> None:
        if Popup.current is not popup:
            return
        try:
            if not popup.winfo_exists():
                return
            focused = popup.focus_get()
        except tk.TclError:
            return
        inside = False
        if focused is not None:
            try:
                path = str(focused)
                popup_path = str(popup)
                inside = path == popup_path or path.startswith(popup_path + ".")
            except Exception:  # noqa: BLE001
                inside = False
        if not inside:
            Popup.close()


class IconBtn(tk.Canvas):
    """ZCode 风格线条图标按钮：悬停变亮，带悬浮提示。行内 测试/编辑/删除 用。"""

    def __init__(self, parent: tk.Misc, kind: str, command, tip: str, fonts: dict) -> None:
        super().__init__(
            parent, width=32, height=26, bg=ROW, highlightthickness=0, bd=0, cursor="hand2"
        )
        self.kind = kind
        self.command = command
        self.fonts = fonts
        self.enabled = True
        self._paint(ICON_MUTED)  # P2-23：静默色 #B9B9B9（官方行内图标实测值）
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        Tooltip(self, tip, fonts)

    def _paint(self, color: str) -> None:
        self.delete("all")
        width = 2
        if self.kind == "test":  # 插头：连通测试
            self.create_line(12, 3, 12, 7, width=width, fill=color)
            self.create_line(20, 3, 20, 7, width=width, fill=color)
            self.create_rectangle(10, 7, 22, 13, outline=color, width=width)
            self.create_line(16, 13, 16, 21, width=width, fill=color)
        elif self.kind == "edit":  # 铅笔：编辑
            self.create_polygon(
                6, 20, 8, 14, 18, 4, 22, 8, 12, 18, 6, 20,
                outline=color, width=width, fill="",
            )
            self.create_line(8, 14, 12, 18, width=width, fill=color)
        else:  # delete：垃圾桶
            self.create_line(8, 8, 24, 8, width=width, fill=color)
            self.create_line(14, 8, 14, 5, 18, 5, 18, 8, width=width, fill=color)
            self.create_rectangle(10, 8, 22, 21, outline=color, width=width)
            self.create_line(14, 12, 14, 18, width=width, fill=color)
            self.create_line(18, 12, 18, 18, width=width, fill=color)

    def _on_enter(self, _event=None) -> None:
        if self.enabled:
            self._paint(TEXT)
            self.configure(bg=HOVER)

    def _on_leave(self, _event=None) -> None:
        self._paint(ICON_MUTED if self.enabled else "#3a3a3a")
        self.configure(bg=ROW)

    def _on_click(self, _event=None) -> None:
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if on:
            self.configure(cursor="hand2")
            self._paint(ICON_MUTED)
        else:
            self.configure(cursor="arrow")
            self._paint("#3a3a3a")


class ModelEditDialog(tk.Toplevel):
    """对应 ZCode「编辑模型配置」的详细弹窗：基础项 + 输入类型 + 模型能力 + 推理参数映射。"""

    def __init__(self, app: "App", row: dict) -> None:
        super().__init__(app)
        self.app = app
        self.row = row
        meta = row["meta"]
        self.title("编辑模型配置")
        # R6-2：弹窗整体底色与官方一致用 CARD(#2B2B2B)，输入框 INPUT 同色靠描边区分
        self.configure(bg=CARD)
        self.transient(app)
        self.resizable(False, True)
        fonts = app.fonts

        outer = tk.Frame(self, bg=CARD)
        outer.pack(fill="both", expand=True, padx=32, pady=24)
        # R6-3：弹窗标题用 h2（11pt bold，实测字高≈21px 对齐官方），与主窗标题拆档
        tk.Label(
            outer, text="编辑模型配置", bg=CARD, fg=TEXT, font=fonts["h2"], anchor="w"
        ).pack(fill="x")

        # P2-23：弹窗字段标签升为 ui 字号（官方 14px），MUTED 色不变
        def field_label(text: str) -> None:
            tk.Label(outer, text=text, bg=CARD, fg=MUTED, font=fonts["ui"], anchor="w").pack(
                fill="x", pady=(14, 4)
            )

        def entry(parent: tk.Frame, value: str, show: str = "") -> tk.Entry:
            box = tk.Frame(parent, bg=INPUT, highlightbackground=LINE2, highlightthickness=1)
            box.pack(fill="x")
            item = tk.Entry(
                box,
                bg=INPUT,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                font=fonts["ui"],
                highlightthickness=0,
                show=show,
            )
            # P2-23：文字左内距 (10,0)→(14,0)（官方 14 逻辑 px）
            item.pack(fill="x", ipady=7, padx=(14, 0))
            item.insert(0, value)
            return item

        field_label("模型 ID")
        self.id_entry = entry(outer, row["id"].get())

        field_label("上下文窗口")
        self.ctx_entry = entry(outer, row["ctx"].get())

        field_label("最大输出 Token（留空 = 用模型默认）")
        self.max_entry = entry(outer, row["max"].get())

        field_label("输入类型（文本始终开启）")
        input_row = tk.Frame(outer, bg=CARD)
        input_row.pack(fill="x")
        meta_image = bool(meta.get("supportsImage"))
        self.var_image = tk.BooleanVar(value=(row.get("extras") or {}).get("image", meta_image))
        self.var_video = tk.BooleanVar(value=(row.get("extras") or {}).get("video", meta.get("inputVideo")))
        self.var_pdf = tk.BooleanVar(value=(row.get("extras") or {}).get("pdf", meta.get("inputPdf")))
        # C2-3：BooleanVar 必须存实例引用，内联临时参数会被 GC 导致勾选框恒空
        self.var_text = tk.BooleanVar(value=True)
        self._chip(input_row, "文本（始终）", self.var_text, disabled=True)
        self._chip(input_row, "图片", self.var_image)
        self._chip(input_row, "视频", self.var_video)
        self._chip(input_row, "PDF", self.var_pdf)

        field_label("模型能力")
        cap_row = tk.Frame(outer, bg=CARD)
        cap_row.pack(fill="x")
        self.var_structured = tk.BooleanVar(
            value=(row.get("extras") or {}).get("structured", meta.get("capStructured"))
        )
        self.var_web = tk.BooleanVar(
            value=(row.get("extras") or {}).get("webSearch", meta.get("capWebSearch"))
        )
        self.var_midconv = tk.BooleanVar(
            value=(row.get("extras") or {}).get("midConv", meta.get("capMidConv"))
        )
        self._chip(cap_row, "结构化输出", self.var_structured)
        self._chip(cap_row, "原生联网搜索", self.var_web)
        self._chip(cap_row, "对话中系统消息", self.var_midconv)

        # U-4：统一「推理等级」命名，「可跳档」改「不必连续」
        field_label("推理等级（从低到高；勾选该模型支持的档即可，不必连续）")
        check_row = tk.Frame(outer, bg=CARD)
        check_row.pack(fill="x")
        self._last_check_row = check_row
        selected = [str(item) for item in row.get("checked") or []]
        extras = [item for item in selected if item not in REASONING_LADDER]
        self.reason_vars: dict[str, tk.BooleanVar] = {}
        for level in [*REASONING_LADDER, *extras]:
            var = tk.BooleanVar(value=level in selected)
            self.reason_vars[level] = var
            self._check(check_row, REASONING_LABELS.get(level, level), var)
        custom_row = tk.Frame(outer, bg=CARD)
        custom_row.pack(fill="x", pady=(6, 0))
        custom_box = tk.Frame(custom_row, bg=INPUT, highlightbackground=LINE2, highlightthickness=1)
        custom_box.pack(side="left", fill="x", expand=True)
        # U-2/P0-3：占位提示 + 回车添加；空输入/重名/非法字符都有弹窗内反馈
        self.custom_var = tk.StringVar()
        self.custom_entry = tk.Entry(
            custom_box,
            textvariable=self.custom_var,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=fonts["ui"],
            highlightthickness=0,
        )
        self.custom_entry.pack(fill="x", ipady=7, padx=(14, 0))
        self.custom_entry.bind("<Return>", lambda _e: self._add_custom_level())
        self.custom_ph = tk.Label(
            custom_box,
            text="输入档位名，回车或点按钮添加",
            bg=INPUT,
            fg=MUTED,
            font=fonts["small"],
            anchor="w",
        )
        self.custom_ph.bind("<Button-1>", lambda _e: self.custom_entry.focus_set())
        self.custom_var.trace_add("write", lambda *_: self._sync_custom_ph())
        self._sync_custom_ph()
        self._add_btn = tk.Button(
            custom_row,
            text="+ 添加自定义档",
            command=self._add_custom_level,
            bg=GHOST,
            fg=TEXT,
            activebackground=HOVER,
            activeforeground=TEXT,
            bd=0,
            font=fonts["small"],
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self._add_btn.pack(side="left", padx=(8, 0), pady=6)

        # R6-2/P0-2：map 是「单个 CEL 表达式字符串」（ZCode schema t.string()，builtin 101 处皆然）
        field_label("推理参数映射")
        self.map_text = tk.Text(
            outer,
            height=6,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=fonts["ui"],
            highlightthickness=1,
            highlightbackground=LINE2,
            wrap="none",
        )
        self.map_text.pack(fill="x")
        tk.Label(
            outer,
            text=(
                "用 CEL 表达式把当前推理档位写进请求体：表达式返回的 JSON 对象会合并到请求里，"
                "reasoningLevel 代表当前档位。"
                "示例：{\"reasoning_effort\": reasoningLevel}。留空 = 清除已有映射。"
            ),
            bg=CARD,
            fg=MUTED,
            font=fonts["small"],
            anchor="w",
            justify="left",
            wraplength=660,
        ).pack(fill="x", pady=(4, 0))
        rmap = row.get("reasoningMap")
        if rmap is None and isinstance(meta.get("reasoningMap"), (dict, str)):
            rmap = meta["reasoningMap"]
        self._map_initial = ""
        if isinstance(rmap, str) and rmap.strip():
            self._map_initial = rmap
            self.map_text.insert("1.0", rmap)
        elif isinstance(rmap, dict) and rmap:
            self._map_initial = json.dumps(rmap, ensure_ascii=False, indent=2)
            self.map_text.insert("1.0", self._map_initial)

        btn_row = tk.Frame(outer, bg=CARD)
        btn_row.pack(fill="x", pady=(20, 0))
        self.error_var = tk.StringVar(value="")
        self.error_label = tk.Label(
            btn_row, textvariable=self.error_var, bg=CARD, fg=ERR, font=fonts["small"], anchor="w"
        )
        self.error_label.pack(side="left", fill="x", expand=True)
        # R6-1：取消按钮必须 pack（原先只创建未 pack，底栏只剩保存）；P2-24：去字距
        self._white_btn(btn_row, "保存", self._save)
        self.app._ghost_btn(btn_row, "取消", self.destroy, pad=16).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def _check(self, parent: tk.Frame, text: str, var: tk.BooleanVar, disabled: bool = False) -> None:
        """经典勾选框（推理等级组专用——R6-7 豁免清单明确不 chips 化）。"""
        item = tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            bg=CARD,
            fg=MUTED if disabled else TEXT,
            selectcolor=INPUT,
            activebackground=CARD,
            activeforeground=TEXT,
            font=self.app.fonts["ui"],
            bd=0,
            highlightthickness=0,
        )
        item.pack(side="left", padx=(0, 14))
        if disabled:
            item.configure(state="disabled")

    def _chip(self, parent: tk.Frame, text: str, var: tk.BooleanVar, disabled: bool = False) -> None:
        """R6-7：官方 chips 形态——indicatoron=0 胶囊，选中底 #454545、描边加深，
        高≈48 物理px（32 逻辑）。disabled 项（文本始终）保持描边 + MUTED 字。
        Windows 的 Checkbutton(indicatoron=0) 不渲染 highlight 环（R6 验收实锤），
        描边用外层 1px Frame 容器实现。"""
        wrap = tk.Frame(parent, bg=LINE, highlightthickness=0, bd=0)
        wrap.pack(side="left", padx=(0, 8))
        chip = tk.Checkbutton(
            wrap,
            text=text,
            variable=var,
            bg=CARD,
            fg=MUTED if disabled else TEXT,
            selectcolor=CHIP_ON,
            activebackground=CARD,
            activeforeground=TEXT,
            disabledforeground=MUTED,
            font=self.app.fonts["ui"],
            bd=0,
            highlightthickness=0,
            indicatoron=0,
            padx=13,
            pady=4,
            cursor="arrow" if disabled else "hand2",
        )
        chip.pack(padx=1, pady=1)

        def refresh(*_args) -> None:
            try:
                if var.get():
                    chip.configure(bg=CHIP_ON, activebackground=CHIP_ON)
                    wrap.configure(bg=CHIP_ON_LINE)
                else:
                    chip.configure(bg=CARD, activebackground=CARD)
                    wrap.configure(bg=LINE)
            except tk.TclError:
                pass

        var.trace_add("write", refresh)
        refresh()
        if disabled:
            chip.configure(state="disabled")

    def _sync_custom_ph(self) -> None:
        # 自定义档输入框占位：有值隐藏、无值显示
        try:
            if self.custom_var.get():
                self.custom_ph.place_forget()
            else:
                self.custom_ph.place(x=14, rely=0.5, y=-1, anchor="w")
        except tk.TclError:
            pass

    def _dialog_note(self, text: str, kind: str = "err") -> None:
        # P0-3/U-2：弹窗内反馈——错误红字、成功绿字（同一行标签换色）
        self.error_var.set(text)
        try:
            self.error_label.configure(fg=ERR if kind == "err" else OK)
        except tk.TclError:
            pass

    def _white_btn(self, parent: tk.Frame, text: str, command) -> tk.Button:
        item = tk.Button(
            parent,
            text=text,
            command=command,
            bg=WHITE,
            fg=WHITE_FG,
            activebackground="#FFFFFF",
            activeforeground=WHITE_FG,
            bd=0,
            font=self.app.fonts["ui"],
            padx=18,
            pady=0,  # R6-4：实测 pady=0 → 47px（官方 41-47，原 pady=6 → 59px）
            cursor="hand2",
        )
        item.pack(side="right")
        return item

    def _add_custom_level(self, *_args) -> None:
        raw = self.custom_var.get()
        name = raw.strip()
        if not name:
            self._dialog_note("先在左侧输入要添加的档名")
            return
        if MODEL_ID_CTRL_RE.search(raw):
            self._dialog_note("档位名不能包含换行或控制字符")
            return
        if len(name) > 60:
            self._dialog_note("档位名过长（最多 60 字）")
            return
        if name in self.reason_vars:
            self._dialog_note(f"「{name}」已经存在，不用重复添加")
            return
        if name in set(REASONING_LABELS.values()):
            # C2-4：与标准档中文标签重名会把字面「低」写进 values，直接拒绝
            self._dialog_note(f"「{name}」是标准档的显示名，请直接勾选对应档位")
            return
        var = tk.BooleanVar(value=True)
        self.reason_vars[name] = var
        self._check(self._last_check_row, name, var)
        self.custom_var.set("")
        self._dialog_note(f"已添加自定义档「{name}」，记得勾选要保留的档位", "ok")

    def _save(self) -> None:
        row = self.row
        model_id = MODEL_ID_CTRL_RE.sub("", self.id_entry.get()).strip()[:200]
        if not model_id:
            self.error_var.set("模型 ID 不能为空")
            return
        # P2-25/A2-3：与行内共用同一谓词（isdecimal + int>0），"+5"/"1_000"/"²" 一律拒
        ctx_raw = self.ctx_entry.get().strip()
        if ctx_raw and not valid_positive_int(ctx_raw):
            self.error_var.set("上下文窗口需为正整数")
            return
        max_raw = self.max_entry.get().strip()
        if max_raw and not valid_positive_int(max_raw):
            self.error_var.set("最大输出需为正整数")
            return
        # R6-2：map 是单个 CEL 表达式字符串——文本原样保存，不做 JSON 解析
        # （CEL 语法校验本轮明确不做）；留空 = 清除已有映射。
        map_text = self.map_text.get("1.0", "end").strip()
        rmap = map_text or None
        checked = [level for level, var in self.reason_vars.items() if var.get()]
        if rmap and not checked:
            self.error_var.set("推理参数映射需要至少勾选一个推理档位")
            return
        # 写回行控件与元数据；真正的文件写入仍由主窗口「保存」统一完成
        row["id"].set(model_id)
        row["ctx"].set(ctx_raw)
        row["max"].set(max_raw)
        row["checked"] = checked
        row["extras"] = {
            "image": bool(self.var_image.get()),
            "video": bool(self.var_video.get()),
            "pdf": bool(self.var_pdf.get()),
            "structured": bool(self.var_structured.get()),
            "webSearch": bool(self.var_web.get()),
            "midConv": bool(self.var_midconv.get()),
        }
        row["extrasChanged"] = True
        row["reasoningMap"] = rmap
        row["mapChanged"] = True
        self.app._sync_default_drop(row, source="checks")
        self.app._update_name_col()
        update_vis = row.get("update_vis_badge")
        if update_vis is not None:
            update_vis(bool(row["extras"]["image"]))
        self.app.show_summary()
        self.app.flash_status(
            f"{model_id} · 已在编辑弹窗更新；点主窗口「保存」后写入配置",
            "guide",
        )
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("模型设置")
        self.minsize(1280, 820)
        self.configure(bg=BG)
        self._place_window(1680, 1040)
        self.providers: list[dict] = []
        self.catalog: list[dict] = []
        self.catalog_ids: list[str] = []
        self.rows: list[dict] = []
        self.fetching = False
        self.closed = False
        self.fetch_gen = 0
        self.fetch_provider = ""  # R4-P1-2：进行中 fetch 的发起供应商快照
        self._status_after = None
        self._rail_rows: dict[str, dict] = {}
        self._rail_sig: tuple = ()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self._fonts()
        self._build()
        self.after(80, self._dark_titlebar)
        self.reload_providers(select_first=True)

    def _fonts(self) -> None:
        family = "Microsoft YaHei UI"
        available = set(tkfont.families())
        if family not in available:
            family = "Segoe UI" if "Segoe UI" in available else "TkDefaultFont"
        mono = "Cascadia Mono" if "Cascadia Mono" in available else "Consolas"
        self.fonts = {
            "ui": tkfont.Font(family=family, size=11),
            # R6-3：主窗/弹窗标题拆档。校准实测（150% DPI，ink≈linespace×35/47）：
            # h1=22pt → linespace 58px → 字高≈43px 对齐官方；h2=11pt bold → 字高≈21px。
            "h1": tkfont.Font(family=family, size=22, weight="bold"),
            "h2": tkfont.Font(family=family, size=11, weight="bold"),
            "small": tkfont.Font(family=family, size=9),
            "name": tkfont.Font(family=family, size=12),
            "mono": tkfont.Font(family=family, size=11),
        }

    def _place_window(self, width: int, height: int) -> None:
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(max(width, 1280), max(1280, screen_w - 24))
        height = min(max(height, 820), max(820, screen_h - 36))
        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _dark_titlebar(self) -> None:
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
            )
        except Exception:
            pass

    def quit_app(self) -> None:
        self.closed = True
        Popup.close()
        self.destroy()

    def _build(self) -> None:
        # P0-1：上下文/最大输出列宽按字体实测，80px 定宽装不下 7 位数字（如 1000000）。
        self.col_field_w = max(COL_FIELD_W, self.fonts["ui"].measure("10000000") + 20)
        # P1-1：思考列宽按「未设置」+ 贴右箭头实测，保证箭头完整不被裁。
        self.col_drop_w = max(
            COL_DROP_W,
            self.fonts["ui"].measure("未设置")
            + self.fonts["ui"].measure("▾")
            + 24  # label padx 10×2 + border 2×2
            + 24  # caret padx 10×2 + border 2×2（P1-4 命中区加大后同步）
            + 10  # caret 贴右外边距
            + 4,  # 高亮边框 2 + 余量 2
        )
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, rowspan=2, sticky="ew", padx=36, pady=(22, 0))
        header.columnconfigure(0, weight=1)
        tk.Label(header, text="模型设置", bg=BG, fg=WHITE, font=self.fonts["h1"]).grid(
            row=0, column=0, sticky="w", pady=(2, 4)
        )
        tk.Label(
            header,
            text="管理自定义模型供应商，配置后可在聊天时选择使用。",
            bg=BG,
            fg=MUTED,
            font=self.fonts["ui"],
        ).grid(row=1, column=0, sticky="w", pady=(36, 16))  # R6-3：标题-副标题间距对齐官方（≈59px）
        actions = tk.Frame(header, bg=BG)
        actions.grid(row=1, column=1, sticky="e")
        reload_btn = self._ghost_btn(actions, "↻", self.reload_click, pad=8)
        reload_btn.pack(side="left", padx=(0, 8))
        Tooltip(reload_btn, RELOAD_TIP, self.fonts)  # R4-P1-9 [U-6]
        self._white_btn(actions, "+  添加供应商", self.reset_new).pack(side="left")

        card = tk.Frame(self, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.grid(row=2, column=0, sticky="nsew", padx=36, pady=(0, 0))
        card.columnconfigure(2, weight=1)
        card.rowconfigure(0, weight=1)
        self.card_ref = card  # P0-1：弹层底缘上翻以卡片为界

        rail = tk.Frame(card, bg=SIDE, width=320)
        rail.grid(row=0, column=0, sticky="nsew")
        rail.grid_propagate(False)
        # R6-6：rail 内部列撑满 320px，行 fill="x" 后行宽 = 320-16（rail_box padx）= 304px
        rail.columnconfigure(0, weight=1)
        rail.rowconfigure(1, weight=1)
        tk.Label(
            rail,
            text="自定义供应商",
            bg=SIDE,
            fg=MUTED,
            font=self.fonts["small"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 6))
        self.rail_box = tk.Frame(rail, bg=SIDE)
        self.rail_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 14))

        # R6-5：rail 与右栏之间补 1px #414141 竖分隔线（官方 rail 右缘有同色分隔线）
        tk.Frame(card, bg=LINE, width=1).grid(row=0, column=1, sticky="ns")

        right = tk.Frame(card, bg=CARD)
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1, minsize=280)

        head = tk.Frame(right, bg=CARD)
        head.grid(row=0, column=0, sticky="ew", padx=32, pady=(16, 4))
        self._cube_icon(head, size=16, color=TEXT).pack(side="left")
        self.name_var = tk.StringVar()
        tk.Entry(
            head,
            textvariable=self.name_var,
            bg=CARD,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=self.fonts["name"],
            highlightthickness=0,
        ).pack(side="left", fill="x", expand=True, padx=(8, 0))

        form = tk.Frame(right, bg=CARD)
        form.grid(row=1, column=0, sticky="ew", padx=32)
        form.columnconfigure(0, weight=1)
        self.base_url = tk.StringVar()
        self.api_key = tk.StringVar()
        self.api_type = tk.StringVar(value="chat")
        self.api_type_ui = tk.StringVar(value=api_type_label("chat"))
        self.provider_id = tk.StringVar()
        self._label(form, "Base URL", 0)
        self._entry(form, self.base_url, 1, mono=True)
        self._label(form, "API 格式", 2)
        self.type_drop = Drop(
            form,
            self.api_type_ui,
            [label for _, label in API_TYPE_OPTIONS],
            fonts=self.fonts,
            command=self.on_type_change,
        )
        self.type_drop.grid(row=3, column=0, sticky="ew")
        # R4-P1-9 [U-3]：API 格式下拉正下方常驻指引一行（MUTED 小字）。
        tk.Label(
            form,
            text=API_TYPE_HINT,
            bg=CARD,
            fg=MUTED,
            font=self.fonts["small"],
            anchor="w",
            justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(4, 0))
        self._label(form, "API Key", 5)
        self._key_field(form, 6)
        # R4-P0-1：API Key 行右侧的「测试连接」按钮移除——测试改为每行模型级的「测试」列，
        # 用该行模型 ID 发一次最小真实请求（见 add_row / on_test_row）。

        tools = tk.Frame(right, bg=CARD)
        tools.grid(row=2, column=0, sticky="ew", padx=32, pady=(12, 8))
        # P2-23：「模型列表」标签升为 ui 字号（官方 14px）
        tk.Label(tools, text="模型列表", bg=CARD, fg=MUTED, font=self.fonts["ui"]).pack(
            side="left"
        )
        add_btn = self._ghost_btn(tools, "+  添加模型", self.add_blank_row)
        add_btn.pack(side="right")
        Tooltip(add_btn, ADD_MODEL_TIP, self.fonts)  # R4-P1-9 [U-4]
        self.fetch_btn = self._ghost_btn(tools, "↓  获取模型列表", self.on_fetch)
        self.fetch_btn.pack(side="right", padx=(0, 8))
        Tooltip(self.fetch_btn, FETCH_TIP, self.fonts)  # R4-P1-9 [U-4]

        table = tk.Frame(right, bg=CARD)
        table.grid(row=3, column=0, sticky="nsew", padx=32, pady=(0, 12))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        wrap = tk.Frame(table, bg=CARD)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(wrap, bg=CARD, highlightthickness=0, bd=0)
        # P0-2：列表滚动条不再显示（Windows 原生白条突兀，用户拍板方案 b），
        # 滚动全靠 bind_all 滚轮 + 满屏守卫；弹层同样一律不出滚动条。
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rows_frame = tk.Frame(self.canvas, bg=CARD)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", self._on_rows_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.columns = tk.Frame(self.rows_frame, bg=CARD)
        # 图标列（删除/编辑/测试）不再放文字列头：图标语义自明，悬停有提示。
        del_head = tk.Frame(self.columns, bg=CARD, width=COL_DEL_W, height=22)
        del_head.pack_propagate(False)
        del_head.pack(side="right")
        test_head = tk.Frame(self.columns, bg=CARD, width=COL_DEL_W, height=22)
        test_head.pack_propagate(False)
        test_head.pack(side="right", padx=(0, 8))
        edit_head = tk.Frame(self.columns, bg=CARD, width=COL_DEL_W, height=22)
        edit_head.pack_propagate(False)
        edit_head.pack(side="right", padx=(0, 8))
        # side=right 的 pack 是从右往左堆：推理等级、默认档位、最大输出、上下文。
        # U-3：统一命名「思考深度」→「推理等级」。
        self._drop_head("推理等级", REASON_HEAD_TIP)
        self._drop_head("默认档位", DEFAULT_HEAD_TIP)
        for head_text in ("最大输出", "上下文"):
            field_head = tk.Frame(self.columns, bg=CARD, width=self.col_field_w, height=22)
            field_head.pack_propagate(False)
            field_head.pack(side="right", padx=(0, 8))
            head_label = tk.Label(
                field_head, text=head_text, bg=CARD, fg=MUTED, font=self.fonts["small"]
            )
            head_label.pack(expand=True)
            Tooltip(head_label, NUM_HEAD_TIP, self.fonts)  # R4-P1-9 [U-2]

        self.empty = tk.Label(
            table,
            # R4-P0-2：未获取目录前没有下拉，空态文案不再误导
            text="还没有模型。点「获取模型列表」拉取候选，或点「+ 添加模型」手动填写。",
            bg=CARD,
            fg=MUTED,
            font=self.fonts["ui"],
        )

        footer = tk.Frame(self, bg=BG)
        footer.grid(row=3, column=0, sticky="ew", padx=36, pady=(12, 16))
        footer.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="选择左侧供应商，或点右上角添加。")
        self.status_label = tk.Label(
            footer,
            textvariable=self.status,
            bg=BG,
            fg=MUTED,
            font=self.fonts["small"],
            anchor="w",
            justify="left",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self._white_btn(footer, "保存", self.on_save).grid(row=0, column=1, padx=(16, 0))
        self.bind_all("<MouseWheel>", self.on_wheel, add="+")

    def _drop_head(self, text: str, tip: str) -> None:
        head = tk.Frame(self.columns, bg=CARD, width=self.col_drop_w, height=22)
        head.pack_propagate(False)
        head.pack(side="right", padx=(0, 8))
        mark = tk.Label(
            head, text="?", bg=CARD, fg=MUTED, font=self.fonts["small"], cursor="hand2"
        )
        mark.pack(side="right", padx=(2, 4))
        label = tk.Label(head, text=text, bg=CARD, fg=MUTED, font=self.fonts["small"])
        label.pack(side="left", expand=True, fill="x")
        Tooltip(mark, tip, self.fonts)
        Tooltip(label, tip, self.fonts)

    def _mini_field(self, parent: tk.Misc, var: tk.StringVar) -> tk.Frame:
        box = tk.Frame(parent, bg=ROW, width=self.col_field_w)
        box.pack_propagate(False)
        entry = tk.Entry(
            box,
            textvariable=var,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=self.fonts["ui"],
            highlightthickness=1,
            highlightbackground=LINE2,
            width=8,
        )
        entry.pack(fill="both", expand=True, ipady=6, padx=(6, 0))
        # 容器冻结了 propagate，必须显式给高度，否则请求高度塌成 1px（D1 回归）。
        # 高度取两者较大者（均实测、勿拍脑袋）：tkfont.metrics 公式 linespace+2*ipady+2*border，
        # 以及 Entry 真实请求高度+2*ipady（Win 下 Entry 自身比公式多 2px，叶子控件创建即有效）。
        linespace = int(round(float(self.fonts["ui"].metrics("linespace"))))
        border = int(str(entry.cget("borderwidth"))) + int(str(entry.cget("highlightthickness")))
        self._field_h = max(linespace + 2 * 6 + 2 * border, entry.winfo_reqheight() + 2 * 6)
        box.configure(height=self._field_h)
        box.numentry = entry  # R4-P1-6：红框校验/占位符需要 entry 引用
        return box

    def _label(self, parent: tk.Frame, text: str, row: int) -> None:
        # P2-23/R6-11：表单标签升为 ui 字号（官方 14px），MUTED 色不变
        tk.Label(
            parent, text=text, bg=CARD, fg=MUTED, font=self.fonts["ui"], anchor="w"
        ).grid(row=row, column=0, sticky="w", pady=(6, 3))

    def _entry(self, parent: tk.Frame, var: tk.StringVar, row: int, mono: bool = False) -> tk.Entry:
        # P1-3：与 _key_field 同款「外框+内 entry」结构，文字左边距统一为 10px，不贴边框。
        box = tk.Frame(parent, bg=INPUT, highlightbackground=LINE2, highlightthickness=1)
        box.grid(row=row, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        entry = tk.Entry(
            box,
            textvariable=var,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=self.fonts["ui"],
            highlightthickness=0,
        )
        entry.grid(row=0, column=0, sticky="ew", ipady=7, padx=(14, 0))
        return entry

    def _key_field(self, parent: tk.Frame, row: int) -> None:
        box = tk.Frame(parent, bg=INPUT, highlightbackground=LINE2, highlightthickness=1)
        box.grid(row=row, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self.key_entry = tk.Entry(
            box,
            textvariable=self.api_key,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=self.fonts["ui"],
            show="•",
            highlightthickness=0,
        )
        self.key_entry.grid(row=0, column=0, sticky="ew", ipady=7, padx=(14, 0))
        self.key_shown = False
        eye = tk.Canvas(box, width=20, height=14, bg=INPUT, highlightthickness=0, cursor="hand2")
        eye.create_oval(2, 3, 18, 11, outline=MUTED)
        self.eye_pupil = eye.create_oval(8, 5, 12, 9, fill=MUTED, outline="")
        eye.bind("<Button-1>", lambda _e: self.toggle_key())
        eye.grid(row=0, column=1, padx=(0, 10))
        self.key_eye = eye

    def _white_btn(self, parent: tk.Misc, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=WHITE,
            fg=WHITE_FG,
            activebackground="#ffffff",
            activeforeground=WHITE_FG,
            bd=0,
            font=self.fonts["ui"],
            padx=14,
            pady=0,  # R6-4：实测 pady=0 → 47px（官方 41-47，原 pady=7 → 61px）
            cursor="hand2",
        )

    def _ghost_btn(self, parent: tk.Misc, text: str, command, pad: int = 12) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=GHOST,
            fg=TEXT,
            activebackground="#333333",
            activeforeground=TEXT,
            bd=0,
            font=self.fonts["ui"],
            padx=pad,
            pady=0,  # R6-4：与白钮同档（实测 47px）
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=LINE2,
        )

    def _cube_icon(self, parent: tk.Misc, size: int = 14, color: str = MUTED) -> tk.Canvas:
        canvas = tk.Canvas(
            parent, width=size, height=size, bg=parent.cget("bg"), highlightthickness=0
        )
        s = float(size)
        canvas._poly = canvas.create_polygon(  # noqa: SLF001 — 左栏刷选中时改描边，不重建 Canvas
            0.5 * s, 0.07 * s,
            0.93 * s, 0.28 * s,
            0.93 * s, 0.72 * s,
            0.5 * s, 0.93 * s,
            0.07 * s, 0.72 * s,
            0.07 * s, 0.28 * s,
            fill="",
            outline=color,
        )
        canvas._lines = (  # noqa: SLF001
            canvas.create_line(0.5 * s, 0.5 * s, 0.07 * s, 0.28 * s, fill=color),
            canvas.create_line(0.5 * s, 0.5 * s, 0.93 * s, 0.28 * s, fill=color),
            canvas.create_line(0.5 * s, 0.5 * s, 0.5 * s, 0.93 * s, fill=color),
        )
        return canvas

    def toggle_key(self) -> None:
        self.key_shown = not self.key_shown
        self.key_entry.configure(show="" if self.key_shown else "•")
        self.key_eye.itemconfigure(self.eye_pupil, fill=TEXT if self.key_shown else MUTED)

    def on_type_change(self, label: str) -> None:
        for key, name in API_TYPE_OPTIONS:
            if name == label:
                self.api_type.set(key)
                return

    # ---- P0-3：状态栏 = 常驻摘要 + 限时瞬时消息 ----

    def _summary_text(self) -> str:
        added = sum(1 for row in self.rows if row["id"].get().strip())
        if self.catalog_ids:
            name = self.name_var.get().strip() or "未命名供应商"
            # R4-P1-9 [U-7]：「目录」→「候选」，不再被误读成已保存清单
            return f"{name} · 候选 {len(self.catalog_ids)} · 已添加 {added}"
        return f"已添加 {added} 个模型"

    def show_summary(self) -> None:
        self._cancel_status_timer()
        self.status.set(self._summary_text())
        self.status_label.configure(fg=MUTED)

    def _cancel_status_timer(self) -> None:
        if getattr(self, "_status_after", None):
            try:
                self.after_cancel(self._status_after)
            except Exception:  # noqa: BLE001
                pass
            self._status_after = None

    def flash_status(self, text: str, kind: str = "ok") -> None:
        """kind：ok=绿 8s 回落摘要；guide=MUTED 6s 回落；err=红 常驻到下次操作；
        hold=进行中提示不自动回落。after 回调带防重入（先取消旧定时器）。"""
        self._cancel_status_timer()
        self.status.set(text)
        self.status_label.configure(fg={"ok": OK, "err": ERR}.get(kind, MUTED))
        if kind == "ok":
            self._status_after = self.after(8000, self.show_summary)
        elif kind == "guide":
            self._status_after = self.after(6000, self.show_summary)

    def _on_rows_configure(self, _event=None) -> None:
        region = self.canvas.bbox("all") or (0, 0, 0, 0)
        self.canvas.configure(scrollregion=region)
        if self.rows:
            self.columns.pack(
                side="top", fill="x", padx=13, pady=(0, 2), before=self.rows[0]["frame"]
            )
            self.empty.place_forget()
        else:
            self.columns.pack_forget()
            self.empty.place(relx=0.5, rely=0.45, anchor="center")

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def on_wheel(self, event) -> None:
        if Popup.current is not None:
            return None
        first, last = self.canvas.yview()
        if first <= 0 and last >= 1:
            return None
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def reload_click(self) -> None:
        current = self.provider_id.get()
        catalog, ids = list(self.catalog), list(self.catalog_ids)
        self.reload_providers()
        if current:
            self.select_provider(current, keep_catalog=True)
            self.catalog, self.catalog_ids = catalog, ids
            self.refresh_row_catalog()
        self.flash_status("已重新读取 ZCode 配置。", "ok")

    def reload_providers(self, select_first: bool = False) -> None:
        error = None
        try:
            data = load_config(DEFAULT_CONFIG)
            self.providers = list_provider_summaries(data, include_key=True)
        except SyncError as exc:
            self.providers = []
            error = str(exc)
        except (AttributeError, TypeError) as exc:
            # R4-P0-3 [C-1] 兜底：畸形配置的意外类型错误转中文提示，启动/刷新不崩
            self.providers = []
            error = f"ZCode 配置文件读取异常，为安全起见未做修改：{exc!r}"
        self.render_rail()
        if error:
            self.flash_status(error, "err")
        elif not self.providers:
            # R4-P1-9 [U-8]：首启空态改为可执行的引导文案
            self.flash_status(EMPTY_PROVIDERS_GUIDE, "guide")
        if select_first and self.providers:
            self.select_provider(self.providers[0]["providerId"])

    def _rail_signature(self) -> tuple:
        # P2-21：签名含 enabled——供应商禁用状态变化时左栏要重绘状态点
        return tuple(
            (
                item.get("providerId"),
                item.get("providerName") or "",
                bool(item.get("enabled", True)),
            )
            for item in self.providers
        )

    def render_rail(self) -> None:
        signature = self._rail_signature()
        if signature == self._rail_sig and self._rail_rows:
            self._paint_rail_selection()
            return
        for child in self.rail_box.winfo_children():
            child.destroy()
        self._rail_rows = {}
        for item in self.providers:
            pid = item.get("providerId")
            row = tk.Frame(
                self.rail_box,
                bg=SIDE,
                highlightthickness=0,
                highlightbackground=SEL,
                cursor="hand2",
            )
            row.pack(fill="x", pady=1)
            inner = tk.Frame(row, bg=SIDE)
            inner.pack(fill="x", padx=10, pady=9)
            icon = self._cube_icon(inner, size=14, color=MUTED)
            icon.pack(side="left")
            name = tk.Label(
                inner,
                text=item.get("providerName") or "",
                bg=SIDE,
                fg=TEXT,
                font=self.fonts["ui"],
                anchor="w",
            )
            name.pack(side="left", padx=8, fill="x", expand=True)
            # P2-21：状态点两态——供应商 enabled=False → 灰点，否则绿点
            provider_enabled = item.get("enabled", True) is not False
            dot = tk.Label(
                inner, text="●", bg=SIDE, fg=GREEN if provider_enabled else MUTED,
                font=self.fonts["small"],
            )
            dot.pack(side="right")
            stored = {"frame": row, "inner": inner, "icon": icon, "name": name, "dot": dot}
            if pid:
                self._rail_rows[str(pid)] = stored
            for widget in (row, inner, icon, name, dot):
                widget.bind("<Button-1>", lambda _e, i=pid: self.select_provider(i))
        self._rail_sig = signature
        self._paint_rail_selection()

    def _paint_rail_selection(self) -> None:
        current = self.provider_id.get()
        for pid, stored in self._rail_rows.items():
            selected = pid == current
            color = TEXT if selected else MUTED
            try:
                stored["frame"].configure(highlightthickness=1 if selected else 0)
                icon = stored["icon"]
                icon.itemconfigure(icon._poly, outline=color)  # noqa: SLF001
                for line in icon._lines:  # noqa: SLF001
                    icon.itemconfigure(line, fill=color)
            except tk.TclError:
                continue

    def reset_new(self) -> None:
        Popup.close()
        self.provider_id.set("")
        self.name_var.set("新供应商")
        self.base_url.set("")
        self.api_key.set("")
        self.api_type.set("chat")
        self.api_type_ui.set(api_type_label("chat"))
        self.catalog = []
        self.catalog_ids = []
        self.clear_rows()
        self.add_blank_row()
        self.render_rail()
        self.flash_status("新建供应商。填写后获取模型列表，从下拉选择再保存。", "guide")

    def select_provider(self, provider_id: str, *, keep_catalog: bool = False) -> None:
        Popup.close()
        item = next((p for p in self.providers if p.get("providerId") == provider_id), None)
        if not item:
            return
        self.provider_id.set(provider_id)
        self.name_var.set(item.get("providerName") or "")
        self.base_url.set(item.get("baseUrl") or "")
        self.api_key.set(item.get("apiKey") or "")
        short = api_type_short(item.get("apiType"))
        self.api_type.set(short)
        self.api_type_ui.set(api_type_label(short))
        if not keep_catalog:
            self.catalog = []
            self.catalog_ids = []
        self.clear_rows()
        models = item.get("models") or []
        if models:
            for rec in models:
                self.add_row(rec)
        else:
            self.add_blank_row()
        self.render_rail()
        self.show_summary()

    def clear_rows(self) -> None:
        for row in self.rows:
            row["frame"].destroy()
        self.rows = []
        self._on_rows_configure()

    def add_blank_row(self) -> None:
        self.add_row({"id": "", "contextWindow": "", "reasoning": ""})

    def _catalog_ctx(self, model_id: str):
        for rec in self.catalog:
            if rec.get("id") == model_id:
                return rec.get("contextWindow")
        return None

    def _name_col_width(self, extra: str = "") -> int:
        """P1-3：名字列宽 = 各行模型名最大实测宽 + caret 区，clamp [140, 260]，全行共用。"""
        text_w = 0
        for row in self.rows:
            value = row["id"].get().strip()
            if value:
                text_w = max(text_w, self.fonts["ui"].measure(value))
        if extra.strip():
            text_w = max(text_w, self.fonts["ui"].measure(extra.strip()))
        chrome = self.fonts["ui"].measure("▾") + 20 + 10 + 8  # caret 字形 + padx 10×2 + 右缘 10 + 余量
        return max(140, min(260, int(text_w + chrome)))

    def _update_name_col(self) -> None:
        width = self._name_col_width()
        for row in self.rows:
            row["id_box"].configure(width=width)

    def add_row(self, rec: dict) -> None:
        frame = tk.Frame(self.rows_frame, bg=ROW, highlightbackground=LINE, highlightthickness=1)
        frame.pack(fill="x", pady=4)
        inner = tk.Frame(frame, bg=ROW)
        inner.pack(fill="x", padx=12, pady=6)

        id_var = tk.StringVar(value=rec.get("id") or "")
        ctx_var = tk.StringVar(value="" if rec.get("contextWindow") in (None, "") else str(rec.get("contextWindow")))
        # A2-5：记录原始最大输出文本——清空已有值再保存 = 删除 maxOutputTokens 的依据
        max_original = "" if rec.get("maxOutputTokens") in (None, "") else str(rec.get("maxOutputTokens"))
        max_var = tk.StringVar(value=max_original)
        checked = self._levels_from_rec(rec)
        default_level = str(rec.get("reasoningDefault") or "")
        reason_var = tk.StringVar(value=self._reason_summary(checked))
        default_var = tk.StringVar(value=self._level_label(default_level, DEFAULT_FOLLOW))
        # row dict 先建后填：各控件构建函数/回调都按 row 引用取变量（重建名字列不丢编辑）。
        row: dict = {
            "frame": frame,
            "id": id_var,
            "ctx": ctx_var,
            "max": max_var,
            "max_original": max_original,
            "reason": reason_var,
            "checked": checked,
            "default": default_var,
            "meta": rec,
        }

        # 右侧各列先建：_field_h 由 _mini_field 实测得出，左侧名字列定宽需要它。
        right = tk.Frame(inner, bg=ROW)
        right.pack(side="right")
        ctx_box = self._mini_field(right, ctx_var)
        ctx_box.pack(side="left", fill="y", padx=(0, 8))
        max_box = self._mini_field(right, max_var)
        max_box.pack(side="left", fill="y", padx=(0, 8))
        # 顺序：最大输出 → 默认档位 → 思考深度。高度与数字列同一 col_h。
        default_box = tk.Frame(right, bg=ROW, width=self.col_drop_w, height=self._field_h)
        default_box.pack_propagate(False)
        default_box.pack(side="left", fill="y", padx=(0, 8))
        default_drop = Drop(
            default_box,
            default_var,
            [DEFAULT_FOLLOW],
            fonts=self.fonts,
            command=lambda v, r=row: self._on_default_picked(r, v),
        )
        default_drop.pack(fill="both", expand=True)
        reason_box = tk.Frame(right, bg=ROW, width=self.col_drop_w, height=self._field_h)
        reason_box.pack_propagate(False)
        reason_box.pack(side="left", fill="y", padx=(0, 8))
        reason = Drop(
            reason_box,
            reason_var,
            [],
            fonts=self.fonts,
            note_fn=lambda r=row: self._reason_note(r),
        )
        reason.open = lambda r=row: self._open_reason_checks(r)  # type: ignore[method-assign]
        reason.pack(fill="both", expand=True)
        label_h = reason.label.winfo_reqheight()
        drop_hl = int(str(reason.cget("highlightthickness")))
        col_h = max(self._field_h, label_h + 2 * 6 + 2 * drop_hl)
        for box in (default_box, reason_box):
            box.configure(height=col_h)
        row["default_drop"] = default_drop
        row["reason_drop"] = reason
        # R4-P0-1：每行「测试」列（思考与删除之间），ghost 样式与删除列同风格
        test_box = tk.Frame(right, bg=ROW, width=COL_DEL_W, height=col_h)
        test_box.pack_propagate(False)
        test_box.pack(side="left", fill="y", padx=(0, 8))
        row["test_btn"] = IconBtn(
            test_box, "test", lambda r=row: self.on_test_row(r),
            "测试连接：用该行模型发一次最小请求", self.fonts,
        )
        row["test_btn"].pack(expand=True)
        edit_box = tk.Frame(right, bg=ROW, width=COL_DEL_W, height=col_h)
        edit_box.pack_propagate(False)
        edit_box.pack(side="left", fill="y", padx=(0, 8))
        IconBtn(
            edit_box, "edit", lambda r=row: self.on_edit_row(r),
            "编辑模型配置（输入类型 / 能力 / 推理等级 / 参数映射）", self.fonts,
        ).pack(expand=True)
        del_box = tk.Frame(right, bg=ROW, width=COL_DEL_W, height=col_h)
        del_box.pack_propagate(False)
        del_box.pack(side="left", fill="y")
        IconBtn(
            del_box, "delete", lambda f=frame: self.remove_row(f),
            "删除该行", self.fonts,
        ).pack(expand=True)
        row["ctx_box"] = ctx_box
        row["max_box"] = max_box

        # P1-3：模型名列放进定宽容器（宽 = 各行名字最大实测宽 + caret 区，clamp [140,260]，
        # 全行共用），caret 贴容器右缘、徽章起点不再随名字长度漂移。
        left = tk.Frame(inner, bg=ROW)
        left.pack(side="left", fill="x", expand=True)
        id_box = tk.Frame(
            left, bg=ROW, width=self._name_col_width(id_var.get()), height=self._field_h
        )
        id_box.pack_propagate(False)
        id_box.pack(side="left")
        row["id_box"] = id_box
        row["name_mode"] = None

        badges = tk.Frame(left, bg=ROW)
        badges.pack(side="left", padx=(10, 0))
        ctx_badge = tk.Label(
            badges, text="", bg=GHOST, fg=MUTED, font=self.fonts["small"], padx=6, pady=1
        )
        vis_badge = tk.Label(
            badges, text="视觉", bg=GHOST, fg=MUTED, font=self.fonts["small"], padx=6, pady=1
        )
        if rec.get("supportsImage"):
            vis_badge.pack(side="left")

        def fmt_badge(value: str) -> str:
            value = value.strip()
            if not value:
                return ""
            try:
                number = int(value)
            except ValueError:
                return value
            if number >= 1_000_000:  # R4-P1-9 [U-2]：≥1M 显示「1M」，可读性优先
                return f"{number // 1_000_000}M"
            return f"{number // 1000}K" if number >= 1000 else str(number)

        def sync_badge(_var=None, _idx=None, _mode=None) -> None:
            # P1-3：徽章与上下文输入框实时联动（trace ctx_var）——空隐藏，数值按 K/M 格式刷新。
            text = fmt_badge(ctx_var.get())
            ctx_badge.pack_forget()
            if text:
                ctx_badge.configure(text=text)
                if vis_badge.winfo_manager():
                    ctx_badge.pack(side="left", padx=(0, 6), before=vis_badge)
                else:
                    ctx_badge.pack(side="left", padx=(0, 6))

        def update_vis_badge(show: bool) -> None:
            # 编辑弹窗切换「图片」输入类型后同步视觉徽章。
            if show:
                vis_badge.pack(side="left")
            else:
                vis_badge.pack_forget()
            sync_badge()

        ctx_var.trace_add("write", sync_badge)
        sync_badge()
        row["vis_badge"] = vis_badge
        row["update_vis_badge"] = update_vis_badge

        self.rows.append(row)
        self._sync_default_drop(row, source="load")
        # R4-P1-9 [U-2]：上下文/最大输出占位提示；R4-P1-6：失焦校验；R4-P0-2：名字列按目录状态构建
        self._attach_placeholder(ctx_box, ctx_var, CTX_PLACEHOLDER)
        self._attach_placeholder(max_box, max_var, MAX_PLACEHOLDER)
        ctx_box.numentry.bind(
            "<FocusOut>", lambda _e, r=row: self._on_num_focus_out(r), add="+"
        )
        max_box.numentry.bind(
            "<FocusOut>", lambda _e, r=row: self._on_num_focus_out(r), add="+"
        )
        id_var.trace_add("write", lambda *_: self._sync_name_ph(row))
        self._build_name_field(row)
        self._update_name_col()
        self.show_summary()
        self.after(10, self._on_rows_configure)

    def _model_drop_empty_text(self) -> str:
        # R4-P1-9 [U-5]：弹层空态区分——未获取过目录 vs 已获取但搜索无结果
        return NO_CANDIDATE_TEXT if not self.catalog_ids else NO_MATCH_TEXT

    def _build_name_field(self, row: dict) -> None:
        """R4-P0-2：模型名字段按目录状态切换——catalog_ids 为空 → 平铺 Entry
        （MUTED 占位「输入模型 ID，或点获取模型列表」）；非空 → 可搜索 Drop。
        重建复用同一 StringVar（row["id"]），两个状态来回切换不丢已填内容。"""
        for key in ("drop", "name_entry", "name_ph"):
            old = row.get(key)
            if old is not None:
                try:
                    old.destroy()
                except tk.TclError:
                    pass
                row[key] = None
        id_var = row["id"]
        id_box = row["id_box"]
        if self.catalog_ids:
            values = list(self.catalog_ids)
            current = id_var.get().strip()
            if current and current not in values:
                values = [current, *values]
            drop = Drop(
                id_box,
                id_var,
                values,
                fonts=self.fonts,
                searchable=True,
                command=lambda _v, r=row: self.on_model_picked(r["id"], r["ctx"]),
                empty_fn=self._model_drop_empty_text,
                placeholder="选择模型",
            )
            drop.configure(bg=ROW, highlightthickness=0)
            drop.label.configure(bg=ROW, font=self.fonts["ui"], padx=0)
            drop.caret.configure(bg=ROW)
            drop.pack(side="left", fill="both", expand=True)
            row["drop"] = drop
            row["name_mode"] = "drop"
        else:
            entry = tk.Entry(
                id_box,
                textvariable=id_var,
                bg=ROW,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                highlightthickness=0,
                font=self.fonts["ui"],
            )
            entry.pack(side="left", fill="both", expand=True)
            ph = tk.Label(
                id_box,
                text=NAME_PLACEHOLDER,
                bg=ROW,
                fg=MUTED,
                font=self.fonts["ui"],
                anchor="w",
            )
            ph.bind(
                "<Button-1>", lambda _e, e=entry: (e.focus_set(), "break")[1]
            )
            row["name_entry"] = entry
            row["name_ph"] = ph
            row["name_mode"] = "entry"
            self._sync_name_ph(row)

    def _sync_name_ph(self, row: dict) -> None:
        # 名字 Entry 占位：有值隐藏、无值显示（R4-P0-2）
        ph = row.get("name_ph")
        if ph is None:
            return
        try:
            if not ph.winfo_exists():
                return
            if row["id"].get():
                ph.place_forget()
            else:
                ph.place(x=0, rely=0.5, y=-1, anchor="w")
        except tk.TclError:
            pass

    def _attach_placeholder(self, box: tk.Frame, var: tk.StringVar, text: str) -> None:
        """R4-P1-9 [U-2]：变量为空时叠加 MUTED 占位文案（纯展示层，点击落到输入框）。"""
        entry = box.numentry
        ph = tk.Label(
            box, text=text, bg=INPUT, fg=MUTED, font=self.fonts["small"], anchor="w"
        )

        def sync(_var=None, _idx=None, _mode=None) -> None:
            try:
                if not ph.winfo_exists():
                    return
                if var.get():
                    ph.place_forget()
                else:
                    ph.place(x=7, rely=0.5, y=-1, anchor="w")
            except tk.TclError:
                pass

        ph.bind("<Button-1>", lambda _e: entry.focus_set())
        var.trace_add("write", sync)
        sync()

    # ---- R4-P1-6 [C-6]：上下文/最大输出 输入侧校验（非正整数 → 红框 + 提示，不静默替换）----

    @staticmethod
    def _num_error(field: str, model_id: str) -> str:
        sample = "1000000" if field == "上下文" else "128000"
        return f"{field}必须是正整数（如 {sample}）：{model_id}"

    @staticmethod
    def _mark_num_box(box: tk.Frame, bad: bool) -> None:
        entry = getattr(box, "numentry", None)
        if entry is None:
            return
        try:
            entry.configure(highlightbackground=ERR if bad else LINE2)
        except tk.TclError:
            pass

    def _row_num_error(self, row: dict, *, require_id: bool = False) -> str | None:
        """校验一行上下文/最大输出：返回第一条中文错误（并标红框），全合法返回 None。空 = 不写。

        require_id：保存路径用。空 ID 行也会被 collected_models 跳过，但非法数字仍要拦截。
        """
        try:
            if not row["frame"].winfo_exists():
                return None
        except tk.TclError:
            return None
        model_id = MODEL_ID_CTRL_RE.sub("", row["id"].get()).strip()[:200]
        if require_id and not model_id:
            model_id = "（未选模型）"
        elif not model_id:
            model_id = "（未命名行）"
        error = None
        for field, var, box in (
            ("上下文", row["ctx"], row["ctx_box"]),
            ("最大输出", row["max"], row["max_box"]),
        ):
            raw = var.get().strip()
            bad = bool(raw) and not valid_positive_int(raw)
            self._mark_num_box(box, bad)
            if bad and error is None:
                error = self._num_error(field, model_id)
        return error

    def _on_num_focus_out(self, row: dict) -> None:
        # 失焦校验：非法值红框 + 状态栏提示，不静默替换
        error = self._row_num_error(row)
        if error:
            self.flash_status(error, "err")

    def on_model_picked(self, id_var: tk.StringVar, ctx_var: tk.StringVar) -> None:
        ctx = self._catalog_ctx(id_var.get())
        if ctx:
            ctx_var.set(str(ctx))
        self._update_name_col()
        self.show_summary()

    # ---- 思考深度勾选 / 默认档：只改变量，不写配置 ----

    @staticmethod
    def _level_label(level: str, empty_label: str) -> str:
        if not level:
            return empty_label
        return REASONING_LABELS.get(level, level)

    @staticmethod
    def _label_to_level(label: str, empty_label: str) -> str:
        if not label or label == empty_label:
            return ""
        return LABEL_TO_LEVEL.get(label, label)

    @staticmethod
    def _levels_from_rec(rec: dict) -> list[str]:
        values = rec.get("reasoningValues")
        if isinstance(values, list) and values:
            return [str(item) for item in values]
        return []

    @staticmethod
    def _reason_summary(checked: list[str]) -> str:
        if not checked:
            return REASON_UNSET
        labels = [REASONING_LABELS.get(level, level) for level in checked]
        if len(labels) <= 3:
            return "、".join(labels)
        return f"{labels[0]}…{labels[-1]}（{len(labels)}）"

    def _sync_default_drop(self, row: dict, source: str = "") -> None:
        checked = [str(item) for item in row.get("checked") or []]
        row["reason"].set(self._reason_summary(checked))
        default_drop = row.get("default_drop")
        # 默认档位始终可直接选：选项含全部标准档，选了还没勾的档由 _on_default_picked 自动补勾。
        options = [DEFAULT_FOLLOW]
        seen: set[str] = set()
        for level in [*REASONING_LADDER, *checked]:
            if level in seen:
                continue
            seen.add(level)
            options.append(REASONING_LABELS.get(level, level))
        current = row["default"].get()
        current_level = self._label_to_level(current, DEFAULT_FOLLOW)
        if current_level and current_level not in checked and source != "default":
            # 勾选变化把原默认档移出了思考深度：收回「随最高勾选」
            row["default"].set(DEFAULT_FOLLOW)
        elif current and current not in options:
            options.append(current)
        if default_drop is not None:
            default_drop.set_options(options)
            default_drop.set_enabled(True)

    def _on_default_picked(self, row: dict, label: str) -> None:
        level = self._label_to_level(label, DEFAULT_FOLLOW)
        checked = [str(item) for item in row.get("checked") or []]
        if level and level not in checked:
            checked.append(level)
            checked.sort(
                key=lambda item: REASONING_LADDER.index(item)
                if item in REASONING_LADDER
                else len(REASONING_LADDER)
            )
            row["checked"] = checked
        if not level:
            row["default"].set(DEFAULT_FOLLOW)
        else:
            row["default"].set(self._level_label(level, DEFAULT_FOLLOW))
        self._sync_default_drop(row, source="default")
        ordered = self._ordered_checks(row)
        labels = [REASONING_LABELS.get(item, item) for item in ordered]
        model_id = row["id"].get().strip() or "该模型"
        if not labels:
            # U-7：全没勾时不输出悬空的「聊天可切换 」
            self.flash_status(f"{model_id} · 默认档位：未勾选任何档，保存后不写入推理等级", "guide")
            return
        tail = REASONING_LABELS.get(level, level) if level else "勾选里的最高档"
        self.flash_status(
            f"{model_id} · 默认档位：新建会话用「{tail}」；聊天可切换 {' / '.join(labels)}，保存后生效",
            "guide",
        )

    def _ordered_checks(self, row: dict) -> list[str]:
        """勾选结果：标准档按梯序，非标准档保持勾选列表原相对顺序，默认档固定在末尾。"""
        checked_list = [str(item) for item in row.get("checked") or []]
        selected = set(checked_list)
        original = [str(item) for item in (row["meta"].get("reasoningValues") or [])]
        ordered = [level for level in REASONING_LADDER if level in selected]
        for item in original:
            if item in selected and item not in ordered:
                ordered.append(item)
        # A2-6：补位循环按 row["checked"] 列表序（set 迭代序跨进程不稳定）
        for item in checked_list:
            if item not in ordered:
                ordered.append(item)
        default = self._label_to_level(row["default"].get(), DEFAULT_FOLLOW)
        if default and default in ordered and ordered[-1] != default:
            ordered.remove(default)
            ordered.append(default)
        return ordered

    def _reason_note(self, row: dict) -> str:
        ordered = self._ordered_checks(row)
        if not ordered:
            return "未勾选：不写入推理等级"
        labels = [REASONING_LABELS.get(level, level) for level in ordered]
        return "聊天时可切换：" + " / ".join(labels)

    def _open_reason_checks(self, row: dict) -> None:
        Popup.close()
        anchor = row["reason_drop"]
        anchor.update_idletasks()
        popup = tk.Toplevel(anchor)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(bg=LINE2)
        popup.attributes("-topmost", True)
        Popup.current = popup
        inner = tk.Frame(popup, bg=POPUP_BG, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(
            inner,
            text="勾选这个模型支持的档即可，不必连续；只保存勾上的。",
            bg=POPUP_BG,
            fg=MUTED,
            font=self.fonts["small"],
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 4))
        vars_by_level: dict[str, tk.BooleanVar] = {}
        selected = {str(item) for item in row.get("checked") or []}
        extras = [item for item in selected if item not in REASONING_LADDER]
        for level in [*REASONING_LADDER, *extras]:
            var = tk.BooleanVar(value=level in selected)
            vars_by_level[level] = var
            tk.Checkbutton(
                inner,
                text=REASONING_LABELS.get(level, level),
                variable=var,
                bg=POPUP_BG,
                fg=TEXT,
                selectcolor=INPUT,
                activebackground=POPUP_BG,
                activeforeground=TEXT,
                font=self.fonts["ui"],
                anchor="w",
                bd=0,
                highlightthickness=0,
            ).pack(fill="x", padx=8)

        def apply_checks() -> None:
            row["checked"] = [level for level, var in vars_by_level.items() if var.get()]
            self._sync_default_drop(row, source="checks")
            Popup.close()
            labels = [REASONING_LABELS.get(level, level) for level in self._ordered_checks(row)]
            model_id = row["id"].get().strip() or "该模型"
            if not labels:
                # U-7：修「不写入配置却生效」的自相矛盾
                self.flash_status(f"已清空 {model_id} 的推理等级：保存后从配置移除", "guide")
                return
            self.flash_status(
                f"{model_id} · 推理等级：聊天时可切换 {' / '.join(labels)}，保存后生效",
                "guide",
            )

        tk.Button(
            inner,
            text="确定",
            command=apply_checks,
            bg=INPUT,
            fg=TEXT,
            activebackground=HOVER,
            activeforeground=TEXT,
            bd=0,
            font=self.fonts["ui"],
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(anchor="e", padx=8, pady=(4, 8))
        popup.update_idletasks()
        width = max(anchor.winfo_width(), 280)
        height = popup.winfo_reqheight()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height()
        screen_h = popup.winfo_screenheight()
        if y + height > screen_h - 8:
            y = max(8, anchor.winfo_rooty() - height)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.deiconify()
        popup.lift()
        popup.focus_force()
        popup.bind("<Escape>", lambda _e: Popup.close())
        # C2-2：与 Drop 弹层同机制——焦点离开 120ms 后复查，不在弹层子树内即关闭
        popup.bind(
            "<FocusOut>",
            lambda _e: popup.after(120, lambda: Drop._close_if_focus_outside(popup)),
        )

    def remove_row(self, frame: tk.Frame) -> None:
        self.rows = [row for row in self.rows if row["frame"] is not frame]
        frame.destroy()
        if not self.rows:
            self.add_blank_row()
        self._update_name_col()
        self._on_rows_configure()
        self.show_summary()

    def refresh_row_catalog(self) -> None:
        # R4-P0-2：目录状态变化时按需重建名字字段（Entry↔Drop），已有目录时仅更新候选
        for row in self.rows:
            want = "drop" if self.catalog_ids else "entry"
            if row.get("name_mode") != want:
                self._build_name_field(row)
                continue
            if want == "drop":
                current = row["id"].get()
                values = list(self.catalog_ids)
                if current and current not in values:
                    values = [current, *values]
                row["drop"].set_options(values)

    def collected_models(self) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for row in self.rows:
            # R4-P1-10 [C-12]：入库前剥离换行/控制字符、strip、限长 200（与 sync.save_provider 同规）
            model_id = MODEL_ID_CTRL_RE.sub("", row["id"].get()).strip()[:200]
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            ctx_raw = row["ctx"].get().strip()
            max_raw = row["max"].get().strip()
            try:
                ctx = int(ctx_raw) if ctx_raw else self._catalog_ctx(model_id)
            except ValueError as exc:
                raise SyncError(self._num_error("上下文", model_id)) from exc
            if max_raw:
                try:
                    max_out = int(max_raw)
                except ValueError as exc:
                    raise SyncError(self._num_error("最大输出", model_id)) from exc
                # A2-5：与原始文本比较——改动（含清空）才算 changed
                max_changed = max_raw != str(row.get("max_original") or "")
            else:
                # A2-5：原值被清空 → maxChanged=True + None（sync 删 maxOutputTokens）；
                # 从未填过 → 不写放大，保持 meta 原值
                max_changed = bool(row.get("max_original"))
                max_out = None if max_changed else row["meta"].get("maxOutputTokens")
            catalog = next((item for item in self.catalog if item.get("id") == model_id), {})
            original_values = [str(item) for item in (row["meta"].get("reasoningValues") or [])]
            new_values = self._ordered_checks(row)
            changed = new_values != original_values
            supports_image = bool(
                catalog.get("supportsImage")
                or row["meta"].get("supportsImage")
                or (row.get("extras") or {}).get("image")
            )
            # C2-5：携带改名前 id，sync 侧按它找到旧规则复用（保留 enabled 等）
            orig_id = str(row["meta"].get("id") or "")
            rec = {
                "id": model_id,
                "origId": orig_id if orig_id and orig_id != model_id else None,
                "contextWindow": ctx,
                "maxOutputTokens": max_out,
                "maxChanged": max_changed,
                "reasoning": new_values[-1] if new_values else "",
                "reasoningChanged": changed,
                "supportsImage": supports_image,
                "extras": row.get("extras"),
                "extrasChanged": bool(row.get("extrasChanged")),
                "reasoningMap": row.get("reasoningMap"),
                "mapChanged": bool(row.get("mapChanged")),
            }
            if not changed:
                rec["reasoningValues"] = row["meta"].get("reasoningValues")
            else:
                rec["reasoningValues"] = new_values or None
            out.append(rec)
        return out

    def on_fetch(self) -> None:
        if self.fetching:
            return
        Popup.close()
        base_url = self.base_url.get().strip()
        api_key = self.api_key.get().strip()
        api_type = self.api_type.get().strip()
        if not base_url:
            self.flash_status("请先填写 Base URL。", "err")
            return
        if not api_key:
            self.flash_status("请先填写 API Key。", "err")
            return
        if api_type not in API_TYPES:
            self.flash_status("请选择 API 格式。", "err")
            return
        self.fetching = True
        self.fetch_gen += 1
        gen = self.fetch_gen
        # R4-P1-2 [C-4/A-3]：快照发起时的供应商，返回时校验，防止切走后旧结果污染新供应商
        self.fetch_provider = self.provider_id.get()
        self.fetch_btn.configure(state="disabled", text="正在获取…")
        self.flash_status("正在获取模型列表…", "hold")

        def work() -> None:
            outcome = ("err", "内部错误: 未知")
            try:
                source, records = fetch_models(base_url, api_key, API_TYPES[api_type])
                outcome = ("ok", source, records)
            except SyncError as exc:
                outcome = ("err", str(exc))
            except Exception as exc:  # noqa: BLE001
                outcome = ("err", f"内部错误: {exc}")
            finally:
                # R4-P1-2 [C-7 连带]：即便 after 编组失败（主线程不在 mainloop），
                # fetching 也不能永久卡死——复位放 finally。
                self.fetching = False
            try:
                if outcome[0] == "ok":
                    self.after(0, lambda: self.on_fetch_ok(gen, outcome[1], outcome[2]))
                else:
                    self.after(0, lambda: self.on_fetch_err(gen, outcome[1]))
            except RuntimeError:
                pass  # 窗口已关/主循环未跑：状态已在 finally 复位，无需回传

        threading.Thread(target=work, daemon=True).start()

    # ---- R4-P0-1：模型级测试——每行「测试」按钮用该行模型 ID 发一次最小真实请求 ----

    def on_edit_row(self, row: dict) -> None:
        Popup.close()
        ModelEditDialog(self, row)

    def on_test_row(self, row: dict) -> None:
        if self.fetching:
            return
        Popup.close()
        model_id = row["id"].get().strip()
        if not model_id:
            # U-6：这一行明明存在，报「请先添加模型」会误导用户去找添加按钮
            self.flash_status("先给这一行选模型，再测试。", "err")
            return
        btn = row.get("test_btn")
        if btn is not None:
            try:
                btn.set_enabled(False)
            except tk.TclError:
                return
        base_url = self.base_url.get().strip()
        api_key = self.api_key.get().strip()
        api_type = self.api_type.get().strip()
        # C2-7：快照发起时的供应商，回调时校验——测试期间切走不误报到新供应商页面
        test_provider = self.provider_id.get()
        self.flash_status(f"正在测试连接：{model_id}…", "hold")

        def work() -> None:
            try:
                result = test_connection(base_url, api_key, api_type, model_id)
            except SyncError as exc:
                result = {
                    "ok": False,
                    "code": None,
                    "endpoint": "",
                    "elapsed": 0.0,
                    "reason": str(exc),
                }
            except Exception as exc:  # noqa: BLE001
                result = {
                    "ok": False,
                    "code": None,
                    "endpoint": "",
                    "elapsed": 0.0,
                    "reason": f"内部错误: {exc}",
                }
            try:
                self.after(0, lambda: self.on_test_row_done(row, model_id, result, test_provider))
            except RuntimeError:
                pass  # 窗口已关/主循环未跑：线程为 daemon，静默退出

        threading.Thread(target=work, daemon=True).start()

    def on_test_row_done(self, row: dict, model_id: str, result: dict, provider: str = "") -> None:
        if self.closed:
            return
        # 行可能在测试进行中被删除（D5 场景）：按钮恢复失败可容忍，状态照常反馈
        btn = row.get("test_btn")
        if btn is not None:
            try:
                if btn.winfo_exists():
                    btn.set_enabled(True)
            except tk.TclError:
                pass
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if provider and provider != self.provider_id.get():
            # C2-7：供应商已切换——只恢复图标，结果不再打扰当前供应商的页面
            return
        elapsed = float(result.get("elapsed") or 0.0)
        reason = str(result.get("reason") or "")
        if result.get("ok"):
            self.flash_status(f"模型 {model_id} · 正常 · 耗时 {elapsed:.1f}s", "ok")
        else:
            # 失败红字常驻到下一次操作
            self.flash_status(f"模型 {model_id} · 失败：{reason}", "err")

    def alive(self, gen: int) -> bool:
        return not self.closed and gen == self.fetch_gen and self.winfo_exists()

    def on_fetch_ok(self, gen: int, source: str, records: list[dict]) -> None:
        if not self.alive(gen):
            return
        try:
            # R4-P1-2 [C-4/A-3]：发起时快照的供应商 ≠ 当前供应商 → 结果丢弃，零污染
            if self.provider_id.get() != getattr(self, "fetch_provider", None):
                self.flash_status("供应商已切换，本次获取结果已丢弃。", "guide")
                return
            self.catalog = records
            self.catalog_ids = [rec["id"] for rec in records]
            self.refresh_row_catalog()
            if not self.rows:
                self.add_blank_row()
            self._update_name_col()
            # 空行不自动带入目录第一项，由用户在下拉里选。
            self.flash_status(
                f"已拉取 {len(records)} 个候选模型：点模型 ID 右侧 ▾ 搜索选择", "guide"
            )
        finally:
            # R4-P1-2：fetching/按钮复位放 finally，任何路径不再永久卡死
            self.fetching = False
            try:
                self.fetch_btn.configure(state="normal", text="↓  获取模型列表")
            except tk.TclError:
                pass

    def on_fetch_err(self, gen: int, message: str) -> None:
        if not self.alive(gen):
            return
        try:
            if self.provider_id.get() != getattr(self, "fetch_provider", None):
                # 已切换供应商：旧获取的错误不再打扰新供应商（按钮仍需复位）
                return
            self.flash_status(message.split("\n", 1)[0], "err")
        finally:
            self.fetching = False
            try:
                self.fetch_btn.configure(state="normal", text="↓  获取模型列表")
            except tk.TclError:
                pass

    def on_save(self) -> None:
        Popup.close()
        # R4-P1-6：保存前输入侧校验（非正整数红框+提示并拦截），不再静默替换。
        # 空 ID 行同样拦截：不能为了跳过空行放过非法数字。
        for row in self.rows:
            error = self._row_num_error(row, require_id=True)
            if error:
                self.flash_status(error, "err")
                return
        # C2-1：重复模型 ID 红字拦截——原先是 collected_models 先到先得静默丢行
        dup_seen: set[str] = set()
        for row in self.rows:
            model_id = MODEL_ID_CTRL_RE.sub("", row["id"].get()).strip()[:200]
            if not model_id:
                continue
            if model_id in dup_seen:
                self.flash_status(f"模型 {model_id} 重复，请合并或删除多余行", "err")
                return
            dup_seen.add(model_id)
        blank = sum(1 for row in self.rows if not MODEL_ID_CTRL_RE.sub("", row["id"].get()).strip())
        try:
            models = self.collected_models()
            if not models:
                self.flash_status("至少添加一个模型。先获取列表，再从下拉选择。", "err")
                return
            result = save_provider(
                name=self.name_var.get(),
                base_url=self.base_url.get(),
                api_key=self.api_key.get(),
                api_type=self.api_type.get(),
                models=models,
                provider_id=self.provider_id.get() or None,
                path=DEFAULT_CONFIG,
            )
        except SyncError as exc:
            self.flash_status(str(exc), "err")
            return
        except Exception as exc:  # noqa: BLE001
            # R4-P0-3 兜底：畸形配置导致的意外错误转中文提示，不崩界面
            self.flash_status(f"保存失败：{exc}", "err")
            return
        catalog, ids = list(self.catalog), list(self.catalog_ids)
        self.providers = result["providers"]
        self.provider_id.set(result["providerId"])
        self.select_provider(result["providerId"], keep_catalog=True)
        self.catalog, self.catalog_ids = catalog, ids
        self.refresh_row_catalog()
        # R4-P1-9 [U-12]：下一步指引具体到页面与按钮
        message = (
            f"{'已新建' if result['created'] else '已更新'} {result['providerName']}，"
            f"{result['modelCount']} 个模型。到 ZCode「模型设置」页点右上角 ↻ 即可在聊天中使用。"
        )
        if blank > 0:
            message += f"；已跳过 {blank} 行未选模型"
        self.flash_status(message, "ok")


def main() -> None:
    app = App()
    app.mainloop()
    sys.exit(0)


if __name__ == "__main__":
    main()
