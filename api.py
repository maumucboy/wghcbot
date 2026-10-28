#!/usr/bin/env python3
"""Backend FastAPI cho Mini App quản lý AmneziaWG.
Phục vụ trang Mini App + API JSON; auth bằng Telegram initData (chỉ admin)."""
import os, time, hmac, hashlib, json, urllib.parse
import requests
from fastapi import FastAPI, Header, HTTPException, Body
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
import awg, db

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "330613623"))
NODE = os.environ.get("NODE_NAME", "eaz2")
ENDPOINT = os.environ.get("AWG_ENDPOINT_DOMAIN", "eaz2.hoichamvn.uk")
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()
db.init()


def check_init(init_data: str) -> bool:
    """Xác thực Telegram WebApp initData, trả True nếu đúng admin."""
    if not init_data:
        return False
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data = dict(pairs)
        their_hash = data.pop("hash", None)
        if not their_hash:
            return False
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, their_hash):
            return False
        # không quá cũ (24h) + đúng admin
        if int(time.time()) - int(data.get("auth_date", 0)) > 86400:
            return False
        user = json.loads(data.get("user", "{}"))
        return int(user.get("id", 0)) == ADMIN_ID
    except Exception:
        return False


def need_auth(x_init_data):
    if not check_init(x_init_data):
        raise HTTPException(status_code=401, detail="unauthorized")


def send_to_admin(name, caption):
    """Gửi file .conf + QR của khách về chat admin qua Bot API."""
    base = f"https://api.telegram.org/bot{TOKEN}"
    conf = f"{awg.CONF_DIR}/{name}.conf"
    png = f"{awg.CONF_DIR}/{name}.png"
    try:
        if os.path.exists(conf):
            with open(conf, "rb") as f:
                requests.post(f"{base}/sendDocument",
                              data={"chat_id": ADMIN_ID, "caption": caption},
                              files={"document": (f"{name}.conf", f)}, timeout=30)
        if os.path.exists(png):
            with open(png, "rb") as f:
                requests.post(f"{base}/sendPhoto",
                              data={"chat_id": ADMIN_ID},
                              files={"photo": (f"{name}.png", f)}, timeout=30)
        return True
    except Exception:
        return False


