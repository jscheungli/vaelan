"""Brouillons Gmail créés par IMAP (APPEND dans [Gmail]/Brouillons) : ils apparaissent dans Gmail et Superhuman, l'utilisateur relit et envoie.
Clés : GMAIL_<CODE>_USER (adresse) et GMAIL_<CODE>_APP_PASSWORD (mot de passe d'application Google) — Render ou section "gmail" de credentials.json."""
import imaplib
import os
import re
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import List, Optional, Tuple


def _key(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", code.upper())


# comptes déclarés autrement que par GMAIL_<CODE>_USER / _APP_PASSWORD : {code: (adresse, nom de la variable du mot de passe)}
FALLBACK = {"OWINE": ("js@owine.co", "GMAIL_JS_AT_OWINE_CO_APPPWD")}


def account(code: str):
    user, pwd = os.getenv(f"GMAIL_{_key(code)}_USER"), os.getenv(f"GMAIL_{_key(code)}_APP_PASSWORD")
    if user and pwd:
        return user, pwd
    if code in FALLBACK and os.getenv(FALLBACK[code][1]):
        return FALLBACK[code][0], os.getenv(FALLBACK[code][1])
    return None, None


def configured(code: str) -> bool:
    return all(account(code))


def create_draft(code: str, to: List[str], subject: str, body: str, cc: Optional[List[str]] = None,
                 attachments: Optional[List[Tuple[str, bytes, str]]] = None, html: Optional[str] = None,
                 inline: Optional[List[Tuple[str, bytes, str]]] = None) -> Tuple[bool, str]:
    """Dépose un brouillon (texte, HTML optionnel avec images inline [(cid, bytes, mime)], pièces jointes) dans la boîte Gmail. Renvoie (ok, message)."""
    user, pwd = account(code)
    if not (user and pwd):
        return False, "Gmail non configuré (mot de passe d'application absent)"
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(to or [])
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
        for cid, data, ctype in (inline or []):
            maintype, _, subtype = (ctype or "image/png").partition("/")
            msg.get_payload()[-1].add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
    for fname, data, ctype in (attachments or []):
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=fname)
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(user, pwd)
        folder = None
        typ, boxes = M.list()
        for b in boxes or []:
            s = b.decode(errors="ignore")
            if "\\Drafts" in s:
                folder = s.split(' "/" ')[-1].strip().strip('"')
        folder = folder or "[Gmail]/Drafts"
        typ, _ = M.append(folder, r"(\Draft \Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        M.logout()
        return (typ == "OK"), ("brouillon créé" if typ == "OK" else f"IMAP {typ}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
