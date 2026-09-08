"""Vaelan — point d'entrée de l'application.

Plateforme de contrôles comptables (Pennylane) multi-sociétés.
Le métier vit dans des « packs de contrôle » enregistrés dans le registre ;
le socle fournit auth, DB, connecteurs, dashboard et journal des runs.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import os

from app.core.config import settings
from app.core.db import init_db
from app.core import packs_loader
from app.core.jobs import mark_interrupted_on_startup
from app.seed import seed_if_empty
from app.web.routes import router as web_router
from app.web.vds_routes import router as vds_router
from app.core import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()                       # crée les tables / colonnes si besoin
    packs_loader.load()             # découvre et enregistre les packs de contrôle
    seed_if_empty()                 # admin + sociétés initiales si base vide
    mark_interrupted_on_startup()   # assainit les jobs restés « running »
    _register_schedules()
    scheduler.start()               # tâches quotidiennes (check-in VDS : synchro Lodgify, relances)
    yield


def _register_schedules():
    from app.core.jobs import start_job
    from app.packs.vds import jobs as vds_jobs
    from app.models import Company
    from sqlmodel import Session, select
    from app.core.db import engine

    def _vds_daily():
        with Session(engine) as s:
            c = s.exec(select(Company).where(Company.code == "VDS")).first()
        start_job("vds_daily", vds_jobs.run_daily, company_id=c.id if c else None, pack="vds",
                  label="Passe quotidienne check-in (Lodgify, invitations, relances, alertes, ERP)")
    def _vds_sync_pm():
        with Session(engine) as s:
            c = s.exec(select(Company).where(Company.code == "VDS")).first()
        start_job("vds_sync", lambda ctx: vds_jobs.run_lodgify_sync(ctx, invite=True), company_id=c.id if c else None, pack="vds",
                  label="Synchro Lodgify (après-midi) — nouvelles réservations, alertes, invitations")
    scheduler.register("vds_daily", 8, _vds_daily)
    scheduler.register("vds_sync_pm", 17, _vds_sync_pm)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12)

_static = os.path.join(os.path.dirname(__file__), "app", "web", "static")
os.makedirs(_static, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static), name="static")

app.include_router(web_router)
app.include_router(vds_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "app": settings.app_name}
