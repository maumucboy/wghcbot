#!/usr/bin/env python3
"""Bot Telegram quản lý AmneziaWG — dashboard, tạo/gia hạn/khoá khách, nhắc hạn."""
import os, time, html, logging, asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, WebAppInfo, LinkPreviewOptions
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, ContextTypes)
import awg, db, routers_ui

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("awgbot")

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "330613623"))
BOT_PASS = os.environ.get("BOT_PASS", "")
NODE = os.environ.get("NODE_NAME", "eaz2")
ENDPOINT = os.environ.get("AWG_ENDPOINT_DOMAIN", "eaz2.hoichamvn.uk")
PANEL_URL = os.environ.get("PANEL_URL", "")  # https://panel.hoichamvn.uk cho nút Mini App

PLAN_LABELS = {1: "1 tháng", 3: "3 tháng", 6: "6 tháng", 12: "12 tháng"}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def name_link(context, name):
    """Tên khách bấm được: mở deep link /start c_<tên> -> hiện thẻ khách."""
    u = context.bot.username
    n = html.escape(name)
    return f'<a href="https://t.me/{u}?start=c_{n}">{n}</a>' if u else f"<code>{n}</code>"

authed = set()  # chat_id đã nhập đúng mật khẩu phiên này


def is_admin(update: Update) -> bool:
    u = update.effective_user
    return u and u.id == ADMIN_ID


def guard(update: Update) -> bool:
    return is_admin(update) and update.effective_chat.id in authed


def fdays(exp):
    d = int((exp - time.time()) / 86400)
    if d > 3650:
        return "chưa đặt hạn"
    if d < 0:
        return f"hết hạn {-d} ngày trước"
    if d == 0:
        return "HẾT HẠN hôm nay"
    return f"còn {d} ngày"


def fdate(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def fsize(n):
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"


def dot(cl, wg):
    if cl["locked"]:
        return "⚫"
    if cl["expires_at"] < time.time():
        return "🟡"
    return "🟢" if wg and wg.get("online") else "🔴"


# ---------- Dashboard ----------
async def show_dashboard(update, context, edit=False):
    wg = awg.wg_status()
    st = awg.server_stats()
    clients = db.all_clients()
    online = sum(1 for c in clients if not c["locked"] and wg.get(c["name"], {}).get("online"))
    head = (f"🖥 <b>{html.escape(ENDPOINT)}</b>\n"
            f"🟢 Online {online}/{len(clients)} · CPU {st['cpu_pct']}% · RAM {st['ram_pct']}% · uptime {st['uptime_d']}d\n"
            f"AWG 2.0 · cổng {awg.AWG_PORT} · node {NODE}\n\n")
    lines = []
    for c in clients[:40]:
        w = wg.get(c["name"], {})
        traf = f" · ↓{fsize(w.get('rx',0))} ↑{fsize(w.get('tx',0))}" if w.get("rx") else ""
        lines.append(f"{dot(c,w)} {name_link(context, c['name'])} · {fdays(c['expires_at'])}{traf}")
    body = "\n".join(lines) if lines else "<i>Chưa có khách.</i>"
    exp_n = len(db.expiring(3))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tạo khách", callback_data="new"),
         InlineKeyboardButton("🔄 Làm mới", callback_data="home")],
        [InlineKeyboardButton(f"📊 Sắp hết hạn ({exp_n})", callback_data="expiring"),
         InlineKeyboardButton("💰 Doanh thu", callback_data="revenue")],
        [InlineKeyboardButton("👥 Danh sách", callback_data="list:0"),
         InlineKeyboardButton("💾 Sao lưu", callback_data="backup")],
        [InlineKeyboardButton(f"🛰 Router khách ({len(db.routers_all())})", callback_data="rt:menu")],
    ] + ([[InlineKeyboardButton("📱 Mở Mini App", web_app=WebAppInfo(url=PANEL_URL))]] if PANEL_URL.startswith("https://") else []))
    text = head + body
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                                      link_preview_options=NO_PREVIEW)
    else:
        await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML", reply_markup=kb,
                                       link_preview_options=NO_PREVIEW)


