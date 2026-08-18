"""
Media Downloader Bot
---------------------
Instagram Reels va TikTok videolarini watermark'siz yuklab beruvchi Telegram bot.

Funksiyalar:
- Kuniga 3 ta bepul yuklash limiti
- Telegram Stars orqali 75 ⭐ ga 1 oylik cheksiz (Premium) obuna
- SQLite baza (foydalanuvchi ID, kunlik yuklamalar soni, sana, premium status)

Muallif: siz uchun tayyorlandi.
"""

import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    PreCheckoutQuery,
    LabeledPrice,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from dotenv import load_dotenv

# ============================================================
# SOZLAMALAR (Replit Secrets / .env dan olinadi)
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
STARS_PROVIDER_TOKEN = os.getenv("STARS_PROVIDER_TOKEN", "")  # Stars uchun odatda "" bo'ladi
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

DAILY_FREE_LIMIT = 3
PREMIUM_PRICE_STARS = 75
PREMIUM_DAYS = 30

# Render "Web Service" turida ishlatilganda platforma shu portni kutadi.
# Agar Render'da "Background Worker" turida joylashtirsangiz, bu ishlatilmaydi.
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi! Replit 'Secrets' bo'limiga BOT_TOKEN qo'shing."
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

URL_REGEX = re.compile(
    r"(https?://(?:www\.|vt\.|vm\.)?(?:instagram\.com|tiktok\.com)\S+)",
    re.IGNORECASE,
)

# ============================================================
# DATABASE (SQLite)
# ============================================================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            downloads_today INTEGER NOT NULL DEFAULT 0,
            last_date TEXT NOT NULL DEFAULT '',
            is_premium INTEGER NOT NULL DEFAULT 0,
            premium_until TEXT DEFAULT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def get_or_create_user(user_id: int) -> sqlite3.Row:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None:
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            "INSERT INTO users (user_id, downloads_today, last_date, is_premium, premium_until) "
            "VALUES (?, 0, ?, 0, NULL)",
            (user_id, today),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    conn.close()
    return row


def reset_if_new_day(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT last_date FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row and row["last_date"] != today:
        cur.execute(
            "UPDATE users SET downloads_today = 0, last_date = ? WHERE user_id = ?",
            (today, user_id),
        )
        conn.commit()
    conn.close()


def is_premium_active(user_id: int) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row["is_premium"]:
        return False
    if row["premium_until"] is None:
        return False
    premium_until = datetime.strptime(row["premium_until"], "%Y-%m-%d %H:%M:%S")
    if premium_until < datetime.now():
        # muddati tugagan — premium'ni o'chiramiz
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET is_premium = 0, premium_until = NULL WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()
        return False
    return True


def increment_download(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET downloads_today = downloads_today + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_downloads_today(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT downloads_today FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["downloads_today"] if row else 0


def activate_premium(user_id: int):
    premium_until = (datetime.now() + timedelta(days=PREMIUM_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
        (premium_until, user_id),
    )
    conn.commit()
    conn.close()
    return premium_until


# ============================================================
# YT-DLP YORDAMIDA YUKLASH
# ============================================================
def _download_video_sync(url: str, out_dir: str) -> str:
    """Sinxron (blocking) yuklash funksiyasi — thread ichida chaqiriladi."""
    output_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": output_template,
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # TikTok/Instagram odatda watermark'siz to'g'ridan-to'g'ri video
        # manbasini beradi — yt-dlp shu manbadan yuklaydi.
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # merge_output_format tufayli kengaytma .mp4 bo'lishi kerak
        if not filename.endswith(".mp4"):
            base, _ = os.path.splitext(filename)
            mp4_path = base + ".mp4"
            if os.path.exists(mp4_path):
                filename = mp4_path
        return filename


async def download_video(url: str) -> str:
    """Asosiy event loop'ni bloklamaslik uchun thread'da ishga tushiriladi."""
    tmp_dir = tempfile.mkdtemp(prefix="mediabot_")
    loop = asyncio.get_running_loop()
    file_path = await loop.run_in_executor(None, _download_video_sync, url, tmp_dir)
    return file_path


# ============================================================
# KLAVIATURALAR
# ============================================================
def premium_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ {PREMIUM_PRICE_STARS} Stars — 1 oylik Premium",
                    callback_data="buy_premium",
                )
            ]
        ]
    )


