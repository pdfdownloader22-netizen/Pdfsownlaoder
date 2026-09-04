# -*- coding: utf-8 -*-
# main.py
# MongoDB-based Telegram PDF/Video Downloader Bot
# Features:
# - MongoDB persistent users/payments/settings
# - Admin panel
# - Welcome message/image
# - PDF/Video download
# - TXT batch links
# - Download logs channel
# - Payment approval/rejection
# - Premium access
# - Force join
# - Add output channels
# - Channel setup instructions
# - Admin-controlled buttons/settings
#
# IMPORTANT:
# This bot downloads only resources that are publicly accessible
# and does not bypass DRM, authentication, CAPTCHA, or private
# access controls.

import os
import re
import time
import uuid
import shutil
import asyncio
import logging
import mimetypes
import json
import tempfile
from html import escape
from pathlib import Path
from urllib.parse import urlparse, unquote, quote

import aiohttp
import aiofiles
from dotenv import load_dotenv

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from flask import (
    Flask,
    request,
    redirect,
    render_template_string,
    session,
)

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

# ============================================================
# ENV
# ============================================================

load_dotenv()


def fix_mojibake(value):
    """Repair common UTF-8/CP1251 mojibake without changing valid Hindi."""
    if not isinstance(value, str) or not value:
        return value

    def has_devanagari(text):
        return any("\u0900" <= ch <= "\u097f" for ch in text)

    # Already-correct Hindi/Unicode: leave it untouched.
    if has_devanagari(value):
        return value

    # UTF-8 bytes decoded as CP1251 are commonly displayed as рџ... / а¤...
    # Try the reversible conversion and only accept it when it produces
    # Devanagari or common emoji/symbol ranges.
    candidates = []
    for encoding in ("cp1251", "cp1252", "latin1"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
            candidates.append(repaired)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue

    for repaired in candidates:
        if has_devanagari(repaired):
            return repaired
        if any("\U0001f300" <= ch <= "\U0001faff" for ch in repaired):
            return repaired

    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MONGODB_URI = os.getenv(
    "MONGODB_URI",
    ""
).strip()

MONGODB_DB = os.getenv(
    "MONGODB_DB",
    "telegram_downloader"
).strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv(
        "ADMIN_IDS",
        ""
    ).split(",")
    if x.strip().isdigit()
}

LOG_CHANNEL_ID = os.getenv(
    "LOG_CHANNEL_ID",
    ""
).strip()

PORT = int(
    os.getenv(
        "PORT",
        "8080"
    )
)

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET"
)

MAX_FILE_SIZE_MB = int(
    os.getenv(
        "MAX_FILE_SIZE_MB",
        "2000"
    )
)

MAX_FILE_SIZE = (
    MAX_FILE_SIZE_MB * 1024 * 1024
)

DOWNLOAD_TIMEOUT = int(
    os.getenv(
        "DOWNLOAD_TIMEOUT",
        "1800"
    )
)

MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv(
        "MAX_CONCURRENT_DOWNLOADS",
        "3"
    )
)

DEFAULT_PDF_PRICE = int(
    os.getenv(
        "PREMIUM_PDF_PRICE",
        "49"
    )
)

DEFAULT_VIDEO_PRICE = int(
    os.getenv(
        "PREMIUM_VIDEO_PRICE",
        "99"
    )
)

DEFAULT_PREMIUM_DAYS = int(
    os.getenv(
        "PREMIUM_DAYS",
        "30"
    )
)

DEFAULT_UPI = os.getenv(
    "UPI_ID",
    ""
).strip()

PDF_PROXY_URL = os.getenv(
    "PDF_PROXY_URL",
    "https://proplayer.probrosystemset.workers.dev/segment?url="
).strip()

BASE_DIR = Path(
    __file__
).resolve().parent

DATA_DIR = BASE_DIR / "data"

DOWNLOAD_DIR = (
    DATA_DIR / "downloads"
)

WELCOME_DIR = (
    DATA_DIR / "welcome"
)

DATA_DIR.mkdir(
    exist_ok=True
)

DOWNLOAD_DIR.mkdir(
    exist_ok=True
)

WELCOME_DIR.mkdir(
    exist_ok=True
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "premium-downloader"
)

# ============================================================
# MONGODB
# ============================================================

if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is missing in .env"
    )

mongo = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000,
)

mongo.admin.command(
    "ping"
)

db = mongo[
    MONGODB_DB
]

users_col = db["users"]
payments_col = db["payments"]
downloads_col = db["downloads"]
settings_col = db["settings"]
channels_col = db["channels"]

users_col.create_index(
    "user_id",
    unique=True
)

payments_col.create_index(
    "payment_id",
    unique=True
)

downloads_col.create_index(
    "created_at"
)

channels_col.create_index(
    "chat_id",
    unique=True
)

# ============================================================
# DEFAULT SETTINGS
# ============================================================

DEFAULT_SETTINGS = {
    "welcome_text": (
        "🎓 <b>Welcome</b>\n\n"
        "📥 PDF और Video download करने के लिए "
        "नीचे दिए options का उपयोग करें।"
    ),

    "help_text": (
        "🆘 <b>Help</b>\n\n"
        "1. PDF Download दबाएँ और PDF URL भेजें।\n"
        "2. Video Download दबाएँ और Video URL भेजें।\n"
        "3. Multiple links के लिए TXT file भेजें।\n"
        "4. Premium feature के लिए Buy Premium खोलें।\n\n"
        "Channel में file भेजने के लिए पहले "
        "अपना channel bot में add करें।"
    ),

    "payment_text": (
        "💳 Payment करने के बाद screenshot "
        "और UTR/Transaction ID भेजें।\n\n"
        "Admin verification के बाद access मिलेगा।"
    ),

    "upi_id": DEFAULT_UPI,

    "pdf_price": DEFAULT_PDF_PRICE,

    "video_price": DEFAULT_VIDEO_PRICE,

    "premium_days": DEFAULT_PREMIUM_DAYS,

    "force_join_enabled": False,

    "force_join_channels": [],

    "download_pdf_enabled": True,

    "download_video_enabled": True,

    "txt_enabled": True,

    "channel_upload_enabled": True,

    "maintenance": False,
}


def init_settings():

    for key, value in DEFAULT_SETTINGS.items():

        settings_col.update_one(
            {
                "key": key
            },
            {
                "$setOnInsert": {
                    "key": key,
                    "value": value,
                }
            },
            upsert=True,
        )


init_settings()


def get_setting(
    key,
    default=None
):

    row = settings_col.find_one(
        {
            "key": key
        }
    )

    if row is None:
        return default

    value = row.get(
        "value",
        default
    )
    return fix_mojibake(value) if isinstance(value, str) else value


def set_setting(
    key,
    value
):

    settings_col.update_one(
        {
            "key": key
        },
        {
            "$set": {
                "key": key,
                "value": value,
            }
        },
        upsert=True,
    )


# ============================================================
# USER DATABASE
# ============================================================

def save_user(
    user
):

    now = int(
        time.time()
    )

    users_col.update_one(
        {
            "user_id": user.id
        },
        {
            "$set": {
                "username": (
                    user.username or ""
                ),
                "first_name": (
                    user.first_name or ""
                ),
                "last_name": (
                    user.last_name or ""
                ),
                "updated_at": now,
            },
            "$setOnInsert": {
                "user_id": user.id,
                "premium": False,
                "premium_until": 0,
                "created_at": now,
            },
        },
        upsert=True,
    )


def get_user(
    user_id
):

    return users_col.find_one(
        {
            "user_id": user_id
        }
    )


def premium_active(
    user_id
):

    user = get_user(
        user_id
    )

    if not user:
        return False

    return bool(
        user.get(
            "premium",
            False
        )
        and
        user.get(
            "premium_until",
            0
        ) > int(
            time.time()
        )
    )


def activate_premium(
    user_id,
    days
):

    now = int(
        time.time()
    )

    existing = get_user(
        user_id
    )

    current_until = (
        existing.get(
            "premium_until",
            0
        )
        if existing
        else 0
    )

    start_from = max(
        current_until,
        now
    )

    until = (
        start_from
        + days * 86400
    )

    users_col.update_one(
        {
            "user_id": user_id
        },
        {
            "$set": {
                "premium": True,
                "premium_until": until,
                "updated_at": now,
            }
        },
        upsert=True,
    )

    return until


