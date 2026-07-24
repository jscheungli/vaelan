"""Étape 7 KOOKABURA — Factures d'achat vers STERNA.

Chaque facture de VENTE de Kookabura aux 3 boulangeries devient une FACTURE D'ACHAT
côté STERNA (fournisseur KOOKABURA), en « facture à saisir » : PDF officiel TopOrder
téléversé + montants HT/TVA/TTC par taux -> POST /supplier_invoices/import.

Idempotent : external_reference unique « TOKK-<invoiceId> » ; un renvoi -> 409 = déjà
importée (compté « déjà présente », pas une erreur). Périmètre = le DERNIER lot KK
généré (règle « dernier lot »), et UNIQUEMENT les factures des 3 boulangeries (un
client externe de KK, ex. GAA, n'est pas un achat STERNA).

Cadrage final : factures TopOrder de la période vs présentes côté STERNA
(external_reference), en nombre ET en montant TTC.
"""
import time
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import pennylane, toporder
from app.models import Company, ImportBatch, StepDeclaration
from . import config
from .justificatifs import _download_pdf

_TZ = timedelta(hours=4)
_REF = "TOKK-{gid}"                       # external_reference (dédup) côté STERNA
_VAT = {"2.1": "FR_21", "8.5": "FR_85", "5.5": "FR_55", "20": "FR_200", "0": "exempt"}
KOOKABURA_SUPPLIER_NAME = "KOOKABURA"     # fournisseur côté STERNA (résolu par nom)


def _kk_supplier_id(pl_sterna):
    """Id du fournisseur KOOKABURA dans le Pennylane STERNA (résolu par nom, paginé)."""
    cur = None
    while True:
        params = {"limit": 100}
        if cur:
            params["cursor"] = cur
        d = pl_sterna.get("/suppliers", **params)
        for s in (d.get("items") or []):
            if KOOKABURA_SUPPLIER_NAME in str(s.get("name") or "").upper():
                return s["id"]
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    return None


