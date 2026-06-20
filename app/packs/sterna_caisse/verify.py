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


def _expected_by_account(company_id, pfx, start, end):
    """Agrège l'attendu (net crédit-débit par compte) depuis les CSV stockés du mois.
    Dédoublonne les lots de période identique (re-génération) en gardant le plus récent."""
    with Session(engine) as s:
        batches = [b for b in s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company_id, ImportBatch.kind == "toslt",
            ImportBatch.establishment == pfx)).all()
            if not (b.date_to < start or b.date_from > end)]
        # dédoublonnage : 1 lot par (date_from, date_to), le plus récent
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
                deb = float(row[7] or 0)
                cred = float(row[8] or 0)
                net[acc] += cred - deb
    return {a: round(v, 2) for a, v in net.items()}, used, missing


def _actual_by_account(pl, journal_id, start, end):
    entries = pl.ledger_entries(journal_id, start.isoformat(), end.isoformat())
    net = defaultdict(float)
    for e in entries:
        for l in pl.entry_lines(e["id"]):
            acc = (l.get("ledger_account") or {}).get("number")
            if not acc:
                continue
            net[acc] += float(l.get("credit") or 0) - float(l.get("debit") or 0)
    return {a: round(v, 2) for a, v in net.items()}, len(entries)


def run_verify(ctx, company_code, pfx):
    establishment = next((n for n, e in config.ESTABLISHMENTS.items() if e["pfx"] == pfx), pfx)
    journal_id = config.JOURNALS[pfx]["tickets"]
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
    ctx.log(f"Vérification Pennylane · {establishment} · {label} (journal {config.journal_code(pfx,'tickets')})")
    ctx.progress(0, 3, step="lecture de l'attendu (CSV générés)…")
    expected, used, missing = _expected_by_account(company.id, pfx, start, end)
    if missing:
        ctx.log(f"⚠️ lots sans CSV stocké (générés avant la conservation) ignorés : {', '.join(missing)}")
    if not expected:
        ctx.log("Aucun CSV stocké → rien à vérifier.")
        return "Rien à vérifier — aucun CSV stocké"
    ctx.log(f"Attendu : {len(expected)} comptes (lots {', '.join(used)})")

    ctx.progress(1, 3, step="lecture des écritures Pennylane…")
    actual, n_entries = _actual_by_account(pl, journal_id, start, end)
    ctx.log(f"Pennylane : {n_entries} écritures, {len(actual)} comptes")

    ctx.progress(2, 3, step="rapprochement…")
    accounts = sorted(set(expected) | set(actual))
    diffs = []
    lines = [f"VÉRIFICATION PENNYLANE — {establishment} — {label}",
             f"Journal {config.journal_code(pfx,'tickets')} · {n_entries} écritures Pennylane · lots {', '.join(used) or '—'}",
             "", f"  {'Compte':<12}{'Attendu (CSV)':>16}{'Pennylane':>16}    État", ""]
    for acc in accounts:
        ev = expected.get(acc, 0.0)
        av = actual.get(acc, 0.0)
        ok = abs(ev - av) < TOL
        if not ok:
            diffs.append((acc, ev, av))
        lines.append(f"  {acc:<12}{ev:>16.2f}{av:>16.2f}    {'OK' if ok else '⚠️ ÉCART'}")
    coherent = not diffs
    lines += ["", ("✅ COHÉRENT — Pennylane correspond exactement aux lots générés."
                   if coherent else f"❌ {len(diffs)} écart(s) : {', '.join(d[0] for d in diffs)}")]
    ctx.set_report("\n".join(lines))

    now = datetime.utcnow() + _TZ
    _record(company.id, pfx, coherent, end, now, ctx.run_id)
    stamp = now.strftime("%d/%m/%Y %H:%M")
    if coherent:
        ctx.log(f"✅ Cohérent — vérifié le {stamp}")
        return f"✅ Cohérent ({label}) — vérifié le {stamp}"
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