# ---------- Danh sách / chi tiết ----------
async def show_list(update, context, page=0):
    clients = db.all_clients()
    wg = awg.wg_status()
    per = 10
    chunk = clients[page*per:(page+1)*per]
    rows = [[InlineKeyboardButton(f"{dot(c,wg.get(c['name'],{}))} {c['name']} · {fdays(c['expires_at'])}",
             callback_data=f"c:{c['name']}")] for c in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"list:{page-1}"))
    if (page+1)*per < len(clients):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"list:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Dashboard", callback_data="home")])
    await update.callback_query.edit_message_text(
        f"👥 Khách ({len(clients)})", reply_markup=InlineKeyboardMarkup(rows))


async def show_client(update, context, name, new_msg=False):
    c = db.get(name)
    if not c:
        if new_msg:
            await context.bot.send_message(update.effective_chat.id, f"Không tìm thấy khách {html.escape(name)}.")
        else:
            await update.callback_query.answer("Không tìm thấy", show_alert=True)
        return
    w = awg.wg_status().get(name, {})
    status = "⚫ Đã khoá" if c["locked"] else (
        f"🟢 Online · handshake {w['handshake_s']}s trước" if w.get("online")
        else ("🔴 Offline" + (f" · {w['handshake_s']//60}m trước" if w.get("handshake_s") else " · chưa kết nối")))
    txt = (f"👤 <b>{html.escape(name)}</b>\n{status}\n\n"
           f"Endpoint khách: <code>{w.get('endpoint') or '—'}</code>\n"
           f"IP tunnel: <code>{w.get('ip') or '—'}</code>\n"
           f"Traffic: ↓{fsize(w.get('rx',0))} · ↑{fsize(w.get('tx',0))}\n"
           f"Gói: {c['plan_months']} tháng\n"
           f"Hết hạn: <b>{fdate(c['expires_at'])}</b> · {fdays(c['expires_at'])}\n"
           f"Giá: {c['price']}₽ · Liên hệ: {html.escape(c['contact'] or '—')}\n"
           f"Ghi chú: {html.escape(c['note'] or '—')}")
    lock_btn = (InlineKeyboardButton("▶️ Mở khoá", callback_data=f"unlock:{name}") if c["locked"]
                else InlineKeyboardButton("⏸ Khoá", callback_data=f"lock:{name}"))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕1th", callback_data=f"ext:{name}:1"),
         InlineKeyboardButton("➕3th", callback_data=f"ext:{name}:3"),
         InlineKeyboardButton("➕6th", callback_data=f"ext:{name}:6"),
         InlineKeyboardButton("➕12th", callback_data=f"ext:{name}:12")],
        [InlineKeyboardButton("📅 Gia hạn số ngày", callback_data=f"extd:{name}")],
        [lock_btn, InlineKeyboardButton("🗑 Xoá", callback_data=f"del:{name}")],
        [InlineKeyboardButton("📎 Tải config", callback_data=f"conf:{name}"),
         InlineKeyboardButton("📝 Ghi chú", callback_data=f"note:{name}")],
        [InlineKeyboardButton("🏠 Dashboard", callback_data="home")],
    ])
    if new_msg:
        await context.bot.send_message(update.effective_chat.id, txt, parse_mode="HTML", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)


async def send_config(update, context, name):
    conf = f"{awg.CONF_DIR}/{name}.conf"
    chat = update.effective_chat.id
    if not os.path.exists(conf):
        await context.bot.send_message(chat, "Không thấy file config."); return
    with open(conf, "rb") as f:
        await context.bot.send_document(chat, InputFile(f, filename=f"{name}.conf"),
                                        caption=f"Config {name} · {ENDPOINT}:{awg.AWG_PORT}")
    png = f"{awg.CONF_DIR}/{name}.png"
    if os.path.exists(png):
        with open(png, "rb") as f:
            await context.bot.send_photo(chat, InputFile(f, filename=f"{name}.png"))


# ---------- Tạo khách ----------
async def ask_new_name(update, context):
    context.user_data["await"] = "name"
    await update.callback_query.edit_message_text(
        "➕ Gửi <b>tên khách</b> (không dấu, không cách, vd: petrov_ip):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Huỷ", callback_data="home")]]))