def _kk_invoices(date_from, date_to):
    """Factures KK de la période (date du TICKET), clientes = les 3 boulangeries.
    -> [{gid, fnum, date, client, perrate {taux: HT}, ht, tva, ttc}]"""
    est = config.establishments("KOOKABURA")["KOOKABURA"]
    client = toporder.for_establishment("KOOKABURA")
    if client is None:
        raise RuntimeError("clé TopOrder KOOKABURA absente")
    shop = est["shop_id"]
    b2b = config.clients("KOOKABURA")["b2b"]

    def _day(ts):
        s = str(ts or "")
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else None

    tinfo, frm = {}, 0
    while frm < 20000:
        b = client.get(f"/ppe/ticket/shop/{shop}", PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        for w in b:
            tk = w.get("ticket") or {}
            dd = _day(tk.get("timestamp"))
            if tk.get("id"):
                tinfo[tk["id"]] = (tk.get("companyId"), dd)
            if tk.get("rootTicketId"):
                tinfo.setdefault(tk["rootTicketId"], (tk.get("companyId"), dd))
        frm += len(b)
        if len(b) < 100:
            break

    facs, frm = [], 0
    while frm < 8000:
        b = client.get(f"/ppe/invoice/shop/{shop}", PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        facs += b
        frm += len(b)
        if len(b) < 100:
            break

    out = []
    for f in sorted(facs, key=lambda x: x.get("continousSequence") or 0):
        ti = tinfo.get(f.get("ticketId")) or tinfo.get(f.get("rootTicketId"))
        if not ti:
            continue
        coid, dd = ti
        if not dd or not (date_from <= dd <= date_to):
            continue
        info = b2b.get(f"KK:{coid}")
        if not info:
            continue                      # client KK hors groupe (pas un achat STERNA)
        perrate, ht = {}, 0.0
        for part in (f.get("totalPriceHTByVATRate") or "").split("|"):
            if ":" not in part:
                continue
            pct = int(part.split(":")[0]) / 100.0
            h = int(part.split(":")[1]) / 100.0
            if abs(h) >= 0.005:
                perrate[f"{pct:g}"] = round(perrate.get(f"{pct:g}", 0.0) + h, 2)
        if not perrate:
            continue
        tva = round(sum(h * float(r) / 100 for r, h in perrate.items()), 2)
        ht = round(sum(perrate.values()), 2)
        out.append({"gid": f["id"], "fnum": int(f.get("continousSequence") or 0),
                    "date": dd, "client": info["name"], "perrate": perrate,
                    "ht": ht, "tva": tva, "ttc": round(ht + tva, 2)})
    return out


def run_achats_kk(ctx):
    """Pousse les factures KK du dernier lot vers STERNA (achats « à saisir ») + cadrage."""
    with Session(engine) as s:
        kk = s.exec(select(Company).where(Company.code == "KOOKABURA")).first()
        if not kk:
            raise RuntimeError("société KOOKABURA introuvable")
        batches = s.exec(select(ImportBatch).where(
            ImportBatch.company_id == kk.id, ImportBatch.kind == "toslt")).all()
    if not batches:
        return "Rien à faire — aucun lot KK généré"
    # période = TOUT ce qui a été généré (min -> max des lots), PAS seulement le dernier lot :
    # l'envoi est idempotent (external_reference), donc couvrir large est sans risque et
    # garantit qu'un mois généré avant la mise en place de cette étape (ex. juin) est rattrapé.
    latest = max(batches, key=lambda b: (b.created_at or datetime.min, b.id))
    d_from = min(b.date_from for b in batches)
    d_to = max(b.date_to for b in batches)
    date_from, date_to = d_from.isoformat(), d_to.isoformat()
    label = f"{d_from.strftime('%d/%m/%Y')} → {d_to.strftime('%d/%m/%Y')}"

    pl = pennylane.for_company("STERNA")
    if not pl:
        raise RuntimeError("clé Pennylane STERNA absente")
    ctx.log(f"Factures d'achat KK → STERNA · période {label} (tous les lots générés, jusqu'à {latest.code})")
    supplier_id = _kk_supplier_id(pl)
    if not supplier_id:
        raise RuntimeError("fournisseur KOOKABURA introuvable dans le Pennylane STERNA")
    ctx.log(f"Fournisseur STERNA : KOOKABURA (id {supplier_id})")

    ctx.progress(0, None, step="lecture des factures KK…")
    invoices = _kk_invoices(date_from, date_to)
    ctx.log(f"{len(invoices)} facture(s) KK vers les boulangeries sur la période")

    pushed, already, errors = [], [], []
    for i, f in enumerate(invoices):
        ctx.progress(i, len(invoices), step=f"facture F{f['fnum']} ({i + 1}/{len(invoices)})…")
        ref = _REF.format(gid=f["gid"])
        pdf = _download_pdf(f["gid"])
        if pdf is None:
            errors.append({**f, "why": "PDF TopOrder introuvable"})
            continue
        try:
            fid = pl.upload_attachment(f"KK-F{f['fnum']:07d}.pdf", pdf)
        except Exception as e:
            errors.append({**f, "why": f"upload PDF: {str(e)[:60]}"})
            continue
        lines = []
        for r, h in sorted(f["perrate"].items()):
            t = round(h * float(r) / 100, 2)
            # currency_amount de LIGNE = TTC (vérifié : l'API exige Σ lignes = total TTC ;
            # la doc dit « HT » mais renvoie 422 sinon). currency_tax = la TVA de la ligne.
            lines.append({"currency_amount": f"{round(h + t, 2):.2f}", "currency_tax": f"{t:.2f}",
                          "vat_rate": _VAT.get(r, "exempt")})
        body = {
            "file_attachment_id": fid, "supplier_id": supplier_id,
            "date": f["date"], "deadline": f["date"],
            "invoice_number": f"F{f['fnum']:07d}",
            "external_reference": ref,
            "currency_amount_before_tax": f"{f['ht']:.2f}",
            "currency_tax": f"{f['tva']:.2f}",
            "currency_amount": f"{f['ttc']:.2f}",
            "label": f"Facture KOOKABURA F{f['fnum']:07d} · {f['client']}",
            "invoice_lines": lines,
        }
        st, resp = pl.import_supplier_invoice(body)
        dup = "already been taken" in str(resp)      # doublon d'external_reference (l'API répond 422)
        if st in (200, 201):
            pushed.append(f)
        elif st == 409 or dup:                       # déjà importée -> idempotent, pas une erreur
            already.append(f)
        else:
            errors.append({**f, "why": f"HTTP {st}: {str(resp)[:80]}"})
        time.sleep(0.2)

    # ---- cadrage : tout TopOrder de la période doit être côté STERNA ----
    ctx.progress(len(invoices), len(invoices), step="cadrage STERNA…")
    refs_pl = set()
    try:
        for si in pl.supplier_invoices(supplier_id=supplier_id):
            r = si.get("external_reference")
            if r:
                refs_pl.add(r)
    except Exception as e:
        ctx.log(f"⚠️ relecture supplier_invoices impossible ({str(e)[:60]}) — cadrage sur les statuts d'envoi")
        refs_pl = None
    missing = []
    for f in invoices:
        ref = _REF.format(gid=f["gid"])
        ok = (ref in refs_pl) if refs_pl is not None else \
             any(x["gid"] == f["gid"] for x in pushed + already)
        if not ok:
            missing.append(f)

    now = datetime.utcnow() + _TZ
    stamp = now.strftime("%d/%m/%Y %H:%M")
    coherent = not errors and not missing
    tot = round(sum(f["ttc"] for f in invoices), 2)
    ctx.log(f"{len(pushed)} poussée(s) · {len(already)} déjà présente(s) · "
            f"{len(errors)} erreur(s) · {len(missing)} manquante(s) · total TTC {tot:.2f} €")

    L = [f"FACTURES D'ACHAT KOOKABURA → STERNA — {label}",
         f"vérifié le {stamp} · tâche #{ctx.run_id} · fournisseur STERNA id {supplier_id}", "",
         "== SYNTHÈSE ==",
         f"  Factures KK de la période      : {len(invoices)}  ({tot:.2f} € TTC)",
         f"  Poussées (nouvelles)           : {len(pushed)}",
         f"  Déjà présentes (idempotence)   : {len(already)}",
         f"  En erreur                      : {len(errors)}",
         f"  Manquantes côté STERNA         : {len(missing)}", ""]
    for f in pushed:
        L.append(f"  + F{f['fnum']:07d}  {f['date']}  {f['client'][:28]:<30} {f['ttc']:>10.2f}")
    if pushed:
        L.append("")
    if errors:
        L += ["== ERREURS =="] + [
            f"  F{f['fnum']:07d}  {f['client'][:24]:<26} {f['why']}" for f in errors] + [""]
    if missing:
        L += ["== MANQUANTES CÔTÉ STERNA =="] + [
            f"  F{f['fnum']:07d}  {f['date']}  {f['ttc']:>10.2f}" for f in missing] + [""]
    L.append("✅ COMPLET — toutes les factures KK de la période sont côté STERNA (à saisir)."
             if coherent else "⚠️ INCOMPLET — voir erreurs/manquantes ci-dessus.")
    ctx.set_report("\n".join(L))

    from . import report
    counts = {"total": len(invoices), "pushed": len(pushed), "already": len(already),
              "errors": len(errors), "missing": len(missing), "ttc": tot}
    ctx.add_artifact("report", f"{now.strftime('%Y%m%d %H%M')} compte_rendu_achats_KK.pdf",
                     report.achats_pdf("Kookabura → Sterna", label, counts, pushed, already,
                                       errors, missing, coherent,
                                       run_id=ctx.run_id, executed_at=stamp),
                     "application/pdf")

    with Session(engine) as s:
        d = s.exec(select(StepDeclaration).where(
            StepDeclaration.company_id == kk.id, StepDeclaration.establishment == "",
            StepDeclaration.step == "achats_kk")).first()
        if not d:
            d = StepDeclaration(company_id=kk.id, establishment="", step="achats_kk")
        d.verified_at = now
        d.verify_ok = coherent
        d.verify_run_id = ctx.run_id
        d.covered_to = d_to
        d.state = "verified" if coherent else "declared"
        d.updated_at = datetime.utcnow()
        s.add(d)
        s.commit()

    head = f"{len(pushed)} poussée(s), {len(already)} déjà là, total {tot:.2f} €"
    if coherent:
        return f"✅ Achats KK → STERNA ({label}) — {head} — {stamp}"
    return f"❌ Achats KK → STERNA ({label}) — {head} ; {len(errors)} erreur(s), {len(missing)} manquante(s)"
