"""Planning des équipes — accès aux données, règles, alertes, utilitaires de semaine."""
import json
import unicodedata
import re
from datetime import date, datetime, timedelta, time
from typing import List, Optional, Dict, Tuple

from sqlmodel import Session, select

from app.core.db import engine
from app.models import Setting, PlEmployee, PlPost, PlShift, PlIncident
from . import config

CODE = config.COMPANY_CODE


# ------------------------------------------------------------------ configuration
def _deep_update(base: dict, upd: dict) -> dict:
    for k, v in (upd or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def get_config(company_code: str = CODE) -> dict:
    cfg = json.loads(json.dumps(config.DEFAULT_CONFIG))
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == company_code, Setting.key == "planning:config")).first()
    if st:
        try:
            _deep_update(cfg, json.loads(st.value))
        except Exception:
            pass
    return cfg


def save_config(values: dict, company_code: str = CODE) -> dict:
    cfg = get_config(company_code)
    _deep_update(cfg, values)
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == company_code, Setting.key == "planning:config")).first()
        if not st:
            st = Setting(company_code=company_code, key="planning:config", value="{}")
        st.value = json.dumps(cfg, ensure_ascii=False)
        st.updated_at = datetime.utcnow()
        s.add(st)
        s.commit()
    return cfg


def configured(company_code: str = CODE) -> bool:
    return bool(get_config(company_code).get("validated_at"))


# ------------------------------------------------------------------ postes
def ensure_posts(company_code: str = CODE, site: str = "SL") -> None:
    """Crée les postes par défaut du site s'il n'en a aucun."""
    with Session(engine) as s:
        if s.exec(select(PlPost).where(PlPost.company_code == company_code, PlPost.site == site)).first():
            return
        for key, label, dep, color, st, en, pause, active, sort in config.POSTS:
            s.add(PlPost(company_code=company_code, site=site, key=key, label=label, department=dep, color=color,
                         start=st, end=en, pause=pause, active=active, sort=sort))
        s.commit()


def posts(company_code: str = CODE, site: str = "SL", active_only: bool = False) -> List[PlPost]:
    with Session(engine) as s:
        q = select(PlPost).where(PlPost.company_code == company_code, PlPost.site == site)
        if active_only:
            q = q.where(PlPost.active == True)  # noqa: E712
        return sorted(s.exec(q).all(), key=lambda p: (p.sort, p.label))


def post_map(company_code: str = CODE, site: str = "SL") -> Dict[str, PlPost]:
    return {p.key: p for p in posts(company_code, site)}


def save_post(pid: Optional[int], company_code: str, site: str, **fields) -> PlPost:
    with Session(engine) as s:
        p = s.get(PlPost, pid) if pid else None
        if not p:
            p = PlPost(company_code=company_code, site=site, key=fields.get("key") or _norm_key(fields.get("label", "POSTE")), label=fields.get("label", ""))
        for k, v in fields.items():
            if hasattr(p, k) and k != "id":
                setattr(p, k, v)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def _norm_key(label: str) -> str:
    t = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode().upper()
    t = re.sub(r"[^A-Z0-9]+", "_", t).strip("_")
    return t[:40] or "POSTE"


# ------------------------------------------------------------------ salariés
def employees(company_code: str = CODE, site: str = None, active_only: bool = True, on: date = None) -> List[PlEmployee]:
    with Session(engine) as s:
        q = select(PlEmployee).where(PlEmployee.company_code == company_code)
        if site:
            q = q.where(PlEmployee.site == site)
        if active_only:
            q = q.where(PlEmployee.active == True)  # noqa: E712
        rows = list(s.exec(q).all())
    if on:
        rows = [e for e in rows if (not e.start_date or e.start_date <= on) and (not e.end_date or e.end_date >= on)]
    return sorted(rows, key=lambda e: (_dept_rank(e), e.last_name.upper(), e.first_name))


def _dept_rank(e: PlEmployee) -> int:
    main = primary_post(e)
    order = ["ADMINISTRATIF", "VENTE", "TRAITEUR", "PATISSERIE", "BOULANGERIE"]
    for i, o in enumerate(order):
        if main and main.startswith(o):
            return i
    return 9


def primary_post(e: PlEmployee) -> Optional[str]:
    ps = e_posts(e)
    return max(ps, key=lambda k: ps[k]) if ps else None


def e_posts(e: PlEmployee) -> Dict[str, int]:
    try:
        return {k: int(v) for k, v in json.loads(e.posts or "{}").items()}
    except Exception:
        return {}


def e_list(e: PlEmployee, field: str) -> list:
    try:
        return list(json.loads(getattr(e, field) or "[]"))
    except Exception:
        return []


def full_name(e: PlEmployee) -> str:
    return f"{e.first_name} {e.last_name}"


