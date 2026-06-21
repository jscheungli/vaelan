"""Vérification Pennylane : un mois donné est-il TOUJOURS cohérent côté Pennylane ?

À tout moment, on relit les écritures TOSLT du mois dans Pennylane, on les agrège
PAR COMPTE, et on compare à l'ATTENDU (les CSV générés et stockés pour ce mois).
Détecte toute suppression / modification / ajout survenu depuis l'import. On
horodate la dernière vérification (on sait que « c'était bon à cette date »).
"""
import csv as _csv
import io
import time
from collections import defaultdict
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import pennylane
from app.models import Company, ImportBatch, JobArtifact, StepDeclaration
from . import config

TOL = 0.01
_TZ = timedelta(hours=4)   # La Réunion


def _expected_by_account(company_id, pfx, start, end):
    """Agrège l'ATTENDU depuis les CSV générés et stockés : net crédit-débit par compte
    (= ce qui DOIT se trouver dans Pennylane). Dédoublonne les lots de période identique
    (re-génération) en gardant le plus récent. Seul le journal TOSLT du CSV est lu pour
    le CA (le 70101 du TOSLF reclasse et gonflerait le total) — mais TOUS les comptes
    d'encaissement / TVA / 411 le sont."""
    with Session(engine) as s:
        batches = [b for b in s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company_id, ImportBatch.kind == "toslt",
            ImportBatch.establishment == pfx)).all()
            if not (b.date_to < start or b.date_from > end)]
        keep = {}
        for b in batches:
            k = (b.date_from, b.date_to)
            if k not in keep or b.id > keep[k].id:
                keep[k] = b
        net = defaultdict(float)
        used, missing = [], []
        for b in keep.values():
            art = s.exec(select(JobArtifact).where(
                JobArtifact.run_id == b.run_id, JobArtifact.kind == "csv")).first() if b.run_id else None
            if not art:
                missing.append(b.code)
                continue
            used.append(b.code)
            text = art.data.decode("utf-8-sig")
            for row in _csv.reader(io.StringIO(text)):
                if not row or row[0] == "Date":
                    continue
                d = row[0]
                if not (start.isoformat() <= d <= end.isoformat()):
                    continue
                acc = row[2]
                net[acc] += float(row[8] or 0) - float(row[7] or 0)   # crédit − débit
    return {a: round(v, 2) for a, v in net.items()}, used, missing


def _aggregates(net, cfg, pfx):
    """Reconstitue les totaux par poste depuis le net par compte (crédit-débit) :
    CA HT/TVA/TTC, TVA PAR TAUX (un compte = un taux), HT PAR TAUX (déduit de la TVA),
    encaissements PAR MODE, et solde des comptes clients 411."""
    ca_accs = {cfg["ca_anonyme"], cfg["ca_b2c"], cfg["ca_b2b"]}
    tva_accs = set(cfg["tva"].values())
    ecfg = cfg["est"][pfx]
    pay_lbl = {ecfg["cb"]: "CB", ecfg["especes"]: "Espèce",
               ecfg["ticket_resto"]: "Ticket restaurant", ecfg["autres"]: "Autres"}
    ecart = ecfg["ecart"]
    ca_ht = round(sum(net.get(a, 0.0) for a in ca_accs), 2)
    tva = round(sum(net.get(a, 0.0) for a in tva_accs), 2)
    # TVA par taux : net du compte de TVA de ce taux
    tva_by_rate = {r: round(net.get(a, 0.0), 2) for r, a in cfg["tva"].items()}
    # HT par taux : déduit de la TVA (HT = TVA / (taux/100)) ; le 0% = résidu du CA HT
    ht_by_rate = {}
    for r, t in tva_by_rate.items():
        if abs(t) > TOL and float(r) > 0:
            ht_by_rate[r] = round(t / (float(r) / 100.0), 2)
    ht0 = round(ca_ht - sum(ht_by_rate.values()), 2)
    if abs(ht0) > TOL:
        ht_by_rate["0"] = ht0
    payments = {m: round(-sum(net.get(a, 0.0) for a in pay_lbl if pay_lbl[a] == m), 2)
                for m in ("CB", "Espèce", "Ticket restaurant", "Autres")}
    ecart_caisse = round(-net.get(ecart, 0.0), 2)
    known = ca_accs | tva_accs | set(pay_lbl) | {ecart}
    clients_411 = round(sum(v for a, v in net.items() if a not in known), 2)
    return {"ca_ht": ca_ht, "tva": tva, "ca_ttc": round(ca_ht + tva, 2),
            "tva_by_rate": tva_by_rate, "ht_by_rate": ht_by_rate,
            "payments": payments, "ecart_caisse": ecart_caisse, "clients_411": clients_411}


