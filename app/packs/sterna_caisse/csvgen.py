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
           "Ticket restaurant": "ticket_resto", "Titre restaurant": "ticket_resto",
           "Chèque": "autres", "Cheque": "autres"}   # chèques comptabilisés dans le compte AUTRES (41125X04)

# Modes de paiement dont CHAQUE encaissement doit être une ligne dédiée (pas d'agrégat
# journalier) : on garde le détail (n° de chèque, client…) dans le libellé pour le rapprochement.
_DETAIL_MODES = {"Chèque", "Cheque"}


def _cheque_no(p):
    """N° de chèque (ou réf. utile) d'un paiement ticketpaymentdata, si l'API l'expose
    (les noms de champ varient). None si rien d'exploitable."""
    for k in ("checkNumber", "chequeNumber", "checkNum", "chequeNum", "checkReference",
              "chequeReference", "paymentReference", "checkRef"):
        v = p.get(k)
        if v not in (None, "", 0, "0"):
            return str(v).strip()[:24]
    return None


def _detail_label(mode, no, client, prefix=""):
    """Libellé enrichi d'un encaissement détaillé : « Chèque n°1234 · DUPONT »."""
    s = (prefix + mode) if prefix else mode
    if no:
        s += f" n°{no}"
    if client:
        s += f" · {client}"
    return s


def _resolver(company_id: int, cfg: dict):
    """Construit le routeur de créances depuis la table de correspondance (DB).

    Renvoie (resolve, diagnose) :
      resolve(pfx, coid, cid) -> (numéro de compte 411, nom) ou (None, None) ;
      diagnose(pfx, coid)     -> {name, siret, pennylane_name, reason} expliquant
      PRÉCISÉMENT ce qui manque pour router la créance (au pré-vol bloquant).
    """
    with Session(engine) as s:
        rows = s.exec(select(ClientAccount).where(
            ClientAccount.company_id == company_id)).all()
    info = {(r.establishment, r.toporder_company_id): r for r in rows}
    # Index GLOBAL par companyId : un companyId TopOrder est GLOBAL (une société = un client
    # Pennylane = un compte 411, peu importe la boulangerie vendeuse). Si un client est synchronisé
    # sous UNE boulangerie (ex. VIVO ENERGY sous Sainte-Marie) et qu'une AUTRE lui vend (La
    # Possession), TopOrder ne le liste pas dans la 2e -> on retombe sur le même 411 via ce mapping.
    by_coid = {}        # companyId -> ligne AVEC compte 411 (pour router)
    by_coid_any = {}    # companyId -> n'importe quelle ligne (pour le diagnostic)
    for r in rows:
        if not r.toporder_company_id:
            continue
        by_coid_any.setdefault(r.toporder_company_id, r)
        if r.account_411:
            by_coid.setdefault(r.toporder_company_id, r)

    def resolve(pfx, coid, cid):
        if coid and coid != ZERO:
            r = info.get((pfx, coid))
            if not (r and r.account_411):
                r = by_coid.get(coid)          # même client vu sous une AUTRE boulangerie
            if r and r.account_411:
                return r.account_411, r.toporder_name or "client"
            ie = config.INTER_ETAB.get(coid)   # vente inter-boulangeries (établissement du groupe)
            if ie:
                return ie["account"], ie["name"]
            return None, None
        if cid and cid != ZERO:
            return cfg["est"][pfx]["b2c_commun"], "Particulier"
        return None, None

    def diagnose(pfx, coid):
        if not coid or coid == ZERO:
            return {"name": None, "siret": None, "pennylane_name": None,
                    "reason": "facture sans référence client (ni société ni particulier) côté TopOrder"}
        ie = config.INTER_ETAB.get(coid)       # établissement du groupe (résolu en dur, pas via la synchro)
        if ie:
            return {"name": ie["name"], "siret": ie.get("siret"), "pennylane_name": ie["name"],
                    "reason": "établissement du groupe (vente inter-boulangeries) — mappé"}
        r = info.get((pfx, coid)) or by_coid_any.get(coid)   # connu sous une autre boulangerie ?
        if r is None:
            return {"name": None, "siret": None, "pennylane_name": None,
                    "reason": "client absent de la table de correspondance — resynchroniser les clients"}
        base = {"name": r.toporder_name, "siret": r.siret, "pennylane_name": r.pennylane_name}
        if not r.siret:
            return {**base, "reason": "pas de SIRET côté TopOrder → impossible de matcher Pennylane (saisir le SIRET dans TopOrder)"}
        if not r.account_411:
            return {**base, "reason": f"client présent mais sans compte client Pennylane (411) — statut « {r.status or '?'} » (créer/lier le compte dans Pennylane)"}
        return {**base, "reason": f"incohérent — statut « {r.status or '?'} »"}

    return resolve, diagnose


