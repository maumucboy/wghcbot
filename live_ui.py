"""📡 Đang kết nối: khách vào IP server nào, tốc độ, top dùng nhiều, khách không hoạt động + khối kết nối trong thẻ khách."""
import asyncio, html, re, time
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, LinkPreviewOptions
import awg, db, traffic

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
MEASURE_S = 5
NAMES_PER_GROUP = 25
GROUP_ICON = {"direct": ["🟢", "🟡", "🟠", "🟣"], "relay": "🔵", "unknown": "⚪"}


def _link(context, name):
    u = context.bot.username
    n = html.escape(name)
    return f'<a href="https://t.me/{u}?start=c_{n}">{n}</a>' if u else f"<code>{n}</code>"


def _ago(s):
    if s is None:
        return "chưa từng kết nối"
    if s < 60:
        return f"{s} giây trước"
    if s < 3600:
        return f"{s // 60} phút trước"
    if s < 86400:
        return f"{s // 3600} giờ trước"
    return f"{s // 86400} ngày trước"


def _groups(dump, rates, ct):
    """-> [(key, icon, title, [names], down, up)] theo IP server/relay."""
    now = time.time()
    g = {}
    for _pub, (name, ep, hs, _rx, _tx) in dump.items():
        if not hs or now - hs > awg.HANDSHAKE_ONLINE:
            continue
        kind, label, sip = traffic.route_of(ep, ct)
        key = (kind, label if kind != "direct" else sip)
        g.setdefault(key, []).append(name)
    direct_keys = sorted([k for k in g if k[0] == "direct"], key=lambda k: -len(g[k]))
    out = []
    for i, k in enumerate(direct_keys + [k for k in g if k[0] == "relay"] + [k for k in g if k[0] == "unknown"]):
        kind = k[0]
        if kind == "direct":
            icon = GROUP_ICON["direct"][min(i, 3)]
            lb = traffic.SERVER_LABELS.get(k[1])
            title = f"<code>{k[1]}</code>" + (f" ({html.escape(lb)})" if lb else "")
        elif kind == "relay":
            icon, title = GROUP_ICON["relay"], f"Relay {html.escape(k[1])}"
        else:
            icon, title = GROUP_ICON["unknown"], "Chưa rõ IP server"
        names = g[k]
        dn = sum(rates.get(n, (0, 0))[0] for n in names)
        up = sum(rates.get(n, (0, 0))[1] for n in names)
        fkey = {"direct": "d:", "relay": "r:", "unknown": "u:"}[kind] + (k[1] or "")
        fkey = fkey.encode()[:50].decode(errors="ignore")
        out.append((fkey, icon, title, names, dn, up))
    return out


_TAG = re.compile(r"<[^>]+>")


def _vis_len(t):
    """Độ dài Telegram tính (sau khi bỏ thẻ HTML)."""
    return len(html.unescape(_TAG.sub("", t)))


def _fit(lines, limit=4000):
    """Ghép theo từng dòng, dừng trước khi vượt giới hạn — không cắt giữa thẻ HTML."""
    out, n = [], 0
    for ln in lines:
        k = _vis_len(ln) + 1
        if n + k > limit - 20:
            out.append("<i>… (bấm nút lọc để xem thêm)</i>")
            break
        out.append(ln); n += k
    return "\n".join(out)


def _short(route):
    kind, label, sip = route
    if kind == "relay":
        return "relay"
    if kind == "direct":
        return traffic.SERVER_LABELS.get(sip, sip)
    return "?"


