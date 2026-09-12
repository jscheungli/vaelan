"""Planning des équipes : configuration (questionnaire), grille hebdomadaire, salariés, remplacements guidés."""
import json
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select

from app.core.db import engine
from app.core.security import current_user
from app.models import PlEmployee, PlPost, PlShift, PlIncident
from app.packs.planning import config as pcfg, service, generator, replace, jobs as pjobs
from app.core.jobs import start_job
from app.models import Run
from app.web.routes import templates, _ctx, _company_or_redirect

router = APIRouter()


def _guard(request: Request, code: str):
    return _company_or_redirect(request, code, feature="planning")


def _site(request: Request, cfg: dict) -> str:
    s = request.query_params.get("site") or request.session.get("planning_site") or cfg.get("site") or "SL"
    if s not in pcfg.SITES:
        s = "SL"
    request.session["planning_site"] = s
    return s


def _who(request: Request) -> str:
    u = current_user(request)
    return (u.name or u.email) if u else "?"


def _d(s: str, default: date = None) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return default or date.today()


# ============================== accueil ==============================
@router.get("/c/{code}/planning", response_class=HTMLResponse)
def planning_home(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    service.ensure_posts(code, site)
    if not cfg.get("validated_at"):
        return RedirectResponse(f"/c/{code}/planning/config", status_code=303)
    return RedirectResponse(f"/c/{code}/planning/semaine/{service.week_monday(date.today()).isoformat()}?site={site}", status_code=303)


# ============================== configuration (questionnaire) ==============================
def _wizard_ctx(request, company, cfg, step):
    site = _site(request, cfg)
    from datetime import date as _date
    hol = {y: pcfg.holiday_dates(y) for y in (_date.today().year, _date.today().year + 1)}
    return _ctx(request, company=company, cfg=cfg, step=step, steps=pcfg.WIZARD_STEPS, sites=pcfg.SITES, site=site, holiday_types=pcfg.HOLIDAY_TYPES,
                holiday_dates=hol, level_max=pcfg.LEVEL_MAX, coverage=service.site_coverage(cfg, site),
                days=pcfg.DAYS, days_short=pcfg.DAYS_SHORT, posts=service.posts(code_or(company), site), seasons=pcfg.SEASONS,
                employees=service.employees(code_or(company), site, active_only=False), e_posts=service.e_posts, e_list=service.e_list,
                full_name=service.full_name, post_keys=[p.key for p in service.posts(code_or(company), site)], json=json)


def code_or(company):
    return company.code


@router.get("/c/{code}/planning/config", response_class=HTMLResponse)
def planning_config(request: Request, code: str, step: int = 0, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    service.ensure_posts(code, _site(request, cfg))
    step = step or int(cfg.get("wizard_step") or 1)
    step = max(1, min(step, len(pcfg.WIZARD_STEPS)))
    ctx = _wizard_ctx(request, company, cfg, step)
    ctx["msg"] = msg
    return templates.TemplateResponse(request, "planning_config.html", ctx)


@router.post("/c/{code}/planning/config/{step}")
async def planning_config_save(request: Request, code: str, step: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    form = await request.form()
    g = lambda k, d=None: (form.get(k) if form.get(k) not in (None, "") else d)
    f = lambda k, d=0.0: float(str(g(k, d)).replace(",", "."))
    upd = {}
    if step == 1:
        for p in service.posts(code, site):
            service.save_post(p.id, code, site, label=g(f"label_{p.id}", p.label), start=g(f"start_{p.id}", p.start), end=g(f"end_{p.id}", p.end),
                              pause=f(f"pause_{p.id}", p.pause), color=g(f"color_{p.id}", p.color), active=bool(g(f"active_{p.id}")))
        if g("new_label"):
            service.save_post(None, code, site, label=g("new_label"), department=g("new_department", "autre"), start=g("new_start", "08:00"),
                              end=g("new_end", "16:00"), pause=f("new_pause", 0.5), color=g("new_color", "#cccccc"), active=True, sort=90)
    elif step == 2:
        cov = {}
        for season in pcfg.SEASONS:
            cov[season] = {}
            for p in service.posts(code, site):
                vals = [int(g(f"cov_{season}_{p.key}_{wd}", 0) or 0) for wd in range(7)]
                if any(vals) or season == "default":
                    cov[season][p.key] = vals
        if site == cfg.get("site"):
            upd["coverage"] = cov
        else:
            cbs = dict(cfg.get("coverage_by_site") or {})
            cbs[site] = cov
            upd["coverage_by_site"] = cbs
    elif step == 3:
        upd["rules"] = {"day_max_hours": f("day_max_hours", 10), "week_max_hours": f("week_max_hours", 48), "avg12_max_hours": f("avg12_max_hours", 44),
                        "rest_min_hours": f("rest_min_hours", 11), "consecutive_max_days": int(f("consecutive_max_days", 6)), "overtime_tolerance": f("overtime_tolerance", 3)}
        upd["sunday"] = {"max_share": f("sunday_max_share", 0.5), "consecutive_max": int(f("sunday_consecutive_max", 2)), "premium_pct": f("sunday_premium", 20)}
        upd["night"] = {"start": g("night_start", "20:00"), "end": g("night_end", "06:00"), "premium_pct": f("night_premium", 25)}
        upd["holidays"] = {"types": [k for k, _, _, _ in pcfg.HOLIDAY_TYPES if g(f"hol_{k}")], "closed_types": [k for k, _, _, _ in pcfg.HOLIDAY_TYPES if g(f"holclosed_{k}")],
                           "premium_pct": f("holiday_premium", 100), "compensation": bool(g("holiday_compensation")), "confirm_days": int(f("holiday_confirm_days", 21))}
        upd["supervision"] = {"manager_posts": [p.key for p in service.posts(code, site) if g(f"mgr_{p.key}")], "manager_required": bool(g("manager_required"))}
        upd["publication"] = {"horizon_weeks": int(f("horizon_weeks", 2))}
    elif step == 4:
        for e in service.employees(code, site, active_only=False):
            _save_employee_form(code, site, e, form)
    elif step == 5:
        upd["validated_at"] = datetime.utcnow().isoformat()
        service.save_config(upd, code)
        return RedirectResponse(f"/c/{code}/planning/semaine/{service.week_monday(date.today()).isoformat()}?site={site}&msg=Configuration validée : mode automatique actif.", status_code=303)
    nxt = min(step + 1, len(pcfg.WIZARD_STEPS))
    upd["wizard_step"] = max(int(cfg.get("wizard_step") or 1), nxt)
    service.save_config(upd, code)
    if form.get("action") == "stay":
        return RedirectResponse(f"/c/{code}/planning/config?step={step}&site={site}&msg=Enregistré.", status_code=303)
    return RedirectResponse(f"/c/{code}/planning/config?step={nxt}&site={site}", status_code=303)


def _save_employee_form(code: str, site: str, e, form, prefix: str = None) -> None:
    """Champs d'une fiche salarié (questionnaire : préfixe `<id>_` ; fiche individuelle : sans préfixe)."""
    pre = f"{e.id}_" if prefix is None else prefix
    g = lambda k, d=None: (form.get(pre + k) if form.get(pre + k) not in (None, "") else d)
    posts = {}
    for p in service.posts(code, site):
        try:
            lvl = int(g(f"lvl_{p.key}", 0) or 0)
        except ValueError:
            lvl = 0
        if 1 <= lvl <= pcfg.LEVEL_MAX:
            posts[p.key] = lvl
    fields = dict(posts=json.dumps(posts), sunday=g("sunday", "oui"), max_days=int(g("max_days", e.max_days)),
                  days_off=json.dumps([wd for wd in range(7) if g(f"off_{wd}")]), cfa_days=json.dumps([wd for wd in range(7) if g(f"cfa_{wd}")]),
                  mobility=json.dumps([st for st in pcfg.SITES if st != e.site and g(f"mob_{st}")]),
                  flexibility=int(g("flexibility", e.flexibility)), priority=int(g("priority", e.priority)), phone=g("phone"), telegram=g("telegram"),
                  active=bool(g("active")), weekly_hours=float(str(g("weekly_hours", e.weekly_hours)).replace(",", ".")))
    if g("first_name"):
        fields.update(first_name=g("first_name"), last_name=g("last_name", e.last_name), contract_type=g("contract_type", e.contract_type), note=g("note"),
                      start_date=_d(g("start_date")) if g("start_date") else None, end_date=_d(g("end_date")) if g("end_date") else None, site=g("site", e.site))
    service.save_employee(e.id, code, **fields)


@router.post("/c/{code}/planning/config/reopen")
def planning_config_reopen(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    service.save_config({"validated_at": None, "wizard_step": 1}, code)
    return RedirectResponse(f"/c/{code}/planning/config?step=1", status_code=303)


# ============================== grille hebdomadaire ==============================
@router.get("/c/{code}/planning/semaine/{monday}", response_class=HTMLResponse)
def planning_week(request: Request, code: str, monday: str, view: str = "emp", msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    m = service.week_monday(_d(monday))
    service.ensure_posts(code, site)
    wd = service.week_data(code, site, m)
    n_inc = len(service.incidents(code, site))
    return templates.TemplateResponse(request, "planning_week.html",
                                      _ctx(request, company=company, cfg=cfg, site=site, sites=pcfg.SITES, monday=m, prev=(m - timedelta(days=7)).isoformat(),
                                           next=(m + timedelta(days=7)).isoformat(), today=service.week_monday(date.today()).isoformat(), view=view, wd=wd,
                                           days_short=pcfg.DAYS_SHORT, msg=msg, n_inc=n_inc, absence_types=pcfg.ABSENCE_TYPES, full_name=service.full_name))


@router.get("/c/{code}/planning/semaine/{monday}/pdf")
def planning_week_pdf(request: Request, code: str, monday: str, view: str = "emp"):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    m = service.week_monday(_d(monday))
    from fastapi.responses import Response
    pdf = service.week_pdf(code, site, m, view=view)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="planning_{site}_semaine_{m.isoformat()}_{view}.pdf"'})


def _shift_json(x: PlShift) -> dict:
    return {"id": x.id, "employee_id": x.employee_id, "date": x.date.isoformat(), "kind": x.kind, "post_key": x.post_key, "start": x.start, "end": x.end,
            "pause": x.pause, "hours": x.hours, "note": x.note, "status": x.status, "source": x.source}


@router.get("/c/{code}/planning/api/week/{monday}")
def api_week(request: Request, code: str, monday: str):
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    cfg = service.get_config(code)
    site = _site(request, cfg)
    m = service.week_monday(_d(monday))
    wd = service.week_data(code, site, m)
    emps = [{"id": e.id, "name": service.full_name(e), "first": e.first_name, "last": e.last_name, "contract": e.weekly_hours, "type": e.contract_type,
             "posts": service.e_posts(e), "days_off": service.e_list(e, "days_off"), "cfa": service.e_list(e, "cfa_days"), "sunday": e.sunday,
             "site": e.site, "primary": service.primary_post(e), "max_days": e.max_days} for e in wd["employees"]]
    posts = [{"key": p.key, "label": p.label, "color": p.color, "start": p.start, "end": p.end, "pause": p.pause, "active": p.active, "department": p.department, "sort": p.sort}
             for p in sorted(wd["posts"].values(), key=lambda p: (p.sort, p.label))]
    return {"monday": m.isoformat(), "days": [d.isoformat() for d in wd["days"]], "site": site, "shifts": [_shift_json(x) for x in wd["shifts"]],
            "employees": emps, "posts": posts, "need": wd["need"], "have": wd["have"], "totals": {str(k): v for k, v in wd["totals"].items()},
            "alerts": wd["alerts"], "published": wd["published"], "absence_types": pcfg.ABSENCE_TYPES, "rules": cfg["rules"]}


@router.post("/c/{code}/planning/api/shift")
async def api_shift_save(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    cfg = service.get_config(code)
    site = _site(request, cfg)
    b = await request.json()
    kind = b.get("kind", "work")
    fields = {"date": _d(b.get("date")), "employee_id": b.get("employee_id") or None, "kind": kind, "post_key": b.get("post_key"),
              "start": b.get("start") if kind != "absence" else None, "end": b.get("end") if kind != "absence" else None,
              "pause": float(b.get("pause") or 0), "note": b.get("note") or None, "source": "manual", "status": b.get("status") or "draft"}
    if kind == "absence":
        fields["hours"] = float(b.get("hours") or 7.0)
    sh = service.save_shift(b.get("id"), code, site, **fields)
    return _shift_json(sh)


@router.post("/c/{code}/planning/api/shift/{sid}/move")
async def api_shift_move(request: Request, code: str, sid: int):
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    cfg = service.get_config(code)
    site = _site(request, cfg)
    b = await request.json()
    with Session(engine) as s:
        sh = s.get(PlShift, sid)
    if not sh:
        return JSONResponse({"error": "introuvable"}, status_code=404)
    fields = {}
    if b.get("date"):
        fields["date"] = _d(b["date"])
    if "employee_id" in b:
        fields["employee_id"] = b["employee_id"] or None
    if b.get("post_key"):
        fields["post_key"] = b["post_key"]
    if b.get("copy"):
        new = service.save_shift(None, code, site, date=fields.get("date", sh.date), employee_id=fields.get("employee_id", sh.employee_id), kind=sh.kind,
                                 post_key=fields.get("post_key", sh.post_key), start=sh.start, end=sh.end, pause=sh.pause, hours=sh.hours, note=None, source="manual", status="draft")
        return _shift_json(new)
    fields["source"] = "manual"
    return _shift_json(service.save_shift(sid, code, site, **fields))


@router.delete("/c/{code}/planning/api/shift/{sid}")
def api_shift_delete(request: Request, code: str, sid: int):
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    service.delete_shift(sid)
    return {"ok": True}


@router.post("/c/{code}/planning/api/week/{monday}/copy")
async def api_week_copy(request: Request, code: str, monday: str):
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    cfg = service.get_config(code)
    site = _site(request, cfg)
    b = await request.json()
    dst = service.week_monday(_d(monday))
    src = service.week_monday(_d(b.get("from"), dst - timedelta(days=7)))
    n = service.copy_week(code, site, src, dst, replace=bool(b.get("replace", True)))
    return {"copied": n}


@router.post("/c/{code}/planning/api/generate")
async def api_generate(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    cfg = service.get_config(code)
    site = _site(request, cfg)
    b = await request.json()
    d_from, d_to = _d(b.get("from")), _d(b.get("to"))
    if d_to < d_from or (d_to - d_from).days > 62:
        return JSONResponse({"error": "période invalide (62 jours max)"}, status_code=400)
    res = generator.generate(code, site, d_from, d_to, replace_auto=True)
    return {"created": res["created"], "assigned": res["assigned"], "unassigned": res["unassigned"]}


@router.post("/c/{code}/planning/api/week/{monday}/publish")
async def api_publish(request: Request, code: str, monday: str):
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    cfg = service.get_config(code)
    site = _site(request, cfg)
    b = await request.json()
    n = service.publish_week(code, site, service.week_monday(_d(monday)), status="draft" if b.get("unpublish") else "published")
    return {"updated": n}


# ============================== contrôles et alertes ==============================
@router.get("/c/{code}/planning/controles", response_class=HTMLResponse)
def planning_controls(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    found = pjobs.collect(code, cfg)
    review = pjobs.weekly_review(code, cfg)
    state = pjobs._sent_state(code)
    with Session(engine) as s:
        runs = s.exec(select(Run).where(Run.kind == "planning_control").order_by(Run.id.desc()).limit(8)).all()
    return templates.TemplateResponse(request, "planning_controls.html",
                                      _ctx(request, company=company, cfg=cfg, alerts=cfg.get("alerts") or {}, catalog=pcfg.RULES_CATALOG, found=found,
                                           sites=pcfg.SITES, site=_site(request, cfg), state=state, runs=runs, msg=msg, n_total=sum(len(v) for v in found.values()),
                                           smtp=mailer_configured(), review=review, review_labels=pjobs.REVIEW_LABELS))


def mailer_configured() -> bool:
    from app.core import mailer
    return mailer.configured()


@router.post("/c/{code}/planning/controles")
async def planning_controls_save(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    form = await request.form()
    if form.get("action") == "run":
        u = current_user(request)
        rid = start_job("planning_control", lambda ctx: pjobs.run_control(ctx, code, "daily", force_email=bool(form.get("email"))), company_id=company.id, pack="planning",
                        label="Contrôle du planning (à la demande)", user=u)
        return RedirectResponse(f"/c/{code}/planning/controles?msg=Contrôle lancé (tâche #{rid}) : le résultat s'affiche dans Tâches et par email si de nouveaux écarts apparaissent.", status_code=303)
    rules = {k: bool(form.get(f"rule_{k}")) for k, _, _, _ in pcfg.RULES_CATALOG}
    service.save_config({"alerts": {"enabled": bool(form.get("enabled")), "emails": (form.get("emails") or "").strip(), "daily": bool(form.get("daily")),
                                    "weekly": bool(form.get("weekly")), "horizon_days": int(form.get("horizon_days") or 14), "rules": rules}}, code)
    return RedirectResponse(f"/c/{code}/planning/controles?msg=Réglages des contrôles enregistrés.", status_code=303)


# ============================== salariés ==============================
@router.get("/c/{code}/planning/salaries", response_class=HTMLResponse)
def planning_employees(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    emps = service.employees(code, None, active_only=False)
    emps.sort(key=lambda e: (e.site != site, e.site, not e.active, e.last_name.upper()))
    return templates.TemplateResponse(request, "planning_employees.html",
                                      _ctx(request, company=company, site=site, sites=pcfg.SITES, employees=emps, posts=service.post_map(code, site),
                                           e_posts=service.e_posts, e_list=service.e_list, full_name=service.full_name, days_short=pcfg.DAYS_SHORT, msg=msg,
                                           level_max=pcfg.LEVEL_MAX))


@router.post("/c/{code}/planning/salaries/{eid}")
async def planning_employee_save(request: Request, code: str, eid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    form = await request.form()
    e = service.get_employee(eid) if eid else None
    if not e:
        e = service.save_employee(None, code, site=form.get("site") or site, first_name=form.get("first_name") or "?", last_name=form.get("last_name") or "?")
    _save_employee_form(code, e.site, e, form, prefix="")
    return RedirectResponse(f"/c/{code}/planning/salaries?site={site}&msg=Fiche enregistrée.", status_code=303)


# ============================== remplacements guidés ==============================
@router.get("/c/{code}/planning/incidents", response_class=HTMLResponse)
def planning_incidents(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    incs = service.incidents(code, site, open_only=False)[:30]
    emps = service.employees(code, site)
    emap = {e.id: service.full_name(e) for e in service.employees(code, active_only=False)}
    return templates.TemplateResponse(request, "planning_incidents.html",
                                      _ctx(request, company=company, site=site, sites=pcfg.SITES, incidents=incs, employees=emps, emap=emap,
                                           absence_types=pcfg.ABSENCE_TYPES, today=date.today().isoformat(), msg=msg, json=json))


@router.post("/c/{code}/planning/incidents/new")
def planning_incident_new(request: Request, code: str, employee_id: int = Form(...), date_from: str = Form(...), date_to: str = Form(""), reason: str = Form("Arrêt maladie")):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    site = _site(request, cfg)
    d1 = _d(date_from)
    d2 = _d(date_to, d1) if date_to else d1
    inc = replace.open_incident(code, site, employee_id, d1, max(d1, d2), reason, by=_who(request))
    return RedirectResponse(f"/c/{code}/planning/incidents/{inc.id}", status_code=303)


@router.get("/c/{code}/planning/incidents/{iid}", response_class=HTMLResponse)
def planning_incident(request: Request, code: str, iid: int, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    inc = service.get_incident(iid)
    if not inc:
        return RedirectResponse(f"/c/{code}/planning/incidents", status_code=303)
    plan = json.loads(inc.plan or "{}")
    pmap = service.post_map(code, inc.site)
    return templates.TemplateResponse(request, "planning_incident.html",
                                      _ctx(request, company=company, site=inc.site, sites=pcfg.SITES, inc=inc, plan=plan, posts=pmap,
                                           days_short=pcfg.DAYS_SHORT, msg=msg, date=date))


@router.post("/c/{code}/planning/incidents/{iid}/answer")
def planning_incident_answer(request: Request, code: str, iid: int, shift_id: int = Form(...), option_id: str = Form(""), outcome: str = Form(...)):
    company, redir = _guard(request, code)
    if redir:
        return redir
    inc = service.get_incident(iid)
    if not inc:
        return RedirectResponse(f"/c/{code}/planning/incidents", status_code=303)
    replace.record_answer(inc, shift_id, option_id, outcome, by=_who(request))
    return RedirectResponse(f"/c/{code}/planning/incidents/{iid}", status_code=303)


@router.post("/c/{code}/planning/incidents/{iid}/close")
def planning_incident_close(request: Request, code: str, iid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    inc = service.get_incident(iid)
    if inc:
        inc.status = "closed"
        service.save_incident(inc)
    return RedirectResponse(f"/c/{code}/planning/incidents", status_code=303)