def get_employee(eid: int) -> Optional[PlEmployee]:
    with Session(engine) as s:
        return s.get(PlEmployee, eid)


def save_employee(eid: Optional[int], company_code: str, **fields) -> PlEmployee:
    with Session(engine) as s:
        e = s.get(PlEmployee, eid) if eid else None
        if not e:
            e = PlEmployee(company_code=company_code, site=fields.get("site", "SL"), first_name=fields.get("first_name", ""), last_name=fields.get("last_name", ""))
        for k, v in fields.items():
            if hasattr(e, k) and k != "id":
                setattr(e, k, v)
        s.add(e)
        s.commit()
        s.refresh(e)
        return e


# ------------------------------------------------------------------ temps
def hm_to_h(hm: str) -> float:
    h, m = (hm or "00:00").split(":")[:2]
    return int(h) + int(m) / 60


def h_to_hm(h: float) -> str:
    h = max(0.0, min(h, 23.99))
    return f"{int(h):02d}:{int(round((h - int(h)) * 60)):02d}"


def shift_hours(start: str, end: str, pause: float) -> float:
    a, b = hm_to_h(start), hm_to_h(end)
    if b < a:
        b += 24
    return round(max(0.0, b - a - (pause or 0)), 2)


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def season_of(d: date) -> str:
    return config.SEASON_OF_MONTH.get(d.month, "default")


def coverage_for(cfg: dict, d: date) -> Dict[str, int]:
    """Personnes attendues par poste pour une date (saison + jour de semaine + fériés)."""
    cov = cfg.get("coverage", {})
    base = dict(cov.get("default", {}))
    base.update(cov.get(season_of(d), {}))
    if d.isoformat() in cfg.get("holidays", {}).get("closed", []):
        return {}
    wd = d.weekday()
    return {k: int(v[wd]) for k, v in base.items() if v[wd] > 0}


# ------------------------------------------------------------------ plages
def shifts(company_code: str, site: str, d_from: date, d_to: date, employee_id: int = None) -> List[PlShift]:
    with Session(engine) as s:
        q = select(PlShift).where(PlShift.company_code == company_code, PlShift.site == site, PlShift.date >= d_from, PlShift.date <= d_to)
        if employee_id:
            q = q.where(PlShift.employee_id == employee_id)
        return sorted(s.exec(q).all(), key=lambda x: (x.date, x.start or "", x.id))


def employee_shifts(company_code: str, employee_id: int, d_from: date, d_to: date) -> List[PlShift]:
    """Toutes les plages d'un salarié, tous sites confondus (mobilité)."""
    with Session(engine) as s:
        q = select(PlShift).where(PlShift.company_code == company_code, PlShift.employee_id == employee_id, PlShift.date >= d_from, PlShift.date <= d_to)
        return sorted(s.exec(q).all(), key=lambda x: (x.date, x.start or ""))


def save_shift(sid: Optional[int], company_code: str, site: str, **fields) -> PlShift:
    with Session(engine) as s:
        sh = s.get(PlShift, sid) if sid else None
        if not sh:
            sh = PlShift(company_code=company_code, site=site, date=fields.get("date"))
        for k, v in fields.items():
            if hasattr(sh, k) and k != "id":
                setattr(sh, k, v)
        if sh.kind == "work" and sh.start and sh.end:
            sh.hours = shift_hours(sh.start, sh.end, sh.pause or 0)
        sh.updated_at = datetime.utcnow()
        s.add(sh)
        s.commit()
        s.refresh(sh)
        return sh


def delete_shift(sid: int) -> None:
    with Session(engine) as s:
        sh = s.get(PlShift, sid)
        if sh:
            s.delete(sh)
            s.commit()


def copy_week(company_code: str, site: str, src_monday: date, dst_monday: date, replace: bool = True) -> int:
    """Copie toutes les plages d'une semaine vers une autre (en brouillon)."""
    src = shifts(company_code, site, src_monday, src_monday + timedelta(days=6))
    with Session(engine) as s:
        if replace:
            for x in s.exec(select(PlShift).where(PlShift.company_code == company_code, PlShift.site == site,
                                                   PlShift.date >= dst_monday, PlShift.date <= dst_monday + timedelta(days=6))).all():
                s.delete(x)
        n = 0
        for x in src:
            s.add(PlShift(company_code=company_code, site=site, employee_id=x.employee_id, date=dst_monday + timedelta(days=x.date.weekday()),
                          kind=x.kind, post_key=x.post_key, start=x.start, end=x.end, pause=x.pause, hours=x.hours, note=None,
                          status="draft", source="manual"))
            n += 1
        s.commit()
    return n


