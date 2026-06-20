"""Jobs du pack Sterna — Caisse.

run_cadrage : pull tickets -> calcul CA -> parse journal de synthèse ->
comparaison (CA Total + modes de paiement + période). Vérification seule.

run_generate_toslt : action UNIFIÉE (cadrage + génération). On parse la synthèse,
on construit le TOSLT en un seul pull, on CADRE (CA Total + modes de paiement +
période) ; le CSV n'est écrit QUE si ça cadre ET que le pré-vol client passe
(chaque créance routée vers un compte 411). Sinon on s'arrête et on explique.
"""
import csv as _csv
import io
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.core.db import engine as _db_engine
from app.models import Company, ImportBatch
from . import engine, synthese, csvgen, config, report

EXPORT_DIR = Path(__file__).resolve().parents[3] / "data" / "exports"
_TZ = timedelta(hours=4)   # La Réunion : UTC+4, sans heure d'été


def _now_local():
    return datetime.utcnow() + _TZ


def _pfx(dt):
    return dt.strftime("%Y%m%d %H%M")          # préfixe de nom de fichier, ex. « 20260620 2045 »


def _batch_code(company_id, pfx, kind):
    """Identifiant de lot compact, ex. SLT07 (SL, Tickets, 7e lot)."""
    letter = "T" if kind == "tickets" else "F"
    with Session(_db_engine) as s:
        n = len(s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company_id,
            ImportBatch.establishment == pfx,
            ImportBatch.kind == ("toslt" if kind == "tickets" else "toslf"))).all())
    return f"{pfx}{letter}{n + 1:02d}"


def _cadrage_issues(ctx, ca_ttc, payments, syn, date_from, date_to):
    """Compare le calcul tickets à la synthèse. Renvoie la liste des écarts (vide = cadré)."""
    issues = []
    if syn.get("ca_total") is None:
        return ["synthèse illisible (CA Total introuvable)"]
    p = syn.get("period")
    if p and (p["date_from"] != date_from or p["date_to"] != date_to):
        issues.append(f"période synthèse {p['date_from']}→{p['date_to']} ≠ demandée")
    diff = round((ca_ttc or 0) - syn["ca_total"], 2)
    ctx.log(f"Cadrage CA : tickets {ca_ttc:.2f} vs synthèse {syn['ca_total']:.2f} → écart {diff:+.2f}")
    if abs(diff) >= 0.05:
        issues.append(f"écart CA {diff:+.2f} €")
    for mode, synv in (syn.get("payments") or {}).items():
        ourv = (payments or {}).get(mode, 0.0)
        d = round(ourv - synv, 2)
        flag = "" if abs(d) < 0.05 else "  ⚠️"
        ctx.log(f"  {mode} : tickets {ourv:.2f} vs synthèse {synv:.2f} → {d:+.2f}{flag}")
        if abs(d) >= 0.05:
            issues.append(f"écart {mode} {d:+.2f} €")
    return issues


