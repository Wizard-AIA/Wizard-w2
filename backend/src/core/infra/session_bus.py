import asyncio
import json
import threading
from typing import AsyncIterator, Any
from src.config import settings
from src.utils.logging import logger

class SessionEventBus:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._redis = None
        self._lock = threading.Lock()
        
        if settings.REDIS_URL:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
                logger.info("SessionEventBus configured with Redis")
            except ImportError:
                logger.warning("Redis configured but redis.asyncio not installed")

    def publish(self, session_id: str, event: dict[str, Any]) -> None:
        event_str = json.dumps(event)
        
        if self._redis:
            # Fire and forget async task if we're in an event loop
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._redis.publish(f"wizard:session:{session_id}", event_str))
            except RuntimeError:
                # No running loop, fallback
                asyncio.run(self._redis.publish(f"wizard:session:{session_id}", event_str))
        else:
            with self._lock:
                queues = self._subscribers.get(session_id, set())
                dead = set()
                for q in queues:
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
                    except Exception:
                        dead.add(q)
                for q in dead:
                    self._subscribers[session_id].discard(q)

    async def subscribe(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        if self._redis:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(f"wizard:session:{session_id}")
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        try:
                            yield json.loads(message["data"])
                        except json.JSONDecodeError:
                            logger.error("Failed to decode session event JSON")
            finally:
                await pubsub.unsubscribe(f"wizard:session:{session_id}")
                await pubsub.close()
        else:
            q: asyncio.Queue = asyncio.Queue()
            with self._lock:
                if session_id not in self._subscribers:
                    self._subscribers[session_id] = set()
                self._subscribers[session_id].add(q)
            
            try:
                while True:
                    event = await q.get()
                    yield event
            finally:
                with self._lock:
                    if session_id in self._subscribers:
                        self._subscribers[session_id].discard(q)
                        if not self._subscribers[session_id]:
                            del self._subscribers[session_id]

session_bus = SessionEventBus()
