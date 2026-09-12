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
        # ---- chaînes : X bascule sur la plage, et son poste d'origine (sous le besoin sans X) est recouvert par Y (appel / échange), voire Z
        def cover_options(post_key, excluded, depth, chain_posts=()):
            """Options simples pour couvrir `post_key` ce jour-là (appel, échange, déplacement avec surplus), hors personnes déjà engagées
            et sans déplacer quelqu'un depuis un poste déjà engagé dans la chaîne."""
            sub = build_single(post_key, excluded, set(chain_posts) | {post_key})
            out = []
            for o in sub:
                if o["kind"] in ("call", "swap"):
                    out.append([o])
                elif o["kind"] == "move" and depth < 3:
                    # déplacement dont le poste d'origine a du surplus : ok directement ; sinon il faut recouvrir à son tour
                    out.append([o])
            return out

        def build_single(post_key, excluded, chain_posts=()):
            """Réutilise la même logique que ci-dessus pour un autre poste (sans les chaînes)."""
            res = []
            need_pk = service.coverage_for(cfg, d, site).get(post_key, 0)
            for e2 in pool:
                if e2.id in excluded:
                    continue
                lvl2 = service.e_posts(e2).get(post_key, 0)
                if not lvl2 or (e2.end_date and e2.end_date < d) or (e2.start_date and e2.start_date > d):
                    continue
                day2 = service.employee_shifts(company_code, e2.id, d, d)
                if any(x.kind == "absence" for x in day2) or (d.weekday() == 6 and e2.sunday == "non"):
                    continue
                wk2 = _week_shifts(company_code, e2.id, d)
                wh2 = sum(x.hours for x in wk2)
                working2 = [x for x in day2 if x.kind == "work"]
                sc = service.level_weight(lvl2) * 4 + e2.flexibility * 5 - e2.priority * 2 + (12 if e2.site == site else (4 if site in service.e_list(e2, "mobility") else -6))
                base2 = {"employee_id": e2.id, "name": service.full_name(e2), "site": e2.site, "phone": e2.phone, "telegram": e2.telegram, "level": lvl2}
                if not working2:
                    if d.weekday() in service.e_list(e2, "cfa_days"):
                        continue
                    rb2 = _rest_before(company_code, e2.id, d, sh.start)
                    if rb2 is not None and rb2 < R["rest_min_hours"]:
                        continue
                    if wh2 + sh.hours <= R["week_max_hours"] and len({x.date for x in wk2}) < 6:
                        res.append({**base2, "kind": "call", "post": post_key, "score": sc - (6 if wh2 + sh.hours > e2.weekly_hours else 0),
                                    "text": f"{service.full_name(e2)} ({config.SITES.get(e2.site, e2.site) if e2.site != site else 'établissement'}) prend {P(post_key)} : appel, {'heures sup' if wh2 + sh.hours > e2.weekly_hours else 'dans son contrat'}"})
                    for x in wk2:
                        if x.site == site and x.date != d and x.date >= date.today() and have(x.date, x.post_key) - 1 >= service.coverage_for(cfg, x.date, site).get(x.post_key, 0):
                            res.append({**base2, "kind": "swap", "post": post_key, "release_shift_id": x.id, "score": sc - 5,
                                        "text": f"{service.full_name(e2)} prend {P(post_key)} et libère {P(x.post_key)} du {config.DAYS_SHORT[x.date.weekday()]} {x.date:%d/%m} (jour en surplus)"})
                            break
                else:
                    for x in working2:
                        if x.site != site or x.post_key == post_key or x.post_key in chain_posts:
                            continue
                        if have(x.date, x.post_key) - 1 >= service.coverage_for(cfg, x.date, site).get(x.post_key, 0):
                            res.append({**base2, "kind": "move", "post": post_key, "shift_id": x.id, "score": sc + 10,
                                        "text": f"{service.full_name(e2)} bascule de {P(x.post_key)} vers {P(post_key)} (son poste garde assez de monde)"})
            res.sort(key=lambda o: -o["score"])
            return res

        chains = []
        for e in pool:
            lvl = service.e_posts(e).get(pk, 0)
            if not lvl or e.site != site:
                continue
            day = service.employee_shifts(company_code, e.id, d, d)
            for x in [y for y in day if y.kind == "work" and y.site == site and y.post_key != pk]:
                need_o = service.coverage_for(cfg, d, site).get(x.post_key, 0)
                if have(x.date, x.post_key) - 1 >= need_o:
                    continue                                  # déjà couvert par une option « move » simple
                first = {"employee_id": e.id, "name": service.full_name(e), "site": e.site, "phone": e.phone, "telegram": e.telegram, "level": lvl,
                         "kind": "move", "post": pk, "shift_id": x.id, "text": f"{service.full_name(e)} bascule de {P(x.post_key)} vers {P(pk)} ({sh.start}-{sh.end})"}
                for cov in cover_options(x.post_key, {e.id, employee_id}, 2, chain_posts={pk, x.post_key})[:2]:
                    steps = [first] + cov
                    score = service.level_weight(lvl) * 4 + e.flexibility * 5 - e.priority * 2 + 12 + min(c["score"] for c in cov) / 3 - 25 * (len(steps) - 1)
                    names = [st["name"] for st in steps]
                    chains.append({"kind": "chain", "id": "chain:" + ":".join(str(st["employee_id"]) for st in steps), "employee_id": e.id, "name": " + ".join(names), "site": site,
                                   "phone": None, "telegram": None, "level": lvl, "week_hours": None, "contract": None, "status": "à contacter", "score": score,
                                   "steps": steps, "why": [f"{service.full_name(e)} est déjà sur place et connaît {P(pk)} (niveau {lvl})", f"son poste {P(x.post_key)} passerait sous le besoin : il est recouvert par {' puis '.join(st['name'] for st in cov)}"],
                                   "cons": [f"{len(steps)} personnes à faire valider : {', '.join(names)}"] + [c["text"] for c in cov],
                                   "action": "à valider par chaque personne concernée avant d'appliquer"})
        options += chains
        options.sort(key=lambda o: -o["score"])
        for i, o in enumerate(options[:26]):
            o["letter"] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i]
            if "steps" not in o:
                o["steps"] = [{"employee_id": o["employee_id"], "name": o["name"], "kind": o["kind"], "post": pk, "shift_id": o.get("shift_id"), "release_shift_id": o.get("release_shift_id"), "text": o.get("action", "")}]
            o["participants"] = [{"employee_id": st["employee_id"], "name": st["name"], "phone": st.get("phone"), "status": "à confirmer"} for st in o["steps"]]
        per_shift.append({"shift_id": sh.id, "date": d.isoformat(), "post": pk, "start": sh.start, "end": sh.end, "hours": sh.hours,
                          "need": need_day.get(pk, 0), "options": options[:26], "resolution": None})
    return {"employee": service.full_name(absent) if absent else "?", "employee_id": employee_id, "site": site, "shifts": per_shift}


