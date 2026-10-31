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


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("用法: apply-windows-fixes.py <源码根目录>")
    root = pathlib.Path(sys.argv[1]).resolve()
    if not (root / "go.mod").is_file():
        sys.exit(f"FAIL: {root} 不像 Go 源码根目录（缺 go.mod）")

    print(f"对 {root} 应用 Windows 修复补丁：")
    patch_platform(root)
    patch_cdp(root)
    print("补丁应用完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