def publish_week(company_code: str, site: str, monday: date, status: str = "published") -> int:
    with Session(engine) as s:
        rows = s.exec(select(PlShift).where(PlShift.company_code == company_code, PlShift.site == site,
                                             PlShift.date >= monday, PlShift.date <= monday + timedelta(days=6))).all()
        for x in rows:
            x.status = status
            s.add(x)
        s.commit()
        return len(rows)


# ------------------------------------------------------------------ analyse d'une semaine (totaux, couverture, alertes)
def week_data(company_code: str, site: str, monday: date) -> dict:
    cfg = get_config(company_code)
    sunday = monday + timedelta(days=6)
    rows = shifts(company_code, site, monday, sunday)
    emps = employees(company_code, site, on=monday)
    # salariés d'autres sites planifiés ici cette semaine (mobilité)
    ids = {x.employee_id for x in rows if x.employee_id}
    known = {e.id for e in emps}
    with Session(engine) as s:
        extra = [s.get(PlEmployee, i) for i in ids - known]
    emps = emps + [e for e in extra if e]
    pmap = post_map(company_code, site)
    days = [monday + timedelta(days=i) for i in range(7)]
    need = {d.isoformat(): coverage_for(cfg, d) for d in days}
    have = {d.isoformat(): {} for d in days}
    for x in rows:
        if x.kind == "work" and x.post_key and x.employee_id:
            have[x.date.isoformat()][x.post_key] = have[x.date.isoformat()].get(x.post_key, 0) + 1
    totals = {}
    for e in emps:
        mine = [x for x in rows if x.employee_id == e.id]
        worked = sum(x.hours for x in mine if x.kind == "work")
        absent = sum(x.hours for x in mine if x.kind == "absence")
        totals[e.id] = {"work": round(worked, 2), "absence": round(absent, 2), "days": len({x.date for x in mine if x.kind == "work"}),
                        "contract": e.weekly_hours, "delta": round(worked + absent - e.weekly_hours, 2)}
    alerts = check_rules(company_code, site, monday, cfg, rows, emps)
    return {"monday": monday, "days": days, "shifts": rows, "employees": emps, "posts": pmap, "need": need, "have": have,
            "totals": totals, "alerts": alerts, "cfg": cfg,
            "published": bool(rows) and all(x.status == "published" for x in rows)}


