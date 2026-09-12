"""Remplacement guidé : pour une absence déclarée, chaque plage touchée reçoit une liste d'options classées
(A, B, C…) dans tout le pool de la société — établissement d'origine d'abord — avec, pour chacune, le raisonnement
en clair et la chaîne de conséquences. L'opérateur choisit ; rien n'est modifié tant qu'une option n'est pas validée."""
import json
from datetime import date, timedelta
from typing import List, Dict, Optional

from sqlmodel import Session, select

from app.core.db import engine
from app.models import PlEmployee, PlShift, PlIncident
from . import service, config

P = lambda k: (k or "").replace("_", " ").title()


def _week_shifts(company_code, eid, d):
    m = service.week_monday(d)
    return [x for x in service.employee_shifts(company_code, eid, m, m + timedelta(days=6)) if x.kind == "work"]


def _rest_before(company_code, eid, d, start_hm):
    """Repos entre la fin de la veille et le début de la plage (None si pas de plage la veille)."""
    prev = [x for x in service.employee_shifts(company_code, eid, d - timedelta(days=1), d - timedelta(days=1)) if x.kind == "work" and x.end]
    if not prev:
        return None
    end_prev = max(service.hm_to_h(x.end) + (24 if service.hm_to_h(x.end) < service.hm_to_h(x.start) else 0) for x in prev)
    return service.hm_to_h(start_hm) + 24 - end_prev


def _rest_after(company_code, eid, d, end_hm):
    nxt = [x for x in service.employee_shifts(company_code, eid, d + timedelta(days=1), d + timedelta(days=1)) if x.kind == "work" and x.start]
    if not nxt:
        return None
    return service.hm_to_h(min(x.start for x in nxt)) + 24 - service.hm_to_h(end_hm)


def _consecutive(company_code, eid, d):
    n, dd = 0, d - timedelta(days=1)
    while [x for x in service.employee_shifts(company_code, eid, dd, dd) if x.kind == "work"]:
        n += 1
        dd -= timedelta(days=1)
    return n


