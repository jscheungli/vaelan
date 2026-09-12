"""VDS × Telegram — briefs d'équipe (check-ins / check-outs / formulaires), notifications, et
assistant (Claude) qui répond dans le(s) groupe(s) où le bot est ajouté.

Les groupes/chats connus sont mémorisés dans Setting `_system` / `telegram:chats` (JSON
{chat_id: {title, type, notify, since}}). Un chat reçoit les notifications VDS si notify = true
(case à cocher dans la configuration ; les nouveaux chats sont activés par défaut)."""
import json
import re
import threading
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlmodel import Session, select

from app.core.db import engine
from app.core import assistant
from app.core.connectors import telegram as tg
from app.models import Setting, VdsReservation, VdsResponse
from . import config, service

_KEY = "telegram:chats"
STATUS_FR = {"pending": "à inviter", "sent": "invité", "reminded": "relancé", "completed": "formulaire signé",
             "refused": "règles REFUSÉES", "cancelled": "annulée"}


# ------------------------------------------------------------------ chats connus
def chats() -> Dict[str, dict]:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == "_system", Setting.key == _KEY)).first()
    try:
        return json.loads(st.value) if st else {}
    except Exception:
        return {}


def save_chats(d: Dict[str, dict]) -> None:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == "_system", Setting.key == _KEY)).first()
        if not st:
            st = Setting(company_code="_system", key=_KEY, value="{}")
        st.value = json.dumps(d, ensure_ascii=False)
        st.updated_at = datetime.utcnow()
        s.add(st)
        s.commit()


def remember_chat(chat: dict) -> dict:
    d = chats()
    cid = str(chat.get("id"))
    entry = d.get(cid) or {"notify": True, "since": datetime.utcnow().isoformat(timespec="minutes")}
    title = chat.get("title") or " ".join(x for x in [chat.get("first_name"), chat.get("last_name")] if x) or chat.get("username")
    if title:
        entry["title"] = title
    entry.setdefault("title", cid)
    if chat.get("type"):
        entry["type"] = chat.get("type")
    d[cid] = entry
    save_chats(d)
    return entry


def targets() -> List[str]:
    return [cid for cid, c in chats().items() if c.get("notify")]


def notify(text: str) -> int:
    """Envoie `text` (HTML) à tous les chats abonnés ; renvoie le nombre d'envois réussis."""
    if not tg.configured():
        return 0
    n = 0
    for cid in targets():
        ok, _ = tg.send_message(cid, text)
        n += 1 if ok else 0
    return n


# ------------------------------------------------------------------ données
def _upcoming(days: int = 30) -> List[VdsReservation]:
    today = service.now_local().date()
    with Session(engine) as s:
        rs = s.exec(select(VdsReservation).where(VdsReservation.status != "cancelled",
                                                 VdsReservation.arrival != None,   # noqa: E711
                                                 VdsReservation.arrival <= today + timedelta(days=days),
                                                 VdsReservation.departure >= today - timedelta(days=1))).all()
    return sorted(rs, key=lambda r: (r.arrival, r.id))


def _line(r: VdsReservation, with_dates: bool = True) -> str:
    ch = config.CHANNELS.get(r.channel, {}).get("label", r.channel)
    dates = f" {service.fmt_date(r.arrival)} → {service.fmt_date(r.departure)}" if with_dates else ""
    st = STATUS_FR.get(r.status, r.status)
    mark = "✅" if r.status == "completed" else ("⛔" if r.status == "refused" else "⏳")
    return f"{mark} <b>{tg.esc(r.guest_name or '?')}</b> · {tg.esc(ch)}{dates} · {r.guests or '?'} pers. · {st}"


def brief(days_ahead: int = 7) -> str:
    """Point du jour : arrivées / départs aujourd'hui et demain, formulaires manquants à J-7."""
    today = service.now_local().date()
    tomorrow = today + timedelta(days=1)
    rs = _upcoming(days_ahead + 1)
    L = [f"☀️ <b>La Villa des Sables du Lagon — point du {today:%d/%m/%Y}</b>"]
    for label, day in (("Aujourd'hui", today), ("Demain", tomorrow)):
        ins = [r for r in rs if r.arrival == day]
        outs = [r for r in rs if r.departure == day]
        if ins or outs:
            L.append(f"\n<b>{label}</b>")
            for r in ins:
                L.append("🔑 Arrivée " + _line(r, with_dates=False) + (f" · prévue {tg.esc(_arrival_time(r))}" if _arrival_time(r) else ""))
            for r in outs:
                L.append("🧹 Départ " + _line(r, with_dates=False))
    missing = [r for r in rs if r.status in ("pending", "sent", "reminded", "refused") and today <= r.arrival <= today + timedelta(days=days_ahead)]
    if missing:
        L.append(f"\n<b>Formulaire manquant (arrivée sous {days_ahead} jours)</b>")
        L += ["• " + _line(r) for r in missing]
    nxt = [r for r in rs if r.arrival > tomorrow][:5]
    if nxt:
        L.append("\n<b>Prochaines arrivées</b>")
        L += ["• " + _line(r) for r in nxt]
    if len(L) == 1:
        L.append("Rien de prévu aujourd'hui ni demain.")
    return "\n".join(L)


def _arrival_time(r: VdsReservation) -> str:
    resp = service.latest_response(r.id)
    if not resp:
        return ""
    try:
        return json.loads(resp.data or "{}").get("arrival_time") or ""
    except Exception:
        return ""


