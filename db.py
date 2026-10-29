"""SQLite lưu thông tin kinh doanh của khách (hạn, gói, giá, ghi chú, khoá) + cài đặt."""
import sqlite3, time, os

DB_PATH = os.environ.get("AWG_BOT_DB", "/root/awgbot/bot.db")
DAY = 86400
PLAN_DAYS = {1: 30, 3: 90, 6: 180, 12: 360}
DEFAULT_PRICES = {1: 0, 3: 0, 6: 0, 12: 0}


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS clients(
            name TEXT PRIMARY KEY, node TEXT DEFAULT 'eaz2', plan_months INTEGER,
            created_at INTEGER, expires_at INTEGER, price INTEGER DEFAULT 0,
            contact TEXT DEFAULT '', note TEXT DEFAULT '', locked INTEGER DEFAULT 0,
            reminded INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS revenue(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT, amount INTEGER, ts INTEGER)""")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS routers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT UNIQUE, base TEXT, login TEXT,
            pass_enc TEXT, iface TEXT DEFAULT '', client TEXT DEFAULT '',
            last_check INTEGER DEFAULT 0, last_state TEXT DEFAULT 'new', last_hs INTEGER,
            last_err TEXT DEFAULT '', last_reconnect INTEGER DEFAULT 0, created_at INTEGER)""")


# ---------- settings ----------
def get_setting(key, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def get_price(months):
    v = get_setting(f"price_{months}")
    return int(v) if v is not None else DEFAULT_PRICES.get(months, 0)


def get_prices():
    return {m: get_price(m) for m in (1, 3, 6, 12)}


def set_price(months, val):
    set_setting(f"price_{months}", int(val))


def reminder_days():
    v = get_setting("reminder_days")
    return int(v) if v is not None else 3


# ---------- clients ----------
def add(name, plan_months, price=0, node="eaz2", trial_days=None, days=None):
    now = int(time.time())
    days = days or trial_days or PLAN_DAYS.get(plan_months, 30)
    exp = now + days * DAY
    with _conn() as c:
        c.execute("""INSERT OR REPLACE INTO clients
            (name,node,plan_months,created_at,expires_at,price,contact,note,locked,reminded)
            VALUES(?,?,?,?,?,?,'','',0,0)""", (name, node, plan_months or 0, now, exp, price))
        if price:
            c.execute("INSERT INTO revenue(name,kind,amount,ts) VALUES(?,?,?,?)", (name, "new", price, now))
    return exp


def extend(name, plan_months, price=0, days=None):
    now = int(time.time())
    days = days or PLAN_DAYS.get(plan_months, 30)
    with _conn() as c:
        r = c.execute("SELECT expires_at FROM clients WHERE name=?", (name,)).fetchone()
        base = max(r["expires_at"], now) if r else now
        # nếu khách đang "chưa đặt hạn" (cũ, hạn ~100 năm), đặt lại từ hôm nay
        if r and r["expires_at"] > now + 365 * 50 * DAY:
            base = now
        exp = base + days * DAY
        c.execute("UPDATE clients SET expires_at=?, plan_months=?, locked=0, reminded=0 WHERE name=?",
                  (exp, plan_months, name))
        if price:
            c.execute("INSERT INTO revenue(name,kind,amount,ts) VALUES(?,?,?,?)", (name, "renew", price, now))
    return exp


def set_locked(name, val):
    with _conn() as c:
        c.execute("UPDATE clients SET locked=? WHERE name=?", (1 if val else 0, name))


def set_field(name, field, value):
    if field not in ("contact", "note", "price"):
        return
    with _conn() as c:
        c.execute(f"UPDATE clients SET {field}=? WHERE name=?", (value, name))


def delete(name):
    with _conn() as c:
        c.execute("DELETE FROM clients WHERE name=?", (name,))


def get(name):
    with _conn() as c:
        r = c.execute("SELECT * FROM clients WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None


def all_clients():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM clients ORDER BY expires_at").fetchall()]


def locked_names():
    with _conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM clients WHERE locked=1").fetchall()]


NO_EXPIRY_YEARS = 100


def import_missing(names, node="eaz2"):
    now = int(time.time())
    added = []
    with _conn() as c:
        have = {r["name"] for r in c.execute("SELECT name FROM clients").fetchall()}
        for n in names:
            if n in have:
                continue
            c.execute("""INSERT INTO clients
                (name,node,plan_months,created_at,expires_at,price,contact,note,locked,reminded)
                VALUES(?,?,0,?,?,0,'','cũ — chưa đặt hạn',0,1)""",
                (n, node, now, now + 365 * NO_EXPIRY_YEARS * DAY))
            added.append(n)
    return added


def expiring(within_days=3):
    now = int(time.time())
    lim = now + within_days * DAY
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM clients WHERE expires_at<=? AND expires_at<? ORDER BY expires_at",
            (lim, now + 365 * 50 * DAY)).fetchall()]


def mark_reminded(name):
    with _conn() as c:
        c.execute("UPDATE clients SET reminded=1 WHERE name=?", (name,))


def revenue_month(ts=None):
    ts = ts or time.time()
    lt = time.localtime(ts)
    start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    with _conn() as c:
        rows = c.execute("SELECT kind,COUNT(*) n,COALESCE(SUM(amount),0) s FROM revenue WHERE ts>=? GROUP BY kind",
                         (start,)).fetchall()
        active = c.execute("SELECT COUNT(*) n FROM clients WHERE locked=0 AND expires_at>?",
                           (int(time.time()),)).fetchone()["n"]
    out = {"active": active, "new": (0, 0), "renew": (0, 0), "total": 0}
    for r in rows:
        out[r["kind"]] = (r["n"], r["s"]); out["total"] += r["s"]
    return out


# ---------- routers (modem khách) ----------
KEY_PATH = os.environ.get("ROUTER_KEY_FILE", os.path.join(os.path.dirname(DB_PATH), "router.key"))
ROUTER_FIELDS = ("label", "base", "login", "pass_enc", "iface", "client", "last_check",
                 "last_state", "last_hs", "last_err", "last_reconnect")


def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("ROUTER_KEY")
    if not key:
        if os.path.exists(KEY_PATH):
            key = open(KEY_PATH).read().strip()
        else:
            key = Fernet.generate_key().decode()
            fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
    return Fernet(key.encode())


def enc(s):
    return _fernet().encrypt(s.encode()).decode()


def dec(s):
    return _fernet().decrypt(s.encode()).decode()


def router_add(label, base, login, password, iface="", client=""):
    with _conn() as c:
        c.execute("""INSERT INTO routers(label,base,login,pass_enc,iface,client,created_at)
            VALUES(?,?,?,?,?,?,?)""", (label, base, login, enc(password), iface, client, int(time.time())))


def router_update(rid, **kw):
    kw = {k: v for k, v in kw.items() if k in ROUTER_FIELDS}
    if "password" in kw:
        kw["pass_enc"] = enc(kw.pop("password"))
    if not kw:
        return
    with _conn() as c:
        c.execute(f"UPDATE routers SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), rid))


def router_get(rid):
    with _conn() as c:
        r = c.execute("SELECT * FROM routers WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None


def router_by_label(label):
    with _conn() as c:
        r = c.execute("SELECT * FROM routers WHERE label=?", (label,)).fetchone()
        return dict(r) if r else None


def routers_all():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM routers ORDER BY label").fetchall()]


def router_delete(rid):
    with _conn() as c:
        c.execute("DELETE FROM routers WHERE id=?", (rid,))
