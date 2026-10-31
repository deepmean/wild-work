#!/usr/bin/env python3
"""对上游源码打 Windows 运行期修复补丁（幂等、锚点匹配、失败即报错）。

为什么用脚本而不是 .patch 文件：
  .patch 对上下文行敏感，上游改一行注释就 apply 失败，等于「上游一动，
  自动构建就挂」。这里改成按**函数级锚点**做精确替换：锚点还在就替换，
  已经是我们期望的形态就跳过（幂等，可重复执行），锚点消失就**显式失败**并
  打印期望内容 —— 绝不静默产出没打补丁的二进制。

修复内容：
  1. internal/platform/platform_windows.go
     OpenURL 从 `rundll32 url.dll,FileProtocolHandler` 改为 ShellExecuteW。
     前者先在 %TEMP% 落临时文件再转交外壳，%TEMP% 不可写时报
     「无权限在 %TEMP% 目录创建文件」——表现为面板里所有链接全部点不开。
  2. internal/cdp/browser.go
     CDP 临时 profile 改为**多候选目录**：程序 data/tmp → 用户缓存目录
     → 系统临时目录。上一版只试了「程序 data/tmp」，那里不可写时会静默
     退回 %TEMP%，于是用户看到的仍是原始报错
     （mkdir ...\\Temp\\wildwork-cdp-xxx: Access is denied），
     完全看不出到底是哪个候选不可写。这一版把**试过的候选路径**带进错误
     信息，并把每个候选的失败原因写进运行日志。

用法：
    python3 patches/apply-windows-fixes.py <源码根目录>
"""
from __future__ import annotations

import pathlib
import sys

# ── 修复 1：platform_windows.go 的 OpenURL ─────────────────────────────

PLATFORM_OLD_IMPORT = 'import (\n\t"os"\n\t"os/exec"\n'
PLATFORM_NEW_IMPORT = 'import (\n\t"fmt"\n\t"os"\n\t"os/exec"\n'

PLATFORM_OLD_OPENURL = '''// OpenURL 用系统默认方式打开 URL。
func OpenURL(url string) error {
\tcmd := exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
\tcmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
\treturn cmd.Start()
}
'''

PLATFORM_NEW_OPENURL = '''// shell32 的 ShellExecuteW：把 URL 直接交给外壳解析，全程不落盘。
var shell32 = windows.NewLazySystemDLL("shell32.dll")

var procShellExecuteW = shell32.NewProc("ShellExecuteW")

// SW_SHOWNORMAL
const swShowNormal = 1

// OpenURL 用系统默认浏览器打开 URL。
//
// ⚠️ 不要把实现改回 `rundll32 url.dll,FileProtocolHandler`：
// 那种写法会先在 %TEMP% 落一个临时文件、再转交外壳执行。一旦 %TEMP% 不可写
// （安全软件保护、路径被重定向、受限令牌、企业策略），OpenURL 就会返回
// 「无权限在 %TEMP% 目录创建文件」，表现为面板里**所有**打开链接的入口一起
// 失效（打开主界面、查看日志、各渠道登录授权、文档链接）。
// ShellExecuteW 直接把 URL 交给 shell 解析，不产生任何临时文件，也不依赖
// PATH 里能找到 rundll32。
func OpenURL(url string) error {
\tverb, err := syscall.UTF16PtrFromString("open")
\tif err != nil {
\t\treturn err
\t}
\tfile, err := syscall.UTF16PtrFromString(url)
\tif err != nil {
\t\treturn err
\t}
\tret, _, _ := procShellExecuteW.Call(
\t\t0,
\t\tuintptr(unsafe.Pointer(verb)),
\t\tuintptr(unsafe.Pointer(file)),
\t\t0,
\t\t0,
\t\tswShowNormal,
\t)
\t// ShellExecuteW 的约定：返回值 > 32 才算成功，<= 32 是错误码。
\tif ret <= 32 {
\t\treturn fmt.Errorf("ShellExecuteW 打开链接失败（返回码 %d）: %s", ret, url)
\t}
\treturn nil
}
'''

# ── 修复 2：cdp/browser.go 的临时 profile 目录 ────────────────────────

