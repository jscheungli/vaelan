"""Synchronisation des comptes clients PRO (table de correspondance).

3 portes (Vaelan ne fait que détecter/alerter ; on corrige dans TopOrder/Pennylane) :
  1. chaque société TopOrder (contactType=0) a-t-elle un SIRET ?       -> sinon no_siret
  2. ce SIRET est-il l'« Identifiant client » d'un client Pennylane ?  -> sinon no_pennylane
  3. feu vert quand tout est « ok » (jumeau trouvé + compte 411).
Clé de matching = **SIRET (14 chiffres) ↔ external_reference Pennylane** (le champ
« Identifiant client », unique). PAS le SIREN : il est partagé entre établissements
d'une même société, donc ambigu. On préserve les mappings 411 historiques.
"""
import re
from datetime import datetime
from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import toporder, pennylane
from app.models import ClientAccount, Company
from . import config


def _key(s):
    """Normalise un identifiant de matching : SIRET (chiffres) OU code assigné
    (ex. asso « ASDR »). On garde l'alphanumérique en majuscules, on retire
    espaces/ponctuation (« 818 173 … » == « 81817359300018 »)."""
    return re.sub(r"[^0-9A-Za-z]", "", str(s or "")).upper()


def _seed_if_empty(company_id: int, company_code: str):
    """Première fois : on charge le mapping validé existant (clients.json)."""
    with Session(engine) as s:
        if s.exec(select(ClientAccount).where(ClientAccount.company_id == company_id)).first():
            return
        for key, v in config.clients(company_code).get("b2b", {}).items():
            pfx, coid = key.split(":", 1)
            s.add(ClientAccount(
                company_id=company_id, establishment=pfx, toporder_company_id=coid,
                toporder_name=v.get("name"), account_411=v.get("account"),
                status="unknown",   # rien n'est « ok » tant qu'une synchro n'a pas vérifié
                note="seed mapping historique",
            ))
        s.commit()


def sync_clients(ctx, company_code="STERNA"):
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
    if not company:
        raise RuntimeError(f"société {company_code} introuvable")
    _seed_if_empty(company.id, company_code)

    # Pennylane : index « Identifiant client » (external_reference = SIRET) -> client
    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError("clé Pennylane absente (variable d'environnement)")
    ctx.log("Lecture des clients Pennylane…")
    pl_by_ref = {}
    cur = None
    while True:
        d = pl.get("/customers", **({"limit": 100, "cursor": cur} if cur else {"limit": 100}))
        for c in d.get("items", []):
            ref = _key(c.get("external_reference"))
            if ref:
                pl_by_ref[ref] = {"id": c["id"], "name": c.get("name"),
                                  "reg_no": c.get("reg_no"),
                                  "external_ref": c.get("external_reference"),
                                  "acc_id": (c.get("ledger_account") or {}).get("id")}
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    ctx.log(f"{len(pl_by_ref)} clients Pennylane avec un Identifiant client (SIRET)")

    counts = {"ok": 0, "no_siret": 0, "no_pennylane": 0, "incoherent": 0}
    n = 0
    seen = set()       # (pfx, company_id) traités lors de cette synchro
    pulled = set()     # établissements réellement lus (pour réconcilier sans risque)
    for est_name, est in config.establishments(company_code).items():
        pfx = est["pfx"]
        ctx.progress(n, None, step=f"sync {pfx}…")
        try:
            comps = _pull_companies(est_name, company_code)
        except Exception as e:
            ctx.log(f"⚠️ {pfx} : lecture TopOrder impossible ({e}) — établissement ignoré")
            continue
        if not comps:
            ctx.log(f"⚠️ {pfx} : aucune société TopOrder remontée "
                    f"(clé absente ? voir {toporder.env_var_for(est_name)}) — non réconcilié")
            continue   # pull vide = probable erreur -> ne pas toucher aux statuts existants
        pulled.add(pfx)
        with Session(engine) as s:
            existing = {r.toporder_company_id: r
                        for r in s.exec(select(ClientAccount).where(
                            ClientAccount.company_id == company.id,
                            ClientAccount.establishment == pfx)).all()}
            for c in comps:
                if c.get("contactType") not in (0, "0"):
                    continue  # particuliers ignorés (compte commun)
                coid = c["id"]
                seen.add((pfx, coid))
                row = existing.get(coid) or ClientAccount(
                    company_id=company.id, establishment=pfx, toporder_company_id=coid)
                row.toporder_name = c.get("name")
                siret = _key(c.get("siret"))
                row.siret = siret or None
                # Cascade STRICTE — clé = SIRET/identifiant TopOrder ↔ Identifiant client Pennylane :
                #   pas d'identifiant            -> no_siret
                #   identifiant sans jumeau PL   -> no_pennylane
                #   jumeau PL mais sans 411      -> incoherent
                #   jumeau PL + compte 411       -> ok
                m = pl_by_ref.get(siret) if siret else None
                if m:  # on tient le lien Pennylane à jour même si le statut n'est pas ok
                    row.pennylane_customer_id = m["id"]
                    row.pennylane_name = m.get("name")
                    row.pennylane_reg_no = m.get("reg_no")
                    row.pennylane_external_ref = m.get("external_ref")
                    if not row.account_411 and m.get("acc_id"):
                        row.account_411 = _account_number(pl, m["acc_id"])
                if not siret:
                    row.status, row.note = "no_siret", "SIRET manquant côté TopOrder"
                elif not m:
                    row.status, row.note = "no_pennylane", \
                        "aucun client Pennylane avec ce SIRET comme Identifiant client"
                elif not row.account_411:
                    row.status, row.note = "incoherent", "client Pennylane trouvé mais sans compte 411"
                else:
                    row.status, row.note = "ok", None
                row.last_synced = datetime.utcnow()
                row.updated_at = datetime.utcnow()
                counts[row.status if row.status in counts else "incoherent"] = \
                    counts.get(row.status, 0) + 1
                s.add(row)
                n += 1
            s.commit()

    # Réconciliation : une ligne d'un établissement BIEN lu mais jamais revue dans
    # TopOrder ne doit pas garder un statut périmé (ex. seed « ok »). On la marque
    # honnêtement « absent_toporder » sans toucher au mapping 411.
    reconciled = 0
    with Session(engine) as s:
        for r in s.exec(select(ClientAccount).where(
                ClientAccount.company_id == company.id)).all():
            if r.establishment in pulled and (r.establishment, r.toporder_company_id) not in seen:
                if r.status != "absent_toporder":
                    r.status = "absent_toporder"
                    r.note = "présent dans le mapping mais introuvable dans TopOrder"
                    r.updated_at = datetime.utcnow()
                    s.add(r)
                    reconciled += 1
        s.commit()
    if reconciled:
        ctx.log(f"{reconciled} client(s) du mapping introuvable(s) dans TopOrder → « absent TopOrder »")

    ctx.log(f"Résultat : {counts['ok']} ok · {counts['no_siret']} sans SIRET · "
            f"{counts['no_pennylane']} sans jumeau Pennylane · {counts['incoherent']} incohérents")
    anomalies = counts["no_siret"] + counts["no_pennylane"] + counts["incoherent"]
    if anomalies:
        return f"{counts['ok']} ok, {anomalies} anomalie(s) à corriger"
    return f"Synchronisé — {counts['ok']} clients, tout est ok ✓"


def _pull_companies(est_name, company_code="STERNA"):
    client = toporder.for_establishment(est_name)
    if client is None:
        return []
    out, frm = [], 0
    while True:
        b = client.get(f"/ppe/contactcompany/shop/{config.establishments(company_code)[est_name]['shop_id']}",
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
