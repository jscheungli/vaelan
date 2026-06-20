"""Moteur caisse Sterna : pull TopOrder + calcul du CA (base tickets).

Migré depuis gen_caisse_tickets. Le ticket est la vérité fiscale : CA = somme
de tous les tickets ; la part facturée non encaissée devient une créance.
Renvoie des données (pas de CSV ici — la génération CSV viendra ensuite).
"""
import time
from collections import defaultdict
from typing import Optional

from app.core.connectors import toporder
from . import config

ZERO = "00000000-0000-0000-0000-000000000000"


def _day(ts) -> Optional[str]:
    ts = str(ts or "")
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None


def _get(client, path, _tries=6, **params):
    last = None
    for _ in range(_tries):
        try:
            return client.get(path, **params)
        except Exception as e:  # 429 / timeouts -> on retente
            last = e
            time.sleep(2)
    raise last


def _fac_ticketids(client, shop_id):
    """ticketId/rootTicketId facturés -> n° de facture (continousSequence)."""
    tk2fac, frm = {}, 0
    while True:
        b = _get(client, f"/ppe/invoice/shop/{shop_id}", PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        for i in b:
            n = i.get("continousSequence")
            if i.get("ticketId"):
                tk2fac[i["ticketId"]] = n
            if i.get("rootTicketId"):
                tk2fac[i["rootTicketId"]] = n
        frm += len(b)
        if frm > 3000:
            break
    return tk2fac


def compute_ca(establishment: str, date_from: str, date_to: str, on_progress=None) -> dict:
    """on_progress(n_tickets, step) est appelé pendant le pull pour le suivi live."""
    est = config.ESTABLISHMENTS[establishment]
    shop_id = est["shop_id"]
    client = toporder.for_establishment(establishment)
    if client is None:
        raise RuntimeError(f"Clé TopOrder absente pour {establishment} "
                           f"({toporder.env_var_for(establishment)})")

    tk2fac = _fac_ticketids(client, shop_id)
    to = int(time.mktime(time.strptime(date_to, "%Y-%m-%d"))) + 4 * 3600 + 24 * 3600

    vat = defaultdict(lambda: [0.0, 0.0])   # taux -> [HT, TTC]
    pay = defaultdict(float)                # moyen de paiement -> net
    by_day = defaultdict(lambda: {"ttc": 0.0, "creances": 0.0})
    creances = []
    n_tickets = 0

    frm, pages = 0, 0
    while True:
        b = _get(client, f"/ppe/ticket/shop/{shop_id}", To=to, PaginationFrom=frm, PaginationTo=frm + 99)
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
            by_day[dd]["ttc"] += ttc
            recv = round(ttc - paysum, 2)
            facd = tk.get("id") in tk2fac or (tk.get("rootTicketId") in tk2fac)
            if facd and recv > 0.01:
                fnum = tk2fac.get(tk.get("id")) or tk2fac.get(tk.get("rootTicketId"))
                coid, cid = tk.get("companyId"), tk.get("customerId")
                creances.append({
                    "date": dd, "ticket_id": tk.get("id"),
                    "facture": (f"{int(fnum):07d}" if fnum else None),
                    "company_id": (coid if coid and coid != ZERO else None),
                    "customer_id": (cid if cid and cid != ZERO else None),
                    "amount": recv,
                })
                by_day[dd]["creances"] += recv
        if on_progress:
            on_progress(n_tickets, f"pull tickets… {n_tickets} traités")
        if pmax < date_from:
            break
        frm += len(b)
        if pages > 600:
            break

    ca_ht = round(sum(v[0] for v in vat.values()), 2)
    ca_ttc = round(sum(v[1] for v in vat.values()), 2)
    return {
        "establishment": establishment, "date_from": date_from, "date_to": date_to,
        "n_tickets": n_tickets,
        "ca_ht": ca_ht, "ca_ttc": ca_ttc, "tva": round(ca_ttc - ca_ht, 2),
        "ca_ht_by_rate": {r: round(v[0], 2) for r, v in sorted(vat.items())},
        "ht_by_rate": {r: round(v[0], 2) for r, v in sorted(vat.items())},
        "tva_by_rate": {r: round(v[1] - v[0], 2) for r, v in sorted(vat.items())},
        "payments": {k: round(v, 2) for k, v in sorted(pay.items())},
        "n_creances": len(creances), "creances_total": round(sum(c["amount"] for c in creances), 2),
        "creances": creances,
        "by_day": {d: {"ttc": round(v["ttc"], 2), "creances": round(v["creances"], 2)} for d, v in sorted(by_day.items())},
    }