CDP_OLD_TEMP = 'profile, err = os.MkdirTemp("", "wildwork-cdp-*")'
CDP_NEW_TEMP = (
    'baseDir, baseTried := tempBaseDir()\n'
    '\t\tprofile, err = os.MkdirTemp(baseDir, "wildwork-cdp-*")'
)

CDP_OLD_ERR = 'return nil, fmt.Errorf("cdp: 创建临时 profile 失败: %w", err)'
CDP_NEW_ERR = (
    'return nil, fmt.Errorf("cdp: 创建临时 profile 失败'
    '（不可写的候选目录: %s）: %w", baseTried, err)'
)

CDP_HELPER = '''
// ── 临时 profile 目录的候选与探针 ───────────────────────────────────────
//
// 为什么不用 os.MkdirTemp("", ...)（等价于 %TEMP%）：某些 Windows 环境下
// %TEMP% 不可写（安全软件保护 / 路径重定向 / 受限令牌 / 企业策略 / 应用被
// 标记为网络来源后受额外限制），CDP 登录会在启动浏览器那一步直接失败，
// 而报错只有一句「Access is denied」，用户完全看不出根因在临时目录。
//
// 返回 (可用的父目录, 探测失败的候选清单)。可用的父目录为空串时，
// 调用方 os.MkdirTemp 会退回系统临时目录（保持原有行为）。
//
// 刻意**不用包级状态**记录候选：多个渠道同时登录会并发调用 Launch，
// 共享变量在无锁情况下是数据竞争，而且错误信息会张冠李戴。
func tempBaseDir() (dir string, tried string) {
\tvar cands []string
\tif exe, err := os.Executable(); err == nil {
\t\t// 程序自己的数据目录：日志与状态都写在这里，正常情况一定可写。
\t\tcands = append(cands, filepath.Join(filepath.Dir(exe), "data", "tmp"))
\t}
\tif cache, err := os.UserCacheDir(); err == nil {
\t\t// exe 放在只读位置（Program Files、网络盘）时的兜底。
\t\t// Windows 上这是 %LOCALAPPDATA%。
\t\tcands = append(cands, filepath.Join(cache, "WildWork", "tmp"))
\t}

\tvar failed []string
\tfor _, c := range cands {
\t\tif dirWritable(c) {
\t\t\treturn c, strings.Join(failed, "; ")
\t\t}
\t\tfailed = append(failed, c)
\t}
\treturn "", strings.Join(failed, "; ")
}

// dirWritable 建目录并真写一个探针文件。
//
// 为什么必须真写：os.MkdirAll 在目录已存在时也返回 nil，
// 光看它成功与否无法证明「这个目录真的可写」。
func dirWritable(dir string) bool {
\tif err := os.MkdirAll(dir, 0o755); err != nil {
\t\treturn false
\t}
\tf, err := os.CreateTemp(dir, ".probe-*")
\tif err != nil {
\t\treturn false
\t}
\tname := f.Name()
\t_ = f.Close()
\t_ = os.Remove(name)
\treturn true
}
'''


# ── 修复 3（诊断）：app.go 暴露路径/权限自检 ──────────────────────────
#
# 为什么需要：ARM64 构建出现过「进程能正常服务 HTTP、能连上游，但 data/
# 一个字节都写不进（日志、CDP 临时 profile 全失败）」，而用 cmd/python 以
# 同一用户写同一路径都成功。外部猜测（ACL、SmartScreen、完整性级别、计划
# 任务令牌）已逐一排除，剩下的只能由程序自己报告它看到的路径与错误码。

APP_OLD_FIELD = "\tlogFile *os.File\n"

APP_NEW_FIELD = (
    "\tlogFile *os.File\n"
    "\n"
    "\t// logDiag 日志初始化的真实结果（含 cwd/exe/路径/错误），经 GET /api/diag 暴露。\n"
    "\t// 动机：ARM64 构建出现「程序能跑但一个字节都写不进 data/」，而同一路径\n"
    "\t// 用 cmd/python 以同一用户写都成功——必须在程序内部取证，不能继续外部猜。\n"
    "\tlogDiag string\n"
)

