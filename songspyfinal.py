import os
import re
import glob
import json
import logging
import sqlite3
import subprocess
import urllib.request
from urllib.parse import quote_plus
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
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

pending = {}
URL_RE = re.compile(r'https?://[^\s]+')

def qp(msg):
    try:
        return {"reply_parameters": ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)}
    except Exception:
        return {}

def add_user(user_id):
    try:
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        con.commit()
    except Exception:
        pass

def cache_get(key):
    try:
        cur.execute("SELECT file_id FROM cache WHERE key=?", (key,))
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None

def cache_set(key, file_id):
    try:
        cur.execute("INSERT OR REPLACE INTO cache (key, file_id, ftype) VALUES (?,?,?)", (key, file_id, "file"))
        con.commit()
    except Exception:
        pass

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FORCE_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(FORCE_CHANNEL, update.effective_user.id)
        if m.status in ["member", "administrator", "creator"]:
            return True
        kb = [[InlineKeyboardButton("🔔 عضویت در کانال", url=f"https://t.me/{FORCE_CHANNEL.replace('@','')}")]]
        await update.message.reply_text("🔒 برای استفاده، اول در کانال عضو شو 👇", reply_markup=InlineKeyboardMarkup(kb), **qp(update.message))
        return False
    except Exception:
        return True

def find_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).strip().rstrip(').,]}"\'') if m else None

def yt_base():
    cmd = ["yt-dlp"]
    if os.path.exists("cookies.txt"):
        cmd += ["--cookies", "cookies.txt"]
    return cmd

