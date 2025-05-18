from __future__ import annotations

"""remote_bot_server TLS edition – now with 100% more encryption.

* Generates a self‑signed cert on first run (openssl required).
* Falls back to plain HTTP if certificates are missing and cannot be created.
* Otherwise works exactly like the previous screaming pile of features.
"""

import json
import logging
import os
import secrets
import string
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ────────────────────────── CONFIG ─────────────────────────────────────────
ENV_FILE = Path(".env")
DB_FILE = Path("db.json")
API_PORT = int(os.getenv("PORT", "8000"))

# TLS files (override paths via SSL_CERT / SSL_KEY env vars)
CERT_FILE = Path(os.getenv("SSL_CERT", "cert.pem"))
KEY_FILE = Path(os.getenv("SSL_KEY", "key.pem"))

# ────────────────────────── helpers ────────────────────────────────────────

def _load_dotenv() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _ensure_ssl() -> None:
    """Create a self‑signed certificate if none exists."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    logging.info("🔒 Generating self‑signed TLS certificate (%s, %s)…", CERT_FILE, KEY_FILE)
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(KEY_FILE),
                str("-out"),
                str(CERT_FILE),
                "-days",
                "825",
                "-nodes",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logging.info("✅ Self‑signed certificate created.")
    except Exception as exc:
        logging.warning("⚠️  Failed to generate certificate automatically: %s", exc)
        logging.warning("   TLS will be disabled unless you supply cert.pem/key.pem manually.")


_load_dotenv()
_ensure_ssl()

TOKEN = os.getenv("BOT_TOKEN") or input("Enter Telegram BOT_TOKEN: ").strip()
if not TOKEN:
    print("❌ BOT_TOKEN required."); sys.exit(1)
if "BOT_TOKEN" not in os.environ:
    ENV_FILE.write_text((ENV_FILE.read_text() if ENV_FILE.exists() else "") + f"BOT_TOKEN={TOKEN}\n")
    print("🔏 TOKEN saved to .env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("remote-bot")

# DB helpers ----------------------------------------------------------------

def load_db() -> Dict[str, Any]:
    if DB_FILE.exists():
        data = json.loads(DB_FILE.read_text())
    else:
        data = {}
    data.setdefault("secrets", {})
    data.setdefault("active", {})
    return data


def save_db(db: Dict[str, Any]):
    DB_FILE.write_text(json.dumps(db, indent=2))

# ──────────────────────── Telegram command handlers ───────────────────────
OWNER_HELP = (
    "Команды:\n"
    "/newkey <имя> – создать ключ.\n"
    "/linkkey <ключ> – подписаться на чужой ключ.\n"
    "/setactivekey <ключ> – выбрать активный.\n"
    "/list – показать свои ключи.\n"
    "/status <секрет> – метрики + кнопки.\n"
    "/renamekey <ключ> <имя> – переименовать ключ."
)

def gen_secret(n: int = 20):
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

# helper: check membership --------------------------------------------------

def is_owner(entry: Dict[str, Any], user_id: int) -> bool:
    return user_id in entry.get("owners", [])

# commands ------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот для мониторинга ПК.\n" + OWNER_HELP)


async def cmd_newkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = " ".join(ctx.args)[:30] if ctx.args else "PC"
    db = load_db()
    secret = gen_secret()
    db["secrets"][secret] = {
        "owners": [update.effective_user.id],
        "nickname": name,
        "status": None,
        "pending": [],
    }
    db["active"][str(update.effective_chat.id)] = secret
    save_db(db)
    await update.message.reply_text(
        f"Секрет `{secret}` создан (название: {name}) и сделан активным.", parse_mode="Markdown"
    )


async def cmd_linkkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Синтаксис: /linkkey <key>")
    secret = ctx.args[0]
    db = load_db()
    entry = db["secrets"].get(secret)
    if not entry:
        return await update.message.reply_text("🚫 Ключ не найден.")
    if update.effective_user.id in entry["owners"]:
        return await update.message.reply_text("✔️ Ты уже владелец этого ключа.")
    entry["owners"].append(update.effective_user.id)
    db["active"][str(update.effective_chat.id)] = secret
    save_db(db)
    await update.message.reply_text("✅ Ключ добавлен и сделан активным.")


async def cmd_setactive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("/setactivekey <key>")
    secret = ctx.args[0]
    db = load_db()
    entry = db["secrets"].get(secret)
    if not entry or not is_owner(entry, update.effective_user.id):
        return await update.message.reply_text("🚫 Нет доступа к этому ключу.")
    db["active"][str(update.effective_chat.id)] = secret
    save_db(db)
    await update.message.reply_text(f"✅ Активный: `{secret}`", parse_mode="Markdown")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    uid = update.effective_user.id
    lines = [f"`{s}` – {e['nickname']}" for s, e in db["secrets"].items() if is_owner(e, uid)]
    active = db["active"].get(str(update.effective_chat.id))
    msg = ("Твои ключи:\n" + "\n".join(lines)) if lines else "Ключей нет. /newkey создаст."
    if active:
        msg += f"\n*Активный:* `{active}`"
    await update.message.reply_text(msg, parse_mode="Markdown")

# helper resolve ------------------------------------------------------------

def resolve_secret(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    db = load_db()
    secret = ctx.args[0] if ctx.args else db["active"].get(str(update.effective_chat.id))
    entry = db["secrets"].get(secret) if secret else None
    if not entry or not is_owner(entry, update.effective_user.id):
        return None
    return secret

# rename key command --------------------------------------------------------
async def cmd_renamekey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Синтаксис: /renamekey <key> <new_name>")
    secret = ctx.args[0]
    new_name = " ".join(ctx.args[1:])[:30]
    db = load_db()
    entry = db["secrets"].get(secret)
    if not entry or not is_owner(entry, update.effective_user.id):
        return await update.message.reply_text("🚫 Нет доступа к этому ключу или ключ не найден.")
    entry["nickname"] = new_name
    save_db(db)
    await update.message.reply_text(f"✅ Название ключа `{secret}` изменено на: {new_name}", parse_mode="Markdown")

# status / buttons ----------------------------------------------------------
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    secret = resolve_secret(update, ctx)
    if not secret:
        return await update.message.reply_text("Нет доступа или активного ключа.")
    entry = load_db()["secrets"].get(secret)
    if not entry or not entry["status"]:
        return await update.message.reply_text("Нет данных от агента.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔃 Обновить", callback_data=f"status:{secret}")],
        [
            InlineKeyboardButton("🔄 Reboot", callback_data=f"reboot:{secret}"),
            InlineKeyboardButton("⏻ Shutdown", callback_data=f"shutdown:{secret}")
        ]
    ])
    await update.message.reply_text(entry["status"], parse_mode="Markdown", reply_markup=kb)


async def cb_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    action, secret = q.data.split(":", 1)
    db = load_db()
    entry = db["secrets"].get(secret)
    if not entry or not is_owner(entry, q.from_user.id):
        return await q.edit_message_text("🚫 Нет доступа.")

    # ────────────── handle inline callback actions ──────────────
    if action == "status":
        if not entry["status"]:
            return await q.edit_message_text("Нет данных от агента.")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔃 Обновить", callback_data=f"status:{secret}")],
            [
                InlineKeyboardButton("🔄 Reboot", callback_data=f"reboot:{secret}"),
                InlineKeyboardButton("⏻ Shutdown", callback_data=f"shutdown:{secret}")
            ]
        ])
        return await q.edit_message_text(entry["status"], parse_mode="Markdown", reply_markup=kb)

    elif action in {"reboot", "shutdown"}:
        entry.setdefault("pending", []).append(action)
        save_db(db)
        return await q.edit_message_text(f"☑️ *{action}* поставлена в очередь.", parse_mode="Markdown")

# ────────────────────────── FastAPI for agents ─────────────────────────────
app = FastAPI()

class StatusPayload(BaseModel):
    text: str

@app.post("/api/push/{secret}")
async def push(secret: str, payload: StatusPayload):
    db = load_db()
    if secret not in db["secrets"]:
        raise HTTPException(404)
    db["secrets"][secret]["status"] = payload.text
    save_db(db)
    return {"ok": True}

@app.get("/api/pull/{secret}")
async def pull(secret: str):
    db = load_db()
    if secret not in db["secrets"]:
        raise HTTPException(404)
    cmds = db["secrets"][secret].get("pending", [])
    db["secrets"][secret]["pending"] = []
    save_db(db)
    return {"commands": cmds}

# ────────────────────────── Bootstrap ──────────────────────────────────────

def start_uvicorn():
    """Run uvicorn, with TLS if certs are available."""
    kwargs = dict(host="0.0.0.0", port=API_PORT, log_level="info")
    if CERT_FILE.exists() and KEY_FILE.exists():
        kwargs.update(ssl_certfile=str(CERT_FILE), ssl_keyfile=str(KEY_FILE))
        log.info("🔐 TLS enabled (%s, %s)", CERT_FILE, KEY_FILE)
    else:
        log.warning("⚠️  TLS disabled – running over plain HTTP.")
    uvicorn.run(app, **kwargs)


def main():
    threading.Thread(target=start_uvicorn, daemon=True).start()
    log.info("🌐 FastAPI on %s%s", API_PORT, " (TLS)" if CERT_FILE.exists() else "")

    app_tg = ApplicationBuilder().token(TOKEN).build()
    app_tg.add_handler(CommandHandler(["start", "help"], cmd_start))
    app_tg.add_handler(CommandHandler("newkey", cmd_newkey))
    app_tg.add_handler(CommandHandler("linkkey", cmd_linkkey))
    app_tg.add_handler(CommandHandler("setactivekey", cmd_setactive))
    app_tg.add_handler(CommandHandler("list", cmd_list))
    app_tg.add_handler(CommandHandler("status", cmd_status))
    app_tg.add_handler(CommandHandler("renamekey", cmd_renamekey))
    app_tg.add_handler(CallbackQueryHandler(cb_action))

    log.info("🤖 Polling…")
    app_tg.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bye.")
