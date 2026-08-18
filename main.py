"""
Media Downloader Bot (Optimallashtirilgan va Xavfsiz Versiya)
------------------------------------------------------------
Instagram Reels va TikTok videolarini watermark'siz yuklab beruvchi Telegram bot.

Tuzatishlar:
- Temp fayllar diskni to'ldirib yubormasligi uchun avtomatik tozalash funksiyasi mukammallashtirildi.
- Video, MP3 va Video Note fayllari xavfsiz o'chirilishi ta'minlandi.
"""

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    LabeledPrice,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from dotenv import load_dotenv

# ============================================================
# SOZLAMALAR
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
STARS_PROVIDER_TOKEN = os.getenv("STARS_PROVIDER_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

DAILY_FREE_LIMIT = 3
PREMIUM_PRICE_STARS = 75
PREMIUM_DAYS = 30
ACTIVE_FILE_TTL_MINUTES = 20

VIDEO_NOTE_SIZE = 384
VIDEO_NOTE_MAX_SECONDS = 60

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! Replit Secrets'ga BOT_TOKEN qo'shing.")

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
# XOTIRADAGI KESHLAR
# ============================================================
pending_urls: dict[str, dict] = {}
active_files: dict[str, dict] = {}


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
QUALITY_FORMATS = {
    "360": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
}


def _download_video_sync(url: str, out_dir: str, quality: str) -> str:
    output_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": output_template,
        "format": QUALITY_FORMATS.get(quality, "mp4/bestvideo+bestaudio/best"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not filename.endswith(".mp4"):
            base, _ = os.path.splitext(filename)
            mp4_path = base + ".mp4"
            if os.path.exists(mp4_path):
                filename = mp4_path
        return filename


async def download_video(url: str, quality: str = "720") -> tuple[str, str]:
    tmp_dir = tempfile.mkdtemp(prefix="mediabot_")
    loop = asyncio.get_running_loop()
    file_path = await loop.run_in_executor(
        None, _download_video_sync, url, tmp_dir, quality
    )
    return file_path, tmp_dir


# ============================================================
# FFMPEG YORDAMIDA MP3 VA VIDEO NOTE HOSIL QILISH
# ============================================================
async def extract_audio(video_path: str) -> str:
    out_path = os.path.splitext(video_path)[0] + f"_{uuid.uuid4().hex[:6]}.mp3"
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        out_path,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg (mp3) xatolik: {stderr.decode(errors='ignore')[-500:]}")
    return out_path


async def make_video_note(video_path: str) -> str:
    out_path = os.path.splitext(video_path)[0] + f"_note_{uuid.uuid4().hex[:6]}.mp4"
    vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={VIDEO_NOTE_SIZE}:{VIDEO_NOTE_SIZE}"
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-t", str(VIDEO_NOTE_MAX_SECONDS),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "128k",
        out_path,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg (video note) xatolik: {stderr.decode(errors='ignore')[-500:]}")
    return out_path


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


def quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="360p", callback_data=f"q:{token}:360"),
                InlineKeyboardButton(text="720p", callback_data=f"q:{token}:720"),
            ],
            [
                InlineKeyboardButton(
                    text="1080p (⭐ Premium)", callback_data=f"q:{token}:1080"
                )
            ],
        ]
    )


def post_download_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 MP3 shaklida yuklash", callback_data=f"mp3:{token}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📹 Dumaloq video qilish", callback_data=f"vnote:{token}"
                )
            ],
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
        "video sifatini tanlaysiz va men uni watermark'siz yuklab beraman.\n\n"
        "🎵 Har bir video ostida MP3 shaklida yuklab olish imkoniyati bor.\n"
        "📹 Video ostida uni dumaloq video (video note) shakliga o'tkazish imkoniyati ham bor.\n\n"
        f"🎁 Kuniga <b>{DAILY_FREE_LIMIT} ta</b> bepul yuklash mavjud (360p/720p).\n"
        f"⭐ 1080p sifat va cheksiz yuklash uchun <b>{PREMIUM_PRICE_STARS} Stars</b>ga "
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
        "Premium bilan siz cheksiz miqdorda va 1080p sifatda video yuklashingiz mumkin bo'ladi.",
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
        cur.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
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
async def process_buy_premium(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Premium obuna (1 oy)",
        description=f"Cheksiz video yuklash va 1080p sifat uchun {PREMIUM_DAYS} kunlik Premium obuna.",
        payload=f"premium_{callback.from_user.id}",
        provider_token=STARS_PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice(label="Premium (1 oy)", amount=PREMIUM_PRICE_STARS)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
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
        "Endi cheksiz va 1080p sifatda video yuklashingiz mumkin!"
    )


