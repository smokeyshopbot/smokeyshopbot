"""
database.py — MongoDB models and all DB helper functions.
Collections:
  - products   : { name, price_inr, price_usdt, stock: [str], created_at }
  - users      : { user_id, username, blocked, wallet_inr, wallet_usdt, joined_at }
  - orders     : { order_id, user_id, product_name, quantity, items, payment_method,
                   amount_inr, amount_usdt, status, created_at, delivered_at }
  - pending_payments : { user_id, order_id/wallet_load, type, expected_usdt,
                         expected_inr, unique_amount, created_at }
"""

from __future__ import annotations
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from bson import ObjectId
from config import MONGO_URI, DB_NAME

_client: Optional[AsyncIOMotorClient] = None
_db = None


def _name_regex(name: str) -> dict:
    """Case-insensitive exact match for product names, escaping regex metacharacters."""
    return {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}


def get_db():
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(
            MONGO_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,  # Fixes SSL handshake on Python 3.13
            serverSelectionTimeoutMS=30000,
        )
        _db = _client[DB_NAME]
    return _db


# ─────────────────────────── USERS ───────────────────────────

async def get_user(user_id: int) -> Optional[dict]:
    return await get_db().users.find_one({"user_id": user_id})


async def upsert_user(user_id: int, username: str = "") -> dict:
    db = get_db()
    username = (username or "").strip().lstrip("@")
    existing = await db.users.find_one({"user_id": user_id})
    if existing:
        if username and existing.get("username") != username:
            await db.users.update_one(
                {"user_id": user_id},
                {"$set": {"username": username, "username_updated_at": datetime.now(timezone.utc)}},
            )
            existing["username"] = username
        return existing
    doc = {
        "user_id": user_id,
        "username": username,
        "blocked": False,
        "wallet_inr": 0.0,
        "wallet_usdt": 0.0,
        "joined_at": datetime.now(timezone.utc),
        "language": None,
        "language_selected": False,
    }
    await db.users.insert_one(doc)
    return doc


async def is_blocked(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user.get("blocked"))