def build_plan(company_code: str, site: str, employee_id: int, d_from: date, d_to: date) -> dict:
    cfg = service.get_config(company_code)
    R = cfg["rules"]
    absent = service.get_employee(employee_id)
    affected = [x for x in service.shifts(company_code, site, d_from, d_to, employee_id) if x.kind == "work"]
    with Session(engine) as s:
        pool = [e for e in s.exec(select(PlEmployee).where(PlEmployee.company_code == company_code, PlEmployee.active == True)).all() if e.id != employee_id]  # noqa: E712
    per_shift = []
    for sh in affected:
        d, pk = sh.date, sh.post_key
        monday = service.week_monday(d)
        site_rows = service.shifts(company_code, site, monday, monday + timedelta(days=6))
        need_day = service.coverage_for(cfg, d, site)

        def have(day, post_key):
            return sum(1 for y in site_rows if y.date == day and y.kind == "work" and y.post_key == post_key and y.employee_id)

        options = []
        for e in pool:
            lvl = service.e_posts(e).get(pk, 0)
            if not lvl or (e.end_date and e.end_date < d) or (e.start_date and e.start_date > d):
                continue
            day = service.employee_shifts(company_code, e.id, d, d)
            if any(x.kind == "absence" for x in day):
                continue
            if d.weekday() == 6 and e.sunday == "non":
                continue
            wk = _week_shifts(company_code, e.id, d)
            wh = sum(x.hours for x in wk)
            wk_days = len({x.date for x in wk})
            working = [x for x in day if x.kind == "work"]
            same_site = e.site == site
            mobile = site in service.e_list(e, "mobility")
            why, cons = [], []
            why.append(f"niveau {lvl} sur {P(pk)}" + (" (poste préféré)" if lvl == 1 else ""))
            score = service.level_weight(lvl) * 4 + e.flexibility * 5 - e.priority * 2
            if same_site:
                score += 12
                why.append("de l'établissement")
            else:
                score += 4 if mobile else -6
                why.append(f"de {config.SITES.get(e.site, e.site)}" + (", se déclare mobile vers ici" if mobile else ", mobilité non déclarée"))
                cons.append("déplacement inter-établissements")
            base_item = {"employee_id": e.id, "name": service.full_name(e), "site": e.site, "phone": e.phone, "telegram": e.telegram, "level": lvl,
                         "week_hours": round(wh, 1), "contract": e.weekly_hours, "status": "à contacter"}
            if not working:
                if d.weekday() in service.e_list(e, "cfa_days"):
                    continue
                rb = _rest_before(company_code, e.id, d, sh.start)
                if rb is not None and rb < R["rest_min_hours"]:
                    continue
                ra = _rest_after(company_code, e.id, d, sh.end)
                new_h = wh + sh.hours
                legal = new_h <= R["week_max_hours"] and wk_days < 6
                if legal:
                    why.append("libre ce jour")
                    if d.weekday() in service.e_list(e, "days_off"):
                        cons.append("jour habituellement non travaillé pour cette personne")
                        score -= 3
                    if new_h > e.weekly_hours:
                        extra = new_h - e.weekly_hours
                        cons.append(f"passe à {new_h:.1f} h : {extra:.1f} h supplémentaires (+25 % jusqu'à la 43e, +50 % au-delà)")
                        score -= 6 + extra
                    else:
                        why.append(f"{wh:.0f} h faites / {e.weekly_hours:.0f} h : il reste de la marge")
                        score += 4
                    if wk_days >= e.max_days:
                        cons.append(f"{wk_days + 1}e jour de la semaine (au-delà de ses {e.max_days} jours habituels)")
                        score -= 8
                    if _consecutive(company_code, e.id, d) + 1 >= R["consecutive_max_days"]:
                        cons.append(f"{_consecutive(company_code, e.id, d) + 1}e jour consécutif : prévoir un repos le lendemain")
                        score -= 6
                    if ra is not None and ra < R["rest_min_hours"]:
                        cons.append(f"repos insuffisant avant sa plage du lendemain ({ra:.1f} h) : décaler ou retirer cette plage")
                        score -= 15
                    if d.weekday() == 6:
                        recent = sum(1 for k in (7, 14) if [x for x in service.employee_shifts(company_code, e.id, d - timedelta(days=k), d - timedelta(days=k)) if x.kind == "work"])
                        if recent:
                            cons.append(f"a déjà travaillé {recent} des 2 derniers dimanches")
                            score -= 8 * recent
                    options.append({**base_item, "kind": "call", "id": f"call:{e.id}", "score": score, "why": why, "cons": cons,
                                    "action": "l'appeler pour prendre la plage"})
                # échange de jour : elle prend la plage et libère une plage à venir de la semaine sur un jour en surplus
                for x in wk:
                    if x.site != site or x.date == d or x.date < date.today():
                        continue
                    if have(x.date, x.post_key) - 1 >= service.coverage_for(cfg, x.date, site).get(x.post_key, 0):
                        options.append({**base_item, "kind": "swap", "id": f"swap:{e.id}:{x.id}", "release_shift_id": x.id, "score": score - 5,
                                        "why": why + ["libre ce jour", f"sa plage {P(x.post_key)} du {config.DAYS_SHORT[x.date.weekday()]} {x.date:%d/%m} est sur un jour en surplus"],
                                        "cons": cons + [f"libère {P(x.post_key)} du {x.date:%d/%m} ({x.start}-{x.end}) : ce jour reste couvert", "heures de la semaine inchangées : pas d'heures supplémentaires"],
                                        "action": "lui proposer d'échanger ses jours"})
                        break
            else:
                for x in working:
                    if x.site != site or x.post_key == pk:
                        continue
                    need_o = service.coverage_for(cfg, x.date, site).get(x.post_key, 0)
                    if have(x.date, x.post_key) - 1 >= need_o:
                        options.append({**base_item, "kind": "move", "id": f"move:{e.id}:{x.id}", "shift_id": x.id, "score": score + 10,
                                        "why": why + [f"déjà sur place ce jour ({P(x.post_key)} {x.start}-{x.end})", (f"son poste reste couvert sans elle ({have(x.date, x.post_key) - 1}/{need_o})" if need_o else f"{P(x.post_key)} n'a pas de besoin de couverture ce jour")],
                                        "cons": cons + [f"{P(x.post_key)} perd une personne ce jour", f"ses horaires deviennent {sh.start}-{sh.end}"],
                                        "action": "le basculer sur cette plage, sans appel"})
        options.sort(key=lambda o: -o["score"])
        for i, o in enumerate(options[:26]):
            o["letter"] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i]
        per_shift.append({"shift_id": sh.id, "date": d.isoformat(), "post": pk, "start": sh.start, "end": sh.end, "hours": sh.hours,
                          "need": need_day.get(pk, 0), "options": options[:26], "resolution": None})
    return {"employee": service.full_name(absent) if absent else "?", "employee_id": employee_id, "site": site, "shifts": per_shift}


