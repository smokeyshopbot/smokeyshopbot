"""
bot.py — Main entry point.

Telegram admin commands were removed intentionally. Use the WebAdmin website as
the only admin panel.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
import hashlib
import socket
import atexit
import os as _os
import threading
import uuid
from datetime import datetime, timezone, timedelta
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

from telegram.error import Conflict
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from config import (
    BOT_TOKEN, BOT_TOKEN_SOURCE, BOT_TOKEN_PREVIEW, BOT_TOKEN_FINGERPRINT, LEGACY_ENV_BOT_TOKEN_NAMES,
    is_admin_id, MONGO_URI, DB_NAME, mask_bot_token, secret_fingerprint,
)
import database as db
from utils.i18n import tr

from handlers.user import (
    cmd_start, cmd_commands, cmd_support, cmd_shop, cmd_favorites, cmd_language, cmd_orders as cmd_user_orders_command, cmd_replacements,
    handle_nav_buttons, handle_language_select, handle_product_select, handle_get_order_lookup,
    handle_payment_method_select, handle_quantity_input, handle_favorite_button, clear_shop_flow,
)
from handlers.payment import (
    handle_check_usdt, handle_upi_paid_button, handle_upi_text_input, handle_upi_screenshot,
    handle_check_binance, handle_binance_paid_button, handle_binance_text_input, handle_binance_screenshot,
    handle_usdt_manual_button, handle_usdt_manual_input, handle_usdt_manual_screenshot,
    is_payment_manual_input_active, clear_payment_manual_flows,
    resume_pending_usdt_payments, resume_pending_payment_notices, start_usdt_global_watcher,
    resume_pending_binance_payments, start_binance_global_watcher,
    recover_stuck_wallet_orders, start_stale_unpaid_expiry_watcher,
)
from handlers.wallet import (
    cmd_wallet, cmd_load_wallet, cmd_wallet_history,
    handle_wallet_currency_select, handle_wallet_amount_input, handle_wallet_history_page, clear_wallet_flow,
)
from handlers.replacements import (
    cmd_report, handle_report_text, handle_report_photo, handle_report_callbacks, is_report_flow_active, clear_report_flow,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


_POLLING_SINGLETON_SOCKET: socket.socket | None = None
_POLLING_LOCK_CLIENT: MongoClient | None = None
_POLLING_LOCK_OWNER: str | None = None
_POLLING_LOCK_KEY: str | None = None
_POLLING_LOCK_STOP = threading.Event()
_POLLING_LOCK_THREAD: threading.Thread | None = None
_POLLING_LOCK_TTL_SECONDS = 180
_POLLING_LOCK_HEARTBEAT_SECONDS = 30


def _token_hash() -> str:
    return hashlib.sha256(str(BOT_TOKEN).encode("utf-8")).hexdigest()


def _log_token_source() -> None:
    """Make token-source problems visible without printing the real token.

    The bot intentionally uses the token saved in WebAdmin Secret Settings via
    MongoDB. Legacy environment token variables are ignored so an old hosting
    variable cannot silently override the WebAdmin token.
    """
    logger.info("🔐 Bot token source: %s", BOT_TOKEN_SOURCE)
    logger.info("🔐 Bot token in use: %s", BOT_TOKEN_PREVIEW)
    logger.info("🔐 MongoDB database in use: %s", DB_NAME)
    for env_name in LEGACY_ENV_BOT_TOKEN_NAMES:
        env_token = str(_os.getenv(env_name) or "").strip()
        if not env_token:
            continue
        if secret_fingerprint(env_token) == BOT_TOKEN_FINGERPRINT:
            logger.info("ℹ️ Legacy env %s is set but matches WebAdmin token; WebAdmin token is still used.", env_name)
        else:
            logger.warning(
                "⚠️ Ignoring legacy env %s token (%s). This bot uses the WebAdmin/MongoDB token (%s). "
                "Remove the old env token from hosting to avoid confusion.",
                env_name, mask_bot_token(env_token), BOT_TOKEN_PREVIEW,
            )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _acquire_local_polling_singleton() -> None:
    """Prevent accidental double polling in the same server/container."""
    global _POLLING_SINGLETON_SOCKET
    if _POLLING_SINGLETON_SOCKET is not None:
        return

    token_hash = _token_hash()
    port = 20000 + (int(token_hash[:8], 16) % 20000)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        logger.error(
            "❌ Another local process on this server/container is already running this Telegram bot. "
            "Stop the duplicate process, or make sure the WebAdmin service is not running bot.py."
        )
        raise SystemExit(1)
    _POLLING_SINGLETON_SOCKET = sock


def _polling_lock_collection(client: MongoClient):
    return client[DB_NAME].runtime_locks


def _format_lock_owner(doc: dict | None) -> str:
    if not doc:
        return "unknown"
    owner = doc.get("owner") or "unknown"
    host = doc.get("hostname") or "unknown-host"
    pid = doc.get("pid") or "unknown-pid"
    expires_at = doc.get("expires_at") or "unknown-expiry"
    return f"owner={owner}, host={host}, pid={pid}, expires_at={expires_at}"


def _acquire_distributed_polling_lock() -> None:
    """Allow only one polling bot across all deployments using MongoDB.

    This catches the case that causes Telegram 409 Conflict on platforms that
    accidentally start multiple containers/workers with the same bot token. The
    lock only controls getUpdates polling. WebAdmin may still use the bot token
    for sending messages/files because it does not poll Telegram updates.
    """
    global _POLLING_LOCK_CLIENT, _POLLING_LOCK_OWNER, _POLLING_LOCK_KEY, _POLLING_LOCK_THREAD
    if _POLLING_LOCK_CLIENT is not None:
        return

    token_hash = _token_hash()
    lock_key = f"telegram_polling:{token_hash}"
    owner = f"{socket.gethostname()}:{_os.getpid()}:{uuid.uuid4().hex[:8]}"
    now = _now_utc()
    expires_at = now + timedelta(seconds=_POLLING_LOCK_TTL_SECONDS)

    try:
        client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=15000,
        )
        client.admin.command("ping")
        locks = _polling_lock_collection(client)
        locks.create_index("key", unique=True)
        locks.create_index("expires_at", expireAfterSeconds=0)

        update = {
            "$set": {
                "key": lock_key,
                "purpose": "telegram_getupdates_polling",
                "owner": owner,
                "hostname": socket.gethostname(),
                "pid": _os.getpid(),
                "token_hash_prefix": token_hash[:12],
                "updated_at": now,
                "expires_at": expires_at,
            }
        }
        lock_doc = locks.find_one_and_update(
            {
                "key": lock_key,
                "$or": [
                    {"expires_at": {"$lte": now}},
                    {"expires_at": {"$exists": False}},
                    {"owner": owner},
                ],
            },
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        try:
            active = _polling_lock_collection(client).find_one({"key": lock_key})
        except Exception:
            active = None
        logger.error(
            "❌ Telegram polling lock is already held by another running bot instance (%s). "
            "This process will exit before calling getUpdates to avoid Telegram 409 Conflict. "
            "Stop duplicate bot services/workers/replicas, wait up to 3 minutes for stale locks, then restart one bot only.",
            _format_lock_owner(active),
        )
        try:
            client.close()
        except Exception:
            pass
        raise SystemExit(1)
    except PyMongoError as exc:
        logger.error(
            "❌ Could not verify the Telegram polling singleton lock in MongoDB: %s. "
            "Bot will not start because starting without this check can create duplicate polling conflicts.",
            exc,
        )
        raise SystemExit(1)

    if not lock_doc or lock_doc.get("owner") != owner:
        logger.error(
            "❌ Telegram polling lock was not acquired (%s). Stop duplicate bot instances and restart one bot only.",
            _format_lock_owner(lock_doc),
        )
        try:
            client.close()
        except Exception:
            pass
        raise SystemExit(1)

    _POLLING_LOCK_CLIENT = client
    _POLLING_LOCK_OWNER = owner
    _POLLING_LOCK_KEY = lock_key
    _POLLING_LOCK_STOP.clear()
    _POLLING_LOCK_THREAD = threading.Thread(target=_polling_lock_heartbeat, name="telegram-polling-lock", daemon=True)
    _POLLING_LOCK_THREAD.start()
    atexit.register(_release_distributed_polling_lock)
    logger.info("✅ Telegram polling lock acquired: %s", owner)


def _polling_lock_heartbeat() -> None:
    while not _POLLING_LOCK_STOP.wait(_POLLING_LOCK_HEARTBEAT_SECONDS):
        try:
            if not (_POLLING_LOCK_CLIENT and _POLLING_LOCK_KEY and _POLLING_LOCK_OWNER):
                return
            now = _now_utc()
            result = _polling_lock_collection(_POLLING_LOCK_CLIENT).update_one(
                {"key": _POLLING_LOCK_KEY, "owner": _POLLING_LOCK_OWNER},
                {"$set": {"updated_at": now, "expires_at": now + timedelta(seconds=_POLLING_LOCK_TTL_SECONDS)}},
            )
            if not result.matched_count:
                logger.error("❌ Telegram polling lock was lost. Exiting to avoid duplicate getUpdates polling.")
                _os._exit(1)
        except Exception as exc:
            logger.error("❌ Telegram polling lock heartbeat failed: %s. Exiting to avoid duplicate polling.", exc)
            _os._exit(1)


def _release_distributed_polling_lock() -> None:
    global _POLLING_LOCK_CLIENT, _POLLING_LOCK_OWNER, _POLLING_LOCK_KEY
    _POLLING_LOCK_STOP.set()
    client = _POLLING_LOCK_CLIENT
    key = _POLLING_LOCK_KEY
    owner = _POLLING_LOCK_OWNER
    if client and key and owner:
        try:
            _polling_lock_collection(client).delete_one({"key": key, "owner": owner})
            logger.info("✅ Telegram polling lock released: %s", owner)
        except Exception as exc:
            logger.warning("⚠️ Could not release Telegram polling lock cleanly: %s", exc)
        try:
            client.close()
        except Exception:
            pass
    _POLLING_LOCK_CLIENT = None
    _POLLING_LOCK_OWNER = None
    _POLLING_LOCK_KEY = None


def _acquire_polling_singletons() -> None:
    _acquire_local_polling_singleton()
    _acquire_distributed_polling_lock()


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


def _clear_user_input_flows(user_id: int | str | None, *, include_payment_manual: bool = True) -> None:
    """Clear active one-user input sessions when a new command takes over.

    Text messages inside a flow still work normally. Only a new bot command or
    a new navigation callback cancels the previous waiting-for-input state.
    Pending payment records are not cancelled; only temporary manual-proof input
    state is cleared.
    """
    clear_report_flow(user_id)
    clear_shop_flow(user_id)
    clear_wallet_flow(user_id)
    if include_payment_manual:
        clear_payment_manual_flows(user_id)


async def clear_input_flows_on_new_command_or_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the previous waiting flow when the user starts another command.

    This prevents cases like starting /report, then opening /shop, where the
    next quantity message could otherwise be consumed by the old report flow.
    Normal non-command messages inside the active flow are still accepted.
    """
    user = update.effective_user
    if not user:
        return

    if update.message and update.message.text and update.message.text.strip().startswith("/"):
        _clear_user_input_flows(user.id, include_payment_manual=True)
        return

    if update.callback_query:
        data = str(update.callback_query.data or "")
        if data.startswith("report:"):
            return
        # Navigation/product/wallet buttons start a new user flow, so cancel a
        # half-finished report. Do not clear shop flow for payment buttons.
        if data.startswith(("nav:", "product:", "product_id:", "favorite_buy:", "favorite_buy_id:", "wallet_currency:")):
            clear_report_flow(user.id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await db.is_blocked(user_id):
        return

    # Replacement/report flow
    if await handle_report_text(update, context):
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


async def _field_has_duplicates(collection, field_name: str, *, non_empty_string_only: bool = True) -> bool:
    """Return True when creating a unique index would fail due duplicates.

    This check never modifies data. It is used before converting an older
    non-unique index into a unique one.
    """
    match = {field_name: {"$exists": True}}
    if non_empty_string_only:
        match[field_name].update({"$type": "string", "$gt": ""})
    rows = await collection.aggregate([
        {"$match": match},
        {"$group": {"_id": f"${field_name}", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$limit": 1},
    ]).to_list(length=1)
    return bool(rows)


async def _ensure_unique_index_safely(collection, keys, *, name: str, duplicate_field: str, partial_filter: dict | None = None):
    """Create/upgrade a unique index only when it is safe for existing data.

    If duplicates already exist, no rows are deleted or changed; startup logs a
    warning and keeps the old index so the bot can continue running.
    """
    duplicate_check_non_empty = duplicate_field != "order_id"
    if await _field_has_duplicates(collection, duplicate_field, non_empty_string_only=duplicate_check_non_empty):
        logger.warning(
            "⚠️ Unique index %s was not created because duplicate %s values already exist. Clean duplicates first.",
            name, duplicate_field,
        )
        return

    keys_list = list(keys) if isinstance(keys, (list, tuple)) else [(keys, 1)]
    try:
        index_info = await collection.index_information()
        for existing_name, spec in index_info.items():
            if list(spec.get("key", [])) == keys_list:
                if spec.get("unique"):
                    return
                # Drop only the old non-unique index definition, never data.
                await collection.drop_index(existing_name)
                break
        options = {"unique": True, "name": name}
        if partial_filter:
            options["partialFilterExpression"] = partial_filter
        await collection.create_index(keys_list, **options)
    except Exception as exc:
        logger.warning("⚠️ Could not ensure unique index %s (non-fatal): %s", name, exc)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes photo messages for manual payment screenshots."""
    user_id = update.effective_user.id
    if await db.is_blocked(user_id):
        return
    if await handle_report_photo(update, context):
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
        await _ensure_unique_index_safely(
            db_conn.pending_payments,
            [("usdt_transaction_hash", 1)],
            name="uniq_usdt_transaction_hash_nonempty",
            duplicate_field="usdt_transaction_hash",
            partial_filter={"usdt_transaction_hash": {"$type": "string", "$gt": ""}},
        )
        await _ensure_unique_index_safely(
            db_conn.pending_payments,
            [("usdt_txn_hash", 1)],
            name="uniq_usdt_manual_txn_hash_nonempty",
            duplicate_field="usdt_txn_hash",
            partial_filter={"usdt_txn_hash": {"$type": "string", "$gt": ""}},
        )
        await _ensure_unique_index_safely(
            db_conn.orders,
            [("order_id", 1)],
            name="uniq_order_id",
            duplicate_field="order_id",
        )
        await db_conn.orders.create_index([("product_name", 1), ("status", 1), ("created_at", 1)])
        await db_conn.orders.create_index([("status", 1), ("user_id", 1)])
        await db_conn.products.create_index([("name", 1), ("enabled", 1)])
        await db_conn.settings.create_index("key", unique=True)
        await db_conn.replacement_reports.create_index("report_id", unique=True)
        await db_conn.replacement_reports.create_index([("status", 1), ("created_at", -1)])
        await db_conn.replacement_reports.create_index([("user_id", 1), ("created_at", -1)])
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


async def log_runtime_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Make Telegram polling conflicts clear instead of repeating stack traces."""
    error = getattr(context, "error", None)
    if isinstance(error, Conflict):
        logger.error(
            "❌ Telegram returned 409 Conflict. Another bot process is already calling getUpdates with this token. "
            "Check duplicate hosting workers/replicas/services. This process will exit instead of retrying forever."
        )
        _release_distributed_polling_lock()
        _os._exit(1)
    logger.exception("Unhandled bot error", exc_info=error)


def main():
    _log_token_source()
    _acquire_polling_singletons()
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(log_runtime_error)

    # ── Cancel stale input flows when users start another command/menu action ──
    app.add_handler(MessageHandler(filters.COMMAND, clear_input_flows_on_new_command_or_navigation), group=-2)
    app.add_handler(CallbackQueryHandler(clear_input_flows_on_new_command_or_navigation), group=-2)

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
    app.add_handler(CommandHandler("replacements", cmd_replacements))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("report", cmd_report))
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
    app.add_handler(CallbackQueryHandler(handle_report_callbacks, pattern=r"^report:"))

    # ── Message handlers ──
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/(getorder|getid)"), handle_get_order_lookup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("🚀 Starting bot...")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        _release_distributed_polling_lock()


if __name__ == "__main__":
    main()
