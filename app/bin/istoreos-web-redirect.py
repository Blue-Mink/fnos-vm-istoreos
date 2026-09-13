#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iStoreOS IP 寻踪转发器

监听 NAS 固定端口（默认 36125），为应用中心/桌面图标提供固定入口：
  · GET /...        → 302 到 http://<VM_IP>:80/...（iStoreOS 管理界面）
  · GET /power/start→ 302 回本站 "/"（开机接口只认 POST，见 do_GET 注释）
  · POST /power/start→ 虚拟机关机页的一键开机，回页每 5 秒刷新回 "/"
  · GET /api/status → JSON 状态

跳转前提：解析到的地址必须 TCP:80 探活通过，否则留在寻踪页继续轮询，
避免 ARP 残留/DHCP 旧租约把浏览器甩到一个不通的地址。

虚拟机 IP 多级发现链（找到即缓存 20s，缓存命中先做 TCP 快速验证）：
  1. libvirt XML 的 MAC → /proc/net/arp 反查（最准）
  2. mDNS：avahi-resolve istoreos.local / openwrt.local，TCP:80 验证
  3. virsh domifaddr --source arp/lease/agent
  4. 对本机 LAN 子网做并行 ping 扫描逼出 ARP，再按 MAC 反查
  5. HTTP 指纹补齐：ARP 表活跃主机的 :80 抓取首页，含
     iStoreOS/iStore/Luci 特征即认定为目标（排除 NAS 自身与其他设备）

发现链全空（现实中真实存在：链路通、IPv6 SLAAC 都正常，但 LAN 上没有
任何 DHCPv4 服务器应答，虚拟机就是拿不到 IPv4）时，入口页不再只是干等，
提供三件事，全部经 libvirt 串口在虚拟机里执行，不依赖网络：
  · POST /net/dhcp      → ifup lan + 请 netifd 自己的租约客户端续租（绝不在
                          netifd 托管的口上手跑 udhcpc，见 guest_dhcp_retry 注释）
  · POST /net/restart   → 重启虚拟机网络（治地址在、路由没了的状态错乱）
  · POST /net/static    → 给 LAN 口设静态 IPv4（含恢复自动获取）
  · POST /net/manual    → 只记一个手动跳转地址（虚拟机在别的网段时用）
