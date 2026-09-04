# main.py

import os
import re
import json
import time
import uuid
import shutil
import sqlite3
import asyncio
import logging
import mimetypes
import tempfile
from pathlib import Path
from urllib.parse import (
    urlparse,
    parse_qs,
    urlencode,
    urlunparse,
    unquote,
)

import aiohttp
import aiofiles
from flask import Flask, request, redirect, render_template_string, session
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@")

PORT = int(os.getenv("PORT", "8080"))
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET_KEY"
)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1800"))
MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "3")
)

DEFAULT_PDF_PRICE = int(
    os.getenv("PREMIUM_PDF_PRICE", "49")
)

DEFAULT_VIDEO_PRICE = int(
    os.getenv("PREMIUM_VIDEO_PRICE", "99")
)

DEFAULT_PREMIUM_DAYS = int(
    os.getenv("PREMIUM_DAYS", "30")
)

DEFAULT_UPI = os.getenv("UPI_ID", "").strip()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
WELCOME_DIR = DATA_DIR / "welcome"

DATA_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)
WELCOME_DIR.mkdir(exist_ok=True)

DB_FILE = DATA_DIR / "bot.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("download-bot")

download_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)

# ============================================================
# DATABASE
# ============================================================

def db():
    connection = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            premium INTEGER DEFAULT 0,
            premium_until INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan TEXT,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at INTEGER,
            approved_at INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            file_type TEXT,
            filename TEXT,
            size INTEGER DEFAULT 0,
            status TEXT DEFAULT 'processing',
            created_at INTEGER
        )
    """)

    defaults = {
        "welcome_text": (
            "🎓 <b>Welcome to Premium Downloader</b>\n\n"
            "📥 PDF और Video download करने के लिए नीचे दिए "
            "options का उपयोग करें।"
        ),
        "help_text": (
            "🆘 <b>Help</b>\n\n"
            "• PDF/Video का public URL भेजें\n"
            "• Multiple links के लिए TXT file भेजें\n"
            "• Premium download के लिए Buy Premium खोलें\n\n"
            "अगर कोई समस्या हो तो admin से संपर्क करें।"
        ),
        "pdf_price": str(DEFAULT_PDF_PRICE),
        "video_price": str(DEFAULT_VIDEO_PRICE),
        "premium_days": str(DEFAULT_PREMIUM_DAYS),
        "upi_id": DEFAULT_UPI,
        "payment_text": (
            "Payment करने के बाद transaction screenshot "
            "और UTR भेजें।"
        ),
    }

    for key, value in defaults.items():
        cur.execute(
            """
            INSERT OR IGNORE INTO settings(key, value)
            VALUES (?, ?)
            """,
            (key, value),
        )

    con.commit()
    con.close()


def get_setting(key, default=""):
    con = db()
    row = con.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    ).fetchone()
    con.close()

    return row["value"] if row else default


def set_setting(key, value):
    con = db()

    con.execute(
        """
        INSERT INTO settings(key,value)
        VALUES (?,?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (key, str(value)),
    )

    con.commit()
    con.close()


def upsert_user(user):
    now = int(time.time())

    con = db()

    con.execute(
        """
        INSERT INTO users(
            user_id,
            username,
            first_name,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            updated_at=excluded.updated_at
        """,
        (
            user.id,
            user.username or "",
            user.first_name or "",
            now,
            now,
        ),
    )

    con.commit()
    con.close()


def is_premium(user_id):
    con = db()

    row = con.execute(
        """
        SELECT premium, premium_until
        FROM users
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()

    con.close()

    if not row:
        return False

    return bool(
        row["premium"]
        and row["premium_until"] > int(time.time())
    )


def activate_premium(user_id, days):
    until = int(time.time()) + days * 86400

    con = db()

    con.execute(
        """
        UPDATE users
        SET premium=1,
            premium_until=?,
            updated_at=?
        WHERE user_id=?
        """,
        (until, int(time.time()), user_id),
    )

    con.commit()
    con.close()


def create_payment(user_id, plan, amount):
    con = db()

    cur = con.execute(
        """
        INSERT INTO payments(
            user_id,
            plan,
            amount,
            status,
            created_at
        )
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (
            user_id,
            plan,
            amount,
            int(time.time()),
        ),
    )

    payment_id = cur.lastrowid

    con.commit()
    con.close()

    return payment_id


def get_pending_payment(payment_id):
    con = db()

    row = con.execute(
        """
        SELECT *
        FROM payments
        WHERE id=?
        """,
        (payment_id,),
    ).fetchone()

    con.close()

    return row


