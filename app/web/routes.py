import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.core.db import engine
from app.core.config import APP_VERSION, APP_COMMIT
from app.core.security import authenticate, current_user, user_companies, role_for
from app.core import registry
from app.core.connectors import pennylane
from app.models import Company

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _ctx(request: Request, **extra):
    base = {
        "request": request, "app_name": "Vaelan", "user": current_user(request),
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
    return templates.TemplateResponse("login.html", _ctx(request, error=error))


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
    return templates.TemplateResponse("companies.html", _ctx(request, companies=user_companies(user)))


@router.get("/c/{code}", response_class=HTMLResponse)
def dashboard(request: Request, code: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == code)).first()
    if not company or role_for(user, company) is None:
        return templates.TemplateResponse("forbidden.html", _ctx(request), status_code=403)

    packs = registry.packs_for(company.code)
    ctx = {"company": company, "session": None}
    cards = [{"pack": p, "tiles": p.tiles(ctx)} for p in packs]
    pl = pennylane.for_company(company.code)
    health = pl.health() if pl else {"ok": False, "error": "clé API non configurée"}
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(request, company=company, cards=cards, role=role_for(user, company), health=health),
    )
