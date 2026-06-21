"""Suivi des paiements (étape 7) — rapprochement encours TopOrder ↔ Pennylane par client.

TopOrder ne permet pas (API) de saisir les règlements reçus en compta. Cette page
aide la personne en charge à voir, par client : l'encours facturé impayé (Pennylane),
ce qui est en attente de facturation (TopOrder, mois courant / antérieur), l'exposition
totale, l'écart, et la liste des VIREMENTS à reporter manuellement dans TopOrder (avec
traçabilité qui/quand). Clé client stable = compte 411 (les comptes sont company-wide).

Un job de synchro calcule l'instantané (ClientPayment) ; la page le lit (rapide) et le
détail client est relu en live (un seul compte).
"""
import time
from collections import defaultdict
from datetime import datetime, timedelta, date

from sqlmodel import Session, select

from app.core.db import engine
from app.core.connectors import pennylane, toporder
from app.models import Company, ClientPayment, PaymentReport
from . import config
from .lettrage import _client_accounts, _fac_from_label

_TZ = timedelta(hours=4)
TOL = 0.01
_ZERO = "00000000-0000-0000-0000-000000000000"
_PENDING = {0, 1, 3}        # DRAFT / WAITING_FOR_BILLING / PURCHASE_ORDER -> en attente de facturation
_BILLED_UNPAID = {4, 5}     # BILLED / PARTIALLY_PAID -> facturé non soldé (encours TopOrder)
_STATUS = {"DRAFT": 0, "WAITING_FOR_BILLING": 1, "QUOTE": 2, "PURCHASE_ORDER": 3,
           "BILLED": 4, "PARTIALLY_PAID": 5, "PAID": 6, "CREDIT_GENERATED": 7, "CANCELLED": 8}
_TYPE = {"CASH_REGISTER": 0, "ORDER": 1, "PORTAGE": 2, "PRODUCT_RETURN": 3}


def _norm(v, table):
    return v if isinstance(v, int) else table.get(str(v).upper(), -1)


def _company2account(company_code):
    """companyId TopOrder -> compte 411 (depuis le mapping client, clé « PFX:companyId »)."""
    out = {}
    for key, v in config.CLIENTS.get("b2b", {}).items():
        cid = key.split(":", 1)[1] if ":" in key else key
        if v.get("account"):
            out[cid] = v["account"]
    return out


# ----------------------------------------------------------------- côté Pennylane
def pennylane_client(company_code, account, pl=None):
    """Détail live d'un compte client 411 : créances ouvertes + paiements (dont virements).
    Sert au drill-in (un seul compte) et, agrégé, au job de synchro."""
    pl = pl or pennylane.for_company(company_code)
    acc = pl.find_account(account)
    if not acc:
        return None
    lines = pl.account_lines(acc["id"])
    today = (datetime.utcnow() + _TZ).date()

    # CUTOVER = bascule Kimayo -> TopOrder = date de la 1re FACTURE TopOrder du client.
    # Une facture TopOrder = une créance (débit) avec une réf. F<n°> (qu'on a bookée
    # depuis TopOrder). Avant cette date, c'est l'ère Kimayo (ancien logiciel) : on
    # ignore ces créances ET les virements antérieurs (on ne peut pas régler une
    # facture TopOrder pas encore émise). Règle métier : après bascule, plus de Kimayo.
    fac_dates = [_pdate(l.get("date")) for l in lines
                 if float(l.get("debit") or 0) > float(l.get("credit") or 0)
                 and _fac_from_label(l.get("label")) is not None]
    fac_dates = [d for d in fac_dates if d]
    cutover = min(fac_dates) if fac_dates else None

    open_creances, payments = [], []
    net = 0.0
    oldest = 0
    kimayo_open = 0
    kimayo_amount = 0.0
    for l in lines:
        d = _pdate(l.get("date"))
        deb = float(l.get("debit") or 0)
        cred = float(l.get("credit") or 0)
        is_open = not ((l.get("lettered_ledger_entry_lines") or {}).get("ids"))
        fnum = _fac_from_label(l.get("label"))
        # ère Kimayo (avant la 1re facture TopOrder) -> hors périmètre TopOrder
        if cutover is None or (d and d < cutover):
            if is_open and deb > cred:
                kimayo_open += 1
                kimayo_amount += deb - cred
            continue
        if is_open:
            net += deb - cred
        if deb > cred and is_open:                 # créance TopOrder ouverte
            age = (today - d).days if d else 0
            oldest = max(oldest, age)
            open_creances.append({"id": l["id"], "fnum": fnum, "amount": round(deb, 2),
                                  "date": l.get("date"), "age": age})
        if cred > deb:                             # encaissement (règlement caisse ou virement)
            is_vir = fnum is None                  # pas de réf. F<n°> -> virement (journal de banque)
            payments.append({"id": l["id"], "fnum": fnum, "amount": round(cred, 2),
                             "date": l.get("date"), "label": l.get("label") or "",
                             "is_virement": is_vir, "lettered": not is_open})
    open_creances.sort(key=lambda x: (x["date"] or ""))
    payments.sort(key=lambda x: (x["date"] or ""), reverse=True)
    return {"net": round(net, 2), "open_creances": open_creances, "payments": payments,
            "oldest_age": oldest, "cutover": cutover.isoformat() if cutover else None,
            "kimayo_open": kimayo_open, "kimayo_amount": round(kimayo_amount, 2)}