def approve_payment(payment_id):
    con = db()

    row = con.execute(
        """
        SELECT *
        FROM payments
        WHERE id=?
        """,
        (payment_id,),
    ).fetchone()

    if not row:
        con.close()
        return None

    con.execute(
        """
        UPDATE payments
        SET status='approved',
            approved_at=?
        WHERE id=?
        """,
        (int(time.time()), payment_id),
    )

    con.commit()
    con.close()

    return row


def create_download(user_id, url, file_type, filename):
    con = db()

    cur = con.execute(
        """
        INSERT INTO downloads(
            user_id,
            url,
            file_type,
            filename,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            url,
            file_type,
            filename,
            int(time.time()),
        ),
    )

    download_id = cur.lastrowid

    con.commit()
    con.close()

    return download_id


# ============================================================
# URL SECURITY / NORMALIZATION
# ============================================================

def is_http_url(value):
    try:
        parsed = urlparse(value.strip())

        return parsed.scheme.lower() in {
            "http",
            "https",
        } and bool(parsed.netloc)

    except Exception:
        return False


def clean_url(value):
    value = value.strip()

    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()

    value = value.strip('"').strip("'")

    return value


def extract_urls(text):
    pattern = r'https?://[^\s<>"\'\]\)]+'

    found = re.findall(
        pattern,
        text,
        flags=re.IGNORECASE
    )

    result = []

    for item in found:
        item = item.rstrip(".,;")

        if is_http_url(item):
            result.append(item)

    return list(dict.fromkeys(result))


# ============================================================
# GOOGLE DRIVE
# ============================================================

def google_drive_file_id(url):
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)

        if match:
            return match.group(1)

    return None


def normalize_google_drive(url):
    parsed = urlparse(url)

    host = parsed.netloc.lower()

    if (
        "drive.google.com" not in host
        and "docs.google.com" not in host
    ):
        return url

    file_id = google_drive_file_id(url)

    if not file_id:
        return url

    return (
        "https://drive.usercontent.google.com/"
        f"download?id={file_id}&export=download"
    )


# ============================================================
# HEADERS
# ============================================================

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0 Mobile Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def build_headers(url):
    headers = dict(COMMON_HEADERS)

    parsed = urlparse(url)

    if parsed.scheme in {"http", "https"}:
        headers["Referer"] = (
            f"{parsed.scheme}://{parsed.netloc}/"
        )

    return headers


# ============================================================
# FILENAME
# ============================================================

def sanitize_filename(name):
    name = unquote(name or "").strip()

    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    ).strip()

    if not name:
        name = "download"

    return name[:180]


def filename_from_headers(response, url):
    content_disposition = response.headers.get(
        "Content-Disposition",
        ""
    )

    match = re.search(
        r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?',
        content_disposition,
        re.IGNORECASE,
    )

    if match:
        return sanitize_filename(
            unquote(match.group(1))
        )

    path = urlparse(url).path

    name = Path(path).name

    return sanitize_filename(name)


# ============================================================
# DOWNLOAD ENGINE
# ============================================================

async def download_url(url, output_dir):
    url = normalize_google_drive(url)

    timeout = aiohttp.ClientTimeout(
        total=DOWNLOAD_TIMEOUT,
        connect=60,
        sock_read=120,
    )

    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=20,
        ttl_dns_cache=300,
    )

    headers = build_headers(url)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers=headers,
        raise_for_status=False,
    ) as session:

        async with session.get(
            url,
            allow_redirects=True,
        ) as response:

            if response.status >= 400:
                raise RuntimeError(
                    f"HTTP {response.status}"
                )

            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length:
                try:
                    size = int(content_length)

                    if size > MAX_FILE_SIZE:
                        raise RuntimeError(
                            f"File exceeds {MAX_FILE_SIZE_MB} MB"
                        )

                except ValueError:
                    pass

            filename = filename_from_headers(
                response,
                str(response.url),
            )

            content_type = (
                response.headers.get(
                    "Content-Type",
                    ""
                )
                .lower()
            )

            if filename == "download":
                if "pdf" in content_type:
                    filename = "document.pdf"

                elif "video" in content_type:
                    filename = "video.mp4"

                else:
                    ext = mimetypes.guess_extension(
                        content_type.split(";")[0]
                    )

                    filename += ext or ""

            if "." not in filename:
                if "pdf" in content_type:
                    filename += ".pdf"

                elif "video" in content_type:
                    filename += ".mp4"

            filename = sanitize_filename(filename)

            output_path = (
                Path(output_dir) / f"{uuid.uuid4().hex}_{filename}"
            )

            total = 0

            async with aiofiles.open(
                output_path,
                "wb"
            ) as file:

                async for chunk in response.content.iter_chunked(
                    1024 * 1024
                ):
                    total += len(chunk)

                    if total > MAX_FILE_SIZE:
                        await file.close()

                        try:
                            output_path.unlink()
                        except Exception:
                            pass

                        raise RuntimeError(
                            f"File exceeds {MAX_FILE_SIZE_MB} MB"
                        )

                    await file.write(chunk)

            if total <= 0:
                try:
                    output_path.unlink()
                except Exception:
                    pass

                raise RuntimeError(
                    "Downloaded file is empty"
                )

            return {
                "path": str(output_path),
                "filename": filename,
                "size": total,
                "content_type": content_type,
                "final_url": str(response.url),
            }


# ============================================================
# DOWNLOAD TYPE
# ============================================================

def detect_type(filename="", content_type=""):
    value = (
        f"{filename} {content_type}"
        .lower()
    )

    if (
        ".pdf" in value
        or "application/pdf" in value
    ):
        return "pdf"

    video_extensions = (
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".m4v",
        ".ts",
        ".m3u8",
        ".mpd",
    )

    if any(ext in value for ext in video_extensions):
        return "video"

    if "video/" in content_type:
        return "video"

    return "unknown"


# ============================================================
# TELEGRAM UI
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📄 PDF Download",
                callback_data="download_pdf",
            ),
            InlineKeyboardButton(
                "🎥 Video Download",
                callback_data="download_video",
            ),
        ],
        [
            InlineKeyboardButton(
                "💎 Buy Premium",
                callback_data="premium",
            ),
        ],
        [
            InlineKeyboardButton(
                "🆘 Help",
                callback_data="help",
            ),
        ],
    ])


def premium_keyboard():
    pdf_price = get_setting(
        "pdf_price",
        str(DEFAULT_PDF_PRICE)
    )

    video_price = get_setting(
        "video_price",
        str(DEFAULT_VIDEO_PRICE)
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"📄 Premium PDF ₹{pdf_price}",
                callback_data="buy_pdf",
            )
        ],
        [
            InlineKeyboardButton(
                f"🎥 Premium Video ₹{video_price}",
                callback_data="buy_video",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="home",
            )
        ],
    ])


# ============================================================
# WELCOME
# ============================================================

async def send_welcome(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if user:
        upsert_user(user)

    text = get_setting(
        "welcome_text",
        "Welcome"
    )

    image_files = list(
        WELCOME_DIR.glob("*")
    )

    if update.callback_query:
        query = update.callback_query

        try:
            await query.message.delete()
        except Exception:
            pass

        if image_files:
            try:
                with open(
                    image_files[0],
                    "rb"
                ) as photo:
                    await query.message.chat.send_photo(
                        photo=photo,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=main_keyboard(),
                    )
                    return
            except Exception:
                pass

        await query.message.chat.send_message(
            text=text,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

        return

    chat_id = update.effective_chat.id

    if image_files:
        try:
            with open(
                image_files[0],
                "rb"
            ) as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=main_keyboard(),
                )
                return
        except Exception:
            pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await send_welcome(
        update,
        context
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    user = query.from_user

    upsert_user(user)

    data = query.data

    if data == "home":
        await send_welcome(
            update,
            context
        )
        return

    if data == "premium":
        await query.message.edit_text(
            "💎 <b>Premium Plans</b>\n\n"
            "Premium खरीदने के लिए नीचे plan select करें।",
            parse_mode="HTML",
            reply_markup=premium_keyboard(),
        )
        return

    if data == "help":
        await query.message.edit_text(
            get_setting(
                "help_text",
                "Help unavailable"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home",
                    )
                ]
            ]),
        )
        return

    if data in {
        "download_pdf",
        "download_video",
    }:

        requested_type = (
            "pdf"
            if data == "download_pdf"
            else "video"
        )

        context.user_data[
            "requested_type"
        ] = requested_type

        label = (
            "PDF"
            if requested_type == "pdf"
            else "Video"
        )

        await query.message.edit_text(
            f"📥 <b>{label} Download</b>\n\n"
            f"अब {label} का direct/public URL भेजें।\n\n"
            "Multiple links के लिए TXT file भी भेज सकते हैं।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home",
                    )
                ]
            ]),
        )

        return

    if data in {"buy_pdf", "buy_video"}:

        plan = (
            "pdf"
            if data == "buy_pdf"
            else "video"
        )

        amount = int(
            get_setting(
                "pdf_price"
                if plan == "pdf"
                else "video_price",
                "49"
            )
        )

        payment_id = create_payment(
            user.id,
            plan,
            amount
        )

        upi = get_setting(
            "upi_id",
            DEFAULT_UPI
        )

        payment_text = get_setting(
            "payment_text",
            ""
        )

        await query.message.edit_text(
            (
                f"💎 <b>Premium {plan.upper()}</b>\n\n"
                f"💰 Price: ₹{amount}\n"
                f"🆔 Payment ID: <code>{payment_id}</code>\n\n"
                f"💳 UPI: <code>{upi}</code>\n\n"
                f"{payment_text}\n\n"
                "Payment के बाद इसी chat में "
                "screenshot + UTR भेजें।"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📤 Send Payment Proof",
                        callback_data=(
                            f"proof_{payment_id}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="premium",
                    )
                ],
            ]),
        )

        return

    if data.startswith("proof_"):
        payment_id = data.split("_", 1)[1]

        context.user_data[
            "proof_payment_id"
        ] = payment_id

        await query.message.reply_text(
            "📤 अब payment screenshot भेजें।\n\n"
            "साथ में UTR/Transaction ID भी text में भेज सकते हैं।"
        )

        return


# ============================================================
# PAYMENT PROOF
# ============================================================

async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    upsert_user(user)

    payment_id = context.user_data.get(
        "proof_payment_id"
    )

    if not payment_id:
        await update.message.reply_text(
            "पहले Buy Premium से payment plan select करें।"
        )
        return

    payment = get_pending_payment(
        payment_id
    )

    if not payment:
        await update.message.reply_text(
            "❌ Payment request नहीं मिली।"
        )
        return

    if payment["user_id"] != user.id:
        await update.message.reply_text(
            "❌ Invalid payment request."
        )
        return

    await update.message.reply_text(
        "✅ Payment proof receive हो गया है।\n"
        "Admin verification के बाद premium activate होगा।"
    )

    caption = (
        "💳 <b>New Payment Proof</b>\n\n"
        f"Payment ID: <code>{payment_id}</code>\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Username: @{user.username or 'N/A'}\n"
        f"Plan: {payment['plan']}\n"
        f"Amount: ₹{payment['amount']}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"approve_{payment_id}",
            ),
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"reject_{payment_id}",
            ),
        ]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=update.message.photo[-1].file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error(
                "Admin proof send error: %s",
                exc
            )

    context.user_data.pop(
        "proof_payment_id",
        None
    )


# ============================================================
# ADMIN PAYMENT CALLBACK
# ============================================================

async def admin_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer(
            "Unauthorized",
            show_alert=True
        )
        return

    await query.answer()

    data = query.data

    if data.startswith("approve_"):
        payment_id = int(
            data.split("_", 1)[1]
        )

        payment = approve_payment(
            payment_id
        )

        if not payment:
            await query.edit_message_caption(
                "❌ Payment not found."
            )
            return

        days = int(
            get_setting(
                "premium_days",
                str(DEFAULT_PREMIUM_DAYS)
            )
        )

        activate_premium(
            payment["user_id"],
            days
        )

        await query.edit_message_caption(
            (
                "✅ <b>Payment Approved</b>\n\n"
                f"Payment ID: <code>{payment_id}</code>\n"
                f"User ID: <code>{payment['user_id']}</code>\n"
                f"Plan: {payment['plan']}\n"
                f"Premium: {days} days"
            ),
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text=(
                    "🎉 <b>Premium Activated!</b>\n\n"
                    f"Plan: {payment['plan'].upper()}\n"
                    f"Validity: {days} days\n\n"
                    "अब आप premium download कर सकते हैं।"
                ),
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
        except Exception:
            pass

        return

    if data.startswith("reject_"):
        payment_id = int(
            data.split("_", 1)[1]
        )

        payment = get_pending_payment(
            payment_id
        )

        if not payment:
            await query.edit_message_caption(
                "❌ Payment not found."
            )
            return

        con = db()

        con.execute(
            """
            UPDATE payments
            SET status='rejected'
            WHERE id=?
            """,
            (payment_id,),
        )

        con.commit()
        con.close()

        await query.edit_message_caption(
            (
                "❌ <b>Payment Rejected</b>\n\n"
                f"Payment ID: <code>{payment_id}</code>"
            ),
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text=(
                    "❌ आपका payment proof reject किया गया है।\n"
                    "कृपया सही payment proof भेजें।"
                ),
            )
        except Exception:
            pass


# ============================================================
# URL DOWNLOAD
# ============================================================

async def process_single_url(
    update,
    context,
    url,
    requested_type=None,
):
    user = update.effective_user

    if not is_http_url(url):
        return False, "Invalid URL"

    if requested_type in {
        "pdf",
        "video",
    } and not is_premium(user.id):
        return (
            False,
            "premium_required"
        )

    async with download_semaphore:

        work_dir = tempfile.mkdtemp(
            dir=DOWNLOAD_DIR
        )

        download_id = None

        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.UPLOAD_DOCUMENT,
            )

            result = await download_url(
                url,
                work_dir
            )

            detected = detect_type(
                result["filename"],
                result["content_type"]
            )

            if requested_type == "pdf":
                if detected != "pdf":
                    raise RuntimeError(
                        "यह URL PDF file नहीं है।"
                    )

            if requested_type == "video":
                if detected != "video":
                    raise RuntimeError(
                        "यह URL video file नहीं है।"
                    )

            file_type = (
                detected
                if detected != "unknown"
                else "file"
            )

            download_id = create_download(
                user.id,
                url,
                file_type,
                result["filename"]
            )

            size_mb = (
                result["size"]
                / 1024
                / 1024
            )

            caption = (
                f"📥 <b>{result['filename']}</b>\n\n"
                f"📦 Size: {size_mb:.2f} MB"
            )

            path = result["path"]

            with open(path, "rb") as file:

                if file_type == "video":
                    await update.effective_chat.send_video(
                        video=InputFile(
                            file,
                            filename=result["filename"]
                        ),
                        caption=caption,
                        parse_mode="HTML",
                        supports_streaming=True,
                    )
                else:
                    await update.effective_chat.send_document(
                        document=InputFile(
                            file,
                            filename=result["filename"]
                        ),
                        caption=caption,
                        parse_mode="HTML",
                    )

            return True, "success"

        except Exception as exc:
            logger.exception(
                "Download error"
            )

            return (
                False,
                str(exc)
            )

        finally:
            shutil.rmtree(
                work_dir,
                ignore_errors=True
            )


# ============================================================
# TEXT URL HANDLER
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    upsert_user(user)

    text = update.message.text or ""

    # Payment UTR
    if context.user_data.get(
        "proof_payment_id"
    ):
        payment_id = context.user_data.pop(
            "proof_payment_id"
        )

        payment = get_pending_payment(
            payment_id
        )

        if payment and payment["user_id"] == user.id:

            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            "🧾 <b>Payment UTR</b>\n\n"
                            f"Payment ID: <code>{payment_id}</code>\n"
                            f"User ID: <code>{user.id}</code>\n"
                            f"UTR: <code>{text[:200]}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            await update.message.reply_text(
                "✅ UTR receive हो गया। Admin verification के बाद premium activate होगा।"
            )

            return

    urls = extract_urls(text)

    if not urls:
        await update.message.reply_text(
            "❌ कोई valid HTTP/HTTPS URL नहीं मिला।"
        )
        return

    requested_type = context.user_data.get(
        "requested_type"
    )

    if requested_type in {
        "pdf",
        "video",
    } and not is_premium(user.id):

        await update.message.reply_text(
            "💎 यह feature Premium है।\n\n"
            "Buy Premium खोलकर plan activate करें।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💎 Buy Premium",
                        callback_data="premium",
                    )
                ]
            ]),
        )

        return

    await update.message.reply_text(
        f"🔎 {len(urls)} link मिले।\n"
        "⏳ Download शुरू हो रहा है..."
    )

    for index, url in enumerate(urls, 1):

        if len(urls) > 1:
            await update.message.reply_text(
                f"📥 Processing {index}/{len(urls)}"
            )

        success, result = await process_single_url(
            update,
            context,
            url,
            requested_type,
        )

        if not success:

            if result == "premium_required":
                await update.message.reply_text(
                    "💎 Premium required."
                )
            else:
                await update.message.reply_text(
                    f"❌ Download failed:\n{result}"
                )


# ============================================================
# TXT FILE
# ============================================================

async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    upsert_user(user)

    document = update.message.document

    filename = (
        document.file_name or ""
    ).lower()

    if not filename.endswith(".txt"):
        await update.message.reply_text(
            "❌ केवल TXT file भेजें।"
        )
        return

    requested_type = context.user_data.get(
        "requested_type"
    )

    if requested_type in {
        "pdf",
        "video",
    } and not is_premium(user.id):

        await update.message.reply_text(
            "💎 TXT batch download के लिए Premium required है।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💎 Buy Premium",
                        callback_data="premium",
                    )
                ]
            ]),
        )

        return

    temp_path = (
        DOWNLOAD_DIR
        / f"{uuid.uuid4().hex}.txt"
    )

    try:
        tg_file = await document.get_file()

        await tg_file.download_to_drive(
            custom_path=str(temp_path)
        )

        async with aiofiles.open(
            temp_path,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as file:
            text = await file.read()

        urls = extract_urls(text)

        if not urls:
            await update.message.reply_text(
                "❌ TXT file में कोई valid URL नहीं मिला।"
            )
            return

        await update.message.reply_text(
            f"📋 {len(urls)} links मिले।\n"
            "⏳ Batch download शुरू हो रहा है..."
        )

        for index, url in enumerate(
            urls,
            1
        ):
            await update.message.reply_text(
                f"📥 {index}/{len(urls)}"
            )

            success, result = await process_single_url(
                update,
                context,
                url,
                requested_type,
            )

            if not success:
                await update.message.reply_text(
                    f"❌ Link {index} failed:\n{result}"
                )

    except Exception as exc:
        logger.exception(
            "TXT error"
        )

        await update.message.reply_text(
            f"❌ TXT processing error:\n{exc}"
        )

    finally:
        try:
            temp_path.unlink()
        except Exception:
            pass


# ============================================================
# ADMIN COMMANDS
# ============================================================

def admin_only(user_id):
    return user_id in ADMIN_IDS


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        await update.message.reply_text(
            "❌ Unauthorized"
        )
        return

    await update.message.reply_text(
        "👨‍💻 <b>Admin Panel</b>\n\n"
        "/stats - Statistics\n"
        "/setwelcome - Welcome text\n"
        "/sethelp - Help text\n"
        "/setpdfprice - PDF premium price\n"
        "/setvideoprice - Video premium price\n"
        "/setdays - Premium days\n"
        "/setupi - UPI ID\n"
        "/setpayment - Payment instructions\n"
        "/setwelcomeimage - Welcome image\n"
        "/users - Users\n",
        parse_mode="HTML",
    )


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    con = db()

    users = con.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]

    premium = con.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE premium=1
        AND premium_until>?
        """,
        (int(time.time()),),
    ).fetchone()["c"]

    downloads = con.execute(
        "SELECT COUNT(*) AS c FROM downloads"
    ).fetchone()["c"]

    pending = con.execute(
        """
        SELECT COUNT(*) AS c
        FROM payments
        WHERE status='pending'
        """
    ).fetchone()["c"]

    con.close()

    await update.message.reply_text(
        (
            "📊 <b>Statistics</b>\n\n"
            f"👥 Users: {users}\n"
            f"💎 Premium: {premium}\n"
            f"📥 Downloads: {downloads}\n"
            f"💳 Pending Payments: {pending}"
        ),
        parse_mode="HTML",
    )