APP_OLD_LOGINIT = (
    "\t// 日志文件 data/app.log\n"
    "\tlogFP := filepath.Join(filepath.Dir(opts.Config.StateFile), \"app.log\")\n"
    "\tif err := os.MkdirAll(filepath.Dir(logFP), 0o755); err == nil {\n"
    "\t\tf, err := os.OpenFile(logFP, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)\n"
    "\t\tif err == nil {\n"
    "\t\t\ta.logFile = f\n"
    "\t\t}\n"
    "\t}\n"
)

APP_NEW_LOGINIT = (
    "\t// 日志文件 data/app.log（同时记录初始化结果，供 GET /api/diag 取证）\n"
    "\tlogFP := filepath.Join(filepath.Dir(opts.Config.StateFile), \"app.log\")\n"
    "\t{\n"
    "\t\tabs := logFP\n"
    "\t\tif p, err := filepath.Abs(logFP); err == nil {\n"
    "\t\t\tabs = p\n"
    "\t\t}\n"
    "\t\twd, _ := os.Getwd()\n"
    "\t\texePath, _ := os.Executable()\n"
    "\t\tdiag := fmt.Sprintf(\"cwd=%s | exe=%s | stateFile=%s | logFP=%s | abs=%s\",\n"
    "\t\t\twd, exePath, opts.Config.StateFile, logFP, abs)\n"
    "\t\tif err := os.MkdirAll(filepath.Dir(logFP), 0o755); err != nil {\n"
    "\t\t\tdiag += \" | MkdirAll_FAIL: \" + err.Error()\n"
    "\t\t} else if f, err := os.OpenFile(logFP, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); err != nil {\n"
    "\t\t\tdiag += \" | OpenFile_FAIL: \" + err.Error()\n"
    "\t\t} else {\n"
    "\t\t\ta.logFile = f\n"
    "\t\t\tdiag += \" | OpenFile_OK\"\n"
    "\t\t}\n"
    "\t\ta.logDiag = diag\n"
    "\t}\n"
)

APP_OLD_ROUTE = '\tmux.HandleFunc("GET /api/logs", func(w http.ResponseWriter, r *http.Request) {\n'

APP_NEW_ROUTE = (
    "\t// GET /api/diag：运行期路径与写权限自检（只读、无副作用、不含任何凭据）。\n"
    "\t// 用于定位「进程能跑但写不进 data/」这类只能由程序自身回答的问题。\n"
    '\tmux.HandleFunc("GET /api/diag", func(w http.ResponseWriter, r *http.Request) {\n'
    "\t\twd, _ := os.Getwd()\n"
    "\t\texePath, _ := os.Executable()\n"
    "\t\tlogFP := filepath.Join(filepath.Dir(a.cfg.StateFile), \"app.log\")\n"
    "\t\tabs := logFP\n"
    "\t\tif p, err := filepath.Abs(logFP); err == nil {\n"
    "\t\t\tabs = p\n"
    "\t\t}\n"
    "\t\tprobe := func(dir string) string {\n"
    "\t\t\tif dir == \"\" {\n"
    "\t\t\t\treturn \"空路径\"\n"
    "\t\t\t}\n"
    "\t\t\tif err := os.MkdirAll(dir, 0o755); err != nil {\n"
    "\t\t\t\treturn \"MkdirAll失败: \" + err.Error()\n"
    "\t\t\t}\n"
    "\t\t\tf, err := os.CreateTemp(dir, \".diag-*\")\n"
    "\t\t\tif err != nil {\n"
    "\t\t\t\treturn \"写探针失败: \" + err.Error()\n"
    "\t\t\t}\n"
    "\t\t\tname := f.Name()\n"
    "\t\t\t_ = f.Close()\n"
    "\t\t\t_ = os.Remove(name)\n"
    "\t\t\treturn \"可写\"\n"
    "\t\t}\n"
    "\t\tresp := map[string]any{\n"
    "\t\t\t\"cwd\":             wd,\n"
    "\t\t\t\"exe\":             exePath,\n"
    "\t\t\t\"state_file\":      a.cfg.StateFile,\n"
    "\t\t\t\"log_fp\":          logFP,\n"
    "\t\t\t\"log_fp_abs\":      abs,\n"
    "\t\t\t\"log_init\":        a.logDiag,\n"
    "\t\t\t\"log_file_open\":   a.logFile != nil,\n"
    "\t\t\t\"os_temp_dir\":     os.TempDir(),\n"
    "\t\t\t\"env_tmp\":         os.Getenv(\"TMP\"),\n"
    "\t\t\t\"env_temp\":        os.Getenv(\"TEMP\"),\n"
    "\t\t\t\"env_localapp\":    os.Getenv(\"LOCALAPPDATA\"),\n"
    "\t\t\t\"env_userprofile\": os.Getenv(\"USERPROFILE\"),\n"
    "\t\t\t\"probe_exe_dir\":   probe(filepath.Dir(exePath)),\n"
    "\t\t\t\"probe_data_dir\":  probe(filepath.Dir(logFP)),\n"
    "\t\t\t\"probe_temp\":      probe(os.TempDir()),\n"
    "\t\t}\n"
    "\t\t// 令牌信息：三个互不相关的位置同时被拒，说明不是目录权限问题，\n"
    "\t\t// 而是进程本身受限（典型是低完整性级别能读不能写）。\n"
    "\t\tfor k, v := range tokenDiag() {\n"
    "\t\t\tresp[k] = v\n"
    "\t\t}\n"
    "\t\twriteJSON(w, http.StatusOK, resp)\n"
    "\t})\n"
)


