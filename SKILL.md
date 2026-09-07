# huashu-win-use — 在 Windows 上用原教旨主义方式操控任何原生 App（移植自 huashu-mac-use）

## 一句话
让任何 coding agent（Claude Code / Codex / OpenCode / OpenClaw 等）操控**Windows 上没有 API 的原生 app**——读后台、写不打扰、每步留取证。

- 原版：[alchaincyf/huashu-mac-use](https://github.com/alchaincyf/huashu-mac-use)（花叔 / 小猫补光灯 / 女娲.skill 作者，MIT）
- 移植：本目录（`scripts/win.py`，~1500 行 Python 3 + pywin32/UIA/Pillow，无 C++）
- 参考：原版 `SKILL.md` + `references/`（控制面详解 / 权限与故障 / 踩坑实录 / 取证规范 / app档案）
- 安装依赖：`build.cmd`（`C:\Program Files\Python311\python.exe` 需与本机一致）

> **铁律**：本 Skill 是**最后手段**，不是首选。任何有原生 API / CLI / 注册表路径的操作，优先用原生接口；本 Skill 只管「没有 API 的原生 GUI」。

---

## 核心哲学（从原版继承）

1. **先探测再选层**：4 层控制面（L0 结构接口 → L1 UIA 树 → L2 窗口坐标 → L3 像素），**永远先问「有没有更好的路」**
2. **写默认零焦点**：写操作直投目标窗口（`PostMessage` + `RealChildWindowFromPoint` 落子控件），**不抢焦点、不受遮挡影响**。过 4 道闸才允许借焦点
3. **工具返回成功 ≠ 生效**：验证阶梯直达「应用状态指示器 / 副作用」，回报 `effect=confirmed/partial/suspected_noop`
4. **停手线**：发布 / 付款 / 删除等不可逆动作强制交还人类

---

## 架构总览

```
Agent 发出 "win <cmd> <args>"
        ↓
dispatch（scripts/win.py:1448）
        ↓
┌──────────────────────────────────────────────────────┐
│ 读命令（原封不动照搬原版）                           │
│   windows [--all] [filter]     枚举所有/过滤窗口     │
│   see <hwnd> --out <path>       截当前前台 + 兄弟窗口│
│   shot <hwnd> --out <path>      PrintWindow 单窗      │
│   shotfg <hwnd> --out <path>    前台闸后前台截图      │
│   frontmost / idle / hud off    基础状态查询          │
└──────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────┐
│ 写命令（4 道闸 + 后台优先 + op 阶梯）                │
│   clickin / clickinfg / hoverin                      │
│   click / clickfg / hover                            │
│   type / typefg                                      │
│   key / keyfg                                        │
│   ax / axset（UIA 无障碍树交互）                    │
│   op <hwnd> <x> <y> <text> <after_shot>             │  ← 默认入口
└──────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────┐
│ 四层控制面 + 3 个验证阶梯                            │
│  1. 窗口存在 → 2. 子窗口坐标确认 → 3. 截屏确认       │
└──────────────────────────────────────────────────────┘
```

---

## 用法速查

### 前置：装环境
```bash
call D:\小宝输出\huashu-win-use\build.cmd
:: 之后每次用 .venv\Scripts\python.exe scripts\win.py <cmd> ...
```

### 读命令
```bash
:: 列出所有窗口（按 owner 排序，owner 为空时 owner=0）
python scripts/win.py windows

:: 只列出可见顶层窗口
python scripts/win.py windows

:: 过滤标题包含"记事本"的
python scripts/win.py windows 记事本

:: 看当前前台 / 空闲状态
python scripts/win.py frontmost
python scripts/win.py idle
python scripts/win.py hud off   ;; 关掉取景框 HUD（首次会自动开一次）

;; see = shot + ax（推荐用，看到 UIA 元素表 + 截图）
python scripts/win.py see <hwnd> --out assets/test.png
:: 输出里告诉你："点它：win clickin <hwnd> <图上x> <图上y> @assets/test.png"

;; shot = 后台单窗口截图（PrintWindow，被遮挡也能截到）
python scripts/win.py shot <hwnd> --out assets/w.png

;; shotfg = 前台闸通过后截图（保证视觉上看到的就是你看到的）
python scripts/win.py shotfg <hwnd> --out assets/w.png
```

### 写命令（默认走零焦点）

#### op —— 默认入口（后台档 → 借焦点档 阶梯）
```bash
:: 在 hwnd 坐标 (x,y) 处 "输入" text，然后截图 after_shot 做差分
python scripts/win.py op <hwnd> <x> <y> "你好 world" shot <after_shot_path>
:: 回报:
;;   effect=confirmed          ✅ 落点变了，写入有效
;;   effect=partial            ⚠️  落点变化很小，可能只是光标
;;   effect=suspected_noop     ❌ 没动静，停手（不重试）
```

#### click / type / key —— 走全局 SendInput（可见操作，借焦点）
```bash
:: click：全局鼠标移动到(x,y)左键单击（可见）
python scripts/win.py click <x> <y>

:: clickin：后台档点击（零焦点，不抢前台）
python scripts/win.py clickin <hwnd> <x> <y>

:: type：全局键盘打字（可见）
python scripts/win.py type "hello"

:: typein：后台档打字（零焦点）
python scripts/win.py typein <hwnd> "hello"

:: key：全局快捷键（可见）
python scripts/win.py key ctrl+c
python scripts/win.py keyalt tab
python scripts/win.py key shift+space
```

#### ax / axset —— UIA 无障碍树（等价 macOS AX）
```bash
:: ax：枚举窗口里的 actionable 控件
python scripts/win.py ax <hwnd>

:: axset：UIA SetPattern 写入，带文本读回验证
python scripts/win.py axset <hwnd> <element_id> "新文本"
python scripts/win.py axset <hwnd> e1 "设置文本" shot <after.png>
```

#### open —— 启动 / 切换应用
```bash
:: 按名称解析并启动（支持 Chromium 系 + Electron）
python scripts/win.py open chrome
python scripts/win.py open vscode
python scripts/win.py open "Microsoft Edge"

;; 启动 Chromium 带调试端口（自动探测端口，返回后连 CDP）
python scripts/win.py open chrome --cdp 2>&1 | grep "CDP port"
```

---

## 四道闸（写操作借焦点时才过）

| 闸 | 含义 | 判断 |
|----|------|------|
| 前台闸 | 是不是当前焦点窗口 | `GetForegroundWindow()` 匹配 |
| 遮挡闸 | 窗口是否被其他窗口盖住 | Win32 顶层窗口 Z-order 检测 |
| 在场闸 | 用户是否真的在桌前 | `GetLastInputInfo` 空闲 ≤ 30s + 合成尾迹 |
| 终端闸 | 是否从 Terminal 调用 | 非交互式 + 不在 TTY → 停手 |

**借焦点锁**：借之前写 lock 文件 + HUD 取景框（对屏幕捕获隐身），还之后清文件。多 agent 串行。

---

## 三层控制面（先问有没有更好的路）

| 层 | 方案 | 优先级 |
|----|------|--------|
| L0 结构接口 | CLI / PowerShell COM / DDE / WM_COPYDATA / UIA SetPattern | 1（首选） |
| L1 UIA 树 | pywinauto(backend='uia') 枚举 actionable 控件 | 2 |
| L2 坐标 + L3 像素 | PostMessage + SendInput + PrintWindow + 像素差分 | 3（最后手段） |

**验证阶梯**：
1. API 返回值 `ok:true`
2. 子窗口坐标确认（RealChildWindowFromPoint）
3. 截屏 + 像素差分（落点邻域 ±12% 对比前后）

---

## 关键差异：macOS ↔ Windows

| 能力 | macOS | Windows |
|------|-------|---------|
| 无障碍 | AX / UIElement | **UIA** (`pywinauto backend='uia'`) |
| 屏幕截 | screencapture / CGWindowList | **PrintWindow** (`/d/小宝输出/huashu-win-use/scripts/win.py` capture_window) |
| 鼠标事件 | `CGEventCreate`（全局） / `postEventToPid`（零焦点） | **SendInput**（全局） / **PostMessage + RealChildWindowFromPoint**（零焦点） |
| 键盘输入 | `CGEventKeyboard...` | **PostMessage + WM_CHAR**（零焦点，直达子控件） |
| 空闲检测 | `CGEventTapCreate`（全栈代理不可用，已弃用） | **GetLastInputInfo** |
| 合成尾迹 | 文件轮询 `/tmp/huashu-synthetic.trail` | 同路径机制（tempdir 可跨应用） |
| HUD | Quartz EventTap 可捕获（会暴露） | **WS_EX_TRANSPARENT + WDA_EXCLUDEFROMCAPTURE**（对屏幕捕获隐身） |
| Chromium | CDP 端口固定 9222 | **自动探测**（3 秒内响应 `/json/version` 的端口） |

---

## 安全边界（铁律）

- 不 rm / 不删除文件 / 不改系统配置
- 不访问浏览器凭据 / 密码管理器 / 私密目录
- 不做 HTTP 请求
- 不做 `OpenProcess(PROCESS_ALL_ACCESS)` ——`PROCESS_QUERY_LIMITED_INFORMATION` 够用
- 不注入 DLL / 不走 ROP / 不走物理设备模拟
- **不可逆操作必须经过人工确认**（op 报 suspected_noop 后绝不重试）

---

## 测试状态（本机实测）

```
win windows                  ✅ 窗口枚举、owner 解析正常
win frontmost                ✅ 取前台窗口（不依赖可见性）
win see <hwnd> --out ...     ✅ 截取当前前台+兄弟窗口
win shot <hwnd> --out ...    ✅ PrintWindow 后台截窗（768x605, 319色）
win op <hwnd> <x> <y> "文本" shot ...  ✅ 后台写入落地，diff_report 报告变化
win ax <hwnd>                ✅ UIA 元素表 39 个
win axset <hwnd> e1 "新文本" ✅ UIA SetPattern 写入
win open chrome --cdp        ✅ 探测调试端口返回
win idle                     ✅ 空闲检测
win hud off                  ✅ 清空 HUD 状态
```

---

## 文件清单

```
D:\小宝输出\huashu-win-use\
├── build.cmd                ;; 装依赖（改 PY 路径对应本机 Python）
├── .gitignore
├── scripts/
│   ├── win.py               ;; 主内核（~1500 行，纯 Python）
│   └── cdp.js               ;; Node CDP（原版，可直接用）
├── references/
│   └── hyper-vision.md      ;; 原版理论说明（保留作参考）
└── assets/                  ;; 截图/产物存放目录（空）
```

---

## 注意事项

1. **Python 路径**：`build.cmd` 里 `PY=C:\Program Files\Python311\python.exe`，按需修改
2. **权限**：部分 app 需要管理员权限才能 attach（UAC 弹窗的窗口特殊处理）
3. **输入法**：中文输入走 Unicode BMP 代理对直接发 WM_CHAR，绕过输入法；部分 app 不支持直接 Unicode 需配合输入法
4. **虚拟桌面**：Windows 10+ 虚拟桌面隔离，窗口枚举可能只看到当前桌面；跨桌面需要用其他方法
5. **远程桌面 / RDP**：RDP 会话的窗口枚举行为不同，测试时注意

---

*移植自 https://github.com/alchaincyf/huashu-mac-use（MIT），保持原版精神不变。*
