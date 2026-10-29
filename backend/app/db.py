"""app.db — MongoDB client (Motor) and persisted settings access."""
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import DB_NAME, DEFAULT_SETTINGS, MONGO_URL

# Explicit timeouts: without socketTimeoutMS an Atlas upsert can stay
# blocked indefinitely and freeze the world dump (single uvicorn event loop).
# 300 s (not 60 s): Atlas M0 throughput is throttled — reading the ~32k
# live marinas takes ~150 s, a batch can exceed 60 s.
client = AsyncIOMotorClient(
    MONGO_URL,
    serverSelectionTimeoutMS=20_000,
    connectTimeoutMS=20_000,
    socketTimeoutMS=300_000,
    maxPoolSize=20,
    retryWrites=True,
)
db = client[DB_NAME]


async def get_settings() -> dict:
    doc = await db.settings.find_one({"_id": "global"})
    return {**DEFAULT_SETTINGS, **(doc or {})}
