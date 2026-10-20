#!/usr/bin/env python3
"""
steamfix - make the Steam client work on networks that throttle or break it.

Two independent halves, deliberately kept apart:

    * Unprivileged proxy (this process): a CONNECT proxy + PAC server on loopback
      that resolves Steam hosts over plain DNS-over-UDP, fails over between the
      returned IPs, keeps connections alive and splits the TLS ClientHello record
      header from the rest (DPI evasion). Start with ``serve()``, stop with ``stop()``.
    * Privileged network config (later run inside a root daemon): ``apply_network()``
      and ``restore_network()``. These only call ``networksetup`` with a validated
      argument list - never a shell - and save/restore the previous state in
      ``/var/tmp/saf-state.json``.

Importing this module has no side effects: it starts nothing, reads no argv,
and touches no system settings. Standard library only.

Ported from SteamAccessFix-macOS (MIT), which is our own repository; the DNS /
ClientHello / failover logic originates in vuviett47's SteamAccessFix upstream.
"""
import asyncio
import concurrent.futures
import ipaddress
import json
import os
import random
import re
import socket
import struct
import subprocess
import time

PORT = 31627                      # one port: CONNECT proxy + PAC (GET /proxy.pac)
DOMAINS = ["steampowered.com", "steamcommunity.com", "steamstatic.com", "steamusercontent.com",
           "steam-chat.com", "steamgames.com", "steamcontent.com", "steamserver.net",
           "counter-strike.net", "dota2.com", "valvesoftware.com"]
DNS_SETS = {"google": ["8.8.8.8", "8.8.4.4"], "cloudflare": ["1.1.1.1", "1.0.0.1"],
            "controld": ["76.76.2.0", "76.76.10.0"], "quad9": ["9.9.9.9", "149.112.112.112"],
            "opendns": ["208.67.222.222", "208.67.220.220"], "adguard": ["94.140.14.14", "94.140.15.15"],
            "nextdns": ["45.90.28.0", "45.90.30.0"], "alidns": ["223.5.5.5", "223.6.6.6"],
            "dnssb": ["185.222.222.222", "45.11.45.11"], "comodo": ["8.26.56.26", "8.20.247.20"]}
GAME_PROCS = ["steam_osx", "dota2", "cs2"]   # flush DNS cache when one starts
STATE = "/var/tmp/saf-state.json"
PAC_HOST = "127.0.0.1"
PAC_PATH = "/proxy.pac"
DNS_TTL = 900                      # upstream: 15 min cache, avoids mid-session drops
DNS_POLL_INTERVAL = 5              # seconds between game-process polls
CONNECT_TIMEOUT = 3.5              # per-IP connect budget during failover

_dns_cache = {}
_ACTIVE = None                    # ProxyServer started by serve(), for stop()

_PAC_RE = re.compile(r"http://127\.0\.0\.1:(\d{1,5})/proxy\.pac")
_HOST_RE = re.compile(r"[A-Za-z0-9._-]+")


# --------------------------------------------------------------------------- helpers

def sh(*args, timeout=20):
    """Run a read-only helper command; return its stdout (empty string on failure).

    Always a list, never a shell, so a service name cannot become shell syntax.
    """
    try:
        p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout or ""


def _networksetup(*args, check=False, timeout=30):
    """Run networksetup with already-validated argv. Never shell=True."""
    cmd = ["networksetup"] + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("networksetup %s failed: %s" % (args[0], (p.stderr or p.stdout).strip()))
    return p


# ------------------------------------------------------------------ input validation

def network_services():
    """Service names from `networksetup -listallnetworkservices` (disabled ones start with `*`)."""
    lines = sh("networksetup", "-listallnetworkservices").splitlines()[1:]
    return {l.strip() for l in lines if l.strip() and not l.startswith("*")}


def _check_svc(svc):
    # These strings end up as argv inside a root daemon: only names the system reports are allowed.
    if not isinstance(svc, str) or svc not in network_services():
        raise ValueError("svc is not a network service on this Mac: %r" % (svc,))
    return svc


