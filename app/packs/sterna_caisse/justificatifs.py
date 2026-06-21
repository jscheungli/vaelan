"""Étape 5 — Attache des justificatifs (PDF factures) aux écritures Pennylane.

Les écritures sont créées par import CSV manuel (sans pièce jointe). Ici, Vaelan
relit les écritures « facture » (pièce contenant F<n°>) dans les journaux TOSLT
(créance caisse) + TOSLF (reclassement), télécharge le PDF officiel de chaque
facture depuis TopOrder (URL publique, le GUID fait jeton) et le RATTACHE à
l'écriture via l'API Pennylane (POST /file_attachments puis PUT /ledger_entries).
À la fin, Vaelan VÉRIFIE que toute écriture facture porte bien sa pièce, et produit
un compte rendu détaillé (combien d'écritures concernées, déjà attachées, attachées
maintenant, en échec).
"""
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import pennylane, toporder
from app.models import Company, ImportBatch, StepDeclaration
from . import config

_TZ = timedelta(hours=4)   # La Réunion
PDF_URL = "https://pdf.toporder.fr/pdf?InvoiceId={gid}"
_FNUM = re.compile(r"F0*(\d+)")


def _fac_num(piece_number):
    """Extrait le n° de facture d'une pièce Pennylane (avant « # », ex.
    « 2026-05-31-F0000043 #SLT04-TOSLF » -> 43). None si pas une écriture facture."""
    if not piece_number:
        return None
    head = str(piece_number).split("#", 1)[0]
    m = _FNUM.search(head)
    return int(m.group(1)) if m else None


def _num2guid(establishment, company_code):
    """Pull TopOrder des factures -> {continousSequence: invoiceId (GUID PDF)}."""
    shop_id = config.establishments(company_code)[establishment]["shop_id"]
    client = toporder.for_establishment(establishment)
    if client is None:
        raise RuntimeError(f"clé TopOrder absente pour {establishment}")
    out, frm = {}, 0
    while True:
        b = None
        for _ in range(6):
            try:
                b = client.get(f"/ppe/invoice/shop/{shop_id}",
                               PaginationFrom=frm, PaginationTo=frm + 99)
                break
            except Exception:
                time.sleep(2)
        if not b:
            break
        for i in b:
            n = i.get("continousSequence")
            if n is not None and i.get("id"):
                out[int(n)] = i["id"]
        frm += len(b)
        if frm > 5000:
            break
    return out


def _download_pdf(gid):
    """Télécharge le PDF officiel de la facture (URL publique). None si KO/non-PDF."""
    try:
        r = httpx.get(PDF_URL.format(gid=gid), headers={"User-Agent": "Vaelan"}, timeout=60)
        if r.status_code != 200 or r.content[:4] != b"%PDF":
            return None
        return r.content
    except Exception:
        return None


