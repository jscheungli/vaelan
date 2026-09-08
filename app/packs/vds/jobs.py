"""VDS — tâches : synchro Lodgify (réservations → invitations), relances / alertes / ERP / purge."""
import json
import re
import unicodedata
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import lodgify
from app.models import VdsReservation, VdsFile, VdsResponse
from . import config, service


def _norm(name) -> str:
    x = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(sorted(re.findall(r"[a-z]{2,}", x)))


def run_lodgify_sync(ctx, invite: bool = True, stays=("Upcoming", "Current")) -> str:
    """Lit les réservations Lodgify (à venir + en cours), crée / met à jour les réservations Vaelan,
    marque les annulées, invite les nouvelles (statut Booked, email connu) si `invite`."""
    cl = lodgify.for_company(config.COMPANY_CODE)
    if not cl:
        raise RuntimeError("clé Lodgify absente (LODGIFY_VDS_APIKEY)")
    p = service.params()
    keep = set(p.get("sync_statuses") or ["Booked"])
    raw_all, created, updated, cancelled, invited, skipped, alerts = [], 0, 0, 0, 0, 0, 0
    for k, stay in enumerate(stays):
        ctx.progress(k, len(stays), step=f"Lodgify · {stay}…")
        raw_all += cl.bookings(stay=stay)
    ctx.log(f"{len(raw_all)} réservations lues (filtres {', '.join(stays)})")
    seen = set()
    for b in raw_all:
        n = lodgify.normalize(b)
        if not n["lodgify_id"] or n["lodgify_id"] in seen:
            continue
        seen.add(n["lodgify_id"])
        if config.VILLA.get("lodgify_property_id") and n.get("property_id") not in (None, config.VILLA["lodgify_property_id"]):
            continue
        legacy_done = bool(re.search(r"formulaire", str(n.get("notes") or ""), re.I))   # note Lodgify « Formulaire renseigné le … »
        with Session(engine) as s:
            r = s.exec(select(VdsReservation).where(VdsReservation.lodgify_id == n["lodgify_id"])).first()
            if not r and n["booking_ref"]:
                # rattachement d'une réservation créée à la main / importée avec la même référence
                r = s.exec(select(VdsReservation).where(VdsReservation.booking_ref == n["booking_ref"],
                                                        VdsReservation.lodgify_id == None)).first()  # noqa: E711
            if not r:
                # réponse SurveySparrow importée (sans dates) du même voyageur : même email, sinon même nom
                cands = s.exec(select(VdsReservation).where(VdsReservation.source == "surveysparrow", VdsReservation.lodgify_id == None,  # noqa: E711
                                                            VdsReservation.channel == n["channel"])).all()
                em = (n["guest_email"] or "").lower().strip()
                nm = _norm(n["guest_name"])
                r = next((c for c in cands if em and (c.guest_email or "").lower() == em), None) or \
                    next((c for c in cands if nm and _norm(c.guest_name) == nm), None)
        live = n["status"] in keep and not n["canceled"]
        if not r:
            if not live:
                skipped += 1
                continue
            r = service.create_reservation(n["channel"], booking_ref=n["booking_ref"], guest_name=n["guest_name"],
                                           guest_email=n["guest_email"], guest_phone=n["guest_phone"], arrival=n["arrival"],
                                           departure=n["departure"], guests=n["guests"], lang=n["lang"], source="lodgify",
                                           lodgify_id=n["lodgify_id"], notes=n.get("notes"),
                                           raw=json.dumps({k: v for k, v in b.items() if k not in ("quote", "transactions", "subtotals")},
                                                          ensure_ascii=False, default=str)[:6000],
                                           status="completed" if legacy_done else "pending")
            if legacy_done:
                service.update_reservation(r.id, completed_at=datetime.utcnow())
            created += 1
            ctx.log(f"+ {r.booking_ref} {config.CHANNELS[r.channel]['label']} · {r.guest_name} · {r.arrival} → {r.departure}"
                    + ("" if r.guest_email else " · SANS EMAIL") + (" · formulaire déjà renseigné (note Lodgify)" if legacy_done else ""))
            # invitation à la main (Marie) : plateformes, ou site direct sans invitation automatique possible
            if not legacy_done and r.arrival and r.arrival >= date.today():
                auto_ok = r.channel == "lodgify" and bool(p.get("auto_invite")) and bool(r.guest_email)
                if not auto_ok:
                    reason = ("réservation via " + config.CHANNELS[r.channel]["label"]) if r.channel != "lodgify" else \
                             ("pas d'email" if not r.guest_email else "invitation automatique désactivée")
                    ok, info = service.send_new_booking_alert(r, reason)
                    ctx.log(f"  🔔 alerte « à inviter à la main » : {info}")
                    alerts += 1 if ok else 0
        else:
            upd = {}
            for k in ("guest_name", "guest_email", "guest_phone", "arrival", "departure", "guests", "notes"):
                v = n.get(k)
                if k in ("arrival", "departure") and v:
                    v = date.fromisoformat(v)
                if v and getattr(r, k) != v and not (k == "guest_name" and r.status == "completed"):
                    upd[k] = v
            if r.lodgify_id is None:
                upd["lodgify_id"] = n["lodgify_id"]
                upd["booking_ref"] = n["booking_ref"] or r.booking_ref
                ctx.log(f"↔ {n['booking_ref']} rattachée à la fiche existante #{r.id} ({r.source}, {r.status})")
            if legacy_done and r.status in ("pending", "sent", "reminded"):
                upd["status"] = "completed"
                upd["completed_at"] = datetime.utcnow()
            if not live and r.status not in ("cancelled", "completed", "refused"):
                upd["status"] = "cancelled"
                cancelled += 1
                ctx.log(f"✗ {r.booking_ref} annulée / déclinée côté Lodgify ({n['status']})")
            if upd:
                service.update_reservation(r.id, **upd)
                updated += 1
                r = service.by_token(r.token)
        if invite and p.get("auto_invite") and r.status == "pending" and live and r.guest_email \
                and r.arrival and r.arrival >= date.today():
            ok, info = service.send_invitation(r)
            invited += 1 if ok else 0
            ctx.log(f"  ✉ invitation {r.guest_email} : {info}")
    ctx.add_artifact("json", f"lodgify_bookings_{datetime.utcnow():%Y%m%d}.json",
                     json.dumps(raw_all, ensure_ascii=False, default=str).encode(), "application/json")
    return (f"Lodgify : {len(seen)} réservations · {created} créées · {updated} mises à jour · {cancelled} annulées · "
            f"{invited} invitations envoyées · {alerts} alerte(s) « à inviter à la main » · {skipped} ignorées (non confirmées)")