def _check_dns_name(name):
    if not isinstance(name, str) or name not in DNS_SETS:
        raise ValueError("dns_name must be one of: %s" % ", ".join(DNS_SETS))
    return name


def _check_port(port, allow_zero=False):
    lo = 0 if allow_zero else 1
    if isinstance(port, bool) or not isinstance(port, int) or not lo <= port <= 65535:
        raise ValueError("port must be an int in %d..65535" % lo)
    return port


def _check_pac_url(url):
    if not isinstance(url, str):
        raise ValueError("pac_url must be a str")
    m = _PAC_RE.fullmatch(url)
    if not m or not 1 <= int(m.group(1)) <= 65535:
        raise ValueError('pac_url must be "http://127.0.0.1:<port>/proxy.pac"')
    return url


def _check_mtu(mtu):
    if mtu is None:
        return None
    if isinstance(mtu, bool) or not isinstance(mtu, int):
        raise ValueError("mtu must be an int in 576..1500")
    if not 576 <= mtu <= 1500:
        raise ValueError("mtu must be an int in 576..1500")
    return mtu


def _check_saved_url(url):
    """Whatever PAC URL was there before us: any http(s) URL without whitespace."""
    if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s]+", url):
        raise ValueError("saved pac_url is not an http(s) URL")
    return url


def _check_saved_mtu(mtu):
    if isinstance(mtu, bool) or not isinstance(mtu, int) or not 1 <= mtu <= 65535:
        raise ValueError("saved mtu is not a plausible MTU")
    return mtu


def _check_saved_dns(dns):
    """Saved DNS servers must be IP literals: this list becomes argv inside a root daemon."""
    if dns is None:
        return []
    if not isinstance(dns, list):
        raise ValueError("saved dns is not a list")
    out = []
    for entry in dns:
        if not isinstance(entry, str):
            raise ValueError("saved dns must contain strings")
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            raise ValueError("saved dns entry is not an IP address: %r" % entry) from None
        out.append(entry)
    return out


# ------------------------------------------------------- privileged: networksetup

def active_service():
    """Name of the network service currently carrying the default route."""
    m = re.search(r"interface: (\S+)", sh("route", "-n", "get", "default"))
    if not m:
        raise RuntimeError("no default route")
    dev = m.group(1)
    order = sh("networksetup", "-listnetworkserviceorder")
    order = order.replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(r"\(\d+\) ([^\n]+)\n\(Hardware Port: [^,]+, Device: " + re.escape(dev) + r"\)", order)
    if not m:
        raise RuntimeError("no network service for device %s" % dev)
    return m.group(1).strip().lstrip("*").strip()


def _get_dns(svc):
    """Current DNS servers for a service; [] means 'empty' (DHCP)."""
    out = sh("networksetup", "-getdnsservers", svc).strip()
    return [] if "aren't any" in out else out.split()


def _load_state(path=None):
    path = path or STATE
    if os.path.islink(path):
        raise ValueError("state file must not be a symlink")
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    if not isinstance(st, dict):
        raise ValueError("state file is not an object")
    return st


def save_state(svc):
    """Write the pre-change network state so restore_network() can undo it."""
    svc = _check_svc(svc)
    dns = sh("networksetup", "-getdnsservers", svc).strip()
    pac = sh("networksetup", "-getautoproxyurl", svc)
    url = re.search(r"URL: (\S+)", pac)
    on = re.search(r"Enabled: Yes", pac) is not None
    mtu = re.search(r"Current Setting: (\d+)", sh("networksetup", "-getMTU", svc))
    st = {"svc": svc,
          "keep_dns": False,   # kept for state-file compatibility with the original script
          "dns": [] if "aren't any" in dns else dns.split(),
          "pac_url": url.group(1) if url and url.group(1) != "(null)" else "",
          "pac_on": on,
          "mtu": int(mtu.group(1)) if mtu else 0}
    path = STATE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # /var/tmp is world-writable: never follow a symlink planted at the state path.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f)
    return st