def run_justificatifs(ctx, company_code, pfx):
    establishment = next((n for n, e in config.establishments(company_code).items() if e["pfx"] == pfx), pfx)
    cfg = config.resolve(company_code)
    ecfg = cfg["est"][pfx]
    journals = [(int(ecfg["journal_tickets_id"]), ecfg["journal_tickets"]),
                (int(ecfg["journal_factures_id"]), ecfg["journal_factures"])]
    journal_label = f"{ecfg['journal_tickets']} + {ecfg['journal_factures']}"

    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
        if not company:
            raise RuntimeError(f"société {company_code} introuvable")
        batches = s.exec(select(ImportBatch).where(
            ImportBatch.company_id == company.id, ImportBatch.kind == "toslt",
            ImportBatch.establishment == pfx)).all()
    if not batches:
        ctx.log("Aucun lot caisse généré → rien à justifier.")
        return "Rien à faire — aucun lot caisse"

    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError("clé Pennylane absente")

    start = min(b.date_from for b in batches)
    end = max(b.date_to for b in batches)
    label = f"{start.strftime('%d/%m/%Y')} → {end.strftime('%d/%m/%Y')}"
    ctx.log(f"Justificatifs · {establishment} · {label} (journaux {journal_label})")

    # 1) écritures « facture » à justifier (pièce avec F<n°>), par journal
    ctx.progress(0, 3, step="lecture des écritures Pennylane…")
    targets = []   # {num, entry_id, piece, journal, has_pdf}
    for jid, jname in journals:
        for e in pl.ledger_entries(jid, start.isoformat(), end.isoformat()):
            num = _fac_num(e.get("piece_number"))
            if num is None:
                continue
            has_pdf = bool(e.get("attachment") or e.get("ledger_attachment_filename"))
            targets.append({"num": num, "entry_id": e["id"], "piece": e.get("piece_number"),
                            "journal": jname, "has_pdf": has_pdf})
    n_total = len(targets)
    n_already = sum(1 for t in targets if t["has_pdf"])
    ctx.log(f"{n_total} écriture(s) facture trouvée(s) · {n_already} ont déjà un PDF · "
            f"{n_total - n_already} à attacher")
    if not n_total:
        ctx.log("Aucune écriture facture (rien à justifier).")

    # 2) GUID PDF par n° de facture (TopOrder)
    ctx.progress(1, 3, step="récupération des factures TopOrder…")
    num2guid = _num2guid(establishment, company_code)
    ctx.log(f"TopOrder : {len(num2guid)} factures connues")

    # 3) attache (téléchargement PDF -> upload -> PUT sur l'écriture existante)
    ctx.progress(2, 3, step="attache des PDF aux écritures…")
    todo = [t for t in targets if not t["has_pdf"]]
    attached, failed, pdf_cache = 0, [], {}
    for i, t in enumerate(todo):
        num = t["num"]
        gid = num2guid.get(num)
        if not gid:
            failed.append({**t, "why": "facture introuvable côté TopOrder"})
            continue
        data = pdf_cache.get(num)
        if data is None:
            data = _download_pdf(gid)
            pdf_cache[num] = data or b""
        if not data:
            failed.append({**t, "why": "PDF indisponible (téléchargement)"})
            continue
        try:
            fa = pl.upload_attachment(f"{pfx}_F{num:07d}.pdf", data)
            if not fa:
                failed.append({**t, "why": "upload pièce refusé"})
                continue
            pl.attach_to_entry(t["entry_id"], fa)
            t["has_pdf"] = True
            attached += 1
        except Exception as e:
            failed.append({**t, "why": f"API: {str(e)[:60]}"})
        time.sleep(0.05)
        if (i + 1) % 10 == 0:
            ctx.progress(2, 3, step=f"attache des PDF… ({i + 1}/{len(todo)})")

    still_missing = [t for t in targets if not t["has_pdf"]]
    coherent = (n_total > 0) and not still_missing
    now = datetime.utcnow() + _TZ
    stamp = now.strftime("%d/%m/%Y %H:%M")

    # détail par n° de facture (regroupe créance + reclassement)
    by_num = defaultdict(lambda: {"journaux": set(), "pdf": 0, "tot": 0})
    for t in targets:
        b = by_num[t["num"]]
        b["journaux"].add(t["journal"])
        b["tot"] += 1
        if t["has_pdf"]:
            b["pdf"] += 1
    detail = [{"num": k, "journaux": " + ".join(sorted(v["journaux"])),
               "pdf": v["pdf"], "tot": v["tot"], "ok": v["pdf"] == v["tot"]}
              for k, v in sorted(by_num.items())]

    counts = {"total": n_total, "already": n_already, "attached": attached,
              "failed": len(failed), "covered": n_total - len(still_missing)}

    # ---- compte rendu texte ----
    L = [f"JUSTIFICATIFS (PDF FACTURES) — {establishment} — {label}",
         f"Journaux {journal_label} · vérifié le {stamp} · tâche #{ctx.run_id}", "",
         "== SYNTHÈSE ==",
         f"  Écritures facture (besoin d'un justificatif) : {n_total}",
         f"  Déjà attachées avant ce passage            : {n_already}",
         f"  Attachées par Vaelan ce passage            : {attached}",
         f"  En échec                                   : {len(failed)}",
         f"  Couvertes (avec PDF au final)              : {counts['covered']} / {n_total}", ""]
    if failed:
        L += ["== ÉCHECS (à corriger) ==",
              f"  {'Facture':<10}{'Journal':<12}{'Raison':<40}"]
        for f in failed:
            L.append(f"  {'F' + str(f['num']):<10}{f['journal']:<12}{f['why'][:38]:<40}")
        L.append("")
    L += ["== DÉTAIL PAR FACTURE ==", f"  {'Facture':<10}{'Journaux':<24}{'PDF':>8}   État"]
    for d in detail:
        L.append(f"  {'F' + str(d['num']):<10}{d['journaux']:<24}{str(d['pdf']) + '/' + str(d['tot']):>8}   "
                 f"{'OK' if d['ok'] else '⚠️ MANQUE'}")
    L += ["", ("✅ COMPLET — toutes les écritures facture portent leur PDF."
               if coherent else
               (f"❌ {len(still_missing)} écriture(s) facture sans PDF."
                if n_total else "Aucune écriture facture à justifier."))]
    ctx.set_report("\n".join(L))

    from . import report
    ctx.add_artifact("report", f"{now.strftime('%Y%m%d %H%M')} compte_rendu_justif_{pfx}.pdf",
                     report.justif_pdf(establishment, journal_label, label, counts, detail,
                                       failed, coherent, run_id=ctx.run_id, executed_at=stamp),
                     "application/pdf")
    _record(company.id, pfx, coherent, end, now, ctx.run_id)

    if coherent:
        ctx.log(f"✅ Complet — {counts['covered']}/{n_total} écritures justifiées — {stamp}")
        return (f"✅ Justificatifs complets ({label}) — {attached} attaché(s), "
                f"{n_already} déjà présent(s) — {stamp}")
    msg = (f"❌ {len(still_missing)} écriture(s) sans PDF" if n_total else "Aucune écriture facture")
    ctx.log(msg + f" — {stamp}")
    return f"{msg} ({label}) — {stamp}"


def _record(company_id, pfx, ok, covered_to, when, run_id):
    with Session(engine) as s:
        d = s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == company_id, StepDeclaration.establishment == pfx,
            StepDeclaration.step == "justificatifs")).first()
        if not d:
            d = StepDeclaration(company_id=company_id, establishment=pfx, step="justificatifs")
        d.verified_at = when
        d.verify_ok = ok
        d.verify_run_id = run_id
        d.covered_to = covered_to
        d.state = "verified" if ok else "declared"
        d.updated_at = datetime.utcnow()
        s.add(d)
        s.commit()
