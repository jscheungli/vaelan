"""Remplacement guidé : pour une absence déclarée, propose des options classées (déplacements sans appel,
personnes à appeler par ordre, renforts d'autres établissements) et applique la décision validée."""
import json
from datetime import date, timedelta
from typing import List, Dict, Optional

from sqlmodel import Session, select

from app.core.db import engine
from app.models import PlEmployee, PlShift, PlIncident
from . import service, config


def _hours_week(company_code, eid, d):
    m = service.week_monday(d)
    return sum(x.hours for x in service.employee_shifts(company_code, eid, m, m + timedelta(days=6)) if x.kind == "work")


def _rest_ok(company_code, eid, d, start_hm, R):
    prev = [x for x in service.employee_shifts(company_code, eid, d - timedelta(days=1), d - timedelta(days=1)) if x.kind == "work" and x.end]
    if not prev:
        return True
    end_prev = max(service.hm_to_h(x.end) + (24 if service.hm_to_h(x.end) < service.hm_to_h(x.start) else 0) for x in prev)
    return service.hm_to_h(start_hm) + 24 - end_prev >= R["rest_min_hours"]


def build_plan(company_code: str, site: str, employee_id: int, d_from: date, d_to: date) -> dict:
    """Plages touchées + options par plage."""
    cfg = service.get_config(company_code)
    R = cfg["rules"]
    absent = service.get_employee(employee_id)
    affected = [x for x in service.shifts(company_code, site, d_from, d_to, employee_id) if x.kind == "work"]
    with Session(engine) as s:
        all_emps = [e for e in s.exec(select(PlEmployee).where(PlEmployee.company_code == company_code, PlEmployee.active == True)).all() if e.id != employee_id]  # noqa: E712
    per_shift = []
    for sh in affected:
        d = sh.date
        pk = sh.post_key
        opts = {"calls": [], "moves": [], "reinforcements": [], "swaps": []}
        monday = service.week_monday(d)
        week_rows = service.shifts(company_code, site, monday, monday + timedelta(days=6))
        def surplus(day, post_key):
            need = service.coverage_for(cfg, day).get(post_key, 0)
            have = sum(1 for y in week_rows if y.date == day and y.kind == "work" and y.post_key == post_key and y.employee_id)
            return have - need
        for e in all_emps:
            ps = service.e_posts(e)
            lvl = ps.get(pk, 0)
            if not lvl:
                continue
            if e.end_date and e.end_date < d or e.start_date and e.start_date > d:
                continue
            day = service.employee_shifts(company_code, e.id, d, d)
            if any(x.kind == "absence" for x in day):
                continue
            if d.weekday() == 6 and e.sunday == "non":
                continue
            working = [x for x in day if x.kind == "work"]
            wh = _hours_week(company_code, e.id, d)
            base = lvl * 10 + e.flexibility * 6 - e.priority * 2 + (8 if e.site == site else 0)
            if not working:
                wk_days = len({x.date for x in service.employee_shifts(company_code, e.id, monday, monday + timedelta(days=6)) if x.kind == "work"})
                if d.weekday() in service.e_list(e, "cfa_days") or not _rest_ok(company_code, e.id, d, sh.start, R):
                    continue
                legal = wh + sh.hours <= R["week_max_hours"] and wk_days < 6
                heavy = wk_days >= e.max_days or wh + sh.hours > e.weekly_hours + R.get("overtime_tolerance", 3)
                if heavy or not legal:
                    # échange de jour : elle prend cette plage et libère une plage à venir de la semaine, sur un jour en surplus
                    for x in service.employee_shifts(company_code, e.id, monday, monday + timedelta(days=6)):
                        if x.kind != "work" or x.site != site or x.date == d or x.date < date.today():
                            continue
                        if surplus(x.date, x.post_key) >= 1:
                            opts["swaps"].append({"employee_id": e.id, "name": service.full_name(e), "release_shift_id": x.id, "phone": e.phone, "telegram": e.telegram,
                                                  "level": lvl, "flex": e.flexibility, "priority": e.priority, "week_hours": round(wh, 1), "contract": e.weekly_hours,
                                                  "text": f"{service.full_name(e)} est libre ce jour mais déjà à {wk_days} jours / {wh:.0f} h : lui proposer de prendre cette plage et de libérer {x.post_key.replace('_', ' ').title()} du {x.date:%d/%m} ({x.start}-{x.end}), jour en surplus",
                                                  "score": base - 4, "status": "à contacter"})
                            break
                if not legal:
                    continue
                if heavy:
                    base -= 12
            if not working:
                item = {"employee_id": e.id, "name": service.full_name(e), "site": e.site, "phone": e.phone, "telegram": e.telegram,
                        "level": lvl, "flex": e.flexibility, "priority": e.priority, "week_hours": round(wh, 1), "contract": e.weekly_hours,
                        "score": base + (4 if d.weekday() in service.e_list(e, "days_off") else 0), "status": "à contacter", "note": ("6e jour" if wk_days >= 5 else "")}
                if e.site == site:
                    opts["calls"].append(item)
                elif site in service.e_list(e, "mobility") or e.site in cfg.get("other_sites", []):
                    item["score"] -= 6
                    opts["reinforcements"].append(item)
            else:
                # déjà sur place ce jour-là sur un autre poste : basculer si son poste reste couvert
                for x in working:
                    if x.site != site or x.post_key == pk:
                        continue
                    need = service.coverage_for(cfg, d).get(x.post_key, 0)
                    have = sum(1 for y in service.shifts(company_code, site, d, d) if y.kind == "work" and y.post_key == x.post_key and y.employee_id)
                    if have - 1 >= need:
                        opts["moves"].append({"employee_id": e.id, "name": service.full_name(e), "shift_id": x.id, "from_post": x.post_key,
                                              "text": f"{service.full_name(e)} est déjà là ({x.post_key.replace('_', ' ').title()} {x.start}-{x.end}) : le basculer sur {pk.replace('_', ' ').title()}, son poste reste couvert ({have - 1}/{need})",
                                              "score": base + 15})
        for k in opts:
            opts[k].sort(key=lambda o: -o["score"])
        per_shift.append({"shift_id": sh.id, "date": d.isoformat(), "post": pk, "start": sh.start, "end": sh.end, "hours": sh.hours, "options": opts,
                          "plan_index": 0, "resolution": None})
    return {"employee": service.full_name(absent) if absent else "?", "employee_id": employee_id, "site": site, "shifts": per_shift,
            "parallel": int(cfg.get("replacement", {}).get("parallel", 3)), "answer_minutes": int(cfg.get("replacement", {}).get("answer_minutes", 30))}