async def show_live(update, context, filt=None):
    q = update.callback_query
    await q.edit_message_text(f"⏳ Đang đo {MEASURE_S} giây…")
    rates, dump = await asyncio.to_thread(traffic.measure, MEASURE_S)
    ct = await asyncio.to_thread(traffic.conntrack_map)
    clients = {c["name"]: c for c in db.all_clients()}
    groups = _groups(dump, rates, ct)

    online = [n for g in groups for n in g[3]]
    if filt is not None:
        sel = [g for g in groups if g[0] == filt]
        online = [n for g in sel for n in g[3]]
    active_total = sum(1 for c in clients.values() if not c["locked"] and c["expires_at"] > time.time())
    lines = [f"📡 <b>Đang kết nối — {len(online)}/{active_total} khách</b>",
             f"<i>cập nhật {time.strftime('%H:%M:%S')} · đo trong {MEASURE_S} giây</i>", ""]
    if not traffic.conntrack_ok():
        lines += ["⚠️ <i>Chưa cài conntrack — chưa biết khách vào IP server nào. "
                  "Chạy lại deploy-vps.sh.</i>", ""]

    lines.append("🖥 <b>Theo IP server</b>")
    for k, icon, title, names, dn, up in groups:
        mark = " ◀" if filt == k else ""
        lines.append(f"{icon} {title} — {len(names)} khách{mark}\n"
                     f"     ↓ <code>{traffic.mbit(dn)}</code> ↑ <code>{traffic.mbit(up)}</code> Mbit/s")
        if filt is None:
            srt = sorted(names, key=lambda n: (-rates.get(n, (0, 0))[0], n))
            shown = ", ".join(_link(context, n) for n in srt[:NAMES_PER_GROUP])
            more = f" <i>… +{len(srt) - NAMES_PER_GROUP} (bấm nút lọc)</i>" if len(srt) > NAMES_PER_GROUP else ""
            lines.append(f"     👥 {shown}{more}")
    if not groups:
        lines.append("<i>Không có khách online.</i>")
    tdn = sum(rates.get(n, (0, 0))[0] for n in online)
    tup = sum(rates.get(n, (0, 0))[1] for n in online)
    lines += ["", f"⚡ <b>Tổng</b> ↓ <b>{traffic.mbit(tdn)}</b> ↑ {traffic.mbit(tup)} Mbit/s"]

    routes = {}
    for _pub, (name, ep, _hs, _rx, _tx) in dump.items():
        routes[name] = traffic.route_of(ep, ct)
    top = sorted(online, key=lambda n: (-rates.get(n, (0, 0))[0], n))
    if filt is not None:
        lines += ["", f"👥 <b>Khách trên IP này</b> — {len(top)}"]
        for i, n in enumerate(top[:60], 1):
            dn, up = rates.get(n, (0, 0))
            lines.append(f"{i}. {_link(context, n)} · ↓<code>{traffic.mbit(dn)}</code> ↑<code>{traffic.mbit(up)}</code>")
        if len(top) > 60:
            lines.append(f"<i>… và {len(top) - 60} khách khác</i>")
    busy = [] if filt is not None else [n for n in top if rates.get(n, (0, 0))[0] >= 1e6][:5]
    if filt is None:
        lines += ["", "🔝 <b>Đang dùng nhiều nhất</b>"]
    for i, n in enumerate(busy, 1):
        dn, up = rates.get(n, (0, 0))
        lines.append(f"{i}. {_link(context, n)} · {html.escape(_short(routes.get(n, ('unknown', '?', None))))} · "
                     f"↓<code>{traffic.mbit(dn)}</code> ↑<code>{traffic.mbit(up)}</code>")
    rest = len(online) - len(busy)
    if filt is None and not busy:
        lines.append("<i>Không ai dùng trên 1 Mbit/s.</i>")
    elif filt is None and rest > 0:
        lines.append(f"<i>… {rest} khách khác dưới 1 Mbit/s</i>")

    if filt is None:
        wg = awg.wg_status()
        idle = []
        for name, c in clients.items():
            if c["locked"] or c["expires_at"] < time.time() or name in online:
                continue
            idle.append((wg.get(name, {}).get("handshake_s"), name))
        idle.sort(key=lambda x: (x[0] is None, x[0] or 0))
        if idle:
            lines += ["", f"💤 <b>Không hoạt động (&gt;3 phút)</b> — {len(idle)}"]
            for hs, n in idle[:8]:
                lines.append(f"{_link(context, n)} · {_ago(hs)}")
            if len(idle) > 8:
                lines.append(f"<i>… và {len(idle) - 8} khách khác</i>")

    def _btn_label(k):
        kind, val = k[:1], k[2:]
        if kind == "d":
            return traffic.server_label(val)
        return "Relay" if kind == "r" else "Chưa rõ"
    frow = [B(f"{g[1]} {_btn_label(g[0])}"[:24], callback_data=f"lv:f:{g[0]}") for g in groups[:4]]
    rows = [[B("🔄 Đo lại", callback_data=f"lv:f:{filt}" if filt is not None else "lv:menu"),
             B("📋 Tất cả khách", callback_data="list:0")]]
    if len(frow) > 1:
        rows.append(frow)
    rows.append([B("⬅️ Tất cả IP" if filt is not None else "⬅️ Menu",
                   callback_data="lv:menu" if filt is not None else "home")])
    text = _fit(lines)
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=KB(rows), link_preview_options=NO_PREVIEW)


