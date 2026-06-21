"""Vérification Pennylane : un mois donné est-il TOUJOURS cohérent côté Pennylane ?

À tout moment, on relit les écritures TOSLT du mois dans Pennylane, on les agrège
PAR COMPTE, et on compare à l'ATTENDU (les CSV générés et stockés pour ce mois).
Détecte toute suppression / modification / ajout survenu depuis l'import. On
horodate la dernière vérification (on sait que « c'était bon à cette date »).
"""
import csv as _csv
import io
from collections import defaultdict
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import pennylane
from app.models import Company, ImportBatch, JobArtifact, StepDeclaration
from . import config

TOL = 0.01
_TZ = timedelta(hours=4)   # La Réunion


def _expected_by_account(company_id, pfx, start, end, pay_accs):
    """Agrège l'attendu depuis les CSV stockés : net crédit-débit par compte +
    paiements SUR FACTURES par mode (lignes d'encaissement des pièces -F/-R).
    Dédoublonne les lots de période identique (re-génération) en gardant le plus récent."""
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
        import re
        net = defaultdict(float)
        fac_pay = defaultdict(float)
        piece2client, fac_detail = {}, []
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
                acc, lib, piece = row[2], row[4], row[6]
                deb = float(row[7] or 0)
                cred = float(row[8] or 0)
                net[acc] += cred - deb
                if (lib.startswith("Facture ") or lib.startswith("Avoir ")) and "·" in lib:
                    piece2client[piece] = lib.split("·", 1)[1].strip()
                # paiement de facture : encaissement (débit) sur une pièce -F / -R
                if acc in pay_accs and ("-F" in piece or "-R" in piece):
                    amt = round(deb - cred, 2)
                    fac_pay[pay_accs[acc]] += amt
                    mfn = re.search(r"-[FR](\d+)", piece)
                    fac_detail.append({"date": d, "fnum": int(mfn.group(1)) if mfn else 0,
                                       "piece": piece, "mode": pay_accs[acc], "amount": amt})
        for x in fac_detail:
            x["nm"] = piece2client.get(x["piece"], "")
    fac_detail.sort(key=lambda x: (x["mode"], x["date"], x["fnum"]))
    return ({a: round(v, 2) for a, v in net.items()},
            {m: round(v, 2) for m, v in fac_pay.items()}, fac_detail, used, missing)


def _aggregates(net, cfg, pfx):
    """Reconstitue les totaux par poste depuis le net par compte (crédit-débit)."""
    ca_accs = {cfg["ca_anonyme"], cfg["ca_b2c"], cfg["ca_b2b"]}
    tva_accs = set(cfg["tva"].values())
    ecfg = cfg["est"][pfx]
    pay_lbl = {ecfg["cb"]: "CB", ecfg["especes"]: "Espèce",
               ecfg["ticket_resto"]: "Ticket restaurant", ecfg["autres"]: "Autres"}
    ecart = ecfg["ecart"]
    ca_ht = round(sum(net.get(a, 0.0) for a in ca_accs), 2)
    tva = round(sum(net.get(a, 0.0) for a in tva_accs), 2)
    payments = {m: round(-sum(net.get(a, 0.0) for a in pay_lbl if pay_lbl[a] == m), 2)
                for m in ("CB", "Espèce", "Ticket restaurant", "Autres")}
    known = ca_accs | tva_accs | set(pay_lbl) | {ecart}
    clients_411 = round(sum(v for a, v in net.items() if a not in known), 2)
    return {"ca_ht": ca_ht, "tva": tva, "ca_ttc": round(ca_ht + tva, 2),
            "payments": payments, "clients_411": clients_411}