def client_view(c, wg):
    w = wg.get(c["name"], {})
    far = c["expires_at"] > time.time() + 365 * 50 * 86400
    days = int((c["expires_at"] - time.time()) / 86400)
    return {
        "name": c["name"], "node": c["node"], "plan": c["plan_months"], "price": c["price"],
        "expires_at": c["expires_at"], "days": None if far else days, "no_expiry": far,
        "locked": bool(c["locked"]), "contact": c["contact"], "note": c["note"],
        "online": w.get("online", False), "handshake_s": w.get("handshake_s"),
        "rx": w.get("rx", 0), "tx": w.get("tx", 0),
        "endpoint": w.get("endpoint"), "ip": w.get("ip"),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/dashboard")
def dashboard(x_init_data: str = Header(None)):
    need_auth(x_init_data)
    wg = awg.wg_status(); st = awg.server_stats()
    clients = [client_view(c, wg) for c in db.all_clients()]
    online = sum(1 for c in clients if not c["locked"] and c["online"])
    return {
        "node": {"name": NODE, "endpoint": ENDPOINT, "port": awg.AWG_PORT,
                 "cpu": st["cpu_pct"], "ram": st["ram_pct"], "uptime_d": st["uptime_d"]},
        "online": online, "total": len(clients),
        "expiring": len(db.expiring(db.reminder_days() if False else 7)),
        "revenue_month": db.revenue_month()["total"],
        "prices": db.get_prices(), "clients": clients,
    }


@app.get("/api/client/{name}")
def client_detail(name: str, x_init_data: str = Header(None)):
    need_auth(x_init_data)
    c = db.get(name)
    if not c:
        raise HTTPException(404, "not found")
    return client_view(c, awg.wg_status())


@app.post("/api/client")
def create(payload: dict = Body(...), x_init_data: str = Header(None)):
    need_auth(x_init_data)
    name = (payload.get("name") or "").strip()
    plan = payload.get("plan")  # 1/3/6/12 hoặc "trial"
    import re
    if not re.fullmatch(r"[A-Za-z0-9_]{2,32}", name):
        raise HTTPException(400, "tên không hợp lệ")
    if db.get(name):
        raise HTTPException(400, "tên đã tồn tại")
    ok, conf, err = awg.add_client(name)
    if not ok:
        raise HTTPException(500, str(err))
    days = payload.get("days")
    if days:
        d = int(days)
        if d < 1 or d > 3650:
            raise HTTPException(400, "số ngày 1–3650")
        exp = db.add(name, 0, int(payload.get("price", 0) or 0), NODE, days=d)
    elif plan == "trial":
        exp = db.add(name, 0, 0, NODE, trial_days=3)
    else:
        m = int(plan); exp = db.add(name, m, db.get_price(m), NODE)
    send_to_admin(name, f"✅ {name} · {ENDPOINT}:{awg.AWG_PORT} · hạn {time.strftime('%d.%m.%Y', time.localtime(exp))}")
    return {"ok": True, "name": name, "expires_at": exp}


@app.post("/api/client/{name}/send")
def send_config(name: str, x_init_data: str = Header(None)):
    need_auth(x_init_data)
    ok = send_to_admin(name, f"📎 {name} · {ENDPOINT}:{awg.AWG_PORT}")
    return {"ok": ok}


@app.post("/api/client/{name}/extend")
def extend(name: str, payload: dict = Body(...), x_init_data: str = Header(None)):
    need_auth(x_init_data)
    days = payload.get("days")
    if days:
        d = int(days)
        if d < 1 or d > 3650:
            raise HTTPException(400, "số ngày 1–3650")
        exp = db.extend(name, 0, int(payload.get("price", 0) or 0), days=d)
    else:
        m = int(payload.get("plan", 1))
        exp = db.extend(name, m, db.get_price(m))
    c = db.get(name); w = awg.wg_status().get(name, {})
    if c and w.get("ip"):
        awg.unlock_client(w["ip"])
    return {"ok": True, "expires_at": exp}


@app.post("/api/client/{name}/lock")
def lock(name: str, x_init_data: str = Header(None)):
    need_auth(x_init_data)
    w = awg.wg_status().get(name, {})
    if w.get("ip"):
        awg.lock_client(w["ip"])
    db.set_locked(name, 1)
    return {"ok": True}


@app.post("/api/client/{name}/unlock")
def unlock(name: str, x_init_data: str = Header(None)):
    need_auth(x_init_data)
    w = awg.wg_status().get(name, {})
    if w.get("ip"):
        awg.unlock_client(w["ip"])
    db.set_locked(name, 0)
    return {"ok": True}


@app.post("/api/client/{name}/note")
def note(name: str, payload: dict = Body(...), x_init_data: str = Header(None)):
    need_auth(x_init_data)
    if "note" in payload:
        db.set_field(name, "note", payload["note"][:200])
    if "contact" in payload:
        db.set_field(name, "contact", payload["contact"][:64])
    return {"ok": True}


@app.delete("/api/client/{name}")
def remove(name: str, x_init_data: str = Header(None)):
    need_auth(x_init_data)
    w = awg.wg_status().get(name, {})
    if w.get("ip"):
        awg.unlock_client(w["ip"])
    awg.remove_client(name); db.delete(name)
    return {"ok": True}


@app.get("/api/client/{name}/config", response_class=PlainTextResponse)
def config(name: str, x_init_data: str = Header(None)):
    need_auth(x_init_data)
    p = f"{awg.CONF_DIR}/{name}.conf"
    if not os.path.exists(p):
        raise HTTPException(404, "no config")
    return open(p).read()


@app.get("/api/revenue")
def revenue(x_init_data: str = Header(None)):
    need_auth(x_init_data)
    r = db.revenue_month()
    exp = [client_view(c, {}) for c in db.expiring(7)]
    return {"active": r["active"], "new": r["new"], "renew": r["renew"],
            "total": r["total"], "expiring": exp}


@app.get("/api/settings")
def get_settings(x_init_data: str = Header(None)):
    need_auth(x_init_data)
    return {"prices": db.get_prices(), "reminder_days": db.reminder_days()}


@app.post("/api/settings")
def set_settings(payload: dict = Body(...), x_init_data: str = Header(None)):
    need_auth(x_init_data)
    for m, v in (payload.get("prices") or {}).items():
        db.set_price(int(m), int(v))
    if "reminder_days" in payload:
        db.set_setting("reminder_days", int(payload["reminder_days"]))
    return {"ok": True}