@router.message(F.text.regexp(URL_REGEX))
async def handle_link(message: Message):
    user_id = message.from_user.id
    get_or_create_user(user_id)
    reset_if_new_day(user_id)

    match = URL_REGEX.search(message.text)
    url = match.group(1) if match else message.text.strip()

    token = uuid.uuid4().hex[:10]
    pending_urls[token] = {
        "url": url,
        "user_id": user_id,
        "created": datetime.now(),
    }

    await message.answer(
        "🎬 Video sifatini tanlang:",
        reply_markup=quality_keyboard(token),
    )


@router.callback_query(F.data.startswith("q:"))
async def process_quality_choice(callback: CallbackQuery):
    try:
        _, token, quality = callback.data.split(":")
    except ValueError:
        await callback.answer("Xatolik yuz berdi.", show_alert=True)
        return

    data = pending_urls.get(token)
    if not data:
        await callback.answer(
            "⏰ Bu so'rov eskirgan. Iltimos, havolani qaytadan yuboring.",
            show_alert=True,
        )
        return

    user_id = callback.from_user.id
    if data["user_id"] != user_id:
        await callback.answer("Bu tugma sizga tegishli emas.", show_alert=True)
        return

    url = data["url"]
    premium = is_premium_active(user_id)

    if quality == "1080" and not premium:
        await callback.answer()
        await callback.message.edit_text(
            "🔒 1080p sifat faqat <b>Premium</b> foydalanuvchilar uchun.\n\n"
            f"⭐ {PREMIUM_PRICE_STARS} Stars evaziga Premium sotib olib, "
            "1080p sifatda va cheksiz yuklashdan foydalanishingiz mumkin:",
            reply_markup=premium_offer_keyboard(),
        )
        return

    if not premium:
        reset_if_new_day(user_id)
        used = get_downloads_today(user_id)
        if used >= DAILY_FREE_LIMIT:
            await callback.answer()
            await callback.message.edit_text(
                "🚫 Bugungi bepul limitingiz tugadi "
                f"({DAILY_FREE_LIMIT}/{DAILY_FREE_LIMIT}).\n\n"
                f"⭐ Cheksiz yuklash uchun {PREMIUM_PRICE_STARS} Starsga "
                "Premium sotib olishingiz mumkin:",
                reply_markup=premium_offer_keyboard(),
            )
            return

    await callback.answer()
    await callback.message.edit_text(
        f"⏳ {quality}p sifatida video yuklanmoqda, biroz kuting..."
    )

    file_path = None
    tmp_dir = None
    try:
        file_path, tmp_dir = await download_video(url, quality)
        active_files[token] = {
            "path": file_path,
            "dir": tmp_dir,
            "user_id": user_id,
            "created": datetime.now(),
        }
        video_file = FSInputFile(file_path)
        await callback.message.answer_video(
            video=video_file,
            caption=f"✅ Mana, so'ragan videongiz ({quality}p, watermark'siz).",
            reply_markup=post_download_keyboard(token),
        )
        await callback.message.delete()
        if not premium:
            increment_download(user_id)
    except Exception as e:
        logger.exception("Yuklashda xatolik: %s", e)
        await callback.message.edit_text(
            "❌ Videoni yuklab bo'lmadi. Havola noto'g'ri yoki video "
            "yopiq/o'chirilgan bo'lishi mumkin. Boshqa havola bilan urinib ko'ring."
        )
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        pending_urls.pop(token, None)