def current_step(plan: dict, shift_entry: dict) -> dict:
    """Ce que l'opérateur doit faire maintenant pour cette plage : plan A/B/C = tranches de `parallel` appels,
    précédées des déplacements sans appel."""
    n = plan.get("parallel", 3)
    calls = [c for c in shift_entry["options"]["calls"] if c["status"] in ("à contacter", "contacté", "pas de réponse")]
    calls_all = shift_entry["options"]["calls"]
    idx = shift_entry.get("plan_index", 0)
    tranche = calls_all[idx * n:(idx + 1) * n]
    letters = "ABCDEFGH"
    steps = []
    moves = [m for m in shift_entry["options"]["moves"] if m.get("status") != "refusé"]
    if idx == 0 and moves:
        steps.append({"kind": "move", "title": "Sans appel : déplacement possible", "items": moves[:2]})
    swaps = [m for m in shift_entry["options"].get("swaps", []) if m.get("status") in ("à contacter", "pas de réponse")]
    if idx == 0 and swaps:
        steps.append({"kind": "swap", "title": "Échange de jour (sans heures supplémentaires)", "items": swaps[:2]})
    if tranche:
        steps.append({"kind": "call", "title": f"Plan {letters[idx]} — appeler en parallèle", "items": tranche,
                      "hint": f"Pas de réponse sous {plan.get('answer_minutes', 30)} min → passer au plan suivant."})
    else:
        reinf = [r for r in shift_entry["options"]["reinforcements"] if r.get("status") in ("à contacter", "pas de réponse")][:n]
        if reinf:
            steps.append({"kind": "reinforcement", "title": "Renfort d'un autre établissement", "items": reinf})
        elif not steps:
            steps.append({"kind": "none", "title": "Plus d'option : plage à laisser non couverte ou à couper (accord du responsable)", "items": []})
    return {"steps": steps, "plan_letter": letters[min(idx, len(letters) - 1)]}