"""
import http.client
import http.server
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

VM_NAME = os.environ.get("ISTO_VM_NAME", "istoreos")
PORT = int(os.environ.get("ISTO_WEB_PORT", "36125"))
TARGET_PORT = int(os.environ.get("ISTO_TARGET_PORT", "80"))
MDNS_NAMES = os.environ.get("ISTO_MDNS_NAMES", "istoreos.local openwrt.local").split()
CACHE_TTL = 20
SWEEP_COOLDOWN = 45
# 与 cmd/main 共用的两个状态文件：开机请求登记 / 用户主动停用标记
START_REQ = "/tmp/istoreos-vm-start.req"
STOP_MARK = "/tmp/istoreos-user-stopped"
FP_MARKERS = (b"istoreos", b"istore", b"luci")
# 入口页手动记下的跳转地址（放在应用共享目录，重装不丢）：
# 寻踪五层全空时的兜底，比如虚拟机被挪到别的网段。
MANUAL_IP_FILE = "/vol1/@appshare/istoreos/manual-ip"
GUEST_LAN_DEV = "br-lan"

_lock = threading.Lock()
_cache = {"ip": None, "mac": None, "ts": 0.0, "sweep_ts": 0.0, "source": None}


def _sh(cmd, timeout=10):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


def tcp_open(ip, port, timeout=0.5):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def vm_running():
    return "running" in _sh(f"virsh -c qemu:///system domstate {VM_NAME}", 8)


def vm_defined():
    return bool(_sh(f"virsh -c qemu:///system dominfo {VM_NAME}", 8).strip())


def vm_power_on():
    """一键/自动开机：shut off→start，paused→resume，running→幂等。返回状态描述。"""
    # 登记开机请求：cmd/main 的关机守望进程据此放弃强制断电（并在关机落定后补开）；
    # 同时撤销「用户停用」标记，否则应用中心会把这个窗口误判成未运行/异常。
    try:
        with open(START_REQ, "w"):
            pass
    except OSError:
        pass
    try:
        os.remove(STOP_MARK)
    except OSError:
        pass
    st = _sh(f"virsh -c qemu:///system domstate {VM_NAME}", 8)
    if "running" in st:
        return "already-running"
    if not vm_defined():
        return "undefined"
    if "paused" in st:
        _sh(f"virsh -c qemu:///system resume {VM_NAME}", 15)
        return "resumed"
    _sh(f"virsh -c qemu:///system start {VM_NAME}", 20)
    return "starting"


def vm_mac():
    xml = _sh(f"virsh -c qemu:///system dumpxml {VM_NAME}", 8)
    m = re.search(r"<mac address='([0-9a-f:]{17})'", xml)
    if m:
        return m.group(1)
    m = re.search(r"52:54:(?:[0-9a-f]{2}:){3}[0-9a-f]{2}", xml)
    return m.group(0) if m else None


def arp_lookup(mac):
    if not mac:
        return None
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3].lower() == mac.lower():
                    return parts[0]
    except OSError:
        pass
    return None


def arp_entries():
    ips = []
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] != "IP":
                    ips.append(parts[0])
    except OSError:
        pass
    return ips


def local_ips():
    ips = set()
    for line in _sh("ip -4 -o addr show", 8).splitlines():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if m:
            ips.add(m.group(1))
    return ips


def domifaddr():
    for src in ("arp", "lease", "agent"):
        out = _sh(f"virsh -c qemu:///system domifaddr {VM_NAME} --source {src}", 8)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", out)
        if m:
            return m.group(1)
    return None


def arp_mac_of(ip):
    """ARP 表中该 IP 对应的 MAC（无记录返回 None）。"""
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    return parts[3].lower()
    except OSError:
        pass
    return None


def mac_ok_for(ip):
    """ARP 守门：若 ARP 表已明确记录该 IP 属于别的 MAC，则否决该候选
    （防止 mDNS/HTTP 指纹误中局域网内其它 OpenWrt/iStore 设备）。"""
    vm = vm_mac()
    if not vm:
        return True
    m = arp_mac_of(ip)
    return m is None or m == vm.lower()


def http_fingerprint(ip, port=TARGET_PORT, timeout=1.2):
    """抓首页头部，确认是 iStoreOS/LuCI 页面。"""
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        conn.request("GET", "/", headers={"User-Agent": "isto-finder/1.0"})
        r = conn.getresponse()
        data = r.read(8192).lower()
        conn.close()
        return any(mk in data for mk in FP_MARKERS)
    except Exception:
        return False


def mdns_probe():
    if not shutil.which("avahi-resolve"):
        return None
    for name in MDNS_NAMES:
        out = _sh(f"avahi-resolve -4 -n {name}", 8)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
        if not m:
            continue
        ip = m.group(1)
        if ip in local_ips():
            continue
        if not mac_ok_for(ip):
            continue
        if tcp_open(ip, TARGET_PORT, 0.6):
            return ip
    return None


def ping_sweep():
    """对物理/LAN 接口的子网做快速 ping 扫描，逼出 ARP 表。
    跳过 docker/virbr 等虚拟网桥。"""
    hosts = []
    out = _sh("ip -4 -o addr show scope global", 8)
    for line in out.splitlines():
        if re.search(r"\b(docker|br-|virbr|veth|vnet|tun|tap|lo)\S*", line):
            continue
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not m:
            continue
        try:
            net = ipaddress.ip_interface(m.group(1)).network
        except ValueError:
            continue
        if net.prefixlen <= 24:
            hosts.extend(str(h) for h in list(net.hosts())[:1024])
    if not hosts:
        return []
    def p(ip):
        try:
            subprocess.run(["ping", "-c", "1", "-W", "1", "-q", ip],
                           capture_output=True, timeout=3)
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(p, hosts))
    return hosts


def httpfp_fallback():
    """ARP 表活跃主机里用 HTTP 指纹找 iStoreOS。"""
    skip = local_ips()
    cands = [ip for ip in arp_entries() if ip not in skip]
    def probe(ip):
        if not mac_ok_for(ip):
            return None
        return ip if tcp_open(ip, TARGET_PORT, 0.4) and http_fingerprint(ip) else None
    hits = []
    with ThreadPoolExecutor(max_workers=48) as ex:
        for r in ex.map(probe, cands):
            if r:
                hits.append(r)
    return hits[0] if hits else None


# ---- 寻踪之外的兜底：手动跳转地址 + 经串口在虚拟机里修网络 ----

def valid_v4(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def manual_ip():
    """入口页手动记下的跳转地址；无效或未设置返回 None。"""
    try:
        with open(MANUAL_IP_FILE) as f:
            v = f.readline().strip()
    except OSError:
        return None
    return v if valid_v4(v) else None


def save_manual_ip(v):
    """空串表示清除，返回生效后的值（None 或地址）。"""
    try:
        os.makedirs(os.path.dirname(MANUAL_IP_FILE), exist_ok=True)
        if v:
            with open(MANUAL_IP_FILE, "w") as f:
                f.write(v + "\n")
        elif os.path.exists(MANUAL_IP_FILE):
            os.remove(MANUAL_IP_FILE)
    except OSError:
        return None
    return v or None


def guest_run(cmds, per=4, tail=900):
    """通过 libvirt 串口在虚拟机里执行命令——没有 IP 时这是唯一入口。

    用 script(1) 造一个 pty 挂住 virsh console，逐条喂命令，把屏幕内容读回来。
    命令只在本文件里写死；外部传入的东西必须先过 valid_v4 才拼得进来。
    """
    lines = ["sleep 2", "printf '\\n'", "sleep 1"]
    for c in cmds:
        lines.append("printf '%s\\n' " + shlex.quote(c))
        lines.append("sleep %d" % per)
    lines.append("sleep 3")
    total = 3 + per * len(cmds) + 20
    pipeline = ("{ " + "; ".join(lines) + "; } | timeout " + str(total) +
                " script -qec 'virsh -c qemu:///system console " + VM_NAME + "' /dev/null")
    try:
        p = subprocess.run(["bash", "-c", pipeline], capture_output=True,
                           text=True, timeout=total + 25)
    except Exception as exc:
        return "串口执行失败：" + str(exc)
    return ((p.stdout or "") + (p.stderr or ""))[-tail:]


def guest_dhcp_retry():
    """让 LAN 口重新走一遍 DHCP。

    千万别在这里手跑 `udhcpc -n -t 6 -i br-lan`：那是 netifd 托管的接口，
    第二个客户端退出时会给脚本发 deconfig，把地址和路由一起冲掉——实测
    静态地址口被这么搞一次就 `ping: Network unreachable`，虚拟机整个失联
    （只能 /etc/init.d/network restart 救回来）。所以只让 netifd 自己重来，
    再顺带请它已有的租约客户端续一次约（静态口没这个对象，报错也无妨）。
    """
    return guest_run(["ifup lan",
                      "ubus call network.interface.lan udhcpc renew 2>/dev/null || true",
                      "ip -4 a show " + GUEST_LAN_DEV,
                      "ip route"], per=7)


def guest_net_restart():
    """整机网络重启：治 netifd 状态错乱（地址在、路由没了这类）。"""
    return guest_run(["/etc/init.d/network restart",
                      "sleep 8",
                      "ip -4 a show " + GUEST_LAN_DEV,
                      "ip route"], per=9)


def guest_net_mode(mode, ip="", prefix="24", gw="", dns=""):
    """在虚拟机里把 LAN 口设为静态地址或恢复自动获取；参数非法返回 None。"""
    if mode == "static":
        try:
            mask = str(ipaddress.IPv4Network(ip + "/" + prefix, strict=False).netmask)
        except ValueError:
            return None
        cmds = ["uci set network.lan.proto='static'",
                "uci set network.lan.ipaddr='" + ip + "'",
                "uci set network.lan.netmask='" + mask + "'"]
        if gw:
            cmds.append("uci set network.lan.gateway='" + gw + "'")
        if dns:
            cmds.append("uci set network.lan.dns='" + dns + "'")
    else:
        cmds = ["uci set network.lan.proto='dhcp'",
                "uci -q del network.lan.ipaddr",
                "uci -q del network.lan.netmask",
                "uci -q del network.lan.gateway",
                "uci -q del network.lan.dns"]
    cmds += ["uci commit network", "ifup lan", "ip -4 a show " + GUEST_LAN_DEV]
    return guest_run(cmds, per=4)


# 串口操作要几十秒，HTTP 请求不能干等：放后台线程跑，入口页 3 秒一轮看进度
_net_lock = threading.Lock()
_net_job = {"running": False, "note": "", "out": "", "ts": 0.0}


def start_net_job(kind, args=(), note=""):
    with _net_lock:
        if _net_job["running"]:
            return False
        if not vm_running():
            _net_job.update(running=False, note="虚拟机没在运行，串口进不去", out="",
                            ts=time.time())
            return False
        _net_job.update(running=True, note=note, out="", ts=time.time())

    def worker():
        try:
            if kind == "dhcp":
                out = guest_dhcp_retry()
            elif kind == "dhcp-mode":
                out = guest_net_mode("dhcp")
            elif kind == "net-restart":
                out = guest_net_restart()
            else:
                out = guest_net_mode("static", *args)
        except Exception as exc:
            out = "执行异常：" + str(exc)
        with _net_lock:
            _net_job.update(running=False, note=note, out=(out or "")[-1200:])

    threading.Thread(target=worker, daemon=True).start()
    return True


def resolve_ip():
    """找虚拟机地址。返回 dict，不只是"有没有 IP"：
      ip      探活通过、可以直接跳过去的地址（没有就 None）
      source  发现层（mac+arp / mdns / domifaddr / sweep+arp / httpfp）
      stage   ok / web-warming（找到地址但 80 还没通）/ no-ip（开好了没地址）/ stopped
      probing 探活没通过的那个候选地址，纯给排障看
      mac     虚拟机网卡 MAC，排障用
    以前只回 (ip, source)，于是「没开机」「开好了但没 IP」「有 IP 但 Web 没起」
    三种完全不同的情况在入口页都是同一句"正在启动"，出问题只能靠串口。
    """
    now = time.time()
    with _lock:
        # 手动指定过地址就优先用它（仍要探活），五层发现链救不了的场景兜底
        man = manual_ip()
        if man and tcp_open(man, TARGET_PORT):
            return {"ip": man, "source": "manual", "stage": "ok",
                    "probing": None, "mac": vm_mac()}
        if not vm_running():
            _cache["ip"] = None
            return {"ip": None, "source": None, "stage": "stopped",
                    "probing": None, "mac": vm_mac()}
        # 缓存命中：TCP 快速验证后直接采用
        if (_cache["ip"] and now - _cache["ts"] < CACHE_TTL
                and mac_ok_for(_cache["ip"])
                and tcp_open(_cache["ip"], TARGET_PORT)):
            return {"ip": _cache["ip"], "source": _cache["source"], "stage": "ok",
                    "probing": None, "mac": _cache["mac"]}
        mac = vm_mac()
        # 1) MAC → ARP
        ip = arp_lookup(mac)
        source = "mac+arp" if ip else None
        # 2) mDNS
        if not ip:
            ip = mdns_probe()
            source = "mdns" if ip else None
        # 3) virsh domifaddr
        if not ip:
            ip = domifaddr()
            source = "domifaddr" if ip else None
        # 4) ping 扫描后再查 ARP
        if not ip and now - _cache["sweep_ts"] > SWEEP_COOLDOWN:
            _cache["sweep_ts"] = now
            ping_sweep()
            ip = arp_lookup(mac)
            if ip:
                source = "sweep+arp"
        # 5) HTTP 指纹补齐
        if not ip:
            ip = httpfp_fallback()
            source = "httpfp" if ip else None
        probing = None
        if ip and not tcp_open(ip, TARGET_PORT):
            # ARP 表/DHCP 租约里的地址可能是上一次开机留下的，
            # 虚拟机还在起 Web 服务时跳过去就是 ERR_ADDRESS_UNREACHABLE，
            # 因此端口没通就当没找到，让入口页继续 5 秒轮询。
            # 注意：地址作废时 source 也得一起作废，否则 /api/status 会出现
            # {"ip": null, "source": "mac+arp"} 这种自相矛盾的排障线索。
            probing, ip, source = ip, None, None
        if ip:
            _cache.update(ip=ip, mac=mac, ts=now, source=source)
            return {"ip": ip, "source": source, "stage": "ok",
                    "probing": None, "mac": mac}
        _cache["ip"] = None
        return {"ip": None, "source": None, "stage": "web-warming" if probing else "no-ip",
                "probing": probing, "mac": mac}


PAGE_TMPL = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">\
<meta name="viewport" content="width=device-width,initial-scale=1">\
$REFRESH<title>iStoreOS</title><style>\
*{box-sizing:border-box}body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;\
background:#0b1117;color:#e6edf3;display:flex;align-items:center;justify-content:center;\
min-height:100vh;margin:0;-webkit-font-smoothing:antialiased}\
.c{text-align:center;max-width:420px;padding:24px}\
.dot{width:10px;height:10px;border-radius:50%;background:#4b5b6b;display:block;margin:0 auto 22px}\
.dot.wait{background:#e8a03a;animation:br 1.6s ease-in-out infinite}\
@keyframes br{50%{opacity:.35}}\
h1{font-size:20px;font-weight:600;margin:0 0 10px;letter-spacing:.5px}\
p{color:#8296a8;font-size:14px;margin:0 0 24px;line-height:1.7}\
code{font-size:12px;color:#5f7385;background:#131c26;padding:2px 8px;border-radius:6px}\
button{font-size:15px;padding:11px 34px;border-radius:999px;border:0;background:#2563eb;\
color:#fff;cursor:pointer;letter-spacing:1px}button:hover{background:#3b76f0}\
button.alt{background:#1d2b38;color:#a9bccd}a{color:#4f8df9;text-decoration:none}\
.sec{text-align:left;font-size:13px;color:#5f7385;margin:20px 0 10px;\
border-top:1px solid #16222e;padding-top:14px}\
form{margin:0 0 12px}input,select{font-size:14px;padding:10px 12px;border-radius:10px;\
border:1px solid #223140;background:#101a23;color:#e6edf3;margin:0 6px 10px 0}\
pre{font-size:12px;color:#8296a8;background:#101a23;padding:10px 12px;border-radius:10px;\
text-align:left;overflow:auto;white-space:pre-wrap}\
</style></head><body><div class="c">$BODY</div></body></html>"""


