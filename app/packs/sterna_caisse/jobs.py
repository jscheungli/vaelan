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


def _cadrage_issues(ctx, ca_ttc, payments, syn, date_from, date_to, fac_payments=None):
    """Compare le calcul Vaelan à la synthèse.

    Renvoie (blocking, warnings) :
      - blocking : période + CA Total (la vérité fiscale) -> empêchent la génération ;
      - warnings : écarts par MODE DE PAIEMENT (encaissements).
    Depuis le moteur 2 flux (encaissements = ticketpaymentdata par date de paiement),
    les encaissements par mode DOIVENT être identiques à la synthèse. Un écart n'est
    donc plus « réconcilié » : c'est une vraie anomalie (warning, affichée en rouge
    dans le compte rendu). Le CA Total reste le seul contrôle bloquant.
    """
    if syn.get("ca_total") is None:
        return ["synthèse illisible (CA Total introuvable)"], []
    blocking, warnings = [], []
    p = syn.get("period")
    if p and (p["date_from"] != date_from or p["date_to"] != date_to):
        blocking.append(f"période synthèse {p['date_from']}→{p['date_to']} ≠ demandée")
    diff = round((ca_ttc or 0) - syn["ca_total"], 2)
    ctx.log(f"Cadrage CA : Vaelan {ca_ttc:.2f} vs synthèse {syn['ca_total']:.2f} → écart {diff:+.2f}")
    if abs(diff) >= 0.05:
        blocking.append(f"écart CA {diff:+.2f} €")
    for mode, synv in (syn.get("payments") or {}).items():
        ourv = (payments or {}).get(mode, 0.0)
        d = round(ourv - synv, 2)
        if abs(d) < 0.05:
            ctx.log(f"  {mode} : encaissé {ourv:.2f} = synthèse {synv:.2f}")
        else:
            ctx.log(f"  {mode} : encaissé {ourv:.2f} vs synthèse {synv:.2f} → écart {d:+.2f} ⚠️")
            warnings.append(f"{mode} {d:+.2f} €")
    return blocking, warnings