def run_generate_toslt(ctx, company_code, establishment, date_from, date_to,
                       synthese_bytes, synthese_name="synthese.pdf"):
    from datetime import date as _date
    pfx = config.ESTABLISHMENTS[establishment]["pfx"]
    with Session(_db_engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
    if not company:
        raise RuntimeError(f"société {company_code} introuvable")

    code = _batch_code(company.id, pfx, "tickets")
    now = _now_local()
    fpfx = _pfx(now)                                  # ex. « 20260620 2045 »
    executed_at = now.strftime("%d/%m/%Y %H:%M")
    ctx.log(f"Lot {code} · {establishment} · {date_from} → {date_to}")
    # on garde la synthèse d'entrée (re-téléchargeable depuis la tâche)
    ctx.add_artifact("input", f"{fpfx} {synthese_name}", synthese_bytes, "application/pdf")

    # 1) Synthèse (son « nombre de clients » sert de total approx. pour la barre)
    ctx.progress(0, None, step="lecture du journal de synthèse…")
    syn = synthese.parse(synthese_bytes)
    total = int(syn["nb_clients"]) if syn.get("nb_clients") else None
    ctx.log(f"Synthèse : CA Total {syn.get('ca_total')} € | ~{total or '?'} clients | période {syn.get('period')}")

    def _prog(n, step):
        cur = min(n, total - 1) if total else n
        ctx.progress(cur, total, step=step)

    # 2) Construction TOSLT (un seul pull ; renvoie CA + paiements même si pré-vol bloque)
    ctx.progress(0, total, step="calcul TOSLT depuis les tickets…")
    res = csvgen.build_toslt(establishment, date_from, date_to, company.id, code,
                             company_code, on_progress=_prog)
    ctx.log(f"Tickets : CA TTC {res['ca_ttc']:.2f} € ({res['n_tickets']} tickets)")

    # 3) VERROU cadrage : on ne génère QUE si ça cadre avec la synthèse
    ctx.progress(total or res["n_tickets"], total or res["n_tickets"], step="cadrage…")
    def _emit_report(csv_agg=None, batch_code=None, balanced=None):
        args = dict(batch_code=batch_code, n_tickets=res["n_tickets"], balanced=balanced,
                    run_id=ctx.run_id, executed_at=executed_at)
        ctx.set_report(report.build("generate", establishment, date_from, date_to, syn, res, csv=csv_agg, **args))
        ctx.add_artifact("report",
                         f"{fpfx} compte_rendu_TOSLT_{code}_{date_from}_{date_to}.pdf",
                         report.build_pdf("generate", establishment, date_from, date_to, syn, res, csv=csv_agg, **args),
                         "application/pdf")

    issues = _cadrage_issues(ctx, res["ca_ttc"], res.get("payments"), syn, date_from, date_to)
    if issues:
        ctx.log("❌ ÉCARTS — CSV NON généré : " + " ; ".join(issues))
        _emit_report()
        return "Écart (CSV non généré) : " + " ; ".join(issues[:3])
    ctx.log("✅ Cadrage parfait.")

    # 4) Pré-vol client : chaque créance doit router vers un compte 411
    if res.get("unresolved"):
        ctx.log(f"⛔ Pré-vol : {len(res['unresolved'])} créance(s) sans compte client — "
                "génération refusée. Corrige dans TopOrder/Pennylane puis resynchronise les clients.")
        for u in res["unresolved"][:15]:
            ctx.log(f"   • {u['date']} · facture {u['facture'] or '?'} · "
                    f"companyId {u['company_id']} · {u['amount']:.2f} €")
        _emit_report()
        return f"Cadré ✓ mais bloqué : {len(res['unresolved'])} créance(s) sans compte client à corriger"

    # 5) Écriture du CSV (sur disque pour debug local + EN BASE comme artefact durable)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{fpfx} import_TOSLT_{code}_{date_from}_{date_to}.csv"
    path = EXPORT_DIR / fname
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(res["header"])
    w.writerows(res["rows"])
    csv_text = "﻿" + buf.getvalue()  # BOM pour Pennylane
    path.write_text(csv_text, encoding="utf-8")
    ctx.add_artifact("csv", fname, csv_text.encode("utf-8"), "text/csv")

    with Session(_db_engine) as s:
        s.add(ImportBatch(
            company_id=company.id, run_id=ctx.run_id, establishment=pfx, code=code, kind="toslt",
            date_from=_date.fromisoformat(date_from), date_to=_date.fromisoformat(date_to),
            status="generated", n_entries=res["n_agg"] + res["n_creances"],
            amount=res["ca_ttc"], csv_path=str(path)))
        s.commit()

    csv_agg = report.aggregate_rows(res["rows"], config.resolve(company_code))
    _emit_report(csv_agg=csv_agg, batch_code=code, balanced=res["balanced"])

    bal = "équilibré ✓" if res["balanced"] else f"⚠️ DÉSÉQUILIBRE (D {res['debit']} ≠ C {res['credit']})"
    ctx.log(f"CSV généré : {len(res['rows'])} lignes · {res['n_agg']} écritures jour "
            f"+ {res['n_creances']} créances · {bal}")
    ctx.log(f"CA TTC {res['ca_ttc']:.2f} € (HT {res['ca_ht']:.2f} + TVA {res['tva']:.2f}) · "
            f"encaissé {res['encaisse']:.2f} · créances 411 {res['creances']:.2f} · écart {res['ecart']:.2f}")
    return f"Cadré ✓ — lot {code} généré — CA TTC {res['ca_ttc']:.2f} € · {res['n_creances']} créances · {bal}"


def run_cadrage(ctx, establishment, date_from, date_to, synthese_bytes,
                synthese_name="synthese.pdf"):
    now = _now_local()
    fpfx = _pfx(now)
    executed_at = now.strftime("%d/%m/%Y %H:%M")
    ctx.log(f"Établissement : {establishment} | période {date_from} → {date_to}")
    ctx.add_artifact("input", f"{fpfx} {synthese_name}", synthese_bytes, "application/pdf")

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
    issues = _cadrage_issues(ctx, ca["ca_ttc"], ca["payments"], syn, date_from, date_to)
    ctx.set_report(report.build("cadrage", establishment, date_from, date_to,
                                syn, ca, csv=None, n_tickets=ca["n_tickets"],
                                run_id=ctx.run_id, executed_at=executed_at))
    ctx.add_artifact("report", f"{fpfx} compte_rendu_cadrage_{date_from}_{date_to}.pdf",
                     report.build_pdf("cadrage", establishment, date_from, date_to,
                                      syn, ca, csv=None, n_tickets=ca["n_tickets"],
                                      run_id=ctx.run_id, executed_at=executed_at),
                     "application/pdf")

    if not issues:
        ctx.log("✅ CADRAGE PARFAIT — l'import pourra être généré.")
        return f"Cadré ✓ — CA TTC {ca['ca_ttc']:.2f} € = synthèse ({ca['n_tickets']} tickets)"
    ctx.log("❌ ÉCARTS détectés : " + " ; ".join(issues))
    return "Écart : " + " ; ".join(issues[:3])
