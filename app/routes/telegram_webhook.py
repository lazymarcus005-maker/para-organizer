"""Telegram webhook endpoint."""

import hmac

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from app.config import settings
from app.integrations.telegram_bot import handle_update

router = APIRouter(tags=["telegram"])


@router.post("/webhook/telegram")
async def telegram_webhook(
    update: dict,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    secret = settings.TELEGRAM_WEBHOOK_SECRET
    if secret:
        if x_telegram_bot_api_secret_token is None or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, secret
        ):
            raise HTTPException(status_code=401, detail="Unauthorized")
    background_tasks.add_task(handle_update, update)
    # Enqueue notify task via Redis task queue
    try:
        from app.task_queue import TaskQueue
        import asyncio
        asyncio.create_task(_enqueue_telegram_notify(update))
    except Exception:
        pass
    return {"ok": True}


async def _enqueue_telegram_notify(update: dict) -> None:
    """Enqueue a notify task for the Telegram update."""
    try:
        queue = TaskQueue()
        await queue.publish("notify", {"update": update, "channel": "telegram"})
        await queue.close()
    except Exception:
        pass