def run_generate_toslt(ctx, company_code, establishment, date_from, date_to,
                       synthese_bytes, synthese_name="synthese.pdf"):
    from datetime import date as _date
    pfx = config.establishments(company_code)[establishment]["pfx"]
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
    ctx.log(f"Tickets : CA TTC {res['ca_ttc']:.2f} € ({res['n_tickets']} tickets) · "
            f"{res['n_factures']} factures (B2B {res['n_b2b']}/B2C {res['n_b2c']}) · "
            f"{res['n_reglements']} règlement(s) caisse")

    # 3) VERROU cadrage : on ne génère QUE si ça cadre avec la synthèse
    ctx.progress(total or res["n_tickets"], total or res["n_tickets"], step="cadrage…")
    def _emit_report(csv_agg=None, batch_code=None, balanced=None):
        args = dict(batch_code=batch_code, n_tickets=res["n_tickets"], balanced=balanced,
                    run_id=ctx.run_id, executed_at=executed_at, fac_payments=res.get("fac_payments"),
                    fac_detail=res.get("fac_payment_detail"))
        ctx.set_report(report.build("generate", establishment, date_from, date_to, syn, res, csv=csv_agg, **args))
        ctx.add_artifact("report",
                         f"{fpfx} compte_rendu_TOSLT_{code}_{date_from}_{date_to}.pdf",
                         report.build_pdf("generate", establishment, date_from, date_to, syn, res, csv=csv_agg, **args),
                         "application/pdf")

    blocking, warnings = _cadrage_issues(ctx, res["ca_ttc"], res.get("payments"), syn, date_from, date_to,
                                         fac_payments=res.get("fac_payments"))
    if blocking:
        ctx.log("❌ ÉCART CA — CSV NON généré : " + " ; ".join(blocking))
        _emit_report()
        return "Écart CA (CSV non généré) : " + " ; ".join(blocking[:3])
    if warnings:
        ctx.log("⚠️ Écart(s) de mode de paiement (non bloquant — voir le compte rendu) : "
                + " ; ".join(warnings))
    ctx.log("✅ Cadrage CA OK.")

    # 4) Pré-vol client : chaque créance doit router vers un compte 411
    if res.get("unresolved"):
        # regroupe par client pour un log lisible et CIBLÉ (nom + raison + factures)
        byc = {}
        for u in res["unresolved"]:
            g = byc.setdefault(u.get("company_id") or "?", {
                "name": u.get("name"), "reason": u.get("reason"), "siret": u.get("siret"),
                "company_id": u.get("company_id"), "factures": [], "total": 0.0})
            g["factures"].append(u.get("facture"))
            g["total"] += u.get("amount") or 0
        ctx.log(f"⛔ Pré-vol : {len(res['unresolved'])} créance(s) sur {len(byc)} client(s) sans compte 411 — "
                "génération refusée. Clients à corriger (voir le compte rendu) :")
        for g in sorted(byc.values(), key=lambda x: -x["total"]):
            ctx.log(f"   • {g['name'] or '(nom inconnu)'} · companyId {g['company_id'] or '—'} · "
                    f"SIRET {g['siret'] or '— (absent)'} · {g['total']:.2f} € · {len(g['factures'])} facture(s)")
            ctx.log(f"       ⚠ {g['reason'] or 'à vérifier'}")
        _emit_report()
        return (f"Cadré ✓ mais bloqué : {len(res['unresolved'])} créance(s) sur {len(byc)} client(s) "
                "sans compte 411 (voir compte rendu)")

    # 5) Écriture du CSV UNIQUE (caisse TOSLT + reclassement TOSLF dans un seul fichier ;
    #    Pennylane sépare par la colonne « Code journal »). Sur disque + EN BASE (durable).
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{fpfx} import_caisse_{code}_{date_from}_{date_to}.csv"
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
            status="generated", n_entries=res["n_agg"] + res["n_factures"] + res["n_reglements"],
            amount=res["ca_ttc"], csv_path=str(path)))
        s.commit()

    _rcfg = config.resolve(company_code)
    _jtoslt = _rcfg["est"][pfx]["journal_tickets"]      # CA : caisse seule (pas le reclass TOSLF)
    _jpay = _rcfg["est"][pfx].get("journal_payments")   # encaissements : caisse + règlements
    csv_agg = report.aggregate_rows(res["rows"], _rcfg, journal=_jtoslt, pay_journal=_jpay)
    _emit_report(csv_agg=csv_agg, batch_code=code, balanced=res["balanced"])

    bal = "équilibré ✓" if res["balanced"] else f"⚠️ DÉSÉQUILIBRE (D {res['debit']} ≠ C {res['credit']})"
    ctx.log(f"CSV unique : {len(res['rows'])} lignes · TOSLT ({res['n_agg']} jour + {res['n_factures']} "
            f"factures + {res['n_reglements']} règlements) + TOSLF ({res['n_toslf']} lignes reclass) · {bal}")
    ctx.log(f"CA TTC {res['ca_ttc']:.2f} € (HT {res['ca_ht']:.2f} + TVA {res['tva']:.2f}) · "
            f"encaissé {res['encaisse']:.2f} · 411 ouvert {res['creances']:.2f} · écart {res['ecart']:.2f}")
    warn = f" · ⚠ {len(warnings)} écart mode paiement (voir CR)" if warnings else ""
    return (f"Cadré ✓ — lot {code} généré (1 CSV : caisse + factures) — CA TTC {res['ca_ttc']:.2f} € · "
            f"{res['n_factures']} factures{warn}")