def run_reminders(ctx) -> str:
    """Passe quotidienne : invitations en attente, relances, alertes internes, ERP J-1, purge des pièces."""
    p = service.params()
    today = service.now_local().date()
    now = datetime.utcnow()
    invited = reminded = alerted = erp = purged = 0
    with Session(engine) as s:
        rs = list(s.exec(select(VdsReservation).where(VdsReservation.status.in_(["pending", "sent", "reminded", "completed"]))).all())
    ctx.log(f"{len(rs)} réservations actives · {today:%d/%m/%Y}")
    ctx.log("Rappel : l'état des risques (ERP) est envoyé par la notification automatique Lodgify (validité 6 mois, à renouveler dans Lodgify).")
    for k, r in enumerate(rs):
        ctx.progress(k, len(rs), step=f"{r.booking_ref or r.id}…")
        if not r.arrival or r.arrival < today - timedelta(days=1):
            continue
        days_left = (r.arrival - today).days
        if r.status == "completed":
            continue          # (état des risques : envoyé par la notification automatique Lodgify, pas ici)
        # --- formulaire incomplet ---
        if r.status == "pending":
            if r.guest_email and p.get("auto_invite"):
                ok, info = service.send_invitation(r)
                ctx.log(f"invitation {r.booking_ref} → {r.guest_email} : {info}")
                invited += 1 if ok else 0
            elif not r.guest_email:
                ctx.log(f"⚠ {r.booking_ref} ({config.CHANNELS[r.channel]['label']}) : pas d'email — lien à envoyer via la messagerie")
        else:
            last = r.reminded_at or r.invited_at or r.created_at
            since = (now - last).days if last else 99
            due = (r.reminder_count or 0) < int(p.get("reminder_days") and p.get("max_reminders") or 3) and since >= int(p.get("reminder_days") or 3)
            pre = days_left in set(p.get("pre_arrival_days") or []) and since >= 1
            if (due or pre) and r.guest_email:
                ok, info = service.send_invitation(r, reminder=True)
                ctx.log(f"relance {r.booking_ref} ({'J-' + str(days_left) if pre else 'n°' + str((r.reminder_count or 0) + 1)}) → {r.guest_email} : {info}")
                reminded += 1 if ok else 0
        # alerte interne : arrivée proche sans formulaire (J-7 et J-2 par défaut ; rattrapage si un jour a été manqué)
        alert_days = set(p.get("alert_days") or [])
        due_alert = days_left in alert_days or (alert_days and days_left <= max(alert_days) and not r.alerted_at)
        if due_alert and (not r.alerted_at or (now - r.alerted_at).days >= 1):
            ok, info = service.send_alert(
                r, f"[VDS] Arrivée dans {days_left} j SANS formulaire — {r.guest_name or '?'} · {config.CHANNELS[r.channel]['label']}",
                f"La réservation {r.booking_ref or r.id} ({config.CHANNELS[r.channel]['label']}) arrive le {service.fmt_date(r.arrival)} "
                f"et le formulaire d'arrivée n'est pas complété (statut : {r.status}, {r.reminder_count or 0} relance(s)).\n"
                f"Email : {r.guest_email or 'AUCUN — envoyer le lien via la messagerie de la plateforme'}\n"
                f"Lien du formulaire : {service.public_url(r)}\nDétail : {service.base_url()}/c/VDS/checkin/{r.id}\n")
            service.update_reservation(r.id, alerted_at=now)
            alerted += 1 if ok else 0
    # --- purge des pièces d'identité N jours après le départ ---
    limit = today - timedelta(days=int(p.get("purge_id_days") or 180))
    with Session(engine) as s:
        old = s.exec(select(VdsReservation).where(VdsReservation.departure != None, VdsReservation.departure < limit)).all()  # noqa: E711
        ids = [r.id for r in old]
        if ids:
            for f in s.exec(select(VdsFile).where(VdsFile.kind == "id_document", VdsFile.reservation_id.in_(ids))).all():
                s.delete(f)
                purged += 1
            s.commit()
    if purged:
        ctx.log(f"purge : {purged} pièce(s) d'identité supprimée(s) (départ avant {limit:%d/%m/%Y})")
    return (f"Relances : {invited} invitation(s) · {reminded} relance(s) · {alerted} alerte(s) interne(s) · "
            f"{purged} pièce(s) purgée(s)")


def run_daily(ctx) -> str:
    """Passe quotidienne planifiée : synchro Lodgify puis relances."""
    parts = []
    try:
        parts.append(run_lodgify_sync(ctx))
    except Exception as e:
        ctx.log(f"synchro Lodgify en échec : {e}")
        parts.append(f"Lodgify : échec ({str(e)[:80]})")
    parts.append(run_reminders(ctx))
    try:
        from . import telegram as vtg
        n = vtg.notify(vtg.brief())
        ctx.log(f"Telegram : point du jour envoyé à {n} chat(s)")
        parts.append(f"Telegram : {n} chat(s)")
    except Exception as e:
        ctx.log(f"Telegram : {e}")
    return " — ".join(parts)
