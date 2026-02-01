import psycopg2
import os
import asyncio
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
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
    c.execute("""
    CREATE TABLE IF NOT EXISTS pending_transactions (
        id SERIAL PRIMARY KEY,
        from_user_id BIGINT,
        from_user_name TEXT,
        to_user_id BIGINT,
        chat_id BIGINT,
        amount DECIMAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            InlineKeyboardButton("Proxy Add", callback_data="proxy_start"),
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
    
    # Get pending transactions
    c.execute("""
        SELECT to_user_id, SUM(amount)
        FROM pending_transactions
        WHERE chat_id = %s
        GROUP BY to_user_id
    """, (chat_id,))
    pending = dict(c.fetchall())
    
    c.close()
    conn.close()

    if not rows:
        return "Swear Jar\n\nNo swears yet 😇"

    text = "Swear Jar\n\n"
    for name, amount in rows:
        pending_text = ""
        if rows[0][1]:  # If there are any pending for this user
            # Find pending for this specific user by checking their name
            c = get_db_connection().cursor()
            c.execute("""
                SELECT telegram_id FROM balances WHERE name = %s AND chat_id = %s
            """, (name, chat_id))
            result = c.fetchone()
            if result:
                user_id = result[0]
                if user_id in pending:
                    pending_text = f" + (${pending[user_id]} pending)"
            c.close()
        
        text += f"{name}: ${amount}{pending_text}\n"
    return text

# ======================
# COMMANDS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Run once to create & pin the swear jar message
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check for pending transactions for this user
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, from_user_name, amount
        FROM pending_transactions
        WHERE to_user_id = %s AND chat_id = %s
        ORDER BY created_at
    """, (user_id, chat_id))
    pending = c.fetchall()
    c.close()
    conn.close()
    
    # If user has pending transactions, show them first
    if pending:
        pending_text = "You have pending swears to confirm:\n\n"
        for trans_id, from_user_name, amount in pending:
            pending_text += f"{from_user_name} wants to add ${amount}\n"
        
        buttons = []
        for trans_id, from_user_name, amount in pending:
            buttons.append([
                InlineKeyboardButton(f"Accept ${amount} from {from_user_name}", callback_data=f"confirm_pending_{trans_id}"),
                InlineKeyboardButton("Reject", callback_data=f"reject_pending_{trans_id}")
            ])
        buttons.append([InlineKeyboardButton("Back to Scoreboard", callback_data="back_to_scoreboard")])
        
        await update.message.reply_text(
            pending_text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    
    # Normal flow - show scoreboard
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

    # Handle proxy add start - show user selection
    if query.data == "proxy_start":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT telegram_id, name
            FROM balances
            WHERE chat_id = %s AND telegram_id != %s
            ORDER BY name
        """, (chat_id, user.id))
        users = c.fetchall()
        c.close()
        conn.close()
        
        if not users:
            await query.answer("No other users in this chat yet!", show_alert=True)
            return
        
        # Create keyboard with user options
        buttons = [[InlineKeyboardButton(name, callback_data=f"proxy_select_{uid}")] for uid, name in users]
        buttons.append([InlineKeyboardButton("Cancel", callback_data="proxy_cancel")])
        keyboard = InlineKeyboardMarkup(buttons)
        
        await query.edit_message_text(
            text="Select who to add swears for:",
            reply_markup=keyboard
        )
        return
    
    # Handle user selection in proxy add
    if query.data.startswith("proxy_select_"):
        to_user_id = int(query.data.split("_")[2])
        # Store in context for next step
        context.user_data['proxy_to_user_id'] = to_user_id
        
        # Get the name
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM balances WHERE telegram_id = %s", (to_user_id,))
        result = c.fetchone()
        to_user_name = result[0] if result else "Unknown"
        c.close()
        conn.close()
        
        # Show input prompt
        await query.edit_message_text(
            text=f"How many swears for {to_user_name}?\n\nReply with a number (e.g., 5 for $0.25)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="proxy_cancel")]])
        )
        
        # Enable message handler for input
        context.user_data['awaiting_proxy_amount'] = True
        return
    
    # Handle proxy cancel
    if query.data == "proxy_cancel":
        context.user_data.pop('proxy_to_user_id', None)
        context.user_data.pop('awaiting_proxy_amount', None)
        await query.edit_message_text(
            text=get_scoreboard(chat_id),
            reply_markup=get_keyboard()
        )
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

    # Handle pending confirmation
    if query.data.startswith("confirm_pending_"):
        transaction_id = int(query.data.split("_")[2])
        conn = get_db_connection()
        c = conn.cursor()
        
        # Get the pending transaction
        c.execute("""
            SELECT from_user_id, from_user_name, to_user_id, amount
            FROM pending_transactions
            WHERE id = %s AND to_user_id = %s AND chat_id = %s
        """, (transaction_id, user.id, chat_id))
        result = c.fetchone()
        
        if not result:
            c.close()
            conn.close()
            await query.answer("Transaction not found or already processed", show_alert=True)
            return
        
        from_user_id, from_user_name, to_user_id, amount = result
        
        # Update balance
        c.execute("""
            INSERT INTO balances (telegram_id, chat_id, name, amount)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_id, chat_id) DO UPDATE
            SET amount = balances.amount + EXCLUDED.amount
        """, (to_user_id, chat_id, user.first_name, amount))
        
        # Delete the pending transaction
        c.execute("DELETE FROM pending_transactions WHERE id = %s", (transaction_id,))
        
        conn.commit()
        c.close()
        conn.close()
        
        await query.edit_message_text(
            text=get_scoreboard(chat_id),
            reply_markup=get_keyboard()
        )
        return

    # Handle reject pending
    if query.data.startswith("reject_pending_"):
        transaction_id = int(query.data.split("_")[2])
        conn = get_db_connection()
        c = conn.cursor()
        
        # Delete the pending transaction
        c.execute("""
            DELETE FROM pending_transactions
            WHERE id = %s AND to_user_id = %s AND chat_id = %s
        """, (transaction_id, user.id, chat_id))
        
        conn.commit()
        c.close()
        conn.close()
        
        await query.edit_message_text(
            text=get_scoreboard(chat_id),
            reply_markup=get_keyboard()
        )
        return

    # Handle back to scoreboard
    if query.data == "back_to_scoreboard":
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages for proxy add amount input"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Restrict users if configured
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        return
    
    # Check if we're waiting for proxy amount input
    if not context.user_data.get('awaiting_proxy_amount'):
        return
    
    to_user_id = context.user_data.get('proxy_to_user_id')
    if not to_user_id:
        return
    
    try:
        # Parse the amount (number of swears)
        swears = int(update.message.text)
        if swears <= 0:
            await update.message.reply_text("Please enter a positive number!")
            return
        
        amount = swears * 0.05
        
        # Get the recipient's name
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM balances WHERE telegram_id = %s", (to_user_id,))
        result = c.fetchone()
        to_user_name = result[0] if result else "Unknown"
        
        # Create pending transaction
        c.execute("""
            INSERT INTO pending_transactions (from_user_id, from_user_name, to_user_id, chat_id, amount)
            VALUES (%s, %s, %s, %s, %s)
        """, (user.id, user.first_name, to_user_id, chat_id, amount))
        
        conn.commit()
        c.close()
        conn.close()
        
        # Get the message ID from reply_to_message to update the scoreboard
        message_id_to_update = None
        if update.message.reply_to_message:
            message_id_to_update = update.message.reply_to_message.message_id
        
        # Clear context
        context.user_data.pop('proxy_to_user_id', None)
        context.user_data.pop('awaiting_proxy_amount', None)
        
        # Update the original message with the scoreboard
        if message_id_to_update:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id_to_update,
                    text=get_scoreboard(chat_id),
                    reply_markup=get_keyboard()
                )
            except:
                pass
        
        # Send temporary confirmation message that deletes after 5 seconds
        confirmation_msg = await update.message.reply_text(
            f"Added {swears} swears (${amount}) pending for {to_user_name}"
        )
        await asyncio.sleep(5)
        await confirmation_msg.delete()
        
    except ValueError:
        await update.message.reply_text("Please enter a valid number!")
        return

# ======================
# MAIN
# ======================
def main():
    init_db()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Swear Jar Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
