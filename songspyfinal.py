import os
import re
import glob
import json
import time
import html
import asyncio
import logging
import sqlite3
import subprocess
import urllib.request
from urllib.parse import quote_plus
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyParameters, LinkPreviewOptions,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from shazamio import Shazam

# ───────────────────────── تنظیمات ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "MusicYab")

MAX_BYTES = 49 * 1024 * 1024
PENDING_TTL = 1800  # ۳۰ دقیقه

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


# ───────────────────────── ابزارهای کمکی ─────────────────────────
def qp(msg):
    try:
        return {"reply_parameters": ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)}
    except Exception:
        return {}


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


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


def find_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).strip().rstrip(').,]}"\'') if m else None


async def safe_delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


def rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_base(base):
    for x in glob.glob(f"temp/{base}.*"):
        rm(x)


# ───────────────────────── نگهداری وضعیت (pending) ─────────────────────────
def expire(key):
    p = pending.pop(key, None)
    if p:
        rm(p.get("audio"))


def register(key, data):
    old = pending.get(key)
    if old and old.get("audio") and old.get("audio") != data.get("audio"):
        rm(old["audio"])
    pending[key] = data
    try:
        asyncio.get_running_loop().call_later(PENDING_TTL, expire, key)
    except Exception:
        pass


# ───────────────────────── دانلود / تبدیل (sync، داخل thread اجرا می‌شن) ─────────────────────────
def run_cmd(cmd, timeout):
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception as e:
        logger.error(e)


def yt_base():
    cmd = ["yt-dlp"]
    if os.path.exists("cookies.txt"):
        cmd += ["--cookies", "cookies.txt"]
    return cmd


def extract_audio(inp, out, start=0, dur=60):
    cmd = ["ffmpeg", "-y"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", inp, "-t", str(dur), "-vn", "-ar", "44100", "-ac", "2", "-b:a", "128k", out]
    run_cmd(cmd, 120)


def download_song_file(query, base):
    os.makedirs("temp", exist_ok=True)
    tmpl = f"temp/{base}.%(ext)s"
    cmd = yt_base() + ["-x", "--audio-format", "mp3", "--no-playlist", "--quiet", "--no-warnings",
                       "-o", tmpl, f"ytsearch1:{query}"]
    run_cmd(cmd, 120)
    for f in glob.glob(f"temp/{base}.*"):
        if f.endswith(".mp3"):
            return f
    c = glob.glob(f"temp/{base}.*")
    return c[0] if c else ""


def download_video_file(url, base):
    os.makedirs("temp", exist_ok=True)
    tmpl = f"temp/{base}.%(ext)s"
    cmd = yt_base() + ["-f", "best[height<=720][filesize<48M]/best[filesize<48M]/best",
                       "--no-playlist", "--quiet", "--no-warnings", "--max-filesize", "48M",
                       "-o", tmpl, url]
    run_cmd(cmd, 180)
    cands = [c for c in glob.glob(f"temp/{base}.*") if not c.endswith((".part", ".json"))]
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        for f in cands:
            if f.endswith(ext):
                return f
    return cands[0] if cands else ""


def download_audio_link(url, base):
    os.makedirs("temp", exist_ok=True)
    tmpl = f"temp/{base}.%(ext)s"
    cmd = yt_base() + ["-x", "--audio-format", "mp3", "--no-playlist", "--quiet", "--no-warnings",
                       "--max-filesize", "48M", "-o", tmpl, url]
    run_cmd(cmd, 180)
    cands = [c for c in glob.glob(f"temp/{base}.*") if not c.endswith((".part", ".json"))]
    for f in cands:
        if f.endswith(".mp3"):
            return f
    return cands[0] if cands else ""


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def spotify_to_query(url):
    try:
        d = http_json(f"https://open.spotify.com/oembed?url={quote_plus(url)}")
        q = f"{d.get('title', '')} {d.get('author_name', '')}".strip()
        if len(q) >= 2:
            return q
    except Exception as e:
        logger.error(e)
    return ""


def fetch_lyrics(title, artist):
    try:
        d = http_json(f"https://lrclib.net/api/get?track_name={quote_plus(title)}&artist_name={quote_plus(artist)}")
        if d.get("plainLyrics"):
            return d["plainLyrics"]
    except Exception:
        pass
    try:
        arr = http_json(f"https://lrclib.net/api/search?q={quote_plus(f'{title} {artist}')}")
        for d in arr or []:
            if d.get("plainLyrics"):
                return d["plainLyrics"]
    except Exception:
        pass
    return ""


# ───────────────────────── Shazam ─────────────────────────
async def recognize(path):
    try:
        sh = Shazam()
        fn = getattr(sh, "recognize", None) or sh.recognize_song
        res = await fn(path)
        if res and res.get("track"):
            t = res["track"]
            return {
                "title": t.get("title", "نامشخص"),
                "artist": t.get("subtitle", "نامشخص"),
                "cover": (t.get("images") or {}).get("coverart", ""),
                "url": t.get("url", ""),
            }
    except Exception as e:
        logger.error(e)
    return None


async def search_tracks(text, limit=6):
    out = []
    try:
        res = await Shazam().search_track(text, limit=limit)
        hits = (res or {}).get("tracks", {}).get("hits", [])
        for h in hits:
            d = h.get("heading", {}) or {}
            if d.get("title"):
                out.append((d.get("title", ""), d.get("subtitle", "")))
    except Exception as e:
        logger.error(e)
    return out


# ───────────────────────── ظاهر پیام‌ها و دکمه‌ها ─────────────────────────
def build_caption(info, src_url, bot_username):
    t = html.escape(f"{info['title']} — {info['artist']}")
    name = html.escape(BOT_NAME)
    cap = f'<code>{t}</code>\n\n<a href="https://t.me/{bot_username}">{name}</a> | تشخیص آهنگ 🧑‍🎤'
    if src_url:
        cap += f' | <a href="{html.escape(src_url, quote=True)}">source</a>'
    return cap


def song_markup(key, info):
    qenc = quote_plus(f"{info['title']} {info['artist']}")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Google", url=f"https://www.google.com/search?q={qenc}"),
            InlineKeyboardButton("YouTube Music", url=f"https://music.youtube.com/search?q={qenc}"),
            InlineKeyboardButton("Spotify", url=f"https://open.spotify.com/search/{qenc}"),
        ],
        [InlineKeyboardButton("🔍 جستجو بر اساس هنرمند", callback_data=f"ar:{key}")],
        [InlineKeyboardButton("❌ آهنگ اشتباه ❌", callback_data=f"wr:{key}")],
    ])