def deactivate_premium(
    user_id
):

    users_col.update_one(
        {
            "user_id": user_id
        },
        {
            "$set": {
                "premium": False,
                "premium_until": 0,
                "updated_at": int(
                    time.time()
                ),
            }
        },
    )


# ============================================================
# PAYMENT DATABASE
# ============================================================

def create_payment(
    user_id,
    plan,
    amount
):

    payment_id = uuid.uuid4().hex[:12]

    payments_col.insert_one(
        {
            "payment_id": payment_id,
            "user_id": user_id,
            "plan": plan,
            "amount": amount,
            "status": "pending",
            "created_at": int(
                time.time()
            ),
            "approved_at": 0,
        }
    )

    return payment_id


def get_payment(
    payment_id
):

    return payments_col.find_one(
        {
            "payment_id": payment_id
        }
    )


# ============================================================
# DOWNLOAD LOG DATABASE
# ============================================================

def save_download_log(
    user,
    url,
    file_type,
    filename,
    size,
    status
):

    downloads_col.insert_one(
        {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "url": url,
            "file_type": file_type,
            "filename": filename,
            "size": size,
            "status": status,
            "created_at": int(
                time.time()
            ),
        }
    )


# ============================================================
# URL
# ============================================================

def valid_url(
    url
):

    try:

        parsed = urlparse(
            url
        )

        return (
            parsed.scheme.lower()
            in {
                "http",
                "https",
            }
            and
            bool(
                parsed.netloc
            )
        )

    except Exception:

        return False


def extract_urls(
    text
):

    pattern = (
        r'https?://[^\s<>"\']+'
    )

    urls = re.findall(
        pattern,
        text or "",
        re.I
    )

    cleaned = []

    for url in urls:

        url = url.rstrip(
            ".,;)]}>"
        )

        if valid_url(
            url
        ):
            cleaned.append(
                url
            )

    return list(
        dict.fromkeys(
            cleaned
        )
    )


# ============================================================
# GOOGLE DRIVE
# ============================================================

def google_drive_id(
    url
):

    patterns = [
        r"/file/d/([A-Za-z0-9_-]+)",
        r"[?&]id=([A-Za-z0-9_-]+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            url
        )

        if match:
            return match.group(1)

    return None


def normalize_url(
    url
):

    parsed = urlparse(
        url
    )

    host = parsed.netloc.lower()

    if (
        "drive.google.com"
        not in host
        and
        "docs.google.com"
        not in host
    ):
        return url

    file_id = google_drive_id(
        url
    )

    if not file_id:
        return url

    return (
        "https://drive.usercontent.google.com/"
        f"download?id={file_id}&export=download"
    )


# ============================================================
# HTTP HEADERS
# ============================================================

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Linux; Android 14) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0 "
        "Mobile Safari/537.36"
    ),

    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "application/pdf,"
        "video/*,"
        "*/*;q=0.8"
    ),

    "Accept-Language":
        "en-US,en;q=0.9,hi;q=0.8",

    "Cache-Control":
        "no-cache",

    "Pragma":
        "no-cache",
}


def build_headers(
    url
):

    headers = dict(
        COMMON_HEADERS
    )

    parsed = urlparse(
        url
    )

    if parsed.scheme in {
        "http",
        "https",
    }:

        headers["Referer"] = (
            f"{parsed.scheme}://"
            f"{parsed.netloc}/"
        )

    return headers


# ============================================================
# FILENAME
# ============================================================

def sanitize_filename(
    name
):

    name = unquote(
        name or ""
    ).strip()

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


def filename_from_response(
    response
):

    disposition = response.headers.get(
        "Content-Disposition",
        ""
    )

    match = re.search(
        r'filename\*?='
        r'(?:UTF-8\'\')?'
        r'"?([^";]+)"?',
        disposition,
        re.I
    )

    if match:

        return sanitize_filename(
            match.group(1)
        )

    name = Path(
        urlparse(
            str(
                response.url
            )
        ).path
    ).name

    return sanitize_filename(
        name
    )


# ============================================================
# DOWNLOAD
# ============================================================

