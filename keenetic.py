"""Điều khiển router Keenetic/Netcraze của khách qua RCI (HTTP API của web UI), đi qua KeenDNS.

Đăng nhập: GET /auth -> 401 + X-NDM-Realm + X-NDM-Challenge (+ cookie phiên)
           POST /auth {login, password=sha256(challenge + md5(login:realm:pass))} -> 200
Lệnh:      POST /rci/  [{"parse": "<lệnh CLI>"}]
Trạng thái: GET /rci/show/interface[/<tên>]
"""
import hashlib, json, re, socket, time
import requests

TIMEOUT = 15
USER_AGENT = "wghcbot-router/1.0"


class RouterError(Exception):
    """Lỗi có mã ngắn để hiện cho admin: offline | auth | rci | iface | handshake."""
    def __init__(self, code, msg=""):
        super().__init__(msg or code)
        self.code = code


def norm_base(addr: str) -> str:
    addr = addr.strip().rstrip("/")
    if not re.match(r"^https?://", addr):
        addr = "https://" + addr
    return addr


class Router:
    def __init__(self, base, login, password, timeout=TIMEOUT):
        self.base = norm_base(base)
        self.login = login
        self.password = password
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT

    # ---------- HTTP ----------
    def _req(self, method, path, **kw):
        try:
            return self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise RouterError("offline", f"không vào được router: {type(e).__name__}")

    def auth(self):
        r = self._req("GET", "/auth")
        if r.status_code == 200:
            return True
        if r.status_code != 401:
            raise RouterError("offline", f"/auth trả {r.status_code}")
        realm = r.headers.get("X-NDM-Realm")
        ch = r.headers.get("X-NDM-Challenge")
        if not realm or not ch:
            raise RouterError("offline", "không phải router Keenetic (thiếu challenge)")
        md5 = hashlib.md5(f"{self.login}:{realm}:{self.password}".encode()).hexdigest()
        h = hashlib.sha256((ch + md5).encode()).hexdigest()
        r = self._req("POST", "/auth", json={"login": self.login, "password": h})
        if r.status_code != 200:
            raise RouterError("auth", f"router từ chối user '{self.login}': sai mật khẩu hoặc user thiếu quyền web")
        return True

    def _json(self, r):
        if r.status_code == 401:
            raise RouterError("auth", "phiên hết hạn / thiếu quyền RCI")
        if r.status_code != 200:
            raise RouterError("rci", f"RCI trả {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise RouterError("rci", "RCI trả dữ liệu không phải JSON")

    def get(self, path):
        return self._json(self._req("GET", "/rci/" + path.lstrip("/")))

    def cli(self, command):
        """Chạy 1 lệnh CLI qua RCI parse. Lỗi nếu router báo status=error."""
        data = self._json(self._req("POST", "/rci/", json=[{"parse": command}]))
        for st in _iter_status(data):
            if str(st.get("status", "")).lower() == "error":
                raise RouterError("rci", f"'{command}': {st.get('message') or st}")
        return data

    # ---------- WireGuard ----------
    def wg_interfaces(self):
        """{tên: dữ liệu} các interface WireGuard/AmneziaWG."""
        data = self.get("show/interface")
        out = {}
        if isinstance(data, dict):
            for name, v in data.items():
                if not isinstance(v, dict):
                    continue
                t = str(v.get("type", "")) + str(v.get("id", name))
                if "wireguard" in t.lower():
                    out[v.get("id", name)] = v
        return out

    def detect_iface(self, endpoint_host):
        """Tìm tunnel trỏ tới server của mình. Trả tên interface."""
        ifs = self.wg_interfaces()
        if not ifs:
            raise RouterError("iface", "router không có tunnel WireGuard")
        ips = _resolve(endpoint_host)
        needles = [endpoint_host.lower()] + ips
        hits = []
        for name, v in ifs.items():
            blob = json.dumps(v).lower()
            if any(n in blob for n in needles):
                hits.append(name)
        if len(hits) == 1:
            return hits[0]
        if not hits and len(ifs) == 1:
            return next(iter(ifs))
        raise RouterError("iface", f"không xác định được tunnel ({', '.join(ifs)}) — nhập tay")

    def wg_status(self, iface):
        v = self.get(f"show/interface/{iface}")
        if not isinstance(v, dict) or not v:
            raise RouterError("iface", f"không có interface {iface}")
        return parse_status(v)

    def reconnect(self, iface, pause=3, wait_hs=20, poll=2, sleep=time.sleep):
        """Tắt -> chờ -> bật -> chờ handshake mới. Trả status sau cùng.
        Luôn cố bật lại (3 lần) dù bước tắt/kiểm tra lỗi, để không bỏ router ở trạng thái tắt."""
        self.cli(f"interface {iface} down")
        sleep(pause)
        last = None
        for _ in range(3):
            try:
                self.cli(f"interface {iface} up")
                last = None
                break
            except RouterError as e:
                last = e
                sleep(2)
        if last:
            raise last
        t0 = time.monotonic()
        st = None
        while time.monotonic() - t0 < wait_hs:
            sleep(poll)
            try:
                st = self.wg_status(iface)
            except RouterError:
                continue
            if st["handshake_s"] is not None and st["handshake_s"] <= wait_hs + pause + 5:
                return st
        st = st or self.wg_status(iface)
        raise RouterError("handshake", f"tunnel đã bật nhưng chưa có handshake ({fmt_hs(st['handshake_s'])})")


# ---------- helpers ----------
def _iter_status(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "status" and isinstance(v, list):
                for s in v:
                    if isinstance(s, dict):
                        yield s
            else:
                yield from _iter_status(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _iter_status(x)


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk(x)


def parse_status(v):
    """Rút trạng thái từ show/interface/<wg>. Tên trường firmware có thể khác nhau nên dò mềm."""
    hs = None
    rx = tx = 0
    for k, val in _walk(v):
        kl = str(k).lower()
        if "handshake" in kl and isinstance(val, (int, float)):
            # firmware trả 'giây trước'; nếu là timestamp unix thì đổi ra giây trước
            sec = int(val)
            if sec > 10**9:
                sec = int(time.time()) - sec
            if sec >= 0 and (hs is None or sec < hs):
                hs = sec
        elif kl in ("rxbytes", "rx-bytes", "rx") and isinstance(val, (int, float)):
            rx = max(rx, int(val))
        elif kl in ("txbytes", "tx-bytes", "tx") and isinstance(val, (int, float)):
            tx = max(tx, int(val))
    state = str(v.get("state", "")).lower()
    link = str(v.get("link", "")).lower()
    up = state in ("up", "") and link in ("up", "")
    return {"up": up and state == "up", "handshake_s": hs, "rx": rx, "tx": tx,
            "state": state or "?", "link": link or "?"}


def _resolve(host):
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


def fmt_hs(s):
    if s is None:
        return "chưa có handshake"
    if s < 90:
        return f"{s} giây trước"
    if s < 5400:
        return f"{s // 60} phút trước"
    return f"{s // 3600} giờ trước"


def is_stale(st, limit):
    return (not st["up"]) or st["handshake_s"] is None or st["handshake_s"] > limit
