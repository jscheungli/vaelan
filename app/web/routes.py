import os
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.core.db import engine
from app.core.config import APP_VERSION, APP_COMMIT
from app.core.security import authenticate, current_user, user_companies, role_for
from app.core import registry
from app.core.connectors import pennylane
from app.core.jobs import start_job, demo_job
from app.models import Company, Run, ClientAccount, ImportBatch, JobArtifact
from app.packs.sterna_caisse import config as caisse_config
from app.packs.sterna_caisse.jobs import run_cadrage, run_generate_toslt
from app.packs.sterna_caisse.clients_sync import sync_clients
from app.packs.sterna_caisse import suivi as caisse_suivi
from app.packs.sterna_caisse.verify import run_verify

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
    cards = [{"pack": p, "phases": p.workflow(ctx)} for p in packs]
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
    with Session(engine) as s:
        batches = s.exec(select(ImportBatch).where(ImportBatch.company_id == company.id)
                         .order_by(ImportBatch.id.desc()).limit(20)).all()
    return templates.TemplateResponse(request, "import.html",
                                      _ctx(request, company=company, establishments=establishments,
                                           batches=batches))


@router.post("/c/{code}/generate")
def generate_run(request: Request, code: str,
                 establishment: str = Form(...), date_from: str = Form(...),
                 date_to: str = Form(...), synthese: UploadFile = File(...)):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    data = synthese.file.read()
    fname = synthese.filename or "synthese.pdf"
    short = establishment.replace("OCOPAIN ", "")
    label = f"Cadrage + génération TOSLT · {short} · {date_from}→{date_to}"
    start_job("generate_toslt",
              lambda ctx: run_generate_toslt(ctx, company.code, establishment, date_from, date_to, data, fname),
              company_id=company.id, pack="sterna.caisse", label=label)
    return RedirectResponse("/jobs", status_code=303)