def record_answer(inc: PlIncident, shift_id: int, option_id: str, outcome: str, by: str = None, employee_id: int = None) -> PlIncident:
    """outcome : confirm / refuse (par participant, `employee_id`), oui (tous confirmés → appliquer ; option à une seule personne : confirme et applique),
    non / pas_de_reponse (option entière), unassign (laisser non couverte)."""
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
            parts = opt.setdefault("participants", [{"employee_id": opt["employee_id"], "name": opt["name"], "status": "à confirmer"}])
            if outcome in ("confirm", "refuse"):
                for pt in parts:
                    if pt["employee_id"] == employee_id:
                        pt["status"] = "confirmé" if outcome == "confirm" else "refusé"
                        log.append({"shift_id": shift_id, "event": f"option {opt.get('letter')} · {pt['name']} : {pt['status']}", "by": by})
                if any(pt["status"] == "refusé" for pt in parts):
                    opt["status"] = "refusée"
                elif all(pt["status"] == "confirmé" for pt in parts):
                    opt["status"] = "tous confirmés"
            elif outcome == "oui":
                if len(parts) == 1:
                    parts[0]["status"] = "confirmé"
                if all(pt["status"] == "confirmé" for pt in parts):
                    opt["status"] = "retenue"
                    log.append({"shift_id": shift_id, "event": f"option {opt.get('letter')} retenue et appliquée ({opt['name']})", "by": by})
                    entry["resolution"] = {"kind": opt["kind"], "employee_id": opt["employee_id"], "name": opt["name"], "letter": opt.get("letter")}
                    _apply(inc, entry, opt)
                else:
                    log.append({"shift_id": shift_id, "event": f"option {opt.get('letter')} : en attente de {', '.join(pt['name'] for pt in parts if pt['status'] != 'confirmé')}", "by": by})
            else:
                opt["status"] = {"non": "pas disponible", "pas_de_reponse": "pas de réponse"}.get(outcome, outcome)
                log.append({"shift_id": shift_id, "event": f"option {opt.get('letter')} · {opt['name']} : {opt['status']}", "by": by})
    inc.plan = json.dumps(plan, ensure_ascii=False)
    if all(x.get("resolution") for x in plan["shifts"]):
        inc.status = "resolved"
    return service.save_incident(inc)