async def download_file(
    url,
    output_dir,
    requested_type=None,
    progress_callback=None
):
    """Robust PDF/video downloader.

    PDF resolution intentionally mirrors pdf-vvv.html:
      1. Send the supplied URL through SEGMENT.
      2. If the response is JSON, extract pdf_url/url.
      3. Send the extracted URL through SEGMENT again.
      4. If SEGMENT returns JSON again, fetch that final URL directly.
      5. Repeat nested URL resolution when required.
      6. Never save JSON/HTML as a PDF; accept only a %PDF- response.
    """

    original_url = normalize_url(url)
    current_url = original_url

    is_pdf_request = (
        requested_type == "pdf"
        or urlparse(original_url).path.lower().endswith(".pdf")
        or "pdf" in original_url.lower()
        or "appx-pdf" in original_url.lower()
    )

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

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        raise_for_status=False,
    ) as session:

        # ------------------------------------------------------------
        # PDF: resolve + final download in the SAME request loop.
        # This avoids the race where the probe request returns a PDF but
        # the second request unexpectedly returns the API JSON again.
        # ------------------------------------------------------------
        if is_pdf_request:
            seen = set()
            force_direct = set()
            final_result = None

            for attempt in range(10):
                target = current_url

                use_proxy = bool(PDF_PROXY_URL) and target not in force_direct

                if use_proxy:
                    request_url = (
                        PDF_PROXY_URL
                        + quote(target, safe="")
                    )
                else:
                    request_url = target

                headers = build_headers(target)
                headers["Accept"] = (
                    "application/pdf,application/octet-stream,"
                    "application/json,text/plain,*/*;q=0.8"
                )

                request_key = (
                    "proxy" if use_proxy else "direct",
                    target,
                )

                if request_key in seen:
                    # If proxy has already been tried, force direct.
                    if use_proxy:
                        force_direct.add(target)
                        continue
                    raise RuntimeError(
                        "PDF URL resolution loop detected; final PDF URL "
                        "could not be fetched."
                    )

                seen.add(request_key)

                async with session.get(
                    request_url,
                    headers=headers,
                    allow_redirects=True,
                ) as response:
                    if response.status >= 400:
                        # A signed URL can occasionally fail through the proxy
                        # while working directly. Retry direct once.
                        if use_proxy:
                            force_direct.add(target)
                            continue
                        raise RuntimeError(
                            f"PDF download HTTP {response.status}"
                        )

                    content_type = response.headers.get(
                        "Content-Type", ""
                    ).lower()

                    try:
                        expected_total = int(
                            response.headers.get(
                                "Content-Length", "0"
                            ) or 0
                        )
                    except (TypeError, ValueError):
                        expected_total = 0

                    if expected_total > MAX_FILE_SIZE:
                        raise RuntimeError(
                            f"File size exceeds {MAX_FILE_SIZE_MB} MB"
                        )

                    first_chunk = await response.content.read(
                        2 * 1024 * 1024
                    )

                    if not first_chunk:
                        if use_proxy:
                            force_direct.add(target)
                            continue
                        raise RuntimeError(
                            "PDF response is empty"
                        )

                    # ----------------------------------------------------
                    # REAL PDF: stream this exact response. No second
                    # request is made, preventing intermittent JSON swaps.
                    # ----------------------------------------------------
                    if first_chunk.startswith(b"%PDF-"):
                        filename = filename_from_response(response)

                        original_name = sanitize_filename(
                            Path(
                                urlparse(original_url).path
                            ).name
                        )

                        if (
                            original_name
                            and original_name != "download"
                            and "." in original_name
                        ):
                            filename = original_name
                        elif (
                            filename == "download"
                            or not filename.lower().endswith(".pdf")
                        ):
                            filename = "document.pdf"

                        if not filename.lower().endswith(".pdf"):
                            filename += ".pdf"

                        filename = sanitize_filename(filename)

                        path = (
                            Path(output_dir)
                            /
                            f"{uuid.uuid4().hex}_{filename}"
                        )

                        total = 0

                        async def report_progress(force=False):
                            if progress_callback:
                                await progress_callback(
                                    total,
                                    expected_total,
                                    force
                                )

                        try:
                            async with aiofiles.open(
                                path,
                                "wb"
                            ) as file:
                                total += len(first_chunk)

                                if total > MAX_FILE_SIZE:
                                    raise RuntimeError(
                                        f"File size exceeds {MAX_FILE_SIZE_MB} MB"
                                    )

                                await file.write(first_chunk)
                                await report_progress()

                                async for chunk in response.content.iter_chunked(
                                    1024 * 1024
                                ):
                                    total += len(chunk)

                                    if total > MAX_FILE_SIZE:
                                        raise RuntimeError(
                                            f"File size exceeds {MAX_FILE_SIZE_MB} MB"
                                        )

                                    await file.write(chunk)
                                    await report_progress()

                            await report_progress(force=True)

                            if total <= 0:
                                raise RuntimeError(
                                    "Downloaded PDF is empty"
                                )

                            # Final on-disk validation.
                            with open(path, "rb") as check_file:
                                if check_file.read(5) != b"%PDF-":
                                    raise RuntimeError(
                                        "Downloaded file failed PDF validation"
                                    )

                            final_result = {
                                "path": str(path),
                                "filename": filename,
                                "size": total,
                                "content_type": "application/pdf",
                                "final_url": str(response.url),
                            }
                            break

                        except Exception:
                            path.unlink(missing_ok=True)
                            raise

                    # ----------------------------------------------------
                    # NOT A PDF: check whether the response is an API JSON
                    # containing another pdf_url. This is the key AppX case.
                    # ----------------------------------------------------
                    payload = None
                    stripped = first_chunk.lstrip()

                    if (
                        "json" in content_type
                        or "text/plain" in content_type
                        or stripped.startswith(b"{")
                        or stripped.startswith(b"[")
                    ):
                        try:
                            payload = json.loads(
                                first_chunk.decode(
                                    "utf-8",
                                    errors="replace"
                                )
                            )
                        except Exception:
                            payload = None

                    extracted = None
                    if isinstance(payload, dict):
                        extracted = (
                            payload.get("pdf_url")
                            or payload.get("url")
                        )

                    if extracted and valid_url(str(extracted)):
                        extracted = str(extracted).strip()

                        if extracted == target:
                            # Same URL returned by proxy -> stop proxying it
                            # and fetch the signed URL directly.
                            force_direct.add(target)
                        else:
                            current_url = extracted

                        continue

                    preview = (
                        first_chunk[:300]
                        .decode("utf-8", errors="replace")
                        .replace("\n", " ")
                        .replace("\r", " ")
                    )

                    if use_proxy:
                        # Proxy may have returned an HTML/error wrapper.
                        # One direct attempt can still succeed for public URLs.
                        force_direct.add(target)
                        continue

                    raise RuntimeError(
                        "PDF server returned neither a valid PDF nor a "
                        f"pdf_url. Response: {preview}"
                    )

            if final_result is None:
                raise RuntimeError(
                    "PDF resolution failed after 10 attempts. "
                    "The source did not return an accessible PDF."
                )

            return final_result

        # ------------------------------------------------------------
        # VIDEO / OTHER FILES: normal streamed download with headers.
        # ------------------------------------------------------------
        request_url = current_url
        headers = build_headers(current_url)

        async with session.get(
            request_url,
            headers=headers,
            allow_redirects=True,
        ) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"HTTP {response.status} while downloading file"
                )

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            try:
                expected_total = int(
                    response.headers.get("Content-Length", "0") or 0
                )
            except (TypeError, ValueError):
                expected_total = 0

            if expected_total > MAX_FILE_SIZE:
                raise RuntimeError(
                    f"File size exceeds {MAX_FILE_SIZE_MB} MB"
                )

            filename = filename_from_response(response)

            if filename == "download":
                if "video/" in content_type:
                    filename = "video.mp4"
                elif "pdf" in content_type:
                    filename = "document.pdf"
                else:
                    filename = "download"

            if "." not in filename:
                extension = (
                    mimetypes.guess_extension(
                        content_type.split(";", 1)[0]
                    )
                    or ""
                )
                filename += extension

            filename = sanitize_filename(filename)
            path = (
                Path(output_dir)
                /
                f"{uuid.uuid4().hex}_{filename}"
            )

            total = 0

            async def report_progress(force=False):
                if progress_callback:
                    await progress_callback(
                        total,
                        expected_total,
                        force
                    )

            try:
                async with aiofiles.open(path, "wb") as file:
                    async for chunk in response.content.iter_chunked(
                        1024 * 1024
                    ):
                        total += len(chunk)

                        if total > MAX_FILE_SIZE:
                            raise RuntimeError(
                                f"File size exceeds {MAX_FILE_SIZE_MB} MB"
                            )

                        await file.write(chunk)
                        await report_progress()

                await report_progress(force=True)

                if total <= 0:
                    raise RuntimeError("Downloaded file is empty")

            except Exception:
                path.unlink(missing_ok=True)
                raise

            return {
                "path": str(path),
                "filename": filename,
                "size": total,
                "content_type": content_type,
                "final_url": str(response.url),
            }


# ============================================================
# TYPE
# ============================================================

def detect_type(
    filename,
    content_type
):

    value = (
        f"{filename} "
        f"{content_type}"
    ).lower()

    if (
        ".pdf" in value
        or
        "application/pdf"
        in value
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
    )

    if any(
        x in value
        for x in video_extensions
    ):

        return "video"

    if (
        "video/"
        in content_type
    ):

        return "video"

    return "unknown"


# ============================================================
# FORCE JOIN
# ============================================================

async def force_join_ok(
    user_id,
    context
):

    if not get_setting(
        "force_join_enabled",
        False
    ):

        return True

    channels = get_setting(
        "force_join_channels",
        []
    )

    if not channels:
        return True

    for channel in channels:

        try:

            member = (
                await context.bot.get_chat_member(
                    chat_id=channel["chat_id"],
                    user_id=user_id,
                )
            )

            if member.status in {
                "left",
                "kicked",
            }:

                return False

        except Exception as exc:

            logger.warning(
                "Force join check failed: %s",
                exc
            )

            return False

    return True


async def require_force_join(
    update,
    context
):

    if await force_join_ok(
        update.effective_user.id,
        context
    ):

        return True

    channels = get_setting(
        "force_join_channels",
        []
    )

    buttons = []

    for channel in channels:

        invite = channel.get(
            "invite",
            ""
        )

        if invite:

            buttons.append([
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=invite
                )
            ])

    buttons.append([
        InlineKeyboardButton(
            "🔄 Check Join",
            callback_data="check_join"
        )
    ])

    await update.effective_message.reply_text(
        "🔒 <b>Access Required</b>\n\n"
        "Bot use करने से पहले required "
        "channel join करें।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            buttons
        )
    )

    return False


# ============================================================
# MAIN KEYBOARD
# ============================================================

def main_keyboard():

    rows = []

    if get_setting(
        "download_pdf_enabled",
        True
    ):

        rows.append([
            InlineKeyboardButton(
                "📄 PDF Download",
                callback_data="download_pdf"
            )
        ])

    if get_setting(
        "download_video_enabled",
        True
    ):

        rows.append([
            InlineKeyboardButton(
                "🎥 Video Download",
                callback_data="download_video"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "💎 Buy Premium",
            callback_data="premium"
        )
    ])

    if get_setting(
        "channel_upload_enabled",
        True
    ):

        rows.append([
            InlineKeyboardButton(
                "📢 Add Channel",
                callback_data="add_channel"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🆘 Help",
            callback_data="help"
        )
    ])

    return InlineKeyboardMarkup(
        rows
    )


# ============================================================
# WELCOME
# ============================================================

async def send_welcome(
    update,
    context
):

    user = update.effective_user

    if user:
        save_user(
            user
        )

    if not await require_force_join(
        update,
        context
    ):

        return

    if get_setting(
        "maintenance",
        False
    ) and user.id not in ADMIN_IDS:

        await update.effective_message.reply_text(
            "🛠 Bot अभी maintenance mode में है।"
        )

        return

    text = get_setting(
        "welcome_text",
        "Welcome"
    )

    images = list(
        WELCOME_DIR.glob("*")
    )

    if images:

        try:

            with open(
                images[0],
                "rb"
            ) as photo:

                await update.effective_message.reply_photo(
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=main_keyboard()
                )

                return

        except Exception as exc:

            logger.warning(
                "Welcome image error: %s",
                exc
            )

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# START
# ============================================================

async def start(
    update,
    context
):

    await send_welcome(
        update,
        context
    )


# ============================================================
# CALLBACK
# ============================================================

