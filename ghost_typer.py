# -*- coding: utf-8 -*-
"""
GhostTyper - 中英文逐字键入模拟器 (Windows)

功能：
  1. 在程序里写好任意中英文内容；
  2. 把光标点进任意软件的输入框；
  3. 按 * 键（主键盘 Shift+8，或小键盘 *）开始"像真人打字"一样逐字键入；
  4. 键入中再按 * 或 Esc 立即停止；焦点切换会自动停止，防止误输入。

原理：
  - 全局低级键盘钩子 (WH_KEYBOARD_LL) 捕获 * 热键，并吞掉该按键，
    避免星号本身被输入到目标框。
  - 逐字发送使用 SendInput + KEYEVENTF_UNICODE，可直接把中文字符
    作为 Unicode 按键事件送到焦点控件，效果等同输入法逐字上屏。
  - 兼容模式改为「逐字写入剪贴板 + Ctrl+V」，用于终端 / 游戏 / 部分
    不接受 Unicode 按键事件的程序。

命令行：
  python ghost_typer.py              启动图形界面
  python ghost_typer.py --selftest   自检（不发送任何真实按键）
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import random
import sys
import threading
import time
import traceback

if sys.platform != "win32":
    raise SystemExit("本程序仅支持 Windows。")

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError as exc:
    raise SystemExit(
        "当前 Python 缺少 tkinter。请安装 Python 时勾选 tcl/tk and IDLE，"
        "或直接双击『运行.bat』自动选用带 tkinter 的解释器。"
    ) from exc


# ----------------------------------------------------------------------------
# Win32 定义
# ----------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t          # 与指针等宽
LRESULT = ctypes.c_ssize_t

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_8 = 0x38
VK_V = 0x56
VK_MULTIPLY = 0x6A       # 小键盘 *
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x00000010
LLKHF_ALTDOWN = 0x00000020

CF_TEXT = 1
CF_OEMTEXT = 7
CF_LOCALE = 16
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# 纯文本剪贴板自带的附属格式，不算"非文本内容"
_TEXT_FORMATS = (CF_TEXT, CF_OEMTEXT, CF_LOCALE, CF_UNICODETEXT)

# 一旦被覆盖就无法重建的二进制格式：位图、图元文件、文件列表
_BINARY_FORMATS = (2, 3, 8, 14, 15, 17)      # CF_BITMAP/CF_METAFILEPICT/CF_DIB/CF_ENHMETAFILE/CF_HDROP/CF_DIBV5
_IMAGE_HINT = ("png", "image", "bitmap", "jpeg", "jpg", "gif", "tiff", "webp", "dib")


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD), ("pt", POINT),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
user32.GetClipboardFormatNameW.restype = ctypes.c_int

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p


class SendFailed(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# 底层发送 / 剪贴板 / 焦点
# ----------------------------------------------------------------------------

def _vk(vk: int, up: bool = False) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(
        vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)))


def _uni(unit: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, unit, flags, 0, 0)))


def _send_many(items: list[INPUT]) -> int:
    arr = (INPUT * len(items))(*items)
    sent = user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))
    if sent != len(items):
        # SendInput 不设 last-error，返回 0 基本都是被 UIPI 拦（目标进程权限更高）
        if sent == 0:
            raise SendFailed(
                "按键一个都没能送达。Windows 会拦下普通进程对管理员窗口的按键注入，"
                "请右键『以管理员身份』重启本程序后重试。"
            )
        raise SendFailed(
            f"SendInput 只送达 {sent}/{len(items)} 个按键事件，"
            "可能是目标程序繁忙或安全软件拦截，建议放慢每字间隔后重试。"
        )
    return sent


def send_unicode_char(ch: str) -> None:
    """把一个字符（含中文 / emoji 代理对）作为 Unicode 按键事件逐个上屏。"""
    buf = ch.encode("utf-16-le")
    items: list[INPUT] = []
    for i in range(0, len(buf), 2):
        unit = buf[i] | (buf[i + 1] << 8)
        items.append(_uni(unit))
        items.append(_uni(unit, True))
    _send_many(items)


def send_return() -> None:
    _send_many([_vk(VK_RETURN), _vk(VK_RETURN, True)])


def send_paste() -> None:
    _send_many([_vk(VK_CONTROL), _vk(VK_V),
                _vk(VK_V, True), _vk(VK_CONTROL, True)])


def send_tab() -> None:
    _send_many([_vk(VK_TAB), _vk(VK_TAB, True)])


def _clipboard_format_name(fmt: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    n = user32.GetClipboardFormatNameW(fmt, buf, 128)
    return buf.value[:n].lower() if n > 0 else ""


def clipboard_risk() -> str:
    """评估当前剪贴板被覆盖的代价。

    返回：
      "none"   —— 只有纯文本，可安全备份与恢复；
      "rich"   —— 富文本（网页 / RTF 等），恢复时只能恢复到纯文本，格式会丢；
      "binary" —— 图片 / 文件等二进制内容，一旦覆盖就彻底丢失。
    """
    if not user32.OpenClipboard(None):
        return "none"
    try:
        risk = "none"
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _TEXT_FORMATS:
                if fmt in _BINARY_FORMATS:
                    return "binary"
                if any(h in _clipboard_format_name(fmt) for h in _IMAGE_HINT):
                    return "binary"
                if risk == "none":
                    risk = "rich"
            fmt = user32.EnumClipboardFormats(fmt)
        return risk
    finally:
        user32.CloseClipboard()


def clipboard_has_nontext() -> bool:
    """剪贴板里是否还挂着非纯文本格式（图片 / 文件 / 富文本）。"""
    return clipboard_risk() != "none"


def clipboard_get() -> str | None:
    if not user32.OpenClipboard(None):
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        raise SendFailed("无法打开剪贴板（可能被其他程序占用）")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise SendFailed("GlobalAlloc 失败")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise SendFailed("GlobalLock 失败")
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise SendFailed("SetClipboardData 失败")
        # 成功后内存所有权归系统，不要 GlobalFree
    finally:
        user32.CloseClipboard()


def key_down_now(vk: int) -> bool:
    """GetKeyState 是线程相关的，在钩子线程里可能不准；同时查全局异步状态。"""
    if user32.GetKeyState(vk) & 0x8000:
        return True
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def foreground_is_own() -> bool:
    """前台窗口是否属于本进程（用于放行本程序内的正常 * 输入）。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


