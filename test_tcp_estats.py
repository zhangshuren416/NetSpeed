# -*- coding: utf-8 -*-
"""
TCP ESTATS 可用性诊断（独立脚本，纯标准库，无第三方依赖）
==========================================================
用途：验证当前 Windows 的 GetPerTcpConnectionEStats 能否正确返回
      每连接字节计数（这是"免管理员按进程统计网速"的唯一数据源）。

用法：python test_tcp_estats.py
      （建议同时在管理员和非管理员下各跑一次对比）

原理：
  测试1 在回环连接上精确传输 8MB，再读 estats 的 DataBytesIn，
        与真实值对比。这是受控实验，不依赖外网。
  测试2 真实远程下载几 MB 做对照（依赖网络，best-effort）。

判定：
  计数 ≈ 真实传输量      -> PASS（接口正常）
  计数为巨大指针/垃圾值  -> FAIL（接口异常，疑似系统 bug）
"""
import ctypes
import platform
import socket
import struct
import sys
import threading
import time

iphlpapi = ctypes.windll.iphlpapi
MAX_PLAUSIBLE = 1 << 48   # 超过 256TB 的单连接计数视为垃圾值


def os_info():
    v = sys.getwindowsversion()
    print(f"系统     : Windows {v.major}.{v.minor} build {v.build}")
    print(f"平台     : {platform.version()}")
    print(f"管理员   : {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    print(f"Python   : {sys.version.split()[0]}")
    print("-" * 64)


def read_estats(row_bytes, is6=False):
    """返回 (enable_ret, read_ret, bytesOut, bytesIn)。"""
    fn = (iphlpapi.GetPerTcp6ConnectionEStats if is6
          else iphlpapi.GetPerTcpConnectionEStats)
    row = (ctypes.c_ubyte * len(row_bytes)).from_buffer_copy(row_bytes)
    rw = ctypes.c_ubyte(1)
    en = fn(row, 1, ctypes.byref(rw), 0, 1, None, 0, 0, None, 0, 0)
    rod = (ctypes.c_ubyte * 96)()
    ret = fn(row, 1, None, 0, 0, None, 0, 0, rod, 0, 96)
    if ret != 0:
        return en, ret, None, None
    out = struct.unpack_from("<Q", rod, 0)[0]
    inn = struct.unpack_from("<Q", rod, 16)[0]
    return en, ret, out, inn


def judge(tag, actual, expected):
    if actual is None:
        print(f"  [{tag}] FAIL：读取失败（接口返回错误）")
        return False
    if actual > MAX_PLAUSIBLE:
        print(f"  [{tag}] FAIL：bytesIn={actual}（巨大垃圾值/指针，非真实计数）")
        return False
    if expected is not None and abs(actual - expected) > expected * 0.05 + 4096:
        print(f"  [{tag}] FAIL：bytesIn={actual}，期望≈{expected}（计数不符）")
        return False
    print(f"  [{tag}] PASS：bytesIn={actual}（期望≈{expected}）")
    return True


def test_loopback():
    print("测试1：回环受控传输 8MB")
    N = 8 * 1024 * 1024
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def server():
        c, _ = srv.accept()
        chunk = b"x" * 65536
        for _ in range(N // 65536):
            c.sendall(chunk)
        time.sleep(10)
        c.close()

    threading.Thread(target=server, daemon=True).start()
    cli = socket.create_connection(("127.0.0.1", port))
    got = 0
    while got < N:
        b = cli.recv(65536)
        if not b:
            break
        got += len(b)

    la = struct.unpack("<I", socket.inet_aton("127.0.0.1"))[0]
    lp = cli.getsockname()[1]
    lpn = struct.unpack("<I", struct.pack(">H", lp) + b"\x00\x00")[0]
    rpn = struct.unpack("<I", struct.pack(">H", port) + b"\x00\x00")[0]
    row = struct.pack("<5I", 5, la, lpn, la, rpn)

    en, ret, out, inn = read_estats(row)
    print(f"  实际传输 {got} 字节；enable={en} read={ret} bytesOut={out}")
    ok = judge("回环", inn, N)
    cli.close()
    srv.close()
    return ok


def test_remote():
    print("测试2：真实远程下载对照（依赖网络）")
    import os
    import urllib.request
    urls = [
        "https://cdn.npmmirror.com/binaries/python/3.12.10/python-3.12.10-amd64.exe",
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe",
    ]
    MY = os.getpid()
    stop = False
    total = [0]

    def dl():
        for url in urls:
            if stop:
                return
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    while not stop and total[0] < 8 * 1024 * 1024:
                        b = r.read(65536)
                        if not b:
                            break
                        total[0] += len(b)
                return
            except Exception:
                continue

    t = threading.Thread(target=dl, daemon=True)
    t.start()
    time.sleep(6)

    size = ctypes.c_ulong(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), 0, 2, 5, 0)
    if size.value == 0:
        print("  [远程] SKIP：无法枚举 TCP 表")
        stop = True
        return None
    buf = (ctypes.c_ubyte * size.value)()
    iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), 0, 2, 5, 0)
    raw = ctypes.string_at(buf, size.value)
    n = struct.unpack_from("<I", raw, 0)[0]
    result = None
    for i in range(n):
        off = 4 + i * 24
        if off + 24 > len(raw):
            break
        state, la, lp, ra, rp, pid = struct.unpack_from("<6I", raw, off)
        if pid != MY or state != 5:
            continue
        if (la & 0xFF) == 127 or (ra & 0xFF) == 127:
            continue
        en, ret, out, inn = read_estats(raw[off:off + 20])
        print(f"  已下载 {total[0]} 字节；enable={en} read={ret} bytesOut={out}")
        result = judge("远程", inn, total[0] if total[0] else None)
        break
    if result is None:
        print("  [远程] SKIP：未找到本进程的远程连接")
    stop = True
    t.join(timeout=3)
    return result


def main():
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os_info()
    lo = test_loopback()
    print()
    re = test_remote()
    print()
    print("=" * 64)
    if lo or re:
        print("结论：PASS —— 本机 TCP ESTATS 接口正常，免管理员进程网速可用。")
    elif re is None and not lo:
        print("结论：FAIL —— 回环计数异常（远程对照未能执行）。")
        print("      疑似系统 bug；可在管理员下再跑一次确认与权限无关。")
    else:
        print("结论：FAIL —— TCP ESTATS 返回异常计数，疑似系统 bug。")
        print("      已知见于部分 Windows 11 24H2 及更新预览版；管理员亦无法修复。")
    print("=" * 64)


if __name__ == "__main__":
    main()