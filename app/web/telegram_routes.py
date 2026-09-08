"""Webhook Telegram (public, protégé par le secret dérivé du token) + actions de configuration."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.connectors import telegram as tg
from app.packs.vds import telegram as vtg, service as vservice
from app.web.routes import _company_or_redirect

router = APIRouter()
_bot_username = {"v": ""}


def bot_username() -> str:
    if not _bot_username["v"] and tg.configured():
        ok, me = tg.get_me()
        if ok and isinstance(me, dict):
            _bot_username["v"] = me.get("username") or ""
    return _bot_username["v"]


@router.post("/telegram/webhook/{secret}")
async def telegram_webhook(request: Request, secret: str):
    if not tg.configured() or secret != tg.webhook_secret() \
            or request.headers.get("x-telegram-bot-api-secret-token", "") != tg.webhook_secret():
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    vtg.process_async(update, bot_username())
    return {"ok": True}


@router.post("/c/{code}/checkin/telegram")
def telegram_config(request: Request, code: str, action: str = Form(...), chat_id: str = Form("")):
    company, redir = _company_or_redirect(request, code, feature="checkin")
    if redir:
        return redir
    msg = ""
    if action == "webhook":
        ok, info = tg.set_webhook(f"{vservice.base_url()}/telegram/webhook/{tg.webhook_secret()}")
        msg = "Webhook Telegram enregistré." if ok else f"Webhook : {info}"
    elif action == "toggle" and chat_id:
        d = vtg.chats()
        if chat_id in d:
            d[chat_id]["notify"] = not d[chat_id].get("notify")
            vtg.save_chats(d)
            msg = f"Notifications {'activées' if d[chat_id]['notify'] else 'désactivées'} pour « {d[chat_id].get('title')} »."
    elif action == "forget" and chat_id:
        d = vtg.chats()
        d.pop(chat_id, None)
        vtg.save_chats(d)
        msg = "Chat oublié."
    elif action == "test":
        n = vtg.notify("🔔 Test Vaelan : les notifications de La Villa des Sables du Lagon arrivent bien dans ce groupe.\n\n" + vtg.brief())
        msg = f"Point du jour envoyé à {n} chat(s)." if n else "Aucun chat abonné (ajoutez le bot à un groupe et envoyez /start)."
    return RedirectResponse(f"/c/{code}/checkin/config?msg={msg}", status_code=303)