async def setwelcome_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    text = update.message.text[
        len("/setwelcome"):
    ].strip()

    if not text:
        await update.message.reply_text(
            "Usage:\n/setwelcome आपका message"
        )
        return

    set_setting(
        "welcome_text",
        text
    )

    await update.message.reply_text(
        "✅ Welcome message updated."
    )


async def sethelp_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    text = update.message.text[
        len("/sethelp"):
    ].strip()

    if not text:
        await update.message.reply_text(
            "Usage:\n/sethelp आपका help message"
        )
        return

    set_setting(
        "help_text",
        text
    )

    await update.message.reply_text(
        "✅ Help updated."
    )


async def setprice_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    key,
    usage,
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split()

    if len(parts) != 2:
        await update.message.reply_text(
            usage
        )
        return

    try:
        price = int(parts[1])

        if price < 0:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid price."
        )
        return

    set_setting(
        key,
        price
    )

    await update.message.reply_text(
        f"✅ Price updated: ₹{price}"
    )


async def setpdfprice_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await setprice_command(
        update,
        context,
        "pdf_price",
        "/setpdfprice 49",
    )


async def setvideoprice_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await setprice_command(
        update,
        context,
        "video_price",
        "/setvideoprice 99",
    )


async def setdays_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split()

    if len(parts) != 2:
        await update.message.reply_text(
            "/setdays 30"
        )
        return

    try:
        days = int(parts[1])

        if days <= 0:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid days."
        )
        return

    set_setting(
        "premium_days",
        days
    )

    await update.message.reply_text(
        f"✅ Premium validity: {days} days"
    )


