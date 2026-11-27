import os
import re
import glob
import json
import logging
import sqlite3
import subprocess
import urllib.request
from urllib.parse import quote_plus
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from shazamio import Shazam

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()

if not BOT_TOKEN or len(BOT_TOKEN) < 20:
    raise ValueError("BOT_TOKEN را در Environment ست کن")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

con = sqlite3.connect("users.db", check_same_thread=False)
cur = con.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
cur.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, file_id TEXT, ftype TEXT)")
con.commit()

pending_query = {}
pending_link = {}
URL_RE = re.compile(r'https?://[^\s]+')

def qp(msg):
    try:
        return {"reply_parameters": ReplyParameters(message_id=msg.message_id, quote=True)}
    except Exception:
        try:
            return {"reply_to_message_id": msg.message_id}
        except Exception:
            return {}

def add_user(user_id: int):
    try:
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        con.commit()
    except Exception:
        pass

def cache_get(key: str):
    try:
        cur.execute("SELECT file_id, ftype FROM cache WHERE key=?", (key,))
        r = cur.fetchone()
        return r if r else (None, None)
    except Exception:
        return None, None

def cache_set(key: str, file_id: str, ftype: str):
    try:
        cur.execute("INSERT OR REPLACE INTO cache (key, file_id, ftype) VALUES (?,?,?)", (key, file_id, ftype))
        con.commit()
    except Exception:
        pass

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not FORCE_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(FORCE_CHANNEL, update.effective_user.id)
        if m.status in ("member", "administrator", "creator"):
            return True
        kb = [[InlineKeyboardButton("🔔 عضویت در کانال", url=f"https://t.me/{FORCE_CHANNEL.replace('@','')}")]]
        await update.message.reply_text("🔒 برای استفاده از ربات، لطفاً ابتدا در کانال عضو شوید 👇", reply_markup=InlineKeyboardMarkup(kb), **qp(update.message))
        return False
    except Exception:
        return True

def find_url(text: str):
    m = URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).strip().rstrip(').,]}"\'').strip()

def platform_of(url: str):
    u = url.lower()
    if "spotify.com" in u:
        return "spotify"
    if "instagram.com" in u:
        return "instagram"
    if "youtu.be" in u or "youtube.com" in u:
        if "/shorts/" in u:
            return "shorts"
        return "youtube"
    return "other"

def yt_base_cmd():
    cmd = ["yt-dlp"]
    if os.path.exists("cookies.txt"):
        cmd += ["--cookies", "cookies.txt"]
    return cmd

