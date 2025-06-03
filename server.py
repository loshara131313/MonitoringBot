from __future__ import annotations

"""remote_bot_server"""
import asyncio
import io
import json
import logging
import os
import re
import secrets
from html import escape
from telegram.constants import ParseMode
import time
import sqlite3
import string
from telegram import InputFile
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
import numpy as np
from statistics import median
from datetime import datetime, timedelta
from typing import Any, Dict, List

import matplotlib
import matplotlib.pyplot as plt
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ────────────────────────── CONFIG ─────────────────────────────────────────
ENV_FILE = Path(".env")
DB_FILE = Path("db.json")
METRIC_DB = Path("metrics.sqlite")
API_PORT = int(os.getenv("PORT", "8000"))

CERT_FILE = Path(os.getenv("SSL_CERT", "cert.pem"))
KEY_FILE = Path(os.getenv("SSL_KEY", "key.pem"))

# matplotlib без X-сервера
matplotlib.use("Agg")

# ────────────────────────── helpers ────────────────────────────────────────

UPTIME_RE = re.compile(r"Uptime:\s*([^\n]+)")

UNIT_NAMES = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]

def human_bytes(num: float) -> str:
    for unit in UNIT_NAMES:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} EiB"

def disk_bar(p: float, length=10) -> str:
    filled = int(round(p * length / 100))
    return "█" * filled + "░" * (length - filled)

def build_status(p: MetricsPayload) -> str:
    lines = [
        "💻 *PC stats*",
        f"⏳ Uptime: {timedelta(seconds=int(p.uptime))}",
        "*━━━━━━━━━━━CPU━━━━━━━━━━━*",
        f"🖥️ CPU: {p.cpu:.1f}%",
    ]
    if p.cpu_temp is not None:
        lines.append(f"🌡️ CPU Temp: {p.cpu_temp:.1f} °C")
    lines.extend([
        "*━━━━━━━━━━━RAM━━━━━━━━━━━*",
        f"🧠 RAM: {human_bytes(p.ram_used)} / {human_bytes(p.ram_total)} ({p.ram:.1f}%)",
        f"🧠 SWAP: {human_bytes(p.swap_used)} / {human_bytes(p.swap_total)} ({p.swap:.1f}%)",
    ])
    if p.gpu is not None:
        lines.extend([
            "*━━━━━━━━━━━GPU━━━━━━━━━━━*",
            f"🎮 GPU: {p.gpu:.1f}%",
        ])
        if p.vram_used is not None and p.vram_total is not None:
            lines.append(
                f"🗄️ VRAM: {p.vram_used:.0f} / {p.vram_total:.0f} MiB ({p.vram:.1f}%)"
            )
        if p.gpu_temp is not None:
            lines.append(f"🌡️ GPU Temp: {p.gpu_temp:.0f} °C")
    if p.disks:
        lines.append("*━━━━━━━━━━━DISKS━━━━━━━━━━*")
        for d in p.disks:
            mp = d.get("mount") or d.get("mountpoint")
            percent = d.get("percent", 0.0)
            used = d.get("used", 0.0)
            total = d.get("total", 0.0)
            bar = disk_bar(percent)
            warn = "❗" if percent >= 90 else ""
            lines.append(
                f"💾 {mp}: {bar} {percent:.0f}% ({human_bytes(used)} / {human_bytes(total)}){warn}"
            )
    return "\n".join(lines)

def _load_dotenv() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

def _ensure_ssl() -> None:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    logging.info("🔒 Generating self-signed TLS certificate…")
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
                "-out",
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
        logging.info("✅ Certificate created.")
    except Exception as exc:
        logging.warning("⚠️  TLS cert generation failed: %s", exc)

_load_dotenv()
_ensure_ssl()

TOKEN = os.getenv("BOT_TOKEN") or input("Enter Telegram BOT_TOKEN: ").strip()
if not TOKEN:
    print("❌ BOT_TOKEN required.")
    sys.exit(1)
