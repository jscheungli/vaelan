import os
from datetime import timedelta
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


# ----------------------------- Utilisateurs (admin) -----------------------------
def _require_super(request):
    u = current_user(request)
    if not u:
        return None, RedirectResponse("/login", status_code=303)
    if not u.is_superuser:
        return None, templates.TemplateResponse(request, "forbidden.html", _ctx(request), status_code=403)
    return u, None


@router.get("/regles", response_class=HTMLResponse)
def regles(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "regles.html", _ctx(request))


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    from app.models import User, UserCompanyAccess
    u, redir = _require_super(request)
    if redir:
        return redir
    with Session(engine) as s:
        users = s.exec(select(User).order_by(User.id)).all()
        companies = s.exec(select(Company).order_by(Company.code)).all()
        access = {(a.user_id, a.company_id): a.role
                  for a in s.exec(select(UserCompanyAccess)).all()}
    return templates.TemplateResponse(request, "admin_users.html",
                                      _ctx(request, users=users, companies=companies, access=access))


@router.post("/admin/users/add")
def admin_user_add(request: Request, email: str = Form(...), name: str = Form(...),
                   password: str = Form(...), is_superuser: str = Form("")):
    from app.models import User
    from app.core.security import hash_password
    u, redir = _require_super(request)
    if redir:
        return redir
    em = email.lower().strip()
    with Session(engine) as s:
        if not s.exec(select(User).where(User.email == em)).first():
            s.add(User(email=em, name=name.strip() or em, password_hash=hash_password(password),
                       is_superuser=bool(is_superuser), active=True))
            s.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/access")
def admin_user_access(request: Request, user_id: int = Form(...),
                      company_id: int = Form(...), role: str = Form("")):
    from app.models import UserCompanyAccess
    u, redir = _require_super(request)
    if redir:
        return redir
    with Session(engine) as s:
        a = s.exec(select(UserCompanyAccess).where(
            UserCompanyAccess.user_id == user_id,
            UserCompanyAccess.company_id == company_id)).first()
        if not role:               # retirer l'accès
            if a:
                s.delete(a)
        elif a:
            a.role = role
            s.add(a)
        else:
            s.add(UserCompanyAccess(user_id=user_id, company_id=company_id, role=role))
        s.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{uid}/toggle")
def admin_user_toggle(request: Request, uid: int):
    from app.models import User
    u, redir = _require_super(request)
    if redir:
        return redir
    with Session(engine) as s:
        target = s.get(User, uid)
        if target and target.id != u.id:        # on ne se désactive pas soi-même
            target.active = not target.active
            s.add(target)
            s.commit()
    return RedirectResponse("/admin/users", status_code=303)


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

    pl = pennylane.for_company(company.code)
    health = pl.health() if pl else {"ok": False, "error": "clé API non configurée"}
    board = caisse_suivi.build_board(company)
    return templates.TemplateResponse(
        request, "suivi.html",
        _ctx(request, company=company, board=board, role=role_for(user, company),
             health=health, home=True),
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
def import_form(request: Request, code: str, est: str = ""):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    establishments = list(caisse_config.ESTABLISHMENTS.keys())
    # pfx (ex. ?est=SM venant du tableau de suivi) -> nom complet à pré-sélectionner
    pfx2name = {e["pfx"]: name for name, e in caisse_config.ESTABLISHMENTS.items()}
    selected = pfx2name.get(est.upper(), "")
    with Session(engine) as s:
        batches = s.exec(select(ImportBatch).where(ImportBatch.company_id == company.id)
                         .order_by(ImportBatch.id.desc()).limit(20)).all()
    # date de début par défaut = lendemain du dernier import (toslt) de l'établissement choisi
    default_from = ""
    if est:
        prev = [b.date_to for b in batches if b.establishment == est.upper() and b.kind == "toslt"]
        if prev:
            default_from = (max(prev) + timedelta(days=1)).isoformat()
    return templates.TemplateResponse(request, "import.html",
                                      _ctx(request, company=company, establishments=establishments,
                                           batches=batches, selected_est=selected,
                                           default_from=default_from))


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
              company_id=company.id, pack="sterna.caisse", label=label, user=current_user(request))
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
              company_id=company.id, pack="sterna.caisse", label=label, user=current_user(request))
    return RedirectResponse("/jobs", status_code=303)


# ----------------------------- Configuration (comptes / journaux) -----------------------------
@router.get("/c/{code}/config", response_class=HTMLResponse)
def config_page(request: Request, code: str, saved: str = ""):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    cfg = caisse_config.resolve(company.code)
    sections = caisse_config.config_sections(cfg)
    defaults = caisse_config.flat_defaults()
    return templates.TemplateResponse(request, "config.html",
                                      _ctx(request, company=company, sections=sections,
                                           defaults=defaults, saved=bool(saved)))


