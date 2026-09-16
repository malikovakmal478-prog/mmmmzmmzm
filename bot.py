import os
import json
import asyncio
import sqlite3
import logging
import threading
from datetime import datetime, date

import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ============ CONFIG ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "SIZNING_BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x.strip()]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "llama-3.1-8b-instant")
DB_PATH = "kino.db"
FREE_DAILY_SEARCH_LIMIT = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============ DATABASE ============
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER DEFAULT 0,
        premium_until TEXT,
        search_count INTEGER DEFAULT 0,
        last_search_date TEXT,
        join_date TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS movies (
        code INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        description TEXT,
        file_id TEXT,
        added_date TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    conn.close()

def get_user(uid):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return row

def add_user(uid, username):
    if get_user(uid):
        return
    conn = db()
    conn.execute(
        "INSERT INTO users (id, username, balance, premium_until, search_count, last_search_date, join_date) VALUES (?,?,0,NULL,0,?,?)",
        (uid, username, str(date.today()), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def is_premium(uid):
    u = get_user(uid)
    if not u or not u["premium_until"]:
        return False
    try:
        return date.fromisoformat(u["premium_until"]) >= date.today()
    except Exception:
        return False

def set_premium(uid, days):
    u = get_user(uid)
    start = date.today()
    if u and u["premium_until"]:
        try:
            existing = date.fromisoformat(u["premium_until"])
            if existing > start:
                start = existing
        except Exception:
            pass
    until = date.fromordinal(start.toordinal() + days)
    conn = db()
    conn.execute("UPDATE users SET premium_until=? WHERE id=?", (str(until), uid))
    conn.commit()
    conn.close()
    return until

def change_balance(uid, amount):
    conn = db()
    conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
    conn.commit()
    conn.close()

def get_setting(key, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = db()
    conn.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?", (key, value, value))
    conn.commit()
    conn.close()

def add_movie(title, description, file_id):
    conn = db()
    cur = conn.execute(
        "INSERT INTO movies (title, description, file_id, added_date) VALUES (?,?,?,?)",
        (title, description, file_id, datetime.now().isoformat())
    )
    code = cur.lastrowid
    conn.commit()
    conn.close()
    return code

def get_movie_by_code(code):
    conn = db()
    row = conn.execute("SELECT * FROM movies WHERE code=?", (code,)).fetchone()
    conn.close()
    return row

def delete_movie(code):
    conn = db()
    conn.execute("DELETE FROM movies WHERE code=?", (code,))
    conn.commit()
    conn.close()

def all_movies_brief():
    conn = db()
    rows = conn.execute("SELECT code, title, description FROM movies").fetchall()
    conn.close()
    return rows

def all_user_ids():
    conn = db()
    rows = conn.execute("SELECT id FROM users").fetchall()
    conn.close()
    return [r["id"] for r in rows]

def stats():
    conn = db()
    users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    movies = conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
    premium = conn.execute("SELECT COUNT(*) c FROM users WHERE premium_until IS NOT NULL AND premium_until >= ?", (str(date.today()),)).fetchone()["c"]
    conn.close()
    return users, movies, premium

# ============ AI SEARCH ============
def ai_find_movie(user_text):
    movies = all_movies_brief()
    if not movies:
        return None
    catalog = "\n".join([f'{m["code"]}: {m["title"]} - {m["description"] or ""}' for m in movies])
    prompt = (
        "Quyida kinolar katalogi (kod: nomi - tavsif) berilgan. "
        "Foydalanuvchi kinoni noaniq so'zlar bilan tasvirlab beradi. "
        "Faqat shu katalogdagi eng mos keladigan kinoning kodini son sifatida qaytar. "
        "Agar mos kino topilmasa faqat 'NONE' deb yoz. Boshqa hech narsa yozma.\n\n"
        f"KATALOG:\n{catalog}\n\n"
        f"FOYDALANUVCHI TASVIRI: {user_text}\n\n"
        "JAVOB (faqat kod raqami yoki NONE):"
    )
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            data=json.dumps({
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0
            }),
            timeout=30
        )
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            code = int(digits)
            if get_movie_by_code(code):
                return code
        return None
    except Exception as e:
        logger.error(f"AI search error: {e}")
        return None

# ============ MEMBERSHIP CHECK ============
async def check_membership(uid, context: ContextTypes.DEFAULT_TYPE):
    channels_raw = get_setting("mandatory_channels", "")
    if not channels_raw:
        return True, []
    channels = [c.strip() for c in channels_raw.split(",") if c.strip()]
    not_joined = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=ch, user_id=uid)
            if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return len(not_joined) == 0, not_joined

def membership_keyboard(not_joined):
    buttons = []
    for ch in not_joined:
        link = f"https://t.me/{ch.lstrip('@')}"
        buttons.append([InlineKeyboardButton(f"➕ {ch}", url=link)])
    buttons.append([InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(buttons)

# ============ MENUS ============
def main_menu(uid):
    kb = [
        ["🔍 AI qidiruv", "🔢 Kod orqali qidirish"],
        ["👤 Profil", "💎 Premium"]
    ]
    if uid in ADMIN_IDS:
        kb.append(["⚙️ Admin panel"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def admin_menu():
    kb = [
        ["➕ Kino qo'shish", "➖ Kino o'chirish"],
        ["📢 Majburiy kanal", "💰 Balans"],
        ["👑 Premium berish", "📊 Statistika"],
        ["📣 Xabar yuborish", "🔙 Orqaga"]
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

# ============ CONVERSATION STATES ============
(AI_SEARCH, CODE_SEARCH,
 ADD_MOVIE_VIDEO, ADD_MOVIE_TITLE, ADD_MOVIE_DESC,
 DEL_MOVIE_CODE,
 SET_CHANNEL,
 BALANCE_UID, BALANCE_AMOUNT,
 PREMIUM_UID, PREMIUM_DAYS,
 BROADCAST_TEXT) = range(12)

pending_movie = {}

# ============ USER HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_user(uid, update.effective_user.username or "")
    ok, not_joined = await check_membership(uid, context)
    if not ok:
        await update.message.reply_text(
            "📢 Botdan foydalanish uchun quyidagi kanal(lar)ga a'zo bo'ling:",
            reply_markup=membership_keyboard(not_joined)
        )
        return
    await update.message.reply_text(
        f"Assalomu alaykum, {update.effective_user.first_name}!\n\n"
        "🎬 AI kino bot orqali xohlagan kinongizni topamiz.",
        reply_markup=main_menu(uid)
    )

async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    ok, not_joined = await check_membership(uid, context)
    if ok:
        await query.answer("✅ Tasdiqlandi!")
        await query.message.delete()
        await context.bot.send_message(uid, "Xush kelibsiz!", reply_markup=main_menu(uid))
    else:
        await query.answer("❌ Hali barcha kanallarga a'zo bo'lmadingiz", show_alert=True)

async def ai_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ok, not_joined = await check_membership(uid, context)
    if not ok:
        await update.message.reply_text("Avval kanallarga a'zo bo'ling.", reply_markup=membership_keyboard(not_joined))
        return ConversationHandler.END
    u = get_user(uid)
    if not is_premium(uid):
        today = str(date.today())
        count = u["search_count"] if u["last_search_date"] == today else 0
        if count >= FREE_DAILY_SEARCH_LIMIT:
            await update.message.reply_text(
                f"❌ Bugungi bepul qidiruv limitingiz tugadi ({FREE_DAILY_SEARCH_LIMIT} ta).\n"
                "💎 Premium olsangiz cheksiz qidirishingiz mumkin."
            )
            return ConversationHandler.END
    await update.message.reply_text("🎬 Kinoni so'zlar bilan tasvirlab bering (janr, mavzu, esda qolgan detal):")
    return AI_SEARCH

async def ai_search_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    await update.message.reply_text("🔎 Qidirilmoqda...")
    code = ai_find_movie(text)
    conn = db()
    today = str(date.today())
    u = get_user(uid)
    count = u["search_count"] + 1 if u["last_search_date"] == today else 1
    conn.execute("UPDATE users SET search_count=?, last_search_date=? WHERE id=?", (count, today, uid))
    conn.commit()
    conn.close()
    if code is None:
        await update.message.reply_text("😔 Afsuski, mos kino topilmadi. Boshqacharoq tasvirlab ko'ring.", reply_markup=main_menu(uid))
        return ConversationHandler.END
    movie = get_movie_by_code(code)
    await update.message.reply_video(movie["file_id"], caption=f"🎬 {movie['title']}\n🔢 Kod: {movie['code']}")
    await update.message.reply_text("Yana kerak bo'lsa, menyudan tanlang.", reply_markup=main_menu(uid))
    return ConversationHandler.END

async def code_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ok, not_joined = await check_membership(uid, context)
    if not ok:
        await update.message.reply_text("Avval kanallarga a'zo bo'ling.", reply_markup=membership_keyboard(not_joined))
        return ConversationHandler.END
    await update.message.reply_text("🔢 Kino kodini kiriting:")
    return CODE_SEARCH

async def code_search_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        code = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Faqat raqam kiriting.")
        return CODE_SEARCH
    movie = get_movie_by_code(code)
    if not movie:
        await update.message.reply_text("❌ Bunday kodli kino topilmadi.", reply_markup=main_menu(uid))
        return ConversationHandler.END
    await update.message.reply_video(movie["file_id"], caption=f"🎬 {movie['title']}\n🔢 Kod: {movie['code']}")
    await update.message.reply_text("Yana kerak bo'lsa, menyudan tanlang.", reply_markup=main_menu(uid))
    return ConversationHandler.END

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    premium_status = "✅ Faol (" + u["premium_until"] + " gacha)" if is_premium(uid) else "❌ Yo'q"
    today = str(date.today())
    used = u["search_count"] if u["last_search_date"] == today else 0
    limit_text = "Cheksiz 💎" if is_premium(uid) else f"{used}/{FREE_DAILY_SEARCH_LIMIT}"
    await update.message.reply_text(
        f"👤 Profil\n\n"
        f"🆔 ID: {u['id']}\n"
        f"💰 Balans: {u['balance']} so'm\n"
        f"💎 Premium: {premium_status}\n"
        f"🔍 Bugungi qidiruvlar: {limit_text}"
    )

async def premium_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💎 Premium imkoniyatlari:\n"
        "• Cheksiz AI qidiruv\n"
        "• Navbatsiz xizmat\n\n"
        "Premium olish uchun admin bilan bog'laning."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("Bekor qilindi.", reply_markup=main_menu(uid))
    return ConversationHandler.END

# ============ ADMIN HANDLERS ============
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return ConversationHandler.END
        return await func(update, context)
    return wrapper

@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚙️ Admin panel:", reply_markup=admin_menu())

@admin_only
async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bosh menyu:", reply_markup=main_menu(update.effective_user.id))

@admin_only
async def add_movie_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎥 Kino videosini yuboring:")
    return ADD_MOVIE_VIDEO

@admin_only
async def add_movie_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video and not update.message.document:
        await update.message.reply_text("❌ Iltimos video fayl yuboring.")
        return ADD_MOVIE_VIDEO
    file_id = update.message.video.file_id if update.message.video else update.message.document.file_id
    pending_movie[update.effective_user.id] = {"file_id": file_id}
    await update.message.reply_text("📝 Kino nomini kiriting:")
    return ADD_MOVIE_TITLE

@admin_only
async def add_movie_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_movie[update.effective_user.id]["title"] = update.message.text
    await update.message.reply_text("📄 Qisqacha tavsif kiriting (AI qidiruv uchun muhim, masalan janr/mavzu):")
    return ADD_MOVIE_DESC

@admin_only
async def add_movie_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    info = pending_movie.pop(uid)
    code = add_movie(info["title"], update.message.text, info["file_id"])
    await update.message.reply_text(f"✅ Kino qo'shildi!\n🔢 Kod: {code}", reply_markup=admin_menu())
    return ConversationHandler.END

@admin_only
async def del_movie_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔢 O'chiriladigan kino kodini kiriting:")
    return DEL_MOVIE_CODE

@admin_only
async def del_movie_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        code = int(update.message.text.strip())
        delete_movie(code)
        await update.message.reply_text(f"✅ Kod {code} o'chirildi.", reply_markup=admin_menu())
    except ValueError:
        await update.message.reply_text("❌ Faqat raqam kiriting.")
        return DEL_MOVIE_CODE
    return ConversationHandler.END

@admin_only
async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = get_setting("mandatory_channels", "yo'q")
    await update.message.reply_text(
        f"Hozirgi majburiy kanallar: {current}\n\n"
        "Yangi kanal(lar)ni @ bilan kiriting (bir nechta bo'lsa vergul bilan ajrating), "
        "o'chirish uchun 'yoq' deb yozing:"
    )
    return SET_CHANNEL

@admin_only
async def set_channel_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() in ("yoq", "yo'q", "-"):
        set_setting("mandatory_channels", "")
        await update.message.reply_text("✅ Majburiy obuna o'chirildi.", reply_markup=admin_menu())
    else:
        set_setting("mandatory_channels", text)
        await update.message.reply_text(f"✅ Majburiy kanallar o'rnatildi: {text}", reply_markup=admin_menu())
    return ConversationHandler.END

@admin_only
async def balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👤 Foydalanuvchi ID sini kiriting:")
    return BALANCE_UID

@admin_only
async def balance_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["target_uid"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Faqat raqam (ID) kiriting.")
        return BALANCE_UID
    await update.message.reply_text("💰 Miqdorni kiriting (qo'shish uchun +5000, ayirish uchun -5000):")
    return BALANCE_AMOUNT

@admin_only
async def balance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Noto'g'ri format. Masalan: +5000 yoki -5000")
        return BALANCE_AMOUNT
    uid = context.user_data.pop("target_uid")
    change_balance(uid, amount)
    u = get_user(uid)
    await update.message.reply_text(f"✅ Bajarildi. {uid} ning yangi balansi: {u['balance']} so'm", reply_markup=admin_menu())
    return ConversationHandler.END

@admin_only
async def premium_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👤 Foydalanuvchi ID sini kiriting:")
    return PREMIUM_UID

@admin_only
async def premium_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["target_uid"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Faqat raqam (ID) kiriting.")
        return PREMIUM_UID
    await update.message.reply_text("📅 Necha kunlik premium berilsin?")
    return PREMIUM_DAYS

@admin_only
async def premium_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Faqat raqam kiriting.")
        return PREMIUM_DAYS
    uid = context.user_data.pop("target_uid")
    until = set_premium(uid, days)
    await update.message.reply_text(f"✅ {uid} ga {days} kunlik premium berildi ({until} gacha).", reply_markup=admin_menu())
    try:
        await context.bot.send_message(uid, f"🎉 Sizga {days} kunlik Premium berildi!")
    except Exception:
        pass
    return ConversationHandler.END

@admin_only
async def stats_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users, movies, premium = stats()
    await update.message.reply_text(
        f"📊 Statistika\n\n👥 Foydalanuvchilar: {users}\n🎬 Kinolar: {movies}\n💎 Premium: {premium}"
    )

@admin_only
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📣 Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:")
    return BROADCAST_TEXT

@admin_only
async def broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    sent, failed = 0, 0
    for uid in all_user_ids():
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Yuborildi: {sent}, xato: {failed}", reply_markup=admin_menu())
    return ConversationHandler.END

# ============ FLASK KEEP-ALIVE ============
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot ishlayapti!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# ============ MAIN ============
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(MessageHandler(filters.Regex("^👤 Profil$"), profile))
    app.add_handler(MessageHandler(filters.Regex("^💎 Premium$"), premium_info))
    app.add_handler(MessageHandler(filters.Regex("^📊 Statistika$"), stats_view))
    app.add_handler(MessageHandler(filters.Regex("^🔙 Orqaga$"), admin_back))

    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔍 AI qidiruv$"), ai_search_start)],
        states={AI_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_search_process)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔢 Kod orqali qidirish$"), code_search_start)],
        states={CODE_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_search_process)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚙️ Admin panel$"), admin_panel)],
        states={}, fallbacks=[]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Kino qo'shish$"), add_movie_start)],
        states={
            ADD_MOVIE_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.ALL, add_movie_video)],
            ADD_MOVIE_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_movie_title)],
            ADD_MOVIE_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_movie_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Kino o'chirish$"), del_movie_start)],
        states={DEL_MOVIE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, del_movie_process)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 Majburiy kanal$"), set_channel_start)],
        states={SET_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_channel_process)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Balans$"), balance_start)],
        states={
            BALANCE_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_uid)],
            BALANCE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^👑 Premium berish$"), premium_start)],
        states={
            PREMIUM_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, premium_uid)],
            PREMIUM_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, premium_days)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📣 Xabar yuborish$"), broadcast_start)],
        states={BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_process)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    logger.info("Bot ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()