def audio_markup(key):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔤 متن ترانه", callback_data=f"ly:{key}"),
        InlineKeyboardButton("🔍", callback_data=f"ar:{key}"),
    ]])


# ───────────────────────── عضویت اجباری ─────────────────────────
async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FORCE_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(FORCE_CHANNEL, update.effective_user.id)
        if m.status in ["member", "administrator", "creator"]:
            return True
        kb = [[InlineKeyboardButton("🔔 عضویت در کانال", url=f"https://t.me/{FORCE_CHANNEL.replace('@', '')}")]]
        await update.message.reply_text("🔒 برای استفاده، اول در کانال عضو شو 👇",
                                        reply_markup=InlineKeyboardMarkup(kb), **qp(update.message))
        return False
    except Exception:
        return True


# ───────────────────────── ارسال MP3 ─────────────────────────
async def send_mp3_auto(chat_id, query, context, reply_to_msg, title=None, performer=None, markup=None):
    caption = f"@{context.bot.username}"
    ckey = f"mp3:{query.lower().strip()}"
    fid = cache_get(ckey)
    if fid:
        try:
            await context.bot.send_audio(chat_id, audio=fid, caption=caption, reply_markup=markup, **qp(reply_to_msg))
            return True
        except Exception:
            pass
    base = f"s_{chat_id}_{reply_to_msg.message_id}_{int(time.time() * 1000)}"
    path = await asyncio.to_thread(download_song_file, query, base)
    if not path or not os.path.exists(path) or os.path.getsize(path) > MAX_BYTES:
        cleanup_base(base)
        return False
    try:
        with open(path, "rb") as f:
            sent = await context.bot.send_audio(
                chat_id, audio=f,
                title=(title or query)[:60],
                performer=(performer or None),
                caption=caption, reply_markup=markup, **qp(reply_to_msg),
            )
        if sent and sent.audio:
            cache_set(ckey, sent.audio.file_id)
    finally:
        cleanup_base(base)
    return True