async def callbacks(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    save_user(
        user
    )

    data = query.data

    if data == "home":

        await query.message.delete()

        await send_welcome(
            update,
            context
        )

        return

    if data == "check_join":

        if await force_join_ok(
            user.id,
            context
        ):

            await query.message.edit_text(
                "✅ Channel membership verified."
            )

            await context.bot.send_message(
                user.id,
                get_setting(
                    "welcome_text",
                    "Welcome"
                ),
                parse_mode="HTML",
                reply_markup=main_keyboard()
            )

        else:

            await query.answer(
                "पहले required channel join करें।",
                show_alert=True
            )

        return

    if data == "download_pdf":

        context.user_data[
            "requested_type"
        ] = "pdf"

        await query.message.edit_text(
            "📄 <b>PDF Download</b>\n\n"
            "अब PDF का URL भेजें।\n\n"
            "Multiple URLs के लिए TXT file भी भेज सकते हैं।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home"
                    )
                ]
            ])
        )

        return

    if data == "download_video":

        context.user_data[
            "requested_type"
        ] = "video"

        await query.message.edit_text(
            "🎥 <b>Video Download</b>\n\n"
            "अब video का direct/public URL भेजें।\n\n"
            "Multiple URLs के लिए TXT file भी भेज सकते हैं।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home"
                    )
                ]
            ])
        )

        return

    if data == "premium":

        pdf_price = get_setting(
            "pdf_price",
            DEFAULT_PDF_PRICE
        )

        video_price = get_setting(
            "video_price",
            DEFAULT_VIDEO_PRICE
        )

        await query.message.edit_text(
            "💎 <b>Premium Plans</b>\n\n"
            "Premium download के लिए plan select करें।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"📄 PDF ₹{pdf_price}",
                        callback_data="buy_pdf"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🎥 Video ₹{video_price}",
                        callback_data="buy_video"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home"
                    )
                ]
            ])
        )

        return

    if data in {
        "buy_pdf",
        "buy_video"
    }:

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
                DEFAULT_PDF_PRICE
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
                f"💎 <b>Premium "
                f"{plan.upper()}</b>\n\n"
                f"💰 Price: ₹{amount}\n"
                f"🆔 Payment ID: "
                f"<code>{payment_id}</code>\n\n"
                f"💳 UPI: "
                f"<code>{upi}</code>\n\n"
                f"{payment_text}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📤 Payment Proof",
                        callback_data=(
                            f"proof_{payment_id}"
                        )
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="premium"
                    )
                ]
            ])
        )

        return

    if data.startswith(
        "proof_"
    ):

        payment_id = data.split(
            "_",
            1
        )[1]

        payment = get_payment(
            payment_id
        )

        if (
            not payment
            or
            payment["user_id"]
            != user.id
        ):

            await query.answer(
                "Invalid payment.",
                show_alert=True
            )

            return

        context.user_data[
            "proof_payment_id"
        ] = payment_id

        await query.message.reply_text(
            "📤 अब payment screenshot भेजें।\n"
            "फिर UTR/Transaction ID text में भेजें।"
        )

        return

    if data == "add_channel":

        await query.message.edit_text(
            "📢 <b>Channel में PDF/Video भेजना</b>\n\n"
            "Step 1️⃣: अपना Telegram channel बनाएं/खोलें।\n\n"
            "Step 2️⃣: Bot को channel में Administrator बनाएं।\n\n"
            "Step 3️⃣: Bot को कम-से-कम ये permissions दें:\n"
            "• Post Messages\n"
            "• Edit Messages\n"
            "• Delete Messages (optional)\n\n"
            "Step 4️⃣: Channel में कोई message भेजें।\n\n"
            "Step 5️⃣: Bot में वापस आकर channel ID/username भेजें।\n\n"
            "⚠️ Bot तभी channel में file भेज पाएगा "
            "जब उसे channel में पर्याप्त permission मिले।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home"
                    )
                ]
            ])
        )

        context.user_data[
            "waiting_channel"
        ] = True

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
                        callback_data="home"
                    )
                ]
            ])
        )

        return


# ============================================================
# PAYMENT PHOTO
# ============================================================

async def payment_photo(
    update,
    context
):

    user = update.effective_user

    payment_id = context.user_data.get(
        "proof_payment_id"
    )

    if not payment_id:
        return

    payment = get_payment(
        payment_id
    )

    if (
        not payment
        or
        payment["user_id"]
        != user.id
    ):

        await update.message.reply_text(
            "❌ Invalid payment."
        )

        return

    context.user_data[
        "payment_proof_file_id"
    ] = update.message.photo[-1].file_id

    await update.message.reply_text(
        "✅ Screenshot receive हो गया।\n\n"
        "अब UTR/Transaction ID भेजें।"
    )


# ============================================================
# PAYMENT UTR
# ============================================================

