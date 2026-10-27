import os
import re
import uuid
import shutil
import asyncio
import subprocess
import sqlite3
import warnings

# 1. ENG AVVAL FFMPEG NI SOZLAYMIZ (pydub va shazamio xato bermasligi uchun)
warnings.filterwarnings("ignore", category=RuntimeWarning)

DOWNLOAD_DIR = "downloads"
BIN_DIR = "bin"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(BIN_DIR, exist_ok=True)


def setup_ffmpeg() -> str:
    """FFmpeg ni avtomatik topish va tizim PATH'ga ulash"""
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg
        src_exe = imageio_ffmpeg.get_ffmpeg_exe()
        ext = ".exe" if os.name == "nt" else ""
        dst_exe = os.path.abspath(os.path.join(BIN_DIR, f"ffmpeg{ext}"))
        if not os.path.exists(dst_exe):
            shutil.copyfile(src_exe, dst_exe)
        os.environ["PATH"] = os.path.abspath(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
        return dst_exe
    except ImportError:
        raise RuntimeError(
            "FFmpeg topilmadi! Terminalda 'pip install imageio-ffmpeg' deb yozib o'rnating."
        )


FFMPEG_PATH = setup_ffmpeg()

# pydub ogohlantirishini butunlay yo'qotish
try:
    import pydub
    pydub.AudioSegment.converter = FFMPEG_PATH
except ImportError:
    pydub = None

import requests
import yt_dlp
from aiohttp import web
from shazamio import Shazam
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, FSInputFile, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

# ==========================================
# SOZLAMALAR
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8721239232:AAGpqO84Ze87-QRg8r7Lad-S8qADQ5JrSWc")
ADMIN_ID = "@cloude_a"  # Username (@cloude_a) yoki ID raqam yozish mumkin

session = AiohttpSession(timeout=120)
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
shazam = Shazam()

# Keshlar va faol yuklamalar nazorati
SONG_CACHE = {}
ACTIVE_DOWNLOADS = set()
BOT_USERNAME = ""


def is_admin(message: Message) -> bool:
    """Foydalanuvchi admin ekanligini ham ID, ham Username orqali tekshirish"""
    if isinstance(ADMIN_ID, int):
        return message.from_user.id == ADMIN_ID
    admin_str = str(ADMIN_ID).strip().lstrip("@").lower()
    if admin_str.isdigit():
        return message.from_user.id == int(admin_str)
    user_un = (message.from_user.username or "").lower()
    return user_un == admin_str


# ==========================================
# 1. SQLITE BAZA FUNKSIYALARI
# ==========================================
def init_db():
    conn = sqlite3.connect("bot_users.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def register_user(user_id: int, username: str | None, first_name: str | None):
    try:
        conn = sqlite3.connect("bot_users.db")
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username or "", first_name or "Foydalanuvchi")
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_all_users() -> list[int]:
    conn = sqlite3.connect("bot_users.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


init_db()


# ==========================================
# 2. AUDIO VA SHAZAM FUNKSIYALARI
# ==========================================
def prepare_audios(media_path: str, file_id: str, duration: float = 15.0) -> dict:
    """Video, ovozli xabar yoki dumaloq videodan MP3 va Shazam uchun WAV tayyorlash"""
    mp3_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_audio.mp3")
    wav1_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_shazam1.wav")
    wav2_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_shazam2.wav")
    wav3_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_shazam3.wav")

    # 1. Asl ovozni MP3 qilish
    cmd_mp3 = [
        FFMPEG_PATH, "-y", "-i", media_path,
        "-vn", "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
        mp3_path
    ]
    subprocess.run(cmd_mp3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Mikrofondan yozilgan ovozli xabar / dumaloq videoni tozalash va balandlatish filtri
    voice_boost_filter = "highpass=f=100,lowpass=f=7500,volume=1.8"

    # 2. Shazam 1-urinish: Boshidan 12 soniya (agar qisqa bo'lsa loop qiladi)
    loop_args = ["-stream_loop", "2"] if (0 < duration < 8) else []
    cmd_wav1 = [
        FFMPEG_PATH, "-y", *loop_args, "-i", media_path,
        "-t", "12", "-vn", "-af", voice_boost_filter,
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        wav1_path
    ]
    subprocess.run(cmd_wav1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 3. Shazam 2-urinish: 4-soniyadan boshlab 12 soniya (agar boshida gapirgan bo'lsa)
    if duration > 7:
        cmd_wav2 = [
            FFMPEG_PATH, "-y", "-ss", "4", "-i", media_path,
            "-t", "12", "-vn", "-af", voice_boost_filter,
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            wav2_path
        ]
        subprocess.run(cmd_wav2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 4. Shazam 3-urinish: O'rtasidan (10-soniyadan) 12 soniya
    if duration > 16:
        cmd_wav3 = [
            FFMPEG_PATH, "-y", "-ss", "10", "-i", media_path,
            "-t", "12", "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            wav3_path
        ]
        subprocess.run(cmd_wav3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return {
        "mp3": mp3_path if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 1000 else None,
        "wav1": wav1_path if os.path.exists(wav1_path) and os.path.getsize(wav1_path) > 1000 else None,
        "wav2": wav2_path if os.path.exists(wav2_path) and os.path.getsize(wav2_path) > 1000 else None,
        "wav3": wav3_path if os.path.exists(wav3_path) and os.path.getsize(wav3_path) > 1000 else None,
    }


async def recognize_music_smart(
    wav1: str | None,
    wav2: str | None = None,
    meta_track: dict | None = None,
    wav3: str | None = None
) -> dict | None:
    """Shazam orqali 3 bosqichli + Metadata orqali musiqani aniqlash"""
    for wav_file in [wav1, wav2, wav3]:
        if wav_file and os.path.exists(wav_file):
            try:
                res = await shazam.recognize(wav_file)
                track = res.get("track")
                if track:
                    return {
                        "title": track.get("title", "Noma'lum"),
                        "artist": track.get("subtitle", "Noma'lum")
                    }
            except Exception as e:
                print(f"Shazam xatosi: {e}")

    if meta_track and meta_track.get("title"):
        return meta_track

    return None


def build_music_keyboard(file_id: str, has_orig_mp3: bool = True) -> InlineKeyboardMarkup | None:
    """Musiqa va effektlar uchun zamonaviy tugmalar yasash"""
    track_info = SONG_CACHE.get(file_id)
    buttons = []

    if track_info:
        short_title = track_info['title'][:20]
        buttons.append([
            InlineKeyboardButton(
                text=f"🎧 To'liq MP3 yuklash ({short_title})",
                callback_data=f"eff|{file_id}|normal"
            )
        ])
        buttons.append([
            InlineKeyboardButton(text="🐢 Slowed", callback_data=f"eff|{file_id}|slowed"),
            InlineKeyboardButton(text="🐇 Speed Up", callback_data=f"eff|{file_id}|speedup"),
            InlineKeyboardButton(text="🔊 Bass", callback_data=f"eff|{file_id}|bass"),
        ])

    if has_orig_mp3:
        buttons.append([
            InlineKeyboardButton(
                text="🔊 Asl ovozni (MP3) olish",
                callback_data=f"orig|{file_id}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


# ==========================================
# 3. MEDIA VA MP3 YUKLASH FUNKSIYALARI
# ==========================================
def download_tiktok_fallback(url: str, output_path: str) -> dict | None:
    """TikWM API orqali TikTok video yoki rasmli slayd-shouni suv belgisiz yuklash"""
    try:
        api_url = "https://www.tikwm.com/api/"
        resp = requests.post(api_url, data={"url": url, "hd": 1}, timeout=15).json()
        if resp.get("code") == 0 and resp.get("data"):
            data = resp["data"]
            music_info = data.get("music_info", {})
            meta_track = None
            if music_info.get("title"):
                meta_track = {
                    "title": music_info.get("title"),
                    "artist": music_info.get("author", "Noma'lum")
                }

            images = data.get("images")
            if images and isinstance(images, list):
                audio_url = music_info.get("play") or data.get("play")
                audio_path = output_path.replace(".mp4", "_tik_audio.mp3")
                if audio_url:
                    a_bytes = requests.get(audio_url, timeout=20).content
                    with open(audio_path, "wb") as f:
                        f.write(a_bytes)
                return {
                    "path": audio_path if os.path.exists(audio_path) else None,
                    "images": images[:10],
                    "duration": 15.0,
                    "meta_track": meta_track
                }

            video_url = data.get("play") or data.get("hdplay")
            if video_url:
                video_bytes = requests.get(video_url, timeout=30).content
                with open(output_path, "wb") as f:
                    f.write(video_bytes)
                return {
                    "path": output_path,
                    "images": None,
                    "duration": float(data.get("duration") or 15.0),
                    "meta_track": meta_track
                }
    except Exception as e:
        print(f"TikTok fallback xatosi: {e}")
    return None


def download_video(url: str, file_id: str) -> dict:
    """YouTube, Instagram (Reels/Stories) va TikTok dan videoni yuklab olish"""
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    final_mp4_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")

    url_lower = url.lower()
    is_youtube = any(d in url_lower for d in ["youtube.com", "youtu.be"])
    is_tiktok = "tiktok.com" in url_lower

    if is_tiktok and "/photo/" in url_lower:
        fb = download_tiktok_fallback(url, final_mp4_path)
        if fb:
            return fb

    if is_youtube:
        fmt = (
            "bestvideo[height<=720][filesize<42M][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=720][ext=mp4]/best[height<=480]/best"
        )
    elif is_tiktok:
        fmt = "best[vcodec^=avc1][ext=mp4]/best[ext=mp4]/best"
    else:
        fmt = "best[ext=mp4]/bestvideo+bestaudio/best"

    ydl_opts = {
        'format': fmt,
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'ffmpeg_location': os.path.dirname(FFMPEG_PATH),
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web']
            }
        },
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]

            final_path = final_mp4_path
            if not os.path.exists(final_path):
                filename = ydl.prepare_filename(info)
                base, _ = os.path.splitext(filename)
                for ext in [".mp4", ".mkv", ".webm"]:
                    if os.path.exists(base + ext):
                        final_path = base + ext
                        break
                else:
                    final_path = filename

            vcodec = info.get("vcodec", "")
            if os.path.exists(final_path) and (not final_path.endswith(".mp4") or "hev" in vcodec or "hvc" in vcodec or "bytevc1" in vcodec):
                mp4_converted = os.path.join(DOWNLOAD_DIR, f"{file_id}_conv.mp4")
                cmd = [
                    FFMPEG_PATH, "-y", "-i", final_path,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                    "-c:a", "aac", "-movflags", "+faststart", mp4_converted
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(mp4_converted):
                    os.remove(final_path)
                    final_path = mp4_converted

            meta_track = None
            if info.get("track"):
                meta_track = {
                    "title": info.get("track"),
                    "artist": info.get("artist") or info.get("uploader", "Noma'lum")
                }

            return {
                "path": final_path,
                "images": None,
                "duration": float(info.get("duration") or 15.0),
                "meta_track": meta_track
            }

    except Exception as e:
        if is_tiktok:
            fallback_res = download_tiktok_fallback(url, final_mp4_path)
            if fallback_res:
                return fallback_res
        raise e


def search_youtube_songs(query: str, limit: int = 5) -> list[dict]:
    """Matn orqali yozilganda YouTube'dan 5 ta qo'shiq topib berish"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query} audio", download=False)
            for entry in info.get("entries", []):
                if entry and entry.get("id"):
                    results.append({
                        "id": entry["id"],
                        "title": entry.get("title", query),
                        "uploader": entry.get("uploader") or entry.get("channel") or "Musiqa",
                        "duration": entry.get("duration") or 0
                    })
    except Exception as e:
        print(f"Qidiruv xatosi: {e}")
    return results


def download_full_song(query_or_url: str, file_id: str, effect: str = "normal") -> str | None:
    """
    1-urinishdayoq 100% MP3 yuklab beruvchi funksiya.
    Agar asosiy audio allaqachon yuklangan bo'lsa, Slowed/SpeedUp uchun YouTube'ga qayta ulanmaydi!
    """
    base_mp3 = os.path.join(DOWNLOAD_DIR, f"{file_id}_base.mp3")
    unique_id = str(uuid.uuid4())[:6]
    raw_template = os.path.join(DOWNLOAD_DIR, f"{file_id}_{unique_id}_raw.%(ext)s")
    final_mp3 = os.path.join(DOWNLOAD_DIR, f"{file_id}_{effect}_{unique_id}.mp3")

    # 1. Agar bazaviy qo'shiq hali yuklanmagan bo'lsa, YouTube'dan tortamiz
    if not os.path.exists(base_mp3):
        clean_query = re.sub(r'[^\w\s\-]', '', query_or_url).strip() or query_or_url
        # ytsearch5 ishlatamiz (1-natija xato bo'lsa, avtomatik 2-sini oladi!)
        target = query_or_url if query_or_url.startswith("http") else f"ytsearch5:{clean_query} audio"

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': raw_template,
            'noplaylist': True,
            'max_downloads': 1,
            'ffmpeg_location': os.path.dirname(FFMPEG_PATH),
            'geo_bypass': True,
            'nocheckcertificate': True,
            'socket_timeout': 15,
            'retries': 3,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios', 'web']
                }
            },
            'match_filter': yt_dlp.utils.match_filter_func("duration < 900 & !is_live"),
            'quiet': True,
            'no_warnings': True,
        }

        raw_file = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(target, download=True)
        except yt_dlp.utils.MaxDownloadsReached:
            pass
        except Exception as e:
            print(f"YouTube qidiruv xatosi: {e}")

        # Yuklangan xom faylni papkadan topamiz
        prefix = f"{file_id}_{unique_id}_raw."
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(prefix):
                raw_file = os.path.join(DOWNLOAD_DIR, fname)
                break

        if not raw_file or not os.path.exists(raw_file):
            return None

        # Xom audioni toza bazaviy MP3 ga o'girib saqlab qo'yamiz
        try:
            cmd_base = [
                FFMPEG_PATH, "-y", "-i", raw_file,
                "-vn", "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                base_mp3
            ]
            subprocess.run(cmd_base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        finally:
            if os.path.exists(raw_file):
                os.remove(raw_file)

    if not os.path.exists(base_mp3):
        return None

    # 2. Agar oddiy (normal) MP3 so'ralgan bo'lsa, tayyor fayldan nusxa beramiz
    if effect == "normal":
        shutil.copyfile(base_mp3, final_mp3)
        return final_mp3

    # 3. Agar Slowed / SpeedUp / Bass so'ralgan bo'lsa, tayyor base_mp3 ustida 1 soniyada effekt qo'llaymiz
    if effect == "slowed":
        af_filter = "asetrate=44100*0.85,aresample=44100,atempo=0.95,aecho=0.8:0.88:60:0.4"
    elif effect == "speedup":
        af_filter = "asetrate=44100*1.22,aresample=44100,atempo=1.02"
    elif effect == "bass":
        af_filter = "bass=g=14:f=110:w=0.6"
    else:
        af_filter = "anull"

    try:
        cmd_eff = [
            FFMPEG_PATH, "-y", "-i", base_mp3,
            "-vn", "-af", af_filter,
            "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            final_mp3
        ]
        subprocess.run(cmd_eff, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return final_mp3 if os.path.exists(final_mp3) else None
    except Exception as e:
        print(f"Effekt berish xatosi: {e}")
        return None


# ==========================================
# 4. BOT HANDLERLARI (START, ADMIN, MEDIA)
# ==========================================
@dp.message(CommandStart())
async def start_cmd(message: Message):
    register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    welcome_text = (
        f"✨ <b>Salom, {message.from_user.first_name}!</b>\n\n"
        "📥 <b>Men ko'p funksiyali Media Yuklovchi va Shazam botman!</b>\n\n"
        "<blockquote>"
        "🎬 <b>Video yuklash:</b> Instagram (Reels/Stories), TikTok va YouTube linkini yuboring.\n"
        "🎵 <b>Musiqa qidirish:</b> Shunchaki qo'shiq yoki ijrochi nomini yozib yuboring.\n"
        "🎤 <b>Shazam:</b> Ovozli xabar (golosovoy), audio, oddiy video yoki dumaloq video yuboring — qo'shiqni topib beraman!\n"
        "🎛 <b>Effektlar:</b> Har qanday qo'shiqni <i>Slowed, Speed Up</i> va <i>Bass Boost</i> qilish imkoniyati!"
        "</blockquote>\n\n"
        "👇 <i>Link, qo'shiq nomi yoki ovozli/dumaloq xabar yuboring:</i>"
    )
    await message.answer(welcome_text)


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message):
        await message.reply(
            f"ℹ️ Sizning Telegram ID raqamingiz: <code>{message.from_user.id}</code>\n"
            f"Username: @{message.from_user.username or 'yoq'}"
        )
        return

    users_count = len(get_all_users())
    text = (
        "🛠 <b>Boshqaruv Paneli (Admin)</b>\n\n"
        f"👥 <b>Jami foydalanuvchilar:</b> {users_count} ta\n\n"
        "📢 <b>Xabar tarqatish (Rassilka):</b>\n"
        "<code>/broadcast Sizning reklama matningiz</code> ko'rinishida yuboring."
    )
    await message.answer(text)


@dp.message(Command("broadcast"))
async def broadcast_handler(message: Message):
    if not is_admin(message):
        return

    text = message.text.replace("/broadcast", "", 1).strip()
    if not text:
        await message.reply("⚠️ Iltimos, xabar matnini ham yozing!\nMasalan: <code>/broadcast Salom barchaga!</code>")
        return

    users = get_all_users()
    status = await message.reply(f"⏳ Xabar {len(users)} ta foydalanuvchiga yuborilmoqda...")
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass

    await status.edit_text(f"✅ <b>Xabar tarqatish yakunlandi!</b>\n📊 Yetib bordi: <b>{sent} / {len(users)}</b> ta foydalanuvchiga.")


# --- OVOZLI XABAR, DUMALOQ VIDEO, ODDIY VIDEO VA AUDIO ORQALI SHAZAM ---
@dp.message(F.voice | F.video_note | F.audio | F.video)
async def recognize_media_message(message: Message):
    register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    status_msg = await message.reply(
        "🎵 <b>Shazam ishlamoqda...</b>\n"
        "<blockquote>🔍 Yuborilgan mediadagi musiqa aniqlanmoqda, biroz kuting...</blockquote>"
    )

    file_id = str(uuid.uuid4())[:8]
    raw_path = None
    audios = {}

    try:
        media = message.voice or message.video_note or message.audio or message.video
        file_info = await bot.get_file(media.file_id)

        # Faylning haqiqiy kengaytmasini (.oga, .mp4, .mp3) saqlab qolamiz
        ext = os.path.splitext(file_info.file_path or "")[1]
        if not ext:
            ext = ".mp4" if (message.video_note or message.video) else ".ogg"

        raw_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_input{ext}")
        await bot.download_file(file_info.file_path, destination=raw_path)

        duration = float(getattr(media, "duration", 12) or 12)
        audios = await asyncio.to_thread(prepare_audios, raw_path, file_id, duration)
        track_info = await recognize_music_smart(
            audios.get("wav1"),
            audios.get("wav2"),
            None,
            audios.get("wav3")
        )

        has_mp3 = bool(audios.get("mp3"))

        if track_info:
            SONG_CACHE[file_id] = track_info
            text = (
                f"✅ <b>Musiqa topildi!</b>\n\n"
                f"<blockquote>🎵 <b>Nomi:</b> {track_info['title']}\n"
                f"👤 <b>Ijrochi:</b> {track_info['artist']}</blockquote>\n"
                f"👇 <i>Kerakli formatni tanlab yuklab oling:</i>"
            )
            kb = build_music_keyboard(file_id, has_orig_mp3=has_mp3)
            await status_msg.edit_text(text, reply_markup=kb)
        else:
            kb = build_music_keyboard(file_id, has_orig_mp3=has_mp3)
            await status_msg.edit_text(
                "😕 <b>Kechirasiz, bu ovozdan musiqani aniqlab bo'lmadi.</b>\n"
                "<blockquote>Ovoz juda qisqa, shovqinli yoki mualliflik ovozi bo'lishi mumkin. "
                "Ovozni 8–10 soniya davomida aniqroq yozib qayta yuborib ko'ring.</blockquote>",
                reply_markup=kb
            )

    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Xatolik yuz berdi:</b>\n<blockquote>{str(e)}</blockquote>")
    finally:
        for p in [raw_path, audios.get("wav1"), audios.get("wav2"), audios.get("wav3")]:
            if p and os.path.exists(p):
                os.remove(p)
        asyncio.create_task(cleanup_later(audios.get("mp3"), file_id, delay=900))  # noqa


# --- LINK ORQALI VIDEO VA MUSIQA ANIQLASH ---
@dp.message(F.text.regexp(r'(https?://\S+)'))
async def process_link(message: Message):
    register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    match = re.search(r'(https?://\S+)', message.text)
    if not match:
        return
    url = match.group(1)

    supported = ["instagram.com", "tiktok.com", "youtube.com", "youtu.be", "pinterest.com", "pin.it"]
    if not any(d in url.lower() for d in supported):
        await message.reply("⚠️ <b>Kechirasiz!</b> Faqat Instagram, TikTok, YouTube yoki Pinterest linkini yuboring.")
        return

    status_msg = await message.reply(
        "⚡️ <b>Media tahlil qilinmoqda...</b>\n"
        "<blockquote>⏳ Video serverga yuklanmoqda, biroz kuting...</blockquote>"
    )
    await bot.send_chat_action(chat_id=message.chat.id, action="upload_video")

    file_id = str(uuid.uuid4())[:8]
    video_path = None
    audios = {}

    try:
        video_data = await asyncio.to_thread(download_video, url, file_id)
        video_path = video_data.get("path")
        images = video_data.get("images")

        if images:
            for img_url in images[:5]:
                await message.reply_photo(photo=img_url)

        if not video_path or not os.path.exists(video_path):
            if not images:
                await status_msg.edit_text(
                    "❌ <b>Videoni yuklab bo'lmadi.</b>\n"
                    "<blockquote>Profil yopiq bo'lishi yoki link xato bo'lishi mumkin.</blockquote>"
                )
            else:
                await status_msg.delete()
            return

        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)

        await status_msg.edit_text(
            "🎵 <b>Musiqa qidirilmoqda...</b>\n"
            "<blockquote>🔍 Shazam orqali videodagi audio aniqlanmoqda...</blockquote>"
        )
        audios = await asyncio.to_thread(
            prepare_audios, video_path, file_id, video_data["duration"]
        )

        track_info = await recognize_music_smart(
            audios.get("wav1"),
            audios.get("wav2"),
            video_data.get("meta_track"),
            audios.get("wav3")
        )

        if track_info:
            SONG_CACHE[file_id] = track_info
            caption = (
                f"🎬 <b>Video tayyor!</b>\n\n"
                f"<blockquote>🎵 <b>Musiqa:</b> {track_info['title']}\n"
                f"👤 <b>Ijrochi:</b> {track_info['artist']}</blockquote>\n"
                f"👇 <i>Musiqani yuklab olish uchun quyidagi tugmalardan foydalaning:</i>"
            )
        else:
            caption = (
                f"🎬 <b>Video tayyor!</b>\n\n"
                f"<blockquote>ℹ️ <i>Videoda mashhur musiqa aniqlanmadi (yoki mualliflik ovozi).</i></blockquote>"
            )

        keyboard = build_music_keyboard(file_id, has_orig_mp3=bool(audios.get("mp3")))

        if not images and file_size_mb <= 49.5:
            await status_msg.delete()
            video_file = FSInputFile(video_path)
            await message.reply_video(
                video=video_file,
                caption=caption,
                reply_markup=keyboard,
                supports_streaming=True
            )
        else:
            await status_msg.edit_text(caption, reply_markup=keyboard)

    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Xatolik yuz berdi:</b>\n<blockquote>{str(e)}</blockquote>")

    finally:
        for p in [video_path, audios.get("wav1"), audios.get("wav2"), audios.get("wav3")]:
            if p and os.path.exists(p):
                os.remove(p)
        asyncio.create_task(cleanup_later(audios.get("mp3"), file_id, delay=900))  # noqa


# --- MATN ORQALI QO'SHIQ QIDIRISH ---
@dp.message(F.text & ~F.text.startswith("/"))
async def search_music_by_text(message: Message):
    register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    query = message.text.strip()
    if len(query) < 2:
        return

    status = await message.reply(f"🔍 <b>«{query}»</b> bo'yicha qo'shiqlar qidirilmoqda...")
    results = await asyncio.to_thread(search_youtube_songs, query, 5)

    if not results:
        await status.edit_text("❌ Hech qanday qo'shiq topilmadi. Boshqa nom bilan yozib ko'ring.")
        return

    text_lines = [f"🎧 <b>«{query}» bo'yicha topilgan musiqalar:</b>\n"]
    buttons = []
    row = []

    for idx, item in enumerate(results, start=1):
        s_id = str(uuid.uuid4())[:8]
        SONG_CACHE[s_id] = {
            "title": item["title"],
            "artist": item["uploader"],
            "url": f"https://www.youtube.com/watch?v={item['id']}"
        }
        mins, secs = divmod(int(item["duration"] or 0), 60)
        dur_str = f"{mins}:{secs:02d}" if item["duration"] else ""
        text_lines.append(f"<b>{idx}.</b> {item['title']} <i>({dur_str})</i>")
        row.append(InlineKeyboardButton(text=f"{idx} 🎵", callback_data=f"eff|{s_id}|normal"))

    buttons.append(row)
    text_lines.append("\n👇 <i>Yuklab olish uchun mos raqamni bosing:</i>")

    await status.edit_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ==========================================
# 5. INLINE REJIM (@bot_nomi qo'shiq nomi)
# ==========================================
@dp.inline_query()
async def inline_music_search(inline_query: InlineQuery):
    query = inline_query.query.strip()
    if not query:
        return

    results = await asyncio.to_thread(search_youtube_songs, query, 5)
    articles = []
    for item in results:
        yt_url = f"https://www.youtube.com/watch?v={item['id']}"
        articles.append(
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=f"🎵 {item['title']}",
                description=f"👤 {item['uploader']} — Yuklab olish uchun bosing",
                input_message_content=InputTextMessageContent(
                    message_text=yt_url
                )
            )
        )
    await inline_query.answer(articles, cache_time=60)


# ==========================================
# 6. TUGMALAR (CALLBACK) HANDLERLARI
# ==========================================
@dp.callback_query(F.data.startswith("orig|"))
async def send_original_audio(call: CallbackQuery):
    file_id = call.data.split("|")[1]
    audio_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_audio.mp3")

    if not os.path.exists(audio_path):
        await call.answer("⏳ Bu fayl eskirgan, qaytadan yuboring!", show_alert=True)
        return

    await call.answer("📤 Asl audio yuborilmoqda...")
    await bot.send_chat_action(chat_id=call.message.chat.id, action="upload_voice")
    audio_file = FSInputFile(audio_path, filename="original_audio.mp3")
    await call.message.reply_audio(
        audio=audio_file,
        title="Original Audio",
        performer="Media Sound",
        caption="🔊 <b>Mediadan qirqib olingan asl ovoz</b>"
    )


@dp.callback_query(F.data.startswith("eff|"))
async def send_effect_audio(call: CallbackQuery):
    _, file_id, effect = call.data.split("|")
    track_info = SONG_CACHE.get(file_id)

    if not track_info:
        await call.answer("⏳ Ma'lumot eskirgan, qaytadan urinib ko'ring!", show_alert=True)
        return

    # Foydalanuvchi tugmani ketma-ket bosib yuborishini bloklaymiz
    lock_key = f"{call.from_user.id}_{file_id}_{effect}"
    if lock_key in ACTIVE_DOWNLOADS:
        await call.answer("⏳ Musiqa hozir yuklanmoqda, iltimos biroz kuting...", show_alert=False)
        return

    ACTIVE_DOWNLOADS.add(lock_key)

    effect_names = {
        "normal": "Original MP3",
        "slowed": "Slowed + Reverb 🐢",
        "speedup": "Speed Up 🐇",
        "bass": "Bass Boosted 🔊"
    }

    await call.answer(f"📥 {effect_names.get(effect)} yuklanmoqda...")
    wait_msg = await call.message.reply(
        f"⏳ <b>{track_info['artist']} — {track_info['title']}</b>\n"
        f"<blockquote>📥 <i>{effect_names.get(effect)} tayyorlanmoqda, biroz kuting...</i></blockquote>"
    )
    await bot.send_chat_action(chat_id=call.message.chat.id, action="upload_voice")

    try:
        target = track_info.get("url") or f"{track_info['artist']} - {track_info['title']}"
        song_path = await asyncio.to_thread(download_full_song, target, file_id, effect)

        if song_path and os.path.exists(song_path):
            eff_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🐢 Slowed", callback_data=f"eff|{file_id}|slowed"),
                    InlineKeyboardButton(text="🐇 Speed Up", callback_data=f"eff|{file_id}|speedup"),
                    InlineKeyboardButton(text="🔊 Bass", callback_data=f"eff|{file_id}|bass"),
                ]
            ])
            audio_file = FSInputFile(song_path, filename=f"{track_info['title']} ({effect}).mp3")
            await wait_msg.delete()
            await call.message.reply_audio(
                audio=audio_file,
                title=f"{track_info['title']} ({effect_names.get(effect)})",
                performer=track_info['artist'],
                caption=(
                    f"🎧 <b>{track_info['artist']} - {track_info['title']}</b>\n"
                    f"✨ <i>Format: {effect_names.get(effect)}</i>"
                ),
                reply_markup=eff_kb
            )
            os.remove(song_path)
        else:
            await wait_msg.edit_text("❌ <b>Kechirasiz, bu musiqani yuklab bo'lmadi.</b>\n<i>Qaytadan urinib ko'ring.</i>")
    except Exception as e:
        await wait_msg.edit_text(f"❌ <b>Xatolik:</b> {str(e)}")
    finally:
        ACTIVE_DOWNLOADS.discard(lock_key)


async def cleanup_later(filepath: str | None, file_id: str, delay: int):
    await asyncio.sleep(delay)
    for p in [filepath, os.path.join(DOWNLOAD_DIR, f"{file_id}_base.mp3")]:
        if p and os.path.exists(p):
            os.remove(p)
    SONG_CACHE.pop(file_id, None)


async def handle_health(request):
    return web.Response(text="Bot 24/7 ishlamoqda!")


async def start_web_server():
    """Server uxlab qolmasligi uchun 7860-portni ochib qo'yamiz"""
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 7860))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    global BOT_USERNAME
    await start_web_server()

    # Barcha xabar turlarini (voice, video_note, video, text, callback) qabul qilishni majburlaymiz
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    BOT_USERNAME = me.username
    print(f"🚀 @{BOT_USERNAME} muvaffaqiyatli ishga tushdi! (FFmpeg: {FFMPEG_PATH})")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())