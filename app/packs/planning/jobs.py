"""Routine de contrôle : chaque jour (et bilan hebdomadaire), les semaines en cours et à venir sont passées
au crible des règles activées ; tout nouvel écart déclenche un email immédiat ; le lundi, un bilan complet."""
import json
import hashlib
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app.core.db import engine
from app.core import mailer
from app.models import Setting, PlEmployee
from . import service, config


def _sites_with_staff(company_code: str):
    with Session(engine) as s:
        return sorted({e.site for e in s.exec(select(PlEmployee).where(PlEmployee.company_code == company_code, PlEmployee.active == True)).all()})  # noqa: E712


def _sent_state(company_code: str) -> dict:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == company_code, Setting.key == "planning:alerts_sent")).first()
    try:
        return json.loads(st.value) if st else {}
    except Exception:
        return {}


def _save_sent(company_code: str, state: dict) -> None:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == company_code, Setting.key == "planning:alerts_sent")).first()
        if not st:
            st = Setting(company_code=company_code, key="planning:alerts_sent", value="{}")
        st.value = json.dumps(state, ensure_ascii=False)
        st.updated_at = datetime.utcnow()
        s.add(st)
        s.commit()


def collect(company_code: str, cfg: dict = None, horizon_days: int = None) -> dict:
    """Écarts par site sur les semaines couvrant [aujourd'hui - 7 j ; aujourd'hui + horizon]."""
    cfg = cfg or service.get_config(company_code)
    A = cfg.get("alerts") or {}
    horizon = int(horizon_days or A.get("horizon_days") or 14)
    today = service.now_local().date() if hasattr(service, "now_local") else date.today()
    weeks = []
    m = service.week_monday(today - timedelta(days=7))
    while m <= today + timedelta(days=horizon):
        weeks.append(m)
        m += timedelta(days=7)
    out = {}
    hol = holiday_reminders(company_code, cfg, today)
    for site in _sites_with_staff(company_code):
        items = []
        for wk in weeks:
            for a in service.check_rules(company_code, site, wk, cfg):
                a = dict(a)
                a["week"] = wk.isoformat()
                a["site"] = site
                a["key"] = hashlib.sha1(f"{site}|{wk}|{a.get('rule')}|{a.get('employee_id')}|{a.get('date')}|{a.get('post')}|{a['text']}".encode()).hexdigest()[:16]
                items.append(a)
        items += [h for h in hol if h["site"] == site]
        out[site] = items
    return out


def holiday_reminders(company_code: str, cfg: dict, today: date) -> list:
    """Un rappel par férié configuré tombant dans les `confirm_days` : ouvert/fermé selon la config, équipe prévue."""
    H = cfg.get("holidays") or {}
    out = []
    for site in _sites_with_staff(company_code):
        for y in (today.year, today.year + 1):
            for k, dd in service.holidays_of(cfg, y).items():
                if 0 <= (dd - today).days <= int(H.get("confirm_days") or 21):
                    closed = k in (H.get("closed_types") or [])
                    n = sum(1 for x in service.shifts(company_code, site, dd, dd) if x.kind == "work" and x.employee_id)
                    txt = f"{config.HOLIDAY_LABELS.get(k, k)} le {dd:%A %d/%m} : {'magasin FERMÉ (aucune plage attendue)' if closed else 'magasin ouvert, équipe réduite'} · {n} plage(s) planifiée(s)" + (" — À VÉRIFIER : des plages sont posées un jour fermé" if closed and n else "")
                    out.append({"rule": "holiday", "level": "warning" if (closed and n) or (not closed and n == 0) else "info", "date": dd.isoformat(), "post": None, "employee_id": None,
                                "text": txt, "week": service.week_monday(dd).isoformat(), "site": site, "key": hashlib.sha1(f"hol|{site}|{dd}".encode()).hexdigest()[:16]})
    return out