def run_generate_kk(ctx, company_code, establishment, date_from, date_to,
                    synthese_bytes=None, synthese_name="synthese.pdf"):
    """Génération KOOKABURA (modèle FACTURE B2B, pas de caisse) : pull des factures dont
    le ticket est dans la période -> TOKKT (CA + 411 créance) + TOKKF (reclass HT 70101->7012),
    dans un seul CSV. Pré-vol : boulangeries clientes mappées. KK n'encaisse pas en caisse
    (les 3 boulangeries paient par virement) mais TopOrder sort un journal de synthèse au même
    format -> on cadre le CA (HT/TVA/TTC) contre ce Z si la synthèse est fournie."""
    from datetime import date as _date
    pfx = config.establishments(company_code)[establishment]["pfx"]
    with Session(_db_engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
    if not company:
        raise RuntimeError(f"société {company_code} introuvable")
    code = _batch_code(company.id, pfx, "tickets")
    now = _now_local()
    fpfx = _pfx(now)
    executed_at = now.strftime("%d/%m/%Y %H:%M")
    ctx.log(f"Génération KOOKABURA · lot {code} · {establishment} · {date_from} → {date_to}")
    ctx.progress(0, None, step="lecture des factures KK…")
    res = csvgen.build_kk(establishment, date_from, date_to, company.id, code, company_code,
                          on_progress=lambda n, st: ctx.progress(n, None, step=st))
    ctx.log(f"Factures KK : {res['n_factures']} en période · CA TTC {res['ca_ttc']:.2f} € "
            f"(HT {res['ca_ht']:.2f} + TVA {res['tva']:.2f})")

    if synthese_bytes:
        ctx.add_artifact("input", f"{fpfx} {synthese_name or 'synthese.pdf'}", synthese_bytes, "application/pdf")
        syn = synthese.parse(synthese_bytes)
        syn.setdefault("payments", {})
        ctx.log(f"Synthèse KK : CA Total {syn.get('ca_total')} € · CA HT {syn.get('ca_ht')} € · "
                f"{int(syn['nb_clients']) if syn.get('nb_clients') else '?'} clients · "
                f"période {syn.get('period')}")
    else:
        ctx.log("⚠️ Aucune synthèse fournie — cadrage sur le CSV seul (CA non confronté au Z).")
        syn = {"ca_total": res["ca_ttc"], "ca_ht": res["ca_ht"], "payments": {},
               "period": {"date_from": date_from, "date_to": date_to}}

    def _emit(csv_agg=None, balanced=None):
        args = dict(batch_code=code, n_tickets=res["n_tickets"], balanced=balanced,
                    run_id=ctx.run_id, executed_at=executed_at)
        ctx.set_report(report.build("generate", establishment, date_from, date_to, syn, res, csv=csv_agg, **args))
        ctx.add_artifact("report", f"{fpfx} compte_rendu_KK_{code}_{date_from}_{date_to}.pdf",
                         report.build_pdf("generate", establishment, date_from, date_to, syn, res, csv=csv_agg, **args),
                         "application/pdf")

    # verrou cadrage : si une synthèse est fournie, on ne génère QUE si le CA cadre avec le Z
    if synthese_bytes:
        blocking, _ = _cadrage_issues(ctx, res["ca_ttc"], res.get("payments"), syn, date_from, date_to)
        if blocking:
            ctx.log("❌ ÉCART CA — CSV NON généré : " + " ; ".join(blocking))
            _emit()
            return "Écart CA (CSV non généré) : " + " ; ".join(blocking[:3])
        ctx.log("✅ Cadrage CA OK.")

    if res.get("unresolved"):
        ctx.log(f"⛔ {len(res['unresolved'])} facture(s) sans boulangerie cliente mappée :")
        for u in res["unresolved"][:15]:
            ctx.log(f"   • facture F{u['facture']} · companyId {u['company_id']} · {u['reason']}")
        _emit()
        return f"Bloqué : {len(res['unresolved'])} facture(s) sans boulangerie cliente mappée (voir compte rendu)"

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{fpfx} import_KK_{code}_{date_from}_{date_to}.csv"
    path = EXPORT_DIR / fname
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(res["header"])
    w.writerows(res["rows"])
    csv_text = "﻿" + buf.getvalue()
    path.write_text(csv_text, encoding="utf-8")
    ctx.add_artifact("csv", fname, csv_text.encode("utf-8"), "text/csv")
    with Session(_db_engine) as s:
        s.add(ImportBatch(
            company_id=company.id, run_id=ctx.run_id, establishment=pfx, code=code, kind="toslt",
            date_from=_date.fromisoformat(date_from), date_to=_date.fromisoformat(date_to),
            status="generated", n_entries=res["n_factures"], amount=res["ca_ttc"], csv_path=str(path)))
        s.commit()
    _rcfg = config.resolve(company_code)
    csv_agg = report.aggregate_rows(res["rows"], _rcfg, journal=_rcfg["est"][pfx]["journal_tickets"])
    _emit(csv_agg=csv_agg, balanced=res["balanced"])
    bal = "équilibré ✓" if res["balanced"] else f"⚠️ DÉSÉQUILIBRE (D {res['debit']} ≠ C {res['credit']})"
    ctx.log(f"CSV KK : {len(res['rows'])} lignes (TOKKT + TOKKF) · {bal} · CA TTC {res['ca_ttc']:.2f} € · "
            f"411 créances {res['creances']:.2f} €")
    return (f"Lot KK {code} généré — CA TTC {res['ca_ttc']:.2f} € · {res['n_factures']} factures "
            f"(70101 → 7012, créances 411 boulangeries)")


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
    blocking, warnings = _cadrage_issues(ctx, ca["ca_ttc"], ca["payments"], syn, date_from, date_to)
    ctx.set_report(report.build("cadrage", establishment, date_from, date_to,
                                syn, ca, csv=None, n_tickets=ca["n_tickets"],
                                run_id=ctx.run_id, executed_at=executed_at))
    ctx.add_artifact("report", f"{fpfx} compte_rendu_cadrage_{date_from}_{date_to}.pdf",
                     report.build_pdf("cadrage", establishment, date_from, date_to,
                                      syn, ca, csv=None, n_tickets=ca["n_tickets"],
                                      run_id=ctx.run_id, executed_at=executed_at),
                     "application/pdf")

    if not blocking:
        warn = f" (⚠ {len(warnings)} écart mode paiement)" if warnings else ""
        ctx.log("✅ CADRAGE CA OK — l'import pourra être généré." + warn)
        return f"Cadré ✓ — CA TTC {ca['ca_ttc']:.2f} € = synthèse ({ca['n_tickets']} tickets){warn}"
    ctx.log("❌ ÉCART CA : " + " ; ".join(blocking))
    return "Écart CA : " + " ; ".join(blocking[:3])
