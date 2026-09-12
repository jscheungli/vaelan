"""VDS — logique du check-in : réservations, réponses, fichiers, PDF récapitulatif, envois."""
import io
import json
import os
from urllib.parse import urlparse
import re
import secrets
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from app.core.db import engine
from app.core import mailer
from app.models import Setting, VdsReservation, VdsResponse, VdsFile, VdsMessage
from . import config
from .texts import t

_TZ = timedelta(hours=4)          # heure de La Réunion
CODE = config.COMPANY_CODE


def now_local() -> datetime:
    return datetime.utcnow() + _TZ


def fmt_dt(dt: Optional[datetime], lang: str = "fr") -> str:
    if not dt:
        return "—"
    d = dt + _TZ
    return d.strftime("%d/%m/%Y %H:%M") if lang == "fr" else d.strftime("%d %b %Y %H:%M")


def fmt_date(d, lang: str = "fr") -> str:
    if not d:
        return "—"
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except Exception:
            return d
    return d.strftime("%d/%m/%Y") if lang == "fr" else d.strftime("%d %b %Y")


# ------------------------------------------------------------------ réglages
def params() -> dict:
    p = dict(config.DEFAULT_PARAMS)
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == "checkin:params")).first()
    if st:
        try:
            p.update({k: v for k, v in json.loads(st.value).items() if k in p})
        except Exception:
            pass
    return p


def save_params(values: dict) -> None:
    p = params()
    p.update(values)
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == "checkin:params")).first()
        if not st:
            st = Setting(company_code=CODE, key="checkin:params", value="{}")
        st.value = json.dumps(p, ensure_ascii=False)
        st.updated_at = datetime.utcnow()
        s.add(st)
        s.commit()


def admin_url() -> str:
    """Racine du back-office et des webhooks : toujours Vaelan."""
    return (os.getenv("PUBLIC_BASE_URL") or "https://vaelan.com").rstrip("/")


def base_url() -> str:
    """Racine des liens VOYAGEURS : réglage `base_url` (config, ex. https://checkin.villa-des-sables-du-lagon.com), sinon Vaelan."""
    return (params().get("base_url") or admin_url()).rstrip("/")


def dedicated_host() -> bool:
    """Vrai quand les liens voyageurs sont servis sur le sous-domaine de la villa (raccourcis /<token>)."""
    return (urlparse(base_url()).hostname or "").lower() == config.PUBLIC_HOST


def public_url(res: VdsReservation) -> str:
    return f"{base_url()}/{res.token}" if dedicated_host() else f"{base_url()}/checkin/{res.token}"


def generic_url(channel: str) -> str:
    return f"{base_url()}/nouveau/{channel}" if dedicated_host() else f"{base_url()}/checkin/nouveau/{channel}"


def alert_emails() -> List[str]:
    raw = params().get("alert_emails") or ""
    return [e.strip() for e in re.split(r"[;,\s]+", raw) if "@" in e]


# ------------------------------------------------------------------ réservations
def new_token() -> str:
    return secrets.token_urlsafe(18)


def by_token(token: str) -> Optional[VdsReservation]:
    if not token or len(token) < 10:
        return None
    with Session(engine) as s:
        return s.exec(select(VdsReservation).where(VdsReservation.token == token)).first()


def create_reservation(channel: str, booking_ref: str = None, guest_name: str = None, guest_email: str = None,
                       guest_phone: str = None, arrival=None, departure=None, guests: int = None,
                       lang: str = "fr", source: str = "manual", lodgify_id: int = None, notes: str = None,
                       raw: str = None, status: str = "pending") -> VdsReservation:
    def _d(x):
        if not x:
            return None
        return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])
    r = VdsReservation(channel=channel if channel in config.CHANNELS else "lodgify",
                       booking_ref=(booking_ref or "").strip() or None, guest_name=(guest_name or "").strip() or None,
                       guest_email=(guest_email or "").strip().lower() or None, guest_phone=(guest_phone or "").strip() or None,
                       arrival=_d(arrival), departure=_d(departure), guests=guests or None,
                       lang="en" if str(lang).lower().startswith("en") else "fr", source=source,
                       lodgify_id=lodgify_id, notes=notes, raw=raw, token=new_token(), status=status)
    with Session(engine) as s:
        s.add(r)
        s.commit()
        s.refresh(r)
    return r


def update_reservation(res_id: int, **fields) -> None:
    with Session(engine) as s:
        r = s.get(VdsReservation, res_id)
        if not r:
            return
        for k, v in fields.items():
            setattr(r, k, v)
        r.updated_at = datetime.utcnow()
        s.add(r)
        s.commit()


def latest_response(res_id: int) -> Optional[VdsResponse]:
    with Session(engine) as s:
        return s.exec(select(VdsResponse).where(VdsResponse.reservation_id == res_id)
                      .order_by(VdsResponse.id.desc())).first()


# ------------------------------------------------------------------ fichiers
def _shrink_image(data: bytes, max_px: int = 1800, quality: int = 82) -> Tuple[bytes, str]:
    """Réduit une photo (EXIF respecté) en JPEG ; renvoie (bytes, content_type)."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((max_px, max_px))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue(), "image/jpeg"


def store_file(kind: str, name: str, data: bytes, content_type: str, reservation_id: int = None,
               response_id: int = None, shrink: bool = False) -> VdsFile:
    if shrink and (content_type or "").startswith("image/"):
        try:
            data, content_type = _shrink_image(data)
            name = re.sub(r"\.[A-Za-z0-9]+$", "", name) + ".jpg"
        except Exception:
            pass          # format non lu par Pillow (HEIC sans plugin…) -> stocké tel quel
    f = VdsFile(reservation_id=reservation_id, response_id=response_id, kind=kind, name=name[:120],
                content_type=content_type or "application/octet-stream", size=len(data), data=data)
    with Session(engine) as s:
        s.add(f)
        s.commit()
        s.refresh(f)
    return f


def files_for(reservation_id: int, kind: str = None) -> List[VdsFile]:
    with Session(engine) as s:
        q = select(VdsFile).where(VdsFile.reservation_id == reservation_id)
        if kind:
            q = q.where(VdsFile.kind == kind)
        return list(s.exec(q.order_by(VdsFile.id)).all())


def reference_file(kind: str) -> Optional[VdsFile]:
    """Document de référence (reservation_id vide) : état des risques (kind erp)."""
    with Session(engine) as s:
        return s.exec(select(VdsFile).where(VdsFile.reservation_id == None, VdsFile.kind == kind)  # noqa: E711
                      .order_by(VdsFile.id.desc())).first()


def set_reference_file(kind: str, name: str, data: bytes, content_type: str) -> VdsFile:
    with Session(engine) as s:
        for f in s.exec(select(VdsFile).where(VdsFile.reservation_id == None, VdsFile.kind == kind)).all():  # noqa: E711
            s.delete(f)
        s.commit()
    return store_file(kind, name, data, content_type)


# ------------------------------------------------------------------ Telegram (équipe)
def _tg_esc(x) -> str:
    return str(x if x is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_test(res) -> bool:
    """Réservation du banc de test (source « test ») : tous ses envois vont à l'adresse de test, préfixés [TEST]."""
    return bool(res is not None and getattr(res, "source", None) == "test")


