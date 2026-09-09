import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

active_downloads = {}
lock = threading.Lock()


def telegram(method, **kwargs):
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        data=kwargs,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


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


def extract_url(text):
    match = re.search(r"https?://[^\s<>\"']+", text or "", re.I)

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


def download_file(chat_id, status_message_id, url):
    job_dir = DOWNLOAD_DIR / str(chat_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    command = [
        "aria2c",
        "--dir",
        str(job_dir),
        "--continue=true",
        "--max-connection-per-server=8",
        "--split=8",
        "--min-split-size=1M",
        "--summary-interval=1",
        url,
    ]

    process = None

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        with lock:
            active_downloads[chat_id] = process

        last_progress = None

        for line in process.stdout:
            line = line.strip()

            match = re.search(
                r"(\d+(?:\.\d+)?)([KMGTP]?i?B)/"
                r"(\d+(?:\.\d+)?)([KMGTP]?i?B)"
                r"\((\d+)%\)",
                line,
            )

            if not match:
                continue

            current = f"{match.group(1)} {match.group(2)}"
            total = f"{match.group(3)} {match.group(4)}"
            percent = int(match.group(5))

            progress = (
                f"⏬ Downloading...\n\n"
                f"Progress: {percent}%\n"
                f"Size: {current} / {total}"
            )

            if progress != last_progress:
                try:
                    edit_message(
                        chat_id,
                        status_message_id,
                        progress,
                    )
                    last_progress = progress
                except Exception:
                    pass

        return_code = process.wait()

        if return_code != 0:
            edit_message(
                chat_id,
                status_message_id,
                "❌ Download failed.",
            )
            return

        files = [
            f
            for f in job_dir.iterdir()
            if f.is_file() and not f.name.endswith(".aria2")
        ]

        if not files:
            edit_message(
                chat_id,
                status_message_id,
                "❌ Download completed, but the file was not found.",
            )
            return

        file_path = max(
            files,
            key=lambda f: f.stat().st_mtime,
        )

        edit_message(
            chat_id,
            status_message_id,
            (
                "✅ Download complete.\n\n"
                f"📁 {file_path.name}\n"
                f"📦 {format_size(file_path.stat().st_size)}\n\n"
                "📤 Uploading..."
            ),
        )

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

        if not response.json().get("ok"):
            raise RuntimeError(response.text)

        telegram(
            "deleteMessage",
            chat_id=chat_id,
            message_id=status_message_id,
        )

    except Exception as e:
        print(f"Download error: {e}")

        try:
            edit_message(
                chat_id,
                status_message_id,
                "❌ An error occurred.",
            )
        except Exception:
            pass

    finally:
        with lock:
            active_downloads.pop(chat_id, None)

        shutil.rmtree(job_dir, ignore_errors=True)


def handle_update(update):
    message = update.get("message")

    if not message:
        return

    user = message.get("from", {})
    user_id = user.get("id")

    if user_id != ALLOWED_USER_ID:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == "/start":
        send_message(
            chat_id,
            "Send me a direct download URL.",
        )
        return

    if text == "/cancel":
        with lock:
            process = active_downloads.get(chat_id)

        if process:
            process.terminate()

            send_message(
                chat_id,
                "🛑 Download cancelled.",
            )
        else:
            send_message(
                chat_id,
                "ℹ️ No active download.",
            )

        return

    url = extract_url(text)

    if not url:
        return

    with lock:
        if chat_id in active_downloads:
            send_message(
                chat_id,
                "⚠️ A download is already running.\nUse /cancel first.",
            )
            return

    status = send_message(
        chat_id,
        "⏬ Starting download...",
    )

    status_message_id = status["result"]["message_id"]

    threading.Thread(
        target=download_file,
        args=(chat_id, status_message_id, url),
        daemon=True,
    ).start()


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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
)
