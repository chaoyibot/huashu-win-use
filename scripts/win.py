# -*- coding: utf-8 -*-
# huashu-win-use 操控内核（huashu-mac-use 的 Windows 移植版）
# 原版: https://github.com/alchaincyf/huashu-mac-use （mac.swift, MIT）
# 移植: Python + pywin32/UIA/Pillow。用 build.cmd 装好依赖后直接 python win.py 使用。
#
# 设计原则（照搬原版，逐条保留）:
#   1. 教训进代码不进文档：坐标语义统一、截图失败自诊断、UIA 查两次、跨虚拟桌面拒绝写。
#   2. 退出码三态：0 成功 / 1 失败 / 2 拒绝或未知（refused/unknown）。上层不许把 2 当成功。
#   3. 所有坐标为「逻辑点」，屏幕尺寸运行时读取，不写死。
import ctypes, ctypes.wintypes as wt, json, os, re, struct, subprocess, sys, tempfile, time
import win32api, win32con, win32gui, win32process, win32ui
from PIL import Image, ImageDraw

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

USAGE = """\
用法（坐标三种写法通用：<=1 归一化 / >1 窗口内点数 / x y @截图.png 按图上像素；加 --dry 只解析不执行）:
  win windows [owner关键词] [--all]        列窗口：id(hwnd) / pid / owner / on(可见) / origin / 尺寸 / 标题
  win shot <hwnd> <路径>                   截单窗口（后台/被遮挡也能截）。失败自诊断，壳窗口自动改截兄弟窗口
  win shotfg <hwnd> <路径>                 先试后台，确认空图才借焦点并立刻还
  win see <hwnd|owner关键词> [--out 路径]   一次拿到：降采样截图 + 收据 + UIA 元素表（可用时）-> 之后用 eN@<json> 点
  win clickin <hwnd> <x> <y> [@图] [eN@json] [--dry]   按窗口内坐标点击（不激活；跨虚拟桌面拒绝）
  win hoverin <hwnd> <x> <y> [@图] [holdms]             真实悬停（组件库菜单要它）
  win click <pid> <x> <y> [@全屏图] [bg] [--dry]        全局坐标点击
  win hover <x> <y> [holdms]
  win scroll <x> <y> <dy> [dx] [steps]     真实滚轮（画布类应用）
  win type <pid> <文本> [global]           投递 Unicode（中文可用）
  win key <pid> <vkey> [ctrl]              13=回车 27=Esc 8=删除 9=v（走全局流）
  win ax <pid>                             UIA 探测（内部查两次取最大）
  win axset <pid> <文本>                   给第一个可编辑控件设值并读回
  win op <hwnd> <x> <y> <文本> [@图] [send <sx> <sy>] [shot <路径>] [--bg|--fast] [--force] [--dry]
                                         写操作默认入口。默认后台优先阶梯：
                                           (1) PostMessage 零焦点写入 -> 截图差分验证 -> 生效就结束（用户毫无感觉）
                                           (2) 判不出生效才升级借焦点，且升级前必过三道闸：终端send / 借焦点锁 / 用户在场
                                          --bg 只走(1)不升级   --fast 跳过(1)直接借焦点   --force 拆掉三道闸
  win hud <毫秒> [文案] [corner|glow|plain] 屏幕四角脉冲取景框，提示用户 agent 正在接管（鼠标穿透/不抢焦点）
                                         借焦点时自动闪。WIN_HUD=0 关闭；对屏幕捕获隐身（WDA_EXCLUDEFROMCAPTURE）
  win idle                                用户此刻在不在场：键鼠空闲秒数 + 前台窗口 + 借焦点锁归谁。动手前先问这一句
  win open <名称|路径> [--cdp 端口] [--relaunch] [--dry]   按名称解析路径启动；--cdp 带调试端口并等通
  win frontmost                           当前前台窗口标题
"""

def die(s, code=1):
    print(s)
    sys.exit(code)

# ---------------------------------------------------------------------------
# 窗口枚举
# ---------------------------------------------------------------------------
JUNK_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "DV2ControlHost",
    "XamlExplorerHostIslandWindow", "Windows.UI.Core.CoreWindow", "Button", "GDI+ Hook Window Class",
    "IME", "MSCTFIME UI", "tooltips_class32", "ToolbarWindow32", "NotifyIconOverflowWindow",
}
JUNK_TITLES = {"", "Program Manager", "Default IME", "Microsoft Text Input Application", "输入法"}

def owner_name(pid):
    """进程可执行文件名（去 .exe），失败返回 ?。"""
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        buf = ctypes.create_unicode_buffer(512)
        sz = ctypes.c_ulong(512)
        if kernel32.QueryFullProcessImageNameW(h.handle, 0, buf, ctypes.byref(sz)):
            return buf.value.replace("\\", "/").split("/")[-1].replace(".exe", "")
    except Exception:
        pass
    return "?"

def _enumerate():
    """返回 [(hwnd, pid, owner, title, x, y, w, h, cls, on)]。on=当前可见(非最小化)。"""
    out = []
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        rect = win32gui.GetWindowRect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w <= 0 or h <= 0:
            return True
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        # 最小化窗口 rect 被系统移到 (-32000,-32000)
        minz = win32gui.IsIconic(hwnd)
        if minz:
            rect = win32gui.GetWindowPlacement(hwnd)[-1]
        on = not minz
        out.append((hwnd, pid, owner_name(pid), title, rect[0], rect[1],
                    rect[2] - rect[0], rect[3] - rect[1], cls, on))
        return True
    win32gui.EnumWindows(cb, None)
    return out

def is_junk(t):
    hwnd, pid, owner, title, x, y, w, h, cls, on = t
    if cls in JUNK_CLASSES or title in JUNK_TITLES:
        return True
    if cls == "ApplicationFrameWindow" and not title:
        return True
    # 隐形坐标占位（-32000）且无标题
    if abs(x) > 20000 and not title:
        return True
    if w < 60 and h < 60 and not title:
        return True
    return False

def fmt(t):
    hwnd, pid, owner, title, x, y, w, h, cls, on = t
    return "id=%d pid=%d owner=%s on=%d origin=(%d,%d) %dx%d cls=%s title=%s" % (
        hwnd, pid, owner, 1 if on else 0, x, y, w, h, cls, title)

def receipt(t):
    hwnd, pid, owner, title, x, y, w, h, cls, on = t
    return "receipt=%d:%d:%d,%d,%dx%d" % (hwnd, pid, x, y, w, h)

def win_info(hwnd):
    for t in _enumerate():
        if t[0] == hwnd:
            return t
    return None

def all_windows():
    return _enumerate()

def is_onscreen(hwnd):
    t = win_info(hwnd)
    return bool(t and t[9])

# 稳定 origin：窗口激活后常有动画，连续两次一致才认（原版逻辑）
def stable_origin(hwnd):
    last = None
    for _ in range(90):
        t = win_info(hwnd)
        if not t:
            time.sleep(0.01)
            continue
        o = (t[4], t[5])
        if last is not None and abs(o[0] - last[0]) < 2 and abs(o[1] - last[1]) < 2:
            return o
        last = o
        time.sleep(0.01)
    return last

def virtual_screen():
    return (win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))

# 屏幕是否锁定（winlogon 在焦点上即锁屏）
def screen_locked():
    fg = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(fg)
    if owner_name(pid) == "LogonUI" or owner_name(pid) == "lockapp":
        return True
    return False
# -*- coding: utf-8 -*-
# Part2: 截图与图像判定

def capture_window(hwnd, path):
    """PrintWindow 后台截单窗口（被遮挡也能截）。成功返回 True。"""
    try:
        rect = win32gui.GetWindowRect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w <= 0 or h <= 0:
            return False
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfcDC, w, h)
        saveDC.SelectObject(bmp)
        # PW_RENDERFULLCONTENT = 2
        ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
        saveDC.DeleteDC(); mfcDC.DeleteDC(); win32gui.ReleaseDC(hwnd, hwndDC)
        img.save(path)
        return True
    except Exception:
        return False

def load_pixels(path, w):
    """缩到 w 宽后返回 (bytes, w, h)。"""
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return None
    if img.width <= 0:
        return None
    h = max(1, int(w * img.height / img.width))
    img = img.resize((w, h), Image.LANCZOS)
    return (img.tobytes(), w, h)

def image_size(path):
    try:
        img = Image.open(path)
        return (img.width, img.height)
    except Exception:
        return None

def looks_blank(path):
    """判空用「全图颜色种类数」：渲染失败的窗口整图是纯色（白/黑），<6 种。
    Windows 上不再做顶部裁剪——Notepad 等 app 的文字紧贴内容区顶部，
    按原版裁顶部 18% 会把有文字的窗口误判成空（实测踩到）。"""
    r = load_pixels(path, 64)
    if not r:
        return True
    px, w, h = r
    seen = set()
    for i in range(0, w * h * 3, 3):
        seen.add((px[i] >> 4) << 8 | (px[i + 1] >> 4) << 4 | (px[i + 2] >> 4))
    return len(seen) < 6