# 令牌自检：新建两个文件（Windows 实现 + 其他平台空实现）。
# 单独成文件而不是塞进 app.go：既不污染上游逻辑，也避免在非 Windows 构建里
# 引用 syscall 的 Windows 专属类型。
DIAG_WINDOWS_GO = '''//go:build windows

package app

import (
\t"syscall"
\t"unsafe"
)

// tokenDiag 报告当前进程令牌的关键属性。
//
// 为什么需要：ARM64 构建出现「exe 目录 / data 目录 / %TEMP% 三处写入同时
// Access is denied，而同一用户用 cmd/python 写同一路径都成功」。目录 ACL、
// MOTW、DENY 条目、Smart App Control 均已排除，剩下唯一能解释「能读不能写」
// 的就是进程令牌受限——低完整性级别（Low IL）对 Medium 对象可读不可写。
// 这里把 IL、AppContainer 标志、提升类型、受限令牌标志如实报出来。
func tokenDiag() map[string]any {
\tout := map[string]any{}

\ttok, err := syscall.OpenCurrentProcessToken()
\tif err != nil {
\t\tout["token_error"] = err.Error()
\t\treturn out
\t}
\tdefer func() { _ = tok.Close() }()

\tadvapi := syscall.NewLazyDLL("advapi32.dll")
\tgetInfo := advapi.NewProc("GetTokenInformation")

\t// TokenIntegrityLevel = 25
\tvar need uint32
\t_, _, _ = getInfo.Call(uintptr(tok), 25, 0, 0, uintptr(unsafe.Pointer(&need)))
\tif need == 0 {
\t\tout["integrity"] = "query_size_zero"
\t} else {
\t\tbuf := make([]byte, need)
\t\tr, _, lastErr := getInfo.Call(
\t\t\tuintptr(tok), 25,
\t\t\tuintptr(unsafe.Pointer(&buf[0])), uintptr(need),
\t\t\tuintptr(unsafe.Pointer(&need)),
\t\t)
\t\tif r == 0 {
\t\t\tout["integrity"] = "query_failed: " + lastErr.Error()
\t\t} else {
\t\t\t// TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes } }
\t\t\t// SID 布局：Revision(1) Count(1) Authority(6) SubAuthority[](4*n)
\t\t\t// 完整性级别取最后一个 SubAuthority。
\t\t\tsidPtr := *(*uintptr)(unsafe.Pointer(&buf[0]))
\t\t\tif sidPtr == 0 {
\t\t\t\tout["integrity"] = "nil_sid"
\t\t\t} else {
\t\t\t\tp := unsafe.Pointer(sidPtr)
\t\t\t\tcount := *(*byte)(unsafe.Add(p, 1))
\t\t\t\tif count == 0 {
\t\t\t\t\tout["integrity"] = "zero_subauthority"
\t\t\t\t} else {
\t\t\t\t\trid := *(*uint32)(unsafe.Add(p, 8+int(count-1)*4))
\t\t\t\t\tout["integrity_rid_hex"] = rid
\t\t\t\t\tswitch rid {
\t\t\t\t\tcase 0x0000:
\t\t\t\t\t\tout["integrity"] = "Untrusted"
\t\t\t\t\tcase 0x1000:
\t\t\t\t\t\tout["integrity"] = "Low"
\t\t\t\t\tcase 0x2000:
\t\t\t\t\t\tout["integrity"] = "Medium"
\t\t\t\t\tcase 0x2100:
\t\t\t\t\t\tout["integrity"] = "MediumPlus"
\t\t\t\t\tcase 0x3000:
\t\t\t\t\t\tout["integrity"] = "High"
\t\t\t\t\tcase 0x4000:
\t\t\t\t\t\tout["integrity"] = "System"
\t\t\t\t\tdefault:
\t\t\t\t\t\tout["integrity"] = "other"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}

\t// TokenIsAppContainer = 29
\tvar isAC uint32
\tvar sz uint32 = 4
\tif r, _, _ := getInfo.Call(uintptr(tok), 29, uintptr(unsafe.Pointer(&isAC)), 4, uintptr(unsafe.Pointer(&sz))); r != 0 {
\t\tout["is_app_container"] = isAC != 0
\t}

\t// TokenElevationType = 18  (1=default 2=full 3=limited)
\tvar et uint32
\tsz = 4
\tif r, _, _ := getInfo.Call(uintptr(tok), 18, uintptr(unsafe.Pointer(&et)), 4, uintptr(unsafe.Pointer(&sz))); r != 0 {
\t\tout["elevation_type"] = et
\t}

\t// TokenIsRestricted = 40
\tvar restricted uint32
\tsz = 4
\tif r, _, _ := getInfo.Call(uintptr(tok), 40, uintptr(unsafe.Pointer(&restricted)), 4, uintptr(unsafe.Pointer(&sz))); r != 0 {
\t\tout["is_restricted"] = restricted != 0
\t}

\treturn out
}
'''