# ───────────────────────── دستورات ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    await update.message.reply_text(
        "🎧 به موزیک‌یاب خوش اومدی 👋\n\n"
        "🎤 ویس یا فیلم بفرست تا تشخیص بدم\n"
        "🔎 اسم آهنگ بنویس تا فایلش رو بفرستم\n"
        "🔗 لینک یوتیوب، شورتس، اینستا و اسپاتیفای بفرست تا ویدیو و آهنگش رو بفرستم ⬇️",
        **qp(update.message))


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    await update.message.reply_text(f"📊 کاربران: {cur.fetchone()[0]}", **qp(update.message))


# ───────────────────────── ویس / آهنگ / ویدیو ─────────────────────────
async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    m = update.message
    msg = await m.reply_text("🎧 دارم گوش می‌دم... ⏳", **qp(m))

    if m.voice:
        file_obj = await m.voice.get_file()
        ext = "ogg"
    elif m.audio:
        file_obj = await m.audio.get_file()
        ext = "mp3"
    elif m.video:
        file_obj = await m.video.get_file()
        ext = "mp4"
    elif m.video_note:
        file_obj = await m.video_note.get_file()
        ext = "mp4"
    elif m.document:
        file_obj = await m.document.get_file()
        ext = "bin"
    else:
        await msg.edit_text("⚠️ این فرمت پشتیبانی نمی‌شه 🎧")
        return

    os.makedirs("temp", exist_ok=True)
    key = f"{update.effective_user.id}_{m.message_id}"
    raw_path = f"temp/{key}_raw.{ext}"
    apath = f"temp/{key}_a.mp3"
    try:
        await file_obj.download_to_drive(raw_path)
        await asyncio.to_thread(extract_audio, raw_path, apath, 0, 60)
        info = await recognize(apath) if os.path.exists(apath) else None
        if not info:
            rm(apath)
            await msg.edit_text("😕 نتونستم تشخیصش بدم، یه تیکه واضح‌تر بفرست 🎧")
            return

        register(key, {**info, "query": f"{info['title']} {info['artist']}", "audio": apath, "attempt": 0})
        cap = build_caption(info, "", context.bot.username)
        kb = song_markup(key, info)
        if info["cover"]:
            await safe_delete(msg)
            await m.reply_photo(photo=info["cover"], caption=cap, parse_mode="HTML", reply_markup=kb, **qp(m))
        else:
            await msg.edit_text(cap, parse_mode="HTML", reply_markup=kb, link_preview_options=NO_PREVIEW)

        ok = await send_mp3_auto(update.effective_chat.id, f"{info['title']} {info['artist']}", context, m,
                                 info["title"], info["artist"], audio_markup(key))
        if not ok:
            await context.bot.send_message(update.effective_chat.id, "⚠️ فایل مستقیم دانلود نشد", **qp(m))
    except Exception as e:
        logger.exception(e)
        try:
            await msg.edit_text("⚠️ خطایی پیش اومد، دوباره تلاش کن 🔄")
        except Exception:
            pass
    finally:
        rm(raw_path)