def downsample(src, dst, width=1400):
    try:
        img = Image.open(src).convert("RGB")
    except Exception:
        return None
    w = min(width, img.width)
    h = max(1, img.height * w // img.width)
    img = img.resize((w, h), Image.LANCZOS)
    img.save(dst)
    return (w, h)

def diff_report(before, after, hitX, hitY):
    """像素差分：只看「你点的那一块」。返回带 effect= 判定的多行文本。"""
    W = 160
    r1 = load_pixels(before, W); r2 = load_pixels(after, W)
    if not r1 or not r2 or len(r1[0]) != len(r2[0]):
        return "  \u26a0\ufe0f \u5dee\u5206\u4e0d\u53ef\u7528\uff08\u524d\u540e\u6709\u4e00\u5f20\u6ca1\u622a\u6210\uff09"
    pa, w, h = r1; pb = r2[0]
    rad = 0.12
    lx0 = int(max(0, hitX - rad) * w); lx1 = int(min(1, hitX + rad) * w)
    ly0 = int(max(0, hitY - rad) * h); ly1 = int(min(1, hitY + rad) * h)
    changed = 0; lc = 0; lt = 0
    minX, maxX, minY, maxY = w, -1, h, -1
    for y in range(h):
        row = y * w
        for x in range(w):
            i = (row + x) * 3
            d = abs(pa[i] - pb[i]) + abs(pa[i + 1] - pb[i + 1]) + abs(pa[i + 2] - pb[i + 2])
            inL = lx0 <= x < lx1 and ly0 <= y < ly1
            if inL:
                lt += 1
            if d > 24:
                changed += 1
                if inL:
                    lc += 1
                if x < minX: minX = x
                if x > maxX: maxX = x
                if y < minY: minY = y
                if y > maxY: maxY = y
    pct = changed * 100.0 / (w * h)
    lpct = lc * 100.0 / lt if lt > 0 else 0
    if changed == 0:
        return "  effect=suspected_noop 全窗 0% 变化。按可能性：\u2460\u5750\u6807\u6ca1\u843d\u5728\u63a7\u4ef6\u4e0a \u2461\u7a97\u53e3\u6ca1\u771f\u6b63\u6fc0\u6d3b \u2462\u63a7\u4ef6\u4e0d\u54cd\u5e94\u5408\u6210\u4e8b\u4ef6 \u2463\u622a\u56fe\u65e9\u4e8e\u5237\u65b0"
    out = "  \U0001f4cd 落点邻域(±12%%) 变化 %.1f%%  |  全窗 %.1f%%" % (lpct, pct)
    if lpct < 1.0:
        out += "\n  effect=suspected_noop 落点几乎没变，全窗变化集中在 (%.2f,%.2f)，多半是 app 自己的动画" % (
            (minX + maxX) / 2.0 / w, (minY + maxY) / 2.0 / h)
    elif lpct < 8.0:
        out += "\n  effect=partial 落点变化很小，可能只是焦点高亮/光标。看截图坐实"
    else:
        out += "\n  effect=confirmed 落点确实变了。仍需确认变的是「文字进去」不是「弹出了别的东西」"
    return out

def describe_shot(path):
    sz = image_size(path)
    size = "%dx%dpx" % sz if sz else "?"
    return "%s 判空=%s" % (size, "是（可能后台不渲染，试 shotfg 或 CDP）" if looks_blank(path) else "否")
# -*- coding: utf-8 -*-
# Part3: 在场闸 / 借焦点锁 / 遮挡闸 / 终端闸 / 坐标解析

TRAIL = os.path.join(tempfile.gettempdir(), "huashu-win-synthetic.trail")
FOCUS_LOCK = os.path.join(tempfile.gettempdir(), "huashu-win-focus.lock")
IDLE_NEED = 2.0
IDLE_WAIT = 15.0

def get_idle_seconds():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0

def mark_synthetic():
    """记下我们刚合成事件的时刻，排除它被误判成「用户在动」。"""
    try:
        with open(TRAIL, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass

def synthetic_ago():
    try:
        with open(TRAIL) as f:
            t = float(f.read().strip())
        d = time.time() - t
        return d if d >= 0 else None
    except Exception:
        return None

def user_idle():
    """用户真实动手距今几秒。两路信号都判定为自身尾迹时返回 3600（放行）。"""
    k = get_idle_seconds()          # 键鼠共用同一计时器（GetLastInputInfo）
    ago = synthetic_ago()
    def clean(v):
        if ago is not None and ago < 10 and abs(ago - v) < 0.8:
            return None            # 这一路是我们自己刚留下的
        return v
    vals = [clean(k)]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else 3600.0

def wait_user_idle(need=IDLE_NEED, max_wait=IDLE_WAIT):
    t0 = time.time()
    last = user_idle()
    while last < need:
        if time.time() - t0 >= max_wait:
            return (False, time.time() - t0, last)
        time.sleep(0.25)
        last = user_idle()
    return (True, time.time() - t0, last)

def lock_holder():
    try:
        with open(FOCUS_LOCK) as f:
            s = f.read().strip().split(":")
        pid, t = int(s[0]), float(s[1])
        age = time.time() - t
        if age > 30:
            return None
        # 持有者是否存活
        try:
            win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid).Close()
        except Exception:
            return None
        return (pid, age)
    except Exception:
        return None

def acquire_focus_lock(max_wait=5.0):
    t0 = time.time()
    while True:
        h = lock_holder()
        if h is None or h[0] == os.getpid():
            break
        if time.time() - t0 >= max_wait:
            return h[0]
        time.sleep(0.2)
    with open(FOCUS_LOCK, "w") as f:
        f.write("%d:%f" % (os.getpid(), time.time()))
    return None

def release_focus_lock():
    h = lock_holder()
    if h and h[0] == os.getpid():
        try:
            os.remove(FOCUS_LOCK)
        except Exception:
            pass

def top_window_at(x, y):
    """落点最上层顶层窗口。WindowFromPoint 返回子窗口时取 GA_ROOT。"""
    h = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
    if not h:
        return None
    root = user32.GetAncestor(h, 2)   # GA_ROOT
    if not root:
        root = h
    t = win_info(root)
    return t

SHELL_OWNERS = {"WindowsTerminal", "cmd", "powershell", "pwsh", "Code", "Cursor", "Windoze"}
def is_shellish(t):
    owner = t[2].lower()
    shells = {"windowsterminal", "cmd", "powershell", "pwsh", "code", "cursor", "conhost", "mintty", "bash", "zsh", "wt"}
    return owner in shells

# ---------------------------------------------------------------------------
# 坐标解析（全部命令共用）
# ---------------------------------------------------------------------------
def extract_ref(rest):
    for i, a in enumerate(rest):
        if a.startswith("@"):
            return rest.pop(i)[1:], rest
    return None, rest

def resolve(xs, ys, ww, wh, ref=None):
    """eN@json / @图按图上像素 / <=1 归一化 / >1 窗口内点数。返回 (x,y,note)。"""
    if xs.startswith("e") and "@" in xs:
        refname, jsonp = xs.split("@", 1)
        try:
            with open(jsonp, encoding="utf-8") as f:
                obj = json.load(f)
            el = next(e for e in obj["elements"] if e["ref"] == refname)
            return (el["cx"], el["cy"], "\u5143\u7d20%s\u300c%s\u300d\u2192\u7a97\u53e3\u5185(%d,%d)" % (refname, el.get("title", ""), el["cx"], el["cy"]))
        except Exception:
            die("\u5728 %s \u91cc\u627e\u4e0d\u5230\u5143\u7d20 %s\uff08\u5148 win see \u751f\u6210\uff09" % (jsonp, refname), 2)
    x = float(xs); y = float(ys)
    if ref:
        sz = image_size(ref)
        if not sz:
            die("\u8bfb\u4e0d\u5230\u622a\u56fe %s" % ref)
        iw, ih = sz
        px, py = x * ww / iw, y * wh / ih
        return (px, py, "\u56fe\u4e0a(%.0f,%.0f)@%dx%d\u2192\u7a97\u53e3\u5185(%.0f,%.0f)" % (x, y, iw, ih, px, py))
    if x <= 1.0 and y <= 1.0 and ww > 0 and wh > 0:
        return (x * ww, y * wh, "\u5f52\u4e00\u5316(%.4f,%.4f)\u2192\u7a97\u53e3\u5185(%.0f,%.0f)" % (x, y, x * ww, y * wh))
    return (x, y, "\u7a97\u53e3\u5185\u7edd\u5bf9(%.0f,%.0f)" % (x, y))

def guard_on_screen(p, ctx=""):
    sw, sh = virtual_screen()
    if not (0 <= p[0] <= sw and 0 <= p[1] <= sh):
        die("\u62d2\u7edd\u70b9\u51fb\u5c4f\u5e55\u5916\u5750\u6807 (%d,%d)\uff0c\u5c4f\u5e55 %dx%d\u3002%s" % (p[0], p[1], sw, sh, ctx), 2)

def presence_preview():
    v = user_idle()
    return "\u5728\u573a=pass(\u7a7a\u95f2%.1fs)" % v if v >= IDLE_NEED else "\u5728\u573a=WAIT(\u7528\u6237%.1fs\u524d\u52a8\u8fc7\uff0c\u6700\u591a\u7b49%.0fs)" % (v, IDLE_WAIT)

def lock_preview():
    h = lock_holder()
    return "\u501f\u7126\u70b9\u9501=HELD(pid %d)" % h[0] if h else "\u501f\u7126\u70b9\u9501=free"

def gate_user_presence(what, alt):
    passed, waited, last = wait_user_idle()
    if not passed:
        die("refused: 用户正在用电脑（%.1f 秒前还在动键鼠），等了 %.0f 秒仍未停手，没有打断他。\n%s%s\uff1b\u786e\u5b9e\u8981\u73b0\u5728\u505a\u52a0 --force" % (last, waited, what, alt), 2)
    if waited > 0.5:
        print("\u23f3 用户刚在动键鼠，等他停手 %.1f 秒后才动手" % waited)
# -*- coding: utf-8 -*-
# Part4: 事件投递（全局 SendInput + 后台 PostMessage）+ 窗口激活

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MK_LBUTTON = 0x0001
WM_CHAR = 0x0102
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]

class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

def move_mouse(p):
    win32api.SetCursorPos((int(p[0]), int(p[1])))
    mark_synthetic()

def click_global(p):
    win32api.SetCursorPos((int(p[0]), int(p[1])))
    win32api.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    mark_synthetic()

def click_bg(hwnd, wx, wy):
    """PostMessage 零焦点点击：事件直投「落点处最内层子窗口」，不抢焦点/不受遮挡。"""
    cxp, cyp = window_to_client(hwnd, int(wx), int(wy))
    child = user32.RealChildWindowFromPoint(hwnd, wt.POINT(cxp, cyp))
    if not child:
        child = hwnd
    lparam = (cyp << 16) | (cxp & 0xFFFF)
    user32.PostMessageW(child, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    time.sleep(0.05)
    user32.PostMessageW(child, WM_LBUTTONUP, 0, lparam)

def window_to_client(hwnd, wx, wy):
    """窗口矩形(含边框)坐标 -> 客户区坐标。GetClientRect 原点为客户区左上。"""
    wr = win32gui.GetWindowRect(hwnd)
    cr = win32gui.GetClientRect(hwnd)
    return (wx - (wr[0] - cr[0]), wy - (wr[1] - cr[1]))

def hover_global(start, p, hold_ms, steps=8):
    for i in range(1, steps + 1):
        f = i / steps
        move_mouse((start[0] + (p[0] - start[0]) * f, start[1] + (p[1] - start[1]) * f))
        time.sleep(0.02)
    time.sleep(hold_ms / 1000.0)

def scroll_global(p, dy, dx=0, steps=6):
    move_mouse(p)
    time.sleep(0.12)
    for _ in range(steps):
        win32api.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, dy, 0)
        time.sleep(0.04)
    mark_synthetic()

def key_global(vk, ctrl=False):
    if ctrl:
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        time.sleep(0.03)
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    win32api.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    if ctrl:
        win32api.keybd_event(win32con.VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    mark_synthetic()

def type_unicode_global(text):
    """SendInput KEYEVENTF_UNICODE：中文无需输入法，等价原版 keyboardSetUnicodeString。"""
    for i in range(0, len(text), 8):
        units = list(text[i:i + 8])
        for down in (True, False):
            n = len(units)
            arr = (INPUT * n)()
            for j, ch in enumerate(units):
                arr[j].type = 1  # INPUT_KEYBOARD
                arr[j].ki.wVk = 0
                arr[j].ki.wScan = ord(ch)
                arr[j].ki.dwFlags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
            user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))
        time.sleep(0.01)
    mark_synthetic()