# ----------------------------------------------------------------------------
# 全局热键钩子
# ----------------------------------------------------------------------------

class HookManager:
    def __init__(self, cmd_q: queue.Queue, app: "App"):
        self.cmd_q = cmd_q
        self.app = app
        self.hhook = 0
        self.thread_id = 0
        self.ready = threading.Event()
        self.error: str | None = None
        self._swallow = set()        # 需要吞掉其抬起事件的 vk 集合
        self._star_down = False      # * 键是否已处于按下状态（过滤长按自动重复）
        self._proc_ref = HOOKPROC(self._hook_proc)   # 必须长期持有引用
        self.thread = threading.Thread(target=self._run, name="hotkey-hook", daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(3.0)

    def stop(self) -> None:
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    def _hook_proc(self, n_code, w_param, l_param):
        try:
            if n_code == HC_ACTION:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = kb.vkCode
                injected = bool(kb.flags & LLKHF_INJECTED)

                if w_param in (WM_KEYUP, WM_SYSKEYUP):
                    if vk in (VK_8, VK_MULTIPLY):
                        self._star_down = False
                        if vk in self._swallow:
                            self._swallow.discard(vk)
                            return 1    # 配套吞掉抬起，避免目标程序认为 * 一直按住
                elif w_param in (WM_KEYDOWN, WM_SYSKEYDOWN) and not injected:
                    shift = (key_down_now(VK_SHIFT) or key_down_now(VK_LSHIFT)
                             or key_down_now(VK_RSHIFT))
                    ctrl = key_down_now(VK_CONTROL)
                    alt = bool(kb.flags & LLKHF_ALTDOWN)
                    need_ctrl = bool(self.app.hotkey_ctrl_required)
                    mod_ok = ctrl if need_ctrl else not ctrl
                    is_star = (mod_ok and not alt
                               and (vk == VK_MULTIPLY
                                    or (vk == VK_8 and shift)))
                    own = foreground_is_own()

                    if is_star and self.app.hotkey_enabled and not own:
                        self._swallow.add(vk)
                        if self._star_down:
                            return 1    # 长按产生的自动重复，吞掉但不反复启停
                        self._star_down = True
                        self.cmd_q.put("star")
                        return 1        # 吞掉星号，不让它进入输入框
                    if vk in (VK_8, VK_MULTIPLY):
                        self._star_down = False
                    if vk == VK_ESCAPE and self.app.typing and not own:
                        self.cmd_q.put("stop")
                        return 1        # 吞掉 Esc
        except Exception:
            pass
        return user32.CallNextHookEx(self.hhook or 0, n_code, w_param, l_param)

    def _run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        self.hhook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_ref, hmod, 0)
        if not self.hhook:
            self.error = f"安装全局热键失败，错误码 {ctypes.get_last_error()}"
        self.ready.set()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.hhook:
            user32.UnhookWindowsHookEx(self.hhook)
            self.hhook = 0