async def setupi_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    upi = update.message.text[
        len("/setupi"):
    ].strip()

    if not upi:
        await update.message.reply_text(
            "/setupi yourupi@upi"
        )
        return

    set_setting(
        "upi_id",
        upi
    )

    await update.message.reply_text(
        "✅ UPI ID updated."
    )


async def setpayment_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    text = update.message.text[
        len("/setpayment"):
    ].strip()

    if not text:
        await update.message.reply_text(
            "/setpayment Payment instructions..."
        )
        return

    set_setting(
        "payment_text",
        text
    )

    await update.message.reply_text(
        "✅ Payment instructions updated."
    )


async def setwelcomeimage_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not admin_only(
        update.effective_user.id
    ):
        return

    context.user_data[
        "waiting_welcome_image"
    ] = True

    await update.message.reply_text(
        "🖼 अब welcome image भेजें।"
    )


# ============================================================
# ADMIN PHOTO
# ============================================================

async def handle_admin_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if not context.user_data.get(
        "waiting_welcome_image"
    ):
        return

    photo = update.message.photo[-1]

    tg_file = await photo.get_file()

    target = (
        WELCOME_DIR
        / "welcome.jpg"
    )

    await tg_file.download_to_drive(
        custom_path=str(target)
    )

    context.user_data.pop(
        "waiting_welcome_image",
        None
    )

    await update.message.reply_text(
        "✅ Welcome image updated."
    )


