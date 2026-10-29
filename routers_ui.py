"""Quản lý modem/router Keenetic của khách: danh sách, thêm, kiểm tra, reconnect hàng loạt, watchdog."""
import asyncio, html, os, re, secrets, time, logging
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB
import db, keenetic

log = logging.getLogger("awgbot.routers")

ENDPOINT = os.environ.get("AWG_ENDPOINT_DOMAIN", "eaz2.hoichamvn.uk")
BOT_LOGIN = os.environ.get("ROUTER_LOGIN", "hcbot")
PARALLEL = int(os.environ.get("ROUTER_PARALLEL", "8"))
COOLDOWN = 900  # watchdog không reconnect lại 1 router trong 15 phút

ICON = {"ok": "🟢", "stale": "🟡", "offline": "🔴", "auth": "🔑", "iface": "❓",
        "rci": "⚠️", "handshake": "🟡", "new": "⚪"}


def stale_limit():
    return int(db.get_setting("router_stale_s", "300"))


def watchdog_on():
    return db.get_setting("router_watchdog", "0") == "1"


def _router(r):
    return keenetic.Router(r["base"], r["login"], db.dec(r["pass_enc"]))


def _label_ok(s):
    return re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", s or "")


# ---------- công việc đồng bộ (chạy trong thread) ----------
def check_one(r):
    """Đăng nhập + đọc trạng thái. Cập nhật DB. Trả (state, status|None, err)."""
    now = int(time.time())
    try:
        rt = _router(r)
        rt.auth()
        iface = r["iface"] or rt.detect_iface(ENDPOINT)
        if iface != r["iface"]:
            db.router_update(r["id"], iface=iface)
        st = rt.wg_status(iface)
        state = "stale" if keenetic.is_stale(st, stale_limit()) else "ok"
        db.router_update(r["id"], last_check=now, last_state=state, last_hs=st["handshake_s"], last_err="")
        return state, st, ""
    except keenetic.RouterError as e:
        db.router_update(r["id"], last_check=now, last_state=e.code, last_err=str(e)[:200])
        return e.code, None, str(e)
    except Exception as e:
        log.exception("check_one %s", r["label"])
        db.router_update(r["id"], last_check=now, last_state="rci", last_err=str(e)[:200])
        return "rci", None, str(e)


def reconnect_one(r, only_stale=False):
    """Trả (kết quả, giây, chi tiết): kết quả = done | skip | <mã lỗi>."""
    t0 = time.monotonic()
    now = int(time.time())
    try:
        rt = _router(r)
        rt.auth()
        iface = r["iface"] or rt.detect_iface(ENDPOINT)
        if iface != r["iface"]:
            db.router_update(r["id"], iface=iface)
        if only_stale:
            st = rt.wg_status(iface)
            if not keenetic.is_stale(st, stale_limit()):
                db.router_update(r["id"], last_check=now, last_state="ok", last_hs=st["handshake_s"], last_err="")
                return "skip", 0, keenetic.fmt_hs(st["handshake_s"])
        st = rt.reconnect(iface)
        db.router_update(r["id"], last_check=int(time.time()), last_state="ok", last_hs=st["handshake_s"],
                         last_err="", last_reconnect=int(time.time()))
        return "done", round(time.monotonic() - t0), keenetic.fmt_hs(st["handshake_s"])
    except keenetic.RouterError as e:
        db.router_update(r["id"], last_check=int(time.time()), last_state=e.code, last_err=str(e)[:200],
                         last_reconnect=now)
        return e.code, round(time.monotonic() - t0), str(e)
    except Exception as e:
        log.exception("reconnect_one %s", r["label"])
        db.router_update(r["id"], last_check=int(time.time()), last_state="rci", last_err=str(e)[:200])
        return "rci", round(time.monotonic() - t0), str(e)


async def run_many(fn, routers, progress=None):
    sem = asyncio.Semaphore(PARALLEL)
    done = 0
    results = {}

    async def one(r):
        nonlocal done
        async with sem:
            results[r["id"]] = await asyncio.to_thread(fn, r)
        done += 1
        if progress:
            await progress(done, len(routers))

    await asyncio.gather(*(one(r) for r in routers))
    return results


