"""Étape 6 — Lettrage des comptes 411 (niveau SOCIÉTÉ, date d'arrêté).

Les comptes clients 411 sont partagés par les 3 boulangeries (même société
Pennylane). On lit, PAR COMPTE et tous journaux confondus (donc on capte aussi
les VIREMENTS du journal de banque), toutes les lignes ouvertes (non lettrées)
jusqu'à la date d'arrêté, on les regroupe par facture (réf. F<n°>) et on lettre :
  - groupe soldé (créance = règlements)        -> lettrage complet (strategy none)
  - groupe partiellement payé (acompte)        -> lettrage partiel (strategy partial)
  - virement (crédit sans réf.) = 1 créance     -> rapprochement certain par montant
  - ambigu / impayé                             -> laissé ouvert, listé dans le rapport
Idempotent : une ligne déjà lettrée (lettered_ledger_entry_lines.ids non vide) est
ignorée -> on RELIT tout l'ouvert à chaque passe (un virement arrivé après coup est
capté à la passe suivante, jamais d'hypothèse sur l'absence passée).
"""
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import pennylane
from app.models import Company, StepDeclaration
from . import config

TOL = 0.01
_TZ = timedelta(hours=4)   # La Réunion
_FAC = re.compile(r"(?:Facture|Avoir)\s+0*(\d+)")
_FREF = re.compile(r"\bF0*(\d+)\b")


def _fac_from_label(lbl):
    """N° de facture depuis le libellé d'une ligne 411 (créance « Facture 000043 »,
    règlement « Règlement CB F0000043 »). None si crédit sans référence (= virement)."""
    lbl = str(lbl or "")
    m = _FAC.search(lbl) or _FREF.search(lbl)
    return int(m.group(1)) if m else None


def _client_accounts(company_code):
    """Comptes clients 411 à lettrer (numéro -> nom lisible) : particuliers communs
    par établissement + comptes auxiliaires B2B (source = mapping client)."""
    cfg = config.resolve(company_code)
    out = {}
    for pfx, e in cfg["est"].items():
        if e.get("b2c_commun"):
            out[e["b2c_commun"]] = f"Particuliers {pfx}"
    for v in config.CLIENTS.get("b2b", {}).values():
        if v.get("account"):
            out[v["account"]] = v.get("name") or "client B2B"
    return out


