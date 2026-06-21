"""Génération du CSV d'import Pennylane — journal TOSLT (caisse, base tickets).

Port fidèle de gen_caisse_tickets : le ticket est la vérité fiscale.
  - écriture JOURNALIÈRE agrégée (anonyme + facturé payé comptant) :
    crédit 70101/taux + TVA collectée ; débit encaissements (rendu monnaie netté) ;
    écart de caisse résiduel.
  - écriture AUTONOME par ticket facturé-à-crédit (créance) : son CA + son
    encaissement éventuel + débit 411 du client. Pièce = …-F{n° facture} → c'est
    cette écriture qui recevra le PDF de la facture en pièce jointe (via l'API).

Le CSV référence les comptes par NUMÉRO et le journal par son CODE (import manuel
Pennylane). Toutes les lignes d'un lot portent l'identifiant de lot en tête de la
« Libellé de pièce » → le lot est repérable et supprimable d'un bloc dans Pennylane.

Pré-vol souple : si un ticket facturé-à-crédit de la période ne se route vers
aucun compte client (B2B sans compte 411), il est remonté dans `unresolved` et la
génération est refusée — l'utilisateur corrige (TopOrder/Pennylane + resync) puis
relance. Les clients propres ailleurs ne bloquent rien.
"""
from collections import defaultdict

from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import toporder
from app.models import ClientAccount
from . import config
from .engine import _get, _day, _fac_ticketids, ZERO

# Format d'import manuel Pennylane. Taux de TVA en virgule décimale (« 2,1% »).
# Catégories analytiques dans les colonnes 10-11 (Famille « Etablissement » / nom
# court établi). 12e colonne « Référence lettrage » : pré-lettrage des créances
# par n° de facture (token F<n°>) → la créance 411 se solde au règlement.
HEADER = ["Date", "Code journal", "Numéro de compte", "Libellé de compte",
          "Libellé de ligne", "Taux de TVA du compte", "Libellé de pièce",
          "Débit", "Crédit", "Famille de catégories", "Catégorie", "Référence lettrage"]


def _rate_label(r: str) -> str:
    """'2.1' -> '2,1%' ; '0' -> '0%' ; '20' -> '20%' (format Pennylane FR)."""
    f = float(r)
    s = str(int(f)) if f == int(f) else f"{f:g}".replace(".", ",")
    return s + "%"

# moyen de paiement TopOrder -> clé de compte d'encaissement
_PAYKEY = {"CB": "cb", "Carte": "cb", "Espèce": "especes", "Espece": "especes",
           "Ticket restaurant": "ticket_resto", "Titre restaurant": "ticket_resto"}


def _resolver(company_id: int, cfg: dict):
    """Construit le routeur de créances depuis la table de correspondance (DB).

    Renvoie resolve(pfx, coid, cid) -> (numéro de compte 411, nom) ou (None, None)
    quand un client PRO n'a pas de compte (= anomalie bloquante au pré-vol).
    """
    with Session(engine) as s:
        rows = s.exec(select(ClientAccount).where(
            ClientAccount.company_id == company_id)).all()
    b2b = {(r.establishment, r.toporder_company_id): (r.account_411, r.toporder_name)
           for r in rows}

    def resolve(pfx, coid, cid):
        if coid and coid != ZERO:
            acc, nm = b2b.get((pfx, coid), (None, None))
            return (acc, nm or "client") if acc else (None, None)
        if cid and cid != ZERO:
            return cfg["est"][pfx]["b2c_commun"], "Particulier"
        return None, None

    return resolve