async def payment_utr(
    update,
    context
):

    payment_id = context.user_data.get(
        "proof_payment_id"
    )

    if not payment_id:
        return False

    payment = get_payment(
        payment_id
    )

    if not payment:
        context.user_data.pop(
            "proof_payment_id",
            None
        )

        return False

    user = update.effective_user

    utr = (
        update.message.text
        or ""
    ).strip()

    proof_file_id = context.user_data.get(
        "payment_proof_file_id"
    )

    payments_col.update_one(
        {
            "payment_id": payment_id
        },
        {
            "$set": {
                "utr": utr[:200],
                "proof_file_id": proof_file_id or "",
                "submitted_at": int(
                    time.time()
                ),
            }
        }
    )

    admin_text = (
        "💳 <b>New Payment Request</b>\n\n"
        f"Payment ID: "
        f"<code>{payment_id}</code>\n"
        f"User ID: "
        f"<code>{user.id}</code>\n"
        f"Username: "
        f"@{user.username or 'N/A'}\n"
        f"Plan: "
        f"{payment['plan'].upper()}\n"
        f"Amount: ₹{payment['amount']}\n"
        f"UTR: "
        f"<code>{utr[:200]}</code>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ APPROVE",
                callback_data=(
                    f"approve_{payment_id}"
                )
            ),
            InlineKeyboardButton(
                "❌ REJECT",
                callback_data=(
                    f"reject_{payment_id}"
                )
            )
        ]
    ])

    for admin_id in ADMIN_IDS:

        try:

            if proof_file_id:

                await context.bot.send_photo(
                    admin_id,
                    proof_file_id,
                    caption=admin_text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            else:

                await context.bot.send_message(
                    admin_id,
                    admin_text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        except Exception as exc:

            logger.error(
                "Payment admin notify error: %s",
                exc
            )

    await update.message.reply_text(
        "✅ Payment proof submit हो गया है।\n\n"
        "Admin verification के बाद premium activate होगा।"
    )

    context.user_data.pop(
        "proof_payment_id",
        None
    )

    context.user_data.pop(
        "payment_proof_file_id",
        None
    )

    return True


# ============================================================
# ADMIN PAYMENT ACTION
# ============================================================

async def admin_payment_action(
    update,
    context
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

    if data.startswith(
        "approve_"
    ):

        payment_id = data.split(
            "_",
            1
        )[1]

        payment = get_payment(
            payment_id
        )

        if not payment:

            await query.answer(
                "Payment not found.",
                show_alert=True
            )

            return

        if payment["status"] == "approved":

            await query.answer(
                "Already approved.",
                show_alert=True
            )

            return

        days = int(
            get_setting(
                "premium_days",
                DEFAULT_PREMIUM_DAYS
            )
        )

        payments_col.update_one(
            {
                "payment_id": payment_id
            },
            {
                "$set": {
                    "status": "approved",
                    "approved_at": int(
                        time.time()
                    ),
                    "approved_by":
                        query.from_user.id,
                }
            }
        )

        until = activate_premium(
            payment["user_id"],
            days
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "✅ Payment Approved\n"
            f"Payment: {payment_id}\n"
            f"User: {payment['user_id']}\n"
            f"Premium: {days} days"
        )

        try:

            await context.bot.send_message(
                payment["user_id"],
                (
                    "🎉 <b>Premium Activated!</b>\n\n"
                    f"Plan: "
                    f"{payment['plan'].upper()}\n"
                    f"Validity: "
                    f"{days} days\n\n"
                    "अब premium download available है।"
                ),
                parse_mode="HTML",
                reply_markup=main_keyboard()
            )

        except Exception:
            pass

        return

    if data.startswith(
        "reject_"
    ):

        payment_id = data.split(
            "_",
            1
        )[1]

        payment = get_payment(
            payment_id
        )

        if not payment:
            return

        payments_col.update_one(
            {
                "payment_id": payment_id
            },
            {
                "$set": {
                    "status": "rejected",
                    "rejected_at": int(
                        time.time()
                    ),
                    "rejected_by":
                        query.from_user.id,
                }
            }
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            f"❌ Payment Rejected\n"
            f"Payment: {payment_id}"
        )

        try:

            await context.bot.send_message(
                payment["user_id"],
                "❌ आपका payment proof reject किया गया है।"
            )

        except Exception:
            pass


# ============================================================
# DOWNLOAD PROCESS
# ============================================================

download_lock = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)


async def process_url(
    update,
    context,
    url,
    requested_type,
    progress_message=None,
    progress_prefix=""
):

    user = update.effective_user

    if not valid_url(
        url
    ):

        return (
            False,
            "Invalid URL"
        )

    if not premium_active(
        user.id
    ):

        return (
            False,
            "premium"
        )

    work_dir = tempfile.mkdtemp(
        dir=DOWNLOAD_DIR
    )

    try:

        async with download_lock:

            # IMPORTANT: telegram.Message uses slots and cannot accept custom
            # attributes such as _progress_state. Keep progress state locally.
            progress_state = {"time": 0.0, "percent": -1, "downloaded": -1}

            async def progress_callback(downloaded, total_size, force=False):
                if not progress_message:
                    return

                now = time.monotonic()
                if not force and now - progress_state["time"] < 1.0:
                    return

                if total_size > 0:
                    percent = min(100, int(downloaded * 100 / total_size))
                else:
                    percent = -1

                # Avoid unnecessary Telegram edit requests.
                if (
                    not force
                    and percent == progress_state["percent"]
                    and downloaded == progress_state["downloaded"]
                ):
                    return

                progress_state["time"] = now
                progress_state["percent"] = percent
                progress_state["downloaded"] = downloaded

                if total_size > 0:
                    filled = min(20, percent // 5)
                    bar = "█" * filled + "░" * (20 - filled)
                    text = (
                        f"{progress_prefix}\n"
                        f"📥 {bar} {percent}%\n"
                        f"📦 {downloaded / 1024 / 1024:.2f} / "
                        f"{total_size / 1024 / 1024:.2f} MB"
                    )
                else:
                    text = (
                        f"{progress_prefix}\n"
                        "📥 Downloading...\n"
                        f"📦 {downloaded / 1024 / 1024:.2f} MB"
                    )

                try:
                    await progress_message.edit_text(
                        fix_mojibake(text)
                    )
                except Exception:
                    # Telegram can reject an identical edit or rate-limit an
                    # update; the actual download must continue regardless.
                    pass

            result = await download_file(
                url,
                work_dir,
                requested_type,
                progress_callback
            )

        detected = detect_type(
            result["filename"],
            result["content_type"]
        )

        if (
            requested_type
            and
            detected != requested_type
        ):

            raise RuntimeError(
                "URL से requested file type नहीं मिला।"
            )

        save_download_log(
            user,
            url,
            detected,
            result["filename"],
            result["size"],
            "success"
        )

        await send_log_channel(
            update,
            context,
            url,
            result,
            detected
        )

        caption = (
            f"📥 <b>{result['filename']}</b>\n\n"
            f"📦 "
            f"{result['size'] / 1024 / 1024:.2f} MB"
        )

        with open(
            result["path"],
            "rb"
        ) as file:

            if detected == "video":

                await update.effective_chat.send_video(
                    video=InputFile(
                        file,
                        filename=result["filename"]
                    ),
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True
                )

            else:

                await update.effective_chat.send_document(
                    document=InputFile(
                        file,
                        filename=result["filename"]
                    ),
                    caption=caption,
                    parse_mode="HTML"
                )

        if progress_message:
            try:
                await progress_message.edit_text(
                    fix_mojibake(
                        f"{progress_prefix}\\n"
                        f"✅ Download complete\\n"
                        f"📄 {result['filename']}\\n"
                        f"📦 {result['size'] / 1024 / 1024:.2f} MB"
                    )
                )
            except Exception:
                pass

        return (
            True,
            "success"
        )

    except Exception as exc:

        save_download_log(
            user,
            url,
            requested_type or "unknown",
            "",
            0,
            f"error: {exc}"
        )

        await send_error_log(
            update,
            context,
            url,
            str(exc)
        )

        if progress_message:
            try:
                await progress_message.edit_text(
                    fix_mojibake(
                        f"{progress_prefix}\\n"
                        f"❌ Download failed\\n"
                        f"⚠️ {str(exc)[:900]}"
                    )
                )
            except Exception:
                pass

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
# LOG CHANNEL
# ============================================================

def log_channel_id():

    try:
        return int(
            LOG_CHANNEL_ID
        )
    except Exception:
        return None


async def send_log_channel(
    update,
    context,
    url,
    result,
    detected
):

    channel_id = log_channel_id()

    if not channel_id:
        return

    user = update.effective_user

    text = (
        "📥 <b>DOWNLOAD LOG</b>\n\n"
        f"👤 User ID: "
        f"<code>{user.id}</code>\n"
        f"👤 Username: "
        f"@{user.username or 'N/A'}\n"
        f"📁 File: "
        f"<code>{result['filename']}</code>\n"
        f"📌 Type: "
        f"{detected}\n"
        f"📦 Size: "
        f"{result['size'] / 1024 / 1024:.2f} MB\n"
        f"🔗 URL:\n"
        f"<code>{url[:3000]}</code>"
    )

    try:

        await context.bot.send_message(
            channel_id,
            text,
            parse_mode="HTML"
        )

    except Exception as exc:

        logger.error(
            "Log channel error: %s",
            exc
        )


async def send_error_log(
    update,
    context,
    url,
    error
):

    channel_id = log_channel_id()

    if not channel_id:
        return

    user = update.effective_user

    try:

        await context.bot.send_message(
            channel_id,
            (
                "❌ <b>DOWNLOAD ERROR</b>\n\n"
                f"User: <code>{user.id}</code>\n"
                f"URL:\n"
                f"<code>{url[:3000]}</code>\n\n"
                f"Error:\n"
                f"<code>{str(error)[:1500]}</code>"
            ),
            parse_mode="HTML"
        )

    except Exception:
        pass


async def send_input_log(
    update,
    context,
    input_type,
    content
):

    channel_id = log_channel_id()

    if not channel_id:
        return

    user = update.effective_user

    try:

        await context.bot.send_message(
            channel_id,
            (
                f"📨 <b>{input_type}</b>\n\n"
                f"User ID: <code>{user.id}</code>\n"
                f"Username: @{user.username or 'N/A'}\n\n"
                f"{content[:3500]}"
            ),
            parse_mode="HTML"
        )

    except Exception:
        pass


# ============================================================
# TEXT
# ============================================================

async def handle_text(
    update,
    context
):

    user = update.effective_user

    save_user(
        user
    )

    if not await require_force_join(
        update,
        context
    ):
        return

    # Channel setup
    if context.user_data.get(
        "waiting_channel"
    ):

        value = (
            update.message.text
            or ""
        ).strip()

        await send_input_log(
            update,
            context,
            "CHANNEL INPUT",
            value
        )

        try:

            chat = await context.bot.get_chat(
                value
            )

            member = (
                await context.bot.get_chat_member(
                    chat.id,
                    context.bot.id
                )
            )

            if member.status not in {
                "administrator",
                "creator",
            }:

                await update.message.reply_text(
                    "❌ Bot को channel में Administrator बनाएं।"
                )

                return

            channels_col.update_one(
                {
                    "chat_id": chat.id
                },
                {
                    "$set": {
                        "chat_id": chat.id,
                        "title": chat.title or "",
                        "username": (
                            chat.username or ""
                        ),
                        "owner_user_id": user.id,
                        "added_at": int(
                            time.time()
                        ),
                    }
                },
                upsert=True
            )

            context.user_data.pop(
                "waiting_channel",
                None
            )

            await update.message.reply_text(
                (
                    "✅ Channel successfully added.\n\n"
                    f"Channel: {chat.title or 'N/A'}\n"
                    f"ID: <code>{chat.id}</code>\n\n"
                    "अब download के बाद file "
                    "channel में भेजने का option "
                    "admin configuration के अनुसार "
                    "use किया जा सकता है।"
                ),
                parse_mode="HTML"
            )

        except Exception as exc:

            await update.message.reply_text(
                "❌ Channel add नहीं हुआ।\n\n"
                "Bot को channel में Admin बनाकर "
                "फिर channel username/ID भेजें।"
            )

            logger.warning(
                "Channel setup: %s",
                exc
            )

        return

    # Payment UTR
    if context.user_data.get(
        "proof_payment_id"
    ):

        if await payment_utr(
            update,
            context
        ):

            return

    urls = extract_urls(
        update.message.text
    )

    if not urls:

        await update.message.reply_text(
            "❌ Valid HTTP/HTTPS URL नहीं मिला।"
        )

        return

    requested_type = context.user_data.get(
        "requested_type"
    )

    if not premium_active(
        user.id
    ):

        await update.message.reply_text(
            "💎 यह download feature Premium है।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💎 Buy Premium",
                        callback_data="premium"
                    )
                ]
            ])
        )

        return

    await send_input_log(
        update,
        context,
        "URL INPUT",
        "\n".join(urls)
    )

    await update.message.reply_text(
        f"🔎 {len(urls)} link मिले।\n"
        "⏳ Download शुरू हो रहा है..."
    )

    for index, url in enumerate(
        urls,
        1
    ):

        progress_message = await update.message.reply_text(
            fix_mojibake(
                f"📥 {index}/{len(urls)}\n"
                "⏳ Download शुरू हो रहा है..."
            )
        )

        success, result = await process_url(
            update,
            context,
            url,
            requested_type,
            progress_message=progress_message,
            progress_prefix=f"📄 {index}/{len(urls)}"
        )

        if not success:
            try:
                await progress_message.edit_text(
                    fix_mojibake(
                        f"📄 {index}/{len(urls)}\n"
                        f"❌ Error: {result}"
                    )
                )
            except Exception:
                pass