@router.get("/c/{code}/batch/{batch_id}/download")
def batch_download(request: Request, code: str, batch_id: int):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    from fastapi.responses import Response
    with Session(engine) as s:
        b = s.get(ImportBatch, batch_id)
        if not b or b.company_id != company.id:
            return RedirectResponse(f"/c/{code}/import", status_code=303)
        # priorité à l'artefact en base (durable) ; repli sur le fichier disque
        art = None
        if b.run_id:
            art = s.exec(select(JobArtifact).where(
                JobArtifact.run_id == b.run_id, JobArtifact.kind == "csv")).first()
    if art:
        return Response(content=art.data, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{art.name}"'})
    if b.csv_path and os.path.exists(b.csv_path):
        return FileResponse(b.csv_path, media_type="text/csv", filename=os.path.basename(b.csv_path))
    return RedirectResponse(f"/c/{code}/import", status_code=303)


@router.post("/c/{code}/import")
def import_run(request: Request, code: str,
               establishment: str = Form(...), date_from: str = Form(...),
               date_to: str = Form(...), synthese: UploadFile = File(...)):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    data = synthese.file.read()
    fname = synthese.filename or "synthese.pdf"
    short = establishment.replace("OCOPAIN ", "")
    label = f"Cadrage caisse · {short} · {date_from}→{date_to}"
    start_job("cadrage", lambda ctx: run_cadrage(ctx, establishment, date_from, date_to, data, fname),
              company_id=company.id, pack="sterna.caisse", label=label)
    return RedirectResponse("/jobs", status_code=303)


# ----------------------------- Suivi de clôture (tableau de bord) -----------------------------
@router.get("/c/{code}/suivi", response_class=HTMLResponse)
def suivi_page(request: Request, code: str, period: str = ""):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    if not period or len(period) != 7:
        from datetime import datetime, timedelta
        period = (datetime.utcnow() + timedelta(hours=4)).strftime("%Y-%m")
    board = caisse_suivi.build_board(company, period)
    return templates.TemplateResponse(request, "suivi.html",
                                      _ctx(request, company=company, board=board))


@router.post("/c/{code}/suivi/declare")
def suivi_declare(request: Request, code: str, period: str = Form(...),
                  establishment: str = Form(...), step: str = Form(...), undo: str = Form("")):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    caisse_suivi.declare(company.id, establishment, period, step, undo=bool(undo))
    return RedirectResponse(f"/c/{code}/suivi?period={period}", status_code=303)


@router.post("/c/{code}/suivi/verify")
def suivi_verify(request: Request, code: str, period: str = Form(...), establishment: str = Form(...)):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    label = f"Vérif Pennylane · {establishment} · {period}"
    start_job("verify_pennylane",
              lambda ctx: run_verify(ctx, company.code, establishment, period),
              company_id=company.id, pack="sterna.caisse", label=label)
    return RedirectResponse(f"/c/{code}/suivi?period={period}", status_code=303)


# ----------------------------- Clients (correspondance) -----------------------------
@router.get("/c/{code}/clients", response_class=HTMLResponse)
def clients_page(request: Request, code: str, q: str = "", etab: str = "",
                 statut: str = "", page: int = 1):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    from collections import Counter
    PAGE_SIZE = 25
    with Session(engine) as s:
        allrows = s.exec(select(ClientAccount).where(ClientAccount.company_id == company.id)
                         .order_by(ClientAccount.establishment, ClientAccount.toporder_name)).all()
    counts = Counter(r.status for r in allrows)
    etablissements = sorted({r.establishment for r in allrows})

    # filtres
    rows = allrows
    if etab:
        rows = [r for r in rows if r.establishment == etab]
    if statut:
        rows = [r for r in rows if r.status == statut]
    if q:
        ql = q.lower().strip()
        def _hit(r):
            return any(ql in (v or "").lower() for v in (
                r.toporder_name, r.pennylane_name, r.siret, r.pennylane_reg_no,
                r.pennylane_external_ref, r.account_411, str(r.pennylane_customer_id or "")))
        rows = [r for r in rows if _hit(r)]

    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    page_rows = rows[start:start + PAGE_SIZE]

    return templates.TemplateResponse(request, "clients.html",
                                      _ctx(request, company=company, rows=page_rows,
                                           counts=dict(counts), etablissements=etablissements,
                                           q=q, etab=etab, statut=statut, page=page, pages=pages,
                                           total=total, total_all=len(allrows),
                                           start=start, page_size=PAGE_SIZE))


@router.post("/c/{code}/clients/sync")
def clients_sync_action(request: Request, code: str):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    start_job("sync_clients", lambda ctx: sync_clients(ctx, company.code),
              company_id=company.id, pack="sterna.caisse", label="Synchronisation comptes clients")
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
        # quels runs ont quels artefacts (requête légère : run_id + kind, sans les données)
        ids = [r.id for r in recent]
        arts = {}
        if ids:
            for run_id, kind in s.exec(
                    select(JobArtifact.run_id, JobArtifact.kind)
                    .where(JobArtifact.run_id.in_(ids))).all():
                arts.setdefault(run_id, set()).add(kind)
    return templates.TemplateResponse(request, "_jobs_feed.html",
                                      _ctx(request, running=running, recent=recent, cmap=cmap, arts=arts))


@router.get("/jobs/{run_id}/report")
def job_report(request: Request, run_id: int):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    with Session(engine) as s:
        run = s.get(Run, run_id)
    if not run or not run.report:
        return RedirectResponse("/jobs", status_code=303)
    from fastapi.responses import PlainTextResponse
    fname = f"compte_rendu_{run.kind}_{run_id}.txt"
    return PlainTextResponse(run.report, headers={
        "Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/jobs/{run_id}/artifact/{kind}")
def job_artifact(request: Request, run_id: int, kind: str):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    from fastapi.responses import Response
    with Session(engine) as s:
        art = s.exec(select(JobArtifact).where(
            JobArtifact.run_id == run_id, JobArtifact.kind == kind)).first()
    if not art:
        return RedirectResponse("/jobs", status_code=303)
    return Response(content=art.data, media_type=art.content_type, headers={
        "Content-Disposition": f'attachment; filename="{art.name}"'})


@router.post("/jobs/demo")
def jobs_demo(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    start_job("demo", demo_job, label="Tâche de démonstration")
    return RedirectResponse("/jobs", status_code=303)