def client_block(name, w, live=None):
    """Khối HTML chèn vào thẻ khách. w = awg.wg_status()[name]; live = (down,up) nếu vừa đo 5s."""
    rates, hist, routes, _upd = traffic.COLLECTOR.snapshot()
    ep = w.get("endpoint")
    ip, _port = traffic.split_endpoint(ep)
    ct = traffic.conntrack_map() if w.get("online") else {}
    route = traffic.route_of(ep, ct) if w.get("online") else routes.get(name, ("unknown", "?", None))
    if route[0] == "relay":
        srv = f"Relay {html.escape(route[1])}"
    elif route[0] == "direct":
        lb = traffic.SERVER_LABELS.get(route[2])
        srv = f"<code>{route[2]}</code>" + (f" ({html.escape(lb)})" if lb else "")
    else:
        srv = "—"
    isp = traffic.asn_lookup(ip) if ip and route[0] != "relay" else None
    out = ["", "🌐 <b>Kết nối</b>",
           f"IP khách: <code>{html.escape(ep or '—')}</code>"]
    if isp:
        out.append(f"Nhà mạng: {html.escape(isp)}")
    out.append(f"Vào server: {srv}" + (" <i>(lần cuối)</i>" if not w.get("online") and route[0] != "unknown" else ""))

    if live is not None:
        dn, up, src = live[0], live[1], f"đo {MEASURE_S}s"
    else:
        dn, up = rates.get(name, (0, 0)); src = f"TB {traffic.SAMPLE_S}s"
    if w.get("online"):
        out += ["", f"⚡ <b>Tốc độ hiện tại</b> <i>({src})</i>",
                f"↓ <b>{traffic.mbit(dn)}</b> Mbit/s · ↑ {traffic.mbit(up)} Mbit/s"]
    h = hist.get(name) or []
    if len(h) >= 2 and max(h) > 0:
        out += ["", f"📈 <b>{len(h) * traffic.SAMPLE_S // 60} phút gần nhất</b> <i>(↓ Mbit/s)</i>",
                f"<code>{traffic.sparkline(h)}</code>",
                f"max {max(h):.1f} · tb {sum(h) / len(h):.1f}"]

    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m-01")
    d1, u1 = db.usage_sum(name, today, today)
    d2, u2 = db.usage_sum(name, month)
    out += ["", "📦 <b>Lưu lượng</b>",
            f"Hôm nay: ↓ {traffic.fbytes(d1)} · ↑ {traffic.fbytes(u1)}",
            f"Tháng {time.strftime('%m')}: ↓ {traffic.fbytes(d2)} · ↑ {traffic.fbytes(u2)}"]

    hist_ip = db.iphist(name, 4)
    if hist_ip:
        out += ["", "🕓 <b>Lịch sử IP server</b>"]
        for r in hist_ip:
            s = r["server"]
            s = f"Relay {s[6:]}" if s.startswith("relay:") else (
                s + (f" ({traffic.SERVER_LABELS[s]})" if s in traffic.SERVER_LABELS else ""))
            out.append(f"{time.strftime('%d.%m %H:%M', time.localtime(r['ts']))} · {html.escape(s)}")
    return "\n".join(out)


async def job_sample(context):
    await asyncio.to_thread(traffic.COLLECTOR.sample)


async def job_prune(context):
    await asyncio.to_thread(db.usage_prune)


async def on_cb(update, context, d):
    if d == "lv:menu":
        await show_live(update, context)
    elif d.startswith("lv:f:"):
        await show_live(update, context, d[5:] or None)
