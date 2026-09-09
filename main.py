import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import requests
from flask import Flask, request


app = Flask(__name__)


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# chat_id -> {
#     "process": subprocess.Popen,
#     "cancelled": threading.Event,
# }
active_downloads = {}

lock = threading.Lock()


# ─────────────────────────────────────────────────────────────
# Telegram API
# ─────────────────────────────────────────────────────────────

def telegram(method, **kwargs):
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        data=kwargs,
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result)

    return result


def send_message(chat_id, text):
    return telegram(
        "sendMessage",
        chat_id=chat_id,
        text=text,
    )


def edit_message(chat_id, message_id, text):
    return telegram(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
    )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)


def extract_url(text):
    match = URL_RE.search(text or "")

    if not match:
        return None

    return match.group(0).rstrip(".,!?)]}")


def format_size(size):
    size = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} PB"


def find_downloaded_file(directory):
    files = [
        file
        for file in directory.iterdir()
        if file.is_file()
        and not file.name.endswith(".aria2")
    ]

    if not files:
        return None

    return max(
        files,
        key=lambda file: file.stat().st_mtime,
    )


# ─────────────────────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────────────────────

def download_file(chat_id, status_message_id, url):
    job_dir = DOWNLOAD_DIR / str(chat_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    cancel_event = threading.Event()
    process = None

    try:
        command = [
            "aria2c",

            "--dir",
            str(job_dir),

            # Resume incomplete downloads.
            "--continue=true",

            # Download using multiple connections.
            "--max-connection-per-server=8",
            "--split=8",
            "--min-split-size=1M",

            # Progress output.
            "--summary-interval=1",

            # Don't create renamed copies such as file.1.zip.
            "--auto-file-renaming=false",

            url,
        ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        with lock:
            active_downloads[chat_id] = {
                "process": process,
                "cancelled": cancel_event,
            }

        last_progress = None

        # ─────────────────────────────────────────────────────
        # Read aria2 output
        # ─────────────────────────────────────────────────────

        for line in process.stdout:
            line = line.strip()

            # If /cancel was used, don't continue processing
            # progress information.
            if cancel_event.is_set():
                continue

            match = re.search(
                r"(\d+(?:\.\d+)?)([KMGTP]?i?B)/"
                r"(\d+(?:\.\d+)?)([KMGTP]?i?B)"
                r"\((\d+)%\)",
                line,
            )

            if not match:
                continue

            current = (
                f"{match.group(1)} "
                f"{match.group(2)}"
            )

            total = (
                f"{match.group(3)} "
                f"{match.group(4)}"
            )

            percent = int(match.group(5))

            progress = (
                "⏬ Downloading...\n\n"
                f"Progress: {percent}%\n"
                f"Size: {current} / {total}"
            )

            if progress == last_progress:
                continue

            last_progress = progress

            try:
                edit_message(
                    chat_id,
                    status_message_id,
                    progress,
                )
            except Exception as exc:
                print(
                    f"Progress update failed: {exc}"
                )

        # Wait for aria2 to actually exit.
        return_code = process.wait()

        # ─────────────────────────────────────────────────────
        # Cancellation
        # ─────────────────────────────────────────────────────

        if cancel_event.is_set():
            try:
                edit_message(
                    chat_id,
                    status_message_id,
                    "🛑 Download cancelled.",
                )
            except Exception:
                pass

            return

        # ─────────────────────────────────────────────────────
        # aria2 crashed / failed
        # ─────────────────────────────────────────────────────

        if return_code < 0:
            try:
                edit_message(
                    chat_id,
                    status_message_id,
                    "❌ Download crashed.\n\n"
                    "The aria2 process stopped unexpectedly.",
                )
            except Exception:
                pass

            return

        if return_code != 0:
            try:
                edit_message(
                    chat_id,
                    status_message_id,
                    "❌ Download failed.",
                )
            except Exception:
                pass

            return

        # ─────────────────────────────────────────────────────
        # Find downloaded file
        # ─────────────────────────────────────────────────────

        file_path = find_downloaded_file(job_dir)

        if not file_path:
            try:
                edit_message(
                    chat_id,
                    status_message_id,
                    "❌ Download completed, "
                    "but the file was not found.",
                )
            except Exception:
                pass

            return

        file_size = file_path.stat().st_size

        # ─────────────────────────────────────────────────────
        # Upload
        # ─────────────────────────────────────────────────────

        try:
            edit_message(
                chat_id,
                status_message_id,
                (
                    "✅ Download complete.\n\n"
                    f"📁 {file_path.name}\n"
                    f"📦 {format_size(file_size)}\n\n"
                    "📤 Uploading..."
                ),
            )
        except Exception:
            pass

        try:
            with file_path.open("rb") as file:
                response = requests.post(
                    f"{TELEGRAM_API}/sendDocument",
                    data={
                        "chat_id": chat_id,
                    },
                    files={
                        "document": (
                            file_path.name,
                            file,
                        )
                    },
                    timeout=600,
                )

            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                raise RuntimeError(result)

        except Exception as exc:
            print(f"Telegram upload failed: {exc}")

            try:
                edit_message(
                    chat_id,
                    status_message_id,
                    "❌ Upload failed.",
                )
            except Exception:
                pass

            return

        # Upload succeeded.
        try:
            telegram(
                "deleteMessage",
                chat_id=chat_id,
                message_id=status_message_id,
            )
        except Exception as exc:
            print(
                f"Could not delete status message: {exc}"
            )

    except Exception as exc:
        # Any unexpected Python-side exception.
        print(f"Download worker crashed: {exc}")

        # Don't call this a crash if the user intentionally
        # cancelled the download.
        if not cancel_event.is_set():
            try:
                edit_message(
                    chat_id,
                    status_message_id,
                    "❌ Download crashed.\n\n"
                    "An unexpected error occurred.",
                )
            except Exception:
                pass

    finally:
        # ─────────────────────────────────────────────────────
        # Always clean up
        # ─────────────────────────────────────────────────────

        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            except Exception:
                pass

        with lock:
            active_downloads.pop(chat_id, None)

        # Removes the downloaded file, .aria2 file,
        # and the entire temporary job directory.
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )


# ─────────────────────────────────────────────────────────────
# Update handling
# ─────────────────────────────────────────────────────────────

def handle_update(update):
    try:
        message = update.get("message")

        if not message:
            return

        user = message.get("from", {})
        user_id = user.get("id")

        # Private bot: only the configured user can use it.
        if user_id != ALLOWED_USER_ID:
            return

        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()

        # ─────────────────────────────────────────────────────
        # /start
        # ─────────────────────────────────────────────────────

        if text == "/start":
            send_message(
                chat_id,
                "👋 Send me a direct download URL.",
            )
            return

        # ─────────────────────────────────────────────────────
        # /cancel
        # ─────────────────────────────────────────────────────

        if text == "/cancel":
            with lock:
                download = active_downloads.get(chat_id)

            if not download:
                send_message(
                    chat_id,
                    "ℹ️ No active download.",
                )
                return

            download["cancelled"].set()

            process = download["process"]

            try:
                if process.poll() is None:
                    process.terminate()
            except Exception as exc:
                print(
                    f"Could not terminate aria2: {exc}"
                )

            return

        # ─────────────────────────────────────────────────────
        # URL
        # ─────────────────────────────────────────────────────

        url = extract_url(text)

        if not url:
            return

        # Only one download at a time.
        with lock:
            if chat_id in active_downloads:
                send_message(
                    chat_id,
                    (
                        "⚠️ A download is already running.\n"
                        "Use /cancel first."
                    ),
                )
                return

        status = send_message(
            chat_id,
            "⏬ Starting download...",
        )

        status_message_id = status["result"]["message_id"]

        threading.Thread(
            target=download_file,
            args=(
                chat_id,
                status_message_id,
                url,
            ),
            daemon=True,
        ).start()

    except Exception as exc:
        print(f"Update handling error: {exc}")


# ─────────────────────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return "OK"


@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "ok",
        "aria2": shutil.which("aria2c") is not None,
    }


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)

    if update:
        threading.Thread(
            target=handle_update,
            args=(update,),
            daemon=True,
        ).start()

    return "OK"


# ─────────────────────────────────────────────────────────────
# Local development
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
