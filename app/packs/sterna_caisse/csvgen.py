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


def _resolver(company_id: int):
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
            return config.caisse_accounts(pfx)["b2c_commun"], "Particulier"
        return None, None

    return resolve


def build_toslt(establishment, date_from, date_to, company_id,
                batch_code, on_progress=None) -> dict:
    """Construit les lignes du CSV TOSLT. Ne touche à rien si `unresolved` non vide."""
    est = config.ESTABLISHMENTS[establishment]
    pfx = est["pfx"]
    shop_id = est["shop_id"]
    cat_family = config.ANALYTIC_FAMILY
    cat_label = config.ANALYTIC_CATEGORY[pfx]
    client = toporder.for_establishment(establishment)
    if client is None:
        raise RuntimeError(f"Clé TopOrder absente pour {establishment} "
                           f"({toporder.env_var_for(establishment)})")
    resolve = _resolver(company_id)

    journal = config.journal_code(pfx, "tickets")     # ex. TOSLT
    ca_acc = config.CA_ANONYME                         # 70101
    tva_acc = config.TVA_ACCOUNT
    acc = config.caisse_accounts(pfx)
    ecart_acc = acc["ecart"]

    import time
    tk2fac = _fac_ticketids(client, shop_id)
    to = int(time.mktime(time.strptime(date_to, "%Y-%m-%d"))) + 4 * 3600 + 24 * 3600

    agg = defaultdict(lambda: {"vat": defaultdict(lambda: [0.0, 0.0]),
                               "pay": defaultdict(float)})
    crean = []
    unresolved = []
    n_tickets = 0
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
            pay = defaultdict(float)
            ttc = 0.0
            for l in (w.get("ticketSalesLines") or []):
                r = f"{float(l.get('vatRate') or 0):g}"
                vat[r][0] += float(l.get("totalPriceHT") or 0)
                vat[r][1] += float(l.get("totalPriceTTC") or 0)
                ttc += float(l.get("totalPriceTTC") or 0)
            paysum = 0.0
            for p in (w.get("ticketPaymentData") or []):
                amt = float(p.get("paymentAmount") or 0)
                pay[p.get("paymentType") or "?"] += amt
                paysum += amt
            recv = round(ttc - paysum, 2)
            if recv > 0.01:   # facturé à crédit -> écriture autonome
                coid, cid = tk.get("companyId"), tk.get("customerId")
                a, nm = resolve(pfx, coid, cid)
                fnum = tk2fac.get(tk.get("id")) or (tk.get("rootTicketId") and tk2fac.get(tk["rootTicketId"]))
                if not a:
                    unresolved.append({"date": dd, "company_id": coid, "customer_id": cid,
                                       "facture": (f"{int(fnum):07d}" if fnum else None),
                                       "amount": recv})
                    continue
                crean.append({"date": dd, "fnum": int(fnum) if fnum else 0,
                              "acc": a, "nm": nm, "vat": dict(vat), "pay": dict(pay),
                              "recv": recv})
            else:             # anonyme / payé comptant -> agrégat journalier
                d = agg[dd]
                for r, (ht, t) in vat.items():
                    d["vat"][r][0] += ht
                    d["vat"][r][1] += t
                for pt, amt in pay.items():
                    d["pay"][pt] += amt
        if on_progress:
            on_progress(n_tickets, f"pull tickets… {n_tickets} traités")
        if pmax < date_from:
            break
        frm += len(b)
        if pages > 600:
            break

    # Pré-vol souple : on refuse de générer si une créance de la période ne se route pas.
    if unresolved:
        return {"unresolved": unresolved, "rows": [], "n_tickets": n_tickets}

    rows = []
    tot = {"ca_ht": 0.0, "tva": 0.0, "enc": 0.0, "creance": 0.0, "ecart": 0.0}

    def emit(date, piece, vat, pay, cre_acc=None, cre_amt=0.0, cre_nm=None, cre_fnum=None):
        ttcsum = sum(v[1] for v in vat.values())
        for r, (ht, ttc_) in sorted(vat.items()):
            if ttc_ == 0:
                continue
            rl = _rate_label(r)
            rows.append([date, journal, ca_acc, "Vente Caisse Magasin (Tickets)",
                         f"CA HT {rl}", rl, piece, "", f"{ht:.2f}", cat_family, cat_label, ""])
            tot["ca_ht"] += ht
            if r in tva_acc and ttc_ - ht > 0.005:
                rows.append([date, journal, tva_acc[r], f"TVA collectée {rl}",
                             f"TVA {rl}", "", piece, "", f"{ttc_ - ht:.2f}", "", "", ""])
                tot["tva"] += ttc_ - ht
        enc = 0.0
        for pt, amt in sorted(pay.items()):
            if abs(amt) < 0.005:
                continue
            pacc = acc.get(_PAYKEY.get(pt, ""), acc["autres"])
            if amt > 0:
                rows.append([date, journal, pacc, pt, pt, "", piece, f"{amt:.2f}", "", "", "", ""])
            else:
                rows.append([date, journal, pacc, pt, pt + " (rendu)", "", piece, "", f"{-amt:.2f}", "", "", ""])
            enc += amt
        tot["enc"] += enc
        if cre_acc is not None:
            # lettrage F<n°> sur la créance 411 -> solde au règlement du client
            lettr = f"F{cre_fnum}" if cre_fnum else ""
            rows.append([date, journal, str(cre_acc), "Créance client",
                         f"Créance {cre_fnum:07d} · {cre_nm}", "", piece,
                         f"{cre_amt:.2f}", "", "", "", lettr])
            tot["creance"] += cre_amt
            ec = round(ttcsum - enc - cre_amt, 2)
        else:
            ec = round(ttcsum - enc, 2)
        if abs(ec) >= 0.01:
            rows.append([date, journal, ecart_acc, "Écart de caisse / avance",
                         ("Avance/dépôt" if ec < 0 else "Écart de caisse"), "", piece,
                         (f"{ec:.2f}" if ec > 0 else ""), (f"{-ec:.2f}" if ec < 0 else ""),
                         "", "", ""])
            tot["ecart"] += ec

    def piece_for(date, suffix=""):
        # identifiant de lot en tête -> tout le lot est repérable/supprimable d'un bloc
        # (séparateur ASCII '-' pour l'import Pennylane)
        return f"{batch_code}-{pfx}{date[8:10]}{date[5:7]}{date[2:4]}{suffix}"

    for dt in sorted(agg):
        emit(dt, piece_for(dt), agg[dt]["vat"], agg[dt]["pay"])
    for c in sorted(crean, key=lambda x: (x["date"], x["fnum"])):
        emit(c["date"], piece_for(c["date"], f"-F{c['fnum']:07d}"),
             c["vat"], c["pay"], c["acc"], c["recv"], c["nm"], c["fnum"])

    deb = sum(float(r[7]) for r in rows if r[7])
    cred = sum(float(r[8]) for r in rows if r[8])
    return {
        "header": HEADER, "rows": rows, "unresolved": [],
        "n_tickets": n_tickets, "n_agg": len(agg), "n_creances": len(crean),
        "ca_ttc": round(tot["ca_ht"] + tot["tva"], 2),
        "ca_ht": round(tot["ca_ht"], 2), "tva": round(tot["tva"], 2),
        "encaisse": round(tot["enc"], 2), "creances": round(tot["creance"], 2),
        "ecart": round(tot["ecart"], 2),
        "balanced": abs(deb - cred) < 0.05, "debit": round(deb, 2), "credit": round(cred, 2),
    }
