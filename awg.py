"""Lớp điều khiển AmneziaWG: gọi manage script, đọc wg show, khoá/mở khách."""
import json, subprocess, time, re, os

MANAGE = "/root/awg/manage_amneziawg.sh"
SERVER_CONF = "/etc/amnezia/amneziawg/awg0.conf"
ENDPOINT_DOMAIN = os.environ.get("AWG_ENDPOINT_DOMAIN", "eaz2.hoichamvn.uk")
AWG_PORT = os.environ.get("AWG_PORT", "443")
CONF_DIR = "/root/awg"
HANDSHAKE_ONLINE = 180  # giây


def _awg_bin():
    for b in ("awg", "wg"):
        if subprocess.run(["which", b], capture_output=True).returncode == 0:
            return b
    return "awg"


AWG = _awg_bin()


def run_manage(args, timeout=60):
    """Chạy manage_amneziawg.sh với --json --yes, trả về dict."""
    cmd = ["bash", MANAGE] + args + ["--json", "--yes"]
    env = dict(os.environ, AWG_YES="1")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    out = p.stdout.strip()
    # manage in JSON ra stdout; log người-đọc ra stderr
    try:
        line = [l for l in out.splitlines() if l.strip().startswith("{") or l.strip().startswith("[")]
        return json.loads(line[-1]) if line else {"ok": False, "error": out or p.stderr.strip(), "rc": p.returncode}
    except Exception as e:
        return {"ok": False, "error": f"parse: {e}: {out[:200]}", "rc": p.returncode}


def _peer_map():
    """Đọc awg0.conf: trả {pubkey: {'name':..., 'ip':...}} và {name: ip}."""
    by_pub, by_name = {}, {}
    try:
        txt = open(SERVER_CONF).read()
    except OSError:
        return by_pub, by_name
    blocks = re.split(r"\n\[Peer\]", txt)
    for b in blocks[1:]:
        name = re.search(r"#_Name\s*=\s*(\S+)", b)
        pub = re.search(r"PublicKey\s*=\s*(\S+)", b)
        aip = re.search(r"AllowedIPs\s*=\s*([0-9.]+)", b)
        nm = name.group(1) if name else None
        ip = aip.group(1) if aip else None
        if pub:
            by_pub[pub.group(1)] = {"name": nm, "ip": ip}
        if nm and ip:
            by_name[nm] = ip
    return by_pub, by_name


def wg_status():
    """Trả {name: {online,handshake_s,rx,tx,endpoint,ip}} ghép từ wg show + conf."""
    by_pub, by_name = _peer_map()
    res = {}
    try:
        dump = subprocess.run([AWG, "show", "awg0", "dump"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        dump = ""
    now = time.time()
    # dòng đầu của dump là interface; các dòng sau là peer:
    # pubkey  psk  endpoint  allowedips  latest-handshake  rx  tx  keepalive
    for ln in dump.splitlines()[1:]:
        f = ln.split("\t")
        if len(f) < 8:
            continue
        pub, endpoint, hs, rx, tx = f[0], f[2], int(f[4]), int(f[5]), int(f[6])
        info = by_pub.get(pub, {})
        nm = info.get("name") or pub[:8]
        hs_s = int(now - hs) if hs > 0 else None
        res[nm] = {
            "online": hs_s is not None and hs_s <= HANDSHAKE_ONLINE,
            "handshake_s": hs_s,
            "rx": rx, "tx": tx,
            "endpoint": endpoint if endpoint != "(none)" else None,
            "ip": info.get("ip"),
        }
    # khách có trong conf nhưng chưa handshake bao giờ
    for nm, ip in by_name.items():
        res.setdefault(nm, {"online": False, "handshake_s": None, "rx": 0, "tx": 0, "endpoint": None, "ip": ip})
    return res


def add_client(name):
    """Tạo khách 2.0, ép endpoint về tên miền. Trả (ok, conf_path, err)."""
    r = run_manage(["add", name])
    if not r.get("ok"):
        return False, None, r.get("error", "lỗi add")
    conf = f"{CONF_DIR}/{name}.conf"
    try:
        txt = open(conf).read()
        txt = re.sub(r"Endpoint\s*=\s*\S+", f"Endpoint = {ENDPOINT_DOMAIN}:{AWG_PORT}", txt)
        open(conf, "w").write(txt)
    except OSError as e:
        return True, conf, f"tạo xong nhưng chưa sửa endpoint: {e}"
    return True, conf, None


def remove_client(name):
    r = run_manage(["remove", name])
    return r.get("ok", False), r.get("error")


def _iptables(action, ip):
    """action: 'add' hoặc 'del'. Chặn 2 chiều IP tunnel của khách."""
    flag = "-I" if action == "add" else "-D"
    ok = True
    for rule in (["-s", ip], ["-d", ip]):
        p = subprocess.run(["iptables", flag, "FORWARD"] + rule + ["-j", "DROP"], capture_output=True)
        if p.returncode != 0 and action == "del":
            pass  # xoá rule không tồn tại — bỏ qua
        elif p.returncode != 0:
            ok = False
    return ok


def lock_client(ip):
    # xoá trước cho sạch rồi thêm (tránh trùng rule)
    _iptables("del", ip); _iptables("del", ip)
    return _iptables("add", ip)


def unlock_client(ip):
    _iptables("del", ip); _iptables("del", ip)
    return True


def reconcile_locks(locked_ips, all_ips):
    """Khi bot khởi động: gỡ hết rule cũ, áp lại đúng danh sách bị khoá."""
    for ip in all_ips:
        unlock_client(ip)
    for ip in locked_ips:
        lock_client(ip)


def server_stats():
    s = {}
    try:
        with open("/proc/meminfo") as f:
            mi = {k: int(v.split()[0]) for k, v in (l.split(":") for l in f)}
        s["ram_pct"] = round(100 * (1 - mi.get("MemAvailable", 0) / mi["MemTotal"]))
    except Exception:
        s["ram_pct"] = None
    try:
        up = float(open("/proc/uptime").read().split()[0])
        s["uptime_d"] = round(up / 86400, 1)
    except Exception:
        s["uptime_d"] = None
    try:
        a = _cpu_sample(); time.sleep(0.25); b = _cpu_sample()
        idle = b[3] - a[3]; total = sum(b) - sum(a)
        s["cpu_pct"] = round(100 * (1 - idle / total)) if total else None
    except Exception:
        s["cpu_pct"] = None
    return s


def _cpu_sample():
    with open("/proc/stat") as f:
        return [int(x) for x in f.readline().split()[1:5]]


def backup():
    """Gọi manage backup, trả đường dẫn file .tar.gz mới nhất."""
    run_manage(["backup"])
    bdir = f"{CONF_DIR}/backups"
    try:
        files = [os.path.join(bdir, x) for x in os.listdir(bdir) if x.endswith(".tar.gz")]
        return max(files, key=os.path.getmtime) if files else None
    except OSError:
        return None