def convert_to_mp3(inp, out):
    subprocess.run(["ffmpeg", "-y", "-i", inp, "-ar", "44100", "-ac", "2", "-b:a", "128k", out], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def download_song_file(query, base):
    os.makedirs("temp", exist_ok=True)
    tmpl = f"temp/{base}.%(ext)s"
    cmd = yt_base() + ["-x", "--audio-format", "mp3", "--no-playlist", "--quiet", "--no-warnings", "-o", tmpl, f"ytsearch1:{query}"]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    for f in glob.glob(f"temp/{base}.*"):
        if f.endswith(".mp3"):
            return f
    c = glob.glob(f"temp/{base}.*")
    return c[0] if c else ""

def download_video_file(url, base):
    os.makedirs("temp", exist_ok=True)
    tmpl = f"temp/{base}.%(ext)s"
    cmd = yt_base() + ["-f", "best[height<=720][filesize<48M]/best[filesize<48M]/best", "--no-playlist", "--quiet", "--no-warnings", "--max-filesize", "48M", "-o", tmpl, url]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    cands = [c for c in glob.glob(f"temp/{base}.*") if not c.endswith(".part") and not c.endswith(".json")]
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        for f in cands:
            if f.endswith(ext):
                return f
    return cands[0] if cands else ""

def download_audio_link(url, base):
    os.makedirs("temp", exist_ok=True)
    tmpl = f"temp/{base}.%(ext)s"
    cmd = yt_base() + ["-x", "--audio-format", "mp3", "--no-playlist", "--quiet", "--no-warnings", "--max-filesize", "48M", "-o", tmpl, url]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    cands = [c for c in glob.glob(f"temp/{base}.*") if not c.endswith(".part") and not c.endswith(".json")]
    for f in cands:
        if f.endswith(".mp3"):
            return f
    return cands[0] if cands else ""

def spotify_to_query(url):
    try:
        oembed = f"https://open.spotify.com/oembed?url={quote_plus(url)}"
        with urllib.request.urlopen(oembed, timeout=10) as r:
            d = json.loads(r.read().decode())
            q = f"{d.get('title','')} {d.get('author_name','')}".strip()
            if len(q) >= 2:
                return q
    except Exception as e:
        logger.error(e)
    return ""

def cleanup_base(base):
    for x in glob.glob(f"temp/{base}.*"):
        try:
            os.remove(x)
        except Exception:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    await update.message.reply_text("🎧 به موزیک‌یاب خوش اومدی 👋\n\n🎤 ویس یا فیلم بفرست تا تشخیص بدم\n🔎 اسم آهنگ بنویس تا فایلش رو بفرستم\n🔗 لینک یوتیوب، شورتس، اینستا و اسپاتیفای بفرست تا خودش دانلود کنه و بفرسته ⬇️", **qp(update.message))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    await update.message.reply_text(f"📊 کاربران: {cur.fetchone()[0]}", **qp(update.message))

async def send_mp3_auto(chat_id, query, context, reply_to_msg):
    ckey = f"mp3:{query.lower().strip()}"
    fid = cache_get(ckey)
    if fid:
        try:
            await context.bot.send_audio(chat_id, audio=fid, caption=f"🎵 {query} ⚡", **qp(reply_to_msg))
            return True
        except Exception:
            pass
    base = f"s_{chat_id}_{reply_to_msg.message_id}"
    path = download_song_file(query, base)
    if not path or not os.path.exists(path):
        cleanup_base(base)
        return False
    if os.path.getsize(path) > 49 * 1024 * 1024:
        cleanup_base(base)
        return False
    with open(path, "rb") as f:
        sent = await context.bot.send_audio(chat_id, audio=f, title=query[:60], caption=f"🎵 {query} 🎧", **qp(reply_to_msg))
    cleanup_base(base)
    try:
        if sent and sent.audio:
            cache_set(ckey, sent.audio.file_id)
    except Exception:
        pass
    return True

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    msg = await update.message.reply_text("🎧 دارم گوش می‌دم... ⏳", **qp(update.message))
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
        await msg.edit_text("⚠️ این فرمت پشتیبانی نمی‌شه 🎧")
        return
    os.makedirs("temp", exist_ok=True)
    raw_path = f"temp/{update.effective_user.id}_{update.message.message_id}.{ext}"
    mp3_path = f"temp/{update.effective_user.id}_{update.message.message_id}_conv.mp3"
    await file_obj.download_to_drive(raw_path)
    try:
        use_path = raw_path
        if ext in ["ogg", "mp4"]:
            convert_to_mp3(raw_path, mp3_path)
            if os.path.exists(mp3_path):
                use_path = mp3_path
        result = await Shazam().recognize_song(use_path)
        if not result or "track" not in result:
            await msg.edit_text("😕 نتونستم تشخیصش بدم، یه تیکه واضح‌تر بفرست 🎧")
            return
        track = result["track"]
        title = track.get("title", "نامشخص")
        subtitle = track.get("subtitle", "نامشخص")
        cover = track.get("images", {}).get("coverart", "")
        url_shazam = track.get("url", "")
        query = f"{title} {subtitle}"
        key = f"{update.effective_user.id}_{update.message.message_id}"
        pending[key] = query
        qenc = quote_plus(query)
        buttons = []
        if url_shazam:
            buttons.append([InlineKeyboardButton("🔎 مشاهده در Shazam", url=url_shazam)])
        buttons.append([InlineKeyboardButton("📝 متن آهنگ", callback_data=f"ly:{key}")])
        buttons.append([InlineKeyboardButton("▶️ یوتیوب", url=f"https://www.youtube.com/results?search_query={qenc}"), InlineKeyboardButton("🟢 اسپاتیفای", url=f"https://open.spotify.com/search/{qenc}")])
        cap = f"🎵 {title}\n👤 {subtitle}\n\n👀 کاور بلوره، لمس کن ببینی ✨\n⬇️ دارم فایلش رو می‌فرستم..."
        if cover:
            await msg.delete()
            await update.message.reply_photo(photo=cover, caption=cap, reply_markup=InlineKeyboardMarkup(buttons), has_spoiler=True, **qp(update.message))
        else:
            await msg.edit_text(cap, reply_markup=InlineKeyboardMarkup(buttons))
        ok = await send_mp3_auto(update.effective_chat.id, query, context, update.message)
        if not ok:
            await context.bot.send_message(update.effective_chat.id, "⚠️ فایل مستقیم دانلود نشد", **qp(update.message))
    except Exception as e:
        logger.error(e)
        await msg.edit_text("⚠️ خطایی پیش اومد، دوباره تلاش کن 🔄")
    finally:
        for p in [raw_path, mp3_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

async def handle_text_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    url = find_url(text)
    if url:
        lu = url.lower()
        msg = await update.message.reply_text("🔗 لینک رو گرفتم، دارم دانلود می‌کنم... ⏳", **qp(update.message))
        try:
            if "spotify.com" in lu:
                query = spotify_to_query(url)
                if not query:
                    await msg.edit_text("😕 لینک اسپاتیفای خوانده نشد ⚠️")
                    return
                await msg.edit_text(f"🎵 {query}\n⏳ دارم فایل رو می‌فرستم... 🎧")
                ok = await send_mp3_auto(update.effective_chat.id, query, context, update.message)
                try:
                    await msg.delete()
                except Exception:
                    pass
                if not ok:
                    await context.bot.send_message(update.effective_chat.id, "⚠️ دانلود نشد، دوباره تلاش کن 🔄", **qp(update.message))
                return
            base = f"lk_{update.effective_user.id}_{update.message.message_id}"
            ckey = f"link:{url}"
            fid = cache_get(ckey)
            if fid:
                try:
                    await context.bot.send_video(update.effective_chat.id, video=fid, supports_streaming=True, has_spoiler=True, caption="🎬 از کش ارسال شد ⚡ 👀 لمس کن ببینی", **qp(update.message))
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    return
                except Exception:
                    pass
            vpath = download_video_file(url, base)
            if vpath and os.path.exists(vpath) and os.path.getsize(vpath) <= 49 * 1024 * 1024:
                with open(vpath, "rb") as f:
                    sent = await context.bot.send_video(update.effective_chat.id, video=f, supports_streaming=True, has_spoiler=True, caption="🎬 بفرما، دانلود شد 👀👆 لمس کن ببینی ✨", **qp(update.message))
                try:
                    if sent and sent.video:
                        cache_set(ckey, sent.video.file_id)
                except Exception:
                    pass
                cleanup_base(base)
                try:
                    await msg.delete()
                except Exception:
                    pass
                return
            cleanup_base(base)
            await msg.edit_text("🎧 ویدیو سنگین بود، دارم نسخه صوتی رو می‌فرستم... ⏳")
            apath = download_audio_link(url, base)
            if apath and os.path.exists(apath) and os.path.getsize(apath) <= 49 * 1024 * 1024:
                with open(apath, "rb") as f:
                    await context.bot.send_audio(update.effective_chat.id, audio=f, caption="🎵 دانلود از لینک 🎧", **qp(update.message))
                cleanup_base(base)
                try:
                    await msg.delete()
                except Exception:
                    pass
            else:
                cleanup_base(base)
                await msg.edit_text("❌ دانلود ناموفق بود. اگه لینک خصوصی باشه باید cookies.txt بذاری 🍪")
        except Exception as e:
            logger.error(e)
            await msg.edit_text("⚠️ خطا در دانلود لینک 🔄")
        return
    if len(text) < 2:
        return
    msg = await update.message.reply_text("🔍 دارم جست‌وجو می‌کنم... ⏳", **qp(update.message))
    try:
        out = await Shazam().search_track(text, limit=1)
        hits = out.get("tracks", {}).get("hits", []) if out else []
        if not hits:
            await msg.edit_text("😕 چیزی پیدا نکردم ⚠️")
            return
        h = hits[0]
        data = h.get("heading", {}) or {}
        title = data.get("title", text)
        subtitle = data.get("subtitle", "")
        query = f"{title} {subtitle}".strip() or text
        await msg.edit_text(f"🎵 {query}\n⏳ دارم فایل MP3 رو می‌فرستم... 🎧")
        ok = await send_mp3_auto(update.effective_chat.id, query, context, update.message)
        try:
            await msg.delete()
        except Exception:
            pass
        if not ok:
            qenc = quote_plus(query)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ گوش دادن در یوتیوب", url=f"https://www.youtube.com/results?search_query={qenc}")]])
            await context.bot.send_message(update.effective_chat.id, f"🎵 {query}\n⚠️ فایل مستقیم دانلود نشد", reply_markup=kb, **qp(update.message))
    except Exception as e:
        logger.error(e)
        await msg.edit_text("⚠️ خطا در جست‌وجو 🔄")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("ly:"):
        key = data[3:]
        query = pending.get(key, "")
        if not query:
            await q.edit_message_text("⏰ منقضی شده، دوباره بفرست 🔄")
            return
        await q.edit_message_text(f"📝 دارم متن آهنگ رو میارم... ⏳\n{query}")
        try:
            parts = query.split(" ", 1)
            t = parts[0]
            a = parts[1] if len(parts) > 1 else ""
            api = f"https://lrclib.net/api/get?track_name={quote_plus(t)}&artist_name={quote_plus(a)}"
            with urllib.request.urlopen(api, timeout=10) as r:
                d = json.loads(r.read().decode())
                lyr = d.get("plainLyrics", "")
            if lyr:
                await context.bot.send_message(q.message.chat_id, f"📝 {query}\n\n{lyr[:3900]}", **qp(q.message))
            else:
                await context.bot.send_message(q.message.chat_id, "😕 متن پیدا نشد", **qp(q.message))
        except Exception:
            await context.bot.send_message(q.message.chat_id, "😕 متن پیدا نشد", **qp(q.message))

async def on_error(update, context):
    logger.exception("Handler error:", exc_info=context.error)

def keep_alive():
    port = int(os.getenv("PORT", "10000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *a):
            pass
    HTTPServer(("0.0.0.0", port), H).serve_forever()

def main():
    app = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(60).write_timeout(60).pool_timeout(30).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("statistics", stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_song))
    app.add_error_handler(on_error)
    PORT = int(os.getenv("PORT", "10000"))
    URL = (os.getenv("RENDER_EXTERNAL_URL", "") or os.getenv("WEBHOOK_URL", "")).rstrip("/")
    if URL:
        print(f"Running with webhook: {URL}")
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=f"{URL}/{BOT_TOKEN}")
    else:
        print("Running with polling...")
        Thread(target=keep_alive, daemon=True).start()
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