def weekly_review(company_code: str, cfg: dict = None, today: date = None) -> dict:
    """Revue des changements et points d'attention (bilan du lundi) : effectifs, contrats, fiches incomplètes,
    semaines à publier, incidents ouverts, configuration."""
    cfg = cfg or service.get_config(company_code)
    today = today or date.today()
    with Session(engine) as s:
        snap_st = s.exec(select(Setting).where(Setting.company_code == company_code, Setting.key == "planning:snapshot")).first()
    try:
        prev = json.loads(snap_st.value) if snap_st else {}
    except Exception:
        prev = {}
    emps = service.employees(company_code, active_only=False)
    cur = {"employees": {str(e.id): {"name": service.full_name(e), "active": e.active, "end": e.end_date.isoformat() if e.end_date else None, "posts": service.e_posts(e), "site": e.site} for e in emps},
           "posts": {f"{p.site}:{p.key}": p.active for site in config.SITES for p in service.posts(company_code, site)},
           "config": hashlib.sha1(json.dumps({k: v for k, v in cfg.items() if k not in ("wizard_step",)}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]}
    R = {"arrivees": [], "departs": [], "fins_contrat": [], "fiches": [], "postes": [], "publication": [], "incidents": [], "config": []}
    pe = prev.get("employees") or {}
    for eid, e in cur["employees"].items():
        if eid not in pe and e["active"]:
            R["arrivees"].append(e["name"])
        elif eid in pe and pe[eid].get("active") and not e["active"]:
            R["departs"].append(e["name"])
        elif eid in pe and pe[eid].get("posts") != e["posts"] and e["active"]:
            R["fiches"].append(f"{e['name']} : postes modifiés")
    for e in emps:
        if not e.active:
            continue
        if e.end_date and 0 <= (e.end_date - today).days <= 30:
            R["fins_contrat"].append(f"{service.full_name(e)} : fin de contrat le {e.end_date:%d/%m/%Y} ({e.contract_type})")
        elif e.end_date and e.end_date < today:
            R["fins_contrat"].append(f"{service.full_name(e)} : contrat terminé le {e.end_date:%d/%m/%Y} mais fiche encore active")
        missing = [x for x, v in (("téléphone", e.phone), ("Telegram", e.telegram)) if not v]
        if not service.e_posts(e):
            missing.append("aucun poste")
        if e.contract_type in ("Apprenti",) and not service.e_list(e, "cfa_days"):
            missing.append("jours de CFA")
        if missing:
            R["fiches"].append(f"{service.full_name(e)} : {', '.join(missing)}")
    pp = prev.get("posts") or {}
    for k, active in cur["posts"].items():
        if k not in pp:
            R["postes"].append(f"nouveau poste {k.split(':')[1]} ({k.split(':')[0]})")
        elif pp[k] != active:
            R["postes"].append(f"poste {k.split(':')[1]} {'activé' if active else 'désactivé'}")
    horizon = int((cfg.get("publication") or {}).get("horizon_weeks") or 2)
    for site in _sites_with_staff(company_code):
        m = service.week_monday(today)
        for k in range(horizon + 1):
            wk = m + timedelta(days=7 * k)
            rows = service.shifts(company_code, site, wk, wk + timedelta(days=6))
            work = [x for x in rows if x.kind == "work"]
            if not work:
                R["publication"].append(f"{config.SITES.get(site, site)} : semaine du {wk:%d/%m} sans aucune plage")
            elif any(x.status != "published" for x in work):
                R["publication"].append(f"{config.SITES.get(site, site)} : semaine du {wk:%d/%m} non publiée ({sum(1 for x in work if x.status != 'published')} plage(s) en brouillon)")
        for inc in service.incidents(company_code, site):
            R["incidents"].append(f"#{inc.id} · {cur['employees'].get(str(inc.employee_id), {}).get('name', '?')} · {inc.date_from:%d/%m} → {inc.date_to:%d/%m} · {inc.reason} (en cours)")
    if prev.get("config") and prev["config"] != cur["config"]:
        R["config"].append("la configuration du planning a été modifiée depuis le dernier bilan")
    if not cfg.get("validated_at"):
        R["config"].append("questionnaire de configuration non validé : le mode automatique n'est pas actif")
    with Session(engine) as s:
        st = snap_st or Setting(company_code=company_code, key="planning:snapshot", value="{}")
        st.value = json.dumps(cur, ensure_ascii=False)
        st.updated_at = datetime.utcnow()
        s.add(st)
        s.commit()
    return R


REVIEW_LABELS = {"arrivees": "Nouveaux salariés", "departs": "Salariés désactivés", "fins_contrat": "Contrats qui se terminent (30 jours) ou terminés", "fiches": "Fiches incomplètes ou modifiées",
                 "postes": "Postes", "publication": "Plannings à générer ou à publier", "incidents": "Remplacements en cours", "config": "Configuration"}


def _fmt_review(R: dict) -> str:
    parts = []
    for k, lbl in REVIEW_LABELS.items():
        if R.get(k):
            parts.append(f"{lbl} :\n" + "\n".join(f"  - {x}" for x in R[k]))
    return "\n\n".join(parts) if parts else "Rien à signaler : effectifs, fiches, postes et publication sont à jour."


def _fmt(items) -> str:
    lines = []
    for a in items:
        lines.append(f"  {'⛔' if a['level'] == 'danger' else ('ℹ️' if a['level'] == 'info' else '⚠️')} [{a.get('rule')}] semaine du {a['week'][8:10]}/{a['week'][5:7]} · {a['text']}")
    return "\n".join(lines) if lines else "  aucun écart"


def run_control(ctx, company_code: str = config.COMPANY_CODE, mode: str = "daily", force_email: bool = False) -> str:
    cfg = service.get_config(company_code)
    A = cfg.get("alerts") or {}
    ctx.log(f"contrôle {mode} · règles actives : {sum(1 for v in (A.get('rules') or {}).values() if v)}/{len(config.RULES_CATALOG)}")
    if not A.get("enabled") and not force_email:
        ctx.set_report("Contrôles désactivés dans la configuration.")
        return "contrôles désactivés"
    found = collect(company_code, cfg)
    state = _sent_state(company_code)
    seen = set(state.get("keys", []))
    new = {site: [a for a in items if a["key"] not in seen] for site, items in found.items()}
    n_all = sum(len(v) for v in found.values())
    n_new = sum(len(v) for v in new.values())
    ctx.log(f"{n_all} écart(s) en tout · {n_new} nouveau(x)")
    to = [e.strip() for e in (A.get("emails") or "").replace(";", ",").split(",") if "@" in e]
    subject = body = None
    weekly = (mode == "weekly") or (mode == "daily" and A.get("weekly") and date.today().weekday() == 0)
    if weekly and A.get("weekly"):
        review = weekly_review(company_code, cfg)
        subject = f"[Planning] Bilan hebdomadaire — {n_all} écart(s) aux règles"
        body = "Bilan des contrôles du planning (règles de la convention collective et règles internes), semaines passée, en cours et à venir.\n\n" + \
               "\n\n".join(f"{config.SITES.get(site, site)} — {len(items)} écart(s)\n{_fmt(items)}" for site, items in found.items()) + \
               "\n\n=== REVUE DE LA SEMAINE : changements et points d'attention ===\n\n" + _fmt_review(review)
    elif n_new and (A.get("daily") or force_email):
        subject = f"[Planning] {n_new} nouvel(s) écart(s) détecté(s)"
        body = "Nouveaux écarts détectés par le contrôle quotidien du planning :\n\n" + \
               "\n\n".join(f"{config.SITES.get(site, site)}\n{_fmt(items)}" for site, items in new.items() if items) + \
               f"\n\nTotal des écarts ouverts : {n_all}. Détail et réglages : {service.admin_url() if hasattr(service, 'admin_url') else 'https://vaelan.com'}/c/{company_code}/planning/controles"
    sent = "aucun email"
    if subject and to:
        ok, info = mailer.send(to, subject, body + "\n\n— Vaelan, contrôle automatique du planning", reply_to=None)
        sent = f"email {'envoyé' if ok else 'en échec'} à {', '.join(to)} ({info})"
        ctx.log(sent)
    elif subject and not to:
        sent = "aucun destinataire configuré"
    # mémoriser les écarts vus (fenêtre glissante : on garde les clés encore présentes + les nouvelles)
    keys = [a["key"] for items in found.values() for a in items]
    state = {"keys": keys, "last_run": datetime.utcnow().isoformat(), "last_mode": mode, "n": n_all, "sent": sent}
    _save_sent(company_code, state)
    report = f"CONTRÔLE PLANNING ({mode}) — {datetime.now():%d/%m/%Y %H:%M}\n\n" + "\n\n".join(f"{config.SITES.get(site, site)} — {len(items)} écart(s), {len(new[site])} nouveau(x)\n{_fmt(items)}" for site, items in found.items()) + f"\n\n{sent}"
    ctx.set_report(report)
    return f"{n_all} écart(s), {n_new} nouveau(x) — {sent}"
