"""Notifications aux salariés après un changement validé : résumé de ce qui a changé + leur semaine.
Email automatique si l'adresse est connue, sinon texte prêt à copier (SMS / WhatsApp) à marquer envoyé."""
import json
from datetime import date, datetime, timedelta
from typing import List, Optional

from sqlmodel import Session, select

from app.core.db import engine
from app.core import mailer
from app.models import PlEmployee, PlShift, PlNotification
from . import service, config

P = lambda k: (k or "").replace("_", " ").title()


def _snapshot(company_code: str, eid: int, monday: date) -> List[dict]:
    rows = service.employee_shifts(company_code, eid, monday, monday + timedelta(days=6))
    return sorted([{"date": x.date.isoformat(), "kind": x.kind, "post": x.post_key or "", "start": x.start or "", "end": x.end or "", "site": x.site}
                   for x in rows if x.kind in ("work", "absence")], key=lambda r: (r["date"], r["start"]))


def _last_sent(company_code: str, eid: int, monday: date) -> Optional[PlNotification]:
    with Session(engine) as s:
        return s.exec(select(PlNotification).where(PlNotification.company_code == company_code, PlNotification.employee_id == eid,
                                                    PlNotification.week_monday == monday, PlNotification.status == "sent")
                      .order_by(PlNotification.id.desc())).first()


def _line(r: dict, pmap: dict) -> str:
    d = date.fromisoformat(r["date"])
    lab = f"{config.DAYS_SHORT[d.weekday()]} {d:%d/%m}"
    if r["kind"] == "absence":
        return f"{lab} : {r['post']}"
    p = pmap.get(r["post"])
    site = f" ({config.SITES.get(r['site'], r['site'])})" if r.get("site") and r["site"] != pmap.get("_site") else ""
    return f"{lab} : {r['start']}–{r['end']} {p.label if p else P(r['post'])}{site}"


def diff_text(old: List[dict], new: List[dict], pmap: dict) -> List[str]:
    key = lambda r: (r["date"], r["kind"], r["post"], r["start"], r["end"], r.get("site"))
    o, n = {key(r): r for r in old}, {key(r): r for r in new}
    out = []
    for k in sorted(set(n) - set(o)):
        out.append("+ " + _line(n[k], pmap))
    for k in sorted(set(o) - set(n)):
        out.append("− " + _line(o[k], pmap))
    return out


def build(company_code: str, e: PlEmployee, monday: date, reason: str, extra: str = "") -> Optional[dict]:
    """Texte de notification si la semaine de la personne a changé depuis la dernière notification envoyée (ou jamais notifiée)."""
    pmap = service.post_map(company_code, e.site)
    pmap["_site"] = e.site
    new = _snapshot(company_code, e.id, monday)
    last = _last_sent(company_code, e.id, monday)
    old = json.loads(last.snapshot) if last else None
    changes = diff_text(old, new, pmap) if old is not None else []
    if old is not None and not changes:
        return None
    sunday = monday + timedelta(days=6)
    week_lines = []
    for i in range(7):
        d = monday + timedelta(days=i)
        items = [r for r in new if r["date"] == d.isoformat()]
        week_lines.append(f"  {config.DAYS_SHORT[d.weekday()]} {d:%d/%m} : " + (" · ".join(_line(r, pmap).split(" : ", 1)[1] for r in items) if items else "repos"))
    head = f"Bonjour {e.first_name},\n\n"
    if old is None:
        head += f"Voici votre planning de la semaine du {monday:%d/%m} au {sunday:%d/%m}"
    else:
        head += f"Votre planning de la semaine du {monday:%d/%m} au {sunday:%d/%m} a été modifié"
    head += (f" ({extra})" if extra else "") + ".\n\n"
    body = head
    if changes:
        body += "Ce qui change :\n" + "\n".join("  " + c for c in changes) + "\n\n"
    body += "Votre semaine :\n" + "\n".join(week_lines) + f"\n\nMerci de confirmer que vous avez bien pris connaissance de ce planning.\n{config.SITES.get(e.site, e.site)}"
    subject = f"Planning semaine du {monday:%d/%m} — {'modification' if old is not None else 'nouveau planning'}"
    return {"subject": subject, "text": body, "snapshot": new}


def managers_of(cfg: dict, sites) -> List[dict]:
    """Responsables configurés (config['managers'][site]) pour les établissements donnés, dédoublonnés."""
    out, seen = [], set()
    for site in dict.fromkeys(sites):
        for m in (cfg.get("managers") or {}).get(site, []):
            k = (m.get("email") or "").lower() or (m.get("name") or "").lower()
            if not (m.get("name") or m.get("email")) or k in seen:
                continue
            seen.add(k)
            out.append({**m, "site": site})
    return out


