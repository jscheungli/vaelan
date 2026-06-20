"""Tableau de suivi de clôture : matrice (établissement × étape) pour un mois.

Navigation mensuelle. Pour chaque cellule on calcule un état + une date de
« couverture » (fait jusqu'au JJ/MM) — utile quand on avance à la semaine sans
attendre la fin du mois. Les étapes que Vaelan ne détecte pas encore sont
DÉCLARATIVES (l'utilisateur déclare « fait », Vaelan confirmera ensuite).
"""
from datetime import date
from calendar import monthrange

from sqlmodel import Session, select

from app.core.db import engine
from app.models import ClientAccount, ImportBatch, StepDeclaration
from . import config

_ANOMALIES = {"no_siret", "no_pennylane", "incoherent", "absent_toporder"}
_MONTHS = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]

# colonnes du tableau (l'ordre = la séquence de clôture)
COLUMNS = [
    {"key": "clients", "label": "1 · Clients"},
    {"key": "caisse", "label": "2 · Caisse"},
    {"key": "factures", "label": "3 · Factures"},
    {"key": "import_pl", "label": "4 · Import PL"},
    {"key": "pj", "label": "5 · PJ"},
    {"key": "lettrage", "label": "6 · Lettrage"},
]
# étapes déclaratives (déclarer → vérifier), par ordre d'apparition
DECLARATIVE = {"import_pl", "pj", "lettrage"}


def _period_nav(period):
    y, m = int(period[:4]), int(period[5:7])
    start = date(y, m, 1)
    end = date(y, m, monthrange(y, m)[1])
    prev = (date(y, m, 1).replace(day=1))
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return start, end, f"{py:04d}-{pm:02d}", f"{ny:04d}-{nm:02d}", f"{_MONTHS[m]} {y}"


def _fr(d):
    return d.strftime("%d/%m") if d else None


def build_board(company, period):
    start, end, prev, nxt, label = _period_nav(period)
    with Session(engine) as s:
        clients = s.exec(select(ClientAccount).where(
            ClientAccount.company_id == company.id)).all()
        batches = s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company.id, ImportBatch.kind == "toslt")).all()
        decls = s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company.id,
            StepDeclaration.period == period)).all()
    decl_map = {(d.establishment, d.step): d for d in decls}

    rows = []
    for est_name, est in config.ESTABLISHMENTS.items():
        pfx = est["pfx"]
        cells = {}

        # 1) Clients (auto, à l'échelle société mais détaillé par établissement)
        cl = [c for c in clients if c.establishment == pfx]
        n_anom = sum(1 for c in cl if c.status in _ANOMALIES)
        if not cl:
            cells["clients"] = {"state": "todo", "text": "à synchroniser"}
        elif n_anom:
            cells["clients"] = {"state": "todo", "text": f"{n_anom} à corriger"}
        else:
            cells["clients"] = {"state": "done", "text": "ok"}

        # 2) Caisse TOSLT (auto : couverture = max date_to des lots dans le mois)
        covered = None
        for b in batches:
            if b.establishment != pfx:
                continue
            if b.date_to < start or b.date_from > end:
                continue
            cov = min(b.date_to, end)
            covered = cov if (covered is None or cov > covered) else covered
        if covered is None:
            cells["caisse"] = {"state": "todo", "text": "à générer"}
        elif covered >= end:
            cells["caisse"] = {"state": "done", "text": f"→ {_fr(end)}"}
        else:
            cells["caisse"] = {"state": "partial", "text": f"→ {_fr(covered)}"}

        # 3) Factures TOSLF — pas encore dans Vaelan
        cells["factures"] = {"state": "soon", "text": "à venir"}

        # 4-5-6) étapes déclaratives
        for key in ("import_pl", "pj", "lettrage"):
            d = decl_map.get((pfx, key))
            if key in ("pj", "lettrage"):
                # déclaratif activable plus tard ; pour l'instant repère « à venir »
                if d and d.state == "verified":
                    cells[key] = {"state": "done", "text": f"vérifié → {_fr(d.covered_to or end)}"}
                elif d:
                    cells[key] = {"state": "declared", "text": f"déclaré → {_fr(d.covered_to or end)}",
                                  "can_verify": True}
                else:
                    cells[key] = {"state": "soon", "text": "à venir"}
            else:  # import_pl : déclaratif actif
                if d and d.state == "verified":
                    cells[key] = {"state": "done", "text": f"vérifié → {_fr(d.covered_to or end)}"}
                elif d:
                    cells[key] = {"state": "declared",
                                  "text": f"déclaré → {_fr(d.covered_to or end)}",
                                  "can_undo": True}
                else:
                    cells[key] = {"state": "todo", "text": "à déclarer", "can_declare": True}

        rows.append({"establishment": pfx, "name": est_name, "cells": cells})

    return {"period": period, "label": label, "prev": prev, "next": nxt,
            "end": end, "columns": COLUMNS, "rows": rows}


def declare(company_id, establishment, period, step, covered_to=None, undo=False):
    """Déclare (ou annule) une étape pour (établissement, mois)."""
    start, end, *_ = _period_nav(period)
    with Session(engine) as s:
        d = s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company_id,
            StepDeclaration.establishment == establishment,
            StepDeclaration.period == period, StepDeclaration.step == step)).first()
        if undo:
            if d:
                s.delete(d)
                s.commit()
            return
        if not d:
            d = StepDeclaration(company_id=company_id, establishment=establishment,
                                period=period, step=step)
        d.state = "declared"
        d.covered_to = covered_to or end
        from datetime import datetime
        d.updated_at = datetime.utcnow()
        s.add(d)
        s.commit()