# ---------- màn hình ----------
def summary_line():
    rs = db.routers_all()
    if not rs:
        return "Chưa có router nào."
    cnt = {}
    for r in rs:
        k = "ok" if r["last_state"] == "ok" else ("stale" if r["last_state"] in ("stale", "handshake") else
             ("new" if r["last_state"] == "new" else "bad"))
        cnt[k] = cnt.get(k, 0) + 1
    last = max((r["last_check"] for r in rs), default=0)
    ago = f"{int((time.time() - last) // 60)} phút trước" if last else "chưa kiểm tra"
    parts = [f"🟢 {cnt.get('ok', 0)} ổn"]
    if cnt.get("stale"):
        parts.append(f"🟡 {cnt['stale']} tunnel treo")
    if cnt.get("bad"):
        parts.append(f"🔴 {cnt['bad']} không vào được")
    if cnt.get("new"):
        parts.append(f"⚪ {cnt['new']} chưa kiểm tra")
    return " · ".join(parts) + f"\nKiểm tra lần cuối: {ago}"


async def show_menu(update, context, edit=True):
    n = len(db.routers_all())
    txt = f"🛰 <b>Router khách ({n})</b>\n{summary_line()}"
    kb = KB([
        [B("📋 Danh sách", callback_data="rt:list:0"), B("🔍 Kiểm tra tất cả", callback_data="rt:checkall")],
        [B("🔁 Reconnect tất cả", callback_data="rt:ask:all")],
        [B("🟡 Chỉ cái đang treo", callback_data="rt:ask:stale")],
        [B("➕ Thêm router", callback_data="rt:add"),
         B(f"🐕 Watchdog: {'BẬT' if watchdog_on() else 'TẮT'}", callback_data="rt:wd")],
        [B("🏠 Dashboard", callback_data="home")],
    ])
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
    else:
        await context.bot.send_message(update.effective_chat.id, txt, parse_mode="HTML", reply_markup=kb)


def _row_text(r):
    hs = f" · {keenetic.fmt_hs(r['last_hs'])}" if r["last_state"] in ("ok", "stale") else ""
    return f"{ICON.get(r['last_state'], '⚪')} {r['label']}{hs}"