def record_answer(inc: PlIncident, shift_id: int, option_id: str, outcome: str, by: str = None) -> PlIncident:
    """outcome : oui (appliquer) / non / pas_de_reponse / unassign (laisser non couverte)."""
    plan = json.loads(inc.plan or "{}")
    entry = next((x for x in plan["shifts"] if x["shift_id"] == shift_id), None)
    if not entry:
        return inc
    log = plan.setdefault("log", [])
    if outcome == "unassign":
        entry["resolution"] = {"kind": "unassigned"}
        log.append({"shift_id": shift_id, "event": "plage laissée non couverte", "by": by})
        _apply_unassign(inc, entry)
    else:
        opt = next((o for o in entry["options"] if o["id"] == option_id), None)
        if opt:
            opt["status"] = {"oui": "retenue", "non": "pas disponible", "pas_de_reponse": "pas de réponse"}.get(outcome, outcome)
            log.append({"shift_id": shift_id, "event": f"option {opt.get('letter')} · {opt['name']} : {opt['status']}", "by": by})
            if outcome == "oui":
                entry["resolution"] = {"kind": opt["kind"], "employee_id": opt["employee_id"], "name": opt["name"], "letter": opt.get("letter")}
                _apply(inc, entry, opt)
    inc.plan = json.dumps(plan, ensure_ascii=False)
    if all(x.get("resolution") for x in plan["shifts"]):
        inc.status = "resolved"
    return service.save_incident(inc)


def _absence_for(inc: PlIncident, sh: PlShift) -> PlShift:
    return PlShift(company_code=inc.company_code, site=inc.site, employee_id=inc.employee_id, date=sh.date, kind="absence", post_key=inc.reason,
                   hours=sh.hours, status=sh.status, source="replacement", note=f"incident #{inc.id}")


def _apply(inc: PlIncident, entry: dict, opt: dict) -> None:
    with Session(engine) as s:
        sh = s.get(PlShift, entry["shift_id"])
        if not sh:
            return
        s.add(_absence_for(inc, sh))
        if opt["kind"] == "move":
            other = s.get(PlShift, opt["shift_id"])
            if other:
                other.post_key, other.start, other.end, other.pause, other.hours = sh.post_key, sh.start, sh.end, sh.pause, sh.hours
                other.note, other.source = f"basculé (incident #{inc.id})", "replacement"
                s.add(other)
            s.delete(sh)
        else:
            if opt["kind"] == "swap":
                rel = s.get(PlShift, opt["release_shift_id"])
                if rel:
                    s.delete(rel)
            sh.employee_id = opt["employee_id"]
            sh.source = "replacement"
            sh.note = f"remplace {entry.get('absent', '')} (incident #{inc.id})".strip()
            s.add(sh)
        s.commit()


def _apply_unassign(inc: PlIncident, entry: dict) -> None:
    with Session(engine) as s:
        sh = s.get(PlShift, entry["shift_id"])
        if not sh:
            return
        s.add(_absence_for(inc, sh))
        sh.employee_id, sh.source, sh.note = None, "replacement", f"non couverte (incident #{inc.id})"
        s.add(sh)
        s.commit()


def open_incident(company_code: str, site: str, employee_id: int, d_from: date, d_to: date, reason: str, by: str = None) -> PlIncident:
    plan = build_plan(company_code, site, employee_id, d_from, d_to)
    for x in plan["shifts"]:
        x["absent"] = plan["employee"]
    inc = PlIncident(company_code=company_code, site=site, employee_id=employee_id, date_from=d_from, date_to=d_to, reason=reason,
                     plan=json.dumps(plan, ensure_ascii=False), created_by=by)
    return service.save_incident(inc)