STATE_FILE = "/tmp/istoreos-install.state"
DISK_MARK_FILE = "/vol1/vm/istoreos.disk-version"


def os_version():
    """磁盘里的 iStoreOS 版本：装机时写的标记，退回虚拟机定义里的 osVersion。"""
    try:
        with open(DISK_MARK_FILE) as f:
            v = f.readline().strip()
            if v:
                return v
    except OSError:
        pass
    xml = _sh(f"virsh -c qemu:///system dumpxml {VM_NAME}", 8)
    m = re.search(r"<osVersion[^>]*>iStoreOS ([0-9.]+)", xml)
    return m.group(1) if m else None


def install_state():
    """后台安装进度（install_callback 秒回模式写入）；无文件返回 None。"""
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()[:200] or None
    except OSError:
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "IstoFinder/1.0"

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _page(self, title, msg, refresh=0, dot=""):
        rf = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        body = f'<span class="dot {dot}"></span><h1>{title}</h1><p>{msg}</p>'
        html = PAGE_TMPL.replace("$REFRESH", rf).replace("$BODY", body)
        html = html.replace("$PORT", str(PORT)).replace("$TARGET", str(TARGET_PORT))
        return html.encode("utf-8")

    def _raw(self, body, refresh=0):
        rf = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        html = PAGE_TMPL.replace("$REFRESH", rf).replace("$BODY", body)
        return html.encode("utf-8")

    def _net_page(self, note=""):
        """网络修复页：不自动刷新（表单页刷新会把填一半的东西冲掉），
        只有串口任务在跑时才 3 秒轮询一次。"""
        st = resolve_ip()
        man = manual_ip() or ""
        with _net_lock:
            job = dict(_net_job)
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;")
        parts = ['<span class="dot"></span><h1>网络修复</h1>',
                 '<p>下面几步都是经 libvirt 串口直接在虚拟机里执行，'
                 '虚拟机没有 IP 也能用。</p>']
        if note:
            parts.append(f'<p><code>{esc(note)}</code></p>')
        if job["running"]:
            parts.append(f'<p>串口正在执行：{esc(job["note"])}'
                         f'<br>约需一分钟，页面会自动刷新。</p>')
        parts.append('<form method="post" action="/net/dhcp">'
                     '<button type="submit">重新获取 IP（重试 DHCP）</button></form>')
        parts.append('<div class="sec">地址在、路由却没了之类的状态错乱</div>')
        parts.append('<form method="post" action="/net/restart">'
                     '<button class="alt" type="submit">重启虚拟机网络</button></form>')
        parts.append('<div class="sec">路由器不发地址时：给虚拟机指定静态地址</div>')
        parts.append('<form method="post" action="/net/static">'
                     '<input name="ip" placeholder="IP 192.168.3.72" inputmode="decimal">'
                     '<select name="prefix"><option value="24">/24</option>'
                     '<option value="16">/16</option><option value="8">/8</option></select>'
                     '<input name="gw" placeholder="网关 192.168.3.1">'
                     '<input name="dns" placeholder="DNS（可留空）">'
                     '<button type="submit">应用</button></form>')
        parts.append('<form method="post" action="/net/dhcp-mode">'
                     '<button class="alt" type="submit">恢复自动获取</button></form>')
        parts.append('<div class="sec">虚拟机在别的网段时：手动指定跳转地址</div>')
        parts.append('<form method="post" action="/net/manual">'
                     f'<input name="ip" placeholder="{man or "已知地址 192.168.1.1"}"'
                     ' inputmode="decimal">'
                     '<button type="submit">保存</button></form>')
        if man:
            parts.append('<form method="post" action="/net/manual">'
                         '<button class="alt" type="submit" name="clear" value="1">'
                         '清除手动地址</button></form>')
        parts.append('<div class="sec">当前</div>')
        parts.append('<p>阶段 <code>{}</code><br>网卡 <code>{}</code><br>系统 <code>{}</code>'
                     '{}<br><a href="/">← 返回入口</a></p>'.format(
                         st["stage"], st["mac"] or "未知", os_version() or "未知",
                         f'<br>手动地址 <code>{man}</code>' if man else ""))
        if not job["running"] and job["out"]:
            parts.append(f'<pre>{esc(job["out"])}</pre>')
        return self._raw("".join(parts), refresh=3 if job["running"] else 0)

    def do_GET(self):
        path = self.path
        pure = path.split("?")[0]
        if pure in ("/healthz", "/api/healthz"):
            self._send(200, b"ok", "text/plain")
            return
        if pure == "/api/status":
            r = resolve_ip()
            payload = json.dumps(
                # running 用同一份快照判断：早先是再调一次 vm_running()，
                # 与 stage 的来源不是同一次 virsh，出现过 stage=no-ip 而
                # running=false 的自相矛盾输出。
                {"running": r["stage"] != "stopped", "ip": r["ip"],
                 "source": r["source"], "stage": r["stage"], "probing": r["probing"],
                 "mac": r["mac"], "os_version": os_version(), "manual": manual_ip(),
                 "target_port": TARGET_PORT, "install_state": install_state()},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        if pure == "/net":
            self._send(200, self._net_page())
            return
        if pure == "/power/start":
            # 开机接口只认 POST。浏览器停在 /power/start 时刷新、后退或
            # meta refresh 都会走到这里，若交给下面的透传逻辑就会被原样
            # 镜像成 http://<VM_IP>/power/start（虚拟机上没这个页面）。
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        r = resolve_ip()
        if r["ip"]:
            tail = pure or "/"
            if "?" in path:
                tail += "?" + path.split("?", 1)[1]
            self.send_response(302)
            self.send_header("Location", f"http://{r['ip']}:{TARGET_PORT}{tail}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        st = install_state()
        if st and st != "ready":
            self._send(200, self._page(
                "正在准备",
                f"首次安装约需 3~8 分钟<br><code>{st}</code>", refresh=5, dot="wait"))
            return
        if not vm_defined():
            self._send(200, self._page(
                "准备中", "正在等待应用完成安装。", refresh=5, dot="wait"))
            return
        if r["stage"] == "stopped":
            if os.path.exists(STOP_MARK):
                # 用户在应用中心点过「停用」：那就别擅自开机，给回 1.1.2 那套
                # 简洁关机页 + 一键开机按钮（点了才开，开了自动跳）。
                btn = ('<form method="POST" action="/power/start">'
                       '<button type="submit">启动虚拟机</button></form>')
                self._send(200, self._page("iStoreOS 已关机", btn, refresh=5))
                return
            # 不是用户关的（应用重启、虚拟机里手动关机等）：打开入口就顺手补开机。
            # 应用中心的「启用」不会回调应用脚本（实测 cmd/main 只收到 stop）。
            vm_power_on()
            self._send(200, self._page(
                "正在启动", "iStoreOS 就绪后会自动打开。", refresh=5, dot="wait"))
            return
        if r["stage"] == "web-warming":
            self._send(200, self._page(
                "正在启动",
                f"已找到 <code>{r['probing']}</code>，iStoreOS 的 Web 服务还在起来。",
                refresh=5, dot="wait"))
            return
        # 虚拟机开着却始终没有可用地址：现实里真发生过——链路通、IPv6 都拿到了，
        # 但 LAN 上没有 DHCPv4 服务器应答。这时不能只让用户干等，给出串口修复入口。
        self._send(200, self._page(
            "正在获取地址",
            f"虚拟机已开机，iStoreOS 还没拿到 IPv4（网卡 <code>{r['mac'] or '未知'}</code>）。"
            f"<br>通常是路由器 DHCP 没发地址；等不下去可以"
            f"<a href=\"/net\">手动处理</a>（重试 DHCP / 设静态地址）。",
            refresh=5, dot="wait"))

    def do_POST(self):
        pure = self.path.split("?")[0]
        raw = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
        except ValueError:
            pass
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        field = lambda k: (form.get(k) or [""])[0].strip()

        if pure == "/power/start":
            res = vm_power_on()
            msg = {"starting": "iStoreOS 就绪后会自动打开。",
                   "resumed": "iStoreOS 就绪后会自动打开。",
                   "already-running": "iStoreOS 已在运行。",
                   "undefined": "正在等待应用完成安装。"}[res]
            # 刷新必须显式回到 "/"：只写 content="5" 会拿当前 URL
            # （/power/start）重新 GET，等于把控制路径跳给虚拟机。
            self._send(200, self._page("正在启动", msg, refresh="5;url=/", dot="wait"))
            return

        if pure == "/net/dhcp":
            ok = start_net_job("dhcp", note="ifup lan + 请 netifd 续租")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/dhcp-mode":
            ok = start_net_job("dhcp-mode", note="把 LAN 口改回自动获取")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/restart":
            ok = start_net_job("net-restart", note="重启虚拟机网络")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/static":
            ip, gw, dns, prefix = field("ip"), field("gw"), field("dns"), field("prefix") or "24"
            bad = [v for v in (ip, gw, dns) if v and not valid_v4(v)]
            if not valid_v4(ip) or bad or prefix not in ("8", "16", "24"):
                self._send(200, self._net_page("地址不合法：请填写正确的 IPv4（网关/DNS 可留空）"))
                return
            ok = start_net_job("static", args=(ip, prefix, gw, dns),
                               note=f"设静态地址 {ip}/{prefix}")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/manual":
            if field("clear") == "1":
                save_manual_ip("")
                self._send(200, self._net_page("已清除手动跳转地址。"))
                return
            v = field("ip")
            if v and not valid_v4(v):
                self._send(200, self._net_page("地址不合法：请填写正确的 IPv4"))
                return
            save_manual_ip(v)
            self._send(200, self._net_page(
                f"手动跳转地址已{'保存，探活通过就会直接跳过去' if v else '清除'}"
                f'{": " + v if v else ""}。'))
            return
        self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        pass

    def handle_error(self, request, client_address):
        """入口页每 5 秒轮询，浏览器经常提前掐线；这种 ConnectionResetError
        不是故障，别把它刷进 journal。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    print(f"[istoreos-web] {PORT} finder -> vm '{VM_NAME}' :{TARGET_PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