# ============================================================
# TXT
# ============================================================

async def handle_txt(
    update,
    context
):

    user = update.effective_user

    save_user(
        user
    )

    if not get_setting(
        "txt_enabled",
        True
    ):

        await update.message.reply_text(
            "❌ TXT batch download disabled है।"
        )

        return

    if not await require_force_join(
        update,
        context
    ):
        return

    if not premium_active(
        user.id
    ):

        await update.message.reply_text(
            "💎 TXT batch download Premium feature है।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💎 Buy Premium",
                        callback_data="premium"
                    )
                ]
            ])
        )

        return

    filename = (
        update.message.document.file_name
        or ""
    )

    if not filename.lower().endswith(
        ".txt"
    ):

        await update.message.reply_text(
            "❌ केवल TXT file भेजें।"
        )

        return

    await send_input_log(
        update,
        context,
        "TXT FILE",
        filename
    )

    temp_path = (
        DOWNLOAD_DIR
        /
        f"{uuid.uuid4().hex}.txt"
    )

    try:

        tg_file = await (
            update.message.document
            .get_file()
        )

        await tg_file.download_to_drive(
            custom_path=str(
                temp_path
            )
        )

        async with aiofiles.open(
            temp_path,
            "r",
            encoding="utf-8",
            errors="ignore"
        ) as file:

            text = await file.read()

        urls = extract_urls(
            text
        )

        if not urls:

            await update.message.reply_text(
                "❌ TXT में कोई valid URL नहीं मिला।"
            )

            return

        await update.message.reply_text(
            fix_mojibake(
                f"📋 {len(urls)} links मिले।\n"
                "⏳ Files एक-एक करके download होंगी।"
            )
        )

        requested_type = context.user_data.get(
            "requested_type"
        )

        for index, url in enumerate(
            urls,
            1
        ):
            # Await keeps every PDF strictly sequential.
            progress_message = await update.message.reply_text(
                fix_mojibake(
                    f"📄 {index}/{len(urls)}\n"
                    "⏳ Download शुरू हो रहा है..."
                )
            )

            success, result = await process_url(
                update,
                context,
                url,
                requested_type,
                progress_message=progress_message,
                progress_prefix=f"📄 {index}/{len(urls)}"
            )

            if not success:
                try:
                    await progress_message.edit_text(
                        fix_mojibake(
                            f"📄 {index}/{len(urls)}\n"
                            f"❌ Error: {result}"
                        )
                    )
                except Exception:
                    pass

    except Exception as exc:

        logger.exception(
            "TXT processing error"
        )

        await update.message.reply_text(
            f"❌ TXT error:\n{exc}"
        )

    finally:

        try:
            temp_path.unlink()
        except Exception:
            pass


# ============================================================
# ADMIN COMMAND
# ============================================================

def is_admin(
    user_id
):

    return user_id in ADMIN_IDS