def yt_get_title(url: str):
    try:
        cmd = yt_base_cmd() + ["--dump-single-json", "--no-playlist", "--skip-download", "--quiet", url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if r.stdout:
            data = json.loads(r.stdout)
            title = data.get("title", "") or ""
            uploader = data.get("uploader", "") or data.get("channel", "") or ""
            duration = data.get("duration", 0) or 0
            return title, uploader, duration
    except Exception as e:
        logger.error(f"yt_get_title: {e}")
    return "", "", 0

def spotify_to_query(url: str):
    try:
        oembed = f"https://open.spotify.com/oembed?url={quote_plus(url)}"
        with urllib.request.urlopen(oembed, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            title = data.get("title", "")
            author = data.get("author_name", "")
            q = f"{title} {author}".strip()
            if len(q) >= 2:
                return q
    except Exception as e:
        logger.error(f"spotify oembed: {e}")
    t, a, _ = yt_get_title(url)
    if t:
        return f"{t} {a}".strip()
    return ""

def get_lyrics(title: str, subtitle: str):
    try:
        base = f"https://lrclib.net/api/get?track_name={quote_plus(title)}&artist_name={quote_plus(subtitle)}"
        with urllib.request.urlopen(base, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            txt = data.get("plainLyrics", "") or ""
            if txt.strip():
                return txt.strip()[:4000]
        s = f"https://lrclib.net/api/search?q={quote_plus(title + ' ' + subtitle)}"
        with urllib.request.urlopen(s, timeout=10) as resp:
            arr = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if arr and isinstance(arr, list):
                txt = arr[0].get("plainLyrics", "") or ""
                if txt.strip():
                    return txt.strip()[:4000]
    except Exception as e:
        logger.error(f"lyrics: {e}")
    return None

def convert_to_mp3(inp: str, out: str):
    cmd = ["ffmpeg", "-y", "-i", inp, "-ar", "44100", "-ac", "2", "-b:a", "128k", out]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def download_mp3_query(query: str, base: str):
    tmpl = f"temp/{base}.%(ext)s"
    cmd = yt_base_cmd() + ["-x", "--audio-format", "mp3", "--no-playlist", "--quiet", "-o", tmpl, f"ytsearch1:{query}"]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    cands = glob.glob(f"temp/{base}.*")
    for f in cands:
        if f.endswith(".mp3"):
            return f
    return cands[0] if cands else ""

def download_media(url: str, base: str, is_audio: bool):
    tmpl = f"temp/{base}.%(ext)s"
    if is_audio:
        cmd = yt_base_cmd() + ["-x", "--audio-format", "mp3", "--no-playlist", "--quiet", "--max-filesize", "48M", "-o", tmpl, url]
    else:
        cmd = yt_base_cmd() + ["-f", "best[height<=720][filesize<48M]/best[filesize<48M]/best", "--no-playlist", "--quiet", "--max-filesize", "48M", "-o", tmpl, url]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    cands = [c for c in glob.glob(f"temp/{base}.*") if not c.endswith(".json") and not c.endswith(".part")]
    for ext in (".mp3", ".mp4", ".mkv", ".webm", ".m4a"):
        for f in cands:
            if f.endswith(ext):
                return f
    return cands[0] if cands else ""

def song_buttons(dl_key: str, title: str, subtitle: str, url_shazam: str):
    q = quote_plus(f"{title} {subtitle}")
    rows = []
    rows.append([InlineKeyboardButton("⬇️ دانلود MP3 🎵", callback_data=f"dl:{dl_key}")])
    rows.append([
        InlineKeyboardButton("📝 متن آهنگ", callback_data=f"l:{dl_key}"),
        InlineKeyboardButton("🔄 اصلاح نام برای ذخیره", callback_data=f"fix:{dl_key}")
    ])
    if url_shazam:
        rows.append([InlineKeyboardButton("🔎 مشاهده در Shazam", url=url_shazam)])
    rows.append([
        InlineKeyboardButton("▶️ YouTube", url=f"https://www.youtube.com/results?search_query={q}"),
        InlineKeyboardButton("🟢 Spotify", url=f"https://open.spotify.com/search/{q}")
    ])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    txt = (
        "🎧 به موزیک‌یاب خوش آمدید 👋\n\n"
        "🎤 ویس، آهنگ یا ویدیو بفرستید تا شناسایی شود\n"
        "🔎 نام آهنگ را بنویسید تا جست‌وجو شود\n"
        "🔗 لینک YouTube ،Shorts ،Instagram و Spotify بفرستید تا دانلود شود\n\n"
        "👀 نکته: تصاویر و ویدیوها به‌صورت محو ارسال می‌شوند؛ برای مشاهده لمس کنید ✨"
    )
    await update.message.reply_text(txt, **qp(update.message))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    c = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM cache")
    ch = cur.fetchone()[0]
    await update.message.reply_text(f"🛠️ پنل مدیریت 👑\n\n👥 کاربران: {c}\n💾 فایل‌های کش: {ch}\n📢 کانال اجباری: {FORCE_CHANNEL or 'غیرفعال'}\n\n📊 /statistics\n📣 /broadcast", **qp(update.message))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    c = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM cache")
    ch = cur.fetchone()[0]
    await update.message.reply_text(f"📊 آمار بات 📈\n\n👥 کاربران: {c}\n💾 فایل‌های کش: {ch}", **qp(update.message))

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT user_id FROM users")
    users = [r[0] for r in cur.fetchall()]
    sent = 0
    if update.message.reply_to_message:
        for uid in users:
            try:
                await context.bot.copy_message(uid, update.effective_chat.id, update.message.reply_to_message.message_id)
                sent += 1
            except Exception:
                pass
    else:
        text = update.message.text.replace("/broadcast", "").strip()
        if not text:
            await update.message.reply_text("📣 برای ارسال همگانی، روی پیام موردنظر ریپلای کنید یا بعد از /broadcast متن را بنویسید.", **qp(update.message))
            return
        for uid in users:
            try:
                await context.bot.send_message(uid, text)
                sent += 1
            except Exception:
                pass
    await update.message.reply_text(f"✅ پیام به {sent} نفر ارسال شد 📣", **qp(update.message))

async def do_shazam_answer(update, msg, use_path, base_key):
    result = await Shazam().recognize_song(use_path)
    if not result or "track" not in result:
        await msg.edit_text("😕 نتیجه‌ای پیدا نشد. لطفاً نسخه واضح‌تر و باکیفیت‌تر ارسال کنید 🎧")
        return
    track = result["track"]
    title = track.get("title", "نامشخص")
    subtitle = track.get("subtitle", "نامشخص")
    cover = track.get("images", {}).get("coverart", "")
    url_sh = track.get("url", "")
    pending_query[base_key] = f"{title} {subtitle}"
    cap = f"🎵 {title}\n👤 {subtitle}\n\n👀 کاور به‌صورت محو است؛ برای مشاهده لمس کنید ✨"
    markup = song_buttons(base_key, title, subtitle, url_sh)
    if cover:
        await msg.delete()
        await update.message.reply_photo(photo=cover, caption=cap, reply_markup=markup, has_spoiler=True, **qp(update.message))
    else:
        await msg.edit_text(f"🎵 {title}\n👤 {subtitle} 🎶")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    msg = await update.message.reply_text("🎧 در حال تحلیل صوت... ⏳", **qp(update.message))
    file_obj = None
    ext = "mp3"
    if update.message.voice:
        file_obj = await update.message.voice.get_file()
        ext = "ogg"
    elif update.message.audio:
        file_obj = await update.message.audio.get_file()
        ext = "mp3"
    elif update.message.video:
        file_obj = await update.message.video.get_file()
        ext = "mp4"
    elif update.message.video_note:
        file_obj = await update.message.video_note.get_file()
        ext = "mp4"
    elif update.message.document:
        file_obj = await update.message.document.get_file()
        ext = "mp3"
    else:
        await msg.edit_text("⚠️ این فرمت پشتیبانی نمی‌شود. لطفاً ویس، آهنگ یا ویدیو ارسال کنید 🎧")
        return
    os.makedirs("temp", exist_ok=True)
    base = f"{update.effective_user.id}_{update.message.message_id}"
    raw = f"temp/{base}.{ext}"
    mp3p = f"temp/{base}_conv.mp3"
    await file_obj.download_to_drive(raw)
    try:
        use_path = raw
        if ext in ("ogg", "mp4"):
            convert_to_mp3(raw, mp3p)
            if os.path.exists(mp3p):
                use_path = mp3p
        await do_shazam_answer(update, msg, use_path, base)
    except Exception as e:
        logger.error(e)
        await msg.edit_text("⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید 🔄")
    finally:
        for p in (raw, mp3p):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    plat = platform_of(url)
    if plat == "other":
        await update.message.reply_text("🔗 فقط لینک YouTube ،Shorts ،Instagram و Spotify پشتیبانی می‌شود ⚠️", **qp(update.message))
        return
    os.makedirs("temp", exist_ok=True)
    base = f"lk_{update.effective_user.id}_{update.message.message_id}"
    if plat == "spotify":
        msg = await update.message.reply_text("🟢 در حال بررسی لینک Spotify... 🔍", **qp(update.message))
        query = spotify_to_query(url)
        if not query:
            await msg.edit_text("😕 اطلاعات این لینک خوانده نشد. لطفاً لینک معتبر ارسال کنید.")
            return
        pending_query[base] = query
        parts = query.split(" ", 1)
        t = parts[0] if parts else query
        s = parts[1] if len(parts) > 1 else ""
        await msg.edit_text(f"🎵 {query}\n📎 Spotify ✨", reply_markup=song_buttons(base, t, s, ""))
        return
    label = {"youtube": "YouTube ▶️", "shorts": "YouTube Shorts ⚡", "instagram": "Instagram 📸"}.get(plat, plat)
    msg = await update.message.reply_text(f"🔗 در حال بررسی لینک {label}... ⏳", **qp(update.message))
    title, uploader, dur = yt_get_title(url)
    if not title:
        title = "فایل رسانه‌ای 🎬"
        uploader = label
    pending_link[base] = url
    mins = f"{dur//60}:{dur%60:02d}" if dur else "—"
    cap = f"🎬 {title}\n👤 {uploader}\n⏱ {mins}\n📎 {label}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 دانلود ویدیو 👀", callback_data=f"v:{base}")],
        [InlineKeyboardButton("🎧 دانلود صوت MP3 ⬇️", callback_data=f"a:{base}")]
    ])
    await msg.edit_text(cap, reply_markup=kb)