def record_answer(inc: PlIncident, shift_id: int, employee_id: int, outcome: str, by: str = None) -> PlIncident:
    """outcome : oui / non / pas_de_reponse / next (passer au plan suivant) / unassign (laisser non couverte)."""
    plan = json.loads(inc.plan or "{}")
    entry = next((x for x in plan["shifts"] if x["shift_id"] == shift_id), None)
    if not entry:
        return inc
    log = plan.setdefault("log", [])
    if outcome == "next":
        entry["plan_index"] = entry.get("plan_index", 0) + 1
        log.append({"shift_id": shift_id, "event": "plan suivant", "by": by})
    elif outcome == "unassign":
        entry["resolution"] = {"kind": "unassigned"}
        log.append({"shift_id": shift_id, "event": "laissée non couverte", "by": by})
        _apply_unassign(inc, entry)
    else:
        for grp in ("calls", "reinforcements", "moves", "swaps"):
            for c in entry["options"][grp]:
                if c["employee_id"] == employee_id:
                    c["status"] = {"oui": "disponible", "non": "pas disponible", "pas_de_reponse": "pas de réponse", "refuse": "refusé"}.get(outcome, outcome)
                    log.append({"shift_id": shift_id, "event": f"{c['name']} : {c['status']}", "by": by})
                    if outcome == "oui":
                        entry["resolution"] = {"kind": grp, "employee_id": employee_id, "name": c["name"]}
                        _apply_assign(inc, entry, c, grp)
    inc.plan = json.dumps(plan, ensure_ascii=False)
    if all(x.get("resolution") for x in plan["shifts"]):
        inc.status = "resolved"
    return service.save_incident(inc)


def _apply_assign(inc: PlIncident, entry: dict, cand: dict, grp: str) -> None:
    """Applique la décision : l'absent passe en absence, la plage est réassignée (ou le déplacement effectué)."""
    with Session(engine) as s:
        sh = s.get(PlShift, entry["shift_id"])
        if not sh:
            return
        s.add(PlShift(company_code=inc.company_code, site=inc.site, employee_id=inc.employee_id, date=sh.date, kind="absence", post_key=inc.reason,
                      hours=sh.hours, status=sh.status, source="replacement", note=f"incident #{inc.id}"))
        if grp == "swaps" and cand.get("release_shift_id"):
            rel = s.get(PlShift, cand["release_shift_id"])
            if rel:
                s.delete(rel)
            sh.employee_id = cand["employee_id"]
            sh.source = "replacement"
            sh.note = f"échange de jour (incident #{inc.id})"
            s.add(sh)
        elif grp == "moves" and cand.get("shift_id"):
            other = s.get(PlShift, cand["shift_id"])
            if other:
                other.post_key = sh.post_key
                other.start, other.end, other.pause, other.hours = sh.start, sh.end, sh.pause, sh.hours
                other.note = f"basculé (incident #{inc.id})"
                other.source = "replacement"
                s.add(other)
            s.delete(sh)
        else:
            sh.employee_id = cand["employee_id"]
            sh.source = "replacement"
            sh.note = f"remplace {entry.get('absent', '')} (incident #{inc.id})".strip()
            s.add(sh)
        s.commit()


def _apply_unassign(inc: PlIncident, entry: dict) -> None:
    with Session(engine) as s:
        sh = s.get(PlShift, entry["shift_id"])
        if not sh:
            return
        s.add(PlShift(company_code=inc.company_code, site=inc.site, employee_id=inc.employee_id, date=sh.date, kind="absence", post_key=inc.reason,
                      hours=sh.hours, status=sh.status, source="replacement", note=f"incident #{inc.id}"))
        sh.employee_id = None
        sh.source = "replacement"
        sh.note = f"non couverte (incident #{inc.id})"
        s.add(sh)
        s.commit()


def open_incident(company_code: str, site: str, employee_id: int, d_from: date, d_to: date, reason: str, by: str = None) -> PlIncident:
    plan = build_plan(company_code, site, employee_id, d_from, d_to)
    for x in plan["shifts"]:
        x["absent"] = plan["employee"]
    inc = PlIncident(company_code=company_code, site=site, employee_id=employee_id, date_from=d_from, date_to=d_to, reason=reason,
                     plan=json.dumps(plan, ensure_ascii=False), created_by=by)
    return service.save_incident(inc)