def apply_network(svc, dns_name, pac_url, mtu=None):
    """Point ``svc`` at our PAC/proxy and DNS set. Saves state first.

    Every argument is validated before the first subprocess call, so this is safe
    to call from a root daemon with data that came from outside.
    """
    svc = _check_svc(svc)
    dns_name = _check_dns_name(dns_name)
    url = _check_pac_url(pac_url)
    mtu = _check_mtu(mtu)
    if os.path.lexists(STATE):  # a 2nd save_state would record OUR settings as the "original" ones
        raise RuntimeError("already applied (%s exists): run restore first" % STATE)
    save_state(svc)
    _networksetup("-setdnsservers", svc, *DNS_SETS[dns_name], check=True)
    flush_dns()
    if mtu:
        _networksetup("-setMTU", svc, mtu, check=True)
    _networksetup("-setautoproxyurl", svc, url, check=True)
    _networksetup("-setautoproxystate", svc, "on", check=True)
    return {"active": True, "svc": svc, "dns": list(DNS_SETS[dns_name]),
            "pac_url": url, "mtu": mtu or 0}


def restore_network():
    """Undo apply_network() using the saved state. Returns the restored service name.

    No state file means nothing to undo (None). A state file naming an unknown
    service raises and is left in place, so it can be inspected.
    """
    path = STATE
    if not os.path.exists(path):
        return None
    st = _load_state(path)
    svc = _check_svc(st.get("svc"))
    if not st.get("keep_dns"):
        _networksetup("-setdnsservers", svc, *(_check_saved_dns(st.get("dns")) or ["Empty"]))
    if st.get("pac_url"):
        _networksetup("-setautoproxyurl", svc, _check_saved_url(st["pac_url"]))
    _networksetup("-setautoproxystate", svc, "on" if st.get("pac_on") else "off")
    if st.get("mtu"):
        _networksetup("-setMTU", svc, _check_saved_mtu(st["mtu"]))
    flush_dns()
    os.remove(path)
    return svc


def status():
    """{'active': bool, 'svc': str|None, 'dns': [..]} - active means our state is applied."""
    active = False
    svc = None
    if os.path.exists(STATE):
        try:
            st = _load_state(STATE)
            svc_name = st.get("svc")
            if isinstance(svc_name, str) and svc_name in network_services():
                svc, active = svc_name, True
        except (OSError, ValueError):
            svc = None
    if svc is None:
        try:
            svc = active_service()
        except RuntimeError:
            return {"active": False, "svc": None, "dns": []}
    return {"active": active, "svc": svc, "dns": _get_dns(svc)}


def flush_dns():
    """Drop the system DNS cache (called after changing servers and on game start)."""
    subprocess.run(["dscacheutil", "-flushcache"], capture_output=True)
    subprocess.run(["killall", "-HUP", "mDNSResponder"], capture_output=True)


def find_mtu():
    """Largest MTU passing 'ping -D' (DF set) to 1.1.1.1; 0 when the probe fails."""
    def ok(n):
        return subprocess.run(["ping", "-D", "-s", str(n), "-c", "1", "-t", "1", "1.1.1.1"],
                              capture_output=True).returncode == 0
    lo, hi = 1172, 1472              # 1200..1500 MTU
    if not ok(lo):
        return 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid - 1)
    return lo + 28


# ----------------------------------------------------------------------- DNS over UDP

def dns_query(host, server, timeout=4):
    """Plain UDP A query; returns all IPv4 answers. Raises on timeout/parse error/no answer."""
    q = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0)
    for label in host.split("."):
        q += bytes([len(label)]) + label.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
        sk.settimeout(timeout)
        sk.sendto(q, (server, 53))
        r = sk.recv(512)
    i = 12
    while r[i]:
        i += r[i] + 1
    i += 5
    ips = []
    for _ in range(struct.unpack(">H", r[6:8])[0]):
        if r[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while r[i]:
                i += r[i] + 1
            i += 1
        typ, _c, _ttl, rdl = struct.unpack(">HHIH", r[i:i + 10])
        i += 10
        if typ == 1:
            ips.append(socket.inet_ntoa(r[i:i + 4]))
        i += rdl
    if not ips:
        raise ValueError("no A record")
    return ips


def _resolve(host, server=None):
    # Google DNS over UDP (plain DNS, not DoH). Cloudflare DoH gave a dead IP for Steam.
    try:
        return dns_query(host, server or DNS_SETS["google"][0])
    except Exception:
        return list(dict.fromkeys(a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET)))