# ============================================================
# ADMIN WEB PANEL
# ============================================================

app = Flask(__name__)

app.secret_key = SECRET_KEY


ADMIN_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>Downloader Admin</title>
<style>
*{
box-sizing:border-box;
}
body{
margin:0;
font-family:Arial,sans-serif;
background:#080b12;
color:#fff;
}
.wrap{
max-width:1000px;
margin:auto;
padding:20px;
}
.card{
background:#111722;
border:1px solid #273044;
border-radius:16px;
padding:18px;
margin-bottom:15px;
}
h1,h2{
margin-top:0;
}
input,textarea,button{
width:100%;
padding:12px;
margin-top:8px;
border-radius:10px;
border:1px solid #303b4d;
background:#080d14;
color:#fff;
}
textarea{
min-height:130px;
resize:vertical;
}
button{
background:#4777ff;
border:0;
font-weight:bold;
cursor:pointer;
}
.grid{
display:grid;
grid-template-columns:
repeat(auto-fit,minmax(220px,1fr));
gap:12px;
}
.stat{
font-size:28px;
font-weight:bold;
}
label{
display:block;
margin-top:12px;
font-size:13px;
color:#9da8ba;
}
a{
color:#7ea0ff;
}
</style>
</head>
<body>

<div class="wrap">

