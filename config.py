"""Configuration loader for the Telegram shop bot.

Only MongoDB connection values are loaded from .env. Bot secrets such as the
Telegram bot token, explorer API keys, support usernames, and timing/settings
are managed from WebAdmin → Secret Settings and stored in MongoDB.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


# MongoDB is the only required bot-side .env configuration. All other bot
# secrets/settings are pulled from the database so changing them in WebAdmin is
# the single source of truth.
MONGO_URI: str = _required("MONGO_URI")
DB_NAME: str = os.getenv("DB_NAME", "shopbot").strip() or "shopbot"
SECRET_SETTINGS_KEY = "secret_settings"


def _load_secret_settings() -> dict:
    try:
        client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=30000,
        )
        doc = client[DB_NAME].settings.find_one({"key": SECRET_SETTINGS_KEY}) or {}
        value = doc.get("value") if isinstance(doc, dict) else {}
        client.close()
        return value if isinstance(value, dict) else {}
    except Exception:
        # Keep config import readable; the required-token error below gives the
        # admin a clear action after WebAdmin is configured.
        return {}


_SECRET_SETTINGS = _load_secret_settings()


def _secret_str(key: str, default: str = "") -> str:
    value = _SECRET_SETTINGS.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _secret_required(key: str, label: str) -> str:
    value = _secret_str(key)
    if not value:
        raise RuntimeError(
            f"Missing {label}. Open WebAdmin → Secret Settings, save {label}, then restart the bot."
        )
    return value


def _secret_int(key: str, default: int) -> int:
    value = _secret_str(key)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Secret setting {key} must be an integer, got: {value!r}") from exc


# Telegram
BOT_TOKEN: str = _secret_required("bot_token", "Telegram bot token")

# Telegram admin panel has been removed. WebAdmin is the only admin panel.
ADMIN_IDS: list[int] = []
ADMIN_ID: int = 0


def is_admin_id(user_id: int | str | None) -> bool:
    return False


# USDT BEP20 verification providers. Managed in WebAdmin → Secret Settings.
BSCSAN_API_KEY_COMPAT = _secret_str("bscscan_api_key")
BSCSCAN_API_KEY: str = BSCSAN_API_KEY_COMPAT
ETHERSCAN_API_KEY: str = _secret_str("etherscan_api_key", BSCSCAN_API_KEY)
BSC_RPC_URL: str = _secret_str("bsc_rpc_url", "https://bsc-rpc.publicnode.com") or "https://bsc-rpc.publicnode.com"
BSC_RPC_URLS: str = _secret_str(
    "bsc_rpc_urls",
    "https://bsc-rpc.publicnode.com,https://bsc.drpc.org,https://rpc.ankr.com/bsc",
)
USDT_LOOKBACK_SECONDS: int = _secret_int("usdt_lookback_seconds", 3600)
BSC_RPC_BLOCK_CHUNK_SIZE: int = _secret_int("bsc_rpc_block_chunk_size", 450)
BEP20_REQUIRED_CONFIRMATIONS: int = _secret_int("bep20_required_confirmations", 3)

# Binance Pay auto-verification. Managed in WebAdmin → Secret Settings.
# These are optional at import time so Binance Pay can still be used manually
# until the admin saves API credentials and restarts the bot.
BINANCE_API_KEY: str = _secret_str("binance_api_key")
BINANCE_API_SECRET: str = _secret_str("binance_api_secret")
BINANCE_API_BASE_URL: str = _secret_str("binance_api_base_url", "https://api.binance.com") or "https://api.binance.com"
BINANCE_RECV_WINDOW_MS: int = _secret_int("binance_recv_window_ms", 5000)
BINANCE_PAY_HISTORY_LOOKBACK_SECONDS: int = _secret_int("binance_pay_history_lookback_seconds", 3600)

# Support
SUPPORT_USERNAMES: list[str] = _split_csv(_secret_str("support_usernames"))
SUPPORT_USERNAME: str = SUPPORT_USERNAMES[0] if SUPPORT_USERNAMES else ""

# Rates & Settings
PAYMENT_TIMEOUT_MINUTES: int = _secret_int("payment_timeout_minutes", 30)
PAYMENT_REMINDER_MINUTES: int = _secret_int("payment_reminder_minutes", 20)
if PAYMENT_REMINDER_MINUTES >= PAYMENT_TIMEOUT_MINUTES:
    PAYMENT_REMINDER_MINUTES = max(1, PAYMENT_TIMEOUT_MINUTES - 10)
USDT_VERIFY_INTERVAL: int = _secret_int("usdt_verify_interval_seconds", 30)
USDT_MANUAL_VERIFY_DELAY_MINUTES: int = _secret_int("usdt_manual_verify_delay_minutes", 5)
LOW_STOCK_ALERT_THRESHOLD: int = _secret_int("low_stock_alert_threshold", 10)
MIN_ORDER_QUANTITY: int = max(1, _secret_int("min_order_quantity", 1))
MAX_ORDER_QUANTITY: int = max(MIN_ORDER_QUANTITY, _secret_int("max_order_quantity", 100))