def resolve(host, server=None):
    """All A records for host, cached. Failed IPs are rotated to the back (see demote_ip)."""
    hit = _dns_cache.get(host)
    if hit and hit[1] > time.monotonic():
        return list(hit[0])
    ips = _resolve(host, server)
    _dns_cache[host] = (ips, time.monotonic() + DNS_TTL)
    return list(ips)


resolve_doh = resolve    # original script's name for the same function (it is plain DNS)


def clear_dns_cache():
    _dns_cache.clear()


def demote_ip(host, ip):
    """Move a failed IP to the back of the cache so the next attempt tries another one."""
    if (hit := _dns_cache.get(host)) and ip in hit[0] and len(hit[0]) > 1:
        hit[0].remove(ip)
        hit[0].append(ip)


def fastest_dns():
    """Time one query per provider in parallel; return (best_name, [(ms, name)...])."""
    def probe(name):
        t = time.monotonic()
        try:
            dns_query("store.steampowered.com", DNS_SETS[name][0], 2)
            return (time.monotonic() - t) * 1000, name
        except Exception:
            return float("inf"), name
    with concurrent.futures.ThreadPoolExecutor(len(DNS_SETS)) as ex:
        res = sorted(ex.map(probe, DNS_SETS))
    if res[0][0] == float("inf"):
        raise RuntimeError("no DNS provider reachable")
    return res[0][1], res


# ------------------------------------------------------------------ TLS ClientHello

def sni_span(rec):
    """Return (offset, length) of SNI host name inside a TLS ClientHello record, else None."""
    try:
        if rec[0] != 0x16 or rec[5] != 0x01:
            return None
        p = 43                       # rec hdr 5 + hs type 1 + hs len 3 + version 2 + random 32
        p += 1 + rec[p]              # session id
        p += 2 + int.from_bytes(rec[p:p + 2], "big")   # cipher suites
        p += 1 + rec[p]              # compression
        end = p + 2 + int.from_bytes(rec[p:p + 2], "big")
        p += 2
        while p + 4 <= end:
            t = int.from_bytes(rec[p:p + 2], "big")
            ln = int.from_bytes(rec[p + 2:p + 4], "big")
            p += 4
            if t == 0:               # server_name: list len 2, type 1, name len 2
                off = p + 5
                ln = int.from_bytes(rec[p + 3:p + 5], "big")
                return (off, ln) if off + ln <= len(rec) else None
            p += ln
    except IndexError:
        pass
    return None


