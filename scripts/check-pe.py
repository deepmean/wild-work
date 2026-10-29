#!/usr/bin/env python3
"""校验 PE 产物的真实架构（不是靠文件名猜）。

为什么需要这个脚本：Go 交叉编译时文件名是**人写的**，写错架构名不会报错，
只会产出一个能跑却架构不对的 exe（Windows ARM64 上会被 x64 模拟层接走，
表现为"能跑但慢一倍"，这种错误在 CI 里完全静默）。所以构建流水线必须读
PE 头里的 Machine 字段做断言。

用法：
    python3 scripts/check-pe.py dist/wild-work-windows-arm64.exe --expect arm64
    python3 scripts/check-pe.py dist/wild-work-windows-amd64.exe --expect amd64

退出码：0 = 架构与期望一致；1 = 不一致或文件不是合法 PE。
"""
import os
import struct
import sys

MACHINE = {
    0x014C: "x86",
    0x8664: "amd64",
    0xAA64: "arm64",
    0x01C4: "arm",
}

PE32_PLUS = 0x20B
PE32 = 0x10B

# 产物下限：这个二进制内嵌了全部前端静态资源（app.js/style.css/index.html
# 合计 ~400KB）以及 Go 运行时，正常体积 17~18MB。设 8MB 是为了抓"构建产物
# 其实是空壳/被 trim 坏"这一类事故，而不是精确比对体积。
MIN_BYTES = 8 * 1024 * 1024


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        fail("用法: check-pe.py <exe> [--expect x86|amd64|arm64|arm] [--min-bytes N]")

    path = args[0]
    expect = None
    min_bytes = MIN_BYTES
    i = 1
    while i < len(args):
        if args[i] == "--expect" and i + 1 < len(args):
            expect = args[i + 1].lower()
            i += 2
        elif args[i] == "--min-bytes" and i + 1 < len(args):
            min_bytes = int(args[i + 1], 0)
            i += 2
        else:
            fail(f"无法识别的参数: {args[i]}")

    if not os.path.isfile(path):
        fail(f"文件不存在: {path}")

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(0x40)
        if len(head) < 0x40 or head[:2] != b"MZ":
            fail(f"{path} 不是 Windows 可执行文件（缺 MZ 头）")

        lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        if lfanew <= 0 or lfanew > size - 24:
            fail(f"{path} 的 e_lfanew=0x{lfanew:X} 越界，PE 头已损坏")

        f.seek(lfanew)
        pe = f.read(26)
        if len(pe) < 26 or pe[:4] != b"PE\0\0":
            fail(f"{path} 缺 PE\\0\\0 签名")

        machine = struct.unpack_from("<H", pe, 4)[0]
        opt_magic = struct.unpack_from("<H", pe, 24)[0]

    arch = MACHINE.get(machine, f"0x{machine:04X}")
    print(f"文件      : {path}")
    print(f"体积      : {size:,} bytes")
    print(f"PE Machine: 0x{machine:04X} ({arch})")
    print(f"可选头     : 0x{opt_magic:04X} ({'PE32+' if opt_magic == PE32_PLUS else 'PE32' if opt_magic == PE32 else '未知'})")

    if opt_magic not in (PE32_PLUS, PE32):
        fail("可选头 magic 不是合法的 PE32/PE32+")

    if size < min_bytes:
        fail(f"体积 {size:,} 小于下限 {min_bytes:,}，疑似构建不完整")

    if expect is not None:
        want = MACHINE.get(expect) if expect in MACHINE else None
        if want is None:
            fail(f"--expect 取值非法: {expect}")
        if arch != expect:
            fail(f"架构不符: 期望 {expect}，实际 {arch}")
        print(f"断言通过  : 产物确实是 {expect}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