# ───────────────────────── لینک ─────────────────────────
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    m = update.message
    chat_id = update.effective_chat.id
    key = f"{update.effective_user.id}_{m.message_id}"
    base = f"lk_{key}"
    apath = f"temp/{base}_a.mp3"
    status = await m.reply_text("🔗 لینک رو گرفتم، دارم دانلود می‌کنم... ⏳", **qp(m))

    try:
        # --- اسپاتیفای: فقط آهنگ ---
        if "spotify.com" in url.lower():
            query = await asyncio.to_thread(spotify_to_query, url)
            if not query:
                await status.edit_text("😕 لینک اسپاتیفای خوانده نشد ⚠️")
                return
            register(key, {"title": query, "artist": "", "query": query, "audio": None, "attempt": 0})
            await status.edit_text(f"🎵 {query}\n⏳ دارم فایل رو می‌فرستم... 🎧")
            ok = await send_mp3_auto(chat_id, query, context, m, markup=audio_markup(key))
            await safe_delete(status)
            if not ok:
                await context.bot.send_message(chat_id, "⚠️ دانلود نشد، دوباره تلاش کن 🔄", **qp(m))
            return

        # --- ویدیو ---
        vpath = await asyncio.to_thread(download_video_file, url, base)
        has_video = bool(vpath) and os.path.exists(vpath) and os.path.getsize(vpath) <= MAX_BYTES

        if has_video:
            await asyncio.to_thread(extract_audio, vpath, apath, 0, 60)
        else:
            cleanup_base(base)
            tmp = await asyncio.to_thread(download_audio_link, url, base)
            if tmp and os.path.exists(tmp) and os.path.getsize(tmp) <= MAX_BYTES:
                os.replace(tmp, apath)
            cleanup_base(base)

        if not os.path.exists(apath):
            cleanup_base(base)
            await status.edit_text("❌ دانلود ناموفق بود. اگه لینک خصوصی باشه باید cookies.txt بذاری 🍪")
            return

        video_msg = None
        if has_video:
            with open(vpath, "rb") as f:
                video_msg = await context.bot.send_video(
                    chat_id, video=f, supports_streaming=True,
                    caption="⏳ دارم آهنگش رو تشخیص می‌دم...", **qp(m))
            await safe_delete(status)
        cleanup_base(base)

        info = await recognize(apath)
        if not info:
            rm(apath)
            txt = "😕 آهنگی تشخیص داده نشد"
            if video_msg:
                await video_msg.edit_caption(caption=txt)
            else:
                await status.edit_text(txt)
            return

        register(key, {**info, "query": f"{info['title']} {info['artist']}", "audio": apath,
                       "attempt": 0, "url": url})
        cap = build_caption(info, url, context.bot.username)
        kb = song_markup(key, info)
        if video_msg:
            await video_msg.edit_caption(caption=cap, parse_mode="HTML", reply_markup=kb)
        else:
            await status.edit_text(cap, parse_mode="HTML", reply_markup=kb, link_preview_options=NO_PREVIEW)

        ok = await send_mp3_auto(chat_id, f"{info['title']} {info['artist']}", context, m,
                                 info["title"], info["artist"], audio_markup(key))
        if not ok:
            await context.bot.send_message(chat_id, "⚠️ فایل مستقیم آهنگ دانلود نشد", **qp(m))
    except Exception as e:
        logger.exception(e)
        cleanup_base(base)
        try:
            await status.edit_text("⚠️ خطا در دانلود لینک 🔄")
        except Exception:
            pass


# ───────────────────────── متن (لینک یا اسم آهنگ) ─────────────────────────
async def handle_text_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    m = update.message
    text = (m.text or "").strip()
    if not text or text.startswith("/"):
        return

    url = find_url(text)
    if url:
        await handle_link(update, context, url)
        return
    if len(text) < 2:
        return

    msg = await m.reply_text("🔍 دارم جست‌وجو می‌کنم... ⏳", **qp(m))
    try:
        results = await search_tracks(text, limit=10)
        if not results:
            await msg.edit_text("😕 چیزی پیدا نکردم ⚠️")
            return
        key = f"{update.effective_user.id}_{m.message_id}"
        register(key, {"title": text, "artist": "", "query": text, "audio": None,
                       "attempt": 0, "results": results})
        rows = [[InlineKeyboardButton(f"{t} — {a}"[:64], callback_data=f"dl:{key}:{i}")]
                for i, (t, a) in enumerate(results)]
        await safe_delete(msg)
        # ایموجی تنها به‌صورت بزرگ نمایش داده می‌شه (مثل تصویر)
        await m.reply_text("🔍", reply_markup=InlineKeyboardMarkup(rows), **qp(m))
    except Exception as e:
        logger.error(e)
        await msg.edit_text("⚠️ خطا در جست‌وجو 🔄")


