"""Envoi d'emails (SMTP, bibliothèque standard) — utilisé par les modules qui notifient des tiers.

Configuration par variables d'environnement (Render : dashboard → Environment) :
  SMTP_HOST (ex. smtp.postmarkapp.com)  SMTP_PORT (587 = STARTTLS, 465 = SSL)  SMTP_USER  SMTP_PASSWORD
  SMTP_FROM  (ex. « La Villa des Sables du Lagon <contact@villa-des-sables-du-lagon.com> »)
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


# ---------------------------------------------------------------- emails « de marque »
# Chaque module envoie AU NOM de la société concernée (« <Société> via Vaelan »), avec un en-tête
# société et un pied de page Vaelan discret et cliquable : le destinataire identifie la société,
# et le curieux découvre Vaelan (landing) — jamais l'inverse.
import html as _html
import re as _re

_FOOT = {
    "fr": ("Message envoyé via Vaelan pour {name}.", "Vaelan, des outils de gestion sur mesure développés par le cabinet Anvael.", "En savoir plus"),
    "en": ("Message sent via Vaelan on behalf of {name}.", "Vaelan, custom management tools developed by Anvael consulting.", "Learn more"),
}
VAELAN_URL = "https://vaelan.com"


def branded_from(brand: dict) -> str:
    """« La Villa des Sables du Lagon via Vaelan <villa-des-sables-du-lagon@vaelan.com> »."""
    name, addr = parseaddr(brand.get("address") or sender())
    label = brand.get("name") or name or "Vaelan"
    if "vaelan" not in label.lower():
        label = f"{label} via Vaelan"
    return formataddr((label, addr))


def _linkify(t: str) -> str:
    t = _html.escape(t)
    return _re.sub(r"(https?://[^\s<]+)", r'<a href="\1" style="color:{color};">\1</a>', t)


def branded_html(text: str, brand: dict, lang: str = "fr") -> str:
    """Corps texte -> HTML sobre : bandeau société (logo ou nom), paragraphes, pied Vaelan."""
    color = brand.get("color") or "#0A2540"
    f1, f2, more = _FOOT.get(lang, _FOOT["fr"])
    paras = "".join(f'<p style="margin:0 0 12px 0;">{_linkify(p).replace(chr(10), "<br>").replace("{color}", color)}</p>'
                    for p in text.strip().split("\n\n"))
    head = (f'<img src="{brand["logo"]}" alt="{_html.escape(brand.get("name") or "")}" style="max-height:64px;max-width:240px;">'
            if brand.get("logo") else f'<span style="font-size:18px;font-weight:700;color:{color};">{_html.escape(brand.get("name") or "")}</span>')
    site = brand.get("site")
    site_label = _html.escape(_re.sub(r"^https?://(www[.])?", "", site)) if site else ""
    site_html = f' · <a href="{site}" style="color:#6b7784;">{site_label}</a>' if site else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;background:#f4f6f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:24px 12px;"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1d2b3a;font-size:15px;line-height:1.5;">
<tr><td style="padding:26px 30px 8px 30px;text-align:center;border-bottom:1px solid #e6ebf0;">{head}</td></tr>
<tr><td style="padding:22px 30px 10px 30px;">{paras}</td></tr>
<tr><td style="padding:14px 30px 24px 30px;font-size:12px;color:#8a949e;border-top:1px solid #e6ebf0;">
{_html.escape(f1.format(name=brand.get("name") or ""))}{site_html}<br>
{_html.escape(f2)} <a href="{VAELAN_URL}" style="color:#0A2540;font-weight:600;">{_html.escape(more)} →</a>
</td></tr></table></td></tr></table></body></html>"""


def branded_text(text: str, brand: dict, lang: str = "fr") -> str:
    f1, f2, _ = _FOOT.get(lang, _FOOT["fr"])
    site = f" · {brand['site']}" if brand.get("site") else ""
    return f"{text.rstrip()}\n\n--\n{f1.format(name=brand.get('name') or '')}{site}\n{f2} {VAELAN_URL}\n"


def send_branded(to, subject: str, text: str, brand: dict, lang: str = "fr",
                 attachments=None, reply_to: Optional[str] = None, bcc=None) -> Tuple[bool, str]:
    """Envoi au nom d'une société : From « <Société> via Vaelan <adresse@vaelan.com> », HTML + texte,
    Reply-To = boîte de la société. brand = {name, address, reply_to, logo, site, color}."""
    return send(to, subject, branded_text(text, brand, lang), html=branded_html(text, brand, lang),
                attachments=attachments, reply_to=reply_to or brand.get("reply_to") or None, bcc=bcc,
                sender_override=branded_from(brand))