@router.post("/c/{code}/config")
async def config_save(request: Request, code: str):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    from app.models import Setting
    form = await request.form()
    defaults = caisse_config.flat_defaults()
    with Session(engine) as s:
        existing = {st.key: st for st in s.exec(select(Setting).where(
            Setting.company_code == company.code)).all()}
        for key, default in defaults.items():
            if key not in form:
                continue
            val = str(form[key]).strip()
            st = existing.get(key)
            if val == default or val == "":
                if st:
                    s.delete(st)                       # retour au défaut -> on retire la surcharge
            elif st:
                st.value = val
                s.add(st)
            else:
                s.add(Setting(company_code=company.code, key=key, value=val))
        s.commit()
    return RedirectResponse(f"/c/{code}/config?saved=1", status_code=303)


# ----------------------------- Suivi de clôture (tableau de bord) -----------------------------
@router.get("/c/{code}/suivi")
def suivi_page(request: Request, code: str):
    # le tableau de suivi est désormais la page d'accueil de la société
    return RedirectResponse(f"/c/{code}", status_code=303)


@router.post("/c/{code}/suivi/declare")
def suivi_declare(request: Request, code: str, establishment: str = Form(...),
                  step: str = Form(...), covered_to: str = Form(""), undo: str = Form("")):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    from datetime import date as _date
    cov = None
    if covered_to:
        try:
            cov = _date.fromisoformat(covered_to)
        except ValueError:
            cov = None
    u = current_user(request)
    caisse_suivi.declare(company.id, establishment, step, cov, undo=bool(undo),
                         user_email=(u.email if u else None))
    return RedirectResponse(f"/c/{code}/suivi", status_code=303)


@router.post("/c/{code}/suivi/verify")
def suivi_verify(request: Request, code: str, establishment: str = Form(...)):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    label = f"Vérif Pennylane (Tickets) · {establishment}"
    start_job("verify_pennylane",
              lambda ctx: run_verify(ctx, company.code, establishment),
              company_id=company.id, pack="sterna.caisse", label=label,
              user=current_user(request))
    return RedirectResponse("/jobs", status_code=303)   # -> page Tâches (suivi live)


@router.post("/c/{code}/suivi/reset")
def suivi_reset(request: Request, code: str, establishment: str = Form(...)):
    company, redir = _company_or_redirect(request, code)
    if redir:
        return redir
    caisse_suivi.reset(company.id, establishment)
    return RedirectResponse(f"/c/{code}", status_code=303)


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
              company_id=company.id, pack="sterna.caisse", label="Synchronisation comptes clients",
              user=current_user(request))
    return RedirectResponse("/jobs", status_code=303)


# ----------------------------- Jobs (tâches) -----------------------------
@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "jobs.html", _ctx(request))


@router.get("/jobs/feed", response_class=HTMLResponse)
def jobs_feed(request: Request, page: int = 1):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    from sqlalchemy import func
    PER_PAGE = 10
    page = max(1, page)
    with Session(engine) as s:
        running = s.exec(select(Run).where(Run.status == "running").order_by(Run.id.desc())).all()
        total = s.exec(select(func.count()).select_from(Run).where(Run.status != "running")).one()
        total = total[0] if isinstance(total, tuple) else total
        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, total_pages)
        recent = s.exec(select(Run).where(Run.status != "running").order_by(Run.id.desc())
                        .offset((page - 1) * PER_PAGE).limit(PER_PAGE)).all()
        cmap = {c.id: c.name for c in s.exec(select(Company)).all()}
        # quels runs ont quels artefacts (requête légère : run_id + kind, sans les données)
        ids = [r.id for r in recent]
        arts = {}
        if ids:
            for run_id, kind in s.exec(
                    select(JobArtifact.run_id, JobArtifact.kind)
                    .where(JobArtifact.run_id.in_(ids))).all():
                arts.setdefault(run_id, set()).add(kind)
    # 286 = HTMX arrête le polling quand aucune tâche ne tourne (sinon le rafraîchissement
    # 2s empêche de scroller, ex. jusqu'au pied de page). Reprend au prochain chargement.
    status = 200 if running else 286
    return templates.TemplateResponse(request, "_jobs_feed.html",
                                      _ctx(request, running=running, recent=recent, cmap=cmap, arts=arts,
                                           page=page, total_pages=total_pages, total=total),
                                      status_code=status)


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