def _tg_notify(text: str, res=None) -> None:
    try:
        from . import telegram as _vtg
        _vtg.notify(("🧪 <b>TEST</b> — " if is_test(res) else "") + text)
    except Exception as e:
        print(f"[telegram] notification : {e}")


# ------------------------------------------------------------------ journal des envois
def log_message(reservation_id: Optional[int], kind: str, to: str, subject: str, body: str,
                ok: bool, info: str, channel: str = "email", sender: str = None) -> None:
    with Session(engine) as s:
        s.add(VdsMessage(reservation_id=reservation_id, kind=kind, channel=channel, to=to, subject=subject, sender=sender,
                         body=(body or "")[:4000], status="sent" if ok else ("skipped" if "non configuré" in info else "error"),
                         error=None if ok else info))
        s.commit()


def recent_messages(n: int = 20) -> List[VdsMessage]:
    with Session(engine) as s:
        return list(s.exec(select(VdsMessage).order_by(VdsMessage.id.desc()).limit(n)).all())


def log_manual(res: VdsReservation, channel: str, text: str, by_user: str, kind: str = None, mode: str = None) -> None:
    """Invitation (premier contact) ou relance envoyée À LA MAIN (SMS, WhatsApp, messagerie Airbnb/Booking, téléphone) :
    journalisée avec son texte, son auteur et sa nature, et la réservation avance (sent / reminded)."""
    mode = mode if mode in ("invite", "remind") else ("invite" if res.status == "pending" else "remind")
    kind = kind or ("invitation_manual" if mode == "invite" else "reminder_manual")
    with Session(engine) as s:
        s.add(VdsMessage(reservation_id=res.id, kind=kind, channel=channel, to=res.guest_phone or res.guest_email or "",
                         subject=f"{'Invitation (premier contact)' if mode == 'invite' else 'Relance'} manuelle ({channel})",
                         body=(text or "")[:4000], status="sent", sender=by_user, by_user=by_user))
        s.commit()
    now = datetime.utcnow()
    if res.status == "pending":
        update_reservation(res.id, status="sent", invited_at=now)
    elif res.status in ("sent", "reminded"):
        update_reservation(res.id, status="reminded", reminded_at=now, reminder_count=(res.reminder_count or 0) + 1)


def suggested_texts(res: VdsReservation) -> dict:
    """Textes prêts à copier pour un envoi manuel par SMS / WhatsApp : premier contact et relance."""
    v = _mail_vars(res)
    v["name"] = v["name"] or ""
    v["phone"] = f" ({res.guest_phone})" if res.guest_phone else ""
    reminder = res.status in ("sent", "reminded")
    fix = lambda x: x.replace("Bonjour ,", "Bonjour,").replace("Hello ,", "Hello,")
    # textes à copier pour SMS / WhatsApp (toujours avec le lien) ; par email, c'est le bouton « Inviter / relancer par email »
    # mode par défaut : premier contact tant que rien n'a été envoyé, relance ensuite ; les deux textes sont proposés
    return {"invite": fix(t(res.lang, "sms_invite", **v)), "remind": fix(t(res.lang, "sms_remind", **v)), "mode": "remind" if reminder else "invite"}


def messages_for(reservation_id: int) -> List[VdsMessage]:
    with Session(engine) as s:
        return list(s.exec(select(VdsMessage).where(VdsMessage.reservation_id == reservation_id)
                           .order_by(VdsMessage.id.desc())).all())


def _mail_vars(res: VdsReservation) -> dict:
    return {"name": (res.guest_name or "").split(" ")[0] or ("" if res.lang == "fr" else ""),
            "ref": res.booking_ref or f"#{res.id}", "arrival": fmt_date(res.arrival, res.lang),
            "departure": fmt_date(res.departure, res.lang), "url": public_url(res),
            "checkin": config.VILLA["checkin"], "checkout": config.VILLA["checkout"]}


def brand() -> dict:
    """Identité d'envoi du module : la villa en expéditeur (« … via Vaelan »), réponses vers sa boîte."""
    p = params()
    return {"name": config.VILLA["name"], "address": p.get("from_email") or config.FROM_ADDRESS,
            "reply_to": p.get("reply_to") or None, "logo": f"{admin_url()}/static/vds/logo.png",
            "site": config.VILLA["site"], "color": config.VILLA["color"]}


def beta_redirect() -> Optional[str]:
    """Adresse de test si le mode bêta est actif (tous les emails voyageurs y sont redirigés)."""
    p = params()
    return (p.get("test_email") or "").strip() if p.get("beta") else None


def guest_send(res: VdsReservation, kind: str, to: str, subject: str, body: str,
               attachments=None) -> Tuple[bool, str]:
    """Envoi d'un email au VOYAGEUR, journalisé. En mode bêta, redirigé vers l'adresse de test
    (sujet préfixé du destinataire réel) — rien ne part au client."""
    test = beta_redirect()
    real_to = to
    if is_test(res):
        to, test = (params().get("test_email") or to), None
        subject = f"[TEST] {subject}"
    if test:
        subject = f"[BÊTA → {to}] {subject}"
        body = f"*** MODE BÊTA — ce message était destiné à {to} ; il vous est redirigé pour validation. ***\n\n" + body
        to = test
    b = brand()
    ok, info = mailer.send_branded([to], subject, body, b, lang=res.lang or "fr", attachments=attachments)
    log_message(res.id, kind, f"{to} (bêta · réel : {real_to})" if test else to, subject, body, ok, info, sender=mailer.branded_from(b))
    return ok, info