# ───────────────────────── دکمه‌ها ─────────────────────────
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat_id

    # متن ترانه
    if data.startswith("ly:"):
        p = pending.get(data[3:])
        if not p:
            await q.answer("⏰ منقضی شده، دوباره بفرست 🔄", show_alert=True)
            return
        wait = await context.bot.send_message(chat_id, "📝 دارم متن آهنگ رو میارم... ⏳", **qp(q.message))
        lyr = await asyncio.to_thread(fetch_lyrics, p["title"], p["artist"])
        await safe_delete(wait)
        if lyr:
            await context.bot.send_message(chat_id, f"📝 {p['query']}\n\n{lyr[:3900]}", **qp(q.message))
        else:
            await context.bot.send_message(chat_id, "😕 متن پیدا نشد", **qp(q.message))
        return

    # جستجو بر اساس هنرمند
    if data.startswith("ar:"):
        key = data[3:]
        p = pending.get(key)
        if not p or not p.get("artist"):
            await q.answer("⏰ منقضی شده یا هنرمندی مشخص نیست", show_alert=True)
            return
        results = await search_tracks(p["artist"], limit=8)
        if not results:
            await q.answer("😕 چیزی پیدا نشد", show_alert=True)
            return
        p["results"] = results
        rows = [[InlineKeyboardButton(f"🎵 {t} — {a}"[:60], callback_data=f"dl:{key}:{i}")]
                for i, (t, a) in enumerate(results)]
        await context.bot.send_message(chat_id, f"🎤 آهنگ‌های {p['artist']}:",
                                       reply_markup=InlineKeyboardMarkup(rows), **qp(q.message))
        return

    # دانلود از لیست هنرمند
    if data.startswith("dl:"):
        try:
            _, key, idx = data.split(":")
            title, artist = pending[key]["results"][int(idx)]
        except Exception:
            await q.answer("⏰ منقضی شده، دوباره جستجو کن", show_alert=True)
            return
        wait = await context.bot.send_message(chat_id, f"🎵 {title} — {artist}\n⏳ دارم می‌فرستم... 🎧", **qp(q.message))
        nkey = f"{key}_{idx}"
        register(nkey, {"title": title, "artist": artist, "query": f"{title} {artist}", "audio": None, "attempt": 0})
        ok = await send_mp3_auto(chat_id, f"{title} {artist}", context, q.message, title, artist, audio_markup(nkey))
        await safe_delete(wait)
        if not ok:
            await context.bot.send_message(chat_id, "⚠️ دانلود نشد", **qp(q.message))
        return

    # آهنگ اشتباه
    if data.startswith("wr:"):
        key = data[3:]
        p = pending.get(key)
        if not p or not p.get("audio") or not os.path.exists(p["audio"]):
            await q.answer("⏰ منقضی شده، دوباره بفرست 🔄", show_alert=True)
            return
        p["attempt"] += 1
        clip = f"temp/{key}_w{p['attempt']}.mp3"
        await asyncio.to_thread(extract_audio, p["audio"], clip, p["attempt"] * 7, 12)
        info = await recognize(clip) if os.path.exists(clip) else None
        rm(clip)
        new_query = f"{info['title']} {info['artist']}" if info else ""
        if not info or new_query == p["query"]:
            await q.answer("😕 نتیجه‌ی دیگه‌ای پیدا نشد", show_alert=True)
            return
        p.update({**info, "query": new_query})
        cap = build_caption(info, p.get("url", ""), context.bot.username)
        kb = song_markup(key, info)
        try:
            if q.message.caption is not None:
                await q.message.edit_caption(caption=cap, parse_mode="HTML", reply_markup=kb)
            else:
                await q.message.edit_text(cap, parse_mode="HTML", reply_markup=kb, link_preview_options=NO_PREVIEW)
        except Exception as e:
            logger.error(e)
        ok = await send_mp3_auto(chat_id, new_query, context, q.message, info["title"], info["artist"],
                                 audio_markup(key))
        if not ok:
            await context.bot.send_message(chat_id, "⚠️ فایل مستقیم دانلود نشد", **qp(q.message))
        return


async def on_error(update, context):
    logger.exception("Handler error:", exc_info=context.error)


# ───────────────────────── اجرا ─────────────────────────
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
    app = (Application.builder().token(BOT_TOKEN)
           .connect_timeout(30).read_timeout(60).write_timeout(60).pool_timeout(30).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("statistics", stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE
        | filters.Document.AUDIO | filters.Document.VIDEO, handle_audio))
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
