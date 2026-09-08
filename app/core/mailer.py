"""Envoi d'emails (SMTP, bibliothèque standard) — utilisé par les modules qui notifient des tiers.

Configuration par variables d'environnement (Render : dashboard → Environment) :
  SMTP_HOST (ex. smtp.postmarkapp.com)  SMTP_PORT (587 = STARTTLS, 465 = SSL)  SMTP_USER  SMTP_PASSWORD
  SMTP_FROM  (ex. « Villa des Sables du Lagon <contact@villa-des-sables-du-lagon.com> »)
  SMTP_MESSAGE_STREAM (optionnel, Postmark : en-tête X-PM-Message-Stream, ex. « outbound » — inutile
  avec un SMTP Token, qui est déjà lié à un flux)
Postmark : SMTP_USER = Access Key, SMTP_PASSWORD = Secret Key du SMTP Token ; l'expéditeur (SMTP_FROM)
doit être une Sender Signature ou un domaine vérifié dans Postmark.
En local : section "smtp" de ~/.config/vaelan/credentials.json (chargée par localenv).
Sans configuration, send() renvoie (False, "SMTP non configuré") : les appelants journalisent
l'envoi comme « skipped » et proposent le lien à copier.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import List, Optional, Tuple


def configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))


def sender() -> str:
    return os.getenv("SMTP_FROM") or ""


def send(to: List[str], subject: str, text: str, html: Optional[str] = None,
         attachments: Optional[List[Tuple[str, bytes, str]]] = None,
         reply_to: Optional[str] = None, bcc: Optional[List[str]] = None,
         sender_override: Optional[str] = None) -> Tuple[bool, str]:
    """Envoie un email ; renvoie (ok, message). attachments = [(nom, bytes, content_type)].
    sender_override : expéditeur propre à un module (doit être vérifié chez le fournisseur SMTP)."""
    to = [t.strip() for t in (to or []) if t and t.strip()]
    if not to:
        return False, "destinataire vide"
    if not configured():
        return False, "SMTP non configuré (SMTP_HOST / SMTP_FROM)"
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT") or 587)
    user = os.getenv("SMTP_USER") or ""
    pwd = os.getenv("SMTP_PASSWORD") or ""
    name, addr = parseaddr(sender_override or sender())
    msg = EmailMessage()
    msg["From"] = formataddr((name, addr)) if name else addr
    if os.getenv("SMTP_MESSAGE_STREAM"):
        msg["X-PM-Message-Stream"] = os.getenv("SMTP_MESSAGE_STREAM")
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for fname, data, ctype in (attachments or []):
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=fname)
    rcpts = to + [b for b in (bcc or []) if b]
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as s:
                if user:
                    s.login(user, pwd)
                s.send_message(msg, from_addr=addr, to_addrs=rcpts)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, pwd)
                s.send_message(msg, from_addr=addr, to_addrs=rcpts)
        return True, "envoyé"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:300]
