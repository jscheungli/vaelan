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
    {"n": "2", "key": "gen_tickets", "label": "Génération du CSV (caisse + factures)", "kind": "gen", "bkind": "toslt"},
    {"n": "3", "key": "import_tickets", "label": "Import du CSV dans Pennylane", "kind": "declare"},
    {"n": "4", "key": "verify_tickets", "label": "Cadrage de l'import par Vaelan", "kind": "verify"},
    {"n": "5", "key": "justificatifs", "label": "Attache des justificatifs (PDF factures) par Vaelan", "kind": "justif"},
    {"n": "6", "key": "lettrage", "label": "Lettrage des comptes 411 (niveau société)", "kind": "lettrage", "scope": "company"},
    {"n": "7", "key": "recon_caisse", "label": "Lettrage caisse/CB ↔ dépôts bancaires (à venir)", "kind": "soon"},
]


def _d(dt):
    return (dt + _TZ).strftime("%d/%m %H:%M") if dt else None       # datetime UTC -> local


def _dl(dt):
    return dt.strftime("%d/%m %H:%M") if dt else None               # datetime déjà local


def _date(d):
    return d.strftime("%d/%m/%Y") if d else None


def _target_date():
    """Dernier jour du mois PRÉCÉDENT (le dernier mois terminé à clôturer)."""
    from datetime import date
    today = (datetime.utcnow() + _TZ).date()
    return today.replace(day=1) - timedelta(days=1)


def build_board(company):
    with Session(engine) as s:
        clients = s.exec(select(ClientAccount).where(ClientAccount.company_id == company.id)).all()
        batches = s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company.id,
            ImportBatch.kind.in_(["toslt", "toslf"]))).all()
        decls = {(d.establishment, d.step): d for d in s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company.id)).all()}
        last_sync = s.exec(select(Run).where(
            Run.company_id == company.id, Run.kind == "sync_clients",
            Run.status == "ok").order_by(Run.id.desc())).first()

    target = _target_date()
    # colonnes : SM, LP, SL (de gauche à droite) — ordre de la config
    ests = [e["pfx"] for e in config.establishments(company.code).values()]
    today = (datetime.utcnow() + _TZ).date()
    rows = []
    for stp in STEPS:
        if stp.get("scope") == "company":
            # étape au niveau société : une seule cellule (comptes 411 partagés)
            c = _cell(stp, "", clients, batches, decls, last_sync, target)
            c["today"] = today.isoformat()
            rows.append({"step": stp, "company": True, "cell": c})
            continue
        cells = {}
        for pfx in ests:
            cells[pfx] = _cell(stp, pfx, clients, batches, decls, last_sync, target)
        rows.append({"step": stp, "cells": cells})
    return {"establishments": ests, "steps": rows, "target": target.strftime("%d/%m/%Y"),
            "today": today.isoformat()}


def _cov_state(covered_to, target):
    """Vert si on a couvert jusqu'au dernier mois terminé, sinon orange (mois à clôturer)."""
    return "done" if (covered_to and covered_to >= target) else "todo"


def _cell(stp, pfx, clients, batches, decls, last_sync, target):
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
        bs = [b for b in batches if b.establishment == pfx and b.kind == stp.get("bkind", "toslt")]
        if not bs:
            return {"state": "todo", "text": "à générer", "act": "link"}
        covered = max(b.date_to for b in bs)
        realized = _d(max((b.created_at for b in bs), default=None))
        st = _cov_state(covered, target)
        return {"state": st, "coverage": _date(covered), "realized": realized, "act": "link",
                "text": (None if st == "done" else "mois à clôturer")}

    if kind == "declare":
        d = decls.get((pfx, stp["key"]))
        if d and d.covered_to:
            st = "declared" if d.covered_to >= target else "todo"
            return {"state": st, "coverage": _date(d.covered_to),
                    "realized": _dl(d.done_at), "by": d.declared_by, "act": "declare", "step": stp["key"],
                    "text": (None if st == "declared" else "mois à clôturer")}
        return {"state": "todo", "text": "à déclarer", "act": "declare", "step": stp["key"]}

    if kind == "verify":
        d = decls.get((pfx, stp["key"]))
        cell = {"act": "verify", "run_id": (d.verify_run_id if d else None)}
        if d and d.verified_at is not None:
            cell["coverage"] = _date(d.covered_to)
            cell["realized"] = _dl(d.verified_at)
            if not d.verify_ok:
                cell["state"], cell["text"] = "error", "écart"
            else:
                cell["state"] = _cov_state(d.covered_to, target)
                cell["text"] = "cohérent" if cell["state"] == "done" else "à re-vérifier (mois à clôturer)"
        else:
            cell["state"] = "todo"
            cell["text"] = "à vérifier"
        return cell

    if kind == "justif":
        d = decls.get((pfx, stp["key"]))
        cell = {"act": "justif", "run_id": (d.verify_run_id if d else None)}
        if d and d.verified_at is not None:
            cell["coverage"] = _date(d.covered_to)
            cell["realized"] = _dl(d.verified_at)
            if not d.verify_ok:
                cell["state"], cell["text"] = "error", "PDF manquant(s)"
            else:
                cell["state"] = _cov_state(d.covered_to, target)
                cell["text"] = "complet" if cell["state"] == "done" else "à relancer (mois à clôturer)"
        else:
            cell["state"] = "todo"
            cell["text"] = "à attacher"
        return cell

    if kind == "lettrage":
        d = decls.get((pfx, stp["key"]))   # pfx == "" (niveau société)
        cell = {"act": "lettrage", "run_id": (d.verify_run_id if d else None)}
        if d and d.verified_at is not None:
            cell["coverage"] = _date(d.covered_to)   # date d'arrêté
            cell["coverage_iso"] = d.covered_to.isoformat() if d.covered_to else None
            cell["realized"] = _dl(d.verified_at)
            cell["state"] = "done" if d.verify_ok else "error"
            cell["text"] = "lettré" if d.verify_ok else "points à traiter"
        else:
            cell["state"] = "todo"
            cell["text"] = "à lettrer"
        return cell

    return {"state": "soon", "text": "à venir"}


def reset(company_id, establishment):
    """Réinitialise le suivi d'un établissement : efface déclarations + vérifications
    (pour repartir de zéro après suppression des imports dans Pennylane)."""
    with Session(engine) as s:
        for d in s.exec(select(StepDeclaration).where(
                StepDeclaration.company_id == company_id,
                StepDeclaration.establishment == establishment)).all():
            s.delete(d)
        s.commit()


def declare(company_id, establishment, step, covered_to, undo=False, user_email=None):
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
        d.declared_by = user_email
        d.state = "declared"
        d.updated_at = datetime.utcnow()
        s.add(d)
        s.commit()