# palette des lettres de lettrage (reprend l'esprit coloré du grand-livre Pennylane)
_LCOLORS = ["#0d6efd", "#198754", "#d63384", "#fd7e14", "#6f42c1", "#0dcaf0",
            "#dc3545", "#20c997", "#6610f2", "#ffc107"]


def _letter(n):
    """0->A, 1->B, … 25->Z, 26->AA, 27->AB … (comme la colonne Lett. de Pennylane)."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _fiscal_year(d):
    """Année de DÉBUT de l'exercice (1er juillet → 30 juin) d'une date 'YYYY-MM-DD'.
    Ex. 2026-03-15 -> 2025 (exercice 2025-2026) ; 2025-09-01 -> 2025."""
    p = _pdate(d)
    if not p:
        return None
    return p.year if p.month >= 7 else p.year - 1


def account_ledger(company_code, account, ex=None, pl=None):
    """Reproduction FIDÈLE du grand-livre Pennylane d'un compte client 411, PAR EXERCICE
    (1er juillet → 30 juin). Une vue cumulée n'aurait aucun sens : chaque exercice, les
    à-nouveaux (journal AN) re-portent le solde précédent (sinon double comptage). On
    n'affiche donc que l'exercice choisi : à-nouveaux du 1er juillet + mouvements de l'année,
    solde courant repartant de 0. Colonnes Date · Jour. · Libellé · Nº pièce · Lett. · D · C · Solde."""
    pl = pl or pennylane.for_company(company_code)
    acc = pl.find_account(account)
    if not acc:
        return None
    raw = pl.account_lines(acc["id"])

    # exercices disponibles (à partir des dates) + exercice courant ; sélection
    today = (datetime.utcnow() + _TZ).date()
    cur_fy = today.year if today.month >= 7 else today.year - 1
    years = sorted({_fiscal_year(l.get("date")) for l in raw if _fiscal_year(l.get("date")) is not None} | {cur_fy},
                   reverse=True)
    try:
        selected = int(ex)
    except (TypeError, ValueError):
        selected = cur_fy
    if selected not in years:
        years = sorted(set(years) | {selected}, reverse=True)

    sub = [l for l in raw if _fiscal_year(l.get("date")) == selected]

    # enrichissement (pièce + journal) UNIQUEMENT sur l'exercice affiché
    try:
        jmap = pl.journals_map()
    except Exception:
        jmap = {}
    ecache = {}
    for l in sub:
        eid = (l.get("ledger_entry") or {}).get("id")
        if eid and eid not in ecache:
            try:
                ecache[eid] = pl.get_entry(eid)
            except Exception:
                ecache[eid] = {}

    # lettrage : groupes (frozenset des ids), lettres A/B/C… dans l'ordre d'apparition de l'exercice
    gletter = {}
    for l in sorted(sub, key=lambda x: (x.get("date") or "", x["id"])):
        ids = (l.get("lettered_ledger_entry_lines") or {}).get("ids")
        if ids and frozenset(ids) not in gletter:
            gletter[frozenset(ids)] = _letter(len(gletter))
    # net par groupe (sur TOUT le compte) -> distingue lettré COMPLET (net 0) vs PARTIEL
    id2 = {l["id"]: l for l in raw}
    gnet = {}
    for l in raw:
        ids = (l.get("lettered_ledger_entry_lines") or {}).get("ids")
        if ids and frozenset(ids) not in gnet:
            gnet[frozenset(ids)] = round(sum(float(id2[i].get("debit") or 0) - float(id2[i].get("credit") or 0)
                                             for i in ids if i in id2), 2)

    lines = []
    for l in sub:
        eid = (l.get("ledger_entry") or {}).get("id")
        ent = ecache.get(eid, {})
        ids = (l.get("lettered_ledger_entry_lines") or {}).get("ids")
        key = frozenset(ids) if ids else None
        letter = gletter.get(key) if key else None
        jr = l.get("journal")
        jcode = (jr.get("code") if isinstance(jr, dict) else None) \
            or jmap.get((ent.get("journal") or {}).get("id"))
        lines.append({
            "id": l["id"], "date": l.get("date"),
            "label": l.get("label") or ent.get("label") or "",
            "journal": jcode or "", "piece": ent.get("piece_number") or "",
            "debit": round(float(l.get("debit") or 0), 2),
            "credit": round(float(l.get("credit") or 0), 2),
            "letter": letter,
            "lcolor": _LCOLORS[(ord(letter[-1]) - 65) % len(_LCOLORS)] if letter else None,
            # lettré COMPLET (groupe soldé) vs PARTIEL (groupe non soldé) vs non lettré (letter None)
            "letter_full": bool(letter) and abs(gnet.get(key, 0.0)) < TOL,
        })
    lines.sort(key=lambda x: (x["date"] or "", x["id"]))
    bal = tot_d = tot_c = 0.0
    for x in lines:
        bal += x["debit"] - x["credit"]
        x["balance"] = round(bal, 2)
        tot_d += x["debit"]
        tot_c += x["credit"]
    return {"lines": lines, "total_debit": round(tot_d, 2), "total_credit": round(tot_c, 2),
            "solde": round(tot_d - tot_c, 2), "name": acc.get("label"), "number": account,
            "letterable": bool(acc.get("letterable")),
            "exercise": selected, "exercise_label": f"{selected}-{selected + 1}",
            "exercises": years, "prev": selected - 1, "next": selected + 1}


def _pdate(d):
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return None


# ----------------------------------------------------------------- côté TopOrder
def _toporder_pending(company_code, on_log=None):
    """Par companyId : (attente_courant, attente_anterieur, encours_toporder) en TTC.
    Pull borné (≈180 j, plafond de pages) des commandes différées, tous établissements."""
    floor = (datetime.utcnow() + _TZ).date() - timedelta(days=180)
    cur_month = (datetime.utcnow() + _TZ).date().replace(day=1)
    agg = defaultdict(lambda: {"cur": 0.0, "ant": 0.0, "enc": 0.0})
    for est_name, e in config.ESTABLISHMENTS.items():
        client = toporder.for_establishment(est_name)
        if client is None:
            continue
        shop = e["shop_id"]
        frm, pages = 0, 0
        stop = False
        while not stop and pages < 250:
            b = None
            for _ in range(5):
                try:
                    b = client.get(f"/ppe/order/full/shop/{shop}",
                                   PaginationFrom=frm, PaginationTo=frm + 9)
                    break
                except Exception:
                    time.sleep(2)
            if not b:
                break
            pages += 1
            for o in b:
                if _norm(o.get("type"), _TYPE) != 1:
                    continue                       # on ne garde que les commandes différées
                st = _norm(o.get("status"), _STATUS)
                cid = o.get("companyReferentId")
                if not cid or cid == _ZERO:
                    continue                       # B2C / anonyme : pas de suivi individuel ici
                amt = float(o.get("roundedBasketPrice") or o.get("basketPrice") or 0)
                od = _order_date(o)
                if od and od < floor:
                    stop = True                    # commandes triées récentes->anciennes
                    continue
                if st in _PENDING:
                    if od and od >= cur_month:
                        agg[cid]["cur"] += amt
                    else:
                        agg[cid]["ant"] += amt
                elif st in _BILLED_UNPAID:
                    agg[cid]["enc"] += amt
            frm += len(b)
            if len(b) < 10:
                break
        if on_log:
            on_log(f"TopOrder {e['pfx']} : {pages} pages lues")
    return agg


def _order_date(o):
    c = o.get("create")
    if isinstance(c, (int, float)) and c > 0:
        return datetime.utcfromtimestamp(c + 4 * 3600).date()
    d = str(o.get("date") or "")
    if len(d) >= 8 and d[:8].isdigit():
        return date(int(d[:4]), int(d[4:6]), int(d[6:8]))
    return None


# ----------------------------------------------------------------- job de synchro
def run_payments_sync(ctx, company_code):
    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError("clé Pennylane absente")
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
        if not company:
            raise RuntimeError(f"société {company_code} introuvable")
        reported = {r.ledger_entry_line_id for r in s.exec(select(PaymentReport).where(
            PaymentReport.company_id == company.id)).all()}

    accounts = _client_accounts(company_code)
    c2a = _company2account(company_code)
    a2c = defaultdict(list)
    for cid, acc in c2a.items():
        a2c[acc].append(cid)

    ctx.progress(0, 2, step="TopOrder : commandes en attente de facturation…")
    ctx.log("Lecture des commandes TopOrder (peut être long, 10/page)…")
    topo = _toporder_pending(company_code, on_log=ctx.log)

    ctx.progress(1, 2, step="Pennylane : encours par compte client…")
    now = datetime.utcnow() + _TZ
    b2c_accounts = {e["b2c_commun"] for e in config.resolve(company_code)["est"].values()}
    rows = []
    items = list(accounts.items())
    for idx, (acc, nm) in enumerate(items):
        if idx % 5 == 0:
            ctx.progress(1, 2, step=f"Pennylane : compte {idx + 1}/{len(items)}…")
        try:
            det = pennylane_client(company_code, acc, pl=pl)
        except Exception as e:
            ctx.log(f"⚠️ {acc} ({nm}) : {str(e)[:60]}")
            continue
        if det is None:
            continue
        kind = "b2c" if acc in b2c_accounts else "b2b"
        # agrégat TopOrder pour ce compte (somme des companyId qui pointent dessus)
        t = {"cur": 0.0, "ant": 0.0, "enc": 0.0}
        for cid in a2c.get(acc, []):
            ti = topo.get(cid)
            if ti:
                t["cur"] += ti["cur"]; t["ant"] += ti["ant"]; t["enc"] += ti["enc"]
        # client jamais passé sur TopOrder (aucune facture TopOrder ET rien en attente)
        # = pur legacy Kimayo -> hors périmètre de cette page.
        if det.get("cutover") is None and abs(t["cur"]) + abs(t["ant"]) + abs(t["enc"]) < TOL:
            continue
        vir = [p for p in det["payments"] if p["is_virement"]]
        vir_a_reporter = [p for p in vir if p["id"] not in reported]
        attente = round(t["cur"] + t["ant"], 2)
        rows.append({
            "account": acc, "name": nm, "kind": kind,
            "encours_pennylane": det["net"], "nb_open": len(det["open_creances"]),
            "oldest_age": det["oldest_age"],
            "virements_a_reporter": round(sum(p["amount"] for p in vir_a_reporter), 2),
            "nb_virements_a_reporter": len(vir_a_reporter),
            "encours_toporder": round(t["enc"], 2),
            "attente_fact_courant": round(t["cur"], 2),
            "attente_fact_anterieur": round(t["ant"], 2),
            "ecart": round(t["enc"] - det["net"], 2),
            "exposition": round(det["net"] + attente, 2),
        })

    # upsert
    with Session(engine) as s:
        existing = {r.account: r for r in s.exec(select(ClientPayment).where(
            ClientPayment.company_id == company.id)).all()}
        seen = set()
        for r in rows:
            seen.add(r["account"])
            cp = existing.get(r["account"]) or ClientPayment(company_id=company.id, account=r["account"])
            for k, v in r.items():
                setattr(cp, k, v)
            cp.synced_at = now
            cp.updated_at = datetime.utcnow()
            s.add(cp)
        # purge les comptes disparus
        for acc, cp in existing.items():
            if acc not in seen:
                s.delete(cp)
        s.commit()

    stamp = now.strftime("%d/%m/%Y %H:%M")
    n_ecart = sum(1 for r in rows if abs(r["ecart"]) > TOL)
    n_vir = sum(r["nb_virements_a_reporter"] for r in rows)
    tot_vir = round(sum(r["virements_a_reporter"] for r in rows), 2)
    ctx.log(f"✅ {len(rows)} clients · {n_ecart} avec écart · {n_vir} virement(s) à reporter ({tot_vir:.2f} €)")
    return f"✅ Synchro paiements — {len(rows)} clients, {n_vir} virement(s) à reporter ({tot_vir:.2f} €) — {stamp}"


def board(company_id, q="", flt="", sort="exposition", page=1, per_page=15):
    """Lecture paginée/filtrée/triée de l'instantané pour la page."""
    with Session(engine) as s:
        rows = s.exec(select(ClientPayment).where(ClientPayment.company_id == company_id)).all()
    synced = max((r.synced_at for r in rows if r.synced_at), default=None)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r.name or "").lower() or ql in r.account]
    if flt == "ecart":
        rows = [r for r in rows if abs(r.ecart) > TOL]
    elif flt == "virements":
        rows = [r for r in rows if r.nb_virements_a_reporter > 0]
    elif flt == "encours":
        rows = [r for r in rows if r.encours_pennylane > TOL]
    keyf = {"exposition": lambda r: -r.exposition, "encours": lambda r: -r.encours_pennylane,
            "ecart": lambda r: -abs(r.ecart), "age": lambda r: -r.oldest_age,
            "name": lambda r: (r.name or "").lower()}.get(sort, lambda r: -r.exposition)
    rows.sort(key=keyf)
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    sl = rows[(page - 1) * per_page: page * per_page]
    totals = {
        "clients": total,
        "encours": round(sum(r.encours_pennylane for r in rows), 2),
        "attente": round(sum(r.attente_fact_courant + r.attente_fact_anterieur for r in rows), 2),
        "ecart_n": sum(1 for r in rows if abs(r.ecart) > TOL),
        "vir_n": sum(r.nb_virements_a_reporter for r in rows),
        "vir_montant": round(sum(r.virements_a_reporter for r in rows), 2),
    }
    return {"rows": sl, "page": page, "pages": pages, "total": total,
            "synced": synced, "totals": totals}