# ----------------------------------------------------------------------------
# 图形界面与打字调度
# ----------------------------------------------------------------------------

SAMPLE_TEXT = (
    "Hello, World! 这是一段中英文混排的演示文字。\n"
    "每个字符都会像真人敲键盘一样依次上屏，而不是一次性粘贴。\n"
    "The quick brown fox jumps over the lazy dog. 2026，让打字更自然。"
)

PUNCTUATION = set("，。！？；：、,.!?;:…—~～\"'“”‘’()（）[]【】{}《》「」〈〉·<>/\\|+-=*_")

MODE_UNICODE = "Unicode 直发（推荐）"
MODE_CLIPBOARD = "剪贴板逐字粘贴（兼容）"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ui_q: queue.Queue = queue.Queue()
        self.cmd_q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()   # 当前这一轮打字的停止信号
        self.run_id = 0                     # 代次号，用来区分新旧 worker
        self.typing = False
        self.hotkey_enabled = True
        self.hotkey_ctrl_required = False

        self.v_interval = tk.StringVar(value="90")
        self.v_jitter = tk.StringVar(value="35")
        self.v_cjk = tk.StringVar(value="45")
        self.v_punct = tk.StringVar(value="180")
        self.v_mode = tk.StringVar(value=MODE_UNICODE)
        self.v_hotkey = tk.BooleanVar(value=True)
        self.v_topmost = tk.BooleanVar(value=False)
        self.v_lock_focus = tk.BooleanVar(value=True)
        self.v_restore_clip = tk.BooleanVar(value=True)
        self.v_tab_real = tk.BooleanVar(value=False)
        self.v_hotkey_ctrl = tk.BooleanVar(value=False)

        self._build_ui()
        self._log("就绪：先写好内容 → 点进目标输入框 → 按 * 开始逐字键入。")
        self._log("提示：若目标软件以管理员身份运行，本程序也需以管理员身份启动，否则按键会被系统拦截。")

        self.hook = HookManager(self.cmd_q, self)
        self.hook.start()
        if self.hook.error:
            self._log("[警告] " + self.hook.error + "（仍可点击『开始』按钮使用）")
        threading.Thread(target=self._dispatcher, name="cmd-dispatcher", daemon=True).start()
        self.root.after(60, self._pump_ui)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----------------------------- UI -----------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title("逐字键入模拟器  GhostTyper 1.0")
        root.geometry("740x720")
        root.minsize(660, 600)
        root.configure(bg="#f4f6f9")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f4f6f9")
        style.configure("TLabelframe", background="#f4f6f9", bordercolor="#d7dde6")
        style.configure("TLabelframe.Label", background="#f4f6f9", foreground="#334155", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TLabel", background="#f4f6f9", foreground="#334155", font=("Microsoft YaHei UI", 9))
        style.configure("TCheckbutton", background="#f4f6f9", foreground="#334155", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=5)
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"), padding=6)

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)

        # 标题 + 状态
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="逐字键入模拟器", font=("Microsoft YaHei UI", 15, "bold"),
                  foreground="#1e3a5f").pack(side="left")
        self.lbl_state = tk.Label(head, text=" ● 待命", bg="#e6edf5", fg="#3b5a7a",
                                  font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4)
        self.lbl_state.pack(side="right")

        ttk.Label(outer, text="① 写好内容    ② 点进任意输入框让光标就位    ③ 按 * 开始（Shift+8 或小键盘 *）",
                  foreground="#5a6b82").pack(anchor="w", pady=(6, 2))
        ttk.Label(outer, text="键入中再按 * 或 Esc 立即停止；切换窗口会自动停止，避免把字打进别的地方。",
                  foreground="#5a6b82").pack(anchor="w", pady=(0, 8))

        # 文本区
        box = ttk.Labelframe(outer, text=" 要键入的内容 ", padding=8)
        box.pack(fill="both", expand=True)
        self.txt = scrolledtext.ScrolledText(box, wrap="word", height=9, undo=True,
                                             font=("Microsoft YaHei UI", 11),
                                             bg="#ffffff", fg="#1f2937",
                                             insertbackground="#2563eb",
                                             relief="solid", borderwidth=1)
        self.txt.pack(fill="both", expand=True)
        self.txt.insert("1.0", SAMPLE_TEXT)

        # 参数
        cfg = ttk.Labelframe(outer, text=" 键入设置 ", padding=8)
        cfg.pack(fill="x", pady=10)

        line1 = ttk.Frame(cfg)
        line1.pack(fill="x", pady=2)
        self._spin(line1, "每字间隔 ms", self.v_interval, 6)
        self._spin(line1, "随机波动 %", self.v_jitter, 6)
        self._spin(line1, "中文额外 ms", self.v_cjk, 6)
        self._spin(line1, "标点停顿 ms", self.v_punct, 6)

        line2 = ttk.Frame(cfg)
        line2.pack(fill="x", pady=(8, 2))
        ttk.Label(line2, text="输入方式").pack(side="left")
        mode = ttk.Combobox(line2, textvariable=self.v_mode, state="readonly", width=24,
                            values=[MODE_UNICODE, MODE_CLIPBOARD])
        mode.pack(side="left", padx=(6, 16))
        ttk.Checkbutton(line2, text="启用 * 热键", variable=self.v_hotkey,
                        command=self._on_hotkey_toggle).pack(side="left", padx=4)
        ttk.Checkbutton(line2, text="焦点变化自动停止", variable=self.v_lock_focus).pack(side="left", padx=4)
        ttk.Checkbutton(line2, text="窗口置顶", variable=self.v_topmost,
                        command=self._on_topmost).pack(side="left", padx=4)
        ttk.Checkbutton(line2, text="结束后恢复剪贴板", variable=self.v_restore_clip).pack(side="left", padx=4)

        line3 = ttk.Frame(cfg)
        line3.pack(fill="x", pady=(6, 2))
        ttk.Checkbutton(line3, text="* 热键需配合 Ctrl（这样才能正常输入星号）",
                        variable=self.v_hotkey_ctrl,
                        command=self._on_hotkey_ctrl_toggle).pack(side="left", padx=4)
        ttk.Checkbutton(line3, text="Tab 发送真实 Tab（不转 4 空格）",
                        variable=self.v_tab_real).pack(side="left", padx=(14, 4))

        ttk.Label(outer, text="Unicode 直发：中文像输入法一样逐字上屏，适用于记事本 / 浏览器 / 微信 / Office。"
                              "  剪贴板模式：逐字粘贴，用于 Unicode 直发无效的程序（远程桌面、部分终端等），"
                              "会临时占用剪贴板，每字至少 0.1 秒；若剪贴板里是图片 / 文件 / 富文本，该模式会被拒绝启动，避免内容丢失。",
                  foreground="#7b8794", wraplength=700, justify="left").pack(anchor="w", pady=(0, 8))

        # 按钮 + 进度
        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        ttk.Button(bar, text="▶ 开始（3 秒后）", style="Accent.TButton",
                   command=self._manual_start).pack(side="left")
        ttk.Button(bar, text="■ 停止", command=lambda: self.stop_typing("已手动停止")).pack(side="left", padx=6)
        ttk.Button(bar, text="载入示例", command=self._load_sample).pack(side="left", padx=6)
        ttk.Button(bar, text="从 txt 载入", command=self._load_file).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=lambda: self.txt.delete("1.0", "end")).pack(side="left", padx=6)

        prog = ttk.Frame(outer)
        prog.pack(fill="x", pady=(10, 4))
        self.pb = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.lbl_prog = ttk.Label(prog, text="0 / 0 字", width=16, anchor="e")
        self.lbl_prog.pack(side="right", padx=(8, 0))

        self.log_box = scrolledtext.ScrolledText(outer, height=5, wrap="word",
                                                 font=("Consolas", 9), bg="#fbfcfe",
                                                 fg="#475569", relief="solid", borderwidth=1)
        self.log_box.pack(fill="x")
        self.log_box.configure(state="disabled")

    def _spin(self, parent, label, var, width):
        ttk.Label(parent, text=label).pack(side="left")
        ttk.Entry(parent, textvariable=var, width=width, justify="center").pack(side="left", padx=(4, 14))

    def _on_hotkey_toggle(self):
        self.hotkey_enabled = bool(self.v_hotkey.get())
        self._log("全局 * 热键已" + ("启用" if self.hotkey_enabled else "停用"))

    def _on_hotkey_ctrl_toggle(self):
        self.hotkey_ctrl_required = bool(self.v_hotkey_ctrl.get())
        self._log("全局 * 热键" + ("需配合 Ctrl 触发" if self.hotkey_ctrl_required
                                   else "单独按 Shift+8 / 小键盘 * 即可触发"))

    def _on_topmost(self):
        self.root.attributes("-topmost", bool(self.v_topmost.get()))

    def _load_sample(self):
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", SAMPLE_TEXT)

    def _load_file(self):
        path = filedialog.askopenfilename(filetypes=[("文本文件", "*.txt;*.md"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            # utf-8-sig 会自动吃掉 BOM；用 utf-8 的话 BOM 会作为 \ufeff 留在正文首字符
            with open(path, "r", encoding="utf-8-sig") as f:
                data = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="gbk", errors="replace") as f:
                data = f.read()
        if data.startswith("\ufeff"):
            data = data[1:]
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", data)
        self._log(f"已载入 {os.path.basename(path)}，共 {len(data)} 个字符")

    # --------------------------- 线程通信 ---------------------------

    def ui(self, fn) -> None:
        """从任意线程安全地投递 UI 更新。"""
        self.ui_q.put(fn)

    def _pump_ui(self) -> None:
        while True:
            try:
                fn = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                # 不再静默吞掉：UI 回调出错时留下痕迹，否则界面不动却查不到原因
                try:
                    print("[UI] 回调异常:\n" + traceback.format_exc(), file=sys.stderr)
                except Exception:
                    pass
        self.root.after(50, self._pump_ui)

    def _dispatcher(self) -> None:
        while True:
            cmd = self.cmd_q.get()
            if cmd == "quit":
                return
            self.ui(lambda c=cmd: self._handle_cmd(c))

    def _log(self, msg: str) -> None:
        def write():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
            line_count = int(self.log_box.index("end-1c").split(".")[0])
            if line_count > 200:
                self.log_box.delete("1.0", f"{line_count - 150}.0")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.ui(write)

    def _set_state(self, state: str) -> None:
        def apply():
            if state == "typing":
                self.lbl_state.configure(text=" ● 键入中", bg="#dcf5e6", fg="#1c7a4a")
            else:
                self.lbl_state.configure(text=" ● 待命", bg="#e6edf5", fg="#3b5a7a")
        self.ui(apply)

    def _set_progress(self, done: int, total: int) -> None:
        def apply():
            self.pb["value"] = done / max(total, 1) * 100
            self.lbl_prog.configure(text=f"{done} / {total} 字")
        self.ui(apply)

    # --------------------------- 打字控制 ---------------------------

    def _num(self, var, default, low, high):
        try:
            v = float(var.get())
            return min(high, max(low, v))
        except (ValueError, tk.TclError):
            return default

    def _collect_cfg(self) -> dict:
        return {
            "interval": self._num(self.v_interval, 90, 0, 5000),
            "jitter": self._num(self.v_jitter, 35, 0, 100),
            "cjk": self._num(self.v_cjk, 45, 0, 5000),
            "punct": self._num(self.v_punct, 180, 0, 5000),
            "mode": self.v_mode.get(),
            "lock_focus": bool(self.v_lock_focus.get()),
            "restore_clip": bool(self.v_restore_clip.get()),
            "tab": bool(self.v_tab_real.get()),
        }

    def _manual_start(self) -> None:
        self.start_typing(delay=3.0, focus_wait=8.0, interactive=True)

    def _handle_cmd(self, cmd: str) -> None:
        if cmd == "star":
            if self.typing:
                self.stop_typing("已由 * 热键停止")
            else:
                self.start_typing(delay=0.12, focus_wait=0.5)
        elif cmd == "stop":
            if self.typing:
                self.stop_typing("已由 Esc 停止")

    def start_typing(self, delay: float, focus_wait: float, interactive: bool = False) -> None:
        if self.typing:
            return
        text = self.txt.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showwarning("没有内容", "请先在上方输入要键入的文字。")
            return
        cfg = self._collect_cfg()
        if cfg["mode"] == MODE_CLIPBOARD:
            risk = clipboard_risk()
            if risk == "binary":
                msg = ("剪贴板里现在有图片 / 文件等二进制内容，「剪贴板逐字粘贴」"
                       "会把它覆盖掉且无法恢复。请改用「Unicode 直发」，或先清空剪贴板。")
                self._log("[已阻止] " + msg)
                if interactive:
                    messagebox.showwarning("剪贴板会被清掉", msg)
                return
            if risk == "rich":
                self._log("注意：原剪贴板是富文本（网页 / RTF 等），"
                          "结束后只能恢复成纯文本，原有格式会丢失。")

        self.run_id += 1
        my_id = self.run_id
        self.stop_evt = threading.Event()
        evt = self.stop_evt            # worker 只认领自己这一代的 event
        self.typing = True
        self._set_state("typing")
        self._set_progress(0, len(text))
        self._log(f"开始排队：共 {len(text)} 字符，模式={cfg['mode']}，请保持目标输入框的焦点。")
        threading.Thread(target=self._type_worker,
                         args=(text, cfg, delay, focus_wait, my_id, evt),
                         name="typer", daemon=True).start()

    def stop_typing(self, reason: str = "已停止") -> None:
        if not self.typing:
            return
        self.stop_evt.set()
        self._log("请求停止：" + reason)

    def _emit(self, ch: str, cfg: dict, evt: threading.Event) -> None:
        if ch == "\r":
            return
        if ch == "\n":
            send_return()
            return
        if ch == "\t":
            if cfg["tab"]:
                send_tab()          # 需要真实 Tab 的场景（跳格、缩进）
                return
            seq = "    "
        else:
            seq = ch
        for c in seq:
            if evt.is_set():
                return
            if c in "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f\x7f":
                continue
            if c == "\ufeff" or c in "\u200b\u200c\u200d\u2060":
                continue            # BOM / 零宽字符，肉眼看不见，发过去只会造成怪现象
            if cfg["mode"] == MODE_CLIPBOARD:
                # 粘贴是异步的：必须等目标程序把当前字符从剪贴板取走，
                # 才能写入下一个字符，否则会连着粘贴同一个字。
                try:
                    clipboard_set(c)
                except SendFailed as exc:
                    self._log(f"[跳过] 剪贴板被占用，字符 {c!r} 未能输入：{exc}")
                    continue
                send_paste()
                if evt.wait(0.04):
                    return
            else:
                send_unicode_char(c)

    def _delay_for(self, ch: str, cfg: dict) -> float:
        base = cfg["interval"] / 1000.0
        j = cfg["jitter"] / 100.0
        d = base * random.uniform(1 - j, 1 + j)
        if ord(ch) > 127:
            d += cfg["cjk"] / 1000.0
        if ch in PUNCTUATION or ch == "\n":
            d += cfg["punct"] / 1000.0
        if cfg["mode"] == MODE_CLIPBOARD:
            d = max(d, 0.10)     # 兼容模式每字至少 100ms，给目标程序留出读剪贴板的时间
        return max(d, 0.005)

    def _type_worker(self, text: str, cfg: dict, delay: float, focus_wait: float,
                     run_id: int, evt: threading.Event) -> None:
        reason = "完成"
        saved_clip = None
        try:
            # 先给一小段时间，让触发热键的按键事件处理完 / 让用户切到目标窗口
            if evt.wait(delay):
                reason = "已停止"
                return

            deadline = time.monotonic() + focus_wait
            while foreground_is_own() and time.monotonic() < deadline:
                if evt.wait(0.05):
                    reason = "已停止"
                    return
            if foreground_is_own():
                reason = "已取消：焦点还在本程序窗口，请先点进目标输入框"
                return

            target_hwnd = user32.GetForegroundWindow()
            if cfg["mode"] == MODE_CLIPBOARD and cfg["restore_clip"]:
                saved_clip = clipboard_get()
                if saved_clip is None:
                    self._log("提示：原剪贴板没有文本内容，本次结束后无内容可恢复。")

            total = len(text)
            for i, ch in enumerate(text):
                if evt.is_set():
                    reason = "已停止"
                    break
                if cfg["lock_focus"] and user32.GetForegroundWindow() != target_hwnd:
                    reason = "已自动停止：目标窗口焦点发生变化"
                    break
                self._emit(ch, cfg, evt)
                if run_id == self.run_id and (i % 2 == 0 or i == total - 1):
                    self._set_progress(i + 1, total)
                if evt.wait(self._delay_for(ch, cfg)):
                    reason = "已停止"
                    break
        except SendFailed as exc:
            reason = "发送失败"
            self._log("[错误] " + str(exc))
        except Exception:
            reason = "出错"
            self._log("[错误] " + traceback.format_exc())
        finally:
            if saved_clip is not None:
                try:
                    clipboard_set(saved_clip)
                except Exception:
                    pass
            if run_id == self.run_id:
                # 只有仍是最新这一轮时才动全局状态，避免上一轮收尾覆盖下一轮
                self.typing = False
                self._set_state("idle")
            self._log("键入结束：" + reason)

    def on_close(self) -> None:
        if self.typing:
            self.stop_evt.set()
        self.cmd_q.put("quit")
        self.hook.stop()
        self.root.destroy()


# ----------------------------------------------------------------------------
# 自检 & 入口
# ----------------------------------------------------------------------------

def run_selftest() -> int:
    print("== GhostTyper 自检 ==")
    print(f"Python           : {sys.version.split()[0]}")
    print(f"INPUT 结构大小   : {ctypes.sizeof(INPUT)} (x64 应为 40 / x86 应为 28)")
    print(f"KBDLLHOOKSTRUCT  : {ctypes.sizeof(KBDLLHOOKSTRUCT)}")

    c = "中"
    buf = c.encode("utf-16-le")
    unit = buf[0] | (buf[1] << 8)
    inp = _uni(unit)
    print(f"Unicode 按键构造 : wVk={inp.u.ki.wVk}, wScan=0x{inp.u.ki.wScan:04X} "
          f"({c}), flags=0x{inp.u.ki.dwFlags:X}")

    old = None
    if clipboard_risk() != "none":
        print("剪贴板读写       : 跳过（当前剪贴板含图片/文件/富文本，"
              "自检会覆盖且无法完整恢复）")
    else:
        old = clipboard_get()
        try:
            clipboard_set("GhostTyper 自检 ✓ 中文")
            got = clipboard_get()
            print(f"剪贴板读写       : {'OK' if got == 'GhostTyper 自检 ✓ 中文' else '失败 -> ' + repr(got)}")
        finally:
            try:
                if old is not None:
                    clipboard_set(old)
                else:
                    if user32.OpenClipboard(None):
                        user32.EmptyClipboard()
                        user32.CloseClipboard()
            except Exception:
                pass

    proc = HOOKPROC(lambda n, w, l: user32.CallNextHookEx(0, n, w, l))
    h = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
    print(f"全局钩子安装     : {'OK' if h else '失败, 错误码 ' + str(ctypes.get_last_error())}")
    if h:
        user32.UnhookWindowsHookEx(h)
    print(f"前台窗口检测     : 本进程前台={foreground_is_own()}")
    print("== 自检结束（未发送任何真实按键）==")
    return 0


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        detail = traceback.format_exc()
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write(detail)
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(0, detail, "GhostTyper 启动失败", 0x10)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