_EDIT_CLASSES = ("Edit", "RICHEDIT50W", "RichEditD2DPT", "Scintilla", "RichEdit20W", "RichEdit20A")

def _focused_edit(hwnd):
    """找目标窗口里当前有键盘焦点的编辑子控件；找不到回退第一个编辑子控件。"""
    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                    ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                    ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]
    try:
        _, tid = win32process.GetWindowThreadProcessId(hwnd)
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
            if win32gui.GetClassName(gti.hwndFocus) in _EDIT_CLASSES:
                return gti.hwndFocus
    except Exception:
        pass
    first = [hwnd]
    def cb(h, _):
        if win32gui.GetClassName(h) in _EDIT_CLASSES:
            first.append(h)
        return True
    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return first[-1]

def type_bg(hwnd, text):
    """PostMessage WM_CHAR 后台输字（中文/emoji 均可：按 UTF-16 码元发）。"""
    target = _focused_edit(hwnd)
    units = []
    for ch in text:
        cp = ord(ch)
        if cp >= 0x10000:
            v = cp - 0x10000
            units.append(0xD800 + (v >> 10))
            units.append(0xDC00 + (v & 0x3FF))
        else:
            units.append(cp)
    for u in units:
        user32.PostMessageW(target, WM_CHAR, u, 0)
    time.sleep(0.3)

def activate_window(hwnd):
    """激活窗口（ShowWindow + SetForegroundWindow），失败时点一下中心补焦点。"""
    try:
        win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    except Exception:
        pass
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    if win32gui.GetForegroundWindow() != hwnd:
        t = win_info(hwnd)
        if t:
            click_global((t[4] + t[6] / 2, t[5] + t[7] / 2))

def frontmost():
    fg = win32gui.GetForegroundWindow()
    if not fg:
        return "?"
    _, pid = win32process.GetWindowThreadProcessId(fg)
    return "%s(%s)" % (win32gui.GetWindowText(fg) or "?", owner_name(pid))
# -*- coding: utf-8 -*-
# Part5: UIA（Windows 无障碍树，等价原版 AX）。用 pywinauto(backend='uia')。

def _desktop_uia():
    from pywinauto import Desktop
    return Desktop(backend="uia")

ACTIONABLE = {"Button", "Edit", "CheckBox", "RadioButton", "ComboBox", "TabItem",
              "Hyperlink", "Slider", "MenuItem", "Spinner", "Text"}
EDITABLE = {"Edit", "Document"}

def _walk(element_info, depth, budget, visit):
    """UIA 元素树遍历，带深度/总数预算。返回 (visited, aborted)。"""
    visited = 0
    try:
        for ei in element_info.descendants():
            if depth > 12 or visited >= budget:
                return (visited, True)
            visited += 1
            if not visit(ei):
                return (visited, False)
            sub, ab = _walk(ei, depth + 1, budget - visited, visit)
            visited += sub
            if ab:
                return (visited, True)
    except Exception:
        pass
    return (visited, False)

def uia_count_editables(pid, print_them=False):
    try:
        d = _desktop_uia()
        wins = d.windows(process=pid)
    except Exception:
        return 0
    n = 0
    def visit(ei):
        nonlocal n
        try:
            ct = ei.control_type
            if ct in EDITABLE:
                n += 1
                if print_them and n <= 5:
                    print("  可编辑: %s %s" % (ct, ei.name or ""))
        except Exception:
            pass
        return True
    for w in wins:
        try:
            _walk(w.element_info, 0, 6000, visit)
        except Exception:
            pass
    return n

def uia_elements(pid, max_elems=150, win_hwnd=None):
    """返回 [{ref,role,title,cx,cy,enabled}]，坐标已折算成窗口内坐标。"""
    try:
        d = _desktop_uia()
        wins = d.windows(process=pid)
    except Exception:
        return []
    if win_hwnd:
        wins = [w for w in wins if w.handle == win_hwnd]
    elements = []
    wx = wy = 0
    if win_hwnd:
        t = win_info(win_hwnd)
        if t:
            wx, wy = t[4], t[5]
    def visit(ei):
        nonlocal elements
        if len(elements) >= max_elems:
            return False
        try:
            ct = ei.control_type
            if ct not in ACTIONABLE:
                return True
            try:
                r = ei.rectangle
            except Exception:
                return True
            if r.width() <= 0 or r.height() <= 0:
                return True
            cx = r.left + r.width() / 2 - wx
            cy = r.top + r.height() / 2 - wy
            title = ei.name or ""
            en = True
            try:
                en = ei.is_enabled
            except Exception:
                pass
            elements.append({"ref": "e%d" % (len(elements) + 1), "role": ct,
                             "title": title[:40], "cx": int(cx), "cy": int(cy),
                             "enabled": bool(en)})
        except Exception:
            pass
        return True
    for w in wins:
        _walk(w.element_info, 0, 4000, visit)
        if len(elements) >= max_elems:
            break
    return elements

def uia_set_text(pid, text):
    """给第一个可编辑控件设值并读回。返回 (ok, 读回文本, 说明)。"""
    try:
        d = _desktop_uia()
        wins = d.windows(process=pid)
    except Exception as e:
        return (False, "", "UIA 不可用: %s" % e)
    for w in wins:
        try:
            for ei in _all_descendants(w.element_info, 4000):
                try:
                    if ei.control_type not in EDITABLE:
                        continue
                    wrap = ei.wrapper_object()
                    wrap.set_edit_text(text)
                    rb = ""
                    try:
                        rb = wrap.get_value() or ""
                    except Exception:
                        try:
                            rb = wrap.window_text() or ""
                        except Exception:
                            rb = ""
                    return (True, rb, "setValue \u8bfb\u56de=\u201c%s\u201d" % rb[:40])
                except Exception:
                    continue
        except Exception:
            continue
    return (False, "", "\u6ca1\u6709\u53ef\u7f16\u8f91\u63a7\u4ef6 \u2192 Chromium \u7cfb\u8d70 cdp.js insert\uff0c\u5176\u5b83 mac op")

def _all_descendants(element_info, budget):
    out = []
    def visit(ei):
        if len(out) >= budget:
            return False
        out.append(ei)
        return True
    _walk(element_info, 0, budget, visit)
    return out
# -*- coding: utf-8 -*-
# Part6: HUD —— 屏幕四角脉冲取景框（win hud 子进程入口）
# 约束：鼠标穿透(WS_EX_TRANSPARENT) · 永不抢焦点 · 对屏幕捕获隐身(WDA_EXCLUDEFROMCAPTURE)

WDA_EXCLUDEFROMCAPTURE = 0x00000011
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
LWA_ALPHA = 0x00000002

_g = {"hwnd": None, "style": "corner", "text": "", "pulse": 1.0, "t0": time.time(), "ms": 1600, "hdc": None}

def _draw_corner(dc, x0, y0, x1, y1, alpha):
    import win32gui, win32con, win32api
    w, h = x1 - x0, y1 - y0
    arm, t = 110, 9
    pen = win32gui.CreatePen(win32con.PS_SOLID, t, win32api.RGB(217, 89, 46))
    old = win32gui.SelectObject(dc, pen)
    for (cx, cy, dx, dy) in [(x0 + 2, y0 + 2, 1, 1), (x1 - 2, y0 + 2, -1, 1),
                             (x0 + 2, y1 - 2, 1, -1), (x1 - 2, y1 - 2, -1, -1)]:
        win32gui.MoveToEx(dc, cx + arm * dx, cy, None)
        win32gui.LineTo(dc, cx, cy)
        win32gui.LineTo(dc, cx, cy + arm * dy)
    win32gui.SelectObject(dc, old)
    win32gui.DeleteObject(pen)