DIAG_OTHER_GO = '''//go:build !windows

package app

// tokenDiag 非 Windows 平台没有令牌概念，返回空表保持 /api/diag 结构一致。
func tokenDiag() map[string]any { return map[string]any{} }
'''


def patch_platform(root: pathlib.Path) -> None:
    p = root / "internal" / "platform" / "platform_windows.go"
    if not p.is_file():
        sys.exit(f"FAIL: 文件不存在 {p}")
    text = p.read_text(encoding="utf-8")

    if "procShellExecuteW" in text:
        print("  [跳过] platform_windows.go 已是 ShellExecuteW 形态")
        return

    if PLATFORM_OLD_IMPORT not in text:
        sys.exit(
            "FAIL: platform_windows.go 的 import 块锚点未找到。\n"
            "上游可能改动了 imports，请更新 patches/apply-windows-fixes.py。"
        )
    text = text.replace(PLATFORM_OLD_IMPORT, PLATFORM_NEW_IMPORT, 1)

    if PLATFORM_OLD_OPENURL not in text:
        sys.exit(
            "FAIL: platform_windows.go 的 OpenURL 函数锚点未找到。\n"
            "上游可能重写了 OpenURL，请更新 patches/apply-windows-fixes.py。"
        )
    text = text.replace(PLATFORM_OLD_OPENURL, PLATFORM_NEW_OPENURL, 1)

    p.write_text(text, encoding="utf-8")
    print("  [已改] platform_windows.go: OpenURL → ShellExecuteW")