def build_toslt(establishment, date_from, date_to, company_id,
                batch_code, company_code, toslf_batch_code=None, on_progress=None) -> dict:
    """Construit les lignes des CSV TOSLT (caisse) ET TOSLF (reclassement factures),
    en un seul pull. Ne touche à rien si `unresolved` non vide."""
    est = config.establishments(company_code)[establishment]
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
    resolve, diagnose = _resolver(company_id, cfg)

    journal = ecfg["journal_tickets"]                  # ex. TOSLT
    ca_acc = cfg["ca_anonyme"]                          # 70101
    tva_acc = cfg["tva"]
    acc = ecfg
    ecart_acc = acc["ecart"]

    import time
    tk2fac, op2fac = _fac_ticketids(client, shop_id)
    to = int(time.mktime(time.strptime(date_to, "%Y-%m-%d"))) + 4 * 3600 + 24 * 3600

    agg = defaultdict(lambda: {"vat": defaultdict(lambda: [0.0, 0.0]),
                               "pay": defaultdict(float), "detail": []})
    factures = []       # CA facturé : créance par facture (TTC plein), par date de TICKET
    reglements = []     # règlements : 1 ligne par PAIEMENT de facture, par date de PAIEMENT
    fac_client = {}     # fnum -> (compte 411, nom) des factures de la période (routage des règlements)
    unresolved = []
    n_tickets = 0
    tot_ttc = 0.0                    # CA TTC de TOUS les tickets (= Z, pour le cadrage)
    pay_total = defaultdict(float)   # encaissements par mode (= total ticketpaymentdata = Z)
    fac_pay = defaultdict(float)     # encaissements SUR FACTURES par mode
    fac_pay_detail = []              # liste des règlements de factures (date, facture, client, mode, montant)
    vat_total = defaultdict(lambda: [0.0, 0.0])  # taux -> [HT, TTC] (pour le compte rendu)

    # ---- PASSE 1 : CA depuis les TICKETS DE VENTE (par date de ticket) ----
    # On ne lit PAS les paiements ici : ils viennent de la passe 2 (par date de paiement),
    # ce qui permet de capter les règlements de factures antérieures et d'exclure les
    # paiements hors période (qui restent en créance). Cf. modèle CEGID de TopOrder.
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
            fnum = tk2fac.get(tk.get("id")) or (tk.get("rootTicketId") and tk2fac.get(tk["rootTicketId"]))
            coid, cid = tk.get("companyId"), tk.get("customerId")
            has_client = (coid and coid != ZERO) or (cid and cid != ZERO)
            if fnum and has_client:        # FACTURÉ avec client -> créance 411 (TTC plein)
                a, nm = resolve(pfx, coid, cid)
                if not a:                  # client PRO connu mais sans compte 411 -> pré-vol bloquant
                    unresolved.append({"date": dd, "company_id": coid, "customer_id": cid,
                                       "facture": f"{int(fnum):07d}", "amount": round(ttc, 2),
                                       **diagnose(pfx, coid)})
                    continue
                factures.append({"date": dd, "fnum": int(fnum), "acc": a, "nm": nm,
                                 "vat": dict(vat), "ttc": round(ttc, 2),
                                 "b2b": bool(coid and coid != ZERO)})
                fac_client[int(fnum)] = (a, nm)
            else:             # NON facturé OU facture SANS client (= vente comptoir) -> CA anonyme
                d = agg[dd]
                for r, (ht, t) in vat.items():
                    d["vat"][r][0] += ht
                    d["vat"][r][1] += t
        if on_progress:
            on_progress(n_tickets, f"Passe 1/2 — CA (tickets de vente) · {n_tickets} tickets")
        if pmax < date_from:
            break
        frm += len(b)
        if pages > 600:
            break

    # ---- PASSE 2 : ENCAISSEMENTS depuis ticketpaymentdata (par date de PAIEMENT) ----
    # Lien paiement -> facture par rootTicketId -> tk2fac (capte les règlements de factures
    # ANTÉRIEURES et nette le rendu de monnaie sur la même pièce). Total = Z de la synthèse.
    frm, pages = 0, 0
    while True:
        b = _get(client, f"/ppe/ticketpaymentdata/shop/{shop_id}", To=to,
                 PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        pages += 1
        pmin = "9999"
        for p in b:
            pdate = _day(p.get("timestamp"))
            if pdate:
                pmin = min(pmin, pdate)
            if not pdate or not (date_from <= pdate <= date_to):
                continue
            amt = float(p.get("paymentAmount") or 0)
            if abs(amt) < 0.005:
                continue
            pt = p.get("paymentType") or "?"
            pay_total[pt] += amt
            fnum = tk2fac.get(p.get("rootTicketId"))
            a = nm = None
            if fnum:
                coid, cid = p.get("companyId"), p.get("customerId")
                if (coid and coid != ZERO) or (cid and cid != ZERO):
                    a, nm = resolve(pfx, coid, cid)
                    if not a:              # client PRO connu mais sans compte 411 -> pré-vol bloquant
                        unresolved.append({"date": pdate, "company_id": coid, "customer_id": cid,
                                           "facture": f"{int(fnum):07d}", "amount": round(amt, 2),
                                           **diagnose(pfx, coid)})
                        continue
                elif int(fnum) in fac_client:   # paiement sans client mais facture connue (période)
                    a, nm = fac_client[int(fnum)]
            # chèques (et modes « détaillés ») : on garde n° de chèque + client pour le libellé
            det = None
            if pt in _DETAIL_MODES:
                cli = nm
                if not cli:
                    coid, cid = p.get("companyId"), p.get("customerId")
                    if (coid and coid != ZERO) or (cid and cid != ZERO):
                        _, cli = resolve(pfx, coid, cid)
                det = {"no": _cheque_no(p), "client": cli}
            if fnum and a:    # RÈGLEMENT de facture : 1 ligne par paiement, lettré F<n°>
                reglements.append({"date": pdate, "fnum": int(fnum), "acc": a, "nm": nm,
                                   "mode": pt, "amount": amt, "detail": det})
                fac_pay[pt] += amt
                fac_pay_detail.append({"date": pdate, "fnum": int(fnum), "nm": nm,
                                       "mode": pt, "amount": round(amt, 2)})
            elif det is not None:   # chèque anonyme -> ligne DÉDIÉE dans l'écriture du jour
                agg[pdate]["detail"].append({"mode": pt, "amount": amt,
                                             "no": det["no"], "client": det["client"]})
            else:             # encaissement anonyme (incl. facture sans client) -> agrégat du jour
                agg[pdate]["pay"][pt] += amt
        if on_progress:
            on_progress(n_tickets, f"Passe 2/2 — encaissements (paiements) · page {pages}")
        if pmin < date_from:
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

    def emit_agg(date, piece, vat, pay, detail=()):
        """Écriture journalière ANONYME : CA + encaissements + écart de caisse.
        Les modes « détaillés » (chèques) sont une LIGNE PAR PAIEMENT (n° + client au
        libellé) au lieu d'un agrégat, pour le rapprochement."""
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
        for dd in detail:                    # chèques : 1 ligne dédiée, libellé enrichi
            amt = round(dd["amount"], 2)
            if abs(amt) < 0.005:
                continue
            pacc = acc.get(_PAYKEY.get(dd["mode"], ""), acc["autres"])
            lbl = _detail_label(dd["mode"], dd.get("no"), dd.get("client"))
            if amt > 0:
                rows.append([date, journal, pacc, dd["mode"], lbl, "", piece, f"{amt:.2f}", "", cat_family, cat_label, ""])
            else:
                rows.append([date, journal, pacc, dd["mode"], lbl + " (rendu)", "", piece, "", f"{-amt:.2f}", cat_family, cat_label, ""])
            enc += amt
        tot["enc"] += enc
        ec = round(ttcsum - enc, 2)
        if abs(ec) >= 0.01:
            rows.append([date, journal, ecart_acc, "Écart de caisse / avance",
                         ("Avance/dépôt" if ec < 0 else "Écart de caisse"), "", piece,
                         (f"{ec:.2f}" if ec > 0 else ""), (f"{-ec:.2f}" if ec < 0 else ""),
                         cat_family, cat_label, ""])
            tot["ecart"] += ec

    def emit_facture(date, piece, fnum, acc411, nm, vat):
        """Écriture par FACTURE : CA (crédit 70101/TVA) + débit 411 du BRUT TTC
        (lettrage F<n°>). Les RÈGLEMENTS (crédit 411) viennent de la passe paiements
        (par date de paiement) -> le solde 411 restant = créance impayée à l'arrêté."""
        lettr = f"F{fnum}"
        ttcsum = _ca_lines(date, piece, vat)
        if ttcsum >= 0:
            rows.append([date, journal, str(acc411), "Client - facture",
                         f"Facture {fnum:07d} · {nm}", "", piece,
                         f"{ttcsum:.2f}", "", cat_family, cat_label, lettr])
        else:
            rows.append([date, journal, str(acc411), "Client - avoir",
                         f"Avoir {fnum:07d} · {nm}", "", piece,
                         "", f"{-ttcsum:.2f}", cat_family, cat_label, lettr])
        tot["creance"] += ttcsum

    jp = ecfg["journal_payments"]      # journal d'encaissements dédié (TOSLP/TOSMP/TOLPP)
    pay_batch = f"{pfx}P{batch_code[len(pfx) + 1:]}"   # lot encaissements (ex. SLT09 -> SLP09)

    def emit_reglement(reg):
        """Encaissement NET d'une facture (regroupé par facture+mode+jour, rendu monnaie
        déjà netté) dans le JOURNAL DE PAIEMENT dédié : débit trésorerie (espèces/CB/…) +
        crédit 411 (libellé « Règlement … F<n°> » -> lettré complet/partiel à l'étape 6).
        Réduit le solde 411 de la facture (de la période OU antérieure)."""
        d, fnum, amt, pt = reg["date"], reg["fnum"], reg["amount"], reg["mode"]
        det = reg.get("detail")          # chèque : n° de chèque pour le rapprochement
        treso = _detail_label(pt, (det or {}).get("no"), None, prefix="Règlement ") + f" F{fnum:07d}"
        lettr = f"F{fnum}"
        piece = f"{pfx}{d[8:10]}{d[5:7]}{d[2:4]}-R{fnum:07d} #{pay_batch}"
        pacc = acc.get(_PAYKEY.get(pt, ""), acc["autres"])
        if amt >= 0:
            rows.append([d, jp, pacc, pt, treso, "", piece,
                         f"{amt:.2f}", "", cat_family, cat_label, ""])
            rows.append([d, jp, str(reg["acc"]), "Règlement client",
                         f"Règlement F{fnum:07d} · {reg['nm']}", "", piece, "", f"{amt:.2f}",
                         cat_family, cat_label, lettr])
        else:   # solde net négatif (trop-perçu / avoir réglé) -> sens inversé
            rows.append([d, jp, pacc, pt, treso, "", piece,
                         "", f"{-amt:.2f}", cat_family, cat_label, ""])
            rows.append([d, jp, str(reg["acc"]), "Règlement client",
                         f"Règlement F{fnum:07d} · {reg['nm']}", "", piece, f"{-amt:.2f}", "",
                         cat_family, cat_label, lettr])
        tot["enc"] += amt
        tot["creance"] -= amt          # le règlement réduit le solde 411 ouvert

    def piece_for(date, suffix=""):
        # pièce du jour en tête (lisible pour le rapprochement espèces/CB/…),
        # identifiant de lot en queue avec « # ». ex. « SM280526 #SMT03 »
        return f"{pfx}{date[8:10]}{date[5:7]}{date[2:4]}{suffix} #{batch_code}"

    for dt in sorted(agg):
        emit_agg(dt, piece_for(dt), agg[dt]["vat"], agg[dt]["pay"], agg[dt]["detail"])
    for f in sorted(factures, key=lambda x: (x["date"], x["fnum"])):
        emit_facture(f["date"], piece_for(f["date"], f"-F{f['fnum']:07d}"),
                     f["fnum"], f["acc"], f["nm"], f["vat"])
    # encaissements NETS : regroupés par (jour, facture, compte 411, mode) -> 1 ligne nette
    # (le rendu monnaie d'un paiement espèces est absorbé : +50 / -23 = +27).
    reg_net, reg_nm = defaultdict(float), {}
    reg_detail = []                  # chèques : 1 ligne par paiement (n° conservé), non nettés
    for r in reglements:
        if r["mode"] in _DETAIL_MODES:
            reg_detail.append(r)
            continue
        k = (r["date"], r["fnum"], r["acc"], r["mode"])
        reg_net[k] += r["amount"]
        reg_nm[k] = r["nm"]
    for (d, fnum, a, pt), amt in sorted(reg_net.items()):
        if abs(round(amt, 2)) < 0.005:
            continue
        emit_reglement({"date": d, "fnum": fnum, "acc": a, "nm": reg_nm[(d, fnum, a, pt)],
                        "mode": pt, "amount": round(amt, 2)})
    for r in sorted(reg_detail, key=lambda x: (x["date"], x["fnum"])):
        if abs(round(r["amount"], 2)) < 0.005:
            continue
        emit_reglement({**r, "amount": round(r["amount"], 2)})

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
    n_toslp = sum(1 for r in rows if r[1] == jp)
    return {
        "header": HEADER, "rows": rows, "unresolved": [],
        "n_toslf": len(toslf_rows), "n_toslp": n_toslp, "journal_payments": jp,
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


def build_kk(establishment, date_from, date_to, company_id, batch_code, company_code, on_progress=None):
    """Génération KOOKABURA (labo interne, 100 % B2B, modèle FACTURE — pas de caisse).
    Une seule passe : on lit les FACTURES (totalPriceHTByVATRate fiable) dont le ticket
    est dans la période, et par facture on book TOKKT (CA + TVA crédit + débit 411
    boulangerie, créance TTC, lettré F<n°>) et TOKKF (reclassement HT 70101 -> 7012).
    Pas d'analytique, pas d'encaissement. Même forme de `res` que build_toslt."""
    import time
    est = config.establishments(company_code)[establishment]
    pfx, shop_id = est["pfx"], est["shop_id"]
    cfg = config.resolve(company_code)
    ecfg = cfg["est"][pfx]
    ca_acc, b2b_acc, tva_acc = cfg["ca_anonyme"], cfg["ca_b2b"], cfg["tva"]
    jt, jf = ecfg["journal_tickets"], ecfg["journal_factures"]   # TOKKT, TOKKF
    b2b = config.clients(company_code)["b2b"]
    client = toporder.for_establishment(establishment)
    if client is None:
        raise RuntimeError(f"Clé TopOrder absente pour {establishment} ({toporder.env_var_for(establishment)})")

    to = int(time.mktime(time.strptime(date_to, "%Y-%m-%d"))) + 4 * 3600 + 24 * 3600
    # tickets -> (companyId, date) fiables, par ticketId/rootTicketId
    tinfo, frm, n_tickets = {}, 0, 0
    while True:
        b = _get(client, f"/ppe/ticket/shop/{shop_id}", To=to, PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        pmax = "0"
        for w in b:
            tk = w.get("ticket") or {}
            dd = _day(tk.get("timestamp"))
            pmax = max(pmax, dd or "0")
            if dd and date_from <= dd <= date_to:
                n_tickets += 1
                if tk.get("id"):
                    tinfo[tk["id"]] = (tk.get("companyId"), dd)
                if tk.get("rootTicketId"):
                    tinfo.setdefault(tk["rootTicketId"], (tk.get("companyId"), dd))
        if on_progress:
            on_progress(n_tickets, f"pull tickets KK… {n_tickets}")
        if pmax < date_from:
            break
        frm += len(b)
        if frm > 6000:
            break
    # factures -> celles dont le ticket est dans la période
    facs, frm = [], 0
    while True:
        b = _get(client, f"/ppe/invoice/shop/{shop_id}", PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        facs += b
        frm += len(b)
        if frm > 4000:
            break
    inperiod = [f for f in facs if (f.get("ticketId") in tinfo or f.get("rootTicketId") in tinfo)]

    rows, unresolved = [], []
    ht_by_rate, tva_by_rate = defaultdict(float), defaultdict(float)
    n_fac = 0
    ca_ht_tot = tva_tot = ttc_tot = 0.0
    for f in sorted(inperiod, key=lambda x: x.get("continousSequence") or 0):
        num = int(f.get("continousSequence") or 0)
        coid, date = tinfo.get(f.get("ticketId")) or tinfo.get(f.get("rootTicketId"))
        perrate = {}
        for part in (f.get("totalPriceHTByVATRate") or "").split("|"):
            if ":" not in part:
                continue
            pct = int(part.split(":")[0]) / 100.0
            ht = int(part.split(":")[1]) / 100.0
            if abs(ht) >= 0.005:
                perrate[pct] = perrate.get(pct, 0.0) + ht
        if not perrate or abs(round(sum(perrate.values()), 2)) < 0.005:
            continue
        info = b2b.get(f"{pfx}:{coid}")
        if not info or not info.get("account"):
            unresolved.append({"date": date, "company_id": coid, "customer_id": None,
                               "facture": f"{num:07d}", "amount": round(sum(perrate.values()), 2),
                               "name": (info or {}).get("name"), "siret": None, "pennylane_name": None,
                               "reason": "boulangerie cliente non mappée (companyId KK inconnu) — vérifier le mapping KK"})
            continue
        acc411, nm = info["account"], info["name"]
        n_fac += 1
        lettr = f"F{num}"
        pieceT = f"KK{date[8:10]}{date[5:7]}{date[2:4]}-F{num:07d} #{batch_code}"
        pieceF = f"F{num:07d} #{batch_code}"
        # --- TOKKT : CA HT/taux + TVA (crédit) puis 411 boulangerie (débit TTC, lettré) ---
        ent_ht = ent_tva = 0.0
        for p, h in sorted(perrate.items()):
            rl = _rate_label(f"{p:g}")
            h2 = round(h, 2)
            if h2 >= 0:
                rows.append([date, jt, ca_acc, "Vente KK", f"CA HT {rl} · {nm}", rl, pieceT, "", f"{h2:.2f}", "", "", ""])
            else:
                rows.append([date, jt, ca_acc, "Avoir KK", f"Avoir CA HT {rl} · {nm}", rl, pieceT, f"{-h2:.2f}", "", "", "", ""])
            ht_by_rate[f"{p:g}"] += h2
            tva = round(h2 * p / 100, 2)
            tacc = tva_acc.get(f"{p:g}")
            if abs(tva) >= 0.005 and tacc:
                if tva >= 0:
                    rows.append([date, jt, tacc, "TVA collectée", f"TVA {rl} · {nm}", rl, pieceT, "", f"{tva:.2f}", "", "", ""])
                else:
                    rows.append([date, jt, tacc, "TVA collectée", f"Avoir TVA {rl} · {nm}", rl, pieceT, f"{-tva:.2f}", "", "", "", ""])
                tva_by_rate[f"{p:g}"] += tva
            ent_ht += h2
            ent_tva += tva
        ttc = round(ent_ht + ent_tva, 2)
        if ttc >= 0:
            rows.append([date, jt, acc411, "Client KK - facture", f"Facture {num:07d} · {nm}", "", pieceT, f"{ttc:.2f}", "", "", "", lettr])
        else:
            rows.append([date, jt, acc411, "Client KK - avoir", f"Avoir {num:07d} · {nm}", "", pieceT, "", f"{-ttc:.2f}", "", "", "", lettr])
        # --- TOKKF : reclassement HT 70101 -> 7012 ---
        if ent_ht >= 0:
            rows.append([date, jf, ca_acc, "Reclassement", f"Reclassement {num:07d} · {nm}", "", pieceF, f"{ent_ht:.2f}", "", "", "", ""])
            rows.append([date, jf, b2b_acc, "CA B2B", f"CA B2B {num:07d}", "", pieceF, "", f"{ent_ht:.2f}", "", "", "", ""])
        else:
            rows.append([date, jf, ca_acc, "Reclassement avoir", f"Reclassement avoir {num:07d} · {nm}", "", pieceF, "", f"{-ent_ht:.2f}", "", "", "", ""])
            rows.append([date, jf, b2b_acc, "CA B2B avoir", f"CA B2B avoir {num:07d}", "", pieceF, f"{-ent_ht:.2f}", "", "", "", ""])
        ca_ht_tot += ent_ht
        tva_tot += ent_tva
        ttc_tot += ttc

    deb = sum(float(r[7]) for r in rows if r[7])
    cred = sum(float(r[8]) for r in rows if r[8])
    ca_ht, tva, ca_ttc = round(ca_ht_tot, 2), round(tva_tot, 2), round(ttc_tot, 2)
    n_toslf = sum(1 for r in rows if r[1] == jf)
    return {
        "header": HEADER, "rows": ([] if unresolved else rows), "unresolved": unresolved,
        "n_tickets": n_tickets, "n_factures": n_fac, "n_b2b": n_fac, "n_b2c": 0, "n_reglements": 0,
        "n_agg": 0, "n_toslf": n_toslf, "n_creances": n_fac,
        "ca_ttc": ca_ttc, "ca_ht": ca_ht, "tva": tva,
        "payments": {}, "fac_payments": {}, "fac_payment_detail": [],
        "ht_by_rate": {k: round(v, 2) for k, v in ht_by_rate.items()},
        "tva_by_rate": {k: round(v, 2) for k, v in tva_by_rate.items()},
        "encaisse": 0.0, "creances": ca_ttc, "ecart": 0.0,
        "balanced": abs(deb - cred) < 0.05, "debit": round(deb, 2), "credit": round(cred, 2),
    }
