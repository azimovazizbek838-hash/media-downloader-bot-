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