def _actual_by_account(pl, journal_ids, start, end):
    """Agrège le net par compte sur PLUSIEURS journaux (TOSLT + TOSLF du CSV unique)."""
    net = defaultdict(float)
    n = 0
    for jid in journal_ids:
        entries = pl.ledger_entries(jid, start.isoformat(), end.isoformat())
        n += len(entries)
        for e in entries:
            time.sleep(0.06)        # rythme gentil pour éviter le 429 (en plus du retry)
            for l in pl.entry_lines(e["id"]):
                acc = (l.get("ledger_account") or {}).get("number")
                if not acc:
                    continue
                net[acc] += float(l.get("credit") or 0) - float(l.get("debit") or 0)
    return {a: round(v, 2) for a, v in net.items()}, n


def run_verify(ctx, company_code, pfx):
    establishment = next((n for n, e in config.ESTABLISHMENTS.items() if e["pfx"] == pfx), pfx)
    cfg = config.resolve(company_code)
    ecfg = cfg["est"][pfx]
    journal_ids = [int(ecfg["journal_tickets_id"]), int(ecfg["journal_factures_id"])]
    journal_label = f"{ecfg['journal_tickets']} + {ecfg['journal_factures']}"
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
        if not company:
            raise RuntimeError(f"société {company_code} introuvable")
        batches = s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company.id, ImportBatch.kind == "toslt",
            ImportBatch.establishment == pfx)).all()
    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError("clé Pennylane absente")
    if not batches:
        ctx.log("Aucun lot caisse généré pour cet établissement → rien à vérifier.")
        return "Rien à vérifier — aucun lot caisse"

    start = min(b.date_from for b in batches)
    end = max(b.date_to for b in batches)
    label = f"{start.strftime('%d/%m/%Y')} → {end.strftime('%d/%m/%Y')}"
    ctx.log(f"Vérification Pennylane · {establishment} · {label} (journaux {journal_label})")
    ctx.progress(0, 3, step="lecture de l'attendu (CSV générés)…")
    expected, used, missing = _expected_by_account(company.id, pfx, start, end)
    if missing:
        ctx.log(f"⚠️ lots sans CSV stocké ignorés : {', '.join(missing)}")
    if not expected:
        ctx.log("Aucun CSV stocké → rien à vérifier.")
        return "Rien à vérifier — aucun CSV stocké"
    ctx.log(f"Attendu : {len(expected)} comptes (lots {', '.join(used)})")

    ctx.progress(1, 3, step="lecture des écritures Pennylane…")
    actual, n_entries = _actual_by_account(pl, journal_ids, start, end)
    ctx.log(f"Pennylane : {n_entries} écritures, {len(actual)} comptes")

    ctx.progress(2, 3, step="rapprochement (tous les contrôles)…")
    exp_agg = _aggregates(expected, cfg, pfx)
    act_agg = _aggregates(actual, cfg, pfx)
    accounts = sorted(set(expected) | set(actual))
    acc_rows = [(a, expected.get(a, 0.0), actual.get(a, 0.0),
                 abs(expected.get(a, 0.0) - actual.get(a, 0.0)) < TOL) for a in accounts]
    diffs = [r for r in acc_rows if not r[3]]
    coherent = not diffs

    def chk(e, a):
        return abs((e or 0) - (a or 0)) < TOL

    def rows_from(keys, getter):
        out = []
        for k, lbl in keys:
            e = getter(exp_agg, k)
            a = getter(act_agg, k)
            if abs(e or 0) > TOL or abs(a or 0) > TOL:
                out.append((lbl, round(e or 0, 2), round(a or 0, 2), chk(e, a)))
        return out

    # MÊMES CONTRÔLES qu'à la génération, mais Attendu (CSV généré) vs Pennylane (API) :
    sections = []
    sections.append({"name": "Chiffre d'affaires & comptes clients", "rows": [
        ("CA HT total", exp_agg["ca_ht"], act_agg["ca_ht"], chk(exp_agg["ca_ht"], act_agg["ca_ht"])),
        ("TVA total", exp_agg["tva"], act_agg["tva"], chk(exp_agg["tva"], act_agg["tva"])),
        ("CA TTC total", exp_agg["ca_ttc"], act_agg["ca_ttc"], chk(exp_agg["ca_ttc"], act_agg["ca_ttc"])),
        ("Écart de caisse", exp_agg["ecart_caisse"], act_agg["ecart_caisse"], chk(exp_agg["ecart_caisse"], act_agg["ecart_caisse"])),
        ("Comptes clients 411 (solde)", exp_agg["clients_411"], act_agg["clients_411"], chk(exp_agg["clients_411"], act_agg["clients_411"])),
    ]})
    rates = sorted(set(list(exp_agg["tva_by_rate"]) + list(act_agg["tva_by_rate"])), key=float)
    tva_rows = rows_from([(r, f"TVA {r}%") for r in rates], lambda agg, r: agg["tva_by_rate"].get(r))
    if tva_rows:
        sections.append({"name": "TVA par taux", "rows": tva_rows})
    hrates = sorted(set(list(exp_agg["ht_by_rate"]) + list(act_agg["ht_by_rate"])), key=float)
    ht_rows = rows_from([(r, f"HT {r}%") for r in hrates], lambda agg, r: agg["ht_by_rate"].get(r))
    if ht_rows:
        sections.append({"name": "HT par taux (déduit de la TVA)", "rows": ht_rows})
    pay_rows = rows_from([(m, m) for m in ("CB", "Espèce", "Ticket restaurant", "Autres")],
                         lambda agg, m: agg["payments"].get(m))
    sections.append({"name": "Encaissements par mode", "rows": pay_rows})
    sections.append({"name": "Détail par compte",
                     "rows": [(str(a), ev, av, ok) for a, ev, av, ok in acc_rows]})

    # ---- rapport texte ----
    L = [f"VÉRIFICATION PENNYLANE — {establishment} — {label}",
         f"Journaux {journal_label} · {n_entries} écritures lues · lots {', '.join(used) or '—'}",
         "Attendu = CSV généré et importé · Pennylane = relu via l'API.", ""]
    for sec in sections:
        L.append(f"== {sec['name'].upper()} ==")
        L.append(f"  {'':<30}{'Attendu (CSV)':>15}{'Pennylane':>15}   État")
        for lbl, e, a, ok in sec["rows"]:
            L.append(f"  {lbl:<30}{e:>15.2f}{a:>15.2f}   {'OK' if ok else '⚠️ ÉCART'}")
        L.append("")
    L.append("✅ COHÉRENT — Pennylane correspond exactement aux lots générés (tous les contrôles OK)."
             if coherent else f"❌ {len(diffs)} écart(s) : {', '.join(d[0] for d in diffs)}")
    ctx.set_report("\n".join(L))

    now = datetime.utcnow() + _TZ
    stamp = now.strftime("%d/%m/%Y %H:%M")
    from . import report
    ctx.add_artifact("report", f"{now.strftime('%Y%m%d %H%M')} compte_rendu_verif_{pfx}.pdf",
                     report.verify_pdf(establishment, journal_label, label, n_entries, used,
                                       sections, coherent, run_id=ctx.run_id, executed_at=stamp),
                     "application/pdf")
    _record(company.id, pfx, coherent, end, now, ctx.run_id)
    if coherent:
        ctx.log(f"✅ Cohérent (tous les contrôles OK) — vérifié le {stamp}")
        return f"✅ Cohérent ({label}) — tous les contrôles OK — vérifié le {stamp}"
    ctx.log(f"❌ {len(diffs)} écart(s) — vérifié le {stamp}")
    return f"❌ Écart ({label}) sur {len(diffs)} compte(s) — vérifié le {stamp}"


def _record(company_id, pfx, ok, covered_to, when, run_id):
    with Session(engine) as s:
        d = s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company_id, StepDeclaration.establishment == pfx,
            StepDeclaration.step == "verify_tickets")).first()
        if not d:
            d = StepDeclaration(company_id=company_id, establishment=pfx, step="verify_tickets")
        d.verified_at = when
        d.verify_ok = ok
        d.verify_run_id = run_id
        d.covered_to = covered_to
        d.state = "verified" if ok else "declared"
        d.updated_at = datetime.utcnow()
        s.add(d)
        s.commit()
