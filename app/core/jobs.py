"""Exécution de tâches en arrière-plan (thread dans le process web).

Chaque tâche = une ligne `runs` mise à jour en continu (étape, progression,
log, statut). La page Jobs lit ces lignes (HTMX). Pas de worker séparé : suffit
à notre échelle. Au démarrage, les runs restés « running » (process redémarré)
sont marqués « interrupted ».
"""
import threading
import time
import traceback
from datetime import datetime
from typing import Callable, Optional

from sqlmodel import Session, select
from app.core.db import engine
from app.core.config import APP_VERSION
from app.models import Run, JobArtifact


class JobContext:
    """Passé à la fonction de job pour publier progression et logs."""
    def __init__(self, run_id: int):
        self.run_id = run_id
        self._log = []

    def _update(self, **fields):
        with Session(engine) as s:
            r = s.get(Run, self.run_id)
            if not r:
                return
            for k, v in fields.items():
                setattr(r, k, v)
            r.updated_at = datetime.utcnow()
            s.add(r)
            s.commit()

    def progress(self, current: int, total: Optional[int] = None, step: Optional[str] = None):
        fields = {"progress_current": current}
        if total is not None:
            fields["progress_total"] = total
        if step is not None:
            fields["step"] = step
        self._update(**fields)

    def log(self, msg: str):
        self._log.append(f"{datetime.utcnow():%H:%M:%S} · {msg}")
        self._update(log="\n".join(self._log[-1000:]))

    def set_report(self, text: str):
        """Attache un compte rendu téléchargeable à la tâche."""
        self._update(report=text)

    def add_artifact(self, kind: str, name: str, data: bytes,
                     content_type: str = "application/octet-stream"):
        """Rattache un fichier téléchargeable (stocké en base) à la tâche."""
        with Session(engine) as s:
            # un seul artefact par (tâche, kind) : on remplace
            for a in s.exec(select(JobArtifact).where(
                    JobArtifact.run_id == self.run_id, JobArtifact.kind == kind)).all():
                s.delete(a)
            s.add(JobArtifact(run_id=self.run_id, kind=kind, name=name,
                              data=data, content_type=content_type))
            s.commit()


def start_job(kind: str, fn: Callable[[JobContext], Optional[str]],
              company_id: Optional[int] = None, pack: Optional[str] = None,
              label: Optional[str] = None) -> int:
    """Crée un run et lance la fonction dans un thread daemon. Renvoie l'id du run."""
    with Session(engine) as s:
        run = Run(kind=kind, company_id=company_id, pack=pack, label=label, status="running",
                  step="démarrage…", progress_current=0, app_version=APP_VERSION)
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id

    def _runner():
        ctx = JobContext(run_id)
        try:
            summary = fn(ctx)
            _finish(run_id, "ok", summary or "Terminé")
        except Exception as e:
            ctx.log("ERREUR : " + str(e))
            ctx.log(traceback.format_exc())
            _finish(run_id, "error", str(e)[:250])

    threading.Thread(target=_runner, daemon=True).start()
    return run_id


def _finish(run_id: int, status: str, summary: str):
    with Session(engine) as s:
        r = s.get(Run, run_id)
        if r:
            r.status = status
            r.summary = summary
            r.finished_at = datetime.utcnow()
            r.updated_at = datetime.utcnow()
            s.add(r)
            s.commit()


def mark_interrupted_on_startup():
    """Au redémarrage du process, plus aucun thread ne tourne : on assainit."""
    with Session(engine) as s:
        for r in s.exec(select(Run).where(Run.status == "running")).all():
            r.status = "interrupted"
            r.finished_at = datetime.utcnow()
            r.summary = (r.summary or "") + " [interrompu au redémarrage]"
            s.add(r)
        s.commit()


# --- job de démonstration (valide l'infra de progression en direct) ---
def demo_job(ctx: JobContext) -> str:
    total = 20
    ctx.log("Démarrage de la tâche de démonstration")
    for i in range(1, total + 1):
        time.sleep(0.8)
        ctx.progress(i, total, step=f"étape {i}/{total}")
        if i % 5 == 0:
            ctx.log(f"avancement {i}/{total}")
    ctx.log("Tâche de démonstration terminée")
    return f"Démo terminée ({total} étapes)"
