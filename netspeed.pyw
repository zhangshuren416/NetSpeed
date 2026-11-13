# -*- coding: utf-8 -*-
"""
网速悬浮窗 (NetSpeed Monitor)
=============================
Windows 桌面悬浮窗，实时显示当前网络的下载 / 上传速度。

特性：
  * 纯 Python 标准库实现（tkinter + ctypes + sqlite3），无需第三方依赖
  * 通过 iphlpapi.GetIfTable2 读取网卡 64 位字节计数器，差分得到实时速度
  * 用 GetAdaptersAddresses 过滤真实网卡，排除 WFP/QoS 过滤行，避免重复计数
  * 无边框置顶小窗，可拖动、记忆位置，Win11 自动圆角，高 DPI 适配
  * 靠边自动隐藏：拖到屏幕边缘自动吸附，鼠标移开滑出屏幕只留细边，
    鼠标放到细边上自动展开（右键菜单可开关）。细边带浅色边框，
    并以速度条显示实时网速：左右停靠时上上传（蓝）/下下载（绿），
    上下停靠时左上传/右下载
  * 双击悬浮窗或右键菜单打开「网络详情与流量统计」窗口，两个标签页：
      - 网卡与流量：每张网卡的实时上传 / 下载速度；按 本次运行 /
        最近1小时 / 最近12小时 / 最近1天 / 最近7天 / 全部 统计每张
        网卡与总流量（SQLite 分钟粒度持久化，重启不丢）
      - 进程网速：每个进程的实时上传 / 下载速度（TCP 连接按 PID 归属，
        GetPerTcpConnectionEStats 差分，无需管理员权限；UDP/QUIC 不计入）
  * 右键菜单：刷新间隔 / 透明度 / 靠边自动隐藏 / 数据保留期 / 开机自启 /
    重置位置 / 退出
  * 单实例运行

用法：pythonw.exe netspeed.pyw   （或直接双击打包好的 NetSpeed.exe）
"""

import ctypes
import json
import math
import os
import sqlite3
import struct as _struct
import sys
import time
import tkinter as tk
from tkinter import font as tkfont

APP_NAME = "NetSpeedMonitor"
MUTEX_NAME = "NetSpeedMonitor_SingleInstance_Mutex"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DB_PATH = os.path.join(CONFIG_DIR, "traffic.db")

# ---------------------------------------------------------------- 外观常量
BG_COLOR = "#16181d"      # 窗口背景
CARD_BG = "#1f232b"       # 卡片/行背景
CARD_HOVER = "#262b35"    # 悬停
BORDER_COLOR = "#2c313a"  # 分隔/描边
TEXT_COLOR = "#e8eaed"    # 主文字
NUM_COLOR = "#f2f4f8"     # 数字
DIM_COLOR = "#9aa0a6"     # 次要文字
FAINT_COLOR = "#6b7280"   # 弱化文字
DOWN_COLOR = "#7ee787"    # 下载（绿）
UP_COLOR = "#79c0ff"      # 上传（蓝）
SEG_ACTIVE = "#3a4150"    # 分段控件选中
MENU_BG = "#23262e"
MENU_ACTIVE = "#3a4150"
FONT_FAMILY = "Segoe UI"
FONT_SIZE = 11
PAD_X = 14
PAD_Y = 8
UI_FAMILY = "Microsoft YaHei UI"
UI_FONT = (UI_FAMILY, 9)


def dpi_scale():
    try:
        return max(1.0, user32.GetDpiForSystem() / 96.0)
    except (OSError, AttributeError):
        return 1.0

DEFAULT_CONFIG = {
    "x": None,            # None 表示默认放在屏幕右上角
    "y": None,
    "interval": 1.0,      # 刷新间隔（秒）
    "alpha": 0.92,        # 不透明度
    "stats_range": "1h",  # 详情窗口默认统计粒度
    "detail_tab": "nic",  # 详情窗口当前标签页：nic=网卡与流量 proc=进程网速
    "auto_hide": True,    # 靠边自动隐藏
    "retention_days": 30,  # 流量数据保留天数
}

# 靠边自动隐藏参数（距离为逻辑像素，按 DPI 缩放）
DOCK_SNAP_DIST = 20       # 松手时距屏幕边缘多近触发吸附
DOCK_PEEK = 10            # 隐藏后留在屏幕内的细边宽度（含边框与速度条）
DOCK_SLIDE_MS = 160       # 滑入/滑出动画时长（毫秒）
DOCK_HIDE_DELAY = 0.3     # 鼠标离开后延迟多久才隐藏（秒）
DOCK_POLL_MS = 80         # 鼠标位置巡检周期（毫秒）
STRIP_BORDER = "#a8b0bf"  # 隐藏细边的浅色边框

INTERVAL_CHOICES = (0.5, 1.0, 2.0)
ALPHA_CHOICES = (1.0, 0.92, 0.8, 0.6)

# 统计粒度：key -> (显示名, 分钟数)；session/all 特殊处理
STATS_RANGES = (
    ("session", "本次运行"),
    ("1h", "最近1小时"),
    ("12h", "最近12小时"),
    ("1d", "最近1天"),
    ("7d", "最近7天"),
    ("all", "全部"),
)
RANGE_MINUTES = {"1h": 60, "12h": 720, "1d": 1440, "7d": 10080}
RETENTION_CHOICES = (7, 30, 90, 365)  # 数据保留期可选天数
DEFAULT_RETENTION_DAYS = 30           # 默认保留期（天）
CLEANUP_INTERVAL = 3600               # 运行期清理检查间隔（秒）

# ---------------------------------------------------------------- Win32 常量
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
iphlpapi = ctypes.windll.iphlpapi

# ---------------------------------------------------------------- 网卡计数
# MIB_IF_ROW2 实测行布局（Win10/11 x64，行步长 1352 字节），
# 关键偏移已用 Get-NetAdapterStatistics 交叉验证：
#   InterfaceIndex +8 / Alias +28 / Type +1128 / OperStatus +1156 /
#   InOctets +1208 / OutOctets +1280
ROW_SIZE = 1352
OFF_INDEX = 8
OFF_ALIAS = 28
OFF_TYPE = 1128
OFF_OPER = 1156
OFF_IN_OCTETS = 1208
OFF_OUT_OCTETS = 1280

IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
IF_OPER_STATUS_UP = 1


class MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [
        ("NumEntries", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("Table", ctypes.c_ubyte * 1),
    ]


def _enum_adapters():
    """GetAdaptersAddresses -> {IfIndex: 友好名称}。

    只保留已连接(Up)且非回环/非隧道的真实网卡。该 API 不返回
    WFP/QoS 等轻量过滤接口行（GetIfTable2 会返回，且过滤行镜像
    主网卡计数器，直接求和会重复计数），因此用它做白名单。
    失败时返回 None。
    """
    size = ctypes.c_ulong(0)
    gaa_flags = 0x000F  # SKIP_UNICAST|ANYCAST|MULTICAST|DNS_SERVER
    ret = iphlpapi.GetAdaptersAddresses(0, gaa_flags, None, None, ctypes.byref(size))
    if ret != 111 or size.value == 0:  # ERROR_BUFFER_OVERFLOW
        return None
    buf = (ctypes.c_ubyte * size.value)()
    if iphlpapi.GetAdaptersAddresses(0, gaa_flags, None, buf, ctypes.byref(size)) != 0:
        return None
    ptr_fmt = "<Q" if ctypes.sizeof(ctypes.c_void_p) == 8 else "<I"
    adapters = {}
    addr = ctypes.addressof(buf)
    while addr:
        node = ctypes.string_at(addr, 112)
        length, ifindex = _struct.unpack_from("<II", node, 0)
        next_ptr = _struct.unpack_from(ptr_fmt, node, 8)[0]
        iftype, oper = _struct.unpack_from("<II", node, 100)
        if oper == IF_OPER_STATUS_UP and iftype not in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
            name_ptr = _struct.unpack_from(ptr_fmt, node, 72)[0]  # FriendlyName
            name = ""
            if name_ptr:
                try:
                    name = ctypes.wstring_at(name_ptr, 256).split("\x00", 1)[0]
                except OSError:
                    name = ""
            adapters[ifindex] = name or f"接口 {ifindex}"
        if length == 0:
            break
        addr = next_ptr
    return adapters


def read_adapters():
    """返回 [(IfIndex, 名称, 接收字节, 发送字节), ...]，只含已连接的真实网卡。

    读取失败时返回 None。
    """
    active = _enum_adapters()
    table_ptr = ctypes.POINTER(MIB_IF_TABLE2)()
    if iphlpapi.GetIfTable2(ctypes.byref(table_ptr)) != 0:
        return None
    try:
        table = table_ptr.contents
        n = table.NumEntries
        if n == 0:
            return []
        base = ctypes.addressof(table) + MIB_IF_TABLE2.Table.offset
        raw = ctypes.string_at(base, n * ROW_SIZE)
        result = []
        for i in range(n):
            off = i * ROW_SIZE
            ifindex = _struct.unpack_from("<I", raw, off + OFF_INDEX)[0]
            if active is not None:
                if ifindex not in active:
                    continue
                name = active[ifindex]
            else:
                # 兜底：GetAdaptersAddresses 失败时按状态/类型/别名过滤
                oper = _struct.unpack_from("<I", raw, off + OFF_OPER)[0]
                iftype = _struct.unpack_from("<I", raw, off + OFF_TYPE)[0]
                if oper != IF_OPER_STATUS_UP:
                    continue
                if iftype in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
                    continue
                alias = raw[off + OFF_ALIAS:off + OFF_ALIAS + 514]
                name = alias.decode("utf-16-le", "replace").split("\x00", 1)[0]
                if name.endswith("-0000"):  # 过滤驱动行
                    continue
            rx = _struct.unpack_from("<Q", raw, off + OFF_IN_OCTETS)[0]
            tx = _struct.unpack_from("<Q", raw, off + OFF_OUT_OCTETS)[0]
            result.append((ifindex, name, rx, tx))
        return result
    finally:
        iphlpapi.FreeMibTable(table_ptr)


def format_speed(bps):
    """把 字节/秒 格式化为 (定宽数字串, 单位)。"""
    if bps < 0:
        bps = 0.0
    if bps < 1024:
        return f"{bps:6.0f}", "B/s "
    for unit in ("KB/s", "MB/s", "GB/s", "TB/s"):
        bps /= 1024.0
        if bps < 1024 or unit == "TB/s":
            if bps >= 100:
                num = f"{bps:6.0f}"
            elif bps >= 10:
                num = f"{bps:6.1f}"
            else:
                num = f"{bps:6.2f}"
            return num, unit


def format_bytes(n):
    """把字节数格式化为易读字符串，如 1.23 GB。"""
    n = float(max(0, n))
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            if n >= 100:
                return f"{n:.0f} {unit}"
            if n >= 10:
                return f"{n:.1f} {unit}"
            return f"{n:.2f} {unit}"


def speed_text(bps):
    num, unit = format_speed(bps)
    return f"{num.strip()} {unit.strip()}"


def speed_fraction(bps):
    """速度 -> 0..1 的细边速度条填充比例。

    对数刻度：1 KB/s 约 1/3，1 MB/s 约 2/3，1 GB/s 满格；
    有流量时至少留一点填充，方便察觉微小活动。
    """
    if bps <= 0:
        return 0.0
    frac = math.log10(1.0 + bps) / 9.0
    return max(0.06, min(1.0, frac))


# ---------------------------------------------------------------- 配置
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in cfg:
            if k in data:
                cfg[k] = data[k]
    except (OSError, ValueError):
        pass
    if cfg["interval"] not in INTERVAL_CHOICES:
        cfg["interval"] = 1.0
    if cfg["alpha"] not in ALPHA_CHOICES:
        cfg["alpha"] = 0.92
    if cfg["stats_range"] not in dict(STATS_RANGES):
        cfg["stats_range"] = "1h"
    if cfg["detail_tab"] not in ("nic", "proc"):
        cfg["detail_tab"] = "nic"
    if not isinstance(cfg["auto_hide"], bool):
        cfg["auto_hide"] = True
    if cfg["retention_days"] not in RETENTION_CHOICES:
        cfg["retention_days"] = DEFAULT_RETENTION_DAYS
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- 开机自启
def _run_key():
    import winreg
    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    )


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    script = os.path.abspath(__file__)
    return f'"{pythonw}" "{script}"'


def is_autostart_enabled():
    import winreg
    try:
        with _run_key() as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    import winreg
    try:
        with _run_key() as key:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