<div class="card">
<h1>👨‍💻 Downloader Admin</h1>
<p>Premium PDF + Video Downloader</p>
</div>

<div class="grid">

<div class="card">
<div>Users</div>
<div class="stat">{{stats.users}}</div>
</div>

<div class="card">
<div>Premium Users</div>
<div class="stat">{{stats.premium}}</div>
</div>

<div class="card">
<div>Downloads</div>
<div class="stat">{{stats.downloads}}</div>
</div>

<div class="card">
<div>Pending Payments</div>
<div class="stat">{{stats.pending}}</div>
</div>

</div>

<form method="post"
      action="/admin/save">

<div class="card">

<h2>🏠 Welcome</h2>

<label>Welcome Message</label>
<textarea name="welcome_text">{{settings.welcome_text}}</textarea>

</div>

<div class="card">

<h2>🆘 Help</h2>

<label>Help Message</label>
<textarea name="help_text">{{settings.help_text}}</textarea>

</div>

<div class="card">

<h2>💎 Premium</h2>

<label>PDF Price</label>
<input type="number"
       name="pdf_price"
       value="{{settings.pdf_price}}">

<label>Video Price</label>
<input type="number"
       name="video_price"
       value="{{settings.video_price}}">

<label>Premium Days</label>
<input type="number"
       name="premium_days"
       value="{{settings.premium_days}}">

