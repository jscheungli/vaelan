"""Jobs du pack Sterna — Caisse.

run_cadrage : pull tickets -> calcul CA -> parse journal de synthèse ->
comparaison (CA Total + modes de paiement + période). C'est le VERROU :
tant que ça ne cadre pas, on ne génère pas l'import.

run_generate_toslt : génère le CSV d'import Pennylane (journal TOSLT). Pré-vol
souple : si une créance de la période ne se route vers aucun compte client, on
refuse de générer et on liste les clients à corriger.
"""
import csv as _csv
import io
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.core.db import engine as _db_engine
from app.models import Company, ImportBatch
from . import engine, synthese, csvgen, config

EXPORT_DIR = Path(__file__).resolve().parents[3] / "data" / "exports"


def _batch_code(company_id, pfx, kind):
    """Identifiant de lot compact, ex. SLT07 (SL, Tickets, 7e lot)."""
    letter = "T" if kind == "tickets" else "F"
    with Session(_db_engine) as s:
        n = len(s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company_id,
            ImportBatch.establishment == pfx,
            ImportBatch.kind == ("toslt" if kind == "tickets" else "toslf"))).all())
    return f"{pfx}{letter}{n + 1:02d}"


def run_generate_toslt(ctx, company_code, establishment, date_from, date_to):
    from datetime import date as _date
    pfx = config.ESTABLISHMENTS[establishment]["pfx"]
    with Session(_db_engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
    if not company:
        raise RuntimeError(f"société {company_code} introuvable")

    code = _batch_code(company.id, pfx, "tickets")
    ctx.log(f"Lot {code} · {establishment} · {date_from} → {date_to}")
    ctx.progress(0, None, step="génération TOSLT (pull tickets + pré-vol)…")

    def _prog(n, step):
        ctx.progress(n, None, step=step)

    res = csvgen.build_toslt(establishment, date_from, date_to, company.id, code, on_progress=_prog)

    if res.get("unresolved"):
        ctx.log(f"⛔ Pré-vol : {len(res['unresolved'])} créance(s) sans compte client — "
                "génération refusée. Corrige dans TopOrder/Pennylane puis resynchronise les clients.")
        for u in res["unresolved"][:15]:
            ctx.log(f"   • {u['date']} · facture {u['facture'] or '?'} · "
                    f"companyId {u['company_id']} · {u['amount']:.2f} €")
        return f"Bloqué : {len(res['unresolved'])} créance(s) sans compte client à corriger"

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"import_TOSLT_{code}_{date_from}_{date_to}.csv"
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(res["header"])
    w.writerows(res["rows"])
    path.write_text("﻿" + buf.getvalue(), encoding="utf-8")  # BOM pour Pennylane

    with Session(_db_engine) as s:
        s.add(ImportBatch(
            company_id=company.id, establishment=pfx, code=code, kind="toslt",
            date_from=_date.fromisoformat(date_from), date_to=_date.fromisoformat(date_to),
            status="generated", n_entries=res["n_agg"] + res["n_creances"],
            amount=res["ca_ttc"], csv_path=str(path)))
        s.commit()

    bal = "équilibré ✓" if res["balanced"] else f"⚠️ DÉSÉQUILIBRE (D {res['debit']} ≠ C {res['credit']})"
    ctx.log(f"CSV généré : {len(res['rows'])} lignes · {res['n_agg']} écritures jour "
            f"+ {res['n_creances']} créances · {bal}")
    ctx.log(f"CA TTC {res['ca_ttc']:.2f} € (HT {res['ca_ht']:.2f} + TVA {res['tva']:.2f}) · "
            f"encaissé {res['encaisse']:.2f} · créances 411 {res['creances']:.2f} · écart {res['ecart']:.2f}")
    return f"Lot {code} généré — {res['ca_ttc']:.2f} € · {res['n_creances']} créances · {bal}"


def run_cadrage(ctx, establishment, date_from, date_to, synthese_bytes):
    ctx.log(f"Établissement : {establishment} | période {date_from} → {date_to}")

    # On parse la synthèse d'abord : son « nombre de clients » sert de total
    # approximatif pour une barre de progression en % pendant le pull.
    ctx.progress(0, None, step="lecture du journal de synthèse…")
    syn = synthese.parse(synthese_bytes)
    total = int(syn["nb_clients"]) if syn.get("nb_clients") else None
    ctx.log(f"Synthèse : CA Total {syn.get('ca_total')} € | ~{total or '?'} clients | période {syn.get('period')}")

    def _prog(n, step):
        # plafonné à total-1 tant que ça tourne (le total est approximatif)
        cur = min(n, total - 1) if total else n
        ctx.progress(cur, total, step=step)

    ctx.progress(0, total, step="calcul du CA depuis les tickets…")
    ca = engine.compute_ca(establishment, date_from, date_to, on_progress=_prog)
    ctx.log(f"CA tickets : {ca['ca_ttc']:.2f} € TTC ({ca['n_tickets']} tickets) "
            f"| HT {ca['ca_ht']:.2f} | TVA {ca['tva']:.2f} | créances {ca['creances_total']:.2f}")

    ctx.progress(total or ca["n_tickets"], total or ca["n_tickets"], step="cadrage…")
    if syn.get("ca_total") is None:
        ctx.log("⚠️ impossible de lire le CA Total dans la synthèse")
        return "Synthèse illisible (CA Total introuvable)"
    ctx.log(f"Synthèse : CA Total {syn['ca_total']:.2f} € | période {syn.get('period')}")

    issues = []
    p = syn.get("period")
    if p and (p["date_from"] != date_from or p["date_to"] != date_to):
        issues.append(f"période synthèse {p['date_from']}→{p['date_to']} ≠ demandée")

    diff = round((ca["ca_ttc"] or 0) - syn["ca_total"], 2)
    ctx.log(f"Cadrage CA : tickets {ca['ca_ttc']:.2f} vs synthèse {syn['ca_total']:.2f} → écart {diff:+.2f}")
    if abs(diff) >= 0.05:
        issues.append(f"écart CA {diff:+.2f} €")

    for mode, synv in (syn.get("payments") or {}).items():
        ourv = ca["payments"].get(mode, 0.0)
        d = round(ourv - synv, 2)
        flag = "" if abs(d) < 0.05 else "  ⚠️"
        ctx.log(f"  {mode} : tickets {ourv:.2f} vs synthèse {synv:.2f} → {d:+.2f}{flag}")
        if abs(d) >= 0.05:
            issues.append(f"écart {mode} {d:+.2f} €")

    if not issues:
        ctx.log("✅ CADRAGE PARFAIT — l'import pourra être généré.")
        return f"Cadré ✓ — CA {ca['ca_ttc']:.2f} € = synthèse ({ca['n_tickets']} tickets)"
    ctx.log("❌ ÉCARTS détectés : " + " ; ".join(issues))
    return "Écart : " + " ; ".join(issues[:3])
