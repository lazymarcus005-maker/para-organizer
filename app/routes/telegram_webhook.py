"""Telegram webhook endpoint."""

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
    if secret and x_telegram_bot_api_secret_token != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    background_tasks.add_task(handle_update, update)
    return {"ok": True}