# ============================================================
# HANDLERLAR
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Salom! Men <b>Media Downloader Bot</b>man.\n\n"
        "📥 Menga Instagram Reels yoki TikTok havolasini yuboring — "
        "videoni watermark'siz yuklab beraman.\n\n"
        f"🎁 Kuniga <b>{DAILY_FREE_LIMIT} ta</b> bepul yuklash mavjud.\n"
        f"⭐ Cheksiz yuklash uchun <b>{PREMIUM_PRICE_STARS} Stars</b>ga "
        "1 oylik Premium sotib olishingiz mumkin — /premium buyrug'i orqali."
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    if is_premium_active(user_id):
        await message.answer("✅ Sizda allaqachon faol <b>Premium</b> obuna bor!")
        return
    await message.answer(
        f"⭐ <b>Premium obuna — {PREMIUM_PRICE_STARS} Stars / 1 oy</b>\n\n"
        "Premium bilan siz cheksiz miqdorda video yuklashingiz mumkin bo'ladi.",
        reply_markup=premium_offer_keyboard(),
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    reset_if_new_day(user_id)
    if is_premium_active(user_id):
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
        )
        until = cur.fetchone()["premium_until"]
        conn.close()
        await message.answer(f"⭐ Sizda faol Premium bor.\nMuddati: <b>{until}</b>")
    else:
        used = get_downloads_today(user_id)
        left = max(0, DAILY_FREE_LIMIT - used)
        await message.answer(
            f"📊 Bugungi limit: <b>{left}/{DAILY_FREE_LIMIT}</b> ta bepul yuklash qoldi."
        )


@router.callback_query(F.data == "buy_premium")
async def process_buy_premium(callback):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Premium obuna (1 oy)",
        description=(
            f"Cheksiz video yuklash uchun {PREMIUM_DAYS} kunlik Premium obuna."
        ),
        payload=f"premium_{callback.from_user.id}",
        # Telegram Stars uchun provider_token BO'SH bo'lishi kerak,
        # currency esa har doim "XTR" bo'ladi.
        provider_token=STARS_PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice(label="Premium (1 oy)", amount=PREMIUM_PRICE_STARS)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    # To'lovni tasdiqlaymiz
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    premium_until = activate_premium(user_id)
    await message.answer(
        "🎉 To'lov muvaffaqiyatli o'tdi!\n"
        f"✅ Sizga <b>Premium</b> status berildi.\n"
        f"📅 Amal qilish muddati: <b>{premium_until}</b> gacha.\n\n"
        "Endi cheksiz video yuklashingiz mumkin!"
    )


@router.message(F.text.regexp(URL_REGEX))
async def handle_link(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    reset_if_new_day(user_id)

    match = URL_REGEX.search(message.text)
    url = match.group(1) if match else message.text.strip()

    premium = is_premium_active(user_id)

    if not premium:
        used = get_downloads_today(user_id)
        if used >= DAILY_FREE_LIMIT:
            await message.answer(
                "🚫 Bugungi bepul limitingiz tugadi "
                f"({DAILY_FREE_LIMIT}/{DAILY_FREE_LIMIT}).\n\n"
                f"⭐ Cheksiz yuklash uchun {PREMIUM_PRICE_STARS} Starsga "
                "Premium sotib olishingiz mumkin:",
                reply_markup=premium_offer_keyboard(),
            )
            return

    status_msg = await message.answer("⏳ Video yuklanmoqda, biroz kuting...")

    file_path = None
    try:
        file_path = await download_video(url)
        video_file = FSInputFile(file_path)
        await message.answer_video(
            video=video_file,
            caption="✅ Mana, so'ragan videongiz (watermark'siz).",
        )
        if not premium:
            increment_download(user_id)
    except Exception as e:
        logger.exception("Yuklashda xatolik: %s", e)
        await message.answer(
            "❌ Videoni yuklab bo'lmadi. Havola noto'g'ri yoki video "
            "yopiq/o'chirilgan bo'lishi mumkin. Boshqa havola bilan urinib ko'ring."
        )
    finally:
        await status_msg.delete()
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                os.rmdir(os.path.dirname(file_path))
            except OSError:
                pass


@router.message(F.text)
async def handle_other_text(message: Message):
    await message.answer(
        "🔗 Iltimos, menga Instagram yoki TikTok video havolasini yuboring.\n"
        "Masalan: https://www.tiktok.com/@user/video/123456789"
    )


# ============================================================
# RENDER UCHUN HEALTH-CHECK WEB SERVER
# ============================================================
# Render "Web Service" turi doim ochiq port kutadi, aks holda
# "no open ports detected" xatosi bilan deploy muvaffaqiyatsiz tugaydi.
# Agar botni "Background Worker" turida joylashtirsangiz, bu server
# shart emas, lekin uni ishga tushirish hech qanday zarar keltirmaydi.
async def handle_health_check(request: web.Request) -> web.Response:
    return web.Response(text="Bot ishlab turibdi ✅")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Health-check server %s portda ishga tushdi", PORT)


# ============================================================
# ISHGA TUSHIRISH
# ============================================================
async def main():
    init_db()
    logger.info("Bot ishga tushmoqda...")
    await bot.delete_webhook(drop_pending_updates=True)

    # Web server va bot pollingini bir vaqtda, parallel ishga tushiramiz.
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
