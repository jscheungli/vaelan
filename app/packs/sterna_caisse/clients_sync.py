"""Synchronisation des comptes clients PRO (table de correspondance).

3 portes (Vaelan ne fait que détecter/alerter ; on corrige dans TopOrder/Pennylane) :
  1. chaque société TopOrder (contactType=0) a-t-elle un SIRET ?  -> sinon no_siret
  2. ce SIRET a-t-il un jumeau Pennylane (reg_no) ?               -> sinon no_pennylane
  3. feu vert quand tout est « ok ».
On préserve les mappings historiques (account_411 déjà présent) ; on ne les
écrase que si on détecte une incohérence.
"""
import re
from datetime import datetime
from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import toporder, pennylane
from app.models import ClientAccount, Company
from . import config


def _siren(s):
    d = re.sub(r"\D", "", str(s or ""))
    return d[:9] if len(d) >= 9 else ""


def _seed_if_empty(company_id: int):
    """Première fois : on charge le mapping validé existant (clients.json)."""
    with Session(engine) as s:
        if s.exec(select(ClientAccount).where(ClientAccount.company_id == company_id)).first():
            return
        for key, v in config.CLIENTS["b2b"].items():
            pfx, coid = key.split(":", 1)
            s.add(ClientAccount(
                company_id=company_id, establishment=pfx, toporder_company_id=coid,
                toporder_name=v.get("name"), account_411=v.get("account"),
                status=("ok" if v.get("account") else "unknown"),
                note="seed mapping historique",
            ))
        s.commit()


def sync_clients(ctx, company_code="STERNA"):
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
    if not company:
        raise RuntimeError(f"société {company_code} introuvable")
    _seed_if_empty(company.id)

    # Pennylane : index SIREN -> customer_id
    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError("clé Pennylane absente (variable d'environnement)")
    ctx.log("Lecture des clients Pennylane…")
    pl_by_siren = {}
    cur = None
    while True:
        d = pl.get("/customers", **({"limit": 100, "cursor": cur} if cur else {"limit": 100}))
        for c in d.get("items", []):
            sir = _siren(c.get("reg_no"))
            if sir:
                pl_by_siren[sir] = {"id": c["id"], "name": c.get("name"),
                                    "acc_id": (c.get("ledger_account") or {}).get("id")}
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    ctx.log(f"{len(pl_by_siren)} clients Pennylane avec SIREN")

    counts = {"ok": 0, "no_siret": 0, "no_pennylane": 0, "incoherent": 0}
    n = 0
    for est_name, est in config.ESTABLISHMENTS.items():
        pfx = est["pfx"]
        ctx.progress(n, None, step=f"sync {pfx}…")
        comps = _pull_companies(est_name)
        with Session(engine) as s:
            existing = {r.toporder_company_id: r
                        for r in s.exec(select(ClientAccount).where(
                            ClientAccount.company_id == company.id,
                            ClientAccount.establishment == pfx)).all()}
            for c in comps:
                if c.get("contactType") not in (0, "0"):
                    continue  # particuliers ignorés (compte commun)
                coid = c["id"]
                row = existing.get(coid) or ClientAccount(
                    company_id=company.id, establishment=pfx, toporder_company_id=coid)
                row.toporder_name = c.get("name")
                siret = re.sub(r"\D", "", str(c.get("siret") or ""))
                row.siret = siret or None
                sir = _siren(siret)
                if not sir:
                    row.status = "no_siret"
                    row.note = "SIRET manquant côté TopOrder"
                else:
                    m = pl_by_siren.get(sir)
                    if m:
                        row.pennylane_customer_id = m["id"]
                        row.pennylane_name = m.get("name")
                        # résoudre le numéro de compte si pas déjà connu
                        if not row.account_411 and m.get("acc_id"):
                            num = _account_number(pl, m["acc_id"])
                            row.account_411 = num
                        row.status = "ok"
                        row.note = None
                    elif row.account_411:
                        row.status = "ok"   # lien historique conservé
                        row.note = "lien historique (non retrouvé par SIRET)"
                    else:
                        row.status = "no_pennylane"
                        row.note = "aucun client Pennylane avec ce SIRET"
                row.last_synced = datetime.utcnow()
                row.updated_at = datetime.utcnow()
                counts[row.status if row.status in counts else "incoherent"] = \
                    counts.get(row.status, 0) + 1
                s.add(row)
                n += 1
            s.commit()

    ctx.log(f"Résultat : {counts['ok']} ok · {counts['no_siret']} sans SIRET · "
            f"{counts['no_pennylane']} sans jumeau Pennylane")
    anomalies = counts["no_siret"] + counts["no_pennylane"]
    if anomalies:
        return f"{counts['ok']} ok, {anomalies} anomalie(s) à corriger"
    return f"Synchronisé — {counts['ok']} clients, tout est ok ✓"


def _pull_companies(est_name):
    client = toporder.for_establishment(est_name)
    if client is None:
        return []
    out, frm = [], 0
    while True:
        b = client.get(f"/ppe/contactcompany/shop/{config.ESTABLISHMENTS[est_name]['shop_id']}",
                       PaginationFrom=frm, PaginationTo=frm + 99)
        if not b:
            break
        out += b
        frm += len(b)
        if len(b) < 100 or frm > 3000:
            break
    return out


_ACC_CACHE = {}
def _account_number(pl, acc_id):
    if acc_id in _ACC_CACHE:
        return _ACC_CACHE[acc_id]
    try:
        num = pl.get(f"/ledger_accounts/{acc_id}").get("number")
    except Exception:
        num = None
    _ACC_CACHE[acc_id] = num
    return num
