"""Đo tốc độ, IP server khách đang vào, lưu lượng theo ngày, lịch sử đổi IP, tra nhà mạng (offline).

Ghi chú chiều traffic (nhìn từ phía KHÁCH):
  awg dump: rx = server nhận từ khách = khách TẢI LÊN (↑)
            tx = server gửi tới khách = khách TẢI XUỐNG (↓)
"""
import collections, ipaddress, os, sqlite3, subprocess, threading, time
import awg, db

AWG_PORT = int(os.environ.get("AWG_PORT", "443"))
SAMPLE_S = 30          # chu kỳ job nền
HIST_LEN = 30          # 30 mẫu × 30s = 15 phút
ASN_DB = os.environ.get("ASN_DB", os.path.join(os.path.dirname(db.DB_PATH), "asn.db"))


def _labels(env_name):
    """'1.2.3.4=eaz2,5.6.7.8=IP cũ' -> {'1.2.3.4': 'eaz2', ...}"""
    out = {}
    for part in os.environ.get(env_name, "").split(","):
        if "=" in part:
            ip, lb = part.split("=", 1)
            if ip.strip():
                out[ip.strip()] = lb.strip() or ip.strip()
    return out


SERVER_LABELS = _labels("SERVER_IPS")   # IP của server -> nhãn
RELAY_LABELS = _labels("RELAY_IPS")     # IP VPS relay  -> nhãn


def server_label(ip):
    return SERVER_LABELS.get(ip, ip) if ip else "?"


# ---------- đọc dữ liệu thô ----------
def split_endpoint(ep):
    """'1.2.3.4:5678' / '[::1]:5678' -> ('1.2.3.4', 5678); lỗi -> (None, None)."""
    if not ep or ep == "(none)":
        return None, None
    try:
        host, port = ep.rsplit(":", 1)
        return host.strip("[]"), int(port)
    except ValueError:
        return None, None