def check_rules(company_code: str, site: str, monday: date, cfg: dict = None, rows: List[PlShift] = None, emps: List[PlEmployee] = None) -> List[dict]:
    """Contrôle des règles sur une semaine (avec la veille et le lendemain pour les enchaînements)."""
    cfg = cfg or get_config(company_code)
    R = cfg["rules"]
    sunday = monday + timedelta(days=6)
    rows = rows if rows is not None else shifts(company_code, site, monday, sunday)
    emps = emps if emps is not None else employees(company_code, site, on=monday)
    alerts = []
    days = [monday + timedelta(days=i) for i in range(7)]
    # couverture
    for d in days:
        need = coverage_for(cfg, d)
        have = {}
        for x in rows:
            if x.date == d and x.kind == "work" and x.post_key and x.employee_id:
                have[x.post_key] = have.get(x.post_key, 0) + 1
        for k, n in need.items():
            if have.get(k, 0) < n:
                alerts.append({"level": "danger", "date": d.isoformat(), "post": k, "employee_id": None,
                               "text": f"{config.DAYS_SHORT[d.weekday()]} {d:%d/%m} · {k.replace('_', ' ').title()} : {have.get(k, 0)}/{n} personne(s)"})
    unassigned = [x for x in rows if x.kind == "work" and not x.employee_id]
    for x in unassigned:
        alerts.append({"level": "danger", "date": x.date.isoformat(), "post": x.post_key, "employee_id": None,
                       "text": f"{config.DAYS_SHORT[x.date.weekday()]} {x.date:%d/%m} · plage {x.start}-{x.end} {x.post_key} non assignée"})
    # par salarié : avec 1 jour avant / après pour repos et enchaînements
    for e in emps:
        mine = employee_shifts(company_code, e.id, monday - timedelta(days=8), sunday + timedelta(days=1))
        work = [x for x in mine if x.kind == "work" and x.start and x.end]
        byday = {}
        for x in work:
            byday.setdefault(x.date, []).append(x)
        wk_hours = sum(x.hours for x in work if monday <= x.date <= sunday)
        if wk_hours > R["week_max_hours"]:
            alerts.append({"level": "danger", "date": None, "post": None, "employee_id": e.id, "text": f"{full_name(e)} : {wk_hours:.1f} h cette semaine (max {R['week_max_hours']:.0f} h)"})
        elif wk_hours > e.weekly_hours + R.get("overtime_tolerance", 2):
            alerts.append({"level": "warning", "date": None, "post": None, "employee_id": e.id, "text": f"{full_name(e)} : {wk_hours:.1f} h planifiées pour un contrat de {e.weekly_hours:.0f} h"})
        for d, xs in byday.items():
            if not (monday <= d <= sunday):
                continue
            h = sum(x.hours for x in xs)
            if h > R["day_max_hours"]:
                alerts.append({"level": "danger", "date": d.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} · {d:%d/%m} : {h:.1f} h dans la journée (max {R['day_max_hours']:.0f} h)"})
            span = max(hm_to_h(x.end) + (24 if hm_to_h(x.end) < hm_to_h(x.start) else 0) for x in xs) - min(hm_to_h(x.start) for x in xs)
            if span > R["day_max_span"]:
                alerts.append({"level": "warning", "date": d.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} · {d:%d/%m} : amplitude {span:.1f} h (max {R['day_max_span']:.0f} h)"})
            if d.weekday() in e_list(e, "days_off"):
                alerts.append({"level": "warning", "date": d.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} · {d:%d/%m} : jour habituellement non travaillé"})
            if d.weekday() in e_list(e, "cfa_days"):
                alerts.append({"level": "danger", "date": d.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} · {d:%d/%m} : jour de CFA"})
            if d.weekday() == 6 and e.sunday == "non":
                alerts.append({"level": "danger", "date": d.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} · dimanche {d:%d/%m} : ne travaille pas le dimanche"})
        # repos quotidien
        ds = sorted(byday)
        for a, b in zip(ds, ds[1:]):
            if (b - a).days == 1 and monday <= b <= sunday + timedelta(days=1):
                end_a = max(hm_to_h(x.end) + (24 if hm_to_h(x.end) < hm_to_h(x.start) else 0) for x in byday[a])
                start_b = min(hm_to_h(x.start) for x in byday[b]) + 24
                rest = start_b - end_a
                if rest < R["rest_min_hours"]:
                    alerts.append({"level": "danger", "date": b.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} · {b:%d/%m} : {rest:.1f} h de repos depuis la veille (min {R['rest_min_hours']:.0f} h)"})
        # jours consécutifs
        run, prev = 0, None
        for d in ds:
            run = run + 1 if prev and (d - prev).days == 1 else 1
            prev = d
            if run > R["consecutive_max_days"] and monday <= d <= sunday:
                alerts.append({"level": "danger", "date": d.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} · {d:%d/%m} : {run}e jour consécutif (max {R['consecutive_max_days']})"})
        wk_days = len({x.date for x in work if monday <= x.date <= sunday})
        if wk_days > e.max_days:
            alerts.append({"level": "warning", "date": None, "post": None, "employee_id": e.id, "text": f"{full_name(e)} : {wk_days} jours travaillés (max {e.max_days})"})
        # dimanches consécutifs
        sundays = sorted({x.date for x in work if x.date.weekday() == 6})
        if len(sundays) >= 2 and sunday in sundays and (sunday - timedelta(days=7)) in sundays and cfg["sunday"]["consecutive_max"] < 2:
            alerts.append({"level": "warning", "date": sunday.isoformat(), "post": None, "employee_id": e.id, "text": f"{full_name(e)} : deux dimanches consécutifs"})
    # encadrement
    sup = cfg.get("supervision", {})
    if sup.get("manager_required"):
        mgr_ids = {e.id for e in emps if e_posts(e).get("VENTE_RESP") or (primary_post(e) in sup.get("manager_posts", []) and e_posts(e).get(primary_post(e), 0) >= 3 and e.contract_type == "CDI")}
        for d in days:
            if not coverage_for(cfg, d):
                continue
            if not any(x.date == d and x.kind == "work" and x.employee_id in mgr_ids for x in rows):
                alerts.append({"level": "warning", "date": d.isoformat(), "post": None, "employee_id": None, "text": f"{config.DAYS_SHORT[d.weekday()]} {d:%d/%m} : aucun responsable de vente planifié"})
    order = {"danger": 0, "warning": 1}
    return sorted(alerts, key=lambda a: (order[a["level"]], a["date"] or "", a["text"]))


# ------------------------------------------------------------------ incidents
def incidents(company_code: str, site: str = None, open_only: bool = True) -> List[PlIncident]:
    with Session(engine) as s:
        q = select(PlIncident).where(PlIncident.company_code == company_code)
        if site:
            q = q.where(PlIncident.site == site)
        if open_only:
            q = q.where(PlIncident.status == "open")
        return sorted(s.exec(q).all(), key=lambda i: -i.id)


def get_incident(iid: int) -> Optional[PlIncident]:
    with Session(engine) as s:
        return s.get(PlIncident, iid)


def save_incident(inc: PlIncident) -> PlIncident:
    with Session(engine) as s:
        inc.updated_at = datetime.utcnow()
        s.add(inc)
        s.commit()
        s.refresh(inc)
        return inc