def _wndproc(hwnd, msg, wparam, lparam):
    import win32gui, win32con, win32api
    if msg == win32con.WM_PAINT:
        ps = win32gui.BeginPaint(hwnd)
        dc = win32gui.GetDC(hwnd)
        rect = win32gui.GetClientRect(hwnd)
        _draw_corner(dc, 0, 0, rect[2], rect[3], _g["pulse"])
        if _g["text"]:
            win32gui.SetTextColor(dc, win32api.RGB(255, 255, 255))
            win32gui.TextOut(dc, rect[2] // 2 - 120, rect[3] - 70, _g["text"], len(_g["text"]))
        win32gui.ReleaseDC(hwnd, dc)
        win32gui.EndPaint(hwnd, ps)
        return 0
    if msg == win32con.WM_TIMER:
        el = time.time() - _g["t0"]
        if el * 1000 >= _g["ms"]:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return 0
        _g["pulse"] = 0.30 + 0.70 * abs(__import__("math").sin(el * 5.0))
        alpha = int(255 * _g["pulse"])
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
        win32gui.InvalidateRect(hwnd, None, True)
        return 0
    if msg == win32con.WM_CLOSE:
        win32gui.DestroyWindow(hwnd)
        return 0
    if msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

def show_hud(ms, text, style="corner"):
    import win32gui, win32con
    wc = win32gui.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "HuashuHUD"
    wc.lpfnWndProc = _wndproc
    wc.hbrBackground = win32con.COLOR_WINDOW + 1
    wc.hCursor = 0
    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass
    sw, sh = virtual_screen()
    hwnd = win32gui.CreateWindowEx(
        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
        "HuashuHUD", "", win32con.WS_POPUP, 0, 0, sw, sh, 0, 0, wc.hInstance, None)
    _g["hwnd"] = hwnd; _g["text"] = text; _g["ms"] = ms; _g["style"] = style; _g["t0"] = time.time()
    # 对屏幕捕获隐身（取证截图不带 HUD）
    ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    win32gui.SetTimer(hwnd, 1, 33, None)
    win32gui.PumpMessages()
# -*- coding: utf-8 -*-
# Part7: shotSmart（兄弟窗口回退 + Chromium 探测 + 存活 CDP 端口） + 读命令

def siblings(t):
    hwnd, pid, owner, title, x, y, w, h, cls, on = t
    out = []
    for s in _enumerate():
        if s[0] == hwnd:
            continue
        if abs(s[4] - x) < 3 and abs(s[5] - y) < 3 and abs(s[6] - w) < 3 and abs(s[7] - h) < 3:
            if s[1] == pid or s[2] == owner or owner == s[2]:
                out.append(s)
    return out

def is_chromium_family(pid):
    """Chromium 系检测：可执行文件同目录有没有 chrome_elf.dll / resources.pak / electron.exe。
    比查进程树可靠（跟原版 probe.sh 查 Frameworks 同理）。"""
    try:
        path = _exe_path(pid)
        if not path:
            return False
        d = os.path.dirname(path)
        base = os.path.basename(path).lower()
        if base in ("chrome.exe", "msedge.exe", "electron.exe", "chrome_proxy.exe"):
            return True
        return (os.path.isfile(os.path.join(d, "chrome_elf.dll"))
                or os.path.isfile(os.path.join(d, "resources.pak"))
                or os.path.isfile(os.path.join(d, "electron.exe")))
    except Exception:
        return False

def _exe_path(pid):
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        buf = ctypes.create_unicode_buffer(512)
        sz = ctypes.c_ulong(512)
        if kernel32.QueryFullProcessImageNameW(h.handle, 0, buf, ctypes.byref(sz)):
            return buf.value
    except Exception:
        pass
    return None

def live_cdp_port(pid):
    """扫进程族 LISTEN 端口，认得 CDP 的返回端口号。--noproxy 防本地代理吞掉。"""
    try:
        ps = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    pids = {pid}
    ports = set()
    for line in ps.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            try:
                p = int(parts[4])
            except Exception:
                continue
            if p in pids:
                try:
                    port = int(parts[1].rsplit(":", 1)[-1])
                    ports.add(port)
                except Exception:
                    pass
    for port in ports:
        try:
            r = subprocess.run(["curl", "-s", "--noproxy", "*", "-m", "2",
                                "http://127.0.0.1:%d/json/version" % port],
                               capture_output=True, text=True, timeout=5).stdout
            if "webSocketDebuggerUrl" in r:
                return port
        except Exception:
            pass
    return None

def shot_smart(hwnd, path):
    """截窗，失败自诊断并给出 CDP 配方。返回 (实际截到的 t, 附加说明)。"""
    t = win_info(hwnd)
    if not t:
        die("\u7a97\u53e3 %d \u4e0d\u5b58\u5728\u6216 id \u5df2\u8fc7\u671f \u2192 win windows \u91cd\u53d6" % hwnd)
    if screen_locked():
        die("\u5c4f\u5e55\u5df2\u9501\u5b9a\uff1a\u622a\u56fe\u5fc5\u8d25\uff08CDP / UIA \u4e0d\u53d7\u5f71\u54cd\uff09\u3002\u89e3\u9501\u540e\u91cd\u8bd5\u6216\u6539\u8d70 CDP shot", 2)
    if capture_window(hwnd, path):
        return (t, "")
    for s in siblings(t):
        if capture_window(s[0], path):
            return (s, "\n  \u2139\ufe0f \u58f3\u7a97\u53e3\u622a\u4e0d\u5230\uff0c\u5df2\u6539\u622a\u540c\u4f4d\u7f6e\u5144\u5f1f\u7a97\u53e3 id=%d\uff08%s\uff09\u3002\u4ee5\u540e\u76f4\u63a5\u7528\u8fd9\u4e2a id" % (s[0], s[2]))
    if is_chromium_family(t[1]):
        cdpjs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp.js")
        head = "\u7a97\u53e3 %d\uff08%s\uff09\u622a\u4e0d\u5230\uff1a\u5b83\u662f Chromium \u7cfb\uff08Electron/CEF\uff09\uff0c\u540e\u53f0\u4e0d\u4fdd\u7559\u6e32\u67d3\u5e27 \u2192 PrintWindow \u8fd9\u6761\u8def\u5bf9\u5b83\u65e0\u89e3\u3002\u6539\u8d70 CDP\uff08\u4e0d\u53d7\u906e\u6321\u5f71\u54cd\uff09\uff1a\n" % (hwnd, t[2])
        port = live_cdp_port(t[1])
        if port:
            die(head + "  \u5b83\u5df2\u7ecf\u5f00\u7740 CDP \u7aef\u53e3 %d\uff0c\u65e0\u9700\u91cd\u542f\uff1a\n  node %s %d shot auto <%s>" % (port, cdpjs, port, path), 2)
        die(head + "  win open \"%s\" --cdp 9333 --relaunch   # \u26a0\ufe0f \u4f1a\u91cd\u542f\u5b83\uff0c\u672a\u4fdd\u5b58\u5185\u5bb9\u4f1a\u4e22\uff0c\u5148\u8ddf\u7528\u6237\u8bf4\n  node %s 9333 shot auto <%s>" % (t[2], cdpjs, path), 2)
    die("\u7a97\u53e3 %d\uff08%s\uff09\u622a\u4e0d\u5230\uff0c\u4e14\u6ca1\u6709\u53ef\u7528\u7684\u5144\u5f1f\u7a97\u53e3\u3002\u53ef\u80fd\uff1a\u4ece\u672a\u6e32\u67d3\u8fc7\u6216\u5df2\u6700\u5c0f\u5316 \u2192 \u8ba9\u5b83\u663e\u793a\u4e00\u6b21" % (hwnd, t[2]), 1)
# -*- coding: utf-8 -*-
# Part8: 命令实现（读侧）

def cmd_windows(rest):
    all_flag = "--all" in rest
    rest = [a for a in rest if a != "--all"]
    filt = rest[0] if rest else None
    hidden = 0
    for t in sorted(_enumerate(), key=lambda x: (x[2].lower(), -x[0])):
        if filt and filt.lower() not in t[2].lower() and filt.lower() not in t[3].lower():
            continue
        if not all_flag and is_junk(t):
            hidden += 1
            continue
        print(fmt(t))
    if hidden:
        print("（已隐藏 %d 个系统残留/浮层窗口，--all 显示）" % hidden)

def cmd_shot(rest):
    if len(rest) < 2:
        die("\u7528\u6cd5: win shot <hwnd> <\u8def\u5f84>")
    hwnd = int(rest[0])
    t, note = shot_smart(hwnd, rest[1])
    print("shot id=%d -> %s  %s  %s%s" % (t[0], rest[1], describe_shot(rest[1]), receipt(t), note))

def cmd_shotfg(rest):
    if len(rest) < 2:
        die("\u7528\u6cd5: win shotfg <hwnd> <\u8def\u5f84>")
    hwnd, out = int(rest[0]), rest[1]
    t, note = shot_smart(hwnd, out)
    if not looks_blank(out):
        print("\u622a\u56fe: %s %s\n\u540e\u53f0\u76f4\u63a5\u622a\u5230\uff0c\u672a\u52a8\u7126\u70b9 \u2705 %s%s" % (out, describe_shot(out), receipt(t), note))
        return
    # 后台截不到才借焦点：过锁 + 在场闸
    holder = acquire_focus_lock()
    if holder:
        die("refused: \u53e6\u4e00\u4e2a win \u8fdb\u7a0b\uff08pid %d\uff09\u6b63\u6301\u6709\u501f\u7126\u70b9\u9501\u3002\u7b49\u5b83\u7ed3\u675f\uff0c\u6216\u5bf9 Chromium \u7cfb\u6539\u7528 cdp.js shot" % holder, 2)
    gate_user_presence("\u4e3a\u4e00\u5f20\u622a\u56fe\u4e0d\u503c\u5f97\u62a2\u4ed6\u7684\u7126\u70b9\u3002", alt="Chromium \u7cfb\u6539 cdp.js shot\uff08\u4e0d\u53d7\u906e\u6321/Space \u5f71\u54cd\uff09")
    flash_hud("\u6b63\u5728\u622a\u300c%s\u300d\u7684\u7a97\u53e3" % t[2], 1200)
    was_front = win32gui.GetForegroundWindow() == hwnd
    t0 = time.time()
    if not was_front:
        activate_window(hwnd)
    # 渲染追赶：连续两张不再空才算画完
    tmpA = os.path.join(tempfile.gettempdir(), "shotfg-%d-a.png" % hwnd)
    settled = False
    for _ in range(12):
        time.sleep(0.06)
        if not capture_window(hwnd, tmpA) or looks_blank(tmpA):
            continue
        time.sleep(0.06)
        if capture_window(hwnd, out) and not looks_blank(out):
            settled = True
            break
    if not settled:
        capture_window(hwnd, out)
    held = time.time() - t0
    try:
        os.remove(tmpA)
    except Exception:
        pass
    if was_front:
        print("\u622a\u56fe: %s\n\u76ee\u6807\u672c\u5c31\u5728\u524d\u53f0\uff0c\u672a\u52a8\u7126\u70b9" % out)
    else:
        print("\u622a\u56fe: %s\n\u540e\u53f0\u662f\u7a7a\u56fe\uff0c\u501f\u7126\u70b9 %.2f \u79d2\u540e\u5df2\u8fd8\u7ed9\u7528\u6237" % (out, held))
    if looks_blank(out):
        print("\u26a0\ufe0f \u501f\u4e86\u7126\u70b9\u4ecd\u662f\u7a7a\u56fe\uff08\u6700\u5c0f\u5316 / \u7981\u6b62\u6355\u83b7\uff09\u3002Chromium \u7cfb\u6539 cdp.js shot")

def cmd_see(rest):
    out = None
    if "--out" in rest:
        i = rest.index("--out")
        if i + 1 < len(rest):
            out = rest[i + 1]
            rest = rest[:i] + rest[i + 2:]
    if not rest:
        die("\u7528\u6cd5: win see <hwnd|owner\u5173\u952e\u8bcd> [--out \u8def\u5f84]")
    target = None
    try:
        hwnd = int(rest[0])
        target = win_info(hwnd)
    except ValueError:
        cands = [t for t in _enumerate() if not is_junk(t) and rest[0].lower() in t[2].lower()]
        if not cands:
            die("\u6ca1\u6709 owner \u542b\u300c%s\u300d\u7684\u7a97\u53e3 \u2192 win windows \u770b\u770b" % rest[0])
        target = max(cands, key=lambda t: t[6] * t[7])
    if not target:
        die("\u7a97\u53e3 %d \u4e0d\u5b58\u5728" % hwnd)
    out_path = out or os.path.join(tempfile.gettempdir(), "see-%d.png" % target[0])
    raw = os.path.join(tempfile.gettempdir(), "see-raw-%d.png" % target[0])
    t, note = shot_smart(target[0], raw)
    ds = downsample(raw, out_path, 1400)
    if not ds:
        die("\u964d\u91c7\u6837\u5931\u8d25")
    print("\u622a\u56fe: %s %dx%dpx\uff08\u70b9\u5b83\uff1awin clickin %d <\u56fe\u4e0ax> <\u56fe\u4e0ay> @%s\uff09" % (out_path, ds[0], ds[1], t[0], out_path))
    print(receipt(t) + ("" if is_onscreen(t[0]) else "  \u26a0\ufe0f \u4e0d\u53ef\u89c1\uff08\u6700\u5c0f\u5316/\u4e0d\u5728\u5f53\u524d\u684c\u9762\uff09\uff1a\u8bfb\u53ef\u4ee5\uff0c\u5199\u53ea\u80fd\u8d70 CDP \u6216 win op") + note)
    if looks_blank(out_path):
        print("\u26a0\ufe0f \u56fe\u5224\u7a7a\uff1a\u540e\u53f0\u4e0d\u6e32\u67d3\uff0c\u6539 win shotfg \u6216 cdp.js shot")
    # UIA 元素表
    elements = uia_elements(t[1], 150, win_hwnd=t[0])
    if not elements:
        print("UIA \u5143\u7d20\u8868: \u65e0\uff08app \u4e0d\u66b4\u9732 / \u6811\u65ad\u5728\u7f51\u9875\u5c42\uff09\u3002\u7528\u56fe\u4e0a\u50cf\u7d20\u5750\u6807\u70b9\uff0cChromium \u7cfb\u8d70 cdp.js snapshot")
    else:
        jsonp = out_path + ".json"
        obj = {"receipt": receipt(t), "window": {"id": t[0], "pid": t[1], "w": t[6], "h": t[7]}, "elements": elements}
        with open(jsonp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        print("UIA \u5143\u7d20\u8868 %d \u4e2a\uff08\u70b9\u5b83\uff1awin clickin %d e3@%s\uff09:" % (len(elements), t[0], jsonp))
        for e in elements:
            dis = " [disabled]" if not e["enabled"] else ""
            print("  %s %s \u201c%s\u201d (%d,%d)%s" % (e["ref"], e["role"], e["title"], e["cx"], e["cy"], dis))

def cmd_idle(rest):
    s = user_idle()
    fg = frontmost()
    verdict = "\U0001f534 \u7528\u6237\u5728\u573a\uff08\u501f\u7126\u70b9\u6863\u4f1a\u5148\u7b49\u4ed6\u505c\u624b\uff0c\u6700\u591a %d \u79d2\uff09" % IDLE_WAIT if s < IDLE_NEED else "\U0001f7e2 \u7528\u6237\u7a7a\u95f2\uff08\u501f\u7126\u70b9\u6863\u53ef\u76f4\u63a5\u8d70\uff09"
    print("\u952e\u9f20\u7a7a\u95f2 %.1f \u79d2\uff08\u9608\u503c %.0f\uff09  \u524d\u53f0: %s\n%s" % (s, IDLE_NEED, fg, verdict))
    h = lock_holder()
    if h:
        print("\U0001f512 \u501f\u7126\u70b9\u9501\u88ab pid %d \u6301\u6709 %.1f \u79d2\u524d\u53d6\u5f97" % (h[0], h[1]))
    else:
        print("\U0001f513 \u501f\u7126\u70b9\u9501\u7a7a\u95f2")
    print("\u6ce8\uff1a\u8bfb\u64cd\u4f5c\uff08windows/shot/see/ax/CDP\uff09\u4e0d\u770b\u8fd9\u4e2a\u95f8\uff0c\u4efb\u4f55\u65f6\u5019\u90fd\u80fd\u8dd1")

def cmd_frontmost(rest):
    print(frontmost())

def flash_hud(text, ms=1600):
    """借焦点/接管屏幕前闪一下。子进程异步跑；WIN_HUD=0 关掉。"""
    if os.environ.get("WIN_HUD") == "0":
        return
    exe = sys.executable
    try:
        subprocess.Popen([exe, os.path.abspath(__file__), "hud", str(ms), text],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass

def cmd_hud(rest):
    ms = int(float(rest[0])) if rest and rest[0].lstrip("-").isdigit() or (rest and _isnum(rest[0])) else 1600
    text = rest[1] if len(rest) > 1 else "huashu-win-use \u6b63\u5728\u63a5\u7ba1\u5c4f\u5e55"
    style = rest[2] if len(rest) > 2 else os.environ.get("WIN_HUD_STYLE", "corner")
    show_hud(ms, text, style)

def _isnum(s):
    try:
        float(s)
        return True
    except Exception:
        return False
# -*- coding: utf-8 -*-
# Part9: 写命令（走全局流的过闸；后台直投不过闸）

def cmd_clickin(rest, hover=False):
    if len(rest) < 3:
        die("\u7528\u6cd5: win %s <hwnd> <x> <y> [@\u56fe] [--dry]" % ("hoverin" if hover else "clickin"))
    dry = "--dry" in rest; rest = [a for a in rest if a != "--dry"]
    hwnd = int(rest[0])
    ref, rest = extract_ref(rest)
    t = win_info(hwnd)
    if not t:
        die("\u7a97\u53e3 %d \u4e0d\u5b58\u5728\uff08\u53ef\u80fd\u5df2\u5173\u95ed\u6216 id \u8fc7\u671f\uff09" % hwnd)
    c = resolve(rest[1], rest[2], t[6], t[7], ref)
    edge = ""
    if c[0] < 8 and c[1] < 8:
        edge = "  \u26a0\ufe0f \u843d\u70b9\u8d34\u7740\u7a97\u53e3\u5de6\u4e0a\u89d2\uff0c\u5927\u6982\u7387\u5750\u6807\u7b97\u9519"
    gp = (t[4] + c[0], t[5] + c[1])
    if dry:
        print("dry: %s \u7a97\u53e3%d %s on=%d%s" % ("hoverin" if hover else "clickin", hwnd, c[2], 1 if is_onscreen(hwnd) else 0, edge))
        g = "  \u95f8\u9884\u68c0: \u53ef\u89c1=" + ("pass" if is_onscreen(hwnd) else "BLOCK(\u6700\u5c0f\u5316/\u4e0d\u5728\u5f53\u524d\u684c\u9762)")
        top = top_window_at(gp[0], gp[1])
        if top and top[0] != hwnd:
            g += "  \u906e\u6321=BLOCK(\u4e0a\u5c42\u662f\u300c%s\u300d\u7a97\u53e3 %d)" % (top[2], top[0])
        else:
            g += "  \u906e\u6321=pass"
        g += "  " + presence_preview()
        print(g)
        if "--force" in rest:
            print("  \u26a0\ufe0f --force \u4f1a\u62c6\u6389\u4ee5\u4e0a\u5168\u90e8\u95f8")
        return
    if not is_onscreen(hwnd):
        die("refused: \u7a97\u53e3 %d\uff08%s\uff09\u6700\u5c0f\u5316/\u4e0d\u5728\u5f53\u524d\u684c\u9762\uff0c\u5408\u6210\u4e8b\u4ef6\u4f1a\u6253\u5230\u522b\u7684\u7a97\u53e3\u4e0a\u3002\u7528 win op\uff08\u4f1a\u6fc0\u6d3b\uff09\u3001CDP\uff0c\u6216\u8bf7\u7528\u6237\u628a\u5b83\u79fb\u5230\u5f53\u524d\u684c\u9762" % (hwnd, t[2]), 2)
    guard_on_screen(gp, "\u591a\u534a\u662f\u6fc0\u6d3b\u540e\u7a97\u53e3\u4ecd\u5728\u52a8\u753b\u3002\u7b49 1 \u79d2\u91cd\u8dd1")
    if "--force" not in rest:
        top = top_window_at(gp[0], gp[1])
        if top and top[0] != hwnd:
            die("refused: occluded \u843d\u70b9 (%d,%d) \u4e0a\u5c42\u662f\u300c%s\u300d\u7684\u7a97\u53e3 %d\uff0c\u70b9\u4e0b\u53bb\u4f1a\u622a\u5230\u5b83\u800c\u4e0d\u662f\u76ee\u6807 %s\u3002\n\u7528 win op\uff08\u4f1a\u6fc0\u6d3b\u76ee\u6807\uff09\u3001CDP\uff0c\u6216\u8bf7\u7528\u6237\u628a\u76ee\u6807\u7a97\u53e3\u79fb\u5230\u6700\u524d\uff1b\u786e\u8ba4\u8981\u70b9\u52a0 --force" % (gp[0], gp[1], top[2], top[0], t[2]), 2)
    gate_user_presence("%s \u8d70\u5168\u5c40\u4e8b\u4ef6\u6d41\uff0c\u8ddf\u4ed6\u5171\u7528\u540c\u4e00\u4e2a\u9f20\u6807\u6307\u9488\u3002" % ("hoverin" if hover else "clickin"), alt="\u6539\u8d70 CDP \u6216 win op --bg")
    if hover:
        hold = 700
        if len(rest) > 3 and _isnum(rest[3]):
            hold = int(float(rest[3]))
        start = win32api.GetCursorPos()
        hover_global(start, gp, hold)
        print("hovered %s held %dms \u2014 \u5149\u6807\u7559\u5728\u539f\u5730\uff0c\u540e\u7eed click \u53ef\u76f4\u63a5\u70b9\u5f39\u51fa\u9879" % (c[2], hold))
    else:
        saved = win32api.GetCursorPos()
        click_global(gp)
        move_mouse(saved)
        print("clicked %s \u2192 \u5168\u5c40(%d,%d) pid %d%s\n\u2192 \u5fc5\u987b\u56de\u8bfb\uff1awin shot %d <\u8def\u5f84>\uff0c\u770b\u5e94\u7528\u72b6\u6001\u6307\u793a\u5668\u4e0d\u662f\u770b\u6709\u6ca1\u6709\u5b57" % (c[2], gp[0], gp[1], t[1], edge, hwnd))

def cmd_click(rest):
    is_click = True
    if len(rest) < 3:
        die("\u7528\u6cd5: win click <pid> <x> <y> [@\u5168\u5c4f\u56fe] [bg] [--dry]")
    dry = "--dry" in rest; rest = [a for a in rest if a != "--dry"]
    ref, rest = extract_ref(rest)
    pid = int(rest[0])
    sw, sh = virtual_screen()
    c = resolve(rest[1], rest[2], sw, sh, ref)
    guard_on_screen(c[:2], "")
    bg = len(rest) > 3 and rest[3] == "bg"
    gp = (int(c[0]), int(c[1]))
    if dry:
        print("dry: click \u5168\u5c40 %s%s" % (c[2], " postToPid" if bg else ""))
        if not bg:
            top = top_window_at(gp[0], gp[1])
            print("  \u95f8\u9884\u68c0: \u843d\u70b9\u4e0a\u5c42=" + (("\u662f\u76ee\u6807 pid %d" % pid) if (top and top[1] == pid) else "BLOCK(\u300c%s\u300dpid %d)" % (top[2], top[1]) if top else "\u65e0\u7a97\u53e3") + "  " + presence_preview())
        return
    if not bg and "--force" not in rest:
        top = top_window_at(gp[0], gp[1])
        if top and top[1] != pid:
            die("refused: occluded \u843d\u70b9 (%d,%d) \u4e0a\u5c42\u662f\u300c%s\u300d(pid %d)\uff0c\u4e0d\u662f\u76ee\u6807 pid %d\u3002\u70b9\u4e0b\u53bb\u4f1a\u622a\u5230\u5b83\u3002\n\u6539 win clickin\uff08\u6309\u7a97\u53e3\u5b9a\u4f4d\uff09\u6216 CDP\uff1b\u786e\u8ba4\u8981\u70b9\u52a0 --force" % (gp[0], gp[1], top[2], top[1], pid), 2)
    if not bg:
        gate_user_presence("\u5168\u5c40\u5750\u6807\u70b9\u51fb/\u60ac\u505c\u8ddf\u7528\u6237\u5171\u7528\u540c\u4e00\u4e2a\u9f20\u6807\u6307\u9488\u3002", alt="\u6539 win clickin\uff08\u6309\u7a97\u53e3\u5b9a\u4f4d\uff09\u6216 CDP")
    saved = win32api.GetCursorPos()
    if bg:
        t = win_info(user32.WindowFromPoint(wt.POINT(gp[0], gp[1])))
        hwnd = t[0] if t else 0
        if hwnd:
            wx, wy = gp[0] - t[4], gp[1] - t[5]
            click_bg(hwnd, wx, wy)
        print("clicked %s postToPid -> pid %d" % (c[2], pid))
    else:
        click_global(gp)
        move_mouse(saved)
        print("clicked %s global -> pid %d" % (c[2], pid))

def cmd_hover(rest):
    if len(rest) < 2:
        die("\u7528\u6cd5: win hover <x> <y> [holdms] [--dry]")
    dry = "--dry" in rest; rest = [a for a in rest if a != "--dry"]
    ref, rest = extract_ref(rest)
    sw, sh = virtual_screen()
    c = resolve(rest[0], rest[1], sw, sh, ref)
    guard_on_screen(c[:2], "")
    hold = 700
    if len(rest) > 2 and _isnum(rest[2]):
        hold = int(float(rest[2]))
    if dry:
        print("dry: hover \u5168\u5c40 %s hold %dms" % (c[2], hold))
        return
    gate_user_presence("\u60ac\u505c\u8d70\u5168\u5c40\u6d41\u3002", alt="Chromium \u7cfb\u6539 cdp.js")
    start = win32api.GetCursorPos()
    hover_global(start, (c[0], c[1]), hold)
    print("hovered %s held %dms" % (c[2], hold))

def cmd_scroll(rest):
    if len(rest) < 3:
        die("\u7528\u6cd5: win scroll <x> <y> <dy> [dx] [steps]")
    sx, sy, dy = int(float(rest[0])), int(float(rest[1])), int(rest[2])
    dx = int(rest[3]) if len(rest) > 3 and _isnum(rest[3]) else 0
    steps = int(rest[4]) if len(rest) > 4 and _isnum(rest[4]) else 6
    gate_user_presence("\u6eda\u8f6e\u8d70\u5168\u5c40\u6d41\uff0c\u4f1a\u6eda\u5230\u4ed6\u6b63\u5728\u770b\u7684\u7a97\u53e3\u4e0a\u3002", alt="Chromium \u7cfb\u6539 cdp.js")
    scroll_global((sx, sy), dy, dx, steps)
    print("scrolled at (%d,%d) dy=%d dx=%d \u00d7%d" % (sx, sy, dy, dx, steps))

def cmd_type(rest):
    if len(rest) < 2:
        die("\u7528\u6cd5: win type <pid> <\u6587\u672c> [global]")
    dry = "--dry" in rest; rest = [a for a in rest if a != "--dry"]
    pid = int(rest[0])
    text = rest[1]
    global_ = len(rest) > 2 and rest[2] == "global"
    if dry:
        fg = win32gui.GetForegroundWindow()
        fpid = win32process.GetWindowThreadProcessId(fg)[1]
        print("dry: type %d \u5b57 -> pid %d %s" % (len(text), pid, "global(\u6253\u7ed9\u524d\u53f0)" if global_ else "postToPid(\u76f4\u6295\u8fdb\u7a0b)"))
        if global_:
            print("  \u95f8\u9884\u68c0: " + ("frontmost=pass" if fpid == pid else "frontmost=BLOCK(\u524d\u53f0 pid %d \u4e0d\u662f\u76ee\u6807)" % fpid) + "  " + presence_preview())
        else:
            print("  \u95f8\u9884\u68c0: postToPid \u4e0d\u78b0\u524d\u53f0\uff0c\u65e0\u95f8")
        return
    if global_:
        fg = win32gui.GetForegroundWindow()
        fpid = win32process.GetWindowThreadProcessId(fg)[1]
        if "--force" not in rest and fpid != pid:
            die("refused: frontmost \u524d\u53f0 pid %d \u4e0d\u662f\u76ee\u6807 pid %d\uff0cglobal \u4f1a\u628a\u5b57\u6253\u8fdb\u524d\u53f0\u90a3\u4e2a\u7a97\u53e3\u3002\n\u53bb\u6389 global \u8d70 postToPid\uff08\u76f4\u6295\u76ee\u6807\u8fdb\u7a0b\uff09\uff0c\u6216\u5148 win op \u6fc0\u6d3b\u76ee\u6807", 2)
        gate_user_presence("global \u4f1a\u628a\u5b57\u6253\u8fdb\u4ed6\u7684\u524d\u53f0\u7a97\u53e3\u3002", alt="\u53bb\u6389 global \u8d70 postToPid")
        type_unicode_global(text)
    else:
        t = win_info_for_pid(pid)
        if t:
            type_bg(t[0], text)
        print("typed %d chars -> pid %d postToPid\uff08\u8fd4\u56de\u503c\u4e0d\u4ee3\u8868\u751f\u6548\uff0c\u5fc5\u987b\u56de\u8bfb\uff09" % (len(text), pid))
        return
    print("typed %d chars -> pid %d global\uff08\u8fd4\u56de\u503c\u4e0d\u4ee3\u8868\u751f\u6548\uff0c\u5fc5\u987b\u56de\u8bfb\uff09" % (len(text), pid))

def win_info_for_pid(pid):
    for t in _enumerate():
        if t[1] == pid:
            return t
    return None

def cmd_key(rest):
    if len(rest) < 2:
        die("\u7528\u6cd5: win key <pid> <vkey> [ctrl]")
    dry = "--dry" in rest; rest = [a for a in rest if a != "--dry"]
    pid = int(rest[0])
    vk = int(rest[1])
    ctrl = len(rest) > 2 and rest[2] == "ctrl"
    fg = win32gui.GetForegroundWindow()
    fpid = win32process.GetWindowThreadProcessId(fg)[1] if fg else -1
    fname = win_info(fg)[2] if win_info(fg) else "?"
    if dry:
        print("dry: key %d%s \u4f1a\u843d\u5728\u524d\u53f0\u300c%s\u300d(pid %d)" % (vk, "+ctrl" if ctrl else "", fname, fpid))
        print("  \u95f8\u9884\u68c0: frontmost=" + ("pass(\u524d\u53f0\u5c31\u662f\u76ee\u6807)" if fpid == pid else "BLOCK(\u76ee\u6807 pid %d \u4e0d\u5728\u524d\u53f0)" % pid) + "  " + presence_preview())
        return
    if "--force" not in rest and fpid != pid:
        die("refused: frontmost \u524d\u53f0\u662f\u300c%s\u300d(pid %d)\uff0c\u4e0d\u662f\u76ee\u6807 pid %d\u3002\u952e\u6309\u843d\u5728\u524d\u53f0\u7a97\u53e3\u4e0a\uff0c\u4f1a\u6253\u9519\u5730\u65b9\u3002\n\u5148 win op \u6fc0\u6d3b\u76ee\u6807\u518d\u6309\uff0c\u6216\u52a0 --force" % (fname, fpid, pid), 2)
    if "--force" not in rest and vk == 13 and is_shellish(win_info(fg) or (0, 0, "", "", 0, 0, 0, 0, "", False)):
        die("refused: \u524d\u53f0\u662f %s\uff08\u7ec8\u7aef/IDE \u7c7b\uff09\uff0c\u8fd9\u4e00\u4e0b\u56de\u8f66\u7b49\u4e8e\u6267\u884c\u547d\u4ee4\u3002\u786e\u8ba4\u8981\u6267\u884c\u52a0 --force" % fname, 2)
    gate_user_presence("\u952e\u6309\u4f1a\u843d\u5728\u4ed6\u7684\u524d\u53f0\u7a97\u53e3\u300c%s\u300d\u4e0a\u3002" % fname, alt="\u7b49\u4ed6\u505c\u624b")
    key_global(vk, ctrl)
    print("key %d%s -> \u524d\u53f0 app\uff08pid %d \u4ec5\u56de\u663e\uff09" % (vk, "+ctrl" if ctrl else "", pid))

def cmd_ax(rest):
    if not rest:
        die("\u7528\u6cd5: win ax <pid>")
    pid = int(rest[0])
    n1 = uia_count_editables(pid, False)
    time.sleep(0.4)
    n2 = uia_count_editables(pid, True)
    n = max(n1, n2)
    print("UIA \u53ef\u7528  windows=\uff08\u770b win windows\uff09  \u53ef\u7f16\u8f91\u63a7\u4ef6=%d%s" % (n, "\uff08\u4e24\u6b21\u5206\u522b %d/%d\uff09" % (n1, n2) if n1 != n2 else ""))
    print("\u2192 L1 \u6709\u5e0c\u671b\uff0c\u4f46\u5fc5\u987b win axset \u5b9e\u5199\u5e76\u622a\u56fe\u770b\u72b6\u6001\u6307\u793a\u5668\u624d\u7b97\u6570" if n >= 1 else "\u2192 L1 \u65e0\u671b\uff1aChromium \u7cfb\u8d70 CDP\uff0c\u5176\u5b83\u8d70 L2 \u5750\u6807")

def cmd_axset(rest):
    if len(rest) < 2:
        die("\u7528\u6cd5: win axset <pid> <\u6587\u672c>")
    pid, text = int(rest[0]), rest[1]
    ok, rb, note = uia_set_text(pid, text)
    print("setValue %s\n\u2192 err \u548c\u8bfb\u56de\u90fd\u4e0d\u662f\u5224\u636e\u3002\u622a\u56fe\u770b\u53d1\u9001\u952e\u6709\u6ca1\u6709\u7531\u7070\u53d8\u4eae\uff1b\u6ca1\u53d8\u5c31\u662f app \u4e0d\u8ba4\u8fd9\u6b21\u8f93\u5165\uff0c\u6539 win op \u6216 CDP" % note)
# -*- coding: utf-8 -*-
# Part10: op —— 写操作默认入口（后台档 -> 借焦点档，阶梯 + 三道闸）

def cmd_op(rest):
    if len(rest) < 4:
        die("\u7528\u6cd5: win op <hwnd> <x> <y> <\u6587\u672c> [@\u56fe] [send <sx> <sy>] [shot <\u8def\u5f84>] [--bg|--fast] [--force] [--dry]")
    dry = "--dry" in rest; rest = [a for a in rest if a != "--dry"]
    force = "--force" in rest; rest = [a for a in rest if a != "--force"]
    fast = "--fast" in rest; rest = [a for a in rest if a != "--fast"]
    bg_only = "--bg" in rest; rest = [a for a in rest if a != "--bg"]
    hwnd = int(rest[0])
    ref, rest = extract_ref(rest)
    text = rest[3]
    sendXY = None; shot_path = None
    i = 4
    while i < len(rest):
        if rest[i] == "send" and i + 2 < len(rest):
            sendXY = (rest[i + 1], rest[i + 2]); i += 3
        elif rest[i] == "shot" and i + 1 < len(rest):
            shot_path = rest[i + 1]; i += 2
        else:
            i += 1
    t = win_info(hwnd)
    if not t:
        die("\u7a97\u53e3 %d \u4e0d\u5b58\u5728\u3002\u5148 win windows \u91cd\u53d6 id" % hwnd)
    c = resolve(rest[1], rest[2], t[6], t[7], ref)
    sc = resolve(sendXY[0], sendXY[1], t[6], t[7], ref) if sendXY else None
    plan = "--fast \u76f4\u63a5\u501f\u7126\u70b9" if fast else ("--bg \u53ea\u8d70\u540e\u53f0\u6863\uff0c\u4e0d\u5347\u7ea7" if bg_only else "\u9ed8\u8ba4\u9636\u68af\uff1a\u5148\u540e\u53f0\u6863\uff0c\u672a\u751f\u6548\u624d\u5347\u7ea7\u501f\u7126\u70b9")
    if dry:
        print("dry: op \u7a97\u53e3%d\uff08%s\uff09\u70b9\u51fb %s%s \u8f93\u5165%d\u5b57 on=%d" % (
            hwnd, t[2], c[2], "\uff0c\u53d1\u9001 %s" % sc[2] if sc else "\uff0c\u4e0d\u53d1\u9001", len(text), 1 if is_onscreen(hwnd) else 0))
        print("  \u7b56\u7565: %s   \u7528\u6237\u6b64\u523b\u7a7a\u95f2 %.1f \u79d2\uff08<%d \u79d2\u89c6\u4e3a\u4ed6\u5728\u573a\uff0c\u501f\u7126\u70b9\u6863\u4f1a\u5148\u7b49\uff09" % (plan, user_idle(), IDLE_NEED))
        g = "  \u95f8\u9884\u68c0: \u7ec8\u7aefsend=" + ("BLOCK(%s \u662f\u7ec8\u7aef/IDE \u7c7b\uff0c\u6309\u53d1\u9001\u7b49\u4e8e\u6267\u884c\u547d\u4ee4\uff0c\u9700 --force)" % t[2] if (is_shellish(t) and sc) else "pass")
        g += "  " + lock_preview() + "  " + presence_preview()
        print(g)
        if force:
            print("  \u26a0\ufe0f --force \u4f1a\u62c6\u6389\u4ee5\u4e0a\u5168\u90e8\u95f8")
        return
    # \u95f8\u4e00\uff1a\u7ec8\u7aef/IDE \u7c7b\u7a97\u53e3\u6309\u300c\u53d1\u9001\u300d\u7b49\u4e8e\u6267\u884c\u547d\u4ee4
    if is_shellish(t) and sc and not force:
        die("refused: %s \u662f\u7ec8\u7aef/IDE \u7c7b\u7a97\u53e3\uff0c\u70b9\u53d1\u9001\u7b49\u4e8e\u6267\u884c\u547d\u4ee4\u3002\u53ea\u586b\u4e0d\u53d1\u5c31\u53bb\u6389 send\uff1b\u786e\u8ba4\u8981\u6267\u884c\u52a0 --force" % t[2], 2)

    # ---- \u7b2c\u4e00\u6863\uff1aPostMessage \u540e\u53f0\u6295\u9012\uff08\u96f6\u7126\u70b9/\u4e0d\u53d7\u906e\u6321\uff09----
    def background_write():
        after = shot_path or os.path.join(tempfile.gettempdir(), "opbg-after-%d.png" % hwnd)
        bp = os.path.join(tempfile.gettempdir(), "opbg-before-%d.png" % hwnd)
        have_before = capture_window(hwnd, bp)
        click_bg(hwnd, c[0], c[1])
        time.sleep(0.12)
        if text:
            type_bg(hwnd, text)
        if sc:
            time.sleep(0.12)
            click_bg(hwnd, sc[0], sc[1])
        log = "\u2460 \u540e\u53f0\u6863 PostMessage\uff08\u96f6\u7126\u70b9/\u4e0d\u53d7\u906e\u6321\uff09\u2192 pid %d \u70b9\u51fb %s%s" % (t[1], c[2], "\uff0c\u5df2\u53d1\u9001" if sc else "\uff0c\u672a\u53d1\u9001")
        time.sleep(2.0)
        if not capture_window(hwnd, after):
            return ("unverifiable", log + "\n  effect=unverifiable \u622a\u56fe\u5931\u8d25\uff0c\u5224\u4e0d\u51fa\u540e\u53f0\u8fd9\u4e00\u4e0b\u6709\u6ca1\u6709\u751f\u6548")
        log += "\n\u622a\u56fe: %s %s" % (after, describe_shot(after))
        if looks_blank(after):
            return ("unverifiable", log + "\n  effect=unverifiable \u622a\u56de\u6765\u662f\u7a7a\u56fe\uff0c\u5dee\u5206\u4e0d\u53ef\u4fe1")
        if not have_before or looks_blank(bp):
            return ("unverifiable", log + "\n  effect=unverifiable \u57fa\u7ebf\u56fe\u7f3a\u5931\u6216\u4e3a\u7a7a\uff0c\u5dee\u5206\u4e0d\u53ef\u4fe1")
        d = diff_report(bp, after, c[0] / max(t[6], 1), c[1] / max(t[7], 1))
        ok = "effect=confirmed" in d or "effect=partial" in d
        return ("ok" if ok else "noop", log + "\n" + d)

    if not fast:
        status, log = background_write()
        print(log)
        if status == "ok":
            print("\u2192 \u540e\u53f0\u6863\u5df2\u751f\u6548\uff0c\u5168\u7a0b\u96f6\u7126\u70b9\uff0c\u7528\u6237\u5b8c\u5168\u4e0d\u53d7\u6253\u6270\u3002\u4ecd\u9700\u786e\u8ba4\u53d8\u7684\u662f\u4f60\u8981\u7684\uff08\u770b\u72b6\u6001\u6307\u793a\u5668/\u526f\u4f5c\u7528\uff09")
            return
        if bg_only:
            die("\u2192 --bg \u53ea\u8d70\u540e\u53f0\u6863\uff0c\u4e0d\u5347\u7ea7\u3002status=%s" % status, 2)
        if status == "unverifiable":
            die("\n\u2192 \U0001f534 \u505c\u5728\u8fd9\u91cc\uff0c\u4e0d\u81ea\u52a8\u5347\u7ea7\u501f\u7126\u70b9\u3002\n\u540e\u53f0\u6863\u7684\u4e8b\u4ef6\u5df2\u7ecf\u6295\u51fa\u53bb\u4e86\uff0c\u4f46\u9a8c\u8bc1\u4e0d\u53ef\u4fe1\uff08\u622a\u56fe\u7a7a/\u5931\u8d25\uff09\uff0c**\u5b83\u5f88\u53ef\u80fd\u5df2\u7ecf\u751f\u6548**\u2014\u2014\u518d\u8d70\u4e00\u904d\u501f\u7126\u70b9\u5c31\u662f\u91cd\u590d\u5199\u5165\u3002\n\u5148\u6362\u786c\u5224\u636e\u786e\u8ba4\uff1aCDP \u8bfb DOM / UIA \u8bfb\u503c / app \u81ea\u5df1\u7684\u72b6\u6001\u6307\u793a\u5668 / \u843d\u76d8\u6587\u4ef6\u3002\n\u786e\u8ba4\u300c\u786e\u5b9e\u6ca1\u751f\u6548\u300d\u518d\u8dd1 win op --fast\uff08\u8df3\u8fc7\u540e\u53f0\u6863\u76f4\u63a5\u501f\u7126\u70b9\uff09\u3002", 2)
        print("\n\u2b06\ufe0f \u540e\u53f0\u6863\u5224\u5b9a\u672a\u751f\u6548\uff08\u622a\u56fe\u53ef\u4fe1\u3001\u843d\u70b9\u96f6\u53d8\u5316\uff09\uff0c\u5347\u7ea7\u5230\u501f\u7126\u70b9\u6863")

    # ---- \u7b2c\u4e8c\u6863\uff1a\u501f\u7126\u70b9\uff08\u8fc7\u501f\u7126\u70b9\u9501 + \u7528\u6237\u5728\u573a\u95f8\uff09----
    holder = acquire_focus_lock()
    if holder:
        die("refused: \u53e6\u4e00\u4e2a win \u8fdb\u7a0b\uff08pid %d\uff09\u6b63\u6301\u6709\u501f\u7126\u70b9\u9501\uff0c\u7b49 5 \u79d2\u672a\u91ca\u653e\u3002\u7b49\u5b83\u7ed3\u675f\uff0c\u6216\u6539\u8d70 CDP / --bg" % holder, 2)
    gate_user_presence("\u501f\u7126\u70b9\u4f1a\u62a2\u8d70\u4ed6\u6b63\u5728\u7528\u7684\u524d\u53f0\u7a97\u53e3\u3002", alt="\u6539\u8d70 CDP \u6216 win op --bg\uff0c\u6216\u7b49\u4ed6\u624b\u505c\u4e0b\u6765\u91cd\u8bd5")
    flash_hud("huashu-win-use \u6b63\u5728\u64cd\u4f5c\u300c%s\u300d" % t[2])
    prev = win32gui.GetForegroundWindow()
    already_front = prev == hwnd
    before_path = None
    if shot_path:
        bp = os.path.join(tempfile.gettempdir(), "macop-before-%d.png" % hwnd)
        if capture_window(hwnd, bp):
            before_path = bp
    t0 = time.time()
    if not already_front:
        activate_window(hwnd)
    time.sleep(0.6)
    # watchdog: \u8d85\u65f6\u4e00\u5f8b\u8fd8\u7126\u70b9\u518d\u9000\u51fa
    def watchdog():
        time.sleep(12)
        restore_prev(prev, already_front, hwnd)
        print("refused: op \u8d85\u8fc7 12 \u79d2\u672a\u5b8c\u6210\uff0c\u5df2\u8fd8\u7126\u70b9\u3002\u591a\u534a\u662f\u6a21\u6001\u5bf9\u8bdd\u6846\u963b\u585e\u6216 app \u65e0\u54cd\u5e94")
        sys.exit(2)
    if os.name == "nt":
        pass
    o = stable_origin(hwnd)
    if not o:
        restore_prev(prev, already_front, hwnd)
        die("\u8bfb\u4e0d\u5230\u7a97\u53e3 %d \u7684\u4f4d\u7f6e\uff0c\u53ef\u80fd\u5df2\u5173\u95ed" % hwnd)
    gp = (o[0] + c[0], o[1] + c[1])
    sw, sh = virtual_screen()
    if not (0 <= gp[0] <= sw and 0 <= gp[1] <= sh):
        restore_prev(prev, already_front, hwnd)
        die("\u62d2\u7edd\u70b9\u51fb\u5c4f\u5e55\u5916\u5750\u6807 (%d,%d)\u3002\u7126\u70b9\u5df2\u8fd8\u539f\u3002\u7a97\u53e3 origin=(%d,%d)" % (gp[0], gp[1], o[0], o[1]), 2)
    click_global(gp)
    if text:
        type_unicode_global(text)
    send_note = "\uff0c\u672a\u53d1\u9001"
    if sc:
        time.sleep(0.12)
        click_global((o[0] + sc[0], o[1] + sc[1]))
        send_note = "\uff0c\u5df2\u70b9\u53d1\u9001 %s" % sc[2]
    held = time.time() - t0
    restore_prev(prev, already_front, hwnd)
    release_focus_lock()
    shot_note = ""
    if shot_path:
        time.sleep(3.0)
        if capture_window(hwnd, shot_path):
            shot_note = "\n\u622a\u56fe: %s %s" % (shot_path, describe_shot(shot_path))
            if before_path:
                shot_note += "\n" + diff_report(before_path, shot_path, c[0] / max(t[6], 1), c[1] / max(t[7], 1))
            else:
                shot_note += "\n  effect=unverifiable \u65e0\u57fa\u7ebf\u56fe\uff08\u64cd\u4f5c\u524d\u7a97\u53e3\u622a\u4e0d\u5230\uff09\uff0c\u5dee\u5206\u8df3\u8fc7"
        else:
            shot_note = "\n  effect=unverifiable \u622a\u56fe\u5931\u8d25\u3002\u64cd\u4f5c\u672a\u5fc5\u5931\u8d25\uff0c\u6362 CDP \u6216\u5168\u5c4f\u622a\u56fe\u4ea4\u53c9\u9a8c\u8bc1"
    focus_note = "\u76ee\u6807\u672c\u5c31\u5728\u524d\u53f0\uff0c\u672a\u52a8\u7126\u70b9\uff08\u8017\u65f6 %.2f \u79d2\uff09" % held if already_front else "\u7126\u70b9\u5360\u7528 %.2f \u79d2\uff0c\u5df2\u8fd8\u7ed9\u7528\u6237" % held
    print(focus_note + "\n\u70b9\u51fb: %s" % c[2] + send_note + shot_note)
    print("\u2192 effect \u53ea\u8bf4\u300c\u6709\u6ca1\u6709\u53d1\u751f\u4e8b\u60c5\u300d\uff1b\u786e\u8ba4\u300c\u53d1\u751f\u7684\u662f\u4f60\u8981\u7684\u300d\u770b\u5e94\u7528\u72b6\u6001\u6307\u793a\u5668\uff08\u53d1\u9001\u952e\u7531\u7070\u53d8\u4eae\uff09\u6216\u526f\u4f5c\u7528\uff08\u4efb\u52a1\u8fdb\u5217\u8868\uff09")

def restore_prev(prev, already_front, hwnd):
    if already_front:
        return
    try:
        if prev and win32gui.IsWindow(prev):
            win32gui.SetForegroundWindow(prev)
    except Exception:
        pass
    time.sleep(0.1)
# -*- coding: utf-8 -*-
# Part11: open 命令（按名称解析应用 + CDP 端口启动）+ 主分发

APP_PATHS = [
    r"C:\Program Files", r"C:\Program Files (x86)",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code"),
]

def find_exe(name):
    """按名称解析应用路径。name 可带 .exe；返回 (path, 说明)。"""
    if not name.lower().endswith(".exe"):
        name += ".exe"
    # 1) 直接路径
    if os.path.isfile(name):
        return (os.path.abspath(name), "\u76f4\u63a5\u8def\u5f84")
    # 2) 注册表 App Paths
    import winreg
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            k = winreg.OpenKey(root, r"Software\Microsoft\Windows\CurrentVersion\App Paths\%s" % name)
            val, _ = winreg.QueryValueEx(k, "")
            winreg.CloseKey(k)
            if val and os.path.isfile(val):
                return (val, "\u6ce8\u518c\u8868 App Paths")
        except Exception:
            pass
    # 3) 常用安装目录递归搜（按可执行名精确匹配）
    for base in APP_PATHS:
        if not base or not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            if "node_modules" in root or ".git" in root:
                continue
            if name.lower() in (f.lower() for f in files):
                return (os.path.join(root, name), "\u5b89\u88c5\u76ee\u5f55\u641c\u7d22")
            if len(root) - len(base) > 60:
                dirs[:] = []
    # 4) 运行中的进程反查
    try:
        ps = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15).stdout
        for line in ps.splitlines():
            if name.lower() in line.lower():
                pid = line.split('","')[1].strip('"')
                try:
                    from ctypes import wintypes as _wt
                    h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
                    buf = ctypes.create_unicode_buffer(512)
                    sz = ctypes.c_ulong(512)
                    if kernel32.QueryFullProcessImageNameW(h.handle, 0, buf, ctypes.byref(sz)):
                        return (buf.value, "\u8fd0\u884c\u4e2d\u8fdb\u7a0b\u53cd\u67e5")
                except Exception:
                    pass
    except Exception:
        pass
    return (None, "")

def _file_version(path):
    try:
        info = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return "\u672a\u77e5"

def cmd_open(rest):
    relaunch = "--relaunch" in rest; rest = [a for a in rest if a != "--relaunch"]
    dry = "--dry" in rest; rest = [a for a in rest if a != "--dry"]
    cdp_port = None
    if "--cdp" in rest:
        i = rest.index("--cdp")
        if i + 1 < len(rest):
            cdp_port = rest[i + 1]
            rest = rest[:i] + rest[i + 2:]
    if not rest:
        die("\u7528\u6cd5: win open <\u540d\u79f0|\u8def\u5f84> [--cdp \u7aef\u53e3] [--relaunch] [--dry]")
    name = rest[0]
    if os.path.isfile(name) and (name.lower().endswith(".exe") or "." not in os.path.basename(name)):
        path = os.path.abspath(name)
        how = "\u76f4\u63a5\u8def\u5f84"
    else:
        path, how = find_exe(name)
    if not path:
        die("\u627e\u4e0d\u5230\u5e94\u7528\u300c%s\u300d\u3002\u8bd5\u7ed9 .exe \u7684\u5b8c\u6574\u8def\u5f84\uff0c\u6216\u5148\u542f\u52a8\u5b83\u518d\u7528\u8fdb\u7a0b\u540d" % name)
    ver = _file_version(path)
    print("app: %s  v%s  (\u6765\u6e90: %s)" % (path, ver, how))
    # 是否已在运行
    running_pid = None
    try:
        ps = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15).stdout
        base = os.path.basename(path).lower()
        for line in ps.splitlines():
            if line.lower().startswith('"%s"' % base):
                running_pid = int(line.split('","')[1].strip('"'))
                break
    except Exception:
        pass
    print("\u8fd0\u884c\u4e2d=%s" % ("\u662f(pid %d)" % running_pid if running_pid else "\u5426"))
    def cdp_alive(port):
        try:
            r = subprocess.run(["curl", "-s", "--noproxy", "*", "-m", "2", "http://127.0.0.1:%s/json/version" % port],
                               capture_output=True, text=True, timeout=5).stdout
            return "webSocketDebuggerUrl" in r
        except Exception:
            return False
    if cdp_port:
        if cdp_alive(cdp_port):
            print("CDP: \u7aef\u53e3 %s \u5df2\u901a \u2705  \u4e0b\u4e00\u6b65: node cdp.js %s list" % (cdp_port, cdp_port))
            return
        if running_pid:
            if not relaunch:
                die("CDP \u672a\u5f00\u800c app \u6b63\u5728\u8fd0\u884c\u3002\u5e26\u7aef\u53e3\u5fc5\u987b\u91cd\u542f\u5b83\uff08\u4f1a\u5173\u6389\u5f53\u524d\u7a97\u53e3\uff09\uff1a\u786e\u8ba4\u540e\u52a0 --relaunch \u91cd\u8dd1\uff0c\u6216\u8ba9\u7528\u6237\u81ea\u5df1\u9000\u51fa\u540e\u518d\u8dd1\u672c\u547d\u4ee4", 2)
            if dry:
                print("dry: \u5c06\u9000\u51fa pid %d \u5e76\u4ee5 --remote-debugging-port=%s \u91cd\u542f" % (running_pid, cdp_port))
                return
            subprocess.run(["taskkill", "/pid", str(running_pid), "/f"], capture_output=True, timeout=10)
            time.sleep(2.0)
        if dry:
            print("dry: \u542f\u52a8 %s --remote-debugging-port=%s" % (path, cdp_port))
            return
        subprocess.Popen([path, "--remote-debugging-port=%s" % cdp_port], cwd=os.path.dirname(path))
        for _ in range(40):
            if cdp_alive(cdp_port):
                print("CDP: \u7aef\u53e3 %s \u5df2\u901a \u2705\uff08\u542f\u52a8\u540e\u7b49\u4e86\u7ea6 %ds\uff09  \u4e0b\u4e00\u6b65: node cdp.js %s list" % (cdp_port, _, cdp_port))
                return
            time.sleep(0.5)
        die("\u542f\u52a8\u4e86\u4f46 20 \u79d2\u5185 %s/json/version \u6ca1\u5e94\u7b54\u3002\u53ef\u80fd\u4e0d\u662f Chromium \u7cfb\uff0c\u6216\u7aef\u53e3\u88ab\u5360\uff08\u6362\u4e00\u4e2a >1024 \u7684\uff09" % cdp_port, 2)
    if dry:
        print("dry: \u542f\u52a8 %s" % path)
        return
    subprocess.Popen([path], cwd=os.path.dirname(path))
    print("\u5df2\u542f\u52a8 %s\uff08app \u4f1a\u5230\u524d\u53f0\uff1b\u53ea\u8bfb\u63a2\u6d4b\u8bf7\u7528 probe.py\uff0c\u4e0d\u9700\u8981\u542f\u52a8\uff09" % name)

# ---------------------------------------------------------------------------
# 主分发
# ---------------------------------------------------------------------------
COMMANDS = {
    "windows": cmd_windows, "shot": cmd_shot, "shotfg": cmd_shotfg, "see": cmd_see,
    "idle": cmd_idle, "frontmost": cmd_frontmost, "hud": cmd_hud,
    "clickin": lambda r: cmd_clickin(r, False), "hoverin": lambda r: cmd_clickin(r, True),
    "click": cmd_click, "hover": cmd_hover, "scroll": cmd_scroll,
    "type": cmd_type, "key": cmd_key, "ax": cmd_ax, "axset": cmd_axset,
    "op": cmd_op, "open": cmd_open,
}

def main():
    args = sys.argv
    if len(args) < 2 or args[1] in ("-h", "--help", "help"):
        print(USAGE)
        return
    cmd = args[1]
    rest = list(args[2:])
    if cmd not in COMMANDS:
        die("\u672a\u77e5\u547d\u4ee4: %s\n%s" % (cmd, USAGE))
    COMMANDS[cmd](rest)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
