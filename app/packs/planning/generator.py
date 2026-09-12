"""Génération automatique d'un planning : besoins de couverture × salariés disponibles, sous les règles.
Heuristique déterministe (glouton par rareté + score), explicable ligne à ligne."""
import json
from datetime import date, timedelta
from typing import List, Dict, Optional

from sqlmodel import Session, select

from app.core.db import engine
from app.models import PlEmployee, PlShift
from . import service, config


def _ctx_employee(e: PlEmployee) -> dict:
    return {"e": e, "posts": service.e_posts(e), "days_off": set(service.e_list(e, "days_off")), "cfa": set(service.e_list(e, "cfa_days")),
            "pattern": {int(k): v for k, v in json.loads(e.pattern or "{}").items()}}


def generate(company_code: str, site: str, d_from: date, d_to: date, replace_auto: bool = True, keep_manual: bool = True) -> dict:
    cfg = service.get_config(company_code)
    R = cfg["rules"]
    pmap = service.post_map(company_code, site)
    emps = [_ctx_employee(e) for e in service.employees(company_code, site, on=d_from)]
    # contexte : plages existantes (absences, plages manuelles/publiées gardées) depuis 8 jours avant
    with Session(engine) as s:
        if replace_auto:
            for x in s.exec(select(PlShift).where(PlShift.company_code == company_code, PlShift.site == site, PlShift.source == "auto",
                                                   PlShift.status == "draft", PlShift.date >= d_from, PlShift.date <= d_to)).all():
                s.delete(x)
            s.commit()
    existing = service.shifts(company_code, site, d_from - timedelta(days=8), d_to)
    ids = [c["e"].id for c in emps]
    # plages d'autres sites (mobilité) pour la même période
    others = []
    with Session(engine) as s:
        if ids:
            others = list(s.exec(select(PlShift).where(PlShift.company_code == company_code, PlShift.site != site,
                                                        PlShift.employee_id.in_(ids), PlShift.date >= d_from - timedelta(days=8), PlShift.date <= d_to)).all())
    allsh = existing + others
    by_emp_day: Dict[tuple, List[PlShift]] = {}
    for x in allsh:
        if x.employee_id:
            by_emp_day.setdefault((x.employee_id, x.date), []).append(x)

    def works(eid, d):
        return [x for x in by_emp_day.get((eid, d), []) if x.kind == "work"]

    def absent(eid, d):
        return any(x.kind == "absence" for x in by_emp_day.get((eid, d), []))

    def week_hours(eid, d):
        m = service.week_monday(d)
        return sum(x.hours for dd in range(7) for x in works(eid, m + timedelta(days=dd)))

    def week_days(eid, d):
        m = service.week_monday(d)
        return sum(1 for dd in range(7) if works(eid, m + timedelta(days=dd)))

    def consecutive(eid, d):
        n, dd = 0, d - timedelta(days=1)
        while works(eid, dd):
            n += 1
            dd -= timedelta(days=1)
        return n

    def sundays_recent(eid, d):
        return sum(1 for k in (7, 14) if works(eid, d - timedelta(days=k)))

    def rest_ok(eid, d, start_h):
        prev = works(eid, d - timedelta(days=1))
        if not prev:
            return True
        end_prev = max(service.hm_to_h(x.end) + (24 if service.hm_to_h(x.end) < service.hm_to_h(x.start) else 0) for x in prev)
        return (start_h + 24 - end_prev) >= R["rest_min_hours"]

    created, unassigned, log = [], [], []
    d = d_from
    while d <= d_to:
        need = service.coverage_for(cfg, d, site)
        # déjà couvert par des plages existantes (manuelles / importées / publiées)
        have = {}
        for x in existing:
            if x.date == d and x.kind == "work" and x.employee_id and x.post_key:
                have[x.post_key] = have.get(x.post_key, 0) + 1
        slots = []
        for pk, n in need.items():
            for _ in range(max(0, n - have.get(pk, 0))):
                slots.append(pk)
        # rareté : postes avec le moins de candidats compétents d'abord
        def eligible_count(pk):
            return sum(1 for c in emps if c["posts"].get(pk))
        slots.sort(key=lambda pk: (eligible_count(pk), -need.get(pk, 0)))
        for pk in slots:
            p = pmap.get(pk)
            if not p:
                continue
            st_h, dur = service.hm_to_h(p.start), service.shift_hours(p.start, p.end, p.pause)
            best, best_score, why = None, None, {}
            for c in emps:
                e = c["e"]
                lvl = c["posts"].get(pk, 0)
                if not lvl or absent(e.id, d) or works(e.id, d) or d.weekday() in c["days_off"] or d.weekday() in c["cfa"]:
                    continue
                if d.weekday() == 6 and e.sunday == "non":
                    continue
                if e.end_date and e.end_date < d or e.start_date and e.start_date > d:
                    continue
                if not rest_ok(e.id, d, st_h) or consecutive(e.id, d) + 1 > R["consecutive_max_days"] or week_days(e.id, d) + 1 > e.max_days:
                    continue
                wh = week_hours(e.id, d)
                if wh + dur > R["week_max_hours"]:
                    continue
                score = {"niveau": service.level_weight(lvl) * 4}
                remaining = e.weekly_hours - wh
                score["heures"] = min(remaining, dur) * 3 if remaining > 0 else -(dur - remaining) * 6
                if wh + dur > e.weekly_hours + R.get("overtime_tolerance", 2):
                    score["dépassement"] = -25
                score["habitude"] = c["pattern"].get(d.weekday(), 50) / 10
                if d.weekday() == 6:
                    score["dimanche"] = -18 * sundays_recent(e.id, d)
                if c["posts"] and min(c["posts"].values()) == lvl:
                    score["poste_principal"] = 6
                total = sum(score.values())
                if best_score is None or total > best_score:
                    best, best_score, why = e, total, score
            if best:
                sh = PlShift(company_code=company_code, site=site, employee_id=best.id, date=d, kind="work", post_key=pk, start=p.start, end=p.end,
                             pause=p.pause, hours=dur, status="draft", source="auto", note=None)
                created.append(sh)
                existing.append(sh)
                by_emp_day.setdefault((best.id, d), []).append(sh)
                log.append({"date": d.isoformat(), "post": pk, "employee_id": best.id, "score": round(best_score, 1), "why": {k: round(v, 1) for k, v in why.items()}})
            else:
                sh = PlShift(company_code=company_code, site=site, employee_id=None, date=d, kind="work", post_key=pk, start=p.start, end=p.end,
                             pause=p.pause, hours=dur, status="draft", source="auto", note="aucun salarié disponible")
                created.append(sh)
                unassigned.append({"date": d.isoformat(), "post": pk})
        d += timedelta(days=1)
        # ---- fin de semaine (ou de période) : compléter les contrats ----
        if d.weekday() == 0 or d > d_to:
            monday = service.week_monday(d - timedelta(days=1))
            wk_end = min(monday + timedelta(days=6), d_to)
            if wk_end < monday:
                continue
            for c in sorted(emps, key=lambda c: -(c["e"].weekly_hours - week_hours(c["e"].id, monday))):
                e = c["e"]
                if not c["posts"]:
                    continue
                guard = 0
                while guard < 7:
                    guard += 1
                    deficit = e.weekly_hours - week_hours(e.id, monday)
                    if deficit < 4 or week_days(e.id, monday) >= e.max_days:
                        break
                    best = None
                    for dd in range(7):
                        day = monday + timedelta(days=dd)
                        if day < d_from or day > wk_end:
                            continue
                        if absent(e.id, day) or works(e.id, day) or dd in c["days_off"] or dd in c["cfa"]:
                            continue
                        if dd == 6 and (e.sunday == "non" or sundays_recent(e.id, day) >= 1):
                            continue
                        if e.end_date and e.end_date < day or e.start_date and e.start_date > day:
                            continue
                        if consecutive(e.id, day) + 1 > R["consecutive_max_days"]:
                            continue
                        nxt = works(e.id, day + timedelta(days=1))
                        need = service.coverage_for(cfg, day, site)
                        if service.is_closed(cfg, day):
                            continue
                        for pk, lvl in sorted(c["posts"].items(), key=lambda kv: kv[1]):
                            p = pmap.get(pk)
                            if not p or not p.active:
                                continue
                            st_h, dur = service.hm_to_h(p.start), service.shift_hours(p.start, p.end, p.pause)
                            if not rest_ok(e.id, day, st_h):
                                continue
                            if nxt and (service.hm_to_h(nxt[0].start) + 24 - (service.hm_to_h(p.end) + (24 if service.hm_to_h(p.end) < st_h else 0))) < R["rest_min_hours"]:
                                continue
                            if week_hours(e.id, monday) + dur > e.weekly_hours + R.get("overtime_tolerance", 2):
                                continue
                            have = sum(1 for x in existing if x.date == day and x.kind == "work" and x.employee_id and x.post_key == pk)
                            ratio = have / max(1, need.get(pk, 1))
                            score = service.level_weight(lvl) * 3 - ratio * 8 + c["pattern"].get(dd, 50) / 10 + (6 if need.get(pk) else 0)
                            if best is None or score > best[0]:
                                best = (score, day, p, dur, lvl)
                    if not best:
                        break
                    score, day, p, dur, lvl = best
                    sh = PlShift(company_code=company_code, site=site, employee_id=e.id, date=day, kind="work", post_key=p.key, start=p.start, end=p.end,
                                 pause=p.pause, hours=dur, status="draft", source="auto", note=None)
                    created.append(sh)
                    existing.append(sh)
                    by_emp_day.setdefault((e.id, day), []).append(sh)
                    log.append({"date": day.isoformat(), "post": p.key, "employee_id": e.id, "score": round(score, 1), "why": {"complément contrat": round(deficit, 1)}})
    with Session(engine) as s:
        for sh in created:
            s.add(sh)
        s.commit()
    return {"created": len(created), "assigned": len(created) - len(unassigned), "unassigned": unassigned, "log": log}