def context_text(days: int = 60) -> str:
    """Contexte donné à l'assistant : réservations à venir (60 j) + statut du formulaire."""
    today = service.now_local().date()
    rows = []
    for r in _upcoming(days):
        resp = service.latest_response(r.id)
        d = json.loads(resp.data or "{}") if resp else {}
        rows.append(f"- {r.guest_name or '?'} | canal {config.CHANNELS.get(r.channel, {}).get('label', r.channel)} | réf {r.booking_ref or r.id} | "
                    f"{r.arrival} → {r.departure} | {r.guests or '?'} pers. | statut formulaire : {STATUS_FR.get(r.status, r.status)}"
                    + (f" | arrivée prévue {d.get('arrival_time')}" if d.get("arrival_time") else "")
                    + (f" | tél {r.guest_phone}" if r.guest_phone else "") + (f" | email {r.guest_email}" if r.guest_email else "")
                    + (f" | occupants : {d.get('occupants_list')}" if d.get("occupants_list") else ""))
    return (f"Date du jour : {today:%d/%m/%Y} (heure de La Réunion). Réservations à venir ({days} jours) :\n" + "\n".join(rows)) if rows else "Aucune réservation à venir."


SYSTEM = ("Tu es Vaelan, l'assistant de l'équipe de La Villa des Sables du Lagon (villa en location saisonnière à La Réunion, "
          "12 personnes max, check-in 16h-20h, check-out 8h-9h, caution 1 000 €, règles : pas de fête, pas de nuisance sonore). "
          "Tu réponds dans un groupe Telegram de l'équipe (propriétaire et gestionnaire) : réponses courtes, concrètes, en français, "
          "sans markdown (texte brut, listes avec des tirets). Tu t'appuies UNIQUEMENT sur le contexte fourni ; si l'information n'y est pas, dis-le. "
          "Ne divulgue jamais de données de voyageurs au-delà de ce qui est utile à l'équipe.")


# ------------------------------------------------------------------ commandes et messages entrants
HELP = ("Commandes :\n/point — arrivées, départs et formulaires manquants\n/arrivees — prochaines arrivées (30 j)\n/departs — prochains départs (30 j)\n"
        "/formulaires — formulaires non signés\n/resa <nom ou référence> — détail d'une réservation\n/aide — cette aide\n\n"
        "Vous pouvez aussi me poser une question en me mentionnant (@bot) ou en répondant à un de mes messages.")


def handle_update(update: dict, bot_username: str = "") -> Optional[str]:
    """Traite un update Telegram ; renvoie le texte de réponse (HTML) ou None."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    remember_chat(chat)
    text = (msg.get("text") or "").strip()
    if not text:
        return None
    is_private = chat.get("type") == "private"
    mention = bool(bot_username) and f"@{bot_username}".lower() in text.lower()
    reply_to_bot = bool(bot_username) and ((msg.get("reply_to_message") or {}).get("from") or {}).get("username", "").lower() == bot_username.lower()
    cmd = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", text, re.S)
    if cmd:
        name, arg = cmd.group(1).lower(), cmd.group(2).strip()
        if name in ("start", "aide", "help"):
            return "Bonjour ! Je suis <b>Vaelan</b>, l'assistant de La Villa des Sables du Lagon.\n\n" + tg.esc(HELP)
        if name in ("point", "brief"):
            return brief()
        if name in ("arrivees", "arrivées", "checkins"):
            rs = [r for r in _upcoming(30) if r.arrival >= service.now_local().date()]
            return "<b>Prochaines arrivées</b>\n" + ("\n".join("• " + _line(r) for r in rs) or "Aucune sous 30 jours.")
        if name in ("departs", "départs", "checkouts"):
            rs = sorted([r for r in _upcoming(30) if r.departure and r.departure >= service.now_local().date()], key=lambda r: r.departure)
            return "<b>Prochains départs</b>\n" + ("\n".join(f"• {service.fmt_date(r.departure)} · " + _line(r, with_dates=False) for r in rs) or "Aucun sous 30 jours.")
        if name in ("formulaires", "forms"):
            rs = [r for r in _upcoming(120) if r.status in ("pending", "sent", "reminded", "refused")]
            return "<b>Formulaires non signés</b>\n" + ("\n".join("• " + _line(r) for r in rs) or "Tous les formulaires à venir sont signés ✅")
        if name == "resa":
            q = arg.lower()
            rs = [r for r in _upcoming(365) if q and (q in (r.guest_name or "").lower() or q in (r.booking_ref or "").lower())]
            if not rs:
                return "Aucune réservation trouvée pour « " + tg.esc(arg) + " »."
            out = []
            for r in rs[:3]:
                out.append(_line(r) + f"\n   réf {tg.esc(r.booking_ref or r.id)} · tél {tg.esc(r.guest_phone or '—')} · email {tg.esc(r.guest_email or '—')}"
                           f"\n   fiche : {service.admin_url()}/c/VDS/checkin/{r.id}")
            return "\n".join(out)
        return "Commande inconnue. " + tg.esc(HELP)
    if is_private or mention or reply_to_bot:
        question = re.sub(rf"@{re.escape(bot_username)}", "", text, flags=re.I).strip() if bot_username else text
        ans = assistant.answer(SYSTEM + "\n\n" + context_text(), question or "Bonjour")
        return tg.esc(ans)
    return None


def process_async(update: dict, bot_username: str = "") -> None:
    """Répond hors requête HTTP (Telegram attend un 200 rapide ; l'assistant peut prendre quelques secondes)."""
    def _run():
        try:
            reply = handle_update(update, bot_username)
            msg = update.get("message") or update.get("edited_message") or {}
            if reply and msg.get("chat"):
                tg.send_message(msg["chat"]["id"], reply, reply_to=msg.get("message_id"))
        except Exception as e:
            print(f"[telegram] {e}")
    threading.Thread(target=_run, daemon=True).start()