<label>UPI ID</label>
<input name="upi_id"
       value="{{settings.upi_id}}">

<label>Payment Instructions</label>
<textarea name="payment_text">{{settings.payment_text}}</textarea>

<button type="submit">
💾 Save Settings
</button>

</div>

</form>

<div class="card">

<h2>🖼 Welcome Image</h2>

<form method="post"
      action="/admin/upload"
      enctype="multipart/form-data">

<input type="file"
       name="image"
       accept="image/*"
       required>

<button type="submit">
Upload / Change Image
</button>

</form>

</div>

</div>

</body>
</html>
"""


def web_admin():
    if not session.get("admin"):
        return redirect("/admin/login")

    con = db()

    stats = {
        "users": con.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"],

        "premium": con.execute(
            """
            SELECT COUNT(*) AS c
            FROM users
            WHERE premium=1
            AND premium_until>?
            """,
            (int(time.time()),),
        ).fetchone()["c"],

        "downloads": con.execute(
            "SELECT COUNT(*) AS c FROM downloads"
        ).fetchone()["c"],

        "pending": con.execute(
            """
            SELECT COUNT(*) AS c
            FROM payments
            WHERE status='pending'
            """
        ).fetchone()["c"],
    }

    con.close()

    settings = {
        key: get_setting(key)
        for key in [
            "welcome_text",
            "help_text",
            "pdf_price",
            "video_price",
            "premium_days",
            "upi_id",
            "payment_text",
        ]
    }

    return render_template_string(
        ADMIN_HTML,
        stats=stats,
        settings=settings,
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":

        password = request.form.get(
            "password",
            ""
        )

        # Admin panel password:
        # use SECRET_KEY as initial password
        if password == SECRET_KEY:

            session["admin"] = True

            return redirect(
                "/admin"
            )

        return """
        <h3>Invalid password</h3>
        <a href="/admin/login">
        Try again
        </a>
        """

    return """
    <!doctype html>
    <html>
    <body style="
    background:#080b12;
    color:white;
    font-family:Arial;
    padding:30px;
    ">
    <h2>Admin Login</h2>

    <form method="post">

    <input
    type="password"
    name="password"
    placeholder="Admin password"
    style="
    padding:12px;
    width:300px;
    background:#111722;
    color:white;
    border:1px solid #303b4d;
    border-radius:10px;
    ">

    <button
    style="
    padding:12px;
    margin-top:10px;
    ">
    Login
    </button>

    </form>

    </body>
    </html>
    """


@app.route("/admin")
def admin_page():
    return web_admin()


@app.route("/admin/save", methods=["POST"])
def admin_save():

    if not session.get("admin"):
        return redirect(
            "/admin/login"
        )

    fields = [
        "welcome_text",
        "help_text",
        "pdf_price",
        "video_price",
        "premium_days",
        "upi_id",
        "payment_text",
    ]

    for field in fields:

        value = request.form.get(
            field,
            ""
        ).strip()

        set_setting(
            field,
            value
        )

    return redirect(
        "/admin"
    )


@app.route(
    "/admin/upload",
    methods=["POST"]
)
def admin_upload():

    if not session.get("admin"):
        return redirect(
            "/admin/login"
        )

    image = request.files.get(
        "image"
    )

    if not image:
        return redirect(
            "/admin"
        )

    for old in WELCOME_DIR.glob("*"):
        try:
            old.unlink()
        except Exception:
            pass

    ext = Path(
        image.filename or "welcome.jpg"
    ).suffix.lower()

    if ext not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }:
        ext = ".jpg"

    image.save(
        WELCOME_DIR
        / f"welcome{ext}"
    )

    return redirect(
        "/admin"
    )


@app.route("/admin/logout")
def admin_logout():

    session.clear()

    return redirect(
        "/admin/login"
    )


# ============================================================
# FLASK THREAD
# ============================================================

def run_web():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):
    logger.exception(
        "Unhandled bot error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

async def post_init(
    application
):
    await application.bot.set_my_commands([
        ("start", "Open downloader"),
        ("admin", "Admin panel"),
    ])


def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    init_db()

    import threading

    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setwelcome",
            setwelcome_command
        )
    )

    application.add_handler(
        CommandHandler(
            "sethelp",
            sethelp_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setpdfprice",
            setpdfprice_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setvideoprice",
            setvideoprice_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setdays",
            setdays_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setupi",
            setupi_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setpayment",
            setpayment_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setwelcomeimage",
            setwelcomeimage_command
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_payment_callback,
            pattern=r"^(approve|reject)_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_admin_photo
        ),
        group=10,
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo
        ),
        group=20,
    )

    application.add_handler(
        MessageHandler(
            filters.Document.FileExtension(
                "txt"
            ),
            handle_document
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