def send_invitation(res: VdsReservation, reminder: bool = False) -> Tuple[bool, str]:
    """Invitation (ou relance) par email au voyageur. Sans email : journalisé « skipped »."""
    kind = "reminder" if reminder else "invitation"
    v = _mail_vars(res)
    v["name"] = v["name"] or ("" if res.lang == "en" else "")
    subject = t(res.lang, "mail_remind_subject" if reminder else "mail_invite_subject", **v)
    body = t(res.lang, "mail_remind_body" if reminder else "mail_invite_body", **v).replace("Bonjour ,", "Bonjour,").replace("Hello ,", "Hello,")
    if not res.guest_email:
        log_message(res.id, kind, "", subject, body, False, "pas d'email : envoyer le lien via la messagerie de la plateforme")
        return False, "pas d'email"
    ok, info = guest_send(res, kind, res.guest_email, subject, body)
    if ok:
        now = datetime.utcnow()
        if reminder:
            update_reservation(res.id, status="reminded", reminded_at=now, reminder_count=(res.reminder_count or 0) + 1)
        else:
            update_reservation(res.id, status="sent", invited_at=now)
    return ok, info


def send_self_link(res: VdsReservation, email: str) -> Tuple[bool, str]:
    """Le voyageur demande, depuis le formulaire, à recevoir son lien par email (pas de pièce sous la main, plus pratique
    sur ordinateur). L'adresse saisie devient l'email de la réservation (pré-remplie ensuite dans le formulaire).
    Garde-fou : un envoi par minute et par réservation."""
    email = (email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        return False, "invalid"
    last = next((m for m in messages_for(res.id) if m.kind == "self_link"), None)
    if last and (datetime.utcnow() - last.sent_at).total_seconds() < 60:
        return True, "déjà envoyé il y a moins d'une minute"
    if email != (res.guest_email or ""):
        update_reservation(res.id, guest_email=email)
        res = by_token(res.token)
    v = _mail_vars(res)
    v["name"] = v["name"] or ""
    fix = lambda x: x.replace("Bonjour ,", "Bonjour,").replace("Hello ,", "Hello,")
    return guest_send(res, "self_link", email, t(res.lang, "mail_self_subject", **v), fix(t(res.lang, "mail_self_body", **v)))


def send_alert(res: Optional[VdsReservation], subject: str, body: str, kind: str = "alert") -> Tuple[bool, str]:
    """Alerte interne (propriétaire / gestionnaire). Réservation de test : adresse de test seulement."""
    to = alert_emails()
    if is_test(res):
        to = [x for x in [(params().get("test_email") or "").strip()] if x]
        subject = f"[TEST] {subject}"
    if not to:
        log_message(res.id if res else None, kind, "", subject, body, False, "alertes : aucun destinataire (configuration)")
        return False, "aucun destinataire"
    b = brand()
    ok, info = mailer.send_branded(to, subject, body, b, lang="fr")
    log_message(res.id if res else None, kind, ", ".join(to), subject, body, ok, info, sender=mailer.branded_from(b))
    return ok, info


def send_new_booking_alert(res: VdsReservation, reason: str) -> Tuple[bool, str]:
    """Nouvelle réservation qui ne recevra PAS d'invitation automatique : alerte à la personne qui invite
    à la main (Marie), avec le texte prêt à copier. En mode bêta, redirigée vers l'adresse de test."""
    p = params()
    to = [e.strip() for e in re.split(r"[;,\s]+", p.get("new_booking_emails") or "") if "@" in e]
    label = config.CHANNELS.get(res.channel, {}).get("label", res.channel)
    txt = suggested_texts(res)["invite"]
    subject = f"[VDS] Nouvelle réservation {label} à inviter — {res.guest_name or '?'} · {fmt_date(res.arrival)} → {fmt_date(res.departure)}"
    body = (f"Bonjour,\n\nUne nouvelle réservation vient d'arriver et ne recevra pas d'invitation automatique ({reason}).\n\n"
            f"Canal : {label}\nVoyageur : {res.guest_name or '?'}\nSéjour : {fmt_date(res.arrival)} → {fmt_date(res.departure)} · {res.guests or '?'} personnes\n"
            f"Référence : {res.booking_ref or res.id}\nTéléphone : {res.guest_phone or '— inconnu'}\nEmail : {res.guest_email or '— inconnu'}\n\n"
            f"Merci de lui envoyer le lien du formulaire par SMS ou WhatsApp au {res.guest_phone or 'numéro indiqué dans la réservation'}"
            f"{' (la messagerie Airbnb bloque les liens)' if res.channel == 'airbnb' else ''}, puis d'enregistrer l'envoi sur sa fiche de suivi Vaelan "
            f"(accès collaborateurs) : {admin_url()}/c/VDS/checkin/{res.id}\n\n"
            f"Formulaire du voyageur (lien public) : {public_url(res)}\n\nMessage suggéré (SMS / WhatsApp, premier contact) :\n"
            f"----------------------------------------\n{txt}\n----------------------------------------\n")
    _tg_notify(f"🆕 <b>Nouvelle réservation {_tg_esc(label)}</b> à inviter à la main — {_tg_esc(res.guest_name or '?')} · "
               f"{fmt_date(res.arrival)} → {fmt_date(res.departure)} · {res.guests or '?'} pers. · tél {_tg_esc(res.guest_phone or '—')}\n"
               f"Fiche (message prêt à copier) : {admin_url()}/c/VDS/checkin/{res.id}", res=res)
    test = beta_redirect()
    real = ", ".join(to)
    if is_test(res):
        to, test, subject = [x for x in [(params().get("test_email") or "").strip()] if x], None, f"[TEST] {subject}"
    if test:
        subject = f"[BÊTA → {real or '—'}] {subject}"
        body = f"*** MODE BÊTA — cette alerte était destinée à {real or '—'}. ***\n\n" + body
        to = [test]
    if not to:
        log_message(res.id, "new_booking", "", subject, body, False, "nouvelle réservation : aucun destinataire (configuration)")
        return False, "aucun destinataire"
    b = brand()
    ok, info = mailer.send_branded(to, subject, body, b, lang="fr")
    log_message(res.id, "new_booking", f"{to[0]} (bêta · réel : {real})" if test else real, subject, body, ok, info, sender=mailer.branded_from(b))
    return ok, info


def send_prearrival_alert(res: VdsReservation, days_left: int) -> Tuple[bool, str]:
    """Alerte interne « arrivée dans N jours sans formulaire » (J-7 / J-2 par défaut)."""
    label = config.CHANNELS[res.channel]["label"]
    return send_alert(
        res, f"[VDS] Arrivée dans {days_left} j SANS formulaire — {res.guest_name or '?'} · {label}",
        f"La réservation {res.booking_ref or res.id} ({label}) arrive le {fmt_date(res.arrival)} "
        f"et le formulaire d'arrivée n'est pas complété (statut : {res.status}, {res.reminder_count or 0} relance(s)).\n"
        f"Email : {res.guest_email or 'AUCUN — envoyer le lien via la messagerie de la plateforme'}\n"
        f"Formulaire du voyageur (lien public) : {public_url(res)}\n"
        f"Fiche de suivi Vaelan (accès collaborateurs) : {admin_url()}/c/VDS/checkin/{res.id}\n")


# ------------------------------------------------------------------ banc de test bout en bout
TEST_SET = [  # (canal, langue, nom, référence)
    ("airbnb", "en", "Test Airbnb", "TEST-AIRBNB"), ("booking", "fr", "Test Booking", "TEST-BOOKING"),
    ("abritel", "fr", "Test Abritel", "TEST-ABRITEL"), ("lodgify", "fr", "Test Site direct", "TEST-DIRECT")]


def test_reservations() -> List[VdsReservation]:
    with Session(engine) as s:
        return list(s.exec(select(VdsReservation).where(VdsReservation.source == "test").order_by(VdsReservation.id)).all())


def create_test_set() -> List[VdsReservation]:
    """Une réservation de test par canal (email = adresse de test, arrivée dans 10 jours) ; ne recrée pas celles qui existent."""
    email = (params().get("test_email") or "").strip()
    have = {r.channel for r in test_reservations()}
    today = now_local().date()
    out = []
    for ch, lang, name, ref in TEST_SET:
        if ch in have:
            continue
        # comme dans la réalité : seul le site direct (Lodgify) fournit l'email ; les plateformes ne donnent que le téléphone
        out.append(create_reservation(ch, booking_ref=ref, guest_name=name, guest_email=email if ch == "lodgify" else None, guest_phone="+262 692 00 00 00",
                                      arrival=today + timedelta(days=10), departure=today + timedelta(days=17), guests=4,
                                      lang=lang, source="test", notes="Banc de test bout en bout — à purger après validation"))
    return out


def set_test_email(rid: int) -> None:
    """Le voyageur nous a communiqué son email (messagerie plateforme, téléphone…) : on le renseigne → relances par email possibles."""
    with Session(engine) as s:
        r = s.get(VdsReservation, rid)
        if r and r.source == "test":
            r.guest_email = (params().get("test_email") or "").strip() or None
            s.add(r)
            s.commit()


def reset_test_reservation(rid: int) -> None:
    """Remet une réservation de test à « à inviter » et efface ses réponses, fichiers et envois."""
    with Session(engine) as s:
        r = s.get(VdsReservation, rid)
        if not r or r.source != "test":
            return
        for m in (VdsFile, VdsResponse, VdsMessage):
            for x in s.exec(select(m).where(m.reservation_id == rid)).all():
                s.delete(x)
        r.status, r.invited_at, r.reminded_at, r.reminder_count, r.alerted_at, r.completed_at, r.erp_sent_at = "pending", None, None, 0, None, None, None
        r.guest_email = ((params().get("test_email") or "").strip() or None) if r.channel == "lodgify" else None
        s.add(r)
        s.commit()


def purge_test_set() -> int:
    """Supprime toutes les réservations de test avec leurs réponses, fichiers et envois."""
    n = 0
    with Session(engine) as s:
        for r in s.exec(select(VdsReservation).where(VdsReservation.source == "test")).all():
            for m in (VdsFile, VdsResponse, VdsMessage):
                for x in s.exec(select(m).where(m.reservation_id == r.id)).all():
                    s.delete(x)
            s.delete(r)
            n += 1
        s.commit()
    return n


def send_erp(res: VdsReservation) -> Tuple[bool, str]:
    erp = reference_file("erp")
    if not erp:
        return False, "état des risques non chargé"
    if not res.guest_email:
        return False, "pas d'email"
    v = _mail_vars(res)
    subject = t(res.lang, "mail_erp_subject", **v)
    body = t(res.lang, "mail_erp_body", **v).replace("Bonjour ,", "Bonjour,").replace("Hello ,", "Hello,")
    ok, info = guest_send(res, "erp", res.guest_email, subject, body, attachments=[(erp.name, erp.data, erp.content_type)])
    if ok:
        update_reservation(res.id, erp_sent_at=datetime.utcnow())
    return ok, info


# ------------------------------------------------------------------ réponse au formulaire
def _clean(s, n=300):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def save_response(res: VdsReservation, form: dict, uploads: list, ip: str, ua: str,
                  signature_png: bytes) -> Tuple[VdsResponse, VdsFile]:
    """Enregistre une réponse complète (règles acceptées, identité, pièces, consentements, signature),
    met la réservation en `completed`, génère le PDF récapitulatif et envoie confirmation + alerte."""
    lang = "en" if form.get("lang") == "en" else "fr"
    rules = {r["key"]: form.get(f"rule_{r['key']}") == "yes" for r in config.RULES}
    confirms = {r["key"]: bool(form.get(f"confirm_{r['key']}")) for r in config.RULES if r["confirm"]}
    marketing = form.get("marketing") if form.get("marketing") in ("email", "sms", "both", "none") else "none"
    address = ", ".join(x for x in [_clean(form.get("street")), " ".join(x for x in [_clean(form.get("postal_code"), 12), _clean(form.get("city"), 80)] if x),
                                    _clean(form.get("country"), 60)] if x)
    data = {
        "rules": rules, "confirms": confirms,
        "full_name": _clean(form.get("full_name"), 120), "birth_date": "", "birth_place": "",   # plus demandés (pièce d'identité jointe)
        "nationality": _clean(form.get("nationality"), 60),
        "street": _clean(form.get("street")), "postal_code": _clean(form.get("postal_code"), 12),
        "city": _clean(form.get("city"), 80), "country": _clean(form.get("country"), 60), "address": address,
        "email": _clean(form.get("email"), 120).lower(), "phone": _clean(form.get("phone"), 30),
        "arrival_time": _clean(form.get("arrival_time"), 20), "occupants": _clean(form.get("occupants"), 3) or str(res.guests or ""),
        "occupants_list": " · ".join(x.strip() for x in str(form.get("occupants_list") or "").splitlines() if x.strip())[:1200],
        "erp": bool(form.get("erp")), "marketing": marketing,
        "mkt_email": _clean(form.get("mkt_email"), 120).lower() if marketing in ("email", "both") else "",
        "mkt_phone": _clean(form.get("mkt_phone"), 30) if marketing in ("sms", "both") else "",
        "rgpd": bool(form.get("rgpd")), "lang": lang, "channel": res.channel,
    }
    resp = VdsResponse(reservation_id=res.id, channel=res.channel, lang=lang, ip=ip, user_agent=(ua or "")[:300],
                       rules_ok=all(rules.values()), refused_rules=",".join(k for k, v in rules.items() if not v) or None,
                       full_name=data["full_name"], email=data["email"] or res.guest_email, phone=data["phone"],
                       birth_date=data["birth_date"], birth_place=data["birth_place"], address=address,
                       marketing=marketing, data=json.dumps(data, ensure_ascii=False), signed=bool(signature_png))
    with Session(engine) as s:
        s.add(resp)
        s.commit()
        s.refresh(resp)
    sig = store_file("signature", "signature.png", signature_png, "image/png", res.id, resp.id)
    for (name, blob, ctype) in uploads:
        store_file("id_document", name, blob, ctype, res.id, resp.id, shrink=True)
    # la réservation hérite des coordonnées saisies (utile pour les envois suivants)
    upd = {"status": "completed", "completed_at": datetime.utcnow()}
    if data["full_name"]:
        upd["guest_name"] = data["full_name"]
    if data["email"] and not res.guest_email:
        upd["guest_email"] = data["email"]
    if data["phone"] and not res.guest_phone:
        upd["guest_phone"] = data["phone"]
    if data["occupants"].isdigit():
        upd["guests"] = int(data["occupants"])
    update_reservation(res.id, **upd)
    res = by_token(res.token)
    pdf = build_recap_pdf(res, resp, sig.data)
    recap = store_file("recap", f"Formulaire arrivee {res.booking_ref or res.id} - {resp.full_name}.pdf", pdf,
                       "application/pdf", res.id, resp.id)
    # confirmation au voyageur + alerte interne
    v = _mail_vars(res)
    v["date"] = fmt_dt(resp.submitted_at, lang)
    v["file"] = recap.name
    to = data["email"] or res.guest_email
    if to:
        subject = t(lang, "mail_confirm_subject", **v)
        body = t(lang, "mail_confirm_body", **v).replace("Bonjour ,", "Bonjour,").replace("Hello ,", "Hello,")
        guest_send(res, "confirmation", to, subject, body, attachments=[(recap.name, pdf, "application/pdf")])
    _tg_notify(f"✅ <b>Formulaire signé</b> — {_tg_esc(resp.full_name or res.guest_name)} · {_tg_esc(config.CHANNELS[res.channel]['label'])} · "
               f"{fmt_date(res.arrival)} → {fmt_date(res.departure)} · {data['occupants'] or res.guests or '?'} pers."
               + (f" · arrivée prévue {_tg_esc(data['arrival_time'])}" if data.get("arrival_time") else "")
               + f"\nFiche : {admin_url()}/c/VDS/checkin/{res.id}", res=res)
    send_alert(res, f"[VDS] Formulaire d'arrivée signé — {res.guest_name} · {config.CHANNELS[res.channel]['label']} · "
                    f"{fmt_date(res.arrival)} → {fmt_date(res.departure)}",
               f"Réservation {res.booking_ref or res.id} ({config.CHANNELS[res.channel]['label']})\n"
               f"Voyageur : {resp.full_name} · {resp.email or '—'} · {resp.phone or '—'}\n"
               f"Séjour : {fmt_date(res.arrival)} → {fmt_date(res.departure)} · {data['occupants'] or res.guests or '?'} personnes · "
               f"arrivée prévue {data['arrival_time'] or '?'}\n"
               f"Règles : {'toutes acceptées' if resp.rules_ok else 'REFUS : ' + resp.refused_rules}\n"
               f"Marketing : {marketing}\nAttestation signée : {recap.name} (jointe à la confirmation envoyée au voyageur)\n"
               f"Pièce(s) d'identité : {len(uploads)} fichier(s)\n\n"
               f"Fiche de suivi Vaelan (accès collaborateurs) : {admin_url()}/c/VDS/checkin/{res.id}\n",
               kind="alert")
    return resp, recap


def save_refusal(res: VdsReservation, refused: List[str], lang: str, ip: str) -> None:
    keys = [k for k in refused if k in {r["key"] for r in config.RULES}]
    resp = VdsResponse(reservation_id=res.id, channel=res.channel, lang=lang, ip=ip, rules_ok=False,
                       refused_rules=",".join(keys) or "?", data=json.dumps({"refused": keys, "lang": lang}), signed=False)
    with Session(engine) as s:
        s.add(resp)
        s.commit()
    update_reservation(res.id, status="refused")
    labels = "; ".join(next((r["fr"] for r in config.RULES if r["key"] == k), k)[:80] for k in keys)
    _tg_notify(f"⛔ <b>Règles refusées</b> — {_tg_esc(res.guest_name or '?')} · {fmt_date(res.arrival)} → {fmt_date(res.departure)} · annulation demandée.\n"
               f"Refus : {_tg_esc(labels)}\nFiche : {admin_url()}/c/VDS/checkin/{res.id}", res=res)
    send_alert(res, f"[VDS] ⚠️ Règles REFUSÉES — {res.guest_name or '?'} · {fmt_date(res.arrival)} — annulation demandée",
               f"Le voyageur {res.guest_name or '?'} (réservation {res.booking_ref or res.id}, {config.CHANNELS[res.channel]['label']}, "
               f"séjour {fmt_date(res.arrival)} → {fmt_date(res.departure)}) a répondu NON à : {labels}\n"
               f"et a demandé l'annulation via le formulaire.\n\nFormulaire du voyageur (lien public) : {public_url(res)}\n"
               f"Fiche de suivi Vaelan (accès collaborateurs) : {admin_url()}/c/VDS/checkin/{res.id}\n", kind="refusal")


# ------------------------------------------------------------------ PDF récapitulatif
def rule_rows(d: dict, lang: str = "fr"):
    """Règles telles que posées au voyageur : (texte, réponse OUI/NON/—, confirmation de sanction cochée).
    Réponses importées (ancien formulaire) : `legacy_rules` = [(texte, oui/non)] tel quel."""
    if d.get("legacy_rules"):
        return [(t, ("OUI" if ok else "NON"), False) for t, ok in d["legacy_rules"]]
    rules, confirms, out = d.get("rules") or {}, d.get("confirms") or {}, []
    for r in config.RULES:
        if r["key"] not in rules:
            out.append((r.get(lang) or r["fr"], "—", False))          # règle ajoutée après cette réponse
        else:
            out.append((r.get(lang) or r["fr"], "OUI" if rules[r["key"]] else "NON", bool(r["confirm"] and confirms.get(r["key"]))))
    return out


def _badge(ans: str) -> str:
    """Réponse OUI (fond vert) / NON (fond rouge) / — dans l'attestation PDF."""
    return {"OUI": "<span class='yes'>OUI</span>", "NON": "<span class='no'>NON</span>"}.get(ans, ans)


def build_recap_pdf(res: VdsReservation, resp: VdsResponse, signature_png: Optional[bytes]) -> bytes:
    """Attestation formelle : règles acceptées une à une, identité, consentements, signature horodatée."""
    import fitz
    d = json.loads(resp.data or "{}")
    lang = resp.lang or "fr"
    ch = config.CHANNELS.get(res.channel, {}).get("label", res.channel)
    esc = lambda x: (str(x if x not in (None, "") else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    conf_txt = {r["key"]: (r.get("confirm_" + lang) or r.get("confirm_fr")) for r in config.RULES if r["confirm"]}
    rows = ""
    for (txt, ans, confirmed), r in zip(rule_rows(d, lang), (config.RULES if not d.get("legacy_rules") else [None] * 99)):
        extra = ("<br/><span class='c'>☑ " + esc(conf_txt.get(r["key"])) + "</span>") if (r and confirmed) else ""
        rows += f"<tr><td class='q'>{esc(txt)}</td><td class='a'>{_badge(ans)}{extra}</td></tr>"
    mk = dict((m[0], m[1] if lang == "fr" else m[2]) for m in config.MARKETING)
    ident = [(t(lang, "full_name"), d.get("full_name"))]
    for k in ("birth_date", "birth_place"):                    # anciennes réponses seulement (plus demandés)
        if d.get(k):
            ident.append((t(lang, k), fmt_date(d.get(k), lang) if k == "birth_date" else d.get(k)))
    ident += [(t(lang, "nationality"), d.get("nationality")),
              ("Adresse" if lang == "fr" else "Address", d.get("address")), (t(lang, "email"), d.get("email") or res.guest_email),
              ("Téléphone" if lang == "fr" else "Phone", d.get("phone")), (t(lang, "arrival_time"), d.get("arrival_time")),
              ("Occupants", str(d.get("occupants") or res.guests or "") + ((" — " + d.get("occupants_list")) if d.get("occupants_list") else ""))]
    ident_rows = "".join(f"<tr><td class='q'>{esc(k)}</td><td class='a'>{esc(v)}</td></tr>" for k, v in ident)
    id_files = [f.name for f in files_for(res.id, "id_document") if f.response_id == resp.id]
    n_id = len(id_files)
    cons = [
        ("État des risques (ERP)" if lang == "fr" else "Risk assessment (ERP)", "OUI" if d.get("erp") else "NON"),
        ("Offres promotionnelles" if lang == "fr" else "Promotional offers", mk.get(d.get("marketing"), d.get("marketing"))
         + ((" — " + d.get("mkt_email")) if d.get("mkt_email") else "") + ((" — " + d.get("mkt_phone")) if d.get("mkt_phone") else "")),
        ("RGPD", "OUI" if d.get("rgpd") else "NON"),
        ("Pièce d'identité jointe" if lang == "fr" else "ID document attached", (f"{n_id} fichier(s) : " + ", ".join(id_files)) if n_id else ("non demandée" if not config.CHANNELS[res.channel]["id_required"] else "—")),
    ]
    cons_rows = "".join(f"<tr><td class='q'>{esc(k)}</td><td class='a'>{_badge(v) if v in ('OUI', 'NON') else esc(v)}</td></tr>" for k, v in cons)
    when = fmt_dt(resp.submitted_at, lang)
    title = ("Attestation d'acceptation des règles et conditions de location" if lang == "fr"
             else "Certificate of acceptance of the rules and rental conditions")
    legal = (f"Le locataire principal reconnaît avoir pris connaissance de chacune des règles ci-dessus, y avoir répondu « OUI », "
             f"s'être engagé à les respecter et à les faire respecter par l'ensemble des occupants, et avoir signé électroniquement "
             f"le présent document le {when} (heure de La Réunion, adresse IP {resp.ip or '—'}). {config.VILLA['legal']} se réserve le droit "
             f"de s'en prévaloir en cas de manquement, notamment pour la retenue totale ou partielle de la caution et la fin anticipée du séjour."
             if lang == "fr" else
             f"The main tenant acknowledges having read each of the rules above, having answered “YES” to them, having committed to comply with "
             f"them and to ensure all occupants do too, and having electronically signed this document on {when} (Réunion time, IP address "
             f"{resp.ip or '—'}). {config.VILLA['legal']} reserves the right to rely on it in case of breach, in particular to withhold the "
             f"security deposit in full or in part and to end the stay early.")
    if d.get("imported_from") == "surveysparrow":
        legal += (" Réponse recueillie via l'ancien formulaire en ligne (SurveySparrow) et reprise à l'identique." if lang == "fr"
                  else " Answer collected through the previous online form (SurveySparrow) and reproduced as is.")
    sig_html = "<div><img src='signature.png' width='220'/></div>" if signature_png else \
        ("<p class='meta'>Signature manuscrite non disponible dans l'export repris.</p>" if lang == "fr" else "<p class='meta'>Handwritten signature not available in the imported export.</p>")
    html = f"""
    <div class='hdr'><img src='logo.png' width='170'/></div>
    <h1>{title}</h1>
    <p class='meta'>{esc(config.VILLA['legal'])} · {esc(config.VILLA['rcs'])}<br/>
    {'Réservation' if lang=='fr' else 'Booking'} <b>{esc(res.booking_ref or res.id)}</b> · {esc(ch)} ·
    {'séjour du' if lang=='fr' else 'stay from'} <b>{fmt_date(res.arrival, lang)}</b> {'au' if lang=='fr' else 'to'} <b>{fmt_date(res.departure, lang)}</b></p>
    <h2>{'Règles de la villa acceptées' if lang=='fr' else 'Accepted villa rules'}</h2>
    <table>{rows}</table>
    <h2>{'Locataire principal' if lang=='fr' else 'Main tenant'}</h2>
    <table>{ident_rows}</table>
    <h2>{'Engagements et consentements' if lang=='fr' else 'Commitments and consents'}</h2>
    <table>{cons_rows}</table>
    <h2>{'Engagement et signature' if lang=='fr' else 'Commitment and signature'}</h2>
    <p class='legal'>{esc(legal)}</p>
    <p class='meta'>{'Signé le' if lang=='fr' else 'Signed on'} {when} ({'heure de La Réunion' if lang=='fr' else 'Réunion time'}) · IP {esc(resp.ip)}<br/>
    {esc(t(lang, 'sign_legal'))}</p>
    {sig_html}
    <p class='foot'>{esc(config.VILLA['name'])} · {esc(config.VILLA['contact_email'])} · {'document généré par' if lang=='fr' else 'document generated by'} Vaelan</p>
    """
    css = """
    body { font-family: sans-serif; font-size: 9.5pt; color: #1d2b3a; }
    h1 { font-size: 15pt; color: #234159; margin: 6pt 0 2pt 0; }
    h2 { font-size: 11pt; color: #234159; margin: 12pt 0 4pt 0; border-bottom: 0.6pt solid #234159; }
    p.meta { color: #55606b; font-size: 8.5pt; margin: 2pt 0; }
    p.foot { color: #8a949e; font-size: 7.5pt; margin-top: 14pt; }
    p.legal { font-size: 9pt; color: #1d2b3a; border: 0.6pt solid #234159; padding: 6pt 8pt; margin: 4pt 0 8pt 0; }
    table { width: 100%; border-collapse: collapse; }
    td { border-bottom: 0.4pt solid #d5dbe1; padding: 3pt 4pt; vertical-align: top; }
    td.q { width: 78%; } td.a { width: 22%; font-weight: bold; color: #234159; }
    span.yes { background-color: #d1e7dd; color: #0f5132; padding: 1pt 6pt; } span.no { background-color: #f8d7da; color: #842029; padding: 1pt 6pt; }
    span.c { font-weight: normal; color: #55606b; font-size: 8pt; }
    div.hdr { text-align: center; }
    """
    arch = fitz.Archive()
    logo_path = os.path.join(os.path.dirname(__file__), "..", "..", "web", "static", "vds", "logo.png")
    try:
        arch.add(open(logo_path, "rb").read(), "logo.png")
    except Exception:
        html = html.replace("<img src='logo.png' width='170'/>", "")
    if signature_png:
        arch.add(signature_png, "signature.png")
    story = fitz.Story(html=html, user_css=css, archive=arch)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    mediabox, where = fitz.paper_rect("a4"), fitz.Rect(40, 36, 555, 800)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    doc = fitz.open("pdf", buf.getvalue())
    doc.set_metadata({"title": title, "author": config.VILLA["legal"], "creator": "Vaelan"})
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


# ------------------------------------------------------------------ import historique SurveySparrow
def norm_name(name) -> str:
    import unicodedata
    x = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(sorted(re.findall(r"[a-z]{2,}", x)))


def _legacy_rules(row: dict, channel: str):
    """Questions Oui/Non de l'ancien formulaire, telles que posées, avec la réponse : [(texte, oui)]."""
    out = []
    for k, v in row.items():
        kk = (k or "").strip()
        if not kk:
            continue
        low = kk.lower()
        if (low.startswith("avez-vous bien") or low.startswith("pourriez-vous nous confirmer") or low.startswith("de manière générale")) \
                and (v or "").strip().lower() in ("yes", "no", "oui", "non"):
            out.append((kk, (v or "").strip().lower() in ("yes", "oui")))
    return out


def _match_lodgify_reservation(channel: str, ref: Optional[str], email: str, name: str) -> Optional[VdsReservation]:
    """Réservation issue de Lodgify (sans réponse) correspondant à une réponse historique."""
    with Session(engine) as s:
        cands = s.exec(select(VdsReservation).where(VdsReservation.lodgify_id != None,  # noqa: E711
                                                    VdsReservation.status.in_(["pending", "sent", "reminded", "completed"]))).all()
        with_resp = {(r[0] if isinstance(r, (tuple, list)) else r) for r in s.exec(select(VdsResponse.reservation_id)).all()}
    cands = [c for c in cands if c.id not in with_resp and c.channel == channel]
    em, nm = (email or "").lower().strip(), norm_name(name)
    return next((c for c in cands if ref and c.booking_ref == ref), None) or \
        next((c for c in cands if em and (c.guest_email or "").lower() == em), None) or \
        next((c for c in cands if nm and norm_name(c.guest_name) == nm), None)

def import_surveysparrow(csv_bytes: bytes, channel: str) -> Tuple[int, int]:
    """Importe un export CSV « Responses - VDS Terms & Conditions Check - <canal> » (historique).
    Renvoie (créés, ignorés = déjà importés)."""
    import csv
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    created = skipped = 0

    def col(row, *starts):
        for k, v in row.items():
            kk = (k or "").strip().lower()
            if any(kk.startswith(s.lower()) for s in starts):
                return (v or "").strip()
        return ""

    with Session(engine) as s:
        existing = {(r.source, (r.notes or "")) for r in s.exec(select(VdsReservation).where(VdsReservation.source == "surveysparrow")).all()}
    for row in rows:
        sub = col(row, "Submitted On")
        try:
            when = datetime.strptime(sub, "%d %B %Y %I:%M %p") - _TZ
        except Exception:
            when = datetime.utcnow()
        name = col(row, "Pourriez-vous nous indiquer votre nom")
        key = f"surveysparrow:{channel}:{sub}:{name}"
        if ("surveysparrow", key) in existing:
            skipped += 1
            continue
        email = col(row, "Quel est votre email") or col(row, "Merci d'indiquer votre email")
        ref = col(row, "Avant de commencer") or col(row, "booking_id_lodgify")
        ref = re.sub(r"[^A-Za-z0-9]", "", ref).upper() or None
        if ref and ref.isdigit() and channel == "lodgify":
            ref = "B" + ref
        # réservation Lodgify déjà synchronisée pour ce voyageur (même référence, email ou nom) et sans réponse ?
        res = _match_lodgify_reservation(channel, ref, email, name)
        if res:
            update_reservation(res.id, status="completed", guest_name=res.guest_name or name, guest_email=res.guest_email or email,
                               notes=((res.notes or "") + " · " + key).strip(" ·"))
        else:
            res = create_reservation(channel, booking_ref=ref, guest_name=name, guest_email=email, lang="fr",
                                     source="surveysparrow", notes=key, status="completed")
        mk_raw = col(row, "Souhaitez-vous être informé")
        marketing = "both" if "email et" in mk_raw else ("email" if "uniquement par email" in mk_raw else ("sms" if "SMS" in mk_raw else "none"))
        legacy = _legacy_rules(row, channel)
        rules = {r["key"]: all(ok for _, ok in legacy) for r in config.RULES}      # synthèse (compat.)
        bd = col(row, "Quelle est votre date de naissance")
        try:
            bd = datetime.strptime(bd, "%m/%d/%Y").date().isoformat()
        except Exception:
            pass
        data = {"rules": rules, "full_name": name, "address": col(row, "Quelle est votre adresse"), "birth_date": bd,
                "birth_place": col(row, "Quel est votre lieu de naissance"), "email": email, "marketing": marketing,
                "mkt_email": col(row, "Merci d'indiquer votre email"), "mkt_phone": col(row, "Merci d'indiquer votre numéro"),
                "erp": col(row, "Merci de confirmer que vous prendrez") == "Agree", "rgpd": col(row, "C'est la dernière") == "Agree",
                "signature_url": col(row, "Il ne manque plus que votre signature"), "id_urls": col(row, "Pourriez-vous joindre"),
                "legacy_rules": legacy, "lang": "fr", "channel": channel, "imported_from": "surveysparrow"}
        resp = VdsResponse(reservation_id=res.id, channel=channel, lang="fr", submitted_at=when, rules_ok=all(rules.values()),
                           refused_rules=None if all(rules.values()) else "all", full_name=name, email=email,
                           birth_date=bd, birth_place=data["birth_place"], address=data["address"], marketing=marketing,
                           data=json.dumps(data, ensure_ascii=False), signed=bool(data["signature_url"]))
        with Session(engine) as s:
            s.add(resp)
            s.commit()
        update_reservation(res.id, completed_at=when)
        created += 1
    return created, skipped


# ------------------------------------------------------------------ export Excel
def excel_export() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Réponses"
    heads = ["Réservation", "Canal", "Arrivée", "Départ", "Statut", "Nom", "Email", "Téléphone", "Date de naissance",
             "Lieu de naissance", "Adresse", "Occupants", "Heure d'arrivée", "Règles OK", "Règles refusées",
             "ERP", "Marketing", "Email newsletter", "Portable SMS", "RGPD", "Signé le", "IP", "Source"]
    ws.append(heads)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="234159")
        c.alignment = Alignment(vertical="center")
    with Session(engine) as s:
        rs = s.exec(select(VdsReservation).order_by(VdsReservation.arrival.desc().nullslast() if hasattr(VdsReservation.arrival.desc(), "nullslast") else VdsReservation.arrival.desc())).all()
        for r in rs:
            resp = s.exec(select(VdsResponse).where(VdsResponse.reservation_id == r.id).order_by(VdsResponse.id.desc())).first()
            d = json.loads(resp.data or "{}") if resp else {}
            ws.append([r.booking_ref, config.CHANNELS.get(r.channel, {}).get("label", r.channel), r.arrival, r.departure, r.status,
                       (resp.full_name if resp else None) or r.guest_name, (resp.email if resp else None) or r.guest_email,
                       (resp.phone if resp else None) or r.guest_phone, resp.birth_date if resp else None,
                       resp.birth_place if resp else None, resp.address if resp else None, d.get("occupants") or r.guests,
                       d.get("arrival_time"), ("oui" if resp.rules_ok else "NON") if resp else "", resp.refused_rules if resp else "",
                       "oui" if d.get("erp") else "", d.get("marketing"), d.get("mkt_email"), d.get("mkt_phone"),
                       "oui" if d.get("rgpd") else "", (resp.submitted_at + _TZ) if resp else None, resp.ip if resp else None, r.source])
    for col, w in zip("ABCDEFGHIJKLMNOPQRSTUVW", [14, 12, 11, 11, 11, 26, 28, 16, 12, 18, 40, 9, 10, 9, 14, 6, 10, 26, 16, 6, 16, 14, 12]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
