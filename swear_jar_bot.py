import psycopg2
import os
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# ======================
# CONFIG
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Optional: restrict to only you two
# Leave empty {} to allow anyone
ALLOWED_USERS = {
    1702020451,  # Jun Onn
    468551427   # Natalie
}

# ======================
# DATABASE SETUP
# ======================
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable not set")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS balances (
        telegram_id BIGINT,
        chat_id BIGINT,
        name TEXT,
        amount DECIMAL DEFAULT 0,
        PRIMARY KEY (telegram_id, chat_id)
    )
    """)
    conn.commit()
    c.close()
    conn.close()

# ======================
# UI HELPERS
# ======================
def get_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕", callback_data="plus"),
            InlineKeyboardButton("➖", callback_data="minus")
        ],
        [
            InlineKeyboardButton("Settle Up!", callback_data="settle")
        ]
    ])

def get_scoreboard(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT name, amount
        FROM balances
        WHERE chat_id = %s
        ORDER BY amount DESC
    """, (chat_id,))
    rows = c.fetchall()
    c.close()
    conn.close()

    if not rows:
        return "Swear Jar\n\nNo swears yet 😇"

    text = "Swear Jar\n\n"
    for name, amount in rows:
        text += f"{name}: ${amount}\n"
    return text

# ======================
# COMMANDS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Run once to create & pin the swear jar message
    """
    chat_id = update.effective_chat.id
    message = await update.message.reply_text(
        get_scoreboard(chat_id),
        reply_markup=get_keyboard()
    )

    # Try to pin the message (fails silently if no permission)
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=message.message_id
        )
    except:
        pass

# ======================
# BUTTON HANDLER
# ======================
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    chat_id = query.message.chat_id

    # Restrict users if configured
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        await query.answer("Not authorized", show_alert=True)
        return

    # Handle settle up confirmation
    if query.data == "settle":
        confirm_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Yes, Settle Up", callback_data="settle_confirm"),
                InlineKeyboardButton("Cancel", callback_data="settle_cancel")
            ]
        ])
        await query.edit_message_text(
            text=f"{get_scoreboard(chat_id)}\n\n{user.first_name}, reset your balance to $0?",
            reply_markup=confirm_keyboard
        )
        return

    # Handle settle up confirmation
    if query.data == "settle_confirm":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            UPDATE balances
            SET amount = 0
            WHERE telegram_id = %s AND chat_id = %s
        """, (user.id, chat_id))
        conn.commit()
        c.close()
        conn.close()
        await query.edit_message_text(
            text=get_scoreboard(chat_id),
            reply_markup=get_keyboard()
        )
        return

    # Handle cancel
    if query.data == "settle_cancel":
        await query.edit_message_text(
            text=get_scoreboard(chat_id),
            reply_markup=get_keyboard()
        )
        return

    # Handle +/- buttons
    delta = 0.05 if query.data == "plus" else -0.05

    conn = get_db_connection()
    c = conn.cursor()

    # Ensure user exists
    c.execute("""
        INSERT INTO balances (telegram_id, chat_id, name, amount)
        VALUES (%s, %s, %s, 0)
        ON CONFLICT (telegram_id, chat_id) DO NOTHING
    """, (user.id, chat_id, user.first_name))

    # Update balance (never below 0)
    c.execute("""
        UPDATE balances
        SET amount = GREATEST(amount + %s, 0)
        WHERE telegram_id = %s AND chat_id = %s
    """, (delta, user.id, chat_id))

    conn.commit()
    c.close()
    conn.close()

    # Update the same message
    await query.edit_message_text(
        text=get_scoreboard(chat_id),
        reply_markup=get_keyboard()
    )

# ======================
# MAIN
# ======================
def main():
    init_db()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))

    print("Swear Jar Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