def notify_managers(company_code: str, sites, monday: date, reason: str, notifs: List[PlNotification], by: str = None, extra: str = "", send: bool = True) -> List[PlNotification]:
    """Un résumé de tout ce qui a changé cette semaine (toutes personnes concernées) aux responsables des établissements touchés."""
    if not notifs:
        return []
    cfg = service.get_config(company_code)
    mgrs = managers_of(cfg, sites)
    if not mgrs:
        return []
    with Session(engine) as s:
        emap = {e.id: e for e in s.exec(select(PlEmployee).where(PlEmployee.id.in_([n.employee_id for n in notifs]))).all()}
    sunday = monday + timedelta(days=6)
    lines = []
    for n in notifs:
        e = emap.get(n.employee_id)
        body = n.text.split("Ce qui change :\n", 1)
        chg = body[1].split("\n\n", 1)[0].strip().replace("\n  ", "\n    ") if len(body) == 2 else "    nouveau planning transmis"
        lines.append(f"• {service.full_name(e) if e else n.employee_id} ({config.SITES.get(e.site, e.site) if e else ''}) — {'email envoyé' if n.status == 'sent' else 'à prévenir par SMS / WhatsApp'}\n    {chg}")
    label = {"publication": "publication de la semaine", "remplacement": "remplacement appliqué", "modification": "modifications validées"}.get(reason, reason)
    subject = f"Planning {' / '.join(config.SITES.get(x, x) for x in dict.fromkeys(sites))} — semaine du {monday:%d/%m} : {label}"
    text = (f"Bonjour,\n\n{label.capitalize()}{' (' + extra + ')' if extra else ''} — semaine du {monday:%d/%m} au {sunday:%d/%m}, {len(notifs)} personne(s) concernée(s)"
            f"{' — validé par ' + by if by else ''}.\n\n" + "\n".join(lines) +
            f"\n\nLes personnes sans email sont à prévenir depuis Vaelan (Planning → Notifications), le texte est prêt à copier.\n{service.admin_url()}/c/{company_code}/planning/notifications?site={sites[0] if sites else ''}")
    out = []
    with Session(engine) as s:
        for m in mgrs:
            rec = f"{m.get('role') or 'Responsable'} · {m.get('name') or m.get('email')}"
            notif = PlNotification(company_code=company_code, site=m["site"], employee_id=None, recipient=rec, week_monday=monday, reason=reason, subject=subject, text=text,
                                   snapshot="[]", channel="email" if m.get("email") else "manual", by_user=by)
            if send and m.get("email") and mailer.configured():
                ok, info = mailer.send([m["email"]], subject, text + "\n\n— envoyé via Vaelan")
                if ok:
                    notif.status, notif.sent_at = "sent", datetime.utcnow()
            s.add(notif)
            s.commit()
            s.refresh(notif)
            s.expunge(notif)
            out.append(notif)
    return out


def queue(company_code: str, employee_ids, monday: date, reason: str, by: str = None, extra: str = "", send: bool = True, sites=None) -> List[PlNotification]:
    """Crée (et envoie par email si possible) une notification par salarié dont la semaine a changé,
    puis un résumé aux responsables des établissements concernés (sites = établissements touchés, sinon ceux des salariés)."""
    out, home = [], []
    with Session(engine) as s:
        for eid in dict.fromkeys(employee_ids):
            e = s.get(PlEmployee, eid)
            if not e:
                continue
            home.append(e.site)
            n = build(company_code, e, monday, reason, extra)
            if not n:
                continue
            # une notification en attente pour la même semaine est remplacée (texte à jour) ; identique → rien à faire
            olds = s.exec(select(PlNotification).where(PlNotification.employee_id == eid, PlNotification.week_monday == monday, PlNotification.status == "pending")).all()
            if any(x.text == n["text"] for x in olds):
                continue
            for old in olds:
                s.delete(old)
            notif = PlNotification(company_code=company_code, site=e.site, employee_id=eid, week_monday=monday, reason=reason, subject=n["subject"], text=n["text"],
                                   snapshot=json.dumps(n["snapshot"], ensure_ascii=False), channel="email" if e.email else "manual", by_user=by)
            if send and e.email and mailer.configured():
                ok, info = mailer.send([e.email], n["subject"], n["text"] + "\n\n— envoyé via Vaelan")
                if ok:
                    notif.status, notif.sent_at = "sent", datetime.utcnow()
            s.add(notif)
            s.commit()
            s.refresh(notif)
            s.expunge(notif)
            out.append(notif)
    if out:
        notify_managers(company_code, list(dict.fromkeys((sites or []) + home)), monday, reason, out, by=by, extra=extra, send=send)
    return out


def queue_week(company_code: str, site: str, monday: date, by: str = None, reason: str = "publication") -> List[PlNotification]:
    """Après publication : tous les salariés qui ont une plage cette semaine sur ce site (ou en avaient lors de la dernière notification)."""
    rows = service.shifts(company_code, site, monday, monday + timedelta(days=6))
    ids = {x.employee_id for x in rows if x.employee_id}
    with Session(engine) as s:
        for n in s.exec(select(PlNotification).where(PlNotification.company_code == company_code, PlNotification.site == site, PlNotification.week_monday == monday, PlNotification.status == "sent")).all():
            if n.employee_id:
                ids.add(n.employee_id)
    return queue(company_code, sorted(ids), monday, reason, by=by, sites=[site])


def pending(company_code: str, site: str = None) -> List[PlNotification]:
    with Session(engine) as s:
        q = select(PlNotification).where(PlNotification.company_code == company_code)
        if site:
            q = q.where(PlNotification.site == site)
        return sorted(s.exec(q).all(), key=lambda n: (n.status != "pending", -n.id))[:200]


def mark_sent(nid: int, by: str = None, channel: str = "manual") -> None:
    with Session(engine) as s:
        n = s.get(PlNotification, nid)
        if n:
            n.status, n.sent_at, n.channel, n.by_user = "sent", datetime.utcnow(), channel, by or n.by_user
            s.add(n)
            s.commit()
