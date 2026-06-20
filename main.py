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
from app.seed import seed_if_empty
from app.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()              # crée les tables si besoin
    packs_loader.load()    # découvre et enregistre les packs de contrôle
    seed_if_empty()        # admin + sociétés initiales si base vide
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12)

_static = os.path.join(os.path.dirname(__file__), "app", "web", "static")
os.makedirs(_static, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static), name="static")

app.include_router(web_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "app": settings.app_name}