async def admin(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "❌ Unauthorized"
        )

        return

    await update.message.reply_text(
        (
            "👨‍💻 <b>ADMIN PANEL</b>\n\n"

            "📊 /stats\n"

            "🏠 /setwelcome TEXT\n"
            "🆘 /sethelp TEXT\n"

            "💰 /setpdfprice NUMBER\n"
            "💰 /setvideoprice NUMBER\n"
            "⏳ /setdays NUMBER\n"
            "💳 /setupi UPI\n"
            "/setpayment TEXT\n"

            "🔒 /forcejoin on|off\n"
            "/addforcejoin CHAT_ID INVITE_URL\n"
            "/removeforcejoin CHAT_ID\n"

            "📄 /pdf on|off\n"
            "🎥 /video on|off\n"
            "📋 /txt on|off\n"

            "📢 /channelupload on|off\n"
            "🛠 /maintenance on|off\n"

            "🖼 /setwelcomeimage\n"

            "👤 /premium USER_ID DAYS\n"
            "🚫 /removePremium USER_ID\n"

            "📢 /channels\n"
            "/removechannel CHAT_ID\n\n"

            "💡 Welcome image बदलने के लिए:\n"
            "/setwelcomeimage भेजें और फिर image भेजें।"
        ),
        parse_mode="HTML"
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def stats(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    users = users_col.count_documents({})

    premium = users_col.count_documents({
        "premium": True,
        "premium_until": {
            "$gt": int(
                time.time()
            )
        }
    })

    downloads = downloads_col.count_documents({})

    success = downloads_col.count_documents({
        "status": "success"
    })

    pending = payments_col.count_documents({
        "status": "pending"
    })

    approved = payments_col.count_documents({
        "status": "approved"
    })

    await update.message.reply_text(
        (
            "📊 <b>BOT STATISTICS</b>\n\n"
            f"👥 Users: {users}\n"
            f"💎 Active Premium: {premium}\n"
            f"📥 Downloads: {downloads}\n"
            f"✅ Successful: {success}\n"
            f"💳 Pending Payments: {pending}\n"
            f"💰 Approved Payments: {approved}"
        ),
        parse_mode="HTML"
    )


# ============================================================
# ADMIN SETTERS
# ============================================================

async def setwelcome(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    text = update.message.text[
        len("/setwelcome"):
    ].strip()

    if not text:

        await update.message.reply_text(
            "/setwelcome आपका welcome message"
        )

        return

    set_setting(
        "welcome_text",
        text
    )

    await update.message.reply_text(
        "✅ Welcome message updated."
    )


async def sethelp(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    text = update.message.text[
        len("/sethelp"):
    ].strip()

    if not text:

        await update.message.reply_text(
            "/sethelp आपका help message"
        )

        return

    set_setting(
        "help_text",
        text
    )

    await update.message.reply_text(
        "✅ Help updated."
    )


async def setpayment(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    text = update.message.text[
        len("/setpayment"):
    ].strip()

    set_setting(
        "payment_text",
        text
    )

    await update.message.reply_text(
        "✅ Payment instructions updated."
    )


async def setupi(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await update.message.reply_text(
            "/setupi yourupi@upi"
        )

        return

    set_setting(
        "upi_id",
        parts[1].strip()
    )

    await update.message.reply_text(
        "✅ UPI updated."
    )


async def set_pdf_price(
    update,
    context
):

    await set_numeric_setting(
        update,
        "pdf_price",
        "/setpdfprice 49"
    )


async def set_video_price(
    update,
    context
):

    await set_numeric_setting(
        update,
        "video_price",
        "/setvideoprice 99"
    )


async def set_days(
    update,
    context
):

    await set_numeric_setting(
        update,
        "premium_days",
        "/setdays 30"
    )


async def set_numeric_setting(
    update,
    key,
    usage
):

    if not is_admin(
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

        value = int(
            parts[1]
        )

        if value < 0:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid number."
        )

        return

    set_setting(
        key,
        value
    )

    await update.message.reply_text(
        f"✅ {key} = {value}"
    )


# ============================================================
# TOGGLE
# ============================================================

async def toggle_setting(
    update,
    key,
    usage
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split()

    if len(parts) != 2:

        await update.message.reply_text(
            usage
        )

        return

    value = (
        parts[1].lower()
        == "on"
    )

    set_setting(
        key,
        value
    )

    await update.message.reply_text(
        f"✅ {key}: "
        f"{'ON' if value else 'OFF'}"
    )


async def forcejoin_toggle(
    update,
    context
):

    await toggle_setting(
        update,
        "force_join_enabled",
        "/forcejoin on|off"
    )


async def pdf_toggle(
    update,
    context
):

    await toggle_setting(
        update,
        "download_pdf_enabled",
        "/pdf on|off"
    )


async def video_toggle(
    update,
    context
):

    await toggle_setting(
        update,
        "download_video_enabled",
        "/video on|off"
    )


async def txt_toggle(
    update,
    context
):

    await toggle_setting(
        update,
        "txt_enabled",
        "/txt on|off"
    )


async def channel_upload_toggle(
    update,
    context
):

    await toggle_setting(
        update,
        "channel_upload_enabled",
        "/channelupload on|off"
    )


async def maintenance_toggle(
    update,
    context
):

    await toggle_setting(
        update,
        "maintenance",
        "/maintenance on|off"
    )


# ============================================================
# FORCE JOIN CHANNELS
# ============================================================

async def add_force_join(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split(
        maxsplit=2
    )

    if len(parts) != 3:

        await update.message.reply_text(
            "/addforcejoin CHAT_ID INVITE_URL"
        )

        return

    try:

        chat_id = int(
            parts[1]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid CHAT_ID"
        )

        return

    invite = parts[2].strip()

    channels = get_setting(
        "force_join_channels",
        []
    )

    channels = [
        x
        for x in channels
        if x.get(
            "chat_id"
        ) != chat_id
    ]

    channels.append({
        "chat_id": chat_id,
        "invite": invite,
    })

    set_setting(
        "force_join_channels",
        channels
    )

    await update.message.reply_text(
        "✅ Force join channel added."
    )


async def remove_force_join(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split()

    if len(parts) != 2:
        return

    try:
        chat_id = int(
            parts[1]
        )
    except ValueError:
        return

    channels = get_setting(
        "force_join_channels",
        []
    )

    channels = [
        x
        for x in channels
        if x.get(
            "chat_id"
        ) != chat_id
    ]

    set_setting(
        "force_join_channels",
        channels
    )

    await update.message.reply_text(
        "✅ Force join channel removed."
    )


# ============================================================
# PREMIUM MANUAL ADMIN
# ============================================================

async def manual_premium(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split()

    if len(parts) != 3:

        await update.message.reply_text(
            "/premium USER_ID DAYS"
        )

        return

    try:

        user_id = int(
            parts[1]
        )

        days = int(
            parts[2]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid values."
        )

        return

    activate_premium(
        user_id,
        days
    )

    await update.message.reply_text(
        "✅ Premium activated."
    )


async def remove_premium(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split()

    if len(parts) != 2:
        return

    try:
        user_id = int(
            parts[1]
        )
    except ValueError:
        return

    deactivate_premium(
        user_id
    )

    await update.message.reply_text(
        "✅ Premium removed."
    )


# ============================================================
# WELCOME IMAGE
# ============================================================

async def setwelcomeimage(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    context.user_data[
        "waiting_welcome_image"
    ] = True

    await update.message.reply_text(
        "🖼 अब welcome image भेजें।"
    )


async def admin_photo(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not context.user_data.get(
        "waiting_welcome_image"
    ):

        return

    photo = update.message.photo[-1]

    tg_file = await photo.get_file()

    for old in WELCOME_DIR.glob("*"):

        try:
            old.unlink()
        except Exception:
            pass

    target = (
        WELCOME_DIR
        /
        "welcome.jpg"
    )

    await tg_file.download_to_drive(
        custom_path=str(
            target
        )
    )

    context.user_data.pop(
        "waiting_welcome_image",
        None
    )

    await update.message.reply_text(
        "✅ Welcome image updated."
    )


# ============================================================
# USER CHANNELS
# ============================================================

async def channels(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    items = list(
        channels_col.find(
            {}
        )
    )

    if not items:

        await update.message.reply_text(
            "No channels."
        )

        return

    text = "📢 <b>CHANNELS</b>\n\n"

    for item in items:

        text += (
            f"• {item.get('title','')}\n"
            f"  ID: <code>{item['chat_id']}</code>\n"
            f"  Owner: {item.get('owner_user_id')}\n\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


async def remove_channel(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    parts = update.message.text.split()

    if len(parts) != 2:
        return

    try:
        chat_id = int(
            parts[1]
        )
    except ValueError:
        return

    channels_col.delete_one(
        {
            "chat_id": chat_id
        }
    )

    await update.message.reply_text(
        "✅ Channel removed."
    )


# ============================================================
# FLASK ADMIN PANEL
# ============================================================

web_app = Flask(
    __name__
)

web_app.secret_key = SECRET_KEY


ADMIN_HTML = """
<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>Downloader Admin Panel</title>
<style>
*{
box-sizing:border-box
}
body{
margin:0;
font-family:Arial,sans-serif;
background:#070a10;
color:#fff
}
.wrap{
max-width:1100px;
margin:auto;
padding:20px
}
.card{
background:#101620;
border:1px solid #273142;
border-radius:16px;
padding:18px;
margin-bottom:15px
}
.grid{
display:grid;
grid-template-columns:
repeat(auto-fit,minmax(220px,1fr));
gap:12px
}
.stat{
font-size:30px;
font-weight:800
}
input,textarea,select{
width:100%;
padding:12px;
margin-top:8px;
border-radius:10px;
border:1px solid #303b4d;
background:#080d14;
color:#fff
}
textarea{
min-height:120px;
resize:vertical
}
button{
width:100%;
padding:12px;
margin-top:12px;
border:0;
border-radius:10px;
background:#4777ff;
color:#fff;
font-weight:700;
cursor:pointer
}
label{
display:block;
margin-top:12px;
font-size:13px;
color:#aab4c4
}
</style>
</head>

<body>

<div class="wrap">

<div class="card">
<h1>👨‍💻 Downloader Admin Panel</h1>
<p>MongoDB Persistent Control Panel</p>
</div>

<div class="grid">

<div class="card">
Users
<div class="stat">
{{stats.users}}
</div>
</div>

<div class="card">
Premium
<div class="stat">
{{stats.premium}}
</div>
</div>

<div class="card">
Downloads
<div class="stat">
{{stats.downloads}}
</div>
</div>

<div class="card">
Pending Payments
<div class="stat">
{{stats.pending}}
</div>
</div>

</div>

<form
method="post"
action="/admin/save">

<div class="card">

<h2>🏠 Welcome</h2>

<label>Welcome Message</label>

<textarea
name="welcome_text"
>{{settings.welcome_text}}</textarea>

</div>

<div class="card">

<h2>🆘 Help</h2>

<label>Help Message</label>

<textarea
name="help_text"
>{{settings.help_text}}</textarea>

</div>

<div class="card">

<h2>💎 Premium</h2>

<label>PDF Price</label>

<input
type="number"
name="pdf_price"
value="{{settings.pdf_price}}">

<label>Video Price</label>

<input
type="number"
name="video_price"
value="{{settings.video_price}}">

<label>Premium Days</label>

<input
type="number"
name="premium_days"
value="{{settings.premium_days}}">

<label>UPI ID</label>

<input
name="upi_id"
value="{{settings.upi_id}}">

<label>Payment Instructions</label>

<textarea
name="payment_text"
>{{settings.payment_text}}</textarea>

<button>
💾 Save Premium Settings
</button>

</div>

<div class="card">

<h2>⚙️ Features</h2>

<label>
PDF Download
<select name="download_pdf_enabled">
<option
value="1"
{% if settings.download_pdf_enabled %}
selected
{% endif %}
>ON</option>
<option
value="0"
{% if not settings.download_pdf_enabled %}
selected
{% endif %}
>OFF</option>
</select>
</label>

<label>
Video Download
<select name="download_video_enabled">
<option
value="1"
{% if settings.download_video_enabled %}
selected
{% endif %}
>ON</option>
<option
value="0"
{% if not settings.download_video_enabled %}
selected
{% endif %}
>OFF</option>
</select>
</label>

<label>
TXT Download
<select name="txt_enabled">
<option
value="1"
{% if settings.txt_enabled %}
selected
{% endif %}
>ON</option>
<option
value="0"
{% if not settings.txt_enabled %}
selected
{% endif %}
>OFF</option>
</select>
</label>

<label>
Channel Upload
<select name="channel_upload_enabled">
<option
value="1"
{% if settings.channel_upload_enabled %}
selected
{% endif %}
>ON</option>
<option
value="0"
{% if not settings.channel_upload_enabled %}
selected
{% endif %}
>OFF</option>
</select>
</label>

<label>
Force Join
<select name="force_join_enabled">
<option
value="1"
{% if settings.force_join_enabled %}
selected
{% endif %}
>ON</option>
<option
value="0"
{% if not settings.force_join_enabled %}
selected
{% endif %}
>OFF</option>
</select>
</label>

<label>
Maintenance
<select name="maintenance">
<option
value="1"
{% if settings.maintenance %}
selected
{% endif %}
>ON</option>
<option
value="0"
{% if not settings.maintenance %}
selected
{% endif %}
>OFF</option>
</select>
</label>

<button>
💾 Save Feature Settings
</button>

</div>

</form>

<div class="card">

<h2>🖼 Welcome Image</h2>

<form
method="post"
action="/admin/upload"
enctype="multipart/form-data">

<input
type="file"
name="image"
accept="image/*"
required>

<button>
Upload / Change Welcome Image
</button>

</form>

</div>

<div class="card">

<h2>📢 Force Join Channels</h2>

<form
method="post"
action="/admin/forcejoin">

<input
name="chat_id"
placeholder="Channel ID"
required>

<input
name="invite"
placeholder="https://t.me/..."
required>

<button>
Add Force Join Channel
</button>

</form>

</div>

<div class="card">

<h2>🚪 Logout</h2>

<a href="/admin/logout">
Logout
</a>

</div>

</div>

</body>
</html>
"""


@web_app.route(
    "/admin/login",
    methods=[
        "GET",
        "POST"
    ]
)
def admin_login():

    if request.method == "POST":

        password = request.form.get(
            "password",
            ""
        )

        if password == SECRET_KEY:

            session[
                "admin"
            ] = True

            return redirect(
                "/admin"
            )

        return (
            "Invalid password"
        )

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
">

<br>

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


@web_app.route(
    "/admin"
)
def admin_page():

    if not session.get(
        "admin"
    ):

        return redirect(
            "/admin/login"
        )

    stats_data = {

        "users":
            users_col.count_documents({}),

        "premium":
            users_col.count_documents({
                "premium": True,
                "premium_until": {
                    "$gt": int(
                        time.time()
                    )
                }
            }),

        "downloads":
            downloads_col.count_documents({}),

        "pending":
            payments_col.count_documents({
                "status": "pending"
            }),
    }

    keys = [
        "welcome_text",
        "help_text",
        "payment_text",
        "upi_id",
        "pdf_price",
        "video_price",
        "premium_days",
        "download_pdf_enabled",
        "download_video_enabled",
        "txt_enabled",
        "channel_upload_enabled",
        "force_join_enabled",
        "maintenance",
    ]

    settings = {
        key:
            get_setting(
                key
            )
        for key in keys
    }

    return render_template_string(
        ADMIN_HTML,
        stats=stats_data,
        settings=settings
    )


@web_app.route(
    "/admin/save",
    methods=["POST"]
)
def admin_save():

    if not session.get(
        "admin"
    ):

        return redirect(
            "/admin/login"
        )

    text_fields = [
        "welcome_text",
        "help_text",
        "payment_text",
        "upi_id",
    ]

    for field in text_fields:

        set_setting(
            field,
            request.form.get(
                field,
                ""
            ).strip()
        )

    numeric_fields = [
        "pdf_price",
        "video_price",
        "premium_days",
    ]

    for field in numeric_fields:

        try:

            value = int(
                request.form.get(
                    field,
                    "0"
                )
            )

            if value < 0:
                value = 0

        except ValueError:

            value = 0

        set_setting(
            field,
            value
        )

    boolean_fields = [
        "download_pdf_enabled",
        "download_video_enabled",
        "txt_enabled",
        "channel_upload_enabled",
        "force_join_enabled",
        "maintenance",
    ]

    for field in boolean_fields:

        set_setting(
            field,
            request.form.get(
                field
            ) == "1"
        )

    return redirect(
        "/admin"
    )


@web_app.route(
    "/admin/upload",
    methods=["POST"]
)
def admin_upload():

    if not session.get(
        "admin"
    ):

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

    extension = (
        Path(
            image.filename
            or
            "welcome.jpg"
        ).suffix.lower()
    )

    if extension not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }:

        return (
            "Invalid image"
        )

    for old in WELCOME_DIR.glob("*"):

        try:
            old.unlink()
        except Exception:
            pass

    image.save(
        WELCOME_DIR
        /
        f"welcome{extension}"
    )

    return redirect(
        "/admin"
    )


@web_app.route(
    "/admin/forcejoin",
    methods=["POST"]
)
def admin_forcejoin():

    if not session.get(
        "admin"
    ):

        return redirect(
            "/admin/login"
        )

    try:

        chat_id = int(
            request.form[
                "chat_id"
            ]
        )

    except Exception:

        return redirect(
            "/admin"
        )

    invite = request.form.get(
        "invite",
        ""
    ).strip()

    channels = get_setting(
        "force_join_channels",
        []
    )

    channels = [
        x
        for x in channels
        if x.get(
            "chat_id"
        ) != chat_id
    ]

    channels.append({
        "chat_id": chat_id,
        "invite": invite,
    })

    set_setting(
        "force_join_channels",
        channels
    )

    return redirect(
        "/admin"
    )


@web_app.route(
    "/admin/logout"
)
def admin_logout():

    session.clear()

    return redirect(
        "/admin/login"
    )


# ============================================================
# WEB SERVER
# ============================================================

def run_web():

    web_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context
):

    logger.exception(
        "Unhandled bot error",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN missing"
        )

    import threading

    thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    thread.start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )

    application.add_handler(
        CommandHandler(
            "setwelcome",
            setwelcome
        )
    )

    application.add_handler(
        CommandHandler(
            "sethelp",
            sethelp
        )
    )

    application.add_handler(
        CommandHandler(
            "setpayment",
            setpayment
        )
    )

    application.add_handler(
        CommandHandler(
            "setupi",
            setupi
        )
    )

    application.add_handler(
        CommandHandler(
            "setpdfprice",
            set_pdf_price
        )
    )

    application.add_handler(
        CommandHandler(
            "setvideoprice",
            set_video_price
        )
    )

    application.add_handler(
        CommandHandler(
            "setdays",
            set_days
        )
    )

    application.add_handler(
        CommandHandler(
            "forcejoin",
            forcejoin_toggle
        )
    )

    application.add_handler(
        CommandHandler(
            "addforcejoin",
            add_force_join
        )
    )

    application.add_handler(
        CommandHandler(
            "removeforcejoin",
            remove_force_join
        )
    )

    application.add_handler(
        CommandHandler(
            "pdf",
            pdf_toggle
        )
    )

    application.add_handler(
        CommandHandler(
            "video",
            video_toggle
        )
    )

    application.add_handler(
        CommandHandler(
            "txt",
            txt_toggle
        )
    )

    application.add_handler(
        CommandHandler(
            "channelupload",
            channel_upload_toggle
        )
    )

    application.add_handler(
        CommandHandler(
            "maintenance",
            maintenance_toggle
        )
    )

    application.add_handler(
        CommandHandler(
            "premium",
            manual_premium
        )
    )

    application.add_handler(
        CommandHandler(
            "removePremium",
            remove_premium
        )
    )

    application.add_handler(
        CommandHandler(
            "setwelcomeimage",
            setwelcomeimage
        )
    )

    application.add_handler(
        CommandHandler(
            "channels",
            channels
        )
    )

    application.add_handler(
        CommandHandler(
            "removechannel",
            remove_channel
        )
    )

    # Admin payment
    application.add_handler(
        CallbackQueryHandler(
            admin_payment_action,
            pattern=r"^(approve|reject)_"
        )
    )

    # Normal callback
    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # Admin welcome image
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            admin_photo
        ),
        group=1
    )

    # Payment screenshot
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            payment_photo
        ),
        group=2
    )

    # TXT
    application.add_handler(
        MessageHandler(
            filters.Document.FileExtension(
                "txt"
            ),
            handle_txt
        )
    )

    # Normal text
    application.add_handler(
        MessageHandler(
            filters.TEXT
            &
            ~filters.COMMAND,
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
