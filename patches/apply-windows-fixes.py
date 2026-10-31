#!/usr/bin/env python3
"""对上游源码打 Windows 运行期补丁（幂等、锚点匹配、失败即报错）。

═══ 现在只保留一项补丁，理由如下 ═══

历史背景：ARM64 版本曾出现「程序能跑，但日志、%TEMP%、CDP 临时 profile
全部 Access is denied」。当时据此加了两个补丁（CDP 临时目录多候选、进程
令牌自检 /api/diag）。后来查明真凶是**文件完整性标签**：

    交付的 exe 放在 Low 完整性目录里，复制出来时标签随文件走，
    从它启动的进程是 Low IL，对 Medium 对象一律可读不可写。

清掉标签（icacls ... /setintegritylevel Medium）后一切正常，
`integrity: "Medium"` / `log_file_open: true` / 三个写探针全部通过。

因此那两项补丁是在错误方向上加的，**已全部移除**：
  - GET /api/diag + logDiag 字段 + diag_token_*.go（诊断专用，不该留在发布版）
  - cdp/browser.go 的 tempBaseDir 多候选（%TEMP% 本来就可用，回归上游）

═══ 唯一保留：OpenURL ═══

上游用 `rundll32 url.dll,FileProtocolHandler`，有两个真实缺陷：

  1. 它先在 %TEMP% 落一个临时文件再转交外壳。%TEMP% 一旦不可写
     （安全软件、路径重定向、受限令牌、Low IL 等），链接就打不开；
  2. `cmd.Start()` 只保证 rundll32 进程起来了，**不检查它是否真的打开成功**。
     失败时面板表现为「点了没反应」，没有任何错误提示。

ShellExecuteW 直接把 URL 交给外壳解析，不产生临时文件，且返回值 >32 才
算成功——失败能如实报错。改动 5 行、无副作用，因此保留。

用法：
    python3 patches/apply-windows-fixes.py <源码根目录>
"""
from __future__ import annotations

import pathlib
import sys

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
// （安全软件保护、路径被重定向、受限令牌、低完整性级别），OpenURL 就会失败，
// 表现为面板里所有「打开链接」的入口一起失效。
// 更隐蔽的是 `cmd.Start()` 只保证 rundll32 进程起来了，不检查它是否真的
// 打开成功——失败时用户看到的是「点了没反应」，而不是一条错误。
// ShellExecuteW 直接把 URL 交给 shell 解析，不产生任何临时文件，也不依赖
// PATH 里能找到 rundll32；返回值 > 32 才判定成功。
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
\t// ShellExecuteW 约定：返回值 > 32 才是成功，<= 32 是错误码。
\tif ret <= 32 {
\t\treturn fmt.Errorf("ShellExecuteW 打开链接失败（返回码 %d）: %s", ret, url)
\t}
\treturn nil
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


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("用法: apply-windows-fixes.py <源码根目录>")
    root = pathlib.Path(sys.argv[1]).resolve()
    if not (root / "go.mod").is_file():
        sys.exit(f"FAIL: {root} 不像 Go 源码根目录（缺 go.mod）")

    print(f"对 {root} 应用 Windows 修复补丁：")
    patch_platform(root)
    print("补丁应用完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