async def show_list(update, context, page=0):
    rs = db.routers_all()
    per = 10
    rows = [[B(_row_text(r), callback_data=f"rt:r:{r['id']}")] for r in rs[page * per:(page + 1) * per]]
    nav = []
    if page > 0:
        nav.append(B("⬅️", callback_data=f"rt:list:{page - 1}"))
    if (page + 1) * per < len(rs):
        nav.append(B("➡️", callback_data=f"rt:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("⬅️ Router", callback_data="rt:menu")])
    await update.callback_query.edit_message_text(f"📋 Router ({len(rs)})", reply_markup=KB(rows))


async def show_router(update, context, rid, note=""):
    r = db.router_get(rid)
    if not r:
        await update.callback_query.answer("Không tìm thấy", show_alert=True); return
    st = r["last_state"]
    stxt = {"ok": "🟢 Tunnel chạy", "stale": "🟡 Tunnel treo", "offline": "🔴 Không vào được router",
            "auth": "🔑 Sai user/mật khẩu", "iface": "❓ Chưa xác định tunnel", "rci": "⚠️ Lỗi lệnh",
            "handshake": "🟡 Bật rồi nhưng chưa handshake", "new": "⚪ Chưa kiểm tra"}.get(st, st)
    chk = time.strftime("%d.%m %H:%M", time.localtime(r["last_check"])) if r["last_check"] else "—"
    txt = (f"🛰 <b>{html.escape(r['label'])}</b>\n{stxt}\n\n"
           f"Địa chỉ: <code>{html.escape(r['base'])}</code>\n"
           f"User bot: <code>{html.escape(r['login'])}</code>\n"
           f"Tunnel: <code>{html.escape(r['iface'] or 'tự dò')}</code>\n"
           f"Handshake: {keenetic.fmt_hs(r['last_hs']) if st in ('ok', 'stale') else '—'}\n"
           f"Kiểm tra lúc: {chk}"
           + (f"\nKhách: <code>{html.escape(r['client'])}</code>" if r["client"] else "")
           + (f"\n\n<i>{html.escape(r['last_err'])}</i>" if r["last_err"] and st != "ok" else "")
           + (f"\n\n{note}" if note else ""))
    kb = KB([
        [B("🔍 Kiểm tra", callback_data=f"rt:chk:{rid}"), B("🔁 Reconnect", callback_data=f"rt:rc:{rid}")],
        [B("🔑 Xem mật khẩu bot", callback_data=f"rt:pw:{rid}"), B("🗑 Xoá", callback_data=f"rt:del:{rid}")],
        [B("⬅️ Danh sách", callback_data="rt:list:0")],
    ])
    await update.callback_query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb,
                                                   disable_web_page_preview=True)


async def ask_add(update, context):
    context.user_data["await"] = "rt_add"
    await update.callback_query.edit_message_text(
        "➕ <b>Thêm router</b>\nGửi 1 dòng:\n"
        "<code>địa_chỉ tên_khách [user] [mật_khẩu]</code>\n\n"
        "• <code>ivan123.keenetic.pro ivan</code> → bot sinh mật khẩu cho user <code>hcbot</code>\n"
        "• <code>ivan123.keenetic.pro ivan Pass123</code> → user <code>hcbot</code>\n"
        "• <code>ivan123.keenetic.pro ivan admin Pass123</code> → user tuỳ chọn",
        parse_mode="HTML", reply_markup=KB([[B("⬅️ Huỷ", callback_data="rt:menu")]]))


async def on_add_text(update, context, txt):
    parts = txt.split()
    if len(parts) not in (2, 3, 4):
        await update.message.reply_text("Sai định dạng. Gửi: địa_chỉ tên_khách [user] [mật_khẩu]"); return
    addr, label = parts[0], parts[1]
    if not re.fullmatch(r"(https?://)?[A-Za-z0-9.-]+(:\d+)?/?", addr) or "." not in addr:
        await update.message.reply_text("Địa chỉ không hợp lệ. Ví dụ: ivan123.keenetic.pro"); return
    if not _label_ok(label):
        await update.message.reply_text("Tên chỉ gồm chữ, số, _ . - (2–32)."); return
    if db.router_by_label(label):
        await update.message.reply_text("Tên đã có. Chọn tên khác."); return
    gen = len(parts) == 2
    login = parts[2] if len(parts) == 4 else BOT_LOGIN
    pwd = parts[-1] if not gen else secrets.token_urlsafe(12)
    client = label if db.get(label) else ""
    db.router_add(label, keenetic.norm_base(addr), login, pwd, client=client)
    context.user_data.pop("await", None)
    r = db.router_by_label(label)
    if gen:
        await update.message.reply_text(
            f"✅ Đã lưu <b>{html.escape(label)}</b>.\n\nTrên router tạo user:\n"
            f"• Tên: <code>{BOT_LOGIN}</code>\n• Mật khẩu: <code>{html.escape(pwd)}</code>\n"
            f"• Quyền: Web-интерфейс (truy cập web)\n\nTạo xong bấm Kiểm tra.",
            parse_mode="HTML", reply_markup=KB([[B("🔍 Kiểm tra", callback_data=f"rt:chk:{r['id']}")],
                                                [B("⬅️ Router", callback_data="rt:menu")]]))
    else:
        try:
            await update.message.delete()  # không để mật khẩu nằm trong chat
        except Exception:
            pass
        msg = await context.bot.send_message(update.effective_chat.id, f"⏳ Đang kiểm tra {label}…")
        state, st, err = await asyncio.to_thread(check_one, r)
        await msg.edit_text(_check_text(label, state, st, err), parse_mode="HTML",
                            reply_markup=KB([[B("⬅️ Router", callback_data="rt:menu")]]))


def _check_text(label, state, st, err):
    if state in ("ok", "stale"):
        r = db.router_by_label(label)
        return (f"{ICON[state]} <b>{html.escape(label)}</b>: vào được router, tunnel "
                f"<code>{html.escape(r['iface'])}</code> · handshake {keenetic.fmt_hs(st['handshake_s'])}")
    return f"{ICON.get(state, '⚠️')} <b>{html.escape(label)}</b>: {html.escape(err)}"


async def ask_bulk(update, context, mode):
    rs = db.routers_all()
    if not rs:
        await update.callback_query.answer("Chưa có router", show_alert=True); return
    what = "TẤT CẢ" if mode == "all" else "các router có tunnel treo"
    txt = (f"🔁 Reconnect {what} ({len(rs)} router)?\n\n"
           "Mỗi router: đăng nhập → tắt tunnel → 3 giây → bật → chờ handshake.\n"
           + ("Khách sẽ mất mạng khoảng 5–10 giây." if mode == "all"
              else "Router đang chạy ổn sẽ được bỏ qua, không mất mạng."))
    await update.callback_query.edit_message_text(txt, reply_markup=KB([
        [B("✅ Chạy", callback_data=f"rt:go:{mode}"), B("⬅️ Huỷ", callback_data="rt:menu")]]))


async def run_bulk(update, context, mode, ids=None):
    q = update.callback_query
    rs = db.routers_all()
    if ids:
        rs = [r for r in rs if r["id"] in ids]
    only_stale = mode == "stale"
    last_edit = [0.0]

    async def progress(done, total):
        if time.monotonic() - last_edit[0] > 1.5 or done == total:
            last_edit[0] = time.monotonic()
            try:
                await q.edit_message_text(f"⏳ Đang chạy… {done}/{total}")
            except Exception:
                pass

    await q.edit_message_text(f"⏳ Đang chạy… 0/{len(rs)}")
    res = await run_many(lambda r: reconnect_one(r, only_stale), rs, progress)
    await q.edit_message_text(**bulk_report(rs, res, context))


def bulk_report(rs, res, context):
    ok = [r for r in rs if res[r["id"]][0] == "done"]
    skip = [r for r in rs if res[r["id"]][0] == "skip"]
    bad = [r for r in rs if res[r["id"]][0] not in ("done", "skip")]
    lines = [f"<b>Xong: {len(ok)} reconnect · {len(skip)} đang ổn · {len(bad)} lỗi</b>"]
    for r in ok[:25]:
        lines.append(f"✓ {html.escape(r['label'])} · {res[r['id']][1]} giây")
    if len(ok) > 25:
        lines.append(f"… thêm {len(ok) - 25} ✓")
    for r in bad:
        code, _, det = res[r["id"]]
        contact = ""
        c = db.get(r["client"]) if r["client"] else None
        if c and c.get("contact"):
            contact = f" · liên hệ {html.escape(c['contact'])}"
        lines.append(f"{ICON.get(code, '✗')} {html.escape(r['label'])} · {html.escape(det)}{contact}")
    context.bot_data["rt_failed"] = [r["id"] for r in bad]
    rows = []
    if bad:
        rows.append([B(f"🔁 Thử lại {len(bad)} lỗi", callback_data="rt:retry")])
    rows.append([B("⬅️ Router", callback_data="rt:menu")])
    return {"text": "\n".join(lines), "parse_mode": "HTML", "reply_markup": KB(rows)}


async def check_all(update, context):
    q = update.callback_query
    rs = db.routers_all()
    if not rs:
        await q.answer("Chưa có router", show_alert=True); return
    await q.edit_message_text(f"⏳ Đang kiểm tra {len(rs)} router…")
    await run_many(check_one, rs)
    await show_menu(update, context)


# ---------- router callback ----------
async def on_cb(update, context, d):
    q = update.callback_query
    if d == "rt:menu":
        await show_menu(update, context)
    elif d.startswith("rt:list:"):
        await show_list(update, context, int(d.split(":")[2]))
    elif d.startswith("rt:r:"):
        await show_router(update, context, int(d.split(":")[2]))
    elif d == "rt:add":
        await ask_add(update, context)
    elif d == "rt:wd":
        db.set_setting("router_watchdog", "0" if watchdog_on() else "1")
        await show_menu(update, context)
    elif d == "rt:checkall":
        await check_all(update, context)
    elif d.startswith("rt:ask:"):
        await ask_bulk(update, context, d.split(":")[2])
    elif d.startswith("rt:go:"):
        await run_bulk(update, context, d.split(":")[2])
    elif d == "rt:retry":
        ids = context.bot_data.get("rt_failed") or []
        if not ids:
            await q.answer("Không có router lỗi", show_alert=True); return
        await run_bulk(update, context, "all", ids)
    elif d.startswith("rt:chk:"):
        rid = int(d.split(":")[2]); r = db.router_get(rid)
        await q.edit_message_text(f"⏳ Đang kiểm tra {r['label']}…")
        state, st, err = await asyncio.to_thread(check_one, r)
        await show_router(update, context, rid, note=_check_text(r["label"], state, st, err))
    elif d.startswith("rt:rc:"):
        rid = int(d.split(":")[2]); r = db.router_get(rid)
        await q.edit_message_text(f"⏳ Đang reconnect {r['label']}…")
        code, sec, det = await asyncio.to_thread(reconnect_one, r)
        note = (f"✓ Reconnect xong sau {sec} giây · handshake {det}" if code == "done"
                else f"{ICON.get(code, '✗')} {html.escape(det)}")
        await show_router(update, context, rid, note=note)
    elif d.startswith("rt:pw:"):
        r = db.router_get(int(d.split(":")[2]))
        await context.bot.send_message(update.effective_chat.id,
            f"🔑 {html.escape(r['label'])}: <code>{html.escape(r['login'])}</code> / "
            f"<tg-spoiler>{html.escape(db.dec(r['pass_enc']))}</tg-spoiler>", parse_mode="HTML")
    elif d.startswith("rt:del:"):
        rid = int(d.split(":")[2]); r = db.router_get(rid)
        await q.edit_message_text(f"Xoá router {r['label']} khỏi bot? (router của khách không bị đụng)",
            reply_markup=KB([[B("🗑 Xoá", callback_data=f"rt:delok:{rid}"),
                              B("⬅️ Huỷ", callback_data=f"rt:r:{rid}")]]))
    elif d.startswith("rt:delok:"):
        db.router_delete(int(d.split(":")[2]))
        await show_list(update, context, 0)


# ---------- watchdog ----------
async def job_watchdog(context):
    if not watchdog_on():
        return
    rs = db.routers_all()
    if not rs:
        return
    prev = {r["id"]: r["last_state"] for r in rs}
    checks = await run_many(check_one, rs)
    now = time.time()
    todo = [r for r in rs if checks[r["id"]][0] in ("stale", "handshake")
            and now - (r["last_reconnect"] or 0) > COOLDOWN]
    lines = []
    if todo:
        res = await run_many(reconnect_one, todo)
        for r in todo:
            code, sec, det = res[r["id"]]
            lines.append(f"✓ {r['label']} reconnect · {det}" if code == "done"
                         else f"✗ {r['label']} · {det}")
    for r in rs:  # báo khi router chuyển sang không vào được (chỉ 1 lần mỗi lần đổi trạng thái)
        st = checks[r["id"]][0]
        if st in ("offline", "auth", "iface", "rci") and prev[r["id"]] != st:
            lines.append(f"{ICON.get(st, '✗')} {r['label']} · {checks[r['id']][2]}")
    if lines:
        await context.bot.send_message(context.job.data, "🐕 <b>Watchdog router</b>\n" +
                                       html.escape("\n".join(lines)), parse_mode="HTML")