if "BOT_TOKEN" not in os.environ:
    ENV_FILE.write_text(
        (ENV_FILE.read_text() if ENV_FILE.exists() else "") + f"BOT_TOKEN={TOKEN}\n"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("remote-bot")
async def check_speedtest_done(ctx: ContextTypes.DEFAULT_TYPE):
    job  = ctx.job
    data = job.data

    secret  = data["secret"]
    chat_id = data["chat_id"]
    msg_id  = data["msg_id"]

    entry = load_db()["secrets"].get(secret, {})

    if "speedtest" in entry.get("pending", []):
        return

    status: str = entry.get("status") or ""
    if "Speedtest" not in status:

        start_ts = data.setdefault("start_ts", time.time())
        TIMEOUT  = 3 * 60
        if time.time() - start_ts > TIMEOUT:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="⚠️  Speedtest занял много времени и был прерван.",
            )
            job.schedule_removal()
        return

    # ─── 3) Результат получен – выкладываем и выходим ───────────────────────────
    await ctx.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=status,
        parse_mode="Markdown",
    )
    job.schedule_removal()

# ───────────────────── SQLite helpers ──────────────────────────────────────
def _init_metric_db() -> sqlite3.Connection:
    con = sqlite3.connect(METRIC_DB, check_same_thread=False, isolation_level=None)
    con.execute(
        """CREATE TABLE IF NOT EXISTS metrics(
               secret    TEXT,
               ts        INTEGER,
               cpu       REAL,
               ram       REAL,
               gpu       REAL,
               vram      REAL,
               cpu_temp  REAL,
               uptime    REAL
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_metrics_secret_ts ON metrics(secret, ts)"
    )
    return con

sql = _init_metric_db()

def record_metric(secret: str, cpu: float, ram: float,
                  gpu: float | None, vram: float | None,
                  cpu_temp: float | None, uptime: float | None):
    sql.execute(
        "INSERT INTO metrics(secret, ts, cpu, ram, gpu, vram, cpu_temp, uptime) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (secret, int(time.time()), cpu, ram, gpu, vram, cpu_temp, uptime),
    )

def fetch_metrics(secret: str, since: int) -> List[tuple[int, float]]:
    rows = sql.execute(
        "SELECT ts, cpu, ram, gpu, vram FROM metrics WHERE secret=? AND ts>=? "
        "ORDER BY ts ASC",
        (secret, since),
    ).fetchall()
    return rows

def group_30s(rows: List[tuple[int, float]]) -> List[tuple[int, float, float, float, float]]:
    buckets: Dict[int, list] = {}
    for r in rows:
        ts = r[0] - (r[0] % 30)
        buckets.setdefault(ts, []).append(r)
    grouped = []
    for ts in sorted(buckets):
        vals = buckets[ts]
        n = len(vals)
        cpu = sum(v[1] for v in vals) / n
        ram = sum(v[2] for v in vals) / n
        gpu = (
            sum(v[3] for v in vals if v[3] is not None) / n
            if any(v[3] is not None for v in vals)
            else None
        )
        vram = (
            sum(v[4] for v in vals if v[4] is not None) / n
            if any(v[4] is not None for v in vals)
            else None
        )
        grouped.append((ts, cpu, ram, gpu, vram))
    return grouped

# ──────────────────────── JSON DB helpers ──────────────────────────────────
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

# ───────────────────────- Telegram command handlers ────────────────────────
OWNER_HELP = (
    "Команды:\n"
    "/newkey <имя> – создать ключ.\n"
    "/linkkey <ключ> – подписаться.\n"
    "/set <ключ> – сделать активным.\n"
    "/list – показать ключи.\n"
    "/status – статус + кнопки.\n"
    "/renamekey <ключ> <имя> – переименовать."
)
def gen_secret(n: int = 20):
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

def is_owner(entry: Dict[str, Any], user_id: int) -> bool:
    return user_id in entry.get("owners", [])

# ───────────────────────- UI helpers ───────────────────────────────────────
def status_keyboard(secret: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔢 Список", callback_data=f"list"),
             InlineKeyboardButton("🔃 Обновить", callback_data=f"status:{secret}"),
        ],
            [InlineKeyboardButton("📊 Все", callback_data=f"graph:all:{secret}")],
            [
                InlineKeyboardButton("📊 CPU", callback_data=f"graph:cpu:{secret}"),
                InlineKeyboardButton("📈 RAM", callback_data=f"graph:ram:{secret}"),
                InlineKeyboardButton("🎮 GPU",  callback_data=f"graph:gpu:{secret}"),
                InlineKeyboardButton("🗄️ VRAM", callback_data=f"graph:vram:{secret}"),
            ],
            [InlineKeyboardButton("🏎️ Speedtest", callback_data=f"speedtest:{secret}")],
            [
                InlineKeyboardButton("🔄 Reboot",   callback_data=f"reboot:{secret}"),
                InlineKeyboardButton("⏻ Shutdown", callback_data=f"shutdown:{secret}"),
            ],
        ]
    )

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот-монитор.\n" + OWNER_HELP)

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
        f"Создан секрет `{secret}` (название: {name}).", parse_mode="Markdown"
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
        return await update.message.reply_text("✔️ Уже есть доступ.")
    entry["owners"].append(update.effective_user.id)
    db["active"][str(update.effective_chat.id)] = secret
    save_db(db)
    await update.message.reply_text("✅ Ключ добавлен и сделан активным.")

async def cmd_setactive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("/set <key>")
    secret = ctx.args[0]
    db = load_db()
    entry = db["secrets"].get(secret)
    if not entry or not is_owner(entry, update.effective_user.id):
        return await update.message.reply_text("🚫 Нет доступа.")
    db["active"][str(update.effective_chat.id)] = secret
    save_db(db)
    await update.message.reply_text(f"✅ Активный: `{secret}`", parse_mode="Markdown")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db   = load_db()
    uid  = update.effective_user.id
    now  = int(time.time())

    rows = []
    for secret, entry in db["secrets"].items():
        if not is_owner(entry, uid):
            continue

        name = entry.get("nickname") or secret

        row = sql.execute(
            "SELECT ts, cpu, ram FROM metrics "
            "WHERE secret=? ORDER BY ts DESC LIMIT 1",
            (secret,),
        ).fetchone()

        if row:
            ts, cpu, ram = row
            fresh = (now - ts) < 300
            info  = f"🖥️{cpu:.0f}% CPU, 🧠{ram:.0f}% RAM"
        else:
            fresh = False
            info  = "нет данных"

        uptime = "-"
        if entry.get("status"):
            m = UPTIME_RE.search(entry["status"])
            if m:
                uptime = m.group(1)

        marker = " <b>❗️ДАННЫЕ УСТАРЕЛИ❗</b>" if not fresh else ""
        rows.append(
            f"<b>{escape(name)}</b> – <code>{escape(secret)}</code>"
            f"\n• {info}, ⏳ {escape(uptime)}{marker}"
            f"\n"
        )

    buttons = [
        InlineKeyboardButton(
            entry.get("nickname") or s,
            callback_data=f"status:{s}",
        )
        for s, entry in list(db["secrets"].items())[:12]
        if is_owner(entry, uid)
    ]
    keyboard = InlineKeyboardMarkup([buttons[i:i + 4] for i in range(0, len(buttons), 4)])

    active = db["active"].get(str(update.effective_chat.id))
    head   = "Твои ключи:" if rows else "Ключей нет. /newkey создаст."
    if active:
        head += f"\n<b>Активный:</b> <code>{escape(active)}</code>"

    await update.message.reply_text(
        head + "\n" + "\n".join(rows),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

def resolve_secret(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    db = load_db()
    secret = ctx.args[0] if ctx.args else db["active"].get(str(update.effective_chat.id))
    entry = db["secrets"].get(secret) if secret else None
    if not entry or not is_owner(entry, update.effective_user.id):
        return None
    return secret

async def cmd_renamekey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Синтаксис: /renamekey <key> <new_name>")
    secret, new_name = ctx.args[0], " ".join(ctx.args[1:])[:30]
    db = load_db()
    entry = db["secrets"].get(secret)
    if not entry or not is_owner(entry, update.effective_user.id):
        return await update.message.reply_text("🚫 Нет доступа.")
    entry["nickname"] = new_name
    save_db(db)
    await update.message.reply_text(f"✅ `{secret}` → {new_name}", parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    secret = resolve_secret(update, ctx)
    if not secret:
        return await update.message.reply_text("Нет доступа или активного ключа.")
    entry = load_db()["secrets"].get(secret)
    if not entry or not entry["status"]:
        return await update.message.reply_text("Нет данных от агента.")
    await update.message.reply_text(
        entry["status"], parse_mode="Markdown", reply_markup=status_keyboard(secret)
    )

# ───────────────────- Plot helpers ─────────────────────────────────────────
def _find_gaps(ts, factor: float = 2.0):
    if len(ts) < 2:
        return [(0, len(ts) - 1)], [], 0

    intervals = [(ts[i] - ts[i - 1]).total_seconds() for i in range(1, len(ts))]
    med = median(intervals) if intervals else 0
    if med <= 0:
        med = max(intervals) if intervals else 60  # fallback 60 s
    thr = med * factor

    segments, gaps = [], []
    start = 0
    for i, dt in enumerate(intervals, start=1):
        if dt > thr:
            segments.append((start, i - 1))
            gaps.append((ts[i - 1], ts[i]))
            start = i
    segments.append((start, len(ts) - 1))
    return segments, gaps, thr


def _plot_segments(ax, ts, ys, segments, *args, **kwargs):
    first = True
    col = None
    for s, e in segments:
        if first:
            line, = ax.plot(ts[s:e+1], ys[s:e+1], *args, **kwargs)
            col = line.get_color()
            first = False
        else:
            kw = dict(kwargs)
            kw.pop("label", None)
            kw["color"] = col
            ax.plot(ts[s:e+1], ys[s:e+1], *args, **kw)
def _make_figure(seconds: int):
    long_span = seconds >= 86_400  # ≥ 1 day
    dpi = 500 if long_span else 150
    fig, ax = plt.subplots(figsize=(12, 6), dpi=dpi)

    base = 9
    plt.rcParams.update({
        "font.size": base,
        "axes.titlesize": base + 4,
        "axes.labelsize": base + 2,
        "xtick.labelsize": base - 1,
        "ytick.labelsize": base - 1,
        "legend.fontsize": base - 1,
    })
    return fig, ax
def plot_metric(secret: str, metric: str, seconds: int):
    rows = fetch_metrics(secret, int(time.time()) - seconds)
    if not rows:
        return None

    rows = group_30s(rows)
    ts = [datetime.fromtimestamp(r[0]) for r in rows]

    idx_map = {
        "cpu": (1, "CPU %"),
        "ram": (2, "RAM %"),
        "gpu": (3, "GPU %"),
        "vram": (4, "VRAM %"),
    }
    col_idx, label = idx_map[metric]
    ys = [np.nan if rows[i][col_idx] is None else rows[i][col_idx] for i in range(len(rows))]

    segments, gaps, _ = _find_gaps(ts)

    plt.style.use("dark_background")
    fig, ax = _make_figure(seconds)

    _plot_segments(ax, ts, ys, segments, linewidth=1.5)

    for g0, g1 in gaps:
        ax.axvspan(g0, g1, facecolor="none", hatch="//", edgecolor="white", alpha=0.3, linewidth=0)

    ax.set_title(f"{label} за {timedelta(seconds=seconds)}")
    ax.set_xlabel("Время")
    ax.set_ylabel("%")
    ax.set_ylim(0, 100)
    ax.grid(True, linestyle="--", linewidth=0.3)
    fig.autofmt_xdate()

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, dpi=fig.dpi, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf
def plot_all_metrics(secret: str, seconds: int):
    rows = fetch_metrics(secret, int(time.time()) - seconds)
    if not rows:
        return None

    rows = group_30s(rows)
    ts = [datetime.fromtimestamp(r[0]) for r in rows]
    segments, gaps, _ = _find_gaps(ts)

    cpu = [r[1] for r in rows]
    ram = [r[2] for r in rows]
    gpu = [np.nan if r[3] is None else r[3] for r in rows]
    vram = [np.nan if r[4] is None else r[4] for r in rows]

    plt.style.use("dark_background")

    fig, ax = _make_figure(seconds)


    for ys, lab in ((cpu, "CPU %"), (ram, "RAM %")):
        _plot_segments(ax, ts, ys, segments, label=lab, linewidth=1.2)
    if not all(np.isnan(g) for g in gpu):
        _plot_segments(ax, ts, gpu, segments, label="GPU %", linewidth=1.2)
    if not all(np.isnan(v) for v in vram):
        _plot_segments(ax, ts, vram, segments, label="VRAM %", linewidth=1.2)

    for g0, g1 in gaps:
        ax.axvspan(g0, g1, facecolor="none", hatch="//", edgecolor="white", alpha=0.3, linewidth=0)

    ax.set_ylim(0, 100)
    ax.set_title(f"Все метрики за {timedelta(seconds=seconds)}")
    ax.set_xlabel("Время")
    ax.set_ylabel("%")
    ax.grid(True, linestyle="--", linewidth=0.3)
    ax.legend(loc="upper left", fontsize="small")
    fig.autofmt_xdate()

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, dpi=fig.dpi, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf

# ─────────────────────- Callback handler ───────────────────────────────────
async def cb_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    parts = q.data.split(":")
    action = parts[0]
    db = load_db()

    # ───── status / reboot / shutdown (старые) ─────
    if action == "status":
        secret = parts[1]
        entry = db["secrets"].get(secret)
        if not entry or not is_owner(entry, q.from_user.id):
            return await q.edit_message_text("🚫 Нет доступа.")
        if not entry["status"]:
            return await q.edit_message_text("Нет данных от агента.")
        return await q.edit_message_text(
            entry["status"], parse_mode="Markdown", reply_markup=status_keyboard(secret)
        )
    if action in {"reboot", "shutdown"}:
        secret = parts[1]
        entry = db["secrets"].get(secret)
        if not entry or not is_owner(entry, q.from_user.id):
            return await q.edit_message_text("🚫 Нет доступа.")
        entry.setdefault("pending", []).append(action)
        save_db(db)
        return await q.edit_message_text(f"☑️ *{action}* поставлена в очередь.", parse_mode="Markdown")
    if action == "list":
        uid = q.from_user.id
        now = int(time.time())
        rows = []

        for secret, entry in db["secrets"].items():
            if not is_owner(entry, uid):
                continue

            name = entry.get("nickname") or secret

            row = sql.execute(
                "SELECT ts, cpu, ram FROM metrics "
                "WHERE secret=? ORDER BY ts DESC LIMIT 1",
                (secret,),
            ).fetchone()

            if row:
                ts, cpu, ram = row
                fresh = (now - ts) < 300
                info = f"🖥️{cpu:.0f}% CPU, 🧠{ram:.0f}% RAM"
            else:
                fresh = False
                info = "нет данных"

            uptime = "-"
            if entry.get("status"):
                m = UPTIME_RE.search(entry["status"])
                if m:
                    uptime = m.group(1)

            marker = " <b>❗️ДАННЫЕ УСТАРЕЛИ❗</b>" if not fresh else ""
            rows.append(
                f"<b>{escape(name)}</b> – <code>{escape(secret)}</code>"
                f"\n• {info}, ⏳ {escape(uptime)}{marker}\n"
            )

        # те же кнопочки, но теперь они уедут в reply_markup
        buttons = [
            InlineKeyboardButton(
                entry.get("nickname") or s,
                callback_data=f"status:{s}",
            )
            for s, entry in db["secrets"].items()
            if is_owner(entry, uid)
        ]
        keyboard = InlineKeyboardMarkup(
            [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        )

        active = db["active"].get(str(q.message.chat_id))
        head = "Твои ключи:" if rows else "Ключей нет. /newkey создаст."
        if active:
            head += f"\n<b>Активный:</b> <code>{escape(active)}</code>"

        await q.edit_message_text(
            head + "\n" + "\n".join(rows),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return



    if action == "speedtest":
        secret = parts[1]
        entry = db["secrets"].get(secret)
        if not entry or not is_owner(entry, q.from_user.id):
            await q.answer("🚫 Нет доступа.", show_alert=True)
            return
        entry.setdefault("pending", []).append("speedtest")
        save_db(db)

        await q.answer()
        msg = await ctx.bot.send_message(
            chat_id=q.message.chat_id,
            text="⏳ Тестируем скорость…"
        )

        ctx.job_queue.run_repeating(
            callback=check_speedtest_done,
            interval=3,
            data={
                "secret": secret,
                "chat_id": msg.chat_id,
                "msg_id": msg.message_id,
            },
        )
        return
    # ───── graph selection ─────
    if action == "graph":
        metric = parts[1]


        if len(parts) == 3:  # graph:<metric>:<secret>
            secret = parts[2]
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("10 мин", callback_data=f"graph:{metric}:600:{secret}"),
                        InlineKeyboardButton("1 час", callback_data=f"graph:{metric}:3600:{secret}"),
                        InlineKeyboardButton("24 ч", callback_data=f"graph:{metric}:86400:{secret}"),
                        InlineKeyboardButton("7 д", callback_data=f"graph:{metric}:604800:{secret}"),
                    ],
                    [InlineKeyboardButton("◀️ Назад", callback_data=f"status:{secret}")],
                ]
            )
            return await q.edit_message_reply_markup(reply_markup=kb)


        seconds = int(parts[2])
        secret = parts[3]

        if metric == "all":
            buf = plot_all_metrics(secret, seconds)
            caption = f"Все метрики за {timedelta(seconds=seconds)}"
        else:
            buf = plot_metric(secret, metric, seconds)
            caption = f"{metric.upper()} за {timedelta(seconds=seconds)}"

        if not buf:
            return await q.edit_message_text("Данных за этот период нет.")

        caption = f"{metric.upper()} за {timedelta(seconds=seconds)}"
        if seconds >= 86400:
            doc = InputFile(buf, filename=f"{metric}_{seconds}.png")
            await ctx.bot.send_document(
                chat_id=q.message.chat_id,
                document=doc,
                caption=caption,
            )
        else:
            await ctx.bot.send_photo(
                chat_id=q.message.chat_id,
                photo=buf,
                caption=caption,
            )
        return

# ────────────────────────── FastAPI for agents ─────────────────────────────
app = FastAPI()


class MetricsPayload(BaseModel):
    cpu: float
    ram: float
    ram_used: float
    ram_total: float
    swap: float
    swap_used: float
    swap_total: float
    uptime: float
    cpu_temp: float | None = None
    gpu: float | None = None
    vram: float | None = None
    vram_used: float | None = None
    vram_total: float | None = None
    gpu_temp: float | None = None
    disks: list[dict] = []

class TextPayload(BaseModel):
    text: str

@app.post("/api/push/{secret}")
async def push(secret: str, payload: MetricsPayload):
    db = load_db()
    if secret not in db["secrets"]:
        raise HTTPException(404)
    record_metric(
        secret,
        payload.cpu,
        payload.ram,
        payload.gpu,
        payload.vram,
        payload.cpu_temp,
        payload.uptime,
    )

    db["secrets"][secret]["status"] = build_status(payload)
    save_db(db)

    return {"ok": True}

@app.post("/api/msg/{secret}")
async def push_text(secret: str, payload: TextPayload):
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
    kwargs = dict(host="0.0.0.0", port=API_PORT, log_level="info")
    if CERT_FILE.exists() and KEY_FILE.exists():
        kwargs.update(ssl_certfile=str(CERT_FILE), ssl_keyfile=str(KEY_FILE))
        log.info("🔐 TLS enabled.")
    else:
        log.warning("⚠️  TLS disabled.")
    uvicorn.run(app, **kwargs)

def main():
    threading.Thread(target=start_uvicorn, daemon=True).start()
    log.info("🌐 FastAPI on port %s", API_PORT)

    app_tg = ApplicationBuilder().token(TOKEN).build()
    app_tg.add_handler(CommandHandler(["start", "help"], cmd_start))
    app_tg.add_handler(CommandHandler("newkey", cmd_newkey))
    app_tg.add_handler(CommandHandler("linkkey", cmd_linkkey))
    app_tg.add_handler(CommandHandler("set", cmd_setactive))
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
