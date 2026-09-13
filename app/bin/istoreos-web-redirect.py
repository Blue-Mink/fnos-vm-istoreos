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
"""
import http.client
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

VM_NAME = os.environ.get("ISTO_VM_NAME", "istoreos")
PORT = int(os.environ.get("ISTO_WEB_PORT", "36125"))
TARGET_PORT = int(os.environ.get("ISTO_TARGET_PORT", "80"))
MDNS_NAMES = os.environ.get("ISTO_MDNS_NAMES", "istoreos.local openwrt.local").split()
CACHE_TTL = 20
SWEEP_COOLDOWN = 45
FP_MARKERS = (b"istoreos", b"istore", b"luci")

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


def resolve_ip():
    now = time.time()
    with _lock:
        # 缓存命中：TCP 快速验证后直接采用
        if (_cache["ip"] and now - _cache["ts"] < CACHE_TTL
                and vm_running()
                and mac_ok_for(_cache["ip"])
                and tcp_open(_cache["ip"], TARGET_PORT)):
            return _cache["ip"], _cache["source"]
        if not vm_running():
            _cache["ip"] = None
            return None, "stopped"
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
        if ip and not tcp_open(ip, TARGET_PORT):
            # ARP 表/DHCP 租约里的地址可能是上一次开机留下的，
            # 虚拟机还在起 Web 服务时跳过去就是 ERR_ADDRESS_UNREACHABLE，
            # 因此端口没通就当没找到，让入口页继续 5 秒轮询。
            ip = None
        if ip:
            _cache.update(ip=ip, mac=mac, ts=now, source=source)
        else:
            _cache["ip"] = None
        return ip, source


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
</style></head><body><div class="c">$BODY</div></body></html>"""


STATE_FILE = "/tmp/istoreos-install.state"


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

    def do_GET(self):
        path = self.path
        pure = path.split("?")[0]
        if pure in ("/healthz", "/api/healthz"):
            self._send(200, b"ok", "text/plain")
            return
        if pure == "/api/status":
            ip, source = resolve_ip()
            payload = json.dumps(
                {"running": bool(ip) or vm_running(), "ip": ip, "source": source,
                 "target_port": TARGET_PORT, "install_state": install_state()},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
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
        ip, _src = resolve_ip()
        if ip:
            tail = pure or "/"
            if "?" in path:
                tail += "?" + path.split("?", 1)[1]
            self.send_response(302)
            self.send_header("Location", f"http://{ip}:{TARGET_PORT}{tail}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        st = install_state()
        if st and st != "ready":
            self._send(200, self._page(
                "正在准备",
                f"首次安装约需 3~8 分钟<br><code>{st}</code>", refresh=5, dot="wait"))
            return
        if not vm_running():
            if vm_defined():
                btn = ('<form method="POST" action="/power/start">'
                       '<button type="submit">启动虚拟机</button></form>')
                self._send(200, self._page("iStoreOS 已关机", btn, refresh=5))
            else:
                self._send(200, self._page(
                    "准备中", "正在等待应用完成安装。", refresh=5, dot="wait"))
        else:
            self._send(200, self._page(
                "正在启动", "iStoreOS 就绪后会自动打开。", refresh=5, dot="wait"))

    def do_POST(self):
        pure = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        except ValueError:
            pass
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
        self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        pass


def main():
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    print(f"[istoreos-web] {PORT} finder -> vm '{VM_NAME}' :{TARGET_PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
