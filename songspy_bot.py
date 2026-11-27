import os
import logging
import sqlite3
import subprocess
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from shazamio import Shazam

BOT_TOKEN = os.getenv("BOT_TOKEN", "توکن-خودت-را-اینجا-بذار")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "")  # مثلا @songspyy_channel یا خالی

logging.basicConfig(level=logging.INFO)

con = sqlite3.connect("users.db", check_same_thread=False)
cur = con.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
con.commit()

def add_user(user_id):
    try:
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        con.commit()
    except:
        pass

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FORCE_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_CHANNEL, update.effective_user.id)
        if member.status in ["member", "administrator", "creator"]:
            return True
        kb = [[InlineKeyboardButton("عضویت در کانال", url=f"https://t.me/{FORCE_CHANNEL.replace('@','')}")]]
        await update.message.reply_text("اول توی کانال عضو شو بعد دوباره تلاش کن 👇", reply_markup=InlineKeyboardMarkup(kb))
        return False
    except:
        return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    await update.message.reply_text("سلام به SongSpy 🕵️‍♂️🎶\n\nکافیه یه ویس، آهنگ یا فیلم بفرستی تا تشخیصش بدم.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("فقط فایل صوتی، ویس یا فیلم بفرست. کیفیت خوب باشه دقت بالای 90 درصده.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    await update.message.reply_text(f"تعداد کاربران: {cur.fetchone()[0]}")

def convert_to_mp3(input_path, output_path):
    cmd = ["ffmpeg", "-y", "-i", input_path, "-ar", "44100", "-ac", "2", output_path]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    if not await check_join(update, context):
        return
    msg = await update.message.reply_text("دارم گوش میدم... 🎧")
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
    elif update.message.document:
        file_obj = await update.message.document.get_file()
        ext = "mp3"
    else:
        await msg.edit_text("این فرمت رو پشتیبانی نمی‌کنم.")
        return
    os.makedirs("temp", exist_ok=True)
    raw_path = f"temp/{update.effective_user.id}_{update.message.message_id}.{ext}"
    mp3_path = f"temp/{update.effective_user.id}_{update.message.message_id}_conv.mp3"
    await file_obj.download_to_drive(raw_path)
    try:
        use_path = raw_path
        if ext in ["ogg", "mp4"]:
            convert_to_mp3(raw_path, mp3_path)
            use_path = mp3_path
        shazam = Shazam()
        result = await shazam.recognize_song(use_path)
        if not result or "track" not in result:
            await msg.edit_text("نتونستم تشخیصش بدم 😢 یه تیکه واضح‌تر بفرست.")
            return
        track = result["track"]
        title = track.get("title", "نامشخص")
        subtitle = track.get("subtitle", "نامشخص")
        cover = track.get("images", {}).get("coverart", "")
        url_shazam = track.get("url", "")
        caption = f"🎵 {title}\n👤 {subtitle}\n"
        buttons = []
        if url_shazam:
            buttons.append([InlineKeyboardButton("مشاهده در Shazam 🔎", url=url_shazam)])
        buttons.append([
            InlineKeyboardButton("یوتیوب ▶️", url=f"https://www.youtube.com/results?search_query={title} {subtitle}"),
            InlineKeyboardButton("اسپاتیفای 🟢", url=f"https://open.spotify.com/search/{title} {subtitle}")
        ])
        if cover:
            await msg.delete()
            await update.message.reply_photo(photo=cover, caption=caption, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await msg.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        logging.error(e)
        await msg.edit_text("خطایی پیش اومد، دوباره تلاش کن.")
    finally:
        for p in [raw_path, mp3_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO | filters.Document.AUDIO, handle_audio))
    PORT = int(os.getenv("PORT", "10000"))
    WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if WEBHOOK_URL:
        print(f"Running with webhook: {WEBHOOK_URL}")
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        print("Bot is running with polling...")
        app.run_polling()

if __name__ == "__main__":
    main()
