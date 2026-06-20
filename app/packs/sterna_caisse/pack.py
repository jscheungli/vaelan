"""Pack de contrôle : Sterna — Caisse.

La page société est un DÉROULÉ ordonné (séquence de clôture), pas un tableau de
tuiles : on voit dans quel ordre exécuter les actions, ce qui est disponible et
ce qui viendra. Le statut de chaque étape est calculé en direct quand c'est utile
(ex. nombre d'anomalies sur les comptes clients).
"""
from sqlmodel import Session, select

from app.core.db import engine
from app.core.registry import Pack, Phase, Step, Action, register
from app.models import ClientAccount, ImportBatch

_ANOMALIES = {"no_siret", "no_pennylane", "incoherent", "absent_toporder"}


def _clients_status(company_id):
    with Session(engine) as s:
        rows = s.exec(select(ClientAccount).where(ClientAccount.company_id == company_id)).all()
    if not rows:
        return "attention", "à synchroniser"
    n_ok = sum(1 for r in rows if r.status == "ok")
    n_anom = sum(1 for r in rows if r.status in _ANOMALIES)
    if n_anom:
        return "attention", f"{n_ok} ok · {n_anom} à corriger"
    return "available", f"{n_ok} ok"


def _n_batches(company_id, kind):
    with Session(engine) as s:
        return len(s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company_id, ImportBatch.kind == kind)).all())


def workflow(ctx):
    company = ctx["company"]
    base = f"/c/{company.code}"

    cl_state, cl_badge = _clients_status(company.id)
    n_toslt = _n_batches(company.id, "toslt")

    return [
        Phase("① Préparation", "Avant toute génération", [
            Step("1", "Comptes clients (pro)",
                 "Synchroniser TopOrder ↔ Pennylane et résoudre les anomalies "
                 "(SIRET / Identifiant client). Indispensable pour router les créances.",
                 state=cl_state, badge=cl_badge,
                 href=f"{base}/clients", cta="Ouvrir les comptes clients"),
        ]),
        Phase("② Génération des imports", "Cadrage inclus · un CSV par établissement et par période", [
            Step("2", "Import caisse — TOSLT (tickets)",
                 "Cadre le CA sur la synthèse, puis génère le CSV (CA + créances 411). "
                 "Le compte rendu et le CSV restent attachés à la tâche.",
                 state="available", badge=(f"{n_toslt} lot(s) généré(s)" if n_toslt else "prêt"),
                 href=f"{base}/import", cta="Cadrer puis générer"),
            Step("3", "Import factures — TOSLF (reclassement)",
                 "Reclassement HT du CA facturé : 70101 → 70102 (B2C) / 7012 (B2B), par facture.",
                 state="soon", badge="à venir"),
        ]),
        Phase("③ Import dans Pennylane", "Étape manuelle", [
            Step("4", "Importer les CSV dans Pennylane",
                 "Importer les lots générés dans Pennylane (chaque lot est supprimable d'un bloc "
                 "grâce à son identifiant). Vaelan conserve le CSV et le compte rendu de chaque lot.",
                 state="manual", badge="manuel"),
        ]),
        Phase("④ Après import (via API)", "Une fois les écritures dans Pennylane", [
            Step("5", "Accrocher les pièces jointes",
                 "Rattacher les PDF de factures aux écritures et contrôler qu'aucun "
                 "justificatif ne manque.",
                 state="soon", badge="à venir"),
            Step("6", "Lettrage des créances",
                 "Lettrer les comptes clients 411 (créances) avec les règlements "
                 "(espèces, virements, inter-société).",
                 state="soon", badge="à venir"),
        ]),
    ]


register(Pack(
    company_code="STERNA",
    domain="caisse",
    title="Caisse",
    workflow_fn=workflow,
    actions=[
        Action(key="import", label="Générer un import (CSV)"),
        Action(key="attach_pj", label="Attacher les PJ manquantes"),
    ],
))
