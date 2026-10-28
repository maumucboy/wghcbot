"""SQLite lưu thông tin kinh doanh của khách (hạn, gói, giá, ghi chú, khoá)."""
import sqlite3, time, os

DB_PATH = os.environ.get("AWG_BOT_DB", "/root/awgbot/bot.db")
DAY = 86400
# tháng -> số ngày (xấp xỉ 30)
PLAN_DAYS = {1: 30, 3: 90, 6: 180, 12: 360}


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS clients(
            name TEXT PRIMARY KEY,
            node TEXT DEFAULT 'eaz2',
            plan_months INTEGER,
            created_at INTEGER,
            expires_at INTEGER,
            price INTEGER DEFAULT 0,
            contact TEXT DEFAULT '',
            note TEXT DEFAULT '',
            locked INTEGER DEFAULT 0,
            reminded INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS revenue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, kind TEXT, amount INTEGER, ts INTEGER
        )""")


def add(name, plan_months, price=0, node="eaz2", trial_days=None):
    now = int(time.time())
    days = trial_days if trial_days else PLAN_DAYS.get(plan_months, 30)
    exp = now + days * DAY
    with _conn() as c:
        c.execute("""INSERT OR REPLACE INTO clients
            (name,node,plan_months,created_at,expires_at,price,contact,note,locked,reminded)
            VALUES(?,?,?,?,?,?,'','',0,0)""",
            (name, node, plan_months or 0, now, exp, price))
        if price:
            c.execute("INSERT INTO revenue(name,kind,amount,ts) VALUES(?,?,?,?)",
                      (name, "new", price, now))
    return exp


def extend(name, plan_months, price=0):
    now = int(time.time())
    days = PLAN_DAYS.get(plan_months, 30)
    with _conn() as c:
        r = c.execute("SELECT expires_at FROM clients WHERE name=?", (name,)).fetchone()
        base = max(r["expires_at"], now) if r else now  # gia hạn cộng dồn nếu còn hạn
        exp = base + days * DAY
        c.execute("UPDATE clients SET expires_at=?, plan_months=?, locked=0, reminded=0 WHERE name=?",
                  (exp, plan_months, name))
        if price:
            c.execute("INSERT INTO revenue(name,kind,amount,ts) VALUES(?,?,?,?)",
                      (name, "renew", price, now))
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


NO_EXPIRY = int(time.time()) + 365 * 100 * DAY  # ~100 năm = coi như chưa đặt hạn


def import_missing(names, node="eaz2"):
    """Nạp khách đã có trên server nhưng chưa có trong DB (tạo trước bot)."""
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
                (n, node, now, now + 365 * 100 * DAY))
            added.append(n)
    return added


def expiring(within_days=3):
    now = int(time.time())
    lim = now + within_days * DAY
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM clients WHERE expires_at<=? ORDER BY expires_at", (lim,)).fetchall()]


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