# ---------------------------------------------------------------- 流量记录
class TrafficRecorder:
    """按分钟桶累计每张网卡的流量，SQLite 持久化。

    * add_deltas 每个刷新周期喂入差分字节数；跨分钟时落盘。
    * 统计只累计程序运行期间（关闭期间无法采样），建议开启开机自启。
    * 保留期可配置（7/30/90/365 天，默认 30）：启动时清理过期数据
      并收缩文件，运行期定时清理，切换保留期立即生效。
    """

    def __init__(self, path, retention_days=DEFAULT_RETENTION_DAYS):
        self.retention_days = retention_days
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS traffic("
            "minute INTEGER NOT NULL, ifindex INTEGER NOT NULL, "
            "name TEXT, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(minute, ifindex))")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS adapters("
            "ifindex INTEGER PRIMARY KEY, name TEXT)")
        self.conn.commit()
        self._minute = None
        self._pending = {}          # ifindex -> [name, rx, tx]（当前分钟未落盘）
        self.session = {}           # ifindex -> [name, rx, tx]（本次运行累计）
        self._last_cleanup = time.time()
        self._prune(vacuum=True)    # 启动时清理过期数据并收缩文件

    def set_retention(self, days):
        """切换保留期：立即按新期限清理（缩短时顺带收缩文件）。"""
        self.retention_days = days
        self._last_cleanup = time.time()
        self._prune(vacuum=True)

    def _prune(self, vacuum=False):
        """删除超过保留期的流量数据；vacuum=True 时收缩数据库文件。"""
        cutoff = int(time.time() // 60) - self.retention_days * 1440
        try:
            cur = self.conn.execute(
                "DELETE FROM traffic WHERE minute < ?", (cutoff,))
            self.conn.commit()
            if vacuum and cur.rowcount > 0:
                self.conn.execute("VACUUM")
        except sqlite3.Error:
            pass

    def _maybe_cleanup(self):
        """运行期定时清理（长期开机自启运行时也能保持 7 天上限）。"""
        now = time.time()
        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            self._last_cleanup = now
            self._prune()

    def add_deltas(self, now_ts, deltas):
        """deltas: [(ifindex, name, drx, dtx), ...]"""
        minute = int(now_ts // 60)
        if self._minute is not None and minute != self._minute:
            self.flush()
        self._minute = minute
        for ifindex, name, drx, dtx in deltas:
            drx = max(0, drx)
            dtx = max(0, dtx)
            p = self._pending.setdefault(ifindex, [name, 0, 0])
            p[0] = name
            p[1] += drx
            p[2] += dtx
            s = self.session.setdefault(ifindex, [name, 0, 0])
            s[0] = name
            s[1] += drx
            s[2] += dtx

    def flush(self):
        self._maybe_cleanup()
        if not self._pending or self._minute is None:
            return
        rows = [(self._minute, i, p[0], p[1], p[2])
                for i, p in self._pending.items()]
        names = [(i, p[0]) for i, p in self._pending.items()]
        try:
            self.conn.executemany(
                "INSERT INTO traffic(minute, ifindex, name, rx, tx) VALUES(?,?,?,?,?) "
                "ON CONFLICT(minute, ifindex) DO UPDATE SET "
                "rx = rx + excluded.rx, tx = tx + excluded.tx, name = excluded.name",
                rows)
            self.conn.executemany(
                "INSERT INTO adapters(ifindex, name) VALUES(?,?) "
                "ON CONFLICT(ifindex) DO UPDATE SET name = excluded.name",
                names)
            self.conn.commit()
        except sqlite3.Error:
            pass
        self._pending = {}

    def query(self, range_key):
        """返回 [(ifindex, name, rx, tx), ...]，按总流量降序。"""
        if range_key == "session":
            rows = [(i, s[0], s[1], s[2]) for i, s in self.session.items()]
        else:
            self.flush()
            sql = ("SELECT t.ifindex, COALESCE(a.name, t.name, '接口 ' || t.ifindex), "
                   "SUM(t.rx), SUM(t.tx) FROM traffic t "
                   "LEFT JOIN adapters a ON a.ifindex = t.ifindex ")
            try:
                if range_key == "all":
                    cur = self.conn.execute(sql + "GROUP BY t.ifindex")
                else:
                    minutes = RANGE_MINUTES.get(range_key, 60)
                    cutoff = int(time.time() // 60) - minutes + 1
                    cur = self.conn.execute(
                        sql + "WHERE t.minute >= ? GROUP BY t.ifindex", (cutoff,))
                rows = cur.fetchall()
            except sqlite3.Error:
                rows = []
        rows = [(i, n, int(rx or 0), int(tx or 0)) for i, n, rx, tx in rows]
        rows.sort(key=lambda r: r[2] + r[3], reverse=True)
        return rows

    def close(self):
        try:
            self.flush()
            self.conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------- 进程网速
# 思路与 Sysinternals TCPView 相同，纯 ctypes、无需管理员权限、无驱动：
#   1. GetExtendedTcpTable(TCP_TABLE_OWNER_PID_ALL) 列出全部 TCP 连接及其 PID
#   2. GetPerTcpConnectionEStats 开启并读取每条连接的累计收发字节
#   3. 按 PID 汇总两次采样的差分 -> 每进程实时速度
# 局限：只能归属 TCP 流量；UDP/QUIC 等无连接流量无法按进程统计。
AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_ALL = 5
MIB_TCP_STATE_ESTAB = 5
TCP_ESTATS_DATA = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_INSUFFICIENT_BUFFER = 122

iphlpapi.GetExtendedTcpTable.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
    ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
iphlpapi.GetExtendedTcpTable.restype = ctypes.c_ulong
for _name in ("GetPerTcpConnectionEStats", "GetPerTcp6ConnectionEStats"):
    _fn = getattr(iphlpapi, _name)
    _fn.argtypes = [ctypes.c_void_p, ctypes.c_int,
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    _fn.restype = ctypes.c_ulong

kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
    ctypes.POINTER(ctypes.c_ulong)]
kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + \
    [ctypes.POINTER(ctypes.c_ulonglong)] * 4
kernel32.GetProcessTimes.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


class ProcessNetMonitor:
    """按进程汇总 TCP 实时速度（字节/秒）。

    * 第一次 sample() 只建立基线（速度全 0），之后每次差分。
    * 只统计 ESTABLISHED 的非回环连接；estats 计数自开启采集起累计，
      连接中途加入时之前的流量不计入（对实时速度无影响）。
    * 进程名缓存按创建时间定期校验，防止 PID 复用导致张冠李戴。
    """

    NAME_VERIFY_INTERVAL = 60.0   # 进程名缓存校验周期（秒）

    def __init__(self):
        self._prev = {}         # key -> (pid, rx, tx)
        self._prev_time = None
        self._enabled = set()   # 已尝试开启 estats 采集的连接
        self._names = {}        # pid -> (创建时间, 名称)
        self._last_verify = 0.0
        self._totals = {}       # pid -> [rx, tx] 监控以来累计（排序辅助）

    def reset(self):
        """清空基线（切换标签页/重新打开时用），下一拍重新起算。"""
        self._prev = {}
        self._prev_time = None
        self._totals = {}

    # ---------------- 连接枚举 ----------------
    @staticmethod
    def _tcp_table(af):
        size = ctypes.c_ulong(0)
        ret = iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), 0, af, TCP_TABLE_OWNER_PID_ALL, 0)
        if ret != ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            return b""
        for _ in range(4):  # 表可能在两次调用间增长，重试几次
            buf = (ctypes.c_ubyte * size.value)()
            ret = iphlpapi.GetExtendedTcpTable(
                buf, ctypes.byref(size), 0, af, TCP_TABLE_OWNER_PID_ALL, 0)
            if ret == 0:
                return ctypes.string_at(buf, size.value)
            if ret != ERROR_INSUFFICIENT_BUFFER:
                return b""
        return b""

    def _connections(self):
        """返回 [(key, pid, row_bytes, is6), ...]，仅 ESTABLISHED 非回环。

        row_bytes 是 MIB_TCPROW_OWNER_PID 的前 20 字节（v4）或
        MIB_TCP6ROW_OWNER_PID 的前 48 字节（v6），恰好就是
        GetPerTcp[6]ConnectionEStats 需要的 MIB_TCP[6]ROW 布局。
        """
        out = []
        raw = self._tcp_table(AF_INET)
        if len(raw) >= 4:
            n = _struct.unpack_from("<I", raw, 0)[0]
            for i in range(n):
                off = 4 + i * 24
                if off + 24 > len(raw):
                    break
                state, la, lp, ra, rp, pid = _struct.unpack_from("<6I", raw, off)
                if state != MIB_TCP_STATE_ESTAB:
                    continue
                if (la & 0xFF) == 127 or (ra & 0xFF) == 127:
                    continue  # 回环不算真实网络流量
                out.append(((4, la, lp, ra, rp, pid), pid,
                            raw[off:off + 20], False))
        raw = self._tcp_table(AF_INET6)
        if len(raw) >= 4:
            n = _struct.unpack_from("<I", raw, 0)[0]
            loopback = b"\x00" * 15 + b"\x01"
            for i in range(n):
                off = 4 + i * 56
                if off + 56 > len(raw):
                    break
                state, pid = _struct.unpack_from("<II", raw, off + 48)
                if state != MIB_TCP_STATE_ESTAB:
                    continue
                la = raw[off:off + 16]
                ra = raw[off + 24:off + 40]
                if la == loopback or ra == loopback:
                    continue
                lp = _struct.unpack_from("<I", raw, off + 20)[0]
                rp = _struct.unpack_from("<I", raw, off + 44)[0]
                out.append(((6, la, lp, ra, rp, pid), pid,
                            raw[off:off + 48], True))
        return out

    # ---------------- 每连接字节计数 ----------------
    def _read_bytes(self, key, row, is6):
        """读取连接累计 (接收字节, 发送字节)；失败返回 None。"""
        fn = (iphlpapi.GetPerTcp6ConnectionEStats if is6
              else iphlpapi.GetPerTcpConnectionEStats)
        rowbuf = (ctypes.c_ubyte * len(row)).from_buffer_copy(row)
        if key not in self._enabled:
            # TCP_ESTATS_DATA_RW_v0：EnableCollection = TRUE
            rw = ctypes.c_ubyte(1)
            fn(rowbuf, TCP_ESTATS_DATA,
               ctypes.byref(rw), 0, ctypes.sizeof(rw),
               None, 0, 0, None, 0, 0)
            self._enabled.add(key)
        # TCP_ESTATS_DATA_ROD_v0（96 字节）：DataBytesOut @0 / DataBytesIn @16
        rod = (ctypes.c_ubyte * 96)()
        if fn(rowbuf, TCP_ESTATS_DATA,
              None, 0, 0, None, 0, 0,
              rod, 0, ctypes.sizeof(rod)) != 0:
            return None
        tx = _struct.unpack_from("<Q", rod, 0)[0]
        rx = _struct.unpack_from("<Q", rod, 16)[0]
        return rx, tx

    # ---------------- 进程名 ----------------
    @staticmethod
    def _lookup(pid):
        """返回 (创建时间, 名称)；打不开的进程回退为 'PID xxx'。"""
        if pid == 0:
            return (0, "System Idle Process")
        if pid == 4:
            return (0, "System")
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return (0, f"PID {pid}")
        try:
            ctime = ctypes.c_ulonglong()
            ft = ctypes.c_ulonglong()
            ok = kernel32.GetProcessTimes(
                h, ctypes.byref(ctime), ctypes.byref(ft),
                ctypes.byref(ft), ctypes.byref(ft))
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            name = f"PID {pid}"
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value) or name
            return (ctime.value if ok else 0, name)
        finally:
            kernel32.CloseHandle(h)

    def _name(self, pid):
        ent = self._names.get(pid)
        if ent is None:
            ent = self._lookup(pid)
            self._names[pid] = ent
        return ent[1]

    def _verify_names(self, pids, now):
        """定期校验缓存进程的创建时间，PID 复用时重新解析名称。"""
        if now - self._last_verify < self.NAME_VERIFY_INTERVAL:
            return
        self._last_verify = now
        for pid in list(self._names):
            if pid not in pids:
                del self._names[pid]
                continue
            if pid in (0, 4):
                continue
            h = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                del self._names[pid]
                continue
            ctime = ctypes.c_ulonglong()
            ft = ctypes.c_ulonglong()
            ok = kernel32.GetProcessTimes(
                h, ctypes.byref(ctime), ctypes.byref(ft),
                ctypes.byref(ft), ctypes.byref(ft))
            kernel32.CloseHandle(h)
            if not ok or ctime.value != self._names[pid][0]:
                del self._names[pid]

    # ---------------- 采样 ----------------
    def sample(self):
        """返回 [(pid, 名称, 下载B/s, 上传B/s), ...]，按合计速度降序。"""
        now = time.perf_counter()
        dt = None
        if self._prev_time is not None and now > self._prev_time:
            dt = now - self._prev_time
        cur = {}
        speeds = {}     # pid -> [down, up]
        live_keys = set()
        for key, pid, row, is6 in self._connections():
            live_keys.add(key)
            b = self._read_bytes(key, row, is6)
            if b is None:
                continue
            rx, tx = b
            cur[key] = (pid, rx, tx)
            sp = speeds.setdefault(pid, [0.0, 0.0])
            prev = self._prev.get(key)
            if prev is not None and prev[0] == pid and dt:
                drx = max(0, rx - prev[1])
                dtx = max(0, tx - prev[2])
                sp[0] += drx / dt
                sp[1] += dtx / dt
                tot = self._totals.setdefault(pid, [0, 0])
                tot[0] += drx
                tot[1] += dtx
        self._enabled &= live_keys
        self._prev = cur
        self._prev_time = now
        self._totals = {p: t for p, t in self._totals.items() if p in speeds}
        self._verify_names(set(speeds), now)
        rows = [(pid, self._name(pid), sp[0], sp[1])
                for pid, sp in speeds.items()]

        def sort_key(r):
            tot = self._totals.get(r[0], (0, 0))
            return (-(r[2] + r[3]), -(tot[0] + tot[1]), r[0])

        rows.sort(key=sort_key)
        return rows