def run_lettrage(ctx, company_code, as_of):
    """as_of : date d'arrêté (datetime.date). Lettre tout l'ouvert jusqu'à cette date."""
    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError("clé Pennylane absente")
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
        if not company:
            raise RuntimeError(f"société {company_code} introuvable")

    accounts = _client_accounts(company_code)
    asof_iso = as_of.isoformat()
    label = f"arrêté au {as_of.strftime('%d/%m/%Y')}"
    ctx.log(f"Lettrage des comptes 411 · {company.name} · {label} · {len(accounts)} comptes clients")

    full, partial, vir_ok = [], [], []
    ambiguous, open_creances, orphans, errors = [], [], [], []
    n_acc = 0
    items = list(accounts.items())
    for idx, (num, nm) in enumerate(items):
        ctx.progress(idx, len(items), step=f"compte {num} ({idx + 1}/{len(items)})…")
        acc = None
        try:
            acc = pl.find_account(num)
        except Exception as e:
            errors.append({"acc": num, "nm": nm, "why": f"compte: {str(e)[:50]}"})
            continue
        if not acc:
            continue                          # compte non créé côté Pennylane -> rien à lettrer
        n_acc += 1
        if not acc.get("letterable", True):
            continue
        lines = pl.account_lines(acc["id"], asof_iso)
        opens = [l for l in lines if not ((l.get("lettered_ledger_entry_lines") or {}).get("ids"))]
        # regroupe par facture ; crédits sans réf = virements
        groups = defaultdict(lambda: {"deb": [], "cred": [], "ds": 0.0, "cs": 0.0})
        virements = []
        for l in opens:
            deb = float(l.get("debit") or 0)
            cred = float(l.get("credit") or 0)
            fnum = _fac_from_label(l.get("label"))
            if fnum is None and cred > TOL:
                virements.append({"id": l["id"], "amount": round(cred, 2),
                                  "date": l.get("date"), "lbl": l.get("label")})
                continue
            if fnum is None:
                continue                      # débit sans réf (rare) : on n'y touche pas
            g = groups[fnum]
            (g["deb"] if deb > cred else g["cred"]).append(l["id"])
            g["ds"] += deb
            g["cs"] += cred

        remaining_creance = []   # créances encore ouvertes après lettrage par facture (pour virements)
        for fnum, g in groups.items():
            ids = g["deb"] + g["cred"]
            bal = round(g["ds"] - g["cs"], 2)
            paid = g["cs"] > TOL
            if not paid:
                # créance pure non payée -> on laisse ouvert (et candidate aux virements)
                if g["ds"] > TOL:
                    age = _age(g, as_of, opens)
                    open_creances.append({"acc": num, "nm": nm, "fnum": fnum,
                                          "amount": round(g["ds"], 2), "age": age, "ids": g["deb"]})
                    remaining_creance.append({"fnum": fnum, "amount": round(g["ds"], 2), "ids": g["deb"]})
                else:
                    orphans.append({"acc": num, "nm": nm, "fnum": fnum, "why": "crédit sans créance ouverte"})
                continue
            if len(ids) < 2:
                orphans.append({"acc": num, "nm": nm, "fnum": fnum, "why": "une seule ligne ouverte"})
                continue
            try:
                if abs(bal) < TOL:
                    pl.letter_lines(ids, "none"); full.append({"acc": num, "nm": nm, "fnum": fnum,
                                                               "amount": round(g["cs"], 2)})
                elif 0 < bal:
                    pl.letter_lines(ids, "partial"); partial.append({"acc": num, "nm": nm, "fnum": fnum,
                                                                    "paid": round(g["cs"], 2), "due": bal})
                else:
                    ambiguous.append({"acc": num, "nm": nm, "fnum": fnum,
                                      "why": f"trop-perçu {-bal:.2f}"})
            except Exception as e:
                errors.append({"acc": num, "nm": nm, "why": f"F{fnum}: {str(e)[:50]}"})
            time.sleep(0.05)

        # virements : rapprochement certain (1 créance ouverte de même montant)
        for v in virements:
            cands = [c for c in remaining_creance if abs(c["amount"] - v["amount"]) < TOL]
            if len(cands) == 1:
                c = cands[0]
                try:
                    pl.letter_lines([v["id"]] + c["ids"], "none")
                    vir_ok.append({"acc": num, "nm": nm, "fnum": c["fnum"],
                                   "amount": v["amount"], "date": v["date"]})
                    remaining_creance.remove(c)
                except Exception as e:
                    errors.append({"acc": num, "nm": nm, "why": f"virement {v['amount']}: {str(e)[:40]}"})
                time.sleep(0.05)
            else:
                ambiguous.append({"acc": num, "nm": nm, "fnum": None, "amount": v["amount"],
                                  "date": v["date"], "why": ("aucune créance du même montant" if not cands
                                                             else f"{len(cands)} créances possibles")})

    now = datetime.utcnow() + _TZ
    stamp = now.strftime("%d/%m/%Y %H:%M")
    counts = {"accounts": n_acc, "full": len(full), "partial": len(partial),
              "vir": len(vir_ok), "ambiguous": len(ambiguous),
              "open": len(open_creances), "errors": len(errors)}
    coherent = not errors and not ambiguous
    ctx.log(f"{len(full)} factures soldées lettrées · {len(partial)} partielles · "
            f"{len(vir_ok)} virements rapprochés · {len(ambiguous)} ambigus · "
            f"{len(open_creances)} créances ouvertes")

    # ---- compte rendu texte ----
    L = [f"LETTRAGE DES COMPTES 411 — {company.name} — {label}",
         f"vérifié le {stamp} · tâche #{ctx.run_id} · {n_acc} comptes clients", "",
         "== SYNTHÈSE ==",
         f"  Factures soldées lettrées      : {len(full)}",
         f"  Lettrages partiels (acomptes)  : {len(partial)}",
         f"  Virements rapprochés (certains): {len(vir_ok)}",
         f"  Ambigus (à traiter à la main)  : {len(ambiguous)}",
         f"  Créances ouvertes (impayées)   : {len(open_creances)}",
         f"  Erreurs API                    : {len(errors)}", ""]
    if partial:
        L += ["== LETTRAGES PARTIELS (reste dû) =="]
        for p in partial:
            L.append(f"  F{p['fnum']:<8} {p['nm'][:28]:<30} payé {p['paid']:>10.2f}  reste {p['due']:>10.2f}")
        L.append("")
    if vir_ok:
        L += ["== VIREMENTS RAPPROCHÉS =="]
        for v in vir_ok:
            L.append(f"  {v['date']}  F{v['fnum']:<8} {v['nm'][:28]:<30} {v['amount']:>10.2f}")
        L.append("")
    if ambiguous:
        L += ["== À TRAITER MANUELLEMENT (ambigus) =="]
        for a in ambiguous:
            ref = f"F{a['fnum']}" if a.get("fnum") else f"virement {a.get('amount', 0):.2f}"
            L.append(f"  {a['nm'][:28]:<30} {ref:<16} {a['why']}")
        L.append("")
    if open_creances:
        L += ["== CRÉANCES OUVERTES (impayées) =="]
        for c in sorted(open_creances, key=lambda x: -x["age"]):
            L.append(f"  F{c['fnum']:<8} {c['nm'][:28]:<30} {c['amount']:>10.2f}  ({c['age']} j)")
        L.append("")
    L += [("✅ COMPLET — toutes les factures soldables ont été lettrées."
           if coherent else
           f"⚠️ {len(ambiguous)} point(s) à traiter manuellement, {len(errors)} erreur(s).")]
    ctx.set_report("\n".join(L))

    from . import report
    ctx.add_artifact("report", f"{now.strftime('%Y%m%d %H%M')} compte_rendu_lettrage.pdf",
                     report.lettrage_pdf(company.name, label, counts, full, partial, vir_ok,
                                         ambiguous, open_creances, errors, coherent,
                                         run_id=ctx.run_id, executed_at=stamp),
                     "application/pdf")
    _record(company.id, coherent, as_of, now, ctx.run_id)

    head = (f"{len(full)} soldée(s), {len(partial)} partielle(s), {len(vir_ok)} virement(s)")
    if coherent:
        ctx.log(f"✅ Lettrage OK — {head} — {stamp}")
        return f"✅ Lettrage ({label}) — {head} — {stamp}"
    ctx.log(f"⚠️ {len(ambiguous)} ambigu(s)/{len(errors)} erreur(s) — {stamp}")
    return f"⚠️ Lettrage ({label}) — {head} ; {len(ambiguous)} à traiter — {stamp}"


def _age(g, as_of, opens):
    """Ancienneté (jours) de la créance la plus ancienne du groupe vs date d'arrêté."""
    dates = []
    idset = set(g["deb"])
    for l in opens:
        if l["id"] in idset and l.get("date"):
            try:
                dates.append(datetime.strptime(l["date"], "%Y-%m-%d").date())
            except ValueError:
                pass
    if not dates:
        return 0
    return (as_of - min(dates)).days


def _record(company_id, ok, covered_to, when, run_id):
    """Suivi : déclaration au niveau société (establishment = '' )."""
    with Session(engine) as s:
        d = s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company_id, StepDeclaration.establishment == "",
            StepDeclaration.step == "lettrage")).first()
        if not d:
            d = StepDeclaration(company_id=company_id, establishment="", step="lettrage")
        d.verified_at = when
        d.verify_ok = ok
        d.verify_run_id = run_id
        d.covered_to = covered_to
        d.state = "verified" if ok else "declared"
        d.updated_at = datetime.utcnow()
        s.add(d)
        s.commit()
