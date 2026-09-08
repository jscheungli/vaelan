"""Connecteur Telegram (Bot API) — notifications d'équipe et assistant dans des groupes.

Clé : TELEGRAM_BOT_TOKEN (Render) ou section "telegram": {"botToken": "…"} de credentials.json (local).
Le webhook est /telegram/webhook/<secret> (secret dérivé du token, jamais le token lui-même) ;
Telegram renvoie aussi l'en-tête X-Telegram-Bot-Api-Secret-Token que l'on vérifie.
"""
import hashlib
import os
from typing import Optional

import httpx


def token() -> Optional[str]:
    return os.getenv("TELEGRAM_BOT_TOKEN") or None


def configured() -> bool:
    return bool(token())


def webhook_secret() -> str:
    return hashlib.sha256(("vaelan-telegram:" + (token() or "")).encode()).hexdigest()[:32]


def api(method: str, **params):
    """Appel Bot API ; renvoie (ok, result|description)."""
    if not token():
        return False, "TELEGRAM_BOT_TOKEN absent"
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"https://api.telegram.org/bot{token()}/{method}", json=params)
        d = r.json()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    return bool(d.get("ok")), (d.get("result") if d.get("ok") else d.get("description"))


def get_me():
    return api("getMe")


def get_webhook_info():
    return api("getWebhookInfo")


def set_webhook(url: str):
    return api("setWebhook", url=url, secret_token=webhook_secret(), allowed_updates=["message"],
               drop_pending_updates=False)


def send_message(chat_id, text: str, parse_mode: str = "HTML", reply_to: Optional[int] = None):
    """Envoie un message (découpé si > 4000 caractères). Renvoie (ok, info)."""
    if not text:
        return False, "texte vide"
    ok, info = True, ""
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    for k, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode, "disable_web_page_preview": True}
        if reply_to and k == 0:
            params["reply_to_message_id"] = reply_to
        ok, info = api("sendMessage", **params)
        if not ok and parse_mode:      # HTML mal formé -> renvoyer en texte brut
            ok, info = api("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True)
        if not ok:
            return False, str(info)[:200]
    return True, "envoyé"


def esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