# ---------------------------------------------------------------- 详情窗口
class DetailWindow:
    """网络详情与流量统计：深色自绘 UI，可滚动，Esc / ✕ 关闭。

    两个标签页：「网卡与流量」（实时速度 + 历史统计）、
    「进程网速」（每进程 TCP 实时速度）。
    """

    BASE_W = 660          # 逻辑宽度（按 DPI 缩放）
    TITLE_H = 48

    def __init__(self, app):
        self.app = app
        self.s = dpi_scale()
        self.W = int(self.BASE_W * self.s)
        self.pad = int(18 * self.s)
        self.row_w = self.W - self.pad * 2
        self._range = app.cfg.get("stats_range", "1h")
        self._tab = app.cfg.get("detail_tab", "nic")
        self._speed_rows = {}
        self._stats_rows = {}
        self._stats_keys = None
        self._proc_rows = {}
        self._proc_keys = None
        self._drag = None
        self._placed = False

        self.f_ui = (UI_FAMILY, 10)
        self.f_small = (UI_FAMILY, 9)
        self.f_tiny = (UI_FAMILY, 8)
        self.f_num = ("Consolas", 11)
        self.f_big = (FONT_FAMILY, 20, "bold")
        self.f_title = (UI_FAMILY, 10, "bold")
        self._ui_font = tkfont.Font(font=self.f_ui)

        win = tk.Toplevel(app.root)
        self.win = win
        win.title("网络详情与流量统计")
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG_COLOR)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())

        self._build_titlebar()
        self._build_body()
        self._apply_win_style()

        self.update_speeds(app.latest_rows)
        self.refresh_stats()
        if self._tab == "proc":
            mon = app.get_proc_monitor()
            mon.reset()
            self.update_processes(mon.sample())
        self._resize()
        win.focus_set()

    # ---------------- 标题栏 ----------------
    def _build_titlebar(self):
        s = self.s
        h = int(self.TITLE_H * s)
        self.title_h = h
        tb = tk.Frame(self.win, bg=BG_COLOR, height=h)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        title = tk.Label(tb, text="网络详情与流量统计", font=self.f_title,
                         bg=BG_COLOR, fg=TEXT_COLOR)
        title.place(x=int(18 * s), rely=0.5, anchor="w")
        hint = tk.Label(tb, text="Esc 关闭", font=self.f_tiny,
                        bg=BG_COLOR, fg=FAINT_COLOR)
        hint.place(relx=1.0, x=-int(58 * s), rely=0.5, anchor="e")
        close = tk.Label(tb, text="✕", font=(FONT_FAMILY, 10),
                         bg=BG_COLOR, fg=DIM_COLOR, cursor="hand2")
        close.place(relx=1.0, y=0, anchor="ne", width=int(48 * s), height=h)
        close.bind("<Enter>", lambda e: close.configure(bg="#e81123", fg="#ffffff"))
        close.bind("<Leave>", lambda e: close.configure(bg=BG_COLOR, fg=DIM_COLOR))
        close.bind("<Button-1>", lambda e: self.close())
        for w in (tb, title):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y())

    def _motion(self, e):
        if not self._drag:
            return
        sx, sy, ox, oy = self._drag
        self.win.geometry(f"+{ox + e.x_root - sx}+{oy + e.y_root - sy}")

    # ---------------- 内容 ----------------
    def _build_body(self):
        s = self.s
        pad = self.pad
        self.canvas = tk.Canvas(self.win, bg=BG_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=BG_COLOR)
        self.body_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.body_id, width=e.width))
        self.win.bind_all("<MouseWheel>", self._wheel)

        # ---- 标签页栏 ----
        tabbar = tk.Frame(self.body, bg=BG_COLOR)
        tabbar.pack(fill="x", padx=pad, pady=(int(10 * s), int(2 * s)))
        self._tabs = {}
        for key, label in (("nic", "网卡与流量"), ("proc", "进程网速")):
            tab = tk.Label(tabbar, text=label, font=self.f_title,
                           bg=BG_COLOR, fg=DIM_COLOR, cursor="hand2",
                           padx=int(14 * s), pady=int(7 * s))
            tab.pack(side="left", padx=(0, int(6 * s)))
            tab.bind("<Button-1>", lambda e, k=key: self._set_tab(k))
            tab.bind("<Enter>", lambda e, k=key: self._tab_hover(k, True))
            tab.bind("<Leave>", lambda e, k=key: self._tab_hover(k, False))
            self._tabs[key] = tab
        self._paint_tabs()

        self.page_nic = tk.Frame(self.body, bg=BG_COLOR)
        self.page_proc = tk.Frame(self.body, bg=BG_COLOR)
        self._build_nic_page()
        self._build_proc_page()
        self._show_tab_page()

    def _build_nic_page(self):
        s = self.s
        page = self.page_nic

        # ---- 实时速度 ----
        self.speed_header = tk.Label(
            page, text=self._speed_header_text(), font=self.f_small,
            bg=BG_COLOR, fg=DIM_COLOR, anchor="w")
        self.speed_header.pack(fill="x", pady=(int(6 * s), int(6 * s)))
        self.speed_box = tk.Frame(page, bg=BG_COLOR)
        self.speed_box.pack(fill="x")

        # ---- 流量统计 ----
        tk.Label(page, text="流量统计", font=self.f_small,
                 bg=BG_COLOR, fg=DIM_COLOR, anchor="w").pack(
            fill="x", pady=(int(18 * s), int(6 * s)))

        seg_box = tk.Frame(page, bg=CARD_BG)
        seg_box.pack(fill="x", pady=(0, int(10 * s)))
        self._segs = {}
        for key, label in STATS_RANGES:
            seg = tk.Label(seg_box, text=label, font=self.f_small,
                           bg=CARD_BG, fg=DIM_COLOR, cursor="hand2",
                           padx=int(11 * s), pady=int(6 * s))
            seg.pack(side="left")
            seg.bind("<Button-1>", lambda e, k=key: self._set_range(k))
            seg.bind("<Enter>", lambda e, k=key: self._seg_hover(k, True))
            seg.bind("<Leave>", lambda e, k=key: self._seg_hover(k, False))
            self._segs[key] = seg
        self._paint_segs()

        tiles = tk.Frame(page, bg=BG_COLOR)
        tiles.pack(fill="x", pady=(0, int(10 * s)))
        self.down_total_var = tk.StringVar(value="0 B")
        self.up_total_var = tk.StringVar(value="0 B")
        self._make_tile(tiles, "↓ 总下载", self.down_total_var, DOWN_COLOR, 0)
        self._make_tile(tiles, "↑ 总上传", self.up_total_var, UP_COLOR, 1)

        self.stats_box = tk.Frame(page, bg=BG_COLOR)
        self.stats_box.pack(fill="x")

        self.footer_label = tk.Label(
            page, text=self._footer_text(), font=self.f_tiny,
            bg=BG_COLOR, fg=FAINT_COLOR, anchor="w")
        self.footer_label.pack(fill="x", pady=(int(12 * s), int(14 * s)))

    def _build_proc_page(self):
        s = self.s
        page = self.page_proc

        self.proc_header = tk.Label(
            page, text=self._proc_header_text(), font=self.f_small,
            bg=BG_COLOR, fg=DIM_COLOR, anchor="w")
        self.proc_header.pack(fill="x", pady=(int(6 * s), int(6 * s)))

        tiles = tk.Frame(page, bg=BG_COLOR)
        tiles.pack(fill="x", pady=(0, int(10 * s)))
        self.proc_down_var = tk.StringVar(value="0 B/s")
        self.proc_up_var = tk.StringVar(value="0 B/s")
        self._make_tile(tiles, "↓ TCP 下载", self.proc_down_var, DOWN_COLOR, 0)
        self._make_tile(tiles, "↑ TCP 上传", self.proc_up_var, UP_COLOR, 1)

        self.proc_box = tk.Frame(page, bg=BG_COLOR)
        self.proc_box.pack(fill="x")

        tk.Label(page, text="仅统计 TCP 连接流量（UDP/QUIC 不计入）· "
                 "速度为进程全部连接合计", font=self.f_tiny,
                 bg=BG_COLOR, fg=FAINT_COLOR, anchor="w").pack(
            fill="x", pady=(int(12 * s), int(14 * s)))

    # ---------------- 标签页 ----------------
    def _paint_tabs(self):
        for key, tab in self._tabs.items():
            if key == self._tab:
                tab.configure(bg=CARD_BG, fg=TEXT_COLOR)
            else:
                tab.configure(bg=BG_COLOR, fg=DIM_COLOR)

    def _tab_hover(self, key, entering):
        if key == self._tab:
            return
        self._tabs[key].configure(bg=CARD_HOVER if entering else BG_COLOR)

    def _show_tab_page(self):
        self.page_nic.pack_forget()
        self.page_proc.pack_forget()
        page = self.page_nic if self._tab == "nic" else self.page_proc
        page.pack(fill="x", padx=self.pad)

    def _set_tab(self, key):
        if key == self._tab:
            return
        self._tab = key
        self.app.cfg["detail_tab"] = key
        save_config(self.app.cfg)
        self._paint_tabs()
        self._show_tab_page()
        if key == "proc":
            mon = self.app.get_proc_monitor()
            mon.reset()
            self.update_processes(mon.sample())  # 第一拍建立基线
        self._resize()

    def proc_tab_active(self):
        return self._tab == "proc"

    def _footer_text(self):
        return ("仅统计程序运行期间 · 建议开启开机自启 · "
                f"数据保留 {self.app.cfg['retention_days']} 天")

    def update_retention_text(self):
        self.footer_label.configure(text=self._footer_text())

    def _speed_header_text(self):
        return f"实时速度 · 每 {self.app.cfg['interval']:g} 秒刷新"

    def _proc_header_text(self):
        return f"进程实时网速 · 每 {self.app.cfg['interval']:g} 秒刷新"

    def update_interval_text(self):
        self.speed_header.configure(text=self._speed_header_text())
        self.proc_header.configure(text=self._proc_header_text())

    def _make_tile(self, parent, caption, var, color, index):
        s = self.s
        tile = tk.Frame(parent, bg=CARD_BG)
        tile.pack(side="left", fill="both", expand=True,
                  padx=(0, int(8 * s)) if index == 0 else (int(8 * s), 0))
        tk.Label(tile, text=caption, font=self.f_small, bg=CARD_BG,
                 fg=DIM_COLOR, anchor="w").pack(
            anchor="w", padx=int(14 * s), pady=(int(10 * s), 0))
        tk.Label(tile, textvariable=var, font=self.f_big, bg=CARD_BG,
                 fg=color, anchor="w").pack(
            anchor="w", padx=int(14 * s), pady=(0, int(10 * s)))

    # ---------------- 分段控件 ----------------
    def _paint_segs(self):
        for key, seg in self._segs.items():
            if key == self._range:
                seg.configure(bg=SEG_ACTIVE, fg="#dbe4ff")
            else:
                seg.configure(bg=CARD_BG, fg=DIM_COLOR)

    def _seg_hover(self, key, entering):
        if key == self._range:
            return
        self._segs[key].configure(bg=CARD_HOVER if entering else CARD_BG)

    def _set_range(self, key):
        self._range = key
        self.app.cfg["stats_range"] = key
        save_config(self.app.cfg)
        self._paint_segs()
        self.refresh_stats()

    # ---------------- 实时速度行 ----------------
    def _make_speed_row(self, name):
        s = self.s
        rw = self.row_w
        h = int(40 * s)
        row = tk.Frame(self.speed_box, bg=CARD_BG, height=h)
        row.pack(fill="x", pady=(0, int(6 * s)))
        row.pack_propagate(False)
        nm = tk.Label(row, text=name, font=self.f_ui, bg=CARD_BG, fg=TEXT_COLOR)
        nm.place(x=int(14 * s), rely=0.5, anchor="w")
        down = tk.Label(row, font=self.f_num, bg=CARD_BG, fg=DOWN_COLOR)
        down.place(x=rw - int(140 * s), rely=0.5, anchor="e")
        up = tk.Label(row, font=self.f_num, bg=CARD_BG, fg=UP_COLOR)
        up.place(x=rw - int(14 * s), rely=0.5, anchor="e")
        return {"down": down, "up": up}

    def update_speeds(self, rows):
        """rows: [(ifindex, name, down_bps, up_bps), ...]"""
        keys = [r[0] for r in rows]
        if keys != list(self._speed_rows.keys()):
            for w in self._speed_rows.values():
                w["down"].master.destroy()
            self._speed_rows = {}
            for i, name, _d, _u in rows:
                self._speed_rows[i] = self._make_speed_row(name)
            self._resize()
        for i, _name, d, u in rows:
            r = self._speed_rows[i]
            r["down"].configure(text=speed_text(d))
            r["up"].configure(text=speed_text(u))

    # ---------------- 统计行 ----------------
    def _make_stats_row(self, name, with_bar=True):
        s = self.s
        rw = self.row_w
        h = int(52 * s) if with_bar else int(40 * s)
        row = tk.Frame(self.stats_box, bg=CARD_BG, height=h)
        row.pack(fill="x", pady=(0, int(6 * s)))
        row.pack_propagate(False)
        ty = int(13 * s)
        nm = tk.Label(row, text=name, font=self.f_ui, bg=CARD_BG, fg=TEXT_COLOR)
        nm.place(x=int(14 * s), y=ty, anchor="w")
        down = tk.Label(row, font=self.f_num, bg=CARD_BG, fg=DOWN_COLOR)
        down.place(x=rw - int(250 * s), y=ty, anchor="e")
        up = tk.Label(row, font=self.f_num, bg=CARD_BG, fg=UP_COLOR)
        up.place(x=rw - int(135 * s), y=ty, anchor="e")
        total = tk.Label(row, font=self.f_num, bg=CARD_BG, fg=NUM_COLOR)
        total.place(x=rw - int(14 * s), y=ty, anchor="e")
        widgets = {"down": down, "up": up, "total": total}
        if with_bar:
            bar_w = rw - int(28 * s)
            bar_h = max(3, int(4 * s))
            bar = tk.Canvas(row, width=bar_w, height=bar_h, bg=BORDER_COLOR,
                            highlightthickness=0, bd=0)
            bar.place(x=int(14 * s), y=h - int(11 * s))
            rect = bar.create_rectangle(0, 0, 0, bar_h, fill=DOWN_COLOR, width=0)
            widgets.update(bar=bar, rect=rect, bar_w=bar_w, bar_h=bar_h)
        return widgets

    def refresh_stats(self):
        rows = self.app.recorder.query(self._range)
        keys = [r[0] for r in rows]
        if keys != self._stats_keys:
            for w in self._stats_rows.values():
                w["down"].master.destroy()
            self._stats_rows = {}
            if rows:
                for i, name, _rx, _tx in rows:
                    self._stats_rows[i] = self._make_stats_row(name)
            else:
                self._stats_rows["empty"] = self._make_stats_row(
                    "（暂无数据）", with_bar=False)
            self._stats_keys = keys
            self._resize()
        total_rx = sum(r[2] for r in rows)
        total_tx = sum(r[3] for r in rows)
        grand = total_rx + total_tx
        for i, _name, rx, tx in rows:
            w = self._stats_rows[i]
            w["down"].configure(text=format_bytes(rx))
            w["up"].configure(text=format_bytes(tx))
            w["total"].configure(text=format_bytes(rx + tx))
            share = (rx + tx) / grand if grand else 0
            w["bar"].coords(w["rect"], 0, 0,
                            max(2, int(share * w["bar_w"])), w["bar_h"])
        self.down_total_var.set(format_bytes(total_rx))
        self.up_total_var.set(format_bytes(total_tx))

    # ---------------- 进程网速行 ----------------
    def _set_proc_name(self, w, name, pid):
        """进程名过长时截断，PID 紧跟其后（弱化的次要文字）。"""
        s = self.s
        text = name
        max_w = self.row_w - int(330 * s)
        while text and self._ui_font.measure(text) > max_w:
            text = text[:-1]
        if text != name:
            text = text.rstrip() + "…"
        w["nm"].configure(text=text)
        if w["pid_lb"] is not None:
            w["pid_lb"].place(
                x=int(14 * s) + self._ui_font.measure(text) + int(8 * s))
        w["name"] = name

    def _make_proc_row(self, name, pid):
        s = self.s
        rw = self.row_w
        h = int(52 * s)
        row = tk.Frame(self.proc_box, bg=CARD_BG, height=h)
        row.pack(fill="x", pady=(0, int(6 * s)))
        row.pack_propagate(False)
        ty = int(13 * s)
        nm = tk.Label(row, font=self.f_ui, bg=CARD_BG, fg=TEXT_COLOR)
        nm.place(x=int(14 * s), y=ty, anchor="w")
        pid_lb = None
        if pid is not None:
            pid_lb = tk.Label(row, text=f"PID {pid}", font=self.f_tiny,
                              bg=CARD_BG, fg=FAINT_COLOR)
            pid_lb.place(y=ty, anchor="w")
        down = tk.Label(row, font=self.f_num, bg=CARD_BG, fg=DOWN_COLOR)
        down.place(x=rw - int(140 * s), y=ty, anchor="e")
        up = tk.Label(row, font=self.f_num, bg=CARD_BG, fg=UP_COLOR)
        up.place(x=rw - int(14 * s), y=ty, anchor="e")
        bar_w = rw - int(28 * s)
        bar_h = max(3, int(4 * s))
        bar = tk.Canvas(row, width=bar_w, height=bar_h, bg=BORDER_COLOR,
                        highlightthickness=0, bd=0)
        bar.place(x=int(14 * s), y=h - int(11 * s))
        rect = bar.create_rectangle(0, 0, 0, bar_h, fill=DOWN_COLOR, width=0)
        w = {"nm": nm, "pid_lb": pid_lb, "name": None,
             "down": down, "up": up,
             "bar": bar, "rect": rect, "bar_w": bar_w, "bar_h": bar_h}
        self._set_proc_name(w, name, pid)
        return w

    def update_processes(self, rows):
        """rows: [(pid, name, down_bps, up_bps), ...]，已按速度降序。"""
        keys = [r[0] for r in rows]
        if keys != self._proc_keys:
            if set(keys) != set(self._proc_rows):
                for w in self._proc_rows.values():
                    w["down"].master.destroy()
                self._proc_rows = {}
                if rows:
                    for pid, name, _d, _u in rows:
                        self._proc_rows[pid] = self._make_proc_row(name, pid)
                else:
                    self._proc_rows["empty"] = self._make_proc_row(
                        "（暂无 TCP 连接）", None)
                self._resize()
            # 仅调整排列顺序，不销毁重建控件
            for pid in (keys if rows else ["empty"]):
                r = self._proc_rows[pid]["down"].master
                r.pack_forget()
                r.pack(fill="x", pady=(0, int(6 * self.s)))
            self._proc_keys = keys
        total_d = sum(r[2] for r in rows)
        total_u = sum(r[3] for r in rows)
        grand = total_d + total_u
        for pid, name, d, u in rows:
            w = self._proc_rows[pid]
            if w["name"] != name:
                self._set_proc_name(w, name, pid)
            w["down"].configure(text=speed_text(d))
            w["up"].configure(text=speed_text(u))
            share = (d + u) / grand if grand else 0.0
            w["bar"].coords(w["rect"], 0, 0,
                            int(share * w["bar_w"]) if share > 0 else 0,
                            w["bar_h"])
        self.proc_down_var.set(speed_text(total_d))
        self.proc_up_var.set(speed_text(total_u))

    # ---------------- 窗口 ----------------
    def _wheel(self, e):
        try:
            if self.body.winfo_reqheight() > self.canvas.winfo_height():
                self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        except tk.TclError:
            pass

    def _resize(self):
        self.win.update_idletasks()
        content_h = self.body.winfo_reqheight()
        sh = self.win.winfo_screenheight()
        h = min(self.title_h + content_h, sh - int(60 * self.s))
        h = max(h, int(320 * self.s))
        if not self._placed:
            sw = self.win.winfo_screenwidth()
            x = (sw - self.W) // 2
            y = max(20, (sh - h) // 2)
            self._placed = True
        else:
            x, y = self.win.winfo_x(), self.win.winfo_y()
        self.win.geometry(f"{self.W}x{h}+{x}+{y}")

    def _apply_win_style(self):
        self.win.update_idletasks()
        hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        try:
            pref = ctypes.c_int(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(pref), ctypes.sizeof(pref))
            border = ctypes.c_int(0x003A312C)  # COLORREF(#2c313a)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
        except OSError:
            pass

    def close(self):
        try:
            self.win.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        self.app.detail = None
        self.app.proc_monitor = None  # 关窗即停止进程采样
        self.win.destroy()


# ---------------------------------------------------------------- 主窗口
class NetSpeedWindow:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.recorder = TrafficRecorder(DB_PATH, self.cfg["retention_days"])
        self.detail = None
        self.proc_monitor = None    # 进程网速采样器（详情窗口打开时按需创建）
        self.latest_rows = []       # [(ifindex, name, down_bps, up_bps)]
        self._last = None           # {ifindex: (rx, tx)}
        self._last_time = None
        self._after_id = None
        self._drag = None

        # 靠边自动隐藏状态
        s = dpi_scale()
        self._dock_snap = int(DOCK_SNAP_DIST * s)   # 吸附判定距离
        self._peek = max(6, int(DOCK_PEEK * s))     # 隐藏后留出的细边
        self._strip_bars = {}     # 细边速度条 canvas 项与几何信息
        self._last_down = 0.0     # 最近一次合计速度（供细边速度条用）
        self._last_up = 0.0
        self._dock_edge = None      # 'left'/'right'/'top'/'bottom'，None=未停靠
        self._dock_shown = (0, 0)   # 停靠时的完整可见位置
        self._dock_hidden = (0, 0)  # 停靠时的隐藏位置（大部分移出屏幕）
        self._dock_state = "shown"  # shown / hidden（动画期间由 _dock_anim_id 标记）
        self._dock_anim_id = None
        self._dock_poll_id = None
        self._dock_leave_since = None  # 鼠标离开窗口的起始时刻
        self._all_menus = ()

        self._setup_window()
        self._build_widgets()
        self._build_strip()
        self._build_menu()
        self._bind_events()
        self._apply_position()
        self._apply_style()
        # 启动时若已停在屏幕边缘，直接进入停靠状态
        self.root.update_idletasks()
        if self.cfg["auto_hide"]:
            self._try_dock(self.root.winfo_x(), self.root.winfo_y())
        self._dock_poll_id = self.root.after(DOCK_POLL_MS, self._dock_poll)
        self.tick()

    # ---------------- 窗口与控件 ----------------
    def _setup_window(self):
        r = self.root
        r.title("网速悬浮窗")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", self.cfg["alpha"])
        r.configure(bg=BG_COLOR)
        r.resizable(False, False)

        self.font = (FONT_FAMILY, FONT_SIZE, "bold")
        f = tkfont.Font(font=self.font)
        self.line_h = f.metrics("linespace")
        self._arrow_w = f.measure("↓")
        self._num_w = f.measure("999.99")
        self._gap = 6
        unit_w = f.measure("MB/s")
        self.win_w = PAD_X * 2 + self._arrow_w + self._gap + self._num_w + self._gap + unit_w
        self.win_h = self.line_h * 2 + PAD_Y * 2
        r.geometry(f"{self.win_w}x{self.win_h}")

    def _build_widgets(self):
        r = self.root
        self.down_num_var = tk.StringVar(value="     0")
        self.down_unit_var = tk.StringVar(value="B/s")
        self.up_num_var = tk.StringVar(value="     0")
        self.up_unit_var = tk.StringVar(value="B/s")
        num_x = PAD_X + self._arrow_w + self._gap
        unit_x = num_x + self._num_w + self._gap

        def make_row(arrow, color, num_var, unit_var, y):
            a = tk.Label(r, text=arrow, font=self.font, fg=color, bg=BG_COLOR)
            a.place(x=PAD_X, y=y)
            n = tk.Label(r, textvariable=num_var, font=self.font,
                         fg=NUM_COLOR, bg=BG_COLOR, anchor="e")
            n.place(x=num_x + self._num_w, y=y, anchor="ne")
            u = tk.Label(r, textvariable=unit_var, font=self.font,
                         fg=DIM_COLOR, bg=BG_COLOR, anchor="w")
            u.place(x=unit_x, y=y)

        make_row("↑", UP_COLOR, self.up_num_var, self.up_unit_var, PAD_Y)
        make_row("↓", DOWN_COLOR, self.down_num_var, self.down_unit_var, PAD_Y + self.line_h)

    # ---------------- 隐藏细边（浅色边框 + 速度条） ----------------
    def _build_strip(self):
        """整窗大小的画布，隐藏时盖在标签上方，只露出贴边的细边部分。

        细边内：左右停靠时上半上传（蓝）、下半下载（绿）；
        上下停靠时左半上传、右半下载。条长随速度变化。
        """
        self.strip_canvas = tk.Canvas(
            self.root, width=self.win_w, height=self.win_h,
            bg=BG_COLOR, highlightthickness=0, bd=0)

    def _draw_strip(self):
        c = self.strip_canvas
        c.delete("all")
        W, H, p = self.win_w, self.win_h, self._peek
        edge = self._dock_edge
        # 注意：滑出方向与露出的窗口边缘相反——向右滑出时，
        # 留在屏幕内的是窗口左缘，其余方向同理
        if edge == "left":
            sx0, sy0, sx1, sy1 = W - p, 0, W, H
        elif edge == "right":
            sx0, sy0, sx1, sy1 = 0, 0, p, H
        elif edge == "top":
            sx0, sy0, sx1, sy1 = 0, H - p, W, H
        else:  # bottom
            sx0, sy0, sx1, sy1 = 0, 0, W, p
        c.create_rectangle(sx0, sy0, sx1 - 1, sy1 - 1,
                           outline=STRIP_BORDER, width=1)
        # 边框内再留 1px 空隙，对半分：上传、下载
        ix0, iy0, ix1, iy1 = sx0 + 2, sy0 + 2, sx1 - 2, sy1 - 2
        if edge in ("left", "right"):
            # 竖条：上半上传、下半下载，均从下往上填充
            mid = H // 2
            up_region = (ix0, iy0, ix1, mid - 1)
            dn_region = (ix0, mid + 1, ix1, iy1)
            anchor = "bottom"
        else:
            # 横条：左半上传、右半下载，均从左往右填充
            mid = W // 2
            up_region = (ix0, iy0, mid - 1, iy1)
            dn_region = (mid + 1, iy0, ix1, iy1)
            anchor = "left"
        bars = {}
        for key, region, color in (
                ("up", up_region, UP_COLOR),
                ("down", dn_region, DOWN_COLOR)):
            c.create_rectangle(*region, fill=BORDER_COLOR, width=0)  # 底轨
            item = c.create_rectangle(*region, fill=color, width=0)
            bars[key] = (item, region, anchor)
        self._strip_bars = bars

    def _show_strip(self):
        if self._dock_edge is None:
            return
        self._draw_strip()
        self.strip_canvas.place(x=0, y=0)
        # 注意：Canvas 把 tkraise/lift 都重定义成了提升画布项，
        # 提升窗口层级需显式调用 Misc 的窗口版 tkraise
        tk.Misc.tkraise(self.strip_canvas)
        self._update_strip(self._last_down, self._last_up)

    def _hide_strip(self):
        self.strip_canvas.place_forget()
        self._strip_bars = {}

    def _update_strip(self, down_bps, up_bps):
        if not self._strip_bars:
            return
        c = self.strip_canvas
        for key, bps in (("up", up_bps), ("down", down_bps)):
            item, (x0, y0, x1, y1), anchor = self._strip_bars[key]
            f = speed_fraction(bps)
            if anchor == "bottom":    # 竖条：从下往上填充
                coords = (x0, y1 - f * (y1 - y0), x1, y1)
            else:                     # 横条：从左往右填充
                coords = (x0, y0, x0 + f * (x1 - x0), y1)
            c.coords(item, *coords)

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, font=UI_FONT)
        self._style_menu(self.menu)
        self.menu.add_command(label="网络详情与流量统计", command=self.open_detail)
        self.menu.add_separator()

        self.interval_var = tk.DoubleVar(value=self.cfg["interval"])
        interval_menu = tk.Menu(self.menu, tearoff=0, font=UI_FONT)
        self._style_menu(interval_menu)
        for sec in INTERVAL_CHOICES:
            interval_menu.add_radiobutton(
                label=f"{sec:g} 秒", variable=self.interval_var, value=sec,
                command=self._on_interval_change,
            )
        self.menu.add_cascade(label="刷新间隔", menu=interval_menu)

        self.alpha_var = tk.DoubleVar(value=self.cfg["alpha"])
        alpha_menu = tk.Menu(self.menu, tearoff=0, font=UI_FONT)
        self._style_menu(alpha_menu)
        for a in ALPHA_CHOICES:
            alpha_menu.add_radiobutton(
                label=f"{int(a * 100)}%", variable=self.alpha_var, value=a,
                command=self._on_alpha_change,
            )
        self.menu.add_cascade(label="透明度", menu=alpha_menu)

        self.auto_hide_var = tk.BooleanVar(value=self.cfg["auto_hide"])
        self.menu.add_checkbutton(
            label="靠边自动隐藏", variable=self.auto_hide_var,
            command=self._on_auto_hide_change,
        )

        self.retention_var = tk.IntVar(value=self.cfg["retention_days"])
        retention_menu = tk.Menu(self.menu, tearoff=0, font=UI_FONT)
        self._style_menu(retention_menu)
        for days in RETENTION_CHOICES:
            retention_menu.add_radiobutton(
                label=f"{days} 天", variable=self.retention_var, value=days,
                command=self._on_retention_change,
            )
        self.menu.add_cascade(label="数据保留", menu=retention_menu)

        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        self.menu.add_checkbutton(
            label="开机自启", variable=self.autostart_var,
            command=self._on_autostart_change,
        )

        self.menu.add_separator()
        self.menu.add_command(label="重置位置", command=self.reset_position)
        self.menu.add_command(label="退出", command=self.quit)

        # 菜单弹出期间暂停自动隐藏（含子菜单）
        self._all_menus = (self.menu, interval_menu, alpha_menu, retention_menu)

    @staticmethod
    def _style_menu(menu):
        menu.configure(bg=MENU_BG, fg=TEXT_COLOR,
                       activebackground=MENU_ACTIVE, activeforeground="#ffffff",
                       relief="flat", borderwidth=0)

    def _bind_events(self):
        r = self.root
        r.bind_all("<ButtonPress-1>", self._on_press)
        r.bind_all("<B1-Motion>", self._on_motion)
        r.bind_all("<ButtonRelease-1>", self._on_release)
        r.bind_all("<Double-Button-1>", self._on_double_click)
        r.bind_all("<Button-3>", self._on_right_click)
        r.protocol("WM_DELETE_WINDOW", self.quit)

    # ---------------- 样式 / 位置 ----------------
    def _hwnd(self):
        hwnd = user32.GetParent(self.root.winfo_id())
        return hwnd or self.root.winfo_id()

    def _apply_style(self):
        hwnd = self._hwnd()
        try:
            get_long = user32.GetWindowLongPtrW
            set_long = user32.SetWindowLongPtrW
        except AttributeError:
            get_long = user32.GetWindowLongW
            set_long = user32.SetWindowLongW
        get_long.restype = ctypes.c_ssize_t
        style = get_long(hwnd, GWL_EXSTYLE)
        set_long(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)
        # Win11 圆角 + 描边（Win10 上调用失败也无妨）
        try:
            pref = ctypes.c_int(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(pref), ctypes.sizeof(pref),
            )
            border = ctypes.c_int(0x003A312C)  # COLORREF(#2c313a)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(border), ctypes.sizeof(border),
            )
        except OSError:
            pass

    def _apply_position(self):
        x, y = self.cfg["x"], self.cfg["y"]
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        if x is None or y is None:
            x = sw - self.win_w - 24
            y = 24
        x = max(0, min(int(x), sw - 40))
        y = max(0, min(int(y), sh - 20))
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

    def reset_position(self):
        self._dock_cancel_anim()
        self._hide_strip()
        self._dock_edge = None
        self._dock_state = "shown"
        self._dock_leave_since = None
        self.cfg["x"] = None
        self.cfg["y"] = None
        self._apply_position()
        save_config(self.cfg)

    # ---------------- 拖动 / 双击 ----------------
    def _on_press(self, event):
        # 只响应悬浮窗自身的按下（详情窗口/菜单上的按下不拖动悬浮窗）
        try:
            on_main = event.widget.winfo_toplevel() is self.root
        except (tk.TclError, AttributeError):
            on_main = False
        if not on_main:
            return
        # 按住停靠中的窗口（含隐藏细边）时先立即展开，再开始拖动
        if self._dock_edge is not None:
            self._dock_expand_instant()
            x, y = self._dock_shown
            self._drag = (event.x_root, event.y_root, x, y)
            return
        self._drag = (event.x_root, event.y_root,
                      self.root.winfo_x(), self.root.winfo_y())

    def _on_motion(self, event):
        if not self._drag:
            return
        sx, sy, ox, oy = self._drag
        x = ox + (event.x_root - sx)
        y = oy + (event.y_root - sy)
        self.root.geometry(f"+{x}+{y}")

    def _on_release(self, _event):
        if self._drag:
            self._drag = None
            x, y = self.root.winfo_x(), self.root.winfo_y()
            # 松手时靠近屏幕边缘则吸附停靠（_try_dock 内部保存配置）
            if not self._try_dock(x, y):
                self.cfg["x"] = x
                self.cfg["y"] = y
                save_config(self.cfg)

    def _on_double_click(self, _event):
        self.open_detail()

    def open_detail(self):
        if self.detail is not None:
            try:
                self.detail.win.lift()
                self.detail.win.focus_set()
                return
            except tk.TclError:
                self.detail = None
        self.detail = DetailWindow(self)

    def get_proc_monitor(self):
        if self.proc_monitor is None:
            self.proc_monitor = ProcessNetMonitor()
        return self.proc_monitor

    # ---------------- 右键菜单 ----------------
    def _on_right_click(self, event):
        if self._dock_edge is not None:
            self._dock_expand_instant()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _on_interval_change(self):
        self.cfg["interval"] = self.interval_var.get()
        save_config(self.cfg)
        if self.detail is not None:
            try:
                self.detail.update_interval_text()
            except tk.TclError:
                self.detail = None
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.tick()

    def _on_alpha_change(self):
        self.cfg["alpha"] = self.alpha_var.get()
        self.root.attributes("-alpha", self.cfg["alpha"])
        save_config(self.cfg)

    def _on_autostart_change(self):
        set_autostart(self.autostart_var.get())

    def _on_retention_change(self):
        days = self.retention_var.get()
        self.cfg["retention_days"] = days
        save_config(self.cfg)
        self.recorder.set_retention(days)
        if self.detail is not None:
            try:
                self.detail.update_retention_text()
                self.detail.refresh_stats()
            except tk.TclError:
                self.detail = None

    # ---------------- 靠边自动隐藏 ----------------
    def _on_auto_hide_change(self):
        enabled = self.auto_hide_var.get()
        self.cfg["auto_hide"] = enabled
        save_config(self.cfg)
        if enabled:
            # 开启时若窗口恰好停在边缘，立即停靠
            self._try_dock(self.root.winfo_x(), self.root.winfo_y())
        else:
            self._dock_cancel_anim()
            if self._dock_edge is not None:
                self._dock_expand_instant()
            self._dock_edge = None
            self._dock_state = "shown"
            self._dock_leave_since = None

    def _try_dock(self, x, y):
        """判断位置是否靠近屏幕边缘；是则吸附并进入停靠状态。

        返回是否停靠。停靠后窗口贴边显示（shown），鼠标移开自动滑出
        屏幕（hidden），只留 _peek 像素细边供鼠标悬停唤出。
        """
        if not self.cfg["auto_hide"]:
            self._dock_edge = None
            return False
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        dist = {
            "left": x,
            "right": sw - (x + self.win_w),
            "top": y,
            "bottom": sh - (y + self.win_h),
        }
        edge = min(dist, key=dist.get)
        if dist[edge] > self._dock_snap:
            self._dock_edge = None
            return False
        # 贴边，另一方向限制在屏幕内
        if edge in ("left", "right"):
            y = max(0, min(int(y), sh - self.win_h))
            if edge == "left":
                shown = (0, y)
                hidden = (-(self.win_w - self._peek), y)
            else:
                shown = (sw - self.win_w, y)
                hidden = (sw - self._peek, y)
        else:
            x = max(0, min(int(x), sw - self.win_w))
            if edge == "top":
                shown = (x, 0)
                hidden = (x, -(self.win_h - self._peek))
            else:
                shown = (x, sh - self.win_h)
                hidden = (x, sh - self._peek)
        self._dock_cancel_anim()
        self._hide_strip()
        self._dock_edge = edge
        self._dock_shown = shown
        self._dock_hidden = hidden
        self._dock_state = "shown"
        self._dock_leave_since = None
        self.root.geometry(f"+{shown[0]}+{shown[1]}")
        self.cfg["x"], self.cfg["y"] = shown
        save_config(self.cfg)
        return True

    def _dock_expand_instant(self):
        """停靠中立即展开到完整可见位置（按下/右键时用，不做动画）。"""
        self._dock_cancel_anim()
        self._hide_strip()
        x, y = self._dock_shown
        self.root.geometry(f"+{x}+{y}")
        self._dock_state = "shown"
        self._dock_leave_since = None

    def _dock_cancel_anim(self):
        if self._dock_anim_id is not None:
            try:
                self.root.after_cancel(self._dock_anim_id)
            except ValueError:
                pass
            self._dock_anim_id = None

    def _dock_slide(self, hide):
        """带缓动的滑入/滑出动画；动画期间巡检暂停。"""
        self._dock_cancel_anim()
        if not hide:
            self._hide_strip()  # 滑入时立即恢复常规内容
        sx, sy = self.root.winfo_x(), self.root.winfo_y()
        tx, ty = self._dock_hidden if hide else self._dock_shown
        target_state = "hidden" if hide else "shown"
        if (sx, sy) == (tx, ty):
            self._dock_state = target_state
            if hide:
                self._show_strip()
            return
        start = time.perf_counter()
        dur = DOCK_SLIDE_MS / 1000.0

        def step():
            t = (time.perf_counter() - start) / dur
            if t >= 1.0:
                self.root.geometry(f"+{tx}+{ty}")
                self._dock_anim_id = None
                self._dock_state = target_state
                self._dock_leave_since = None
                if hide:
                    self._show_strip()
                return
            p = 1.0 - (1.0 - t) ** 3  # ease-out
            nx = round(sx + (tx - sx) * p)
            ny = round(sy + (ty - sy) * p)
            self.root.geometry(f"+{nx}+{ny}")
            self._dock_anim_id = self.root.after(12, step)

        step()

    def _dock_poll(self):
        """低频巡检鼠标位置，驱动 隐藏 <-> 展开 状态切换。

        用巡检而非 <Enter>/<Leave>：窗口滑动到静止鼠标下方、
        子控件间移动等场景不会产生 Enter/Leave 事件，巡检更可靠。
        """
        self._dock_poll_id = self.root.after(DOCK_POLL_MS, self._dock_poll)
        if (self._dock_edge is None or self._drag is not None
                or self._dock_anim_id is not None):
            return
        try:
            if any(m.winfo_ismapped() for m in self._all_menus):
                self._dock_leave_since = None
                return
            px, py = self.root.winfo_pointerxy()
            x, y = self.root.winfo_x(), self.root.winfo_y()
        except tk.TclError:
            return
        inside = (x <= px < x + self.win_w and y <= py < y + self.win_h)
        now = time.perf_counter()
        if self._dock_state == "shown":
            if inside:
                self._dock_leave_since = None
            elif self._dock_leave_since is None:
                self._dock_leave_since = now
            elif now - self._dock_leave_since >= DOCK_HIDE_DELAY:
                self._dock_slide(hide=True)
        elif self._dock_state == "hidden" and inside:
            self._dock_slide(hide=False)

    # ---------------- 定时刷新 ----------------
    def tick(self):
        now = time.perf_counter()
        samples = read_adapters()
        if samples is not None:
            rows = []
            deltas = []
            total_down = total_up = 0.0
            if self._last is not None:
                dt = now - self._last_time
                if dt > 0:
                    for ifindex, name, rx, tx in samples:
                        prev = self._last.get(ifindex)
                        if prev is not None:
                            drx = rx - prev[0]
                            dtx = tx - prev[1]
                            if drx < 0 or dtx < 0:
                                # 网卡重置 / 接口变化：这一拍按 0 处理
                                drx = dtx = 0
                            down, up = drx / dt, dtx / dt
                        else:
                            drx = dtx = 0
                            down = up = 0.0
                        rows.append((ifindex, name, down, up))
                        deltas.append((ifindex, name, drx, dtx))
                        total_down += down
                        total_up += up
                    self._update_labels(total_down, total_up)
                    self.recorder.add_deltas(time.time(), deltas)
            else:
                rows = [(i, name, 0.0, 0.0) for i, name, _rx, _tx in samples]
            self.latest_rows = rows
            if self.detail is not None:
                try:
                    self.detail.update_speeds(rows)
                    self.detail.refresh_stats()
                    # 仅当详情窗口停在「进程网速」标签页时才采样进程
                    if self.detail.proc_tab_active():
                        self.detail.update_processes(
                            self.get_proc_monitor().sample())
                except tk.TclError:
                    self.detail = None
            self._last = {i: (rx, tx) for i, _n, rx, tx in samples}
            self._last_time = now
        self._after_id = self.root.after(
            int(self.cfg["interval"] * 1000), self.tick)

    def _update_labels(self, down_bps, up_bps):
        num, unit = format_speed(down_bps)
        self.down_num_var.set(num)
        self.down_unit_var.set(unit.strip())
        num, unit = format_speed(up_bps)
        self.up_num_var.set(num)
        self.up_unit_var.set(unit.strip())
        self._last_down, self._last_up = down_bps, up_bps
        self._update_strip(down_bps, up_bps)

    # ---------------- 退出 ----------------
    def quit(self):
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except ValueError:
                pass
            self._after_id = None
        if self._dock_poll_id is not None:
            try:
                self.root.after_cancel(self._dock_poll_id)
            except ValueError:
                pass
            self._dock_poll_id = None
        self._dock_cancel_anim()
        self.recorder.close()
        # 停靠中保存展开位置，避免下次启动恢复到屏幕外
        if self._dock_edge is not None:
            x, y = self._dock_shown
        else:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        self.cfg["x"] = x
        self.cfg["y"] = y
        save_config(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------- 入口
def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor v2
    except (OSError, AttributeError):
        try:
            user32.SetProcessDPIAware()
        except OSError:
            pass


def main():
    # 单实例
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        dpi = user32.GetDpiForSystem()
        if dpi:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except (OSError, AttributeError, tk.TclError):
        pass
    app = NetSpeedWindow(root)
    root.mainloop()
    if mutex:
        kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    main()