import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = set()

# Optional security: only allow specific Telegram user IDs
_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw:
    for x in _raw.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # if you didn't set allowlist yet, allow everyone (we'll lock it later)
    return update.effective_user and update.effective_user.id in ALLOWED_USER_IDS

async def guard(update: Update) -> bool:
    if not is_allowed(update):
        await update.message.reply_text("Not authorized.")
        return False
    return True

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(
        "Norah Ops Bot ✅\n\n"
        "/daily - Owner daily sales summary\n"
        "/shift - Manager shift brief\n"
        "/covers - Today reservations overview\n"
        "/notes - Notes (coming)\n"
        "/help - Show commands"
    )

async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(
        "📊 Norah Daily Summary (MVP)\n"
        "- Sales: pending\n"
        "- Covers: pending\n"
        "- Avg ticket: pending\n"
        "- Issues: none logged\n\n"
        "Next step: we’ll connect Agora/CoverManager or allow manual input."
    )

async def shift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(
        "👥 Shift Brief (MVP)\n"
        "1) Confirm reservations + walk-in plan\n"
        "2) Music: daylist 20:00–22:00 then night shift\n"
        "3) Bathrooms check every 30–40 min\n"
        "4) Service room doors closed at all times\n"
    )

async def covers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text("📌 Covers (MVP): pending (we’ll connect CoverManager next).")

async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text("🗒️ Notes (MVP): coming next.")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN env var")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler("shift", shift_cmd))
    app.add_handler(CommandHandler("covers", covers_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))

    # POLLING = simplest to deploy (no webhook URL needed)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