def _absence_for(inc: PlIncident, sh: PlShift) -> PlShift:
    return PlShift(company_code=inc.company_code, site=inc.site, employee_id=inc.employee_id, date=sh.date, kind="absence", post_key=inc.reason,
                   hours=sh.hours, status=sh.status, source="replacement", note=f"incident #{inc.id}")


def _apply(inc: PlIncident, entry: dict, opt: dict) -> None:
    if opt["kind"] == "chain":
        _apply_chain(inc, entry, opt)
        return
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


def _apply_chain(inc: PlIncident, entry: dict, opt: dict) -> None:
    """Étape 1 : X bascule sur la plage de l'absent (sa plage d'origine devient la plage à couvrir) ;
    étapes suivantes : appel / échange / déplacement pour couvrir la plage libérée à l'étape précédente."""
    with Session(engine) as s:
        sh = s.get(PlShift, entry["shift_id"])
        if not sh:
            return
        s.add(_absence_for(inc, sh))
        target = sh
        for st in opt["steps"]:
            if st["kind"] == "move":
                mover = s.get(PlShift, st["shift_id"])
                if not mover:
                    continue
                # la plage d'origine du déplacé devient la nouvelle plage à couvrir : on échange les contenus
                vacated = PlShift(company_code=inc.company_code, site=inc.site, employee_id=None, date=mover.date, kind="work", post_key=mover.post_key,
                                  start=mover.start, end=mover.end, pause=mover.pause, hours=mover.hours, status=target.status, source="replacement", note=f"libérée (incident #{inc.id})")
                mover.post_key, mover.start, mover.end, mover.pause, mover.hours = target.post_key, target.start, target.end, target.pause, target.hours
                mover.note, mover.source = f"basculé (incident #{inc.id})", "replacement"
                s.add(mover)
                if target.id == sh.id:
                    s.delete(target)
                else:
                    s.delete(target)
                s.add(vacated)
                s.flush()
                target = vacated
            elif st["kind"] in ("call", "swap"):
                if st["kind"] == "swap" and st.get("release_shift_id"):
                    rel = s.get(PlShift, st["release_shift_id"])
                    if rel:
                        s.delete(rel)
                target.employee_id = st["employee_id"]
                target.source = "replacement"
                target.note = f"remplacement en chaîne (incident #{inc.id})"
                s.add(target)
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