@router.callback_query(F.data.startswith("mp3:"))
async def process_mp3_request(callback: CallbackQuery):
    token = callback.data.split(":", 1)[1]
    entry = active_files.get(token)

    if not entry or not os.path.exists(entry["path"]):
        await callback.answer(
            "⏰ Fayl muddati tugagan. Videoni qaytadan yuklab, urinib ko'ring.",
            show_alert=True,
        )
        return
    if entry["user_id"] != callback.from_user.id:
        await callback.answer("Bu tugma sizga tegishli emas.", show_alert=True)
        return

    await callback.answer("🎵 Audio ajratilmoqda...")

    mp3_path = None
    try:
        mp3_path = await extract_audio(entry["path"])
        audio_file = FSInputFile(mp3_path)
        await callback.message.answer_audio(
            audio=audio_file,
            caption="🎵 Audio (MP3) fayl tayyor.",
        )
    except Exception as e:
        logger.exception("MP3 ajratishda xatolik: %s", e)
        await callback.message.answer(
            "❌ Audio ajratib bo'lmadi. Qaytadan urinib ko'ring."
        )
    finally:
        if mp3_path and os.path.exists(mp3_path):
            try:
                os.remove(mp3_path)
            except OSError:
                pass


@router.callback_query(F.data.startswith("vnote:"))
async def process_video_note_request(callback: CallbackQuery):
    token = callback.data.split(":", 1)[1]
    entry = active_files.get(token)

    if not entry or not os.path.exists(entry["path"]):
        await callback.answer(
            "⏰ Fayl muddati tugagan. Videoni qaytadan yuklab, urinib ko'ring.",
            show_alert=True,
        )
        return
    if entry["user_id"] != callback.from_user.id:
        await callback.answer("Bu tugma sizga tegishli emas.", show_alert=True)
        return

    await callback.answer("📹 Dumaloq video tayyorlanmoqda...")

    vnote_path = None
    try:
        vnote_path = await make_video_note(entry["path"])
        vnote_file = FSInputFile(vnote_path)
        await callback.message.answer_video_note(
            video_note=vnote_file,
            length=VIDEO_NOTE_SIZE,
        )
    except Exception as e:
        logger.exception("Video note qilishda xatolik: %s", e)
        await callback.message.answer(
            "❌ Dumaloq video tayyorlab bo'lmadi. Qaytadan urinib ko'ring."
        )
    finally:
        if vnote_path and os.path.exists(vnote_path):
            try:
                os.remove(vnote_path)
            except OSError:
                pass


@router.message(F.text)
async def handle_other_text(message: Message):
    await message.answer(
        "🔗 Iltimos, menga Instagram yoki TikTok video havolasini yuboring.\n"
        "Masalan: https://www.tiktok.com/@user/video/123456789"
    )


# ============================================================
# ESKIRGAN FAYLLARNI TOZALASH (Disk to'lib qolmasligi uchun)
# ============================================================
async def cleanup_expired_cache_loop():
    while True:
        await asyncio.sleep(300)
        now = datetime.now()

        expired_files = [
            t
            for t, e in active_files.items()
            if now - e["created"] > timedelta(minutes=ACTIVE_FILE_TTL_MINUTES)
        ]
        for t in expired_files:
            entry = active_files.pop(t, None)
            if entry:
                tmp_dir = entry.get("dir")
                if tmp_dir and os.path.exists(tmp_dir):
                    try:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    except OSError:
                        pass

        expired_pending = [
            t
            for t, e in pending_urls.items()
            if now - e["created"] > timedelta(minutes=ACTIVE_FILE_TTL_MINUTES)
        ]
        for t in expired_pending:
            pending_urls.pop(t, None)


# ============================================================
# HEALTH-CHECK SERVER
# ============================================================
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

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot),
        cleanup_expired_cache_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