async def ask_plan(update, context, name):
    context.user_data["new_name"] = name
    rows = [[InlineKeyboardButton(f"{PLAN_LABELS[m]} — {db.get_price(m)}₽", callback_data=f"mk:{m}")]
            for m in (1, 3, 6, 12)]
    rows.append([InlineKeyboardButton("📅 Số ngày tuỳ chọn", callback_data="mk:days")])
    rows.append([InlineKeyboardButton("🎁 Dùng thử 3 ngày", callback_data="mk:trial")])
    rows.append([InlineKeyboardButton("⬅️ Huỷ", callback_data="home")])
    await context.bot.send_message(update.effective_chat.id,
        f"Khách: <b>{html.escape(name)}</b>\nChọn thời hạn:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def do_create(update, context, plan):
    name = context.user_data.get("new_name")
    if not name:
        await update.callback_query.answer("Hết phiên, tạo lại", show_alert=True); return
    if plan == "days":
        context.user_data["await"] = "cdays"
        await update.callback_query.edit_message_text(f"📅 Nhập số ngày cho <b>{html.escape(name)}</b> (vd 45):",
                                                      parse_mode="HTML")
        return
    await update.callback_query.edit_message_text(f"⏳ Đang tạo {name}…")
    ok, conf, err = awg.add_client(name)
    if not ok:
        await update.callback_query.edit_message_text(f"❌ Lỗi: {html.escape(str(err))}"); return
    if plan == "trial":
        exp = db.add(name, 0, 0, NODE, trial_days=3)
    else:
        m = int(plan); exp = db.add(name, m, db.get_price(m), NODE)
    await update.callback_query.edit_message_text(
        f"✅ Đã tạo <b>{html.escape(name)}</b>\nHết hạn {fdate(exp)} · {fdays(exp)}",
        parse_mode="HTML")
    await send_config(update, context, name)
    context.user_data.pop("new_name", None)


# ---------- Expiring / revenue ----------
async def show_expiring(update, context):
    lst = db.expiring(7)
    if not lst:
        await update.callback_query.edit_message_text("✅ Không có khách sắp hết hạn (7 ngày).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠", callback_data="home")]])); return
    lines, rows = [], []
    for c in lst:
        lines.append(f"{dot(c,{})} <code>{c['name']}</code> — {fdays(c['expires_at'])}")
        rows.append([InlineKeyboardButton(f"🔁 Gia hạn {c['name']}", callback_data=f"c:{c['name']}")])
    rows.append([InlineKeyboardButton("🏠 Dashboard", callback_data="home")])
    await update.callback_query.edit_message_text("📊 <b>Sắp hết hạn (7 ngày)</b>\n\n" + "\n".join(lines),
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def show_revenue(update, context):
    r = db.revenue_month()
    txt = (f"💰 <b>Doanh thu tháng {time.strftime('%m')}</b>\n\n"
           f"Khách đang hoạt động: {r['active']}\n"
           f"Gia hạn: {r['renew'][0]} · {r['renew'][1]}₽\n"
           f"Khách mới: {r['new'][0]} · {r['new'][1]}₽\n"
           f"<b>Tổng thu: {r['total']}₽</b>")
    await update.callback_query.edit_message_text(txt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Dashboard", callback_data="home")]]))


# ---------- Handlers ----------
async def cmd_start(update, context):
    if not is_admin(update):
        return
    arg = context.args[0] if context.args else ""
    if arg.startswith("c_") and update.effective_chat.id in authed:
        try:
            await update.message.delete()  # xoá dòng "/start c_..." cho chat gọn
        except Exception:
            pass
        await show_client(update, context, arg[2:], new_msg=True); return
    if update.effective_chat.id in authed:
        await show_dashboard(update, context); return
    if not BOT_PASS:
        authed.add(update.effective_chat.id); await show_dashboard(update, context); return
    context.user_data["await"] = "pass"
    await update.message.reply_text("🔒 Nhập mật khẩu quản lý:")


async def on_text(update, context):
    if not is_admin(update):
        return
    st = context.user_data.get("await")
    txt = (update.message.text or "").strip()
    if st == "pass":
        if txt == BOT_PASS:
            authed.add(update.effective_chat.id); context.user_data.pop("await", None)
            await show_dashboard(update, context)
        else:
            await update.message.reply_text("❌ Sai mật khẩu.")
        return
    if update.effective_chat.id not in authed:
        return
    if st == "name":
        import re
        if not re.fullmatch(r"[A-Za-z0-9_]{2,32}", txt):
            await update.message.reply_text("Tên chỉ gồm chữ, số, gạch dưới (2–32). Gửi lại:"); return
        if db.get(txt):
            await update.message.reply_text("Tên đã tồn tại. Gửi tên khác:"); return
        context.user_data.pop("await", None)
        await ask_plan(update, context, txt)
    elif st == "rt_add":
        await routers_ui.on_add_text(update, context, txt)
    elif st and st.startswith("note:"):
        name = st.split(":", 1)[1]; db.set_field(name, "note", txt)
        context.user_data.pop("await", None)
        await update.message.reply_text(f"✅ Đã lưu ghi chú cho {name}.")
    elif st == "cdays":
        name = context.user_data.get("new_name")
        try:
            d = int(txt)
        except ValueError:
            await update.message.reply_text("Nhập số nguyên ngày:"); return
        if not (1 <= d <= 3650):
            await update.message.reply_text("Số ngày 1–3650:"); return
        context.user_data.pop("await", None); context.user_data.pop("new_name", None)
        await update.message.reply_text(f"⏳ Đang tạo {name}…")
        ok, conf, err = awg.add_client(name)
        if not ok:
            await update.message.reply_text(f"❌ {html.escape(str(err))}"); return
        exp = db.add(name, 0, 0, NODE, days=d)
        await update.message.reply_text(f"✅ {name} · hạn {fdate(exp)} · {fdays(exp)}")
        await send_config(update, context, name)
    elif st and st.startswith("edays:"):
        name = st.split(":", 1)[1]
        try:
            d = int(txt)
        except ValueError:
            await update.message.reply_text("Nhập số nguyên ngày:"); return
        if not (1 <= d <= 3650):
            await update.message.reply_text("Số ngày 1–3650:"); return
        context.user_data.pop("await", None)
        exp = db.extend(name, 0, 0, days=d)
        w = awg.wg_status().get(name, {})
        if w.get("ip"):
            awg.unlock_client(w["ip"])
        db.set_locked(name, 0)
        await update.message.reply_text(f"✅ Gia hạn {name} tới {fdate(exp)} · {fdays(exp)}")


async def on_cb(update, context):
    q = update.callback_query
    if not guard(update):
        await q.answer("Gõ /start trước", show_alert=True); return
    await q.answer()
    d = q.data
    try:
        if d.startswith("rt:"):
            await routers_ui.on_cb(update, context, d)
        elif d == "home":
            await show_dashboard(update, context, edit=True)
        elif d == "new":
            await ask_new_name(update, context)
        elif d.startswith("mk:"):
            await do_create(update, context, d.split(":", 1)[1])
        elif d == "expiring":
            await show_expiring(update, context)
        elif d == "revenue":
            await show_revenue(update, context)
        elif d == "backup":
            await q.edit_message_text("⏳ Đang sao lưu…")
            path = awg.backup()
            if path:
                with open(path, "rb") as f:
                    await context.bot.send_document(update.effective_chat.id,
                        InputFile(f, filename=os.path.basename(path)), caption="💾 Backup")
            await show_dashboard(update, context)
        elif d.startswith("list:"):
            await show_list(update, context, int(d.split(":")[1]))
        elif d.startswith("c:"):
            await show_client(update, context, d[2:])
        elif d.startswith("conf:"):
            await send_config(update, context, d.split(":", 1)[1])
        elif d.startswith("ext:"):
            _, name, m = d.split(":"); m = int(m)
            exp = db.extend(name, m, db.get_price(m))
            c = db.get(name)
            if c and c["locked"]:
                db.set_locked(name, 0)
                if c.get("note") is not None and awg.wg_status().get(name, {}).get("ip"):
                    awg.unlock_client(awg.wg_status()[name]["ip"])
            await q.answer(f"Đã gia hạn tới {fdate(exp)}", show_alert=True)
            await show_client(update, context, name)
        elif d.startswith("extd:"):
            name = d.split(":", 1)[1]
            context.user_data["await"] = f"edays:{name}"
            await context.bot.send_message(update.effective_chat.id, f"📅 Nhập số ngày gia hạn cho {name}:")
        elif d.startswith("lock:"):
            name = d.split(":", 1)[1]; w = awg.wg_status().get(name, {})
            if w.get("ip"):
                awg.lock_client(w["ip"])
            db.set_locked(name, 1)
            await show_client(update, context, name)
        elif d.startswith("unlock:"):
            name = d.split(":", 1)[1]; w = awg.wg_status().get(name, {})
            if w.get("ip"):
                awg.unlock_client(w["ip"])
            db.set_locked(name, 0)
            await show_client(update, context, name)
        elif d.startswith("del:"):
            name = d.split(":", 1)[1]
            w = awg.wg_status().get(name, {})
            if w.get("ip"):
                awg.unlock_client(w["ip"])
            awg.remove_client(name); db.delete(name)
            await q.answer("Đã xoá", show_alert=True)
            await show_dashboard(update, context, edit=True)
        elif d.startswith("note:"):
            name = d.split(":", 1)[1]
            context.user_data["await"] = f"note:{name}"
            await context.bot.send_message(update.effective_chat.id, f"Gửi ghi chú cho {name}:")
    except Exception as e:
        log.exception("cb error")
        await context.bot.send_message(update.effective_chat.id, f"⚠️ Lỗi: {html.escape(str(e))}")


# ---------- Jobs ----------
async def job_expiry(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    wg = awg.wg_status()
    for c in db.all_clients():
        # khoá khi hết hạn
        if not c["locked"] and c["expires_at"] < now:
            ip = wg.get(c["name"], {}).get("ip")
            if ip:
                awg.lock_client(ip)
            db.set_locked(c["name"], 1)
            await context.bot.send_message(ADMIN_ID,
                f"⚫ <code>{c['name']}</code> đã hết hạn — đã khoá.", parse_mode="HTML")
        # nhắc trước 3 ngày
        elif not c["locked"] and not c["reminded"] and now < c["expires_at"] <= now + 3*86400:
            db.mark_reminded(c["name"])
            await context.bot.send_message(ADMIN_ID,
                f"🔔 <code>{c['name']}</code> {fdays(c['expires_at'])} ({fdate(c['expires_at'])})",
                parse_mode="HTML")


async def job_node_watch(context: ContextTypes.DEFAULT_TYPE):
    """Cảnh báo nếu số khách online tụt mạnh so với lần trước (nghi IP bị chặn)."""
    wg = awg.wg_status()
    active = [c for c in db.all_clients() if not c["locked"] and c["expires_at"] > time.time()]
    online = sum(1 for c in active if wg.get(c["name"], {}).get("online"))
    prev = context.bot_data.get("online_baseline")
    context.bot_data["online_baseline"] = online
    if prev is not None and prev >= 4 and online <= prev * 0.4:
        await context.bot.send_message(ADMIN_ID,
            f"⚠️ Online tụt mạnh: {prev} → {online}. Nghi IP node <b>{NODE}</b> bị chặn.\n"
            f"Kiểm tra: ping {ENDPOINT} từ SIM 4G, hoặc đổi IP + sửa DNS.", parse_mode="HTML")


async def on_start(app: Application):
    db.init()
    # nạp khách đã có trên server nhưng chưa trong DB (tạo trước bot)
    wg = awg.wg_status()
    imp = db.import_missing(list(wg.keys()), NODE)
    if imp:
        log.info("imported existing clients: %s", imp)
    locked = [wg[n]["ip"] for n in db.locked_names() if wg.get(n, {}).get("ip")]
    allips = [v["ip"] for v in wg.values() if v.get("ip")]
    try:
        awg.reconcile_locks(locked, allips)
    except Exception:
        log.exception("reconcile_locks")
    await app.bot.send_message(ADMIN_ID, "✅ Bot đã khởi động. Gõ /start.")


def main():
    app = Application.builder().token(TOKEN).post_init(on_start).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(job_expiry, interval=1800, first=30)
    app.job_queue.run_repeating(job_node_watch, interval=600, first=120)
    app.job_queue.run_repeating(routers_ui.job_watchdog, interval=300, first=180, data=ADMIN_ID)
    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
