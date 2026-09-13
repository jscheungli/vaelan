"""CIOP : exercices, lignes d'investissement (libellé, établissement, date), configuration, génération du dossier."""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app.core.db import engine
from app.core.jobs import start_job
from app.core.security import current_user
from app.models import Run, JobArtifact
from app.packs.ciop import service, jobs as cjobs
from app.web.routes import templates, _ctx, _company_or_redirect

router = APIRouter()


def _guard(request: Request, code: str):
    return _company_or_redirect(request, code, feature="ciop")


def _fy(s: str) -> date:
    return date.fromisoformat(s[:10])


def _runs(company, kinds=("ciop_collect", "ciop_build"), n=6):
    with Session(engine) as s:
        rows = s.exec(select(Run).where(Run.company_id == company.id, Run.kind.in_(kinds)).order_by(Run.id.desc()).limit(n)).all()
        arts = {a.run_id: a for a in s.exec(select(JobArtifact).where(JobArtifact.run_id.in_([r.id for r in rows]))).all()} if rows else {}
    return rows, arts


@router.get("/c/{code}/ciop", response_class=HTMLResponse)
def ciop_home(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    items = []
    for fy in service.fy_ends(cfg):
        ex = service.get_exercise(code, fy)
        items.append({"fy": fy, "label": service.fy_label(fy), "collected_at": ex.get("collected_at"), "built_at": ex.get("built_at"), "totals": ex.get("totals") or {}})
    return templates.TemplateResponse(request, "ciop_home.html", _ctx(request, company=company, cfg=cfg, items=items))


@router.get("/c/{code}/ciop/config", response_class=HTMLResponse)
def ciop_config(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return templates.TemplateResponse(request, "ciop_config.html", _ctx(request, company=company, cfg=service.get_config(code), msg=msg))


@router.post("/c/{code}/ciop/config")
async def ciop_config_save(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    g = lambda k, d="": (f.get(k) or d).strip()
    upd = {"identity": {k: g(f"id_{k}") for k in ("name", "address", "siren", "legal_form", "ape")},
           "declarant": {k: g(f"dc_{k}") for k in ("name", "quality", "address", "place", "date", "signature_note")},
           "params": {"code_invest": g("p_code_invest", "ART"), "article": g("p_article", "244 quater W"), "rate_pct": float(g("p_rate_pct", "35").replace(",", ".")),
                      "fy_end_month": int(g("p_fy_end_month", "6")), "fy_end_day": int(g("p_fy_end_day", "30")),
                      "purchase_journals": [x.strip().upper() for x in g("p_purchase_journals", "HA").split(",") if x.strip()], "account_keyword": g("p_account_keyword", "CIOP")},
           "partners": [{"name": g(f"pa{i}_name"), "address": g(f"pa{i}_address"), "siren": g(f"pa{i}_siren"), "share": g(f"pa{i}_share")} for i in range(4) if g(f"pa{i}_name")],
           "sites": [{"code": g(f"si{i}_code").upper(), "label": g(f"si{i}_label"), "address": g(f"si{i}_address"), "aliases": [a.strip() for a in g(f"si{i}_aliases").split(",") if a.strip()]}
                     for i in range(6) if g(f"si{i}_code") and g(f"si{i}_label")]}
    service.save_config(code, upd)
    return RedirectResponse(f"/c/{code}/ciop/config?msg=Configuration enregistrée.", status_code=303)


@router.get("/c/{code}/ciop/{fy}", response_class=HTMLResponse)
def ciop_exercise(request: Request, code: str, fy: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    fy_end = _fy(fy)
    ex = service.get_exercise(code, fy_end)
    runs, arts = _runs(company)
    running = [r for r in runs if r.status == "running"]
    return templates.TemplateResponse(request, "ciop_exercise.html",
                                      _ctx(request, company=company, cfg=cfg, fy=fy_end, fy_label=service.fy_label(fy_end), ex=ex, totals=ex.get("totals") or {},
                                           runs=runs, arts=arts, running=running, msg=msg, site_of=service.site_of, fr=service._fr, eur=lambda x: f"{x:,.2f}".replace(",", " ").replace(".", ",") + " €"))


@router.get("/c/{code}/ciop/{fy}/piece/{entry_id}")
def ciop_piece(request: Request, code: str, fy: str, entry_id: int):
    """La pièce d'une ligne, redemandée à Pennylane à la volée (les URL signées expirent en 30 min)."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    ex = service.get_exercise(code, _fy(fy))
    line = next((l for l in ex.get("lines", []) if int(l.get("entry_id") or 0) == entry_id), None)
    got = service.fetch_piece(code, entry_id, (line or {}).get("attachment_name") or "")
    if not got:
        return Response("Pièce introuvable dans Pennylane.", status_code=404, media_type="text/plain; charset=utf-8")
    fn = service.file_name(line) if line else f"{entry_id}.pdf"
    return Response(got[0], media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{fn}"'})


@router.post("/c/{code}/ciop/{fy}/lines")
async def ciop_lines_save(request: Request, code: str, fy: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    service.update_lines(code, _fy(fy), dict(f))
    return RedirectResponse(f"/c/{code}/ciop/{fy}?msg=Lignes enregistrées.", status_code=303)


@router.post("/c/{code}/ciop/{fy}/collect")
def ciop_collect(request: Request, code: str, fy: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    fy_end = _fy(fy)
    start_job("ciop_collect", lambda ctx: cjobs.run_collect(ctx, code, fy_end), company_id=company.id, pack="ciop",
              label=f"CIOP {code} {service.fy_label(fy_end)} : lecture Pennylane", user=current_user(request))
    return RedirectResponse(f"/c/{code}/ciop/{fy}?msg=Lecture Pennylane lancée, rechargez la page dans quelques secondes.", status_code=303)


@router.post("/c/{code}/ciop/{fy}/build")
def ciop_build(request: Request, code: str, fy: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    fy_end = _fy(fy)
    start_job("ciop_build", lambda ctx: cjobs.run_build(ctx, code, fy_end), company_id=company.id, pack="ciop",
              label=f"CIOP {code} {service.fy_label(fy_end)} : dossier", user=current_user(request))
    return RedirectResponse(f"/c/{code}/ciop/{fy}?msg=Génération du dossier lancée, le ZIP apparaîtra ci-dessous.", status_code=303)
