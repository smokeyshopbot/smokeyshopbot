# Railway deploy: Telegram bot service

Upload/push the contents of this folder as the root of the Railway service.
The root must contain `bot.py`, `requirements.txt`, `railway.json`, and `Procfile` directly.

Start command:

```bash
python bot.py
```

Required Railway variables:

```env
MONGO_URI=your_mongodb_atlas_uri
MONGO_DB_NAME=shopbot
BOT_TOKEN=your_telegram_bot_token
ADMIN_IDS=your_telegram_user_id
```

`DB_NAME` also works, but `MONGO_DB_NAME` is supported for compatibility.

Deploy only one polling bot service for one Telegram bot token.
