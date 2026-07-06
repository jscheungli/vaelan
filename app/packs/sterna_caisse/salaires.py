"""Comptes 421 (salariés) — liste des comptes par société.

Vue RÉSERVÉE au rôle « salaires » (comptable paie), isolée du reste. Le détail d'un compte
réutilise `payments.account_ledger` (grand-livre par exercice, générique). STERNA + KOOKABURA.

Pennylane ne filtre pas les comptes par préfixe de numéro (opérateurs gte/lt refusés) : on
pagine TOUS les comptes et on garde les 421* -> mis en cache (les salariés changent rarement).
"""
import time

from app.core.connectors import pennylane

_CACHE = {}      # company_code -> (timestamp, liste)
_TTL = 1800      # 30 min


def list_accounts(company_code, force=False):
    """Tous les comptes 421* de la société : [{number, label(=nom du salarié)}]."""
    now = time.time()
    hit = _CACHE.get(company_code)
    if hit and not force and (now - hit[0]) < _TTL:
        return hit[1]
    pl = pennylane.for_company(company_code)
    if not pl:
        return []
    out, cur, pages = [], None, 0
    while pages < 80:
        params = {"limit": 100}
        if cur:
            params["cursor"] = cur
        d = pl.get("/ledger_accounts", **params)
        for a in (d.get("items") or []):
            n = str(a.get("number") or "")
            if n.startswith("421"):
                out.append({"number": n, "label": a.get("label") or ""})
        pages += 1
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    # le compte collectif « 421 » en tête, puis les salariés par nom
    out.sort(key=lambda x: (len(x["number"]) > 3, (x["label"] or "").lower(), x["number"]))
    _CACHE[company_code] = (now, out)
    return out