async def handle_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    url = find_url(text)
    if url:
        await handle_link(update, context, url)
        return
    if len(text) < 2:
        return
    msg = await update.message.reply_text("🔍 در حال جست‌وجو... ⏳", **qp(update.message))
    try:
        out = await Shazam().search_track(text, limit=3)
        hits = out.get("tracks", {}).get("hits", []) if out else []
        if not hits:
            qq = quote_plus(text)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ جست‌وجو در YouTube", url=f"https://www.youtube.com/results?search_query={qq}")]])
            await msg.edit_text("😕 در Shazam نتیجه‌ای یافت نشد.", reply_markup=kb)
            return
        for h in hits[:3]:
            data = h.get("heading", {}) or h.get("track", {}) or {}
            title = data.get("title") or h.get("title", "نامشخص")
            subtitle = data.get("subtitle") or h.get("subtitle", "نامشخص")
            key = f"tx_{update.effective_user.id}_{update.message.message_id}_{title[:8]}"
            pending_query[key] = f"{title} {subtitle}"
            await update.message.reply_text(f"🎵 {title}\n👤 {subtitle} 🎶", reply_markup=song_buttons(key, title, subtitle, ""), **qp(update.message))
        await msg.delete()
    except Exception as e:
        logger.error(e)
        await msg.edit_text("⚠️ خطا در جست‌وجو. لطفاً دوباره تلاش کنید 🔄")