def keepalive(sock):
    """Detect dead peers in ~15s + 3s*3 instead of the 2h OS default."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 15)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except (OSError, AttributeError):
        pass


# --------------------------------------------------------------------------- proxy

def pac_url(port=PORT):
    """The PAC URL apply_network() expects for this proxy port."""
    return "http://%s:%d%s" % (PAC_HOST, _check_port(port), PAC_PATH)


def pac_script(port=PORT):
    """PAC file body: only Steam domains go through the proxy, everything else DIRECT."""
    _check_port(port)
    cond = " || ".join('dnsDomainIs(host,"%s")' % d for d in DOMAINS)
    return ('function FindProxyForURL(url, host) { if (%s) '
            'return "PROXY %s:%d; DIRECT"; return "DIRECT"; }' % (cond, PAC_HOST, port))


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        w.close()


class ProxyServer:
    """CONNECT proxy + PAC server on 127.0.0.1. Nothing here needs root."""

    def __init__(self, port=PORT, dns="google", log=None):
        self.port = _check_port(port, allow_zero=True)
        self.dns = _check_dns_name(dns)
        self.log = log
        self.pac = pac_script(self.port or PORT)
        self._server = None
        self._loop = None
        self._stop = asyncio.Event()

    # -- lifecycle
    async def start(self, host=PAC_HOST):
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle, host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]   # resolves port=0
        self.pac = pac_script(self.port)
        return self

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def run_forever(self):
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.close()

    def stop(self):
        """Ask the server to shut down. Safe from another thread or a signal handler."""
        loop = self._loop
        if loop is None:
            self._stop.set()
            return True
        try:
            loop.call_soon_threadsafe(self._stop.set)
        except RuntimeError:                 # loop already closed
            return False
        return True

    def _say(self, msg):
        if self.log:
            self.log(msg)

    # -- protocol
    async def _handle(self, cr, cw):
        try:
            try:
                head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 30)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                cw.close()
                return
            line = head.split(b"\r\n", 1)[0].decode(errors="replace")
            if line.startswith("GET "):
                body = self.pac.encode()
                cw.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ns-proxy-autoconfig\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
                await cw.drain()
                cw.close()
                return
            if not line.startswith("CONNECT "):
                cw.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                await cw.drain()
                cw.close()
                return
            parts = line.split()
            host, _, port_s = (parts[1] if len(parts) > 1 else "").rpartition(":")
            if not _HOST_RE.fullmatch(host) or not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
                cw.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await cw.drain()
                cw.close()
                return
            await self._tunnel(cr, cw, host, int(port_s))
        except Exception as e:                       # one bad client must not kill the proxy
            self._say("fail %s: %s" % (type(e).__name__, e))
            cw.close()

    async def _tunnel(self, cr, cw, host, port):
        loop = asyncio.get_running_loop()
        server = DNS_SETS[self.dns][0]
        ips = await loop.run_in_executor(None, resolve, host, server)
        for ip in ips:               # failover: 3.5s per IP, failed IP rotates to back of cache
            try:
                sr, sw = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
                break
            except (OSError, asyncio.TimeoutError):
                demote_ip(host, ip)
        else:
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        self._say("CONNECT %s:%d -> %s" % (host, port, ip))
        sock = sw.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            keepalive(sock)
        client_sock = cw.get_extra_info("socket")
        if client_sock is not None:
            keepalive(client_sock)
        cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cw.drain()
        first = await cr.read(16384)
        if first[:1] == b"\x16" and len(first) >= 5:   # read whole TLS record, not one TCP segment (MSS 1460)
            need = 5 + int.from_bytes(first[3:5], "big")
            while len(first) < need:
                more = await asyncio.wait_for(cr.read(need - len(first)), 15)
                if not more:
                    break
                first += more
        span = sni_span(first)
        if span:                     # split ClientHello after TLS record header (DPI evasion)
            cut = 5                  # mid-SNI cut gets RST on some ISPs; this one passes
            sw.write(first[:cut])
            await sw.drain()
            await asyncio.sleep(0.05)
            first = first[cut:]
        sw.write(first)
        await sw.drain()
        await asyncio.gather(pipe(cr, sw), pipe(sr, cw))


def serve(port=PORT, dns="google", watch=False):
    """Run the CONNECT proxy + PAC server until stop()/Ctrl-C. No root needed.

    ``dns`` names the DNS provider used for resolution (a DNS_SETS key), not the
    system setting - changing that is apply_network()'s job. ``watch=True`` also
    polls for Steam start and flushes the DNS cache.
    """
    _check_port(port)
    proxy = ProxyServer(port, dns)
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, proxy

    async def main():
        await proxy.start()
        task = asyncio.create_task(watch_steam()) if watch else None
        try:
            await proxy._stop.wait()
        finally:
            if task is not None:
                task.cancel()
            await proxy.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        _ACTIVE = previous
    return proxy


def stop():
    """Stop the server started by serve() from any thread. False when nothing runs."""
    proxy = _ACTIVE
    if proxy is None:
        return False
    return proxy.stop()


async def watch_steam(interval=DNS_POLL_INTERVAL, games=None):
    """Flush the DNS cache when a game process newly appears (upstream 'Game Launch Hook')."""
    seen = set()
    while True:
        now = {n for n in (games or GAME_PROCS)
               if subprocess.run(["pgrep", "-x", n], capture_output=True).returncode == 0}
        if now - seen:
            flush_dns()
        seen = now
        await asyncio.sleep(interval)
