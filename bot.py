"""
bot.py — Main entry point.

Telegram admin commands were removed intentionally. Use the WebAdmin website as
the only admin panel.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ApplicationHandlerStop,
)

from config import BOT_TOKEN, is_admin_id
import database as db
from utils.i18n import tr

from handlers.user import (
    cmd_start, cmd_commands, cmd_support, cmd_shop, cmd_favorites, cmd_language, cmd_orders as cmd_user_orders_command,
    handle_nav_buttons, handle_language_select, handle_product_select, handle_get_order_lookup,
    handle_payment_method_select, handle_quantity_input, handle_favorite_button,
)
from handlers.payment import (
    handle_check_usdt, handle_upi_paid_button, handle_upi_text_input, handle_upi_screenshot,
    handle_check_binance, handle_binance_paid_button, handle_binance_text_input, handle_binance_screenshot,
    handle_usdt_manual_button, handle_usdt_manual_input, handle_usdt_manual_screenshot,
    is_payment_manual_input_active,
    resume_pending_usdt_payments, resume_pending_payment_notices, start_usdt_global_watcher,
    resume_pending_binance_payments, start_binance_global_watcher,
    recover_stuck_wallet_orders, start_stale_unpaid_expiry_watcher,
)
from handlers.wallet import (
    cmd_wallet, cmd_load_wallet, cmd_wallet_history,
    handle_wallet_currency_select, handle_wallet_amount_input, handle_wallet_history_page,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


PAYMENT_ALLOWED_DURING_MAINTENANCE_PREFIXES = (
    "check_usdt:",
    "check_binance:",
    "usdt_manual:",
    "upi_paid:",
    "binance_paid:",
)


async def is_maintenance_exempt_user(user_id: int | str | None) -> bool:
    if is_admin_id(user_id):
        return True
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return False
    try:
        settings = await db.get_secret_settings()
        raw = str(settings.get("admin_ids") or "")
        admin_ids = {int(part.strip()) for part in raw.split(",") if part.strip()}
        return uid in admin_ids
    except Exception:
        return False


async def maintenance_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop normal user updates while maintenance mode is ON.

    Exceptions:
    - Telegram admin/tester IDs configured in WebAdmin Secret Settings.
    - Existing payment/check/manual-proof flow, so users who already paid can
      still verify payment and receive their completed order/wallet result.
    """
    user = update.effective_user
    if not user or await is_maintenance_exempt_user(user.id):
        return
    if not await db.is_maintenance_mode():
        return

    if update.callback_query:
        data = str(update.callback_query.data or "")
        if data.startswith(PAYMENT_ALLOWED_DURING_MAINTENANCE_PREFIXES):
            return

    if update.message and is_payment_manual_input_active(user.id):
        return

    lang = await db.get_user_language(user.id)
    text = tr(lang, "maintenance_text")
    if update.callback_query:
        try:
            await update.callback_query.answer(tr(lang, "maintenance_alert"), show_alert=True)
            await update.callback_query.edit_message_text(text)
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(text)
    raise ApplicationHandlerStop


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await db.is_blocked(user_id):
        return

    # USDT manual txn hash
    if await handle_usdt_manual_input(update, context):
        return

    # UPI: payer/account holder name / UTR
    if await handle_upi_text_input(update, context):
        return

    # Binance: account name
    if await handle_binance_text_input(update, context):
        return

    # Wallet: amount input
    if await handle_wallet_amount_input(update, context):
        return

    # Shop: quantity
    if await handle_quantity_input(update, context):
        return


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes photo messages for manual payment screenshots."""
    user_id = update.effective_user.id
    if await db.is_blocked(user_id):
        return
    if await handle_upi_screenshot(update, context):
        return
    if await handle_binance_screenshot(update, context):
        return
    if await handle_usdt_manual_screenshot(update, context):
        return


async def post_init(application):
    try:
        db_conn = db.get_db()
        await db_conn.users.create_index("user_id", unique=True)
        await db_conn.pending_payments.create_index("ref_id")
        await db_conn.pending_payments.create_index([("user_id", 1), ("status", 1)])
        await db_conn.pending_payments.create_index([("status", 1), ("created_at", 1)])
        await db_conn.pending_payments.create_index([("pay_type", 1), ("user_id", 1), ("created_at", -1)])
        await db_conn.pending_payments.create_index("binance_transaction_id", unique=True, sparse=True)
        await db_conn.orders.create_index("order_id")
        await db_conn.orders.create_index([("product_name", 1), ("status", 1), ("created_at", 1)])
        await db_conn.orders.create_index([("status", 1), ("user_id", 1)])
        await db_conn.products.create_index([("name", 1), ("enabled", 1)])
        await db_conn.settings.create_index("key", unique=True)
        logger.info("✅ Database indexes ensured.")
    except Exception as e:
        logger.warning(f"⚠️ Could not create DB indexes (non-fatal): {e}")

    try:
        await recover_stuck_wallet_orders(application.bot)
    except Exception as e:
        logger.exception("⚠️ Could not recover stuck wallet orders: %s", e)

    try:
        await resume_pending_usdt_payments(application)
    except Exception as e:
        logger.exception("⚠️ Could not resume per-payment BEP20 tasks: %s", e)

    try:
        await resume_pending_binance_payments(application)
    except Exception as e:
        logger.exception("⚠️ Could not recover Binance Pay payments: %s", e)

    try:
        await resume_pending_payment_notices(application)
    except Exception as e:
        logger.exception("⚠️ Could not resume payment reminder/expiry tasks: %s", e)

    try:
        await start_stale_unpaid_expiry_watcher(application)
    except Exception as e:
        logger.exception("⚠️ Could not start stale unpaid expiry watcher: %s", e)

    try:
        await start_usdt_global_watcher(application)
    except Exception as e:
        logger.exception("❌ Could not start global BEP20 watcher: %s", e)

    try:
        await start_binance_global_watcher(application)
    except Exception as e:
        logger.exception("❌ Could not start global Binance Pay watcher: %s", e)

    logger.info("✅ Bot started in user-only mode. Use WebAdmin for all admin actions.")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Maintenance guard: runs before normal handlers ──
    app.add_handler(MessageHandler(filters.ALL, maintenance_guard), group=-1)
    app.add_handler(CallbackQueryHandler(maintenance_guard), group=-1)

    # ── User commands ──
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("commands", cmd_commands))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("loadwallet", cmd_load_wallet))
    app.add_handler(CommandHandler("wallethistory", cmd_wallet_history))
    app.add_handler(CommandHandler("orders", cmd_user_orders_command))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("language", cmd_language))

    # ── User inline button callbacks ──
    app.add_handler(CallbackQueryHandler(handle_language_select, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(handle_nav_buttons, pattern=r"^nav:"))
    app.add_handler(CallbackQueryHandler(handle_product_select, pattern=r"^(product:|product_id:)"))
    app.add_handler(CallbackQueryHandler(handle_favorite_button, pattern=r"^(fav:|fav_id:|favorite_product:|favorite_product_id:|favorite_buy:|favorite_buy_id:)"))
    app.add_handler(CallbackQueryHandler(handle_payment_method_select, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(handle_check_usdt, pattern=r"^check_usdt:"))
    app.add_handler(CallbackQueryHandler(handle_check_binance, pattern=r"^check_binance:"))
    app.add_handler(CallbackQueryHandler(handle_usdt_manual_button, pattern=r"^usdt_manual:"))
    app.add_handler(CallbackQueryHandler(handle_upi_paid_button, pattern=r"^upi_paid:"))
    app.add_handler(CallbackQueryHandler(handle_binance_paid_button, pattern=r"^binance_paid:"))
    app.add_handler(CallbackQueryHandler(handle_wallet_currency_select, pattern=r"^wallet_currency:"))
    app.add_handler(CallbackQueryHandler(handle_wallet_history_page, pattern=r"^wallethistory:"))

    # ── Message handlers ──
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/(getorder|getid)"), handle_get_order_lookup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("🚀 Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