def read_dump():
    """{pubkey: (name, endpoint, handshake_ts, rx, tx)}"""
    by_pub, _ = awg._peer_map()
    try:
        out = subprocess.run([awg.AWG, "show", "awg0", "dump"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return {}
    res = {}
    for ln in out.splitlines()[1:]:
        f = ln.split("\t")
        if len(f) < 8:
            continue
        name = by_pub.get(f[0], {}).get("name") or f[0][:8]
        try:
            res[f[0]] = (name, f[2], int(f[4]), int(f[5]), int(f[6]))
        except ValueError:
            continue
    return res


def parse_conntrack(text, port=AWG_PORT):
    """Dòng conntrack UDP tới cổng AWG -> {(ip khách, cổng khách): ip server}."""
    m = {}
    for ln in text.splitlines():
        if "udp" not in ln:
            continue
        kv = {}
        for tok in ln.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv.setdefault(k, v)  # lấy chiều gốc (lần xuất hiện đầu)
        try:
            if int(kv.get("dport", 0)) != port:
                continue
            m[(kv["src"], int(kv["sport"]))] = kv["dst"]
        except (KeyError, ValueError):
            continue
    return m


def conntrack_map():
    try:
        with open("/proc/net/nf_conntrack") as f:
            return parse_conntrack(f.read())
    except OSError:
        pass
    try:
        out = subprocess.run(["conntrack", "-L", "-p", "udp", "--dport", str(AWG_PORT)],
                             capture_output=True, text=True, timeout=10).stdout
        return parse_conntrack(out)
    except Exception:
        return {}


def conntrack_ok():
    return os.path.exists("/proc/net/nf_conntrack") or subprocess.run(
        ["which", "conntrack"], capture_output=True).returncode == 0


def route_of(endpoint, ct):
    """-> (kind, label, server_ip). kind: 'relay' | 'direct' | 'unknown'."""
    ip, port = split_endpoint(endpoint)
    if not ip:
        return "unknown", "?", None
    if ip in RELAY_LABELS:
        return "relay", RELAY_LABELS[ip], ct.get((ip, port))
    dst = ct.get((ip, port))
    if dst:
        return "direct", server_label(dst), dst
    return "unknown", "?", None


# ---------- tra nhà mạng (offline, asn.db do asn_build.py tạo) ----------
ASN_NAMES = {3216: "Билайн", 16345: "Билайн", 8402: "Билайн", 8359: "МТС",
             31133: "МегаФон", 25159: "МегаФон", 15378: "T2", 12958: "T2", 12389: "Ростелеком"}


def asn_lookup(ip):
    """-> 'Билайн (AS3216)' | 'SOME-ISP (AS123)' | None."""
    try:
        n = int(ipaddress.IPv4Address(ip))
    except Exception:
        return None
    if not os.path.exists(ASN_DB):
        return None
    try:
        c = sqlite3.connect(f"file:{ASN_DB}?mode=ro", uri=True)
        r = c.execute("SELECT end_ip, asn, descr FROM asn WHERE start_ip<=? ORDER BY start_ip DESC LIMIT 1",
                      (n,)).fetchone()
        c.close()
    except sqlite3.Error:
        return None
    if not r or r[0] < n or not r[1]:
        return None
    name = ASN_NAMES.get(r[1]) or (r[2] or "").split(" ")[0][:24] or "?"
    return f"{name} (AS{r[1]})"


# ---------- đo ----------
def _rates(a, b, dt):
    """Hai lần đọc dump -> {name: (down_bps, up_bps)}."""
    out = {}
    for pub, (name, _ep, _hs, rx1, tx1) in b.items():
        if pub not in a:
            continue
        _, _, _, rx0, tx0 = a[pub]
        drx = rx1 - rx0 if rx1 >= rx0 else rx1
        dtx = tx1 - tx0 if tx1 >= tx0 else tx1
        out[name] = (dtx * 8 / dt, drx * 8 / dt)
    return out


def measure(seconds=5):
    """Đo tức thời (chặn ~seconds giây — gọi qua asyncio.to_thread)."""
    a, t0 = read_dump(), time.monotonic()
    time.sleep(seconds)
    b, t1 = read_dump(), time.monotonic()
    return _rates(a, b, t1 - t0), b


class Collector:
    """Job nền mỗi 30s: tốc độ TB 30s, biểu đồ 15 phút, cộng lưu lượng ngày, ghi đổi IP server."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = None            # (monotonic, dump)
        self.rates = {}             # name -> (down_bps, up_bps)
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=HIST_LEN))
        self.route = {}             # name -> (kind, label, server_ip)
        self.updated = 0

    def sample(self):
        dump, now = read_dump(), time.monotonic()
        ct = conntrack_map()
        day = time.strftime("%Y-%m-%d")
        with self.lock:
            prev = self.last
            self.last = (now, dump)
            if prev:
                dt = now - prev[0]
                rates = _rates(prev[1], dump, dt)
                self.rates = rates
                usage = []
                for name, (dn, up) in rates.items():
                    self.hist[name].append(dn / 1e6)
                    if dn or up:
                        usage.append((day, name, int(dn * dt / 8), int(up * dt / 8)))
                db.usage_add(usage)
            for _pub, (name, ep, hs, _rx, _tx) in dump.items():
                if not hs or time.time() - hs > awg.HANDSHAKE_ONLINE:
                    continue
                r = route_of(ep, ct)
                if r[0] == "unknown":
                    continue
                key = f"relay:{r[1]}" if r[0] == "relay" else (r[2] or "?")
                old = db.iphist_last(name)
                if old != key:
                    db.iphist_add(name, key)
                self.route[name] = r
            self.updated = time.time()

    def snapshot(self):
        with self.lock:
            return dict(self.rates), {k: list(v) for k, v in self.hist.items()}, dict(self.route), self.updated


COLLECTOR = Collector()


# ---------- định dạng ----------
BARS = "▁▂▃▄▅▆▇█"


def sparkline(vals):
    if not vals:
        return ""
    mx = max(vals) or 1
    return "".join(BARS[min(7, int(v / mx * 7.999))] for v in vals)


def mbit(bps):
    v = bps / 1e6
    return f"{v:.1f}" if v < 100 else f"{v:.0f}"


def fbytes(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
