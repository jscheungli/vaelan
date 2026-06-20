"""Tableau de suivi de clôture : ÉTAPES en lignes, ÉTABLISSEMENTS en colonnes.

Suivi GLOBAL (plus de navigation par mois) : chaque cellule montre jusqu'à quelle
DATE l'étape est faite (couverture) et QUAND elle a été réalisée. Toute étape est
relançable à tout moment. Les étapes que Vaelan ne détecte pas encore sont
déclaratives (l'utilisateur déclare « fait jusqu'au … »), puis Vaelan vérifie.
"""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.core.db import engine
from app.models import ClientAccount, ImportBatch, StepDeclaration, Run
from . import config

_ANOMALIES = {"no_siret", "no_pennylane", "incoherent", "absent_toporder"}
_TZ = timedelta(hours=4)   # La Réunion (affichage)

# Les 9 étapes de la séquence de clôture (lignes du tableau).
STEPS = [
    {"n": "1", "key": "clients", "label": "Synchronisation des clients", "kind": "clients"},
    {"n": "2", "key": "gen_tickets", "label": "Génération du CSV — écritures Tickets", "kind": "gen"},
    {"n": "3", "key": "import_tickets", "label": "Import des écritures Tickets dans Pennylane", "kind": "declare"},
    {"n": "4", "key": "verify_tickets", "label": "Cadrage de l'import Tickets par Vaelan", "kind": "verify"},
    {"n": "5", "key": "gen_factures", "label": "Génération du CSV — écritures Factures", "kind": "soon"},
    {"n": "6", "key": "import_factures", "label": "Import des écritures Factures dans Pennylane", "kind": "soon"},
    {"n": "7", "key": "verify_factures", "label": "Cadrage de l'import Factures par Vaelan", "kind": "soon"},
    {"n": "8", "key": "justificatifs", "label": "Import des justificatifs (PDF factures) par Vaelan", "kind": "soon"},
    {"n": "9", "key": "lettrage", "label": "Lettrage des comptes", "kind": "soon"},
]


def _d(dt):
    return (dt + _TZ).strftime("%d/%m %H:%M") if dt else None       # datetime UTC -> local


def _dl(dt):
    return dt.strftime("%d/%m %H:%M") if dt else None               # datetime déjà local


def _date(d):
    return d.strftime("%d/%m/%Y") if d else None


def build_board(company):
    with Session(engine) as s:
        clients = s.exec(select(ClientAccount).where(ClientAccount.company_id == company.id)).all()
        batches = s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company.id, ImportBatch.kind == "toslt")).all()
        decls = {(d.establishment, d.step): d for d in s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company.id)).all()}
        last_sync = s.exec(select(Run).where(
            Run.company_id == company.id, Run.kind == "sync_clients",
            Run.status == "ok").order_by(Run.id.desc())).first()

    ests = [e["pfx"] for e in config.ESTABLISHMENTS.values()]
    rows = []
    for stp in STEPS:
        cells = {}
        for pfx in ests:
            cells[pfx] = _cell(stp, pfx, clients, batches, decls, last_sync)
        rows.append({"step": stp, "cells": cells})
    return {"establishments": ests, "steps": rows}


def _cell(stp, pfx, clients, batches, decls, last_sync):
    kind = stp["kind"]
    if kind == "clients":
        cl = [c for c in clients if c.establishment == pfx]
        realized = _d(last_sync.finished_at) if last_sync else None
        if not cl:
            return {"state": "todo", "text": "à synchroniser", "realized": realized}
        n_anom = sum(1 for c in cl if c.status in _ANOMALIES)
        if n_anom:
            return {"state": "todo", "text": f"{n_anom} à corriger", "realized": realized,
                    "act": "link"}
        return {"state": "done", "text": "ok", "realized": realized, "act": "link"}

    if kind == "gen":
        bs = [b for b in batches if b.establishment == pfx]
        if not bs:
            return {"state": "todo", "text": "à générer", "act": "link"}
        covered = max(b.date_to for b in bs)
        realized = _d(max((b.created_at for b in bs), default=None))
        return {"state": "done", "coverage": _date(covered), "realized": realized, "act": "link"}

    if kind == "declare":
        d = decls.get((pfx, stp["key"]))
        if d and d.covered_to:
            return {"state": "declared", "coverage": _date(d.covered_to),
                    "realized": _dl(d.done_at), "act": "declare", "step": stp["key"]}
        return {"state": "todo", "text": "à déclarer", "act": "declare", "step": stp["key"]}

    if kind == "verify":
        d = decls.get((pfx, stp["key"]))
        cell = {"act": "verify", "run_id": (d.verify_run_id if d else None)}
        if d and d.verified_at is not None:
            cell["coverage"] = _date(d.covered_to)
            cell["realized"] = _dl(d.verified_at)
            cell["state"] = "done" if d.verify_ok else "error"
            cell["text"] = "cohérent" if d.verify_ok else "écart"
        else:
            cell["state"] = "todo"
            cell["text"] = "à vérifier"
        return cell

    return {"state": "soon", "text": "à venir"}


def declare(company_id, establishment, step, covered_to, undo=False):
    with Session(engine) as s:
        d = s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company_id, StepDeclaration.establishment == establishment,
            StepDeclaration.step == step)).first()
        if undo:
            if d:
                s.delete(d)
                s.commit()
            return
        if not d:
            d = StepDeclaration(company_id=company_id, establishment=establishment, step=step)
        d.covered_to = covered_to
        d.done_at = datetime.utcnow() + _TZ
        d.state = "declared"
        d.updated_at = datetime.utcnow()
        s.add(d)
        s.commit()