def patch_cdp(root: pathlib.Path) -> None:
    p = root / "internal" / "cdp" / "browser.go"
    if not p.is_file():
        sys.exit(f"FAIL: 文件不存在 {p}")
    text = p.read_text(encoding="utf-8")

    if "tempBaseDir" in text:
        print("  [跳过] cdp/browser.go 已是多候选 tempBaseDir 形态")
        return

    if CDP_OLD_TEMP not in text:
        sys.exit(
            "FAIL: cdp/browser.go 的 MkdirTemp 锚点未找到。\n"
            "上游可能改动了临时 profile 的创建方式，请更新 patches/apply-windows-fixes.py。"
        )
    text = text.replace(CDP_OLD_TEMP, CDP_NEW_TEMP, 1)

    if CDP_OLD_ERR not in text:
        sys.exit(
            "FAIL: cdp/browser.go 的错误信息锚点未找到。\n"
            "请更新 patches/apply-windows-fixes.py。"
        )
    text = text.replace(CDP_OLD_ERR, CDP_NEW_ERR, 1)

    # 依赖 strings（tempBaseDir 用它拼接候选清单）。
    if '"strings"' not in text:
        sys.exit("FAIL: cdp/browser.go 缺少 strings import，tempBaseDir 无法拼接候选。")

    text = text.rstrip("\n") + "\n" + CDP_HELPER

    p.write_text(text, encoding="utf-8")
    print("  [已改] cdp/browser.go: 临时 profile → 多候选 tempBaseDir（带失败路径自证）")


def patch_diag(root: pathlib.Path) -> None:
    p = root / "internal" / "app" / "app.go"
    if not p.is_file():
        sys.exit(f"FAIL: 文件不存在 {p}")

    # 0) 令牌自检文件先落盘（不依赖 app.go 的状态，保证幂等时也补齐）
    pwin = root / "internal" / "app" / "diag_token_windows.go"
    pother = root / "internal" / "app" / "diag_token_other.go"
    if not pwin.exists():
        pwin.write_text(DIAG_WINDOWS_GO, encoding="utf-8")
        print("  [已改] app/diag_token_windows.go: 新增令牌自检（IL/AppContainer/受限）")
    if not pother.exists():
        pother.write_text(DIAG_OTHER_GO, encoding="utf-8")
        print("  [已改] app/diag_token_other.go: 非 Windows 空实现")

    text = p.read_text(encoding="utf-8")

    if "logDiag" in text:
        print("  [跳过] app.go 已含 logDiag 诊断")
        return

    if '"fmt"' not in text:
        sys.exit("FAIL: app.go 缺少 fmt import，诊断补丁无法拼接字符串。")

    # 1) 结构体加字段
    if APP_OLD_FIELD not in text:
        sys.exit(
            "FAIL: app.go 的 logFile 字段锚点未找到。\n"
            "请更新 patches/apply-windows-fixes.py 的 APP_OLD_FIELD。"
        )
    text = text.replace(APP_OLD_FIELD, APP_NEW_FIELD, 1)

    # 2) 日志初始化块改成带诊断
    if APP_OLD_LOGINIT not in text:
        sys.exit(
            "FAIL: app.go 的日志初始化锚点未找到。\n"
            "请更新 patches/apply-windows-fixes.py 的 APP_OLD_LOGINIT。"
        )
    text = text.replace(APP_OLD_LOGINIT, APP_NEW_LOGINIT, 1)

    # 3) 新增 /api/diag 端点（挂在 /api/logs 之前）
    if APP_OLD_ROUTE not in text:
        sys.exit(
            "FAIL: app.go 的 /api/logs 路由锚点未找到。\n"
            "请更新 patches/apply-windows-fixes.py 的 APP_OLD_ROUTE。"
        )
    text = text.replace(APP_OLD_ROUTE, APP_NEW_ROUTE + APP_OLD_ROUTE, 1)

    p.write_text(text, encoding="utf-8")
    print("  [已改] app.go: 日志初始化带诊断 + 新增 GET /api/diag")


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("用法: apply-windows-fixes.py <源码根目录>")
    root = pathlib.Path(sys.argv[1]).resolve()
    if not (root / "go.mod").is_file():
        sys.exit(f"FAIL: {root} 不像 Go 源码根目录（缺 go.mod）")

    print(f"对 {root} 应用 Windows 修复补丁：")
    patch_platform(root)
    patch_cdp(root)
    patch_diag(root)
    print("补丁应用完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