def normalize_language(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in {"spanish", "espanol", "español"}:
        return "es"
    if value in {"english", "eng"}:
        return "en"
    return value if value in {"en", "es"} else "en"


async def get_user_language(user_id: int, default: str = "en") -> str:
    user = await get_user(user_id)
    if not user:
        return normalize_language(default)
    return normalize_language(user.get("language") or user.get("language_code") or default)


async def has_selected_language(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user.get("language_selected"))


async def set_user_language(user_id: int, language: str) -> str:
    language = normalize_language(language)
    await upsert_user(user_id, "")
    await get_db().users.update_one(
        {"user_id": user_id},
        {"$set": {"language": language, "language_selected": True, "language_updated_at": datetime.now(timezone.utc)}},
    )
    return language


async def get_enabled_languages() -> list[str]:
    doc = await get_db().settings.find_one({"key": "language_settings"}) or {}
    value = doc.get("value") if isinstance(doc, dict) else {}
    enabled = value.get("enabled_languages") if isinstance(value, dict) else None
    if not isinstance(enabled, list):
        enabled = ["en", "es"]
    cleaned = []
    for lang in enabled:
        lang = normalize_language(str(lang))
        if lang not in cleaned:
            cleaned.append(lang)
    if "en" not in cleaned:
        cleaned.insert(0, "en")
    return [lang for lang in cleaned if lang in {"en", "es"}] or ["en"]


async def get_language_settings() -> dict:
    enabled = await get_enabled_languages()
    return {"default_language": "en", "enabled_languages": enabled}


async def set_blocked(user_id: int, blocked: bool):
    await get_db().users.update_one(
        {"user_id": user_id}, {"$set": {"blocked": blocked}}
    )


async def get_all_users() -> list[dict]:
    """Return active users newest first."""
    return await get_db().users.find({"blocked": False}).sort("joined_at", -1).to_list(length=None)


async def get_all_users_including_blocked() -> list[dict]:
    """Return all users newest first."""
    return await get_db().users.find().sort("joined_at", -1).to_list(length=None)


async def get_user_favorite_products(user_id: int) -> list[str]:
    user = await get_user(user_id)
    favorites = user.get("favorite_products", []) if user else []
    return [str(name) for name in favorites if str(name).strip()]


async def is_favorite_product(user_id: int, product_name: str) -> bool:
    favorites = await get_user_favorite_products(user_id)
    target = str(product_name or "").strip().lower()
    return any(str(name).strip().lower() == target for name in favorites)


async def set_product_favorite(user_id: int, product_name: str, favorite: bool) -> bool:
    product_name = str(product_name or "").strip()
    if not product_name:
        return False
    await upsert_user(user_id, "")
    if favorite:
        await get_db().users.update_one({"user_id": user_id}, {"$addToSet": {"favorite_products": product_name}})
    else:
        current = await get_user_favorite_products(user_id)
        kept = [name for name in current if str(name).strip().lower() != product_name.lower()]
        await get_db().users.update_one({"user_id": user_id}, {"$set": {"favorite_products": kept}})
    return True


async def toggle_product_favorite(user_id: int, product_name: str) -> bool:
    currently = await is_favorite_product(user_id, product_name)
    await set_product_favorite(user_id, product_name, not currently)
    return not currently


async def add_wallet_inr(user_id: int, amount: float):
    await get_db().users.update_one(
        {"user_id": user_id}, {"$inc": {"wallet_inr": round(amount, 2)}}
    )


async def add_wallet_usdt(user_id: int, amount: float):
    await get_db().users.update_one(
        {"user_id": user_id}, {"$inc": {"wallet_usdt": round(amount, 6)}}
    )


async def deduct_wallet_inr(user_id: int, amount: float) -> bool:
    user = await get_user(user_id)
    if not user or user.get("wallet_inr", 0) < amount:
        return False
    await get_db().users.update_one(
        {"user_id": user_id}, {"$inc": {"wallet_inr": -round(amount, 2)}}
    )
    return True


async def deduct_wallet_usdt(user_id: int, amount: float) -> bool:
    user = await get_user(user_id)
    if not user or user.get("wallet_usdt", 0) < amount:
        return False
    await get_db().users.update_one(
        {"user_id": user_id}, {"$inc": {"wallet_usdt": -round(amount, 6)}}
    )
    return True


# ─────────────────────────── PRODUCTS ────────────────────────

async def get_all_products() -> list[dict]:
    """Return products newest first."""
    return await get_db().products.find().sort("created_at", -1).to_list(length=None)


async def get_product(name: str) -> Optional[dict]:
    return await get_db().products.find_one({"name": _name_regex(name)})


async def get_product_by_id(product_id: str) -> Optional[dict]:
    try:
        oid = ObjectId(str(product_id))
    except Exception:
        return None
    return await get_db().products.find_one({"_id": oid})


async def add_product(name: str, price_inr: float, price_usdt: float) -> bool:
    existing = await get_product(name)
    if existing:
        return False
    await get_db().products.insert_one({
        "name": name,
        "price_inr": price_inr,
        "price_usdt": price_usdt,
        "stock": [],
        "enabled": True,
        "min_order_quantity": 1,
        "max_order_quantity": 100,
        "low_stock_threshold": 10,
        "low_stock_alert_sent": False,
        "created_at": datetime.now(timezone.utc),
    })
    return True


async def remove_product(name: str) -> bool:
    res = await get_db().products.delete_one(
        {"name": _name_regex(name)}
    )
    return res.deleted_count > 0


async def update_product_price(name: str, price_inr: float, price_usdt: float) -> bool:
    res = await get_db().products.update_one(
        {"name": _name_regex(name)},
        {"$set": {"price_inr": price_inr, "price_usdt": price_usdt}},
    )
    return res.matched_count > 0


async def set_product_enabled(name: str, enabled: bool) -> bool:
    """Enable/disable a product without deleting it."""
    res = await get_db().products.update_one(
        {"name": _name_regex(name)},
        {"$set": {"enabled": bool(enabled)}},
    )
    return res.matched_count > 0


async def set_low_stock_alert_sent(product_name: str, sent: bool) -> bool:
    res = await get_db().products.update_one(
        {"name": _name_regex(product_name)},
        {"$set": {"low_stock_alert_sent": bool(sent)}},
    )
    return res.matched_count > 0


async def add_stock(product_name: str, items: list[str]) -> int:
    """Add only fresh stock items to the end of this product's queue.

    Exact duplicate stock is rejected within the same product, including
    stock already available, stock already delivered, and duplicates repeated
    in the same upload. The same stock text may still be added to a different
    product.

    Stock is delivered FIFO: older items already in the array stay at the
    front, and newly added fresh items go to the back.
    """
    product = await get_product(product_name)
    if not product:
        return 0

    existing = {str(item).strip() for item in (product.get("stock", []) or []) if str(item).strip()}
    cursor = get_db().orders.find(
        {"product_name": _name_regex(product_name), "items.0": {"$exists": True}},
        {"items": 1},
    )
    async for order in cursor:
        existing.update(str(item).strip() for item in (order.get("items", []) or []) if str(item).strip())

    seen_in_upload: set[str] = set()
    fresh_items: list[str] = []
    for item in items:
        clean = str(item).strip()
        if not clean:
            continue
        if clean in existing or clean in seen_in_upload:
            continue
        fresh_items.append(clean)
        seen_in_upload.add(clean)

    if not fresh_items:
        return 0

    res = await get_db().products.update_one(
        {"_id": product["_id"]},
        {"$push": {"stock": {"$each": fresh_items}}},
    )
    return len(fresh_items) if res.matched_count else 0


async def remove_stock_items(product_name: str, items: list[str]) -> dict:
    """
    Remove specific stock item(s) from a product.

    Matching is exact after the admin input is stripped. If the same stock value
    exists multiple times, one occurrence is removed for each matching item sent
    by the admin. Returns removed items, not-found items, and remaining count.
    """
    product = await get_product(product_name)
    if not product:
        return {"removed": [], "not_found": items, "remaining": 0}

    stock = list(product.get("stock", []))
    removed: list[str] = []
    not_found: list[str] = []

    for item in items:
        try:
            index = stock.index(item)
        except ValueError:
            not_found.append(item)
            continue
        removed.append(stock.pop(index))

    if removed:
        await get_db().products.update_one(
            {"_id": product["_id"]},
            {"$set": {"stock": stock}},
        )

    return {"removed": removed, "not_found": not_found, "remaining": len(stock)}


async def pop_stock(product_name: str, quantity: int) -> list[str]:
    """Atomically remove and return the oldest `quantity` stock items.

    This is FIFO delivery: stock added first is sold/delivered first.
    """
    if quantity < 1:
        return []

    stock_expr = {"$ifNull": ["$stock", []]}
    db = get_db()
    updated = await db.products.find_one_and_update(
        {
            "name": _name_regex(product_name),
            "$expr": {"$gte": [{"$size": stock_expr}, quantity]},
        },
        [
            {
                "$set": {
                    "_last_popped": {"$slice": [stock_expr, quantity]},
                    "stock": {
                        "$cond": [
                            {"$gt": [{"$size": stock_expr}, quantity]},
                            {
                                "$slice": [
                                    stock_expr,
                                    quantity,
                                    {"$max": [1, {"$subtract": [{"$size": stock_expr}, quantity]}]},
                                ]
                            },
                            [],
                        ]
                    },
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return []

    items = updated.get("_last_popped", [])
    await db.products.update_one(
        {"_id": updated["_id"]},
        {"$unset": {"_last_popped": ""}},
    )
    return items


async def get_stock_count(product_name: str) -> int:
    product = await get_product(product_name)
    return len(product.get("stock", [])) if product else 0


async def get_pending_stock_quantity(product_name: str) -> int:
    """Total quantity already paid but waiting for stock for this product."""
    cursor = get_db().orders.aggregate([
        {
            "$match": {
                "product_name": _name_regex(product_name),
                "status": "pending_stock",
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ])
    rows = await cursor.to_list(length=1)
    return int(rows[0].get("total", 0)) if rows else 0


async def get_available_stock_count(product_name: str) -> int:
    """Stock available for new buyers after paid pending-stock orders are considered."""
    actual = await get_stock_count(product_name)
    pending_qty = await get_pending_stock_quantity(product_name)
    return max(0, actual - pending_qty)


async def get_all_products_with_availability(include_disabled: bool = False) -> list[dict]:
    """Return products with available_stock and pending_stock_quantity fields.

    By default, disabled products are hidden from the shop. Admin views can pass
    include_disabled=True to see everything. Missing "enabled" means enabled for
    backwards compatibility with existing products.
    """
    if include_disabled:
        products = await get_all_products()
    else:
        products = await get_db().products.find({
            "$or": [{"enabled": True}, {"enabled": {"$exists": False}}]
        }).sort("created_at", -1).to_list(length=None)
    for product in products:
        actual_stock = len(product.get("stock", []) or [])
        pending_qty = await get_pending_stock_quantity(product.get("name", ""))
        product["enabled"] = product.get("enabled", True)
        product["actual_stock"] = actual_stock
        product["pending_stock_quantity"] = pending_qty
        product["available_stock"] = max(0, actual_stock - pending_qty)
    return products


async def clear_stock(product_name: str) -> bool:
    res = await get_db().products.update_one(
        {"name": _name_regex(product_name)},
        {"$set": {"stock": []}},
    )
    return res.matched_count > 0


# ─────────────────────────── ORDERS ──────────────────────────

async def create_order(
    user_id: int,
    product_name: str,
    quantity: int,
    payment_method: str,
    amount_inr: float,
    amount_usdt: float,
    username: str = "",
) -> str:
    order_id = str(uuid.uuid4())[:8].upper()
    username = (username or "").strip().lstrip("@")
    if username:
        await upsert_user(user_id, username)
    await get_db().orders.insert_one({
        "order_id": order_id,
        "user_id": user_id,
        "username": username,
        "product_name": product_name,
        "quantity": quantity,
        "items": [],
        "payment_method": payment_method,
        "amount_inr": amount_inr,
        "amount_usdt": amount_usdt,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "delivered_at": None,
    })
    return order_id


async def get_order(order_id: str) -> Optional[dict]:
    return await get_db().orders.find_one({"order_id": order_id})


async def update_order_status(order_id: str, status: str, items: list[str] = None):
    now = datetime.now(timezone.utc)
    update = {"$set": {"status": status}}
    if items is not None:
        update["$set"]["items"] = items
    if status == "delivered":
        update["$set"]["delivered_at"] = now
    if status == "pending_stock":
        update["$set"].setdefault("pending_stock_at", now)
    await get_db().orders.update_one({"order_id": order_id}, update)


async def mark_order_pending_stock(order_id: str):
    await update_order_status(order_id, "pending_stock")


async def has_pending_stock_ahead(product_name: str, created_at, order_id: str | None = None) -> bool:
    """True when an older paid order for the same product is already waiting for stock."""
    query = {
        "product_name": _name_regex(product_name),
        "status": "pending_stock",
    }
    if created_at:
        query["created_at"] = {"$lt": created_at}
    if order_id:
        query["order_id"] = {"$ne": order_id}
    return await get_db().orders.count_documents(query, limit=1) > 0


async def get_pending_stock_orders(product_name: str, limit: int = 100) -> list[dict]:
    """Oldest paid orders waiting for this product's stock."""
    return await get_db().orders.find({
        "product_name": _name_regex(product_name),
        "status": "pending_stock",
    }).sort("created_at", 1).limit(limit).to_list(length=limit)


async def get_stuck_wallet_pending_orders(limit: int = 100) -> list[dict]:
    """Wallet-paid orders should never remain plain pending.

    This helper finds old wallet orders that were charged but not finalized,
    so startup recovery can deliver them or move them to pending_stock.
    """
    limit = max(1, min(int(limit or 100), 500))
    return await get_db().orders.find({
        "status": "pending",
        "payment_method": {"$in": ["wallet_inr", "wallet_usdt"]},
    }).sort("created_at", 1).limit(limit).to_list(length=limit)


async def get_user_orders(user_id: int, limit: int = 20, skip: int = 0) -> list[dict]:
    """Return the newest orders belonging to one user."""
    return await get_db().orders.find(
        {"user_id": user_id}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def count_user_orders(user_id: int) -> int:
    """Count all orders belonging to one user for pagination."""
    return await get_db().orders.count_documents({"user_id": user_id})


async def get_recent_orders(limit: int = 20, skip: int = 0) -> list[dict]:
    """Return newest orders across all users for admin views, with user info joined in."""
    limit = max(1, min(int(limit or 20), 100))
    skip = max(0, int(skip or 0))
    pipeline = [
        {"$sort": {"created_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "user_id",
            "as": "user_doc",
        }},
        {"$unwind": {"path": "$user_doc", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "username": {
                "$ifNull": [
                    "$username",
                    {"$ifNull": ["$user_doc.username", ""]}
                ]
            },
            "user_blocked": {"$ifNull": ["$user_doc.blocked", False]},
        }},
    ]
    return await get_db().orders.aggregate(pipeline).to_list(length=limit)


async def count_all_orders() -> int:
    """Count all orders across all users for admin pagination."""
    return await get_db().orders.count_documents({})


async def get_all_pending_stock_orders(limit: int = 100) -> list[dict]:
    """All paid orders waiting for stock, oldest first."""
    return await get_db().orders.find({"status": "pending_stock"}).sort(
        "created_at", 1
    ).limit(limit).to_list(length=limit)


def _order_paid_amounts(order: dict) -> tuple[float, float]:
    """Return paid amount as (INR, USDT) using the actual payment currency only.

    Orders keep both INR and USDT product-price equivalents, but revenue must
    count only the currency used for payment.
    """
    method = str((order or {}).get("payment_method") or "").lower()
    amount_inr = float((order or {}).get("amount_inr") or 0)
    amount_usdt = float((order or {}).get("amount_usdt") or 0)

    if method in {"upi", "wallet_inr", "inr"} or "upi" in method or method.endswith("_inr"):
        return amount_inr, 0.0
    if (
        method in {"usdt", "wallet_usdt", "binance", "binance_pay", "binance_usdt", "bep20"}
        or "usdt" in method
        or "binance" in method
        or "bep20" in method
    ):
        return 0.0, amount_usdt

    if amount_usdt and not amount_inr:
        return 0.0, amount_usdt
    return amount_inr, 0.0


def _paid_inr_mongo_expr() -> dict:
    """Mongo expression: amount_inr only for INR-paid order methods."""
    method = {"$toLower": {"$ifNull": ["$payment_method", ""]}}
    return {"$cond": [
        {"$or": [
            {"$in": [method, ["upi", "wallet_inr", "inr"]]},
            {"$regexMatch": {"input": method, "regex": "upi"}},
            {"$regexMatch": {"input": method, "regex": "_inr$"}},
        ]},
        {"$ifNull": ["$amount_inr", 0]},
        0,
    ]}


def _paid_usdt_mongo_expr() -> dict:
    """Mongo expression: amount_usdt only for USDT/Binance/BEP20-paid order methods."""
    method = {"$toLower": {"$ifNull": ["$payment_method", ""]}}
    return {"$cond": [
        {"$or": [
            {"$in": [method, ["usdt", "wallet_usdt", "binance", "binance_pay", "binance_usdt", "bep20"]]},
            {"$regexMatch": {"input": method, "regex": "usdt"}},
            {"$regexMatch": {"input": method, "regex": "binance"}},
            {"$regexMatch": {"input": method, "regex": "bep20"}},
        ]},
        {"$ifNull": ["$amount_usdt", 0]},
        0,
    ]}


async def get_user_order_stats(user_id: int) -> dict:
    """Aggregated order stats for one user."""
    db = get_db()
    orders = await db.orders.find({"user_id": user_id}).to_list(length=None)
    total_orders = len(orders)
    delivered = sum(1 for o in orders if o.get("status") == "delivered")
    pending_stock = sum(1 for o in orders if o.get("status") == "pending_stock")
    pending = sum(1 for o in orders if o.get("status") == "pending")
    failed = sum(1 for o in orders if o.get("status") in {"failed", "expired"})
    paid_orders = [o for o in orders if o.get("status") in {"delivered", "pending_stock"}]
    paid_amounts = [_order_paid_amounts(o) for o in paid_orders]
    total_inr = sum(v[0] for v in paid_amounts)
    total_usdt = sum(v[1] for v in paid_amounts)
    return {
        "total_orders": total_orders,
        "delivered": delivered,
        "pending_stock": pending_stock,
        "pending": pending,
        "failed": failed,
        "total_inr": total_inr,
        "total_usdt": total_usdt,
    }


async def get_bot_stats() -> dict:
    """Aggregated stats for admin dashboard."""
    db = get_db()
    users_total = await db.users.count_documents({})
    users_blocked = await db.users.count_documents({"blocked": True})
    orders_total = await db.orders.count_documents({})
    orders_delivered = await db.orders.count_documents({"status": "delivered"})
    orders_pending_stock = await db.orders.count_documents({"status": "pending_stock"})
    orders_pending = await db.orders.count_documents({"status": "pending"})
    orders_failed = await db.orders.count_documents({"status": {"$in": ["failed", "expired"]}})
    products_total = await db.products.count_documents({})
    products_enabled = await db.products.count_documents({"$or": [{"enabled": True}, {"enabled": {"$exists": False}}]})

    revenue_rows = await db.orders.aggregate([
        {"$match": {"status": {"$in": ["delivered", "pending_stock"]}}},
        {"$group": {
            "_id": None,
            "inr": {"$sum": _paid_inr_mongo_expr()},
            "usdt": {"$sum": _paid_usdt_mongo_expr()},
        }},
    ]).to_list(length=1)
    revenue = revenue_rows[0] if revenue_rows else {"inr": 0, "usdt": 0}

    products = await db.products.find().to_list(length=None)
    total_stock = sum(len(p.get("stock", []) or []) for p in products)

    return {
        "users_total": users_total,
        "users_blocked": users_blocked,
        "orders_total": orders_total,
        "orders_delivered": orders_delivered,
        "orders_pending_stock": orders_pending_stock,
        "orders_pending": orders_pending,
        "orders_failed": orders_failed,
        "products_total": products_total,
        "products_enabled": products_enabled,
        "total_stock": total_stock,
        "revenue_inr": float(revenue.get("inr", 0) or 0),
        "revenue_usdt": float(revenue.get("usdt", 0) or 0),
    }


async def get_buyer_ranking(limit: int = 10, skip: int = 0) -> list[dict]:
    """Return top buyers by paid order value with user info.

    Counts delivered orders and paid orders waiting for stock. Pending/unpaid,
    failed, and expired orders are excluded.
    """
    limit = max(1, min(int(limit or 10), 50))
    skip = max(0, int(skip or 0))
    pipeline = [
        {"$match": {"status": {"$in": ["delivered", "pending_stock"]}}},
        {"$group": {
            "_id": "$user_id",
            "total_orders": {"$sum": 1},
            "delivered_orders": {"$sum": {"$cond": [{"$eq": ["$status", "delivered"]}, 1, 0]}},
            "pending_stock_orders": {"$sum": {"$cond": [{"$eq": ["$status", "pending_stock"]}, 1, 0]}},
            "total_inr": {"$sum": _paid_inr_mongo_expr()},
            "total_usdt": {"$sum": _paid_usdt_mongo_expr()},
            "last_order_at": {"$max": "$created_at"},
        }},
        {"$lookup": {
            "from": "users",
            "localField": "_id",
            "foreignField": "user_id",
            "as": "user_doc",
        }},
        {"$unwind": {"path": "$user_doc", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": 0,
            "user_id": "$_id",
            "username": "$user_doc.username",
            "total_orders": 1,
            "delivered_orders": 1,
            "pending_stock_orders": 1,
            "total_inr": 1,
            "total_usdt": 1,
            "last_order_at": 1,
        }},
        {"$sort": {"total_usdt": -1, "total_inr": -1, "total_orders": -1, "user_id": 1}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    return await get_db().orders.aggregate(pipeline).to_list(length=limit)


async def count_ranked_buyers() -> int:
    """Count users with at least one paid-value order."""
    rows = await get_db().orders.aggregate([
        {"$match": {"status": {"$in": ["delivered", "pending_stock"]}}},
        {"$group": {"_id": "$user_id"}},
        {"$count": "count"},
    ]).to_list(length=1)
    return int(rows[0].get("count", 0)) if rows else 0




# ─────────────────────── WALLET HISTORY ──────────────────────

async def get_user_wallet_logs(user_id: int, limit: int = 10, skip: int = 0) -> list[dict]:
    """Return wallet top-up/payment logs for one user, newest first.

    These are stored in pending_payments with pay_type='wallet'. They include
    BEP20, UPI and Binance Pay wallet top-ups across waiting, submitted,
    completed, expired and rejected states.
    """
    limit = max(1, min(int(limit or 10), 50))
    skip = max(0, int(skip or 0))
    return await get_db().pending_payments.find({
        "user_id": user_id,
        "pay_type": "wallet",
    }).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def count_user_wallet_logs(user_id: int) -> int:
    """Count wallet top-up logs for one user."""
    return await get_db().pending_payments.count_documents({
        "user_id": user_id,
        "pay_type": "wallet",
    })



# ───────────────────── PAYMENT METHOD SETTINGS ─────────────────

PAYMENT_SETTINGS_KEY = "payment_settings"

DEFAULT_PAYMENT_SETTINGS = {
    "usdt_bep20": {
        "enabled": False,
        "wallet_address": "",
    },
    "upi": {
        "enabled": False,
        "upi_id": "",
        "upi_name": "",
    },
    "binance": {
        "enabled": False,
        "binance_pay_id": "",
        "binance_pay_name": "",
    },
    "wallet_limits": {
        "min_inr": "50",
        "min_usdt": "1",
    },
}


def _clean_payment_settings(settings: dict | None) -> dict:
    """Return a normalized payment-settings document.

    Payment details must come from MongoDB/WebAdmin only. Missing values are
    treated as disabled, never filled from .env.
    """
    settings = settings or {}
    cleaned = {
        method: dict(values)
        for method, values in DEFAULT_PAYMENT_SETTINGS.items()
    }

    for method, defaults in DEFAULT_PAYMENT_SETTINGS.items():
        incoming = settings.get(method) if isinstance(settings, dict) else None
        if not isinstance(incoming, dict):
            continue
        for key in defaults:
            if key == "enabled":
                cleaned[method][key] = bool(incoming.get(key))
            else:
                value = str(incoming.get(key) or "").strip()
                cleaned[method][key] = value if value else str(defaults.get(key, ""))

    for key, default_value in DEFAULT_PAYMENT_SETTINGS["wallet_limits"].items():
        try:
            amount = float(str(cleaned["wallet_limits"].get(key) or default_value).strip())
        except ValueError:
            amount = float(default_value)
        if amount <= 0:
            amount = float(default_value)
        cleaned["wallet_limits"][key] = f"{amount:g}"

    # Be tolerant of older/malformed database documents created during testing,
    # while still never falling back to .env payment details.
    if isinstance(settings, dict):
        legacy_usdt_wallet = str(settings.get("usdt_wallet_address") or settings.get("wallet_address") or settings.get("USDT_WALLET") or "").strip()
        if legacy_usdt_wallet and not cleaned["usdt_bep20"].get("wallet_address"):
            cleaned["usdt_bep20"]["wallet_address"] = legacy_usdt_wallet
        if settings.get("usdt_enabled") is not None and not cleaned["usdt_bep20"].get("enabled"):
            cleaned["usdt_bep20"]["enabled"] = bool(settings.get("usdt_enabled"))

    # Do not allow a method to be enabled without the details required for users.
    if not cleaned["usdt_bep20"].get("wallet_address"):
        cleaned["usdt_bep20"]["enabled"] = False
    if not cleaned["upi"].get("upi_id"):
        cleaned["upi"]["enabled"] = False
    if not cleaned["binance"].get("binance_pay_id"):
        cleaned["binance"]["enabled"] = False

    return cleaned


async def get_payment_settings() -> dict:
    doc = await get_db().settings.find_one({"key": PAYMENT_SETTINGS_KEY})
    value = doc.get("value") if doc else None
    return _clean_payment_settings(value if isinstance(value, dict) else None)


async def set_payment_settings(settings: dict) -> dict:
    cleaned = _clean_payment_settings(settings)
    await get_db().settings.update_one(
        {"key": PAYMENT_SETTINGS_KEY},
        {"$set": {"key": PAYMENT_SETTINGS_KEY, "value": cleaned, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return cleaned


def payment_method_enabled(settings: dict, method: str) -> bool:
    method = (method or "").lower()
    settings = _clean_payment_settings(settings)
    if method in {"usdt", "usdt_bep20", "bep20"}:
        return bool(settings["usdt_bep20"].get("enabled") and settings["usdt_bep20"].get("wallet_address"))
    if method in {"upi", "inr", "wallet_inr"}:
        return bool(settings["upi"].get("enabled") and settings["upi"].get("upi_id"))
    if method in {"binance", "binance_pay", "binance_usdt"}:
        return bool(settings["binance"].get("enabled") and settings["binance"].get("binance_pay_id"))
    if method == "wallet_usdt":
        return payment_method_enabled(settings, "usdt") or payment_method_enabled(settings, "binance")
    return False


async def get_enabled_payment_methods() -> dict:
    settings = await get_payment_settings()
    return {
        "usdt": payment_method_enabled(settings, "usdt"),
        "upi": payment_method_enabled(settings, "upi"),
        "binance": payment_method_enabled(settings, "binance"),
        "wallet_inr": payment_method_enabled(settings, "wallet_inr"),
        "wallet_usdt": payment_method_enabled(settings, "wallet_usdt"),
    }

# ─────────────────────────── SETTINGS ───────────────────────

async def set_setting(key: str, value):
    await get_db().settings.update_one(
        {"key": key},
        {"$set": {"key": key, "value": value, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def get_setting(key: str, default=None):
    doc = await get_db().settings.find_one({"key": key})
    return doc.get("value", default) if doc else default


async def get_secret_settings() -> dict:
    value = await get_setting("secret_settings", {})
    return value if isinstance(value, dict) else {}


def parse_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def parse_positive_float(value, default: float, *, minimum: float = 0.000001) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(minimum), parsed)


async def set_maintenance_mode(enabled: bool):
    await set_setting("maintenance_mode", bool(enabled))


async def is_maintenance_mode() -> bool:
    return bool(await get_setting("maintenance_mode", False))

# ─────────────────────── MAINTENANCE NOTIFICATION QUEUE ────────────────────

async def queue_maintenance_notification(kind: str, product_name: str, payload: dict | None = None):
    """Queue product/stock/price notifications created while maintenance is ON.

    Broadcasts are intentionally not queued. Queued product notifications are
    flushed when maintenance mode is turned OFF. One queued row is kept per
    notification kind + product, so repeated edits during maintenance collapse
    into the latest notification instead of spamming users later.
    """
    kind = str(kind or "").strip()
    product_name = str(product_name or "").strip()
    if not kind or not product_name:
        return
    now = datetime.now(timezone.utc)
    await get_db().maintenance_notifications.update_one(
        {"kind": kind, "product_name": product_name},
        {
            "$set": {
                "kind": kind,
                "product_name": product_name,
                "payload": payload or {},
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def get_maintenance_notifications(limit: int = 100) -> list[dict]:
    cursor = get_db().maintenance_notifications.find({}).sort("created_at", 1).limit(max(1, int(limit or 100)))
    return await cursor.to_list(length=max(1, int(limit or 100)))


async def delete_maintenance_notification(notification_id):
    await get_db().maintenance_notifications.delete_one({"_id": notification_id})


async def count_maintenance_notifications() -> int:
    return await get_db().maintenance_notifications.count_documents({})


# ─────────────────────── PENDING PAYMENTS ────────────────────

async def create_pending_payment(
    user_id: int,
    ref_id: str,          # order_id or "wallet_<uuid>"
    pay_type: str,        # "order" or "wallet"
    method: str,          # "usdt", "upi", or "binance"
    expected_inr: float,
    expected_usdt: float,
    unique_usdt: float,   # The unique decimal amount for USDT matching
    currency: str = "",   # For wallet top-ups: "inr" or "usdt"
    load_amount: float = 0.0,
) -> dict:
    doc = {
        "user_id": user_id,
        "ref_id": ref_id,
        "pay_type": pay_type,
        "method": method,
        "expected_inr": expected_inr,
        "expected_usdt": expected_usdt,
        "unique_usdt": unique_usdt,
        "currency": currency,
        "load_amount": load_amount,
        "status": "waiting",
        "upi_payee_name": None,
        "upi_txn_id": None,
        "upi_screenshot_file_id": None,
        "binance_name": None,
        "binance_screenshot_file_id": None,
        "usdt_txn_hash": None,
        "usdt_screenshot_file_id": None,
        # Stores the payment instruction message so it can be deleted even
        # after a bot restart or when delivery is retried later.
        "payment_chat_id": None,
        "payment_msg_id": None,
        # For wallet top-ups: prevents double credit if user presses Check Payment
        # multiple times or if auto-polling and manual checking overlap.
        "wallet_credited_at": None,
        "reminder_sent_at": None,
        "expired_at": None,
        "created_at": time.time(),
        "language": await get_user_language(user_id),
    }
    await get_db().pending_payments.insert_one(doc)
    return doc


async def get_pending_payment(user_id: int) -> Optional[dict]:
    return await get_db().pending_payments.find_one(
        {"user_id": user_id, "status": "waiting"}
    )


async def get_pending_by_ref(ref_id: str) -> Optional[dict]:
    return await get_db().pending_payments.find_one({"ref_id": ref_id})


async def update_pending_status(ref_id: str, status: str, reviewed: bool = False, reviewed_by: int | None = None):
    update = {"status": status}
    if reviewed:
        update["reviewed_at"] = datetime.now(timezone.utc)
        if reviewed_by is not None:
            update["reviewed_by"] = reviewed_by
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id}, {"$set": update}
    )


async def confirm_pending_payment_if_waiting(ref_id: str) -> Optional[dict]:
    """Atomically mark a waiting payment as confirmed.

    This prevents duplicate processing when the background BEP20 auto-check and
    the user's Check Payment button detect the same transfer at the same time.
    """
    return await get_db().pending_payments.find_one_and_update(
        {"ref_id": ref_id, "status": "waiting"},
        {"$set": {"status": "confirmed", "confirmed_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


def _usdt_tx_hash_from_transaction(transaction: dict | None) -> str:
    tx = transaction or {}
    return str(tx.get("hash") or tx.get("txhash") or tx.get("transactionHash") or "").strip()


async def confirm_pending_usdt_payment_if_waiting(ref_id: str, transaction: dict | None = None) -> Optional[dict]:
    """Atomically mark a waiting BEP20 payment as confirmed.

    When a transaction hash is available, store it and refuse to reuse the same
    hash for another order/top-up. This is important now that tiny USDT rounding
    differences are tolerated.
    """
    tx_hash = _usdt_tx_hash_from_transaction(transaction)
    database = get_db()
    update = {
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc),
    }

    if tx_hash:
        duplicate = await database.pending_payments.find_one({
            "usdt_transaction_hash": tx_hash,
            "ref_id": {"$ne": ref_id},
        })
        if duplicate:
            return None
        update.update({
            "usdt_auto_verified": True,
            "usdt_transaction_hash": tx_hash,
            "usdt_transaction_amount": str((transaction or {}).get("match_actual_usdt") or (transaction or {}).get("value_usdt") or ""),
            "usdt_expected_amount": str((transaction or {}).get("match_expected_usdt") or ""),
            "usdt_amount_difference": str((transaction or {}).get("match_difference_usdt") or ""),
            "usdt_match_type": str((transaction or {}).get("match_type") or ""),
            "usdt_transaction_source": str((transaction or {}).get("source") or ""),
        })

    return await database.pending_payments.find_one_and_update(
        {"ref_id": ref_id, "status": "waiting"},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


async def confirm_expired_usdt_payment(ref_id: str, transaction: dict | None = None) -> Optional[dict]:
    """Mark an expired BEP20 row as confirmed when a late transaction is found."""
    tx_hash = _usdt_tx_hash_from_transaction(transaction)
    database = get_db()
    update = {
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc),
    }
    if tx_hash:
        duplicate = await database.pending_payments.find_one({
            "usdt_transaction_hash": tx_hash,
            "ref_id": {"$ne": ref_id},
        })
        if duplicate:
            return None
        update.update({
            "usdt_auto_verified": True,
            "usdt_transaction_hash": tx_hash,
            "usdt_transaction_amount": str((transaction or {}).get("match_actual_usdt") or (transaction or {}).get("value_usdt") or ""),
            "usdt_expected_amount": str((transaction or {}).get("match_expected_usdt") or ""),
            "usdt_amount_difference": str((transaction or {}).get("match_difference_usdt") or ""),
            "usdt_match_type": str((transaction or {}).get("match_type") or ""),
            "usdt_transaction_source": str((transaction or {}).get("source") or ""),
        })
    return await database.pending_payments.find_one_and_update(
        {"ref_id": ref_id, "status": "expired"},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


async def confirm_pending_binance_payment_if_waiting(ref_id: str, transaction: dict) -> Optional[dict]:
    """Atomically mark a waiting Binance Pay payment as confirmed.

    The Binance transaction id is stored so the same Pay history row cannot be
    reused for another order/top-up. A unique sparse index is created at bot
    startup; this helper also performs a defensive duplicate check for older DBs.
    """
    tx_id = str((transaction or {}).get("transactionId") or (transaction or {}).get("tranId") or "").strip()
    if not tx_id:
        return None

    database = get_db()
    duplicate = await database.pending_payments.find_one({
        "binance_transaction_id": tx_id,
        "ref_id": {"$ne": ref_id},
    })
    if duplicate:
        return None

    update = {
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc),
        "binance_auto_verified": True,
        "binance_transaction_id": tx_id,
        "binance_transaction_time": transaction.get("transactionTime"),
        "binance_transaction_amount": str(transaction.get("amount") or ""),
        "binance_transaction_currency": str(transaction.get("currency") or ""),
    }
    payer = transaction.get("payerInfo")
    if isinstance(payer, dict):
        update["binance_payer_name"] = str(payer.get("name") or "").strip()
        update["binance_payer_id"] = str(payer.get("binanceId") or "").strip()

    try:
        return await database.pending_payments.find_one_and_update(
            {"ref_id": ref_id, "method": "binance", "status": "waiting"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
    except Exception:
        # Most commonly a duplicate-key error if two workers race on the same tx.
        return None


async def get_confirmed_usdt_payments_needing_completion() -> list[dict]:
    """Return BEP20 payments that were marked confirmed but not completed.

    Older versions could mark/detect a BEP20 payment and then fail before
    delivery/credit. This lets the bot recover automatically after restart.
    """
    return await get_db().pending_payments.find({
        "method": "usdt",
        "status": {"$in": ["confirmed", "approved"]},
    }).to_list(length=None)


async def get_confirmed_binance_payments_needing_completion() -> list[dict]:
    """Return auto-confirmed Binance Pay rows that may still need delivery/credit."""
    return await get_db().pending_payments.find({
        "method": "binance",
        "status": "confirmed",
        "binance_auto_verified": True,
    }).to_list(length=None)


async def mark_wallet_load_credited(ref_id: str) -> Optional[dict]:
    """Atomically mark a wallet top-up as credited.

    Returns the payment row only the first time it is credited. Subsequent calls
    return None, which prevents duplicate wallet balance updates.
    """
    return await get_db().pending_payments.find_one_and_update(
        {
            "ref_id": ref_id,
            "pay_type": "wallet",
            "$or": [
                {"wallet_credited_at": None},
                {"wallet_credited_at": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "wallet_credited_at": datetime.now(timezone.utc),
                "status": "completed",
            }
        },
        return_document=ReturnDocument.AFTER,
    )


async def set_pending_payment_config(ref_id: str, payment_details: dict):
    """Store the exact payment details shown to the user for this session.

    This prevents a later admin settings change from making an active payment
    verify against a different wallet/payee than the user was shown.
    """
    clean = {}
    for key, value in (payment_details or {}).items():
        clean[str(key)] = str(value or "").strip()
    if clean:
        await get_db().pending_payments.update_one(
            {"ref_id": ref_id},
            {"$set": {"payment_details": clean}},
        )


async def set_pending_payment_message(ref_id: str, chat_id: int, msg_id: int):
    """Persist the Telegram payment instruction message for later cleanup."""
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": {"payment_chat_id": chat_id, "payment_msg_id": msg_id}},
    )


async def set_pending_payment_message_meta(ref_id: str, *, kind: str, template: str):
    """Store a payment message template so the countdown can be edited in-place."""
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": {"payment_message_kind": kind, "payment_message_template": template}},
    )


async def clear_pending_payment_message(ref_id: str):
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$unset": {"payment_chat_id": "", "payment_msg_id": ""}},
    )


async def set_usdt_manual_details(ref_id: str, txn_hash: str, screenshot_file_id: str | None = None):
    update = {"usdt_txn_hash": txn_hash, "status": "usdt_manual_submitted"}
    if screenshot_file_id:
        update["usdt_screenshot_file_id"] = screenshot_file_id
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": update},
    )


async def set_upi_details(ref_id: str, payee_name: str, txn_id: str, screenshot_file_id: str | None = None):
    update = {
        "upi_payee_name": payee_name,
        "upi_txn_id": txn_id,
        "status": "upi_submitted",
    }
    if screenshot_file_id:
        update["upi_screenshot_file_id"] = screenshot_file_id
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": update},
    )


async def set_binance_details(ref_id: str, binance_name: str, screenshot_file_id: str):
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": {
            "binance_name": binance_name,
            "binance_screenshot_file_id": screenshot_file_id,
            "status": "binance_submitted",
        }},
    )


async def mark_payment_reminder_sent(ref_id: str) -> Optional[dict]:
    """Mark reminder sent only once for an active waiting payment."""
    return await get_db().pending_payments.find_one_and_update(
        {
            "ref_id": ref_id,
            "status": "waiting",
            "$or": [
                {"reminder_sent_at": None},
                {"reminder_sent_at": {"$exists": False}},
            ],
        },
        {"$set": {"reminder_sent_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


async def expire_pending_payment_if_waiting(ref_id: str) -> Optional[dict]:
    """Expire a payment only if it is still waiting.

    Returns the expired row only for the process that actually changed it. This
    prevents duplicate expiry notifications when multiple workers/checks race.
    """
    return await get_db().pending_payments.find_one_and_update(
        {"ref_id": ref_id, "status": "waiting"},
        {"$set": {"status": "expired", "expired_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


def _created_at_ts(value) -> float | None:
    """Return a UTC timestamp for legacy/new created_at values.

    Orders use timezone-aware datetimes, pending_payments may use time.time(),
    and older local test rows can contain strings like ``2026-05-17 08:24 UTC``.
    Cleanup must support all of them, otherwise unpaid orders can stay Pending.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        candidates = [
            raw,
            raw.replace("Z", "+00:00"),
            raw.replace(" UTC", "+00:00"),
            raw.replace(" utc", "+00:00"),
        ]
        dt = None
        for candidate in candidates:
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except Exception:
                pass
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except Exception:
                    pass
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def expire_stale_unpaid_payments_and_orders(timeout_minutes: int = 30) -> dict:
    """Expire old unpaid payment sessions and their plain-pending orders.

    Manual review submissions are intentionally not expired here: once a user has
    submitted proof, the order remains pending until an admin approves/rejects it.
    This cleanup supports numeric, datetime, and ISO-string created_at fields.
    """
    timeout_seconds = max(60, int(timeout_minutes or 30) * 60)
    cutoff_ts = time.time() - timeout_seconds
    now_dt = datetime.now(timezone.utc)
    database = get_db()
    result = {"payments_expired": 0, "orders_expired": 0, "orphan_orders_expired": 0, "failed_orders_marked": 0}

    # Expire waiting payments by parsing created_at in Python so legacy datetime
    # or string values are handled too, not only numeric time.time() values.
    waiting_payments = await database.pending_payments.find(
        {"status": "waiting"},
        {"ref_id": 1, "pay_type": 1, "created_at": 1},
    ).to_list(length=None)
    stale_payments = [p for p in waiting_payments if (_created_at_ts(p.get("created_at")) or time.time()) < cutoff_ts]
    stale_refs = [str(p.get("ref_id") or "") for p in stale_payments if p.get("ref_id")]
    if stale_refs:
        payment_update = await database.pending_payments.update_many(
            {"ref_id": {"$in": stale_refs}, "status": "waiting"},
            {"$set": {"status": "expired", "expired_at": now_dt}},
        )
        result["payments_expired"] += int(payment_update.modified_count or 0)

        order_refs = [str(p.get("ref_id") or "") for p in stale_payments if p.get("pay_type") == "order" and p.get("ref_id")]
        if order_refs:
            order_update = await database.orders.update_many(
                {"order_id": {"$in": order_refs}, "status": "pending"},
                {"$set": {"status": "expired", "expired_at": now_dt}},
            )
            result["orders_expired"] += int(order_update.modified_count or 0)

    review_or_paid_statuses = {
        "upi_submitted", "binance_submitted", "usdt_manual_submitted",
        "approved", "confirmed", "completed",
    }
    pending_orders = await database.orders.find(
        {"status": "pending"},
        {"order_id": 1, "created_at": 1},
    ).to_list(length=None)
    for order in pending_orders:
        ref_id = str(order.get("order_id") or "")
        if not ref_id:
            continue
        order_ts = _created_at_ts(order.get("created_at"))
        if order_ts is None or order_ts >= cutoff_ts:
            continue
        pending = await database.pending_payments.find_one({"ref_id": ref_id})
        if pending and pending.get("status") in review_or_paid_statuses:
            continue
        if pending and pending.get("status") == "rejected":
            update = await database.orders.update_one(
                {"order_id": ref_id, "status": "pending"},
                {"$set": {"status": "failed", "failed_at": now_dt}},
            )
            result["failed_orders_marked"] += int(update.modified_count or 0)
            continue
        if pending and pending.get("status") == "waiting":
            pending_ts = _created_at_ts(pending.get("created_at"))
            # If an older row has an unparsable payment timestamp, do not let it
            # block expiry when the order itself is already older than the timeout.
            if pending_ts is not None and pending_ts >= cutoff_ts:
                continue
            await database.pending_payments.update_one(
                {"ref_id": ref_id, "status": "waiting"},
                {"$set": {"status": "expired", "expired_at": now_dt}},
            )
        update = await database.orders.update_one(
            {"order_id": ref_id, "status": "pending"},
            {"$set": {"status": "expired", "expired_at": now_dt}},
        )
        changed = int(update.modified_count or 0)
        result["orphan_orders_expired"] += changed
        result["orders_expired"] += changed

    return result


async def get_all_waiting_payments() -> list[dict]:
    return await get_db().pending_payments.find({"status": "waiting"}).to_list(length=None)


async def get_all_pending_usdt() -> list[dict]:
    return await get_db().pending_payments.find(
        {"method": "usdt", "status": "waiting"}
    ).to_list(length=None)


async def get_all_pending_binance() -> list[dict]:
    return await get_db().pending_payments.find(
        {"method": "binance", "status": "waiting"}
    ).to_list(length=None)


async def get_all_pending_unique_usdt_payments() -> list[dict]:
    """Rows whose unique_usdt amount should not collide across auto-verifiers."""
    return await get_db().pending_payments.find({
        "method": {"$in": ["usdt", "binance"]},
        "status": "waiting",
        "unique_usdt": {"$gt": 0},
    }).to_list(length=None)


async def get_used_binance_transaction_ids() -> set[str]:
    rows = await get_db().pending_payments.find(
        {"binance_transaction_id": {"$exists": True, "$ne": ""}},
        {"binance_transaction_id": 1},
    ).to_list(length=None)
    return {str(row.get("binance_transaction_id") or "").strip() for row in rows if row.get("binance_transaction_id")}


async def get_used_usdt_transaction_hashes() -> set[str]:
    rows = await get_db().pending_payments.find(
        {"usdt_transaction_hash": {"$exists": True, "$ne": ""}},
        {"usdt_transaction_hash": 1},
    ).to_list(length=None)
    return {str(row.get("usdt_transaction_hash") or "").strip() for row in rows if row.get("usdt_transaction_hash")}


async def clear_pending(user_id: int):
    await get_db().pending_payments.delete_many({"user_id": user_id, "status": "waiting"})
