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

    def _relogin(self):
        self.s.close()
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.auth()

    def reconnect(self, iface, wait=75, poll=3, sleep=time.sleep):
        """Tắt + bật tunnel trong CÙNG MỘT request, rồi đăng nhập lại kiểm tra.

        Router thường đi Internet qua chính tunnel này (defaultgw), nên khi tunnel tắt
        thì đường KeenDNS tới bot đứt giữa chừng: request "down" riêng lẻ sẽ treo và lệnh
        "up" gửi sau không bao giờ tới. Gửi gộp [down, up] để router tự làm cả hai;
        response mất cũng không sao. Sau đó dò lại tối đa `wait` giây, thấy tunnel chưa
        bật thì gửi "up" lần nữa."""
        t0 = time.monotonic()
        body = [{"parse": f"interface {iface} down"}, {"parse": f"interface {iface} up"}]
        try:
            self.s.request("POST", self.base + "/rci/", json=body, timeout=8)
        except requests.RequestException:
            pass  # bình thường: đường về đứt khi tunnel tắt
        st, last_err = None, None
        while time.monotonic() - t0 < wait:
            sleep(poll)
            try:
                self._relogin()
                st = self.wg_status(iface)
            except RouterError as e:
                last_err = e
                continue
            if not st["up"]:
                try:
                    self.cli(f"interface {iface} up")
                except RouterError as e:
                    last_err = e
                continue
            elapsed = time.monotonic() - t0
            restarted = st["uptime"] is not None and st["uptime"] <= elapsed + 5
            fresh = st["handshake_s"] is not None and st["handshake_s"] <= elapsed + 5
            if restarted and fresh:
                return st
        if st is None:
            raise RouterError(getattr(last_err, "code", "offline"),
                              f"mất liên lạc router sau khi reconnect ({last_err})")
        if not st["up"]:
            raise RouterError("rci", "tunnel vẫn tắt sau khi gửi lệnh bật")
        raise RouterError("handshake", f"tunnel đã bật nhưng chưa có handshake mới ({fmt_hs(st['handshake_s'])})")


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
    conf = str(v.get("summary", {}).get("layer", {}).get("conf", "")).lower() if isinstance(v.get("summary"), dict) else ""
    up = state == "up" and conf in ("running", "")
    uptime = v.get("uptime") if isinstance(v.get("uptime"), (int, float)) else None
    return {"up": up, "handshake_s": hs, "rx": rx, "tx": tx, "uptime": uptime,
            "state": state or "?", "link": link or "?", "conf": conf or "?"}


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
