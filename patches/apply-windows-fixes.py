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
     CDP 临时 profile 从 os.MkdirTemp("", ...)（= %TEMP%）改为
     程序目录下的 data/tmp，落不下去才退回系统临时目录。

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
CDP_NEW_TEMP = 'profile, err = os.MkdirTemp(tempBaseDir(), "wildwork-cdp-*")'

CDP_HELPER = '''
// tempBaseDir 返回存放临时浏览器 profile 的父目录：优先程序目录下的
// data/tmp，落不下去才退回系统临时目录（返回空串 = 走系统默认）。
//
// 为什么不用 os.MkdirTemp("", ...)（等价于 %TEMP%）：某些 Windows 环境下
// %TEMP% 不可写（安全软件保护 / 路径重定向 / 受限令牌 / 企业策略），CDP
// 登录会在启动浏览器那一步直接失败，而报错只有一句「拒绝访问」，用户完全
// 看不出根因在临时目录。程序自己的 data 目录一定可写（日志和状态都写在那里）。
//
// 探针为什么真写一个文件：os.MkdirAll 在目录已存在时也返回 nil，
// 光看它成功与否无法证明「这个目录真的可写」。
func tempBaseDir() string {
\texe, err := os.Executable()
\tif err != nil {
\t\treturn ""
\t}
\tbase := filepath.Join(filepath.Dir(exe), "data", "tmp")
\tif err := os.MkdirAll(base, 0o755); err != nil {
\t\treturn ""
\t}
\tf, err := os.CreateTemp(base, ".probe-*")
\tif err != nil {
\t\treturn ""
\t}
\tname := f.Name()
\t_ = f.Close()
\t_ = os.Remove(name)
\treturn base
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

    if "tempBaseDir()" in text:
        print("  [跳过] cdp/browser.go 已改用 tempBaseDir")
        return

    if CDP_OLD_TEMP not in text:
        sys.exit(
            "FAIL: cdp/browser.go 的 MkdirTemp 锚点未找到。\n"
            "上游可能改动了临时 profile 的创建方式，请更新 patches/apply-windows-fixes.py。"
        )
    text = text.replace(CDP_OLD_TEMP, CDP_NEW_TEMP, 1)
    text = text.rstrip("\n") + "\n" + CDP_HELPER

    p.write_text(text, encoding="utf-8")
    print("  [已改] cdp/browser.go: 临时 profile 目录 → 程序目录 data/tmp")


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
