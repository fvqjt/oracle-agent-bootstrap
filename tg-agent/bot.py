#!/usr/bin/env python3
"""TG Agent — Runner Bot.

Runs inside the Runner VM.  Polls Telegram directly, executes commands
locally via subprocess, sends results back to Telegram.  Reports bot
status (busy/active/idle) to Oracle via the oracle-agent report endpoint.
"""
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    IDLE_TIMEOUT_SEC,
    STATUS_REPORT_INTERVAL_SEC,
    EXEC_TIMEOUT_SEC,
    TG_BOT_POLLING,
    TG_SEND_FILE_LIMIT_MB,
    TG_RECV_FILE_LIMIT_MB,
    AGENT_DIR,
)
from state_mgr import StateManager
from status_reporter import StatusReporter

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tg.bot")

state_mgr = StateManager(idle_threshold_sec=IDLE_TIMEOUT_SEC)
status_reporter = StatusReporter(state_mgr, AGENT_DIR, STATUS_REPORT_INTERVAL_SEC)

# chat_id -> dest_path, for pending /upload operations
pending_uploads: dict[int, str] = {}

HELP_TEXT = (
    "🤖 TG Agent (Runner Bot)\n\n"
    "/exec <command> — 执行 Shell 命令\n"
    "/download <path> — 下载文件\n"
    "/upload <dest_path> — 上传文件（随后发送文件附件）\n"
    "/status — 查看 Bot 状态\n"
    "/help — 显示此帮助\n"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── output sending ──────────────────────────────────────────────

async def send_output(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    """Send text in ≤4000-char chunks wrapped in Markdown code blocks."""
    MAX_LEN = 4000
    if not text:
        text = "(no output)"
    if len(text) <= MAX_LEN:
        await context.bot.send_message(chat_id, f"```\n{text}\n```",
                                       parse_mode="Markdown")
        return
    chunks = []
    i = 0
    while i < len(text):
        chunk = text[i:i + MAX_LEN]
        if i + MAX_LEN < len(text):
            last_nl = chunk.rfind("\n")
            if last_nl > MAX_LEN * 0.5:
                chunk = text[i:i + last_nl + 1]
        chunks.append(chunk)
        i += len(chunk)
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        msg = f"```\n{chunk}\n```"
        if total > 1:
            msg += f"\n[{idx}/{total}]"
        await context.bot.send_message(chat_id, msg, parse_mode="Markdown")


# ── command handlers ────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — same as /help."""
    await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_mgr.on_user_interaction()
    await update.message.reply_text(HELP_TEXT)
    state_mgr.on_user_interaction()


async def cmd_exec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_mgr.on_user_interaction()
    user_input = update.message.text.partition(" ")[2].strip()
    if not user_input:
        await update.message.reply_text("用法: /exec <command>")
        return

    placeholder = await update.message.reply_text("⏳ 正在执行...")

    state_mgr.task_start()
    try:
        proc = subprocess.run(
            ["bash", "-c", user_input],
            capture_output=True, text=True, timeout=EXEC_TIMEOUT_SEC,
        )
        output = proc.stdout
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        if proc.returncode != 0:
            output += f"\n[exit code: {proc.returncode}]"
    except subprocess.TimeoutExpired:
        output = f"⏱ 命令超时（{EXEC_TIMEOUT_SEC}s），已终止。"
    except Exception as e:
        output = f"❌ 执行失败: {e}"
    finally:
        state_mgr.task_end()

    state_mgr.on_user_interaction()
    try:
        await context.bot.edit_message_text(
            chat_id=placeholder.chat_id,
            message_id=placeholder.message_id,
            text="✅ 执行完成",
        )
    except Exception:
        pass
    await send_output(context, update.effective_chat.id, output)


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_mgr.on_user_interaction()
    path = update.message.text.partition(" ")[2].strip()
    if not path:
        await update.message.reply_text("用法: /download <file_path>")
        return

    if not os.path.exists(path):
        await update.message.reply_text(f"❌ 文件不存在: {path}")
        return
    if os.path.isdir(path):
        await update.message.reply_text(f"❌ 是目录，不是文件: {path}")
        return

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > TG_SEND_FILE_LIMIT_MB:
        await update.message.reply_text(
            f"❌ 文件过大: {size_mb:.1f} MB，上限 {TG_SEND_FILE_LIMIT_MB} MB"
        )
        return

    try:
        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                caption=f"📄 {path} ({size_mb:.1f} MB)",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ 发送失败: {e}")
    state_mgr.on_user_interaction()


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_mgr.on_user_interaction()
    dest_path = update.message.text.partition(" ")[2].strip()
    if not dest_path:
        await update.message.reply_text("用法: /upload <dest_path>\n随后发送文件附件。")
        return
    pending_uploads[update.effective_chat.id] = dest_path
    await update.message.reply_text(f"📎 请发送文件，将保存到 Runner 的 `{dest_path}`",
                                    parse_mode="Markdown")
    state_mgr.on_user_interaction()


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_mgr.on_user_interaction()
    chat_id = update.effective_chat.id
    if chat_id not in pending_uploads:
        await update.message.reply_text("请先 /upload <path> 指定目标路径。")
        return
    dest_path = pending_uploads.pop(chat_id)

    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ 未检测到文件附件。")
        return

    size_mb = (doc.file_size or 0) / (1024 * 1024)
    if size_mb > TG_RECV_FILE_LIMIT_MB:
        await update.message.reply_text(
            f"❌ 文件过大: {size_mb:.1f} MB，上限 {TG_RECV_FILE_LIMIT_MB} MB"
        )
        return

    placeholder = await update.message.reply_text("⬇️ 正在下载到 Runner...")

    state_mgr.task_start()
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=dest_path)
        await context.bot.edit_message_text(
            chat_id=placeholder.chat_id,
            message_id=placeholder.message_id,
            text=f"✅ 文件已保存到 `{dest_path}` ({size_mb:.1f} MB)",
            parse_mode="Markdown",
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=placeholder.chat_id,
            message_id=placeholder.message_id,
            text=f"❌ 下载失败: {e}",
        )
    finally:
        state_mgr.task_end()
    state_mgr.on_user_interaction()


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_mgr.on_user_interaction()
    state = state_mgr.current_state()
    idle = state_mgr.idle_seconds()
    info = (
        f"🤖 Runner Bot 状态\n\n"
        f"状态: {state.value}\n"
        f"活跃任务: {state_mgr.active_tasks}\n"
        f"空闲时间: {idle:.0f}s\n"
        f"空闲阈值: {IDLE_TIMEOUT_SEC}s\n"
        f"执行超时: {EXEC_TIMEOUT_SEC}s\n"
        f"当前时间: {_now()}\n"
    )
    await update.message.reply_text(info)
    state_mgr.on_user_interaction()


# ── lifecycle hooks ─────────────────────────────────────────────

async def on_startup(application: Application):
    log.info("Runner Bot starting up")
    # Start status reporter background task
    asyncio.create_task(status_reporter.run_forever())
    # Send initial status report
    await status_reporter.report_once()


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("exec", cmd_exec))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))


    log.info("Starting Runner Bot (idle_timeout=%ds, report_interval=%ds, bot_polling=%s)",
             IDLE_TIMEOUT_SEC, STATUS_REPORT_INTERVAL_SEC, TG_BOT_POLLING)

    if (TG_BOT_POLLING or "1") == "1":
        # Legacy mode: this Bot owns the getUpdates lock and serves the user directly
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        # Centralized mode: Oracle (dispatcher) owns getUpdates; this Runner is a
        # pure exec agent (commands arrive via the oracle-agent task channel).
        # Keep Application lifecycle alive so on_startup (status_reporter) runs.
        log.warning("TG polling DISABLED — centralized mode (no getUpdates lock held)")

        async def _keepalive():
            await app.initialize()
            try:
                await app.start()
                while True:
                    await asyncio.sleep(3600)
            finally:
                await app.stop()
                await app.shutdown()

        asyncio.run(_keepalive())


if __name__ == "__main__":
    main()