def _actual_by_account(pl, journal_ids, start, end):
    """Agrège le net par compte sur PLUSIEURS journaux (TOSLT + TOSLF du CSV unique)."""
    net = defaultdict(float)
    n = 0
    for jid in journal_ids:
        entries = pl.ledger_entries(jid, start.isoformat(), end.isoformat())
        n += len(entries)
        for e in entries:
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
    pay_accs = {ecfg["cb"]: "CB", ecfg["especes"]: "Espèce",
                ecfg["ticket_resto"]: "Ticket restaurant", ecfg["autres"]: "Autres"}
    ctx.log(f"Vérification Pennylane · {establishment} · {label} (journaux {journal_label})")
    ctx.progress(0, 3, step="lecture de l'attendu (CSV générés)…")
    expected, fac_pay, fac_detail, used, missing = _expected_by_account(company.id, pfx, start, end, pay_accs)
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

    # contrôles structurés (Attendu CSV vs Pennylane)
    def chk(a, b):
        return abs((a or 0) - (b or 0)) < TOL
    summary = [("CA HT total", exp_agg["ca_ht"], act_agg["ca_ht"]),
               ("TVA total", exp_agg["tva"], act_agg["tva"]),
               ("CA TTC total", exp_agg["ca_ttc"], act_agg["ca_ttc"]),
               ("Comptes clients 411 (solde)", exp_agg["clients_411"], act_agg["clients_411"])]
    summary = [(lbl, e, a, chk(e, a)) for lbl, e, a in summary]
    pay_rows = [(m, exp_agg["payments"][m], act_agg["payments"][m], chk(exp_agg["payments"][m], act_agg["payments"][m]))
                for m in ("CB", "Espèce", "Ticket restaurant", "Autres")
                if abs(exp_agg["payments"][m]) > TOL or abs(act_agg["payments"][m]) > TOL]
    # réconciliation : comptes 411 conformes -> les paiements de factures (qui expliquent l'écart) sont intacts
    cli_ok = chk(exp_agg["clients_411"], act_agg["clients_411"])
    recon = [(m, v) for m, v in fac_pay.items() if abs(v) > TOL]

    # ---- rapport texte ----
    L = [f"VÉRIFICATION PENNYLANE — {establishment} — {label}",
         f"Journaux {journal_label} · {n_entries} écritures lues · lots {', '.join(used) or '—'}", "",
         "== CONTRÔLES (Attendu CSV vs Pennylane) =="]
    for lbl, e, a, ok in summary:
        L.append(f"  {lbl:<28}{e:>14.2f}{a:>14.2f}   {'OK' if ok else '⚠️ ÉCART'}")
    L += ["", "== PAIEMENTS PAR MODE (net encaissé) =="]
    for m, e, a, ok in pay_rows:
        L.append(f"  {m:<28}{e:>14.2f}{a:>14.2f}   {'OK' if ok else '⚠️ ÉCART'}")
    L += ["", "== RÉCONCILIATION DES PAIEMENTS DE FACTURES =="]
    L.append("  (paiements de factures encaissés en caisse, bookés au crédit du 411 — ils expliquent")
    L.append("   l'écart de mode de paiement vs la synthèse au moment de la génération)")
    for m, v in recon:
        L.append(f"  {m:<28}{v:>14.2f}   bookés au crédit du 411 (lettrés F<n°>)")
    L.append(f"  Contrôle : comptes clients 411 conformes entre CSV et Pennylane -> "
             f"{'✓ paiements de factures intacts' if cli_ok else '⚠️ écart sur les 411'}")
    if fac_detail:
        L.append("  Détail des paiements de factures :")
        L.append(f"    {'Date':<12}{'Facture':<10}{'Client':<24}{'Mode':<12}{'Montant':>12}")
        for x in fac_detail:
            L.append(f"    {x['date']:<12}{('F' + str(x['fnum'])):<10}{(x.get('nm') or '')[:22]:<24}"
                     f"{x['mode']:<12}{x['amount']:>12.2f}")
    L += ["", "== DÉTAIL PAR COMPTE ==", f"  {'Compte':<12}{'Attendu (CSV)':>16}{'Pennylane':>16}   État"]
    for a, ev, av, ok in acc_rows:
        L.append(f"  {a:<12}{ev:>16.2f}{av:>16.2f}   {'OK' if ok else '⚠️ ÉCART'}")
    L += ["", ("✅ COHÉRENT — Pennylane correspond exactement aux lots générés (tous les contrôles OK)."
               if coherent else f"❌ {len(diffs)} écart(s) : {', '.join(d[0] for d in diffs)}")]
    ctx.set_report("\n".join(L))

    now = datetime.utcnow() + _TZ
    stamp = now.strftime("%d/%m/%Y %H:%M")
    from . import report
    ctx.add_artifact("report", f"{now.strftime('%Y%m%d %H%M')} compte_rendu_verif_{pfx}.pdf",
                     report.verify_pdf(establishment, journal_label, label, n_entries, used,
                                       summary, pay_rows, recon, cli_ok, fac_detail, acc_rows, coherent,
                                       run_id=ctx.run_id, executed_at=stamp),
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