def build_toslt(establishment, date_from, date_to, company_id,
                batch_code, company_code, toslf_batch_code=None, on_progress=None) -> dict:
    """Construit les lignes des CSV TOSLT (caisse) ET TOSLF (reclassement factures),
    en un seul pull. Ne touche à rien si `unresolved` non vide."""
    est = config.ESTABLISHMENTS[establishment]
    pfx = est["pfx"]
    shop_id = est["shop_id"]
    cfg = config.resolve(company_code)                 # comptes/journaux éditables (DB + défauts)
    ecfg = cfg["est"][pfx]
    cat_family = cfg["analytic_family"]
    cat_label = ecfg["category"]
    client = toporder.for_establishment(establishment)
    if client is None:
        raise RuntimeError(f"Clé TopOrder absente pour {establishment} "
                           f"({toporder.env_var_for(establishment)})")
    resolve = _resolver(company_id, cfg)

    journal = ecfg["journal_tickets"]                  # ex. TOSLT
    ca_acc = cfg["ca_anonyme"]                          # 70101
    tva_acc = cfg["tva"]
    acc = ecfg
    ecart_acc = acc["ecart"]

    import time
    tk2fac, op2fac = _fac_ticketids(client, shop_id)
    to = int(time.mktime(time.strptime(date_to, "%Y-%m-%d"))) + 4 * 3600 + 24 * 3600

    agg = defaultdict(lambda: {"vat": defaultdict(lambda: [0.0, 0.0]),
                               "pay": defaultdict(float)})
    factures = []       # flux CA facturé : par facture (CA + 411 brut + paiements comptant)
    reglements = []     # flux règlement : paiement de facture encaissé en caisse (orderPaymentId lié)
    unresolved = []
    n_tickets = 0
    tot_ttc = 0.0                    # CA TTC de TOUS les tickets (= Z, pour le cadrage)
    pay_total = defaultdict(float)   # encaissements par mode (pour le cadrage)
    fac_pay = defaultdict(float)     # encaissements SUR FACTURES par mode (réconciliation de l'écart)
    fac_pay_detail = []              # liste des paiements de factures (date, facture, client, mode, montant)
    vat_total = defaultdict(lambda: [0.0, 0.0])  # taux -> [HT, TTC] (pull brut, pour le compte rendu)
    frm, pages = 0, 0
    while True:
        b = _get(client, f"/ppe/ticket/shop/{shop_id}", To=to,
                 PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        pages += 1
        pmax = "0"
        for w in b:
            tk = w.get("ticket") or {}
            dd = _day(tk.get("timestamp"))
            pmax = max(pmax, dd or "0")
            if not dd or not (date_from <= dd <= date_to):
                continue
            n_tickets += 1
            vat = defaultdict(lambda: [0.0, 0.0])
            ttc = 0.0
            for l in (w.get("ticketSalesLines") or []):
                r = f"{float(l.get('vatRate') or 0):g}"
                ht_l = float(l.get("totalPriceHT") or 0)
                ttc_l = float(l.get("totalPriceTTC") or 0)
                vat[r][0] += ht_l
                vat[r][1] += ttc_l
                ttc += ttc_l
                vat_total[r][0] += ht_l
                vat_total[r][1] += ttc_l
            tot_ttc += ttc
            payments_raw = w.get("ticketPaymentData") or []
            pay = defaultdict(float)
            for p in payments_raw:
                amt = float(p.get("paymentAmount") or 0)
                pay[p.get("paymentType") or "?"] += amt
                pay_total[p.get("paymentType") or "?"] += amt
            fnum = tk2fac.get(tk.get("id")) or (tk.get("rootTicketId") and tk2fac.get(tk["rootTicketId"]))
            if fnum:          # FACTURÉ -> écriture par facture (CA + 411 brut + paiements comptant)
                coid, cid = tk.get("companyId"), tk.get("customerId")
                a, nm = resolve(pfx, coid, cid)
                if not a:
                    unresolved.append({"date": dd, "company_id": coid, "customer_id": cid,
                                       "facture": f"{int(fnum):07d}", "amount": round(ttc, 2)})
                    continue
                factures.append({"date": dd, "fnum": int(fnum), "acc": a, "nm": nm,
                                 "vat": dict(vat), "pay": dict(pay), "ttc": round(ttc, 2),
                                 "b2b": bool(coid and coid != ZERO)})
                for _pt, _amt in pay.items():           # paiements comptant sur facture
                    if abs(_amt) < 0.005:
                        continue
                    fac_pay[_pt] += _amt
                    fac_pay_detail.append({"date": dd, "fnum": int(fnum), "nm": nm,
                                           "mode": _pt, "amount": round(_amt, 2)})
            else:             # NON facturé : CA anonyme -> agrégat ; paiements -> règlement OU agrégat
                d = agg[dd]
                for r, (ht, t) in vat.items():
                    d["vat"][r][0] += ht
                    d["vat"][r][1] += t
                for p in payments_raw:
                    amt = float(p.get("paymentAmount") or 0)
                    if abs(amt) < 0.005:
                        continue
                    pt = p.get("paymentType") or "?"
                    rfnum = op2fac.get(p.get("orderPaymentId"))
                    if rfnum:   # RÈGLEMENT d'une facture encaissé en caisse (comptant décalé inclus)
                        pdate = _day(p.get("timestamp")) or dd
                        a, nm = resolve(pfx, p.get("companyId"), p.get("customerId"))
                        if not a:
                            unresolved.append({"date": pdate, "company_id": p.get("companyId"),
                                               "customer_id": p.get("customerId"),
                                               "facture": f"{int(rfnum):07d}", "amount": round(amt, 2)})
                            continue
                        reglements.append({"date": pdate, "fnum": int(rfnum), "acc": a,
                                           "nm": nm, "mode": pt, "amount": amt})
                        fac_pay[pt] += amt              # règlement de facture encaissé en caisse
                        fac_pay_detail.append({"date": pdate, "fnum": int(rfnum), "nm": nm,
                                               "mode": pt, "amount": round(amt, 2)})
                    else:       # encaissement anonyme
                        d["pay"][pt] += amt
        if on_progress:
            on_progress(n_tickets, f"pull tickets… {n_tickets} traités")
        if pmax < date_from:
            break
        frm += len(b)
        if pages > 600:
            break

    ca_ttc_z = round(tot_ttc, 2)
    payments = {k: round(v, 2) for k, v in sorted(pay_total.items())}
    fac_payments = {k: round(v, 2) for k, v in sorted(fac_pay.items())}
    ht_by_rate = {r: round(v[0], 2) for r, v in sorted(vat_total.items())}
    tva_by_rate = {r: round(v[1] - v[0], 2) for r, v in sorted(vat_total.items())}

    # Pré-vol souple : on refuse de générer si une créance de la période ne se route pas.
    if unresolved:
        _nb2b = sum(1 for f in factures if f["b2b"])
        return {"unresolved": unresolved, "rows": [], "n_tickets": n_tickets,
                "ca_ttc": ca_ttc_z, "payments": payments, "fac_payments": fac_payments,
                "fac_payment_detail": fac_pay_detail,
                "ht_by_rate": ht_by_rate, "tva_by_rate": tva_by_rate,
                # compteurs + CA HT/TVA pour le log et le compte rendu (CSV non généré)
                "n_factures": len(factures), "n_b2b": _nb2b, "n_b2c": len(factures) - _nb2b,
                "n_reglements": len(reglements),
                "ca_ht": round(sum(ht_by_rate.values()), 2),
                "tva": round(sum(tva_by_rate.values()), 2)}

    rows = []
    tot = {"ca_ht": 0.0, "tva": 0.0, "enc": 0.0, "creance": 0.0, "ecart": 0.0}

    def _ca_lines(date, piece, vat):
        """CA 70101/taux + TVA (gardé dans le Z). Gère les AVOIRS (HT/TVA négatifs
        bookés en sens inversé : débit au lieu de crédit). Renvoie le TTC.
        On accumule les valeurs ARRONDIES (= celles écrites dans le CSV) pour que
        l'écart de caisse équilibre l'écriture EXACTEMENT (sinon décalage d'1 cent
        flottant -> « marge légale 1€ » à l'import Pennylane)."""
        ttcsum = 0.0
        for r, (ht_raw, ttc_) in sorted(vat.items()):
            ht = round(ht_raw, 2)
            if abs(ht) < 0.005 and abs(ttc_) < 0.005:
                continue
            rl = _rate_label(r)
            # CA HT : crédit si vente, débit si avoir
            if ht >= 0:
                rows.append([date, journal, ca_acc, "Vente Caisse Magasin (Tickets)",
                             f"CA HT {rl}", rl, piece, "", f"{ht:.2f}", cat_family, cat_label, ""])
            else:
                rows.append([date, journal, ca_acc, "Avoir caisse",
                             f"Avoir CA HT {rl}", rl, piece, f"{-ht:.2f}", "", cat_family, cat_label, ""])
            tot["ca_ht"] += ht
            ttcsum += ht
            tva = round(ttc_ - ht_raw, 2)
            if r in tva_acc and abs(tva) > 0.005:
                if tva >= 0:
                    rows.append([date, journal, tva_acc[r], f"TVA collectée {rl}",
                                 f"TVA {rl}", "", piece, "", f"{tva:.2f}", cat_family, cat_label, ""])
                else:
                    rows.append([date, journal, tva_acc[r], f"TVA collectée {rl}",
                                 f"Avoir TVA {rl}", "", piece, f"{-tva:.2f}", "", cat_family, cat_label, ""])
                tot["tva"] += tva
                ttcsum += tva
        return round(ttcsum, 2)

    def emit_agg(date, piece, vat, pay):
        """Écriture journalière ANONYME : CA + encaissements + écart de caisse."""
        ttcsum = _ca_lines(date, piece, vat)
        enc = 0.0
        for pt, amt_raw in sorted(pay.items()):
            amt = round(amt_raw, 2)          # valeur arrondie = celle écrite (équilibre exact)
            if abs(amt) < 0.005:
                continue
            pacc = acc.get(_PAYKEY.get(pt, ""), acc["autres"])
            if amt > 0:
                rows.append([date, journal, pacc, pt, pt, "", piece, f"{amt:.2f}", "", cat_family, cat_label, ""])
            else:
                rows.append([date, journal, pacc, pt, pt + " (rendu)", "", piece, "", f"{-amt:.2f}", cat_family, cat_label, ""])
            enc += amt
        tot["enc"] += enc
        ec = round(ttcsum - enc, 2)
        if abs(ec) >= 0.01:
            rows.append([date, journal, ecart_acc, "Écart de caisse / avance",
                         ("Avance/dépôt" if ec < 0 else "Écart de caisse"), "", piece,
                         (f"{ec:.2f}" if ec > 0 else ""), (f"{-ec:.2f}" if ec < 0 else ""),
                         cat_family, cat_label, ""])
            tot["ecart"] += ec

    def emit_facture(date, piece, fnum, acc411, nm, vat, pay):
        """Écriture par FACTURE : CA + débit 411 (brut TTC) + ventilation des
        paiements (débit encaissement + crédit 411). Lettrage F<n°>. Le solde 411
        restant = créance (impayé)."""
        lettr = f"F{fnum}"
        ttcsum = _ca_lines(date, piece, vat)
        # la facture : débit 411 du montant brut TTC (crédit si avoir)
        if ttcsum >= 0:
            rows.append([date, journal, str(acc411), "Client - facture",
                         f"Facture {fnum:07d} · {nm}", "", piece,
                         f"{ttcsum:.2f}", "", cat_family, cat_label, lettr])
        else:
            rows.append([date, journal, str(acc411), "Client - avoir",
                         f"Avoir {fnum:07d} · {nm}", "", piece,
                         "", f"{-ttcsum:.2f}", cat_family, cat_label, lettr])
        paid = 0.0
        for pt, amt in sorted(pay.items()):
            if abs(amt) < 0.005:
                continue
            pacc = acc.get(_PAYKEY.get(pt, ""), acc["autres"])
            # encaissement (banque) + crédit 411 (règlement, lettré F<n°>)
            if amt > 0:
                rows.append([date, journal, pacc, pt, f"{pt} facture {fnum:07d}", "", piece,
                             f"{amt:.2f}", "", cat_family, cat_label, ""])
                rows.append([date, journal, str(acc411), "Règlement client",
                             f"Règlement {pt} F{fnum:07d}", "", piece, "", f"{amt:.2f}",
                             cat_family, cat_label, lettr])
            else:  # rendu : sens inversé
                rows.append([date, journal, pacc, pt, f"{pt} facture {fnum:07d} (rendu)", "", piece,
                             "", f"{-amt:.2f}", cat_family, cat_label, ""])
                rows.append([date, journal, str(acc411), "Règlement client",
                             f"Règlement {pt} F{fnum:07d} (rendu)", "", piece, f"{-amt:.2f}", "",
                             cat_family, cat_label, lettr])
            paid += amt
        tot["enc"] += paid
        tot["creance"] += round(ttcsum - paid, 2)   # 411 restant = impayé

    def emit_reglement(reg):
        """Règlement de facture encaissé en caisse : débit encaissement + crédit 411
        (lettré F<n°>). Solde le 411 de la facture (émise dans cette période ou avant)."""
        d, fnum, amt, pt = reg["date"], reg["fnum"], reg["amount"], reg["mode"]
        lettr = f"F{fnum}"
        piece = f"{pfx}{d[8:10]}{d[5:7]}{d[2:4]}-R{fnum:07d} #{batch_code}"
        pacc = acc.get(_PAYKEY.get(pt, ""), acc["autres"])
        if amt > 0:
            rows.append([d, journal, pacc, pt, f"Règlement {pt} F{fnum:07d}", "", piece,
                         f"{amt:.2f}", "", cat_family, cat_label, ""])
            rows.append([d, journal, str(reg["acc"]), "Règlement client",
                         f"Règlement F{fnum:07d} · {reg['nm']}", "", piece, "", f"{amt:.2f}",
                         cat_family, cat_label, lettr])
        else:
            rows.append([d, journal, pacc, pt, f"Règlement {pt} F{fnum:07d} (rendu)", "", piece,
                         "", f"{-amt:.2f}", cat_family, cat_label, ""])
            rows.append([d, journal, str(reg["acc"]), "Règlement client",
                         f"Règlement F{fnum:07d} (rendu) · {reg['nm']}", "", piece, f"{-amt:.2f}", "",
                         cat_family, cat_label, lettr])
        tot["enc"] += amt

    def piece_for(date, suffix=""):
        # pièce du jour en tête (lisible pour le rapprochement espèces/CB/…),
        # identifiant de lot en queue avec « # ». ex. « SM280526 #SMT03 »
        return f"{pfx}{date[8:10]}{date[5:7]}{date[2:4]}{suffix} #{batch_code}"

    for dt in sorted(agg):
        emit_agg(dt, piece_for(dt), agg[dt]["vat"], agg[dt]["pay"])
    for f in sorted(factures, key=lambda x: (x["date"], x["fnum"])):
        emit_facture(f["date"], piece_for(f["date"], f"-F{f['fnum']:07d}"),
                     f["fnum"], f["acc"], f["nm"], f["vat"], f["pay"])
    for reg in sorted(reglements, key=lambda x: (x["date"], x["fnum"])):
        emit_reglement(reg)

    # ----- TOSLF : reclassement HT 70101 -> 7012 (B2B) / 70102 (B2C) par facture -----
    jf = ecfg["journal_factures"]
    lbl_b2b, lbl_b2c = "Reclassement B2B", "Reclassement B2C"
    toslf_rows = []
    tcode = toslf_batch_code or batch_code

    def toslf_emit(f):
        fnum, b2b, date = f["fnum"], f["b2b"], f["date"]
        dest = cfg["ca_b2b"] if b2b else cfg["ca_b2c"]
        lbl = lbl_b2b if b2b else lbl_b2c
        piece = f"F{fnum:07d} #{tcode}"
        for r, (ht, ttc_) in sorted(f["vat"].items()):
            if abs(ht) < 0.005:
                continue
            rl = _rate_label(r)
            if ht >= 0:   # sortie de l'anonyme (débit 70101) / entrée en 7012-70102 (crédit)
                toslf_rows.append([date, jf, ca_acc, "Reclassement (sortie caisse)",
                                   f"Reclass {fnum:07d} {rl}", rl, piece, f"{ht:.2f}", "", cat_family, cat_label, ""])
                toslf_rows.append([date, jf, dest, lbl, f"{lbl} {fnum:07d} {rl}", rl, piece,
                                   "", f"{ht:.2f}", cat_family, cat_label, ""])
            else:         # avoir : sens inversé
                h = -ht
                toslf_rows.append([date, jf, ca_acc, "Reclassement (sortie caisse)",
                                   f"Reclass avoir {fnum:07d} {rl}", rl, piece, "", f"{h:.2f}", cat_family, cat_label, ""])
                toslf_rows.append([date, jf, dest, lbl, f"{lbl} avoir {fnum:07d} {rl}", rl, piece,
                                   f"{h:.2f}", "", cat_family, cat_label, ""])

    for f in sorted(factures, key=lambda x: (x["date"], x["fnum"])):
        toslf_emit(f)

    # UN SEUL CSV : les écritures TOSLF (reclassement) rejoignent les TOSLT.
    # Pennylane sépare les écritures par la colonne « Code journal ».
    rows = rows + toslf_rows
    deb = sum(float(r[7]) for r in rows if r[7])
    cred = sum(float(r[8]) for r in rows if r[8])
    n_b2b = sum(1 for f in factures if f["b2b"])
    return {
        "header": HEADER, "rows": rows, "unresolved": [],
        "n_toslf": len(toslf_rows),
        "n_tickets": n_tickets, "n_agg": len(agg), "n_creances": len(factures),
        "n_factures": len(factures), "n_b2b": n_b2b, "n_b2c": len(factures) - n_b2b,
        "n_reglements": len(reglements),
        "ca_ttc": ca_ttc_z, "payments": payments, "fac_payments": fac_payments,
        "fac_payment_detail": fac_pay_detail,
        "ht_by_rate": ht_by_rate, "tva_by_rate": tva_by_rate,
        "ca_ht": round(tot["ca_ht"], 2), "tva": round(tot["tva"], 2),
        "encaisse": round(tot["enc"], 2), "creances": round(tot["creance"], 2),
        "ecart": round(tot["ecart"], 2),
        "balanced": abs(deb - cred) < 0.05, "debit": round(deb, 2), "credit": round(cred, 2),
    }
