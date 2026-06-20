import os
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.core.db import engine
from app.core.config import APP_VERSION, APP_COMMIT
from app.core.security import authenticate, current_user, user_companies, role_for
from app.core import registry
from app.core.connectors import pennylane
from app.core.jobs import start_job, demo_job
from app.models import Company, Run
from app.packs.sterna_caisse import config as caisse_config
from app.packs.sterna_caisse.jobs import run_cadrage

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _ctx(request: Request, **extra):
    # NB: la signature moderne de TemplateResponse injecte `request` elle-même.
    base = {
        "app_name": "Vaelan", "user": current_user(request),
        "version": APP_VERSION, "commit": APP_COMMIT,
    }
    base.update(extra)
    return base


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return RedirectResponse("/companies" if current_user(request) else "/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if current_user(request):
        return RedirectResponse("/companies", status_code=303)
    return templates.TemplateResponse(request, "login.html", _ctx(request, error=error))


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    u = authenticate(email, password)
    if not u:
        return RedirectResponse("/login?error=Identifiants+invalides", status_code=303)
    request.session["user_id"] = u.id
    return RedirectResponse("/companies", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/companies", response_class=HTMLResponse)
def companies(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "companies.html", _ctx(request, companies=user_companies(user)))


@router.get("/c/{code}", response_class=HTMLResponse)
def dashboard(request: Request, code: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == code)).first()
    if not company or role_for(user, company) is None:
        return templates.TemplateResponse(request, "forbidden.html", _ctx(request), status_code=403)

    packs = registry.packs_for(company.code)
    ctx = {"company": company, "session": None}
    cards = [{"pack": p, "tiles": p.tiles(ctx)} for p in packs]
    pl = pennylane.for_company(company.code)
    health = pl.health() if pl else {"ok": False, "error": "clé API non configurée"}
    return templates.TemplateResponse(
        request, "dashboard.html",
        _ctx(request, company=company, cards=cards, role=role_for(user, company), health=health),
    )


# ----------------------------- Import / cadrage -----------------------------
def _company_or_redirect(request: Request, code: str):
    user = current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == code)).first()
    if not company or role_for(user, company) is None:
        return None, templates.TemplateResponse(request, "forbidden.html", _ctx(request), status_code=403)
    return company, None


@router.get("/c/{code}/import", response_class=HTMLResponse)
def import_form(request: Request, code: str):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    establishments = list(caisse_config.ESTABLISHMENTS.keys())
    return templates.TemplateResponse(request, "import.html",
                                      _ctx(request, company=company, establishments=establishments))


@router.post("/c/{code}/import")
def import_run(request: Request, code: str,
               establishment: str = Form(...), date_from: str = Form(...),
               date_to: str = Form(...), synthese: UploadFile = File(...)):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    data = synthese.file.read()
    short = establishment.replace("OCOPAIN ", "")
    label = f"Cadrage caisse · {short} · {date_from}→{date_to}"
    start_job("cadrage", lambda ctx: run_cadrage(ctx, establishment, date_from, date_to, data),
              company_id=company.id, pack="sterna.caisse", label=label)
    return RedirectResponse("/jobs", status_code=303)


# ----------------------------- Jobs (tâches) -----------------------------
@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "jobs.html", _ctx(request))


@router.get("/jobs/feed", response_class=HTMLResponse)
def jobs_feed(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    with Session(engine) as s:
        running = s.exec(select(Run).where(Run.status == "running").order_by(Run.id.desc())).all()
        recent = s.exec(select(Run).where(Run.status != "running").order_by(Run.id.desc()).limit(30)).all()
        cmap = {c.id: c.name for c in s.exec(select(Company)).all()}
    return templates.TemplateResponse(request, "_jobs_feed.html",
                                      _ctx(request, running=running, recent=recent, cmap=cmap))


@router.post("/jobs/demo")
def jobs_demo(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    start_job("demo", demo_job, label="Tâche de démonstration")
    return RedirectResponse("/jobs", status_code=303)