async def send_cached_or_new_audio(chat_id, query, context, qp_kwargs, want_fix=False):
    ckey = f"mp3:{query.lower().strip()}"
    fid, _ = cache_get(ckey)
    if fid and not want_fix:
        try:
            await context.bot.send_audio(chat_id, audio=fid, caption=f"🎵 {query} ⚡", **qp_kwargs)
            return True
        except Exception:
            pass
    base = f"dl_{chat_id}_{os.urandom(4).hex()}"
    path = download_mp3_query(query, base)
    if not path or not os.path.exists(path):
        return False
    if os.path.getsize(path) > 49 * 1024 * 1024:
        os.remove(path)
        return False
    parts = query.split(" ", 1)
    performer = parts[1] if len(parts) > 1 else ""
    fname = f"{query}.mp3" if want_fix else None
    with open(path, "rb") as f:
        if fname:
            sent = await context.bot.send_audio(chat_id, audio=f, title=query[:60], performer=performer[:60], filename=fname, caption=f"🎵 {query}\n🔄 نسخه اصلاح‌شده برای ذخیره در Music 📁", **qp_kwargs)
        else:
            sent = await context.bot.send_audio(chat_id, audio=f, title=query[:60], performer=performer[:60], caption=f"🎵 {query} 🎧", **qp_kwargs)
    try:
        os.remove(path)
    except Exception:
        pass
    try:
        if sent and sent.audio:
            cache_set(ckey, sent.audio.file_id, "audio")
    except Exception:
        pass
    return True

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    qqp = qp(q.message)
    try:
        if data.startswith("l:"):
            key = data[2:]
            query = pending_query.get(key, "")
            if not query:
                await q.edit_message_text("⏰ این درخواست منقضی شده است. لطفاً دوباره ارسال کنید 🔄")
                return
            await q.edit_message_text(f"📝 در حال دریافت متن آهنگ... ⏳\n{query}")
            parts = query.split(" ", 1)
            t = parts[0] if parts else query
            s = parts[1] if len(parts) > 1 else ""
            lyr = get_lyrics(t, s)
            if not lyr:
                lyr = get_lyrics(query, "")
            if lyr:
                await context.bot.send_message(q.message.chat_id, f"📝 {query}\n\n{lyr[:3900]}", **qqp)
            else:
                await context.bot.send_message(q.message.chat_id, "😕 متن این آهنگ یافت نشد.", **qqp)
            return
        if data.startswith("dl:") or data.startswith("fix:"):
            is_fix = data.startswith("fix:")
            key = data.split(":", 1)[1]
            query = pending_query.get(key)
            if not query:
                await q.edit_message_text("⏰ این درخواست منقضی شده است. لطفاً دوباره ارسال کنید 🔄")
                return
            await q.edit_message_text(f"⏳ در حال دانلود... 🎧\n{query}")
            ok = await send_cached_or_new_audio(q.message.chat_id, query, context, qqp, want_fix=is_fix)
            if not ok:
                qq = quote_plus(query)
                await context.bot.send_message(q.message.chat_id, f"⚠️ دانلود مستقیم ممکن نشد به‌دلیل حجم بالا 📦:\nhttps://www.youtube.com/results?search_query={qq}", **qqp)
            return
        if data.startswith("v:") or data.startswith("a:"):
            is_audio = data.startswith("a:")
            key = data[2:]
            url = pending_link.get(key)
            if not url:
                await q.edit_message_text("⏰ این لینک منقضی شده است. لطفاً دوباره ارسال کنید 🔄")
                return
            ckey = f"{'a' if is_audio else 'v'}:{url}"
            fid, _ = cache_get(ckey)
            if fid:
                try:
                    if is_audio:
                        await context.bot.send_audio(q.message.chat_id, audio=fid, caption="⚡ از کش ارسال شد 🎧", **qqp)
                    else:
                        await context.bot.send_video(q.message.chat_id, video=fid, supports_streaming=True, has_spoiler=True, caption="🎬 برای مشاهده لمس کنید 👀", **qqp)
                    return
                except Exception:
                    pass
            await q.edit_message_text(f"⏳ در حال دانلود... 🎬\n{url}")
            base = f"md_{q.from_user.id}_{q.message.message_id}"
            path = download_media(url, base, is_audio)
            if path and os.path.exists(path):
                if os.path.getsize(path) > 49 * 1024 * 1024:
                    os.remove(path)
                    await context.bot.send_message(q.message.chat_id, "⚠️ حجم فایل بیشتر از ۵۰ مگابایت است 📦", **qqp)
                    return
                if is_audio:
                    with open(path, "rb") as f:
                        sent = await context.bot.send_audio(q.message.chat_id, audio=f, caption="🎧 بفرمایید 🎵", **qqp)
                    try:
                        if sent.audio:
                            cache_set(ckey, sent.audio.file_id, "audio")
                    except Exception:
                        pass
                else:
                    with open(path, "rb") as f:
                        sent = await context.bot.send_video(q.message.chat_id, video=f, supports_streaming=True, has_spoiler=True, caption="🎬 برای مشاهده لمس کنید 👀", **qqp)
                    try:
                        if sent.video:
                            cache_set(ckey, sent.video.file_id, "video")
                    except Exception:
                        pass
                try:
                    os.remove(path)
                except Exception:
                    pass
                for f in glob.glob(f"temp/{base}.*"):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            else:
                await context.bot.send_message(q.message.chat_id, "❌ دانلود ناموفق بود. اگر لینک Instagram خصوصی است، فایل cookies.txt را کنار ربات قرار دهید 🍪", **qqp)
            return
    except Exception as e:
        logger.error(f"button: {e}")
        try:
            await context.bot.send_message(q.message.chat_id, "⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید 🔄")
        except Exception:
            pass

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("statistics", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_router))
    PORT = int(os.getenv("PORT", "10000"))
    URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=f"{URL}/{BOT_TOKEN}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
