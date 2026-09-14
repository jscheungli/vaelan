"""Moteur du prévisionnel La Parisienne — fonctions pures (aucun accès base).

Entrées : cfg (dict, voir config.py) et actuals = {"stores": {code: {mois: {...}}}, "entities": {code: {mois: {...}}}}.

Modèle :
  Main Business Income[m] = run-rate désaisonnalisé × coefficient saisonnier[m] × (1 + tendance)^(k/12)
  Food, other opex        = ratios (% CA) ; Labor, Rent = montants mensuels lissés ; D&A = montant mensuel
  EBITDA (Store)          = income − food − labor − rent − other opex
  EBITDA (After G&A)      = Σ EBITDA (Store) − G&A ; Profit before tax = EBITDA (After G&A) − D&A − financial − operation taxes
  Corporate income tax    = taux × résultat trimestriel positif (le mois suivant la fin du trimestre) ; Profit after tax
  Cash[m]                 = Cash[m−1] + profit after tax + D&A ± prêts (remboursement / tirage) ± événements ± décalage loyers ± friction
Textes produits (alertes, diagnostics) en anglais : ils alimentent les livrables destinés aux associés.
"""
import statistics as st
from datetime import datetime
from typing import Dict, List, Optional

from . import config


# ----------------------------------------------------------------- utilitaires mois
def month_add(m: str, k: int) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    mo += k
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return f"{y:04d}-{mo:02d}"


def month_diff(a: str, b: str) -> int:
    """b − a en mois."""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def mlabel(m: str, long: bool = False) -> str:
    if not m:
        return ""
    names = config.MONTHS_FR_LONG if long else config.MONTHS_FR
    return f"{names[int(m[5:7]) - 1]} {m[:4]}"


def mlabel_en(m: str, long: bool = False, short_year: bool = False) -> str:
    if not m:
        return ""
    names = config.MONTHS_EN_LONG if long else config.MONTHS_EN
    return f"{names[int(m[5:7]) - 1]} {m[2:4] if short_year else m[:4]}"


def median(xs: List[float], default: float = 0.0) -> float:
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else default


def other_of(v: dict) -> float:
    """Autres charges d'exploitation (hors loyer et amortissements) = 5501 − 5501.09 − 5501.14 − 5501.15."""
    return (v.get("opex_5501") or 0) - (v.get("rent") or 0) - (v.get("amort") or 0) - (v.get("depr") or 0)


def da_of(v: dict) -> float:
    return (v.get("amort") or 0) + (v.get("depr") or 0)


def ebitda_of(v: dict) -> float:
    """EBITDA magasin au sens du modèle (≈ « EBITDA (Store) » du management report à 1 % près)."""
    return (v.get("revenue") or 0) - (v.get("food") or 0) - (v.get("labor") or 0) - (v.get("rent") or 0) - other_of(v)


def last_actual_month(actuals: dict) -> Optional[str]:
    """Dernier mois où l'on a à la fois les P&L magasins et les balances des entités."""
    ent = actuals.get("entities") or {}
    ms = [set(d) for d in ent.values() if d]
    common = set.intersection(*ms) if ms else set()
    stores_months = set()
    for d in (actuals.get("stores") or {}).values():
        stores_months |= set(d)
    if common:
        both = common & stores_months
        return max(both) if both else max(common)
    return max(stores_months) if stores_months else None


# ----------------------------------------------------------------- saisonnalité
def _normalize(coefs: List[float]) -> List[float]:
    mean = sum(coefs) / 12
    return [c / mean for c in coefs] if mean else [1.0] * 12


def season_from_history(hist: Dict[str, dict], years: int = 2, as_of: str = None) -> Optional[List[float]]:
    """Coefficients mensuels = moyenne, sur les dernières années civiles complètes, de CA mois / CA moyen de l'année."""
    by_year: Dict[str, Dict[int, float]] = {}
    for m, v in hist.items():
        if as_of and m > as_of:
            continue
        rev = v.get("revenue") or 0
        if rev > 0:
            by_year.setdefault(m[:4], {})[int(m[5:7])] = rev
    full = sorted(y for y, d in by_year.items() if len(d) == 12)[-max(1, years):]
    if not full:
        return None
    idx = [[] for _ in range(12)]
    for y in full:
        mean = sum(by_year[y].values()) / 12
        for mo, rev in by_year[y].items():
            idx[mo - 1].append(rev / mean)
    return _normalize([st.mean(x) for x in idx])


def group_history(actuals: dict, codes=("ZHY", "BFC")) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for c in codes:
        for m, v in (actuals.get("stores", {}).get(c) or {}).items():
            out.setdefault(m, {"revenue": 0.0})
            out[m]["revenue"] += v.get("revenue") or 0
    return out


def resolve_season(store: dict, hist: dict, general: dict, group_hist: dict, as_of: str = None):
    mode = store.get("season") or "auto"
    years = int(general.get("season_years") or 2)
    if mode == "custom" and store.get("season_custom") and len(store["season_custom"]) == 12:
        return _normalize([float(x) for x in store["season_custom"]]), "custom monthly profile"
    if mode in config.SEASON_PROFILES:
        return list(config.SEASON_PROFILES[mode]), {"school": "school-calendar profile", "flat": "no seasonality"}.get(mode, mode)
    if mode == "auto":
        s = season_from_history(hist, years, as_of)
        if s:
            return s, f"store history ({years} full calendar years)"
    s = season_from_history(group_hist, years, as_of)
    if s:
        return s, "group profile (ZHY + BFC)" + ("" if mode == "group" else " — store history too short")
    return list(config.SEASON_PROFILES["school"]), "school-calendar profile (fallback)"


# ----------------------------------------------------------------- calibrage
def calibrate_store(store: dict, hist: dict, general: dict, as_of: str, group_hist: dict) -> dict:
    season, season_src = resolve_season(store, hist, general, group_hist, as_of)
    months = sorted(m for m, v in hist.items() if m <= as_of and (v.get("revenue") or 0) > 0)
    n = int(general.get("calib_months") or 6)
    recent = months[-n:]
    c = {"season": season, "season_src": season_src, "months_used": recent, "n_history": len(months)}
    if store.get("runrate_annual"):
        c["runrate"], c["runrate_src"] = float(store["runrate_annual"]) / 12, "entered (annual revenue)"
    elif months:
        last12 = months[-12:]
        sa = sorted((hist[m]["revenue"] or 0) / season[int(m[5:7]) - 1] for m in last12)
        used = sa[1:-1] if len(sa) >= 6 else sa
        c["runrate"] = st.mean(used)
        c["runrate_src"] = f"auto: seasonally-adjusted average of the last {len(last12)} months" + (", excluding extremes" if len(sa) >= 6 else "")
    else:
        c["runrate"], c["runrate_src"] = 0.0, "no history — enter an annual revenue"
    c["last12_revenue"] = sum(hist[m]["revenue"] or 0 for m in months[-12:]) if months else 0.0

    def ratio(key, fn):
        if store.get(key) is not None and store.get(key) != "":
            return float(store[key]) / 100, "entered"
        vals = [fn(hist[m]) / hist[m]["revenue"] for m in recent if (hist[m].get("revenue") or 0) > 0]
        return (median(vals), f"auto: median of {len(vals)} months") if vals else (0.0, "no history")

    def amount(key, fn):
        if store.get(key) is not None and store.get(key) != "":
            return float(store[key]), "entered"
        vals = [fn(hist[m]) for m in recent]
        return (median(vals), f"auto: median of {len(vals)} months") if vals else (0.0, "no history")

    c["food_pct"], c["food_src"] = ratio("food_pct", lambda v: v.get("food") or 0)
    c["other_pct"], c["other_src"] = ratio("other_pct", other_of)
    c["labor"], c["labor_src"] = amount("labor", lambda v: v.get("labor") or 0)
    c["rent"], c["rent_src"] = amount("rent", lambda v: v.get("rent") or 0)
    c["da"], c["da_src"] = amount("da_monthly", da_of)
    c["labor_pct_equiv"] = c["labor"] / c["runrate"] if c["runrate"] else 0.0
    c["ebitda_pct_equiv"] = (1 - c["food_pct"] - c["other_pct"] - (c["labor"] + c["rent"]) / c["runrate"]) if c["runrate"] else 0.0
    return c


def calibrate_ho(actuals: dict, general: dict, as_of: str) -> (float, str):
    if general.get("ho_monthly"):
        return float(general["ho_monthly"]), "entered"
    hist = actuals.get("stores", {}).get("HO") or {}
    months = sorted(m for m in hist if m <= as_of)[-int(general.get("calib_months") or 6):]
    vals = [hist[m].get("ga") or 0 for m in months]
    return (median(vals), f"auto: median of the last {len(vals)} months") if vals else (0.0, "no history")


# ----------------------------------------------------------------- prêts
def loan_schedule(loan: dict, as_of: str, months: List[str], gap_override=None, renew_override=None, repay_lead: int = 1) -> dict:
    """-> {"repay": {mois: −montant}, "draw": {mois: +montant}, "outstanding": {mois: encours en début de mois}, "events": [...]}
    Le remboursement est affiché `repay_lead` mois AVANT le mois de renouvellement (maturity) : la banque veut être remboursée
    avant de renouveler ; le nouveau tirage tombe le mois du renouvellement (+ renew_gap)."""
    principal = float(loan.get("principal") or 0)
    term = int(loan.get("term_months") or 12)
    renew = bool(loan.get("renew")) if renew_override is None else bool(renew_override)
    gap = int(loan.get("renew_gap") or 0) if gap_override is None else int(gap_override)
    amt_renew = float(loan.get("renew_amount") or principal)
    maturity = loan.get("maturity") or months[0]
    outstanding = principal
    while maturity <= as_of:          # échéance déjà passée : prêt supposé roulé (sinon remboursé)
        if not renew:
            outstanding = 0.0
            break
        outstanding = amt_renew
        maturity = month_add(maturity, term)
    repay, draw, outs, evs = {}, {}, {}, []
    pending = []
    lead = max(0, int(repay_lead or 0))
    repay_month = max(month_add(maturity, -lead), months[0])
    for m in months:
        outs[m] = outstanding
        for pm, pa in list(pending):
            if pm == m:
                draw[m] = draw.get(m, 0) + pa
                outstanding = pa
                evs.append({"loan": loan.get("label"), "entity": loan.get("entity"), "month": m, "type": "drawdown", "amount": pa, "renewal_of": maturity})
                pending.remove((pm, pa))
        if m == repay_month and outstanding > 0:
            repay[m] = repay.get(m, 0) - outstanding
            evs.append({"loan": loan.get("label"), "entity": loan.get("entity"), "month": m, "type": "repayment", "amount": -outstanding,
                        "renewal": month_add(maturity, gap) if renew else None})
            outstanding = 0.0
            if renew:
                pending.append((month_add(maturity, gap), amt_renew))
            maturity = month_add(maturity, term)
            repay_month = month_add(maturity, -lead)
    return {"repay": repay, "draw": draw, "outstanding": outs, "events": evs}


def loan_events_pre(sched, loans):
    return [x for l in loans for x in sched[l["id"]]["events"]]


# ----------------------------------------------------------------- prévisionnel
ENTITY_KEYS = ("revenue", "ebitda_stores", "ga", "ebitda_after_ga", "da", "fees", "interest", "fin", "tax_ops", "pbt", "cit", "pat",
               "rent_adj", "friction", "capex", "loan_repay", "loan_draw", "loans", "cca", "interco", "other", "delta", "cash")


def forecast(cfg: dict, actuals: dict, as_of: str = None, horizon: int = None, overrides: dict = None, label: str = None) -> dict:
    g = dict(cfg.get("general") or {})
    ov = dict(overrides or {})
    as_of = as_of or last_actual_month(actuals)
    if not as_of:
        raise ValueError("Aucun historique importé : importer d'abord un Board Management Report.")
    horizon = int(ov.get("horizon") or horizon or g.get("horizon") or 24)
    months = [month_add(as_of, k) for k in range(1, horizon + 1)]
    wage_infl = float(g.get("wage_inflation_pct") or 0) / 100
    cost_infl = float(g.get("cost_inflation_pct") or 0) / 100
    entities = list(config.ENTITIES)
    group_hist = group_history(actuals)

    # ---- magasins
    stores_cfg = [dict(s) for s in cfg.get("stores") or []]
    for s in stores_cfg:
        if s["code"] in (ov.get("stores_active") or {}):
            s["active"] = bool(ov["stores_active"][s["code"]])
    active_codes = {s["code"] for s in stores_cfg if s.get("active")}
    events = [e for e in (cfg.get("events") or []) if e.get("active", True) and (not e.get("store") or e["store"] in active_codes)]
    if ov.get("extra_events"):
        events += list(ov["extra_events"])
    res_stores, calib = {}, {}
    for s in stores_cfg:
        if not s.get("active"):
            continue
        code = s["code"]
        hist = actuals.get("stores", {}).get(code) or {}
        c = calibrate_store(s, hist, g, as_of, group_hist)
        rr = c["runrate"] * (1 + float(ov.get("revenue_pct") or 0) / 100)
        growth = float(s.get("growth_pct") or 0) / 100 + float(ov.get("growth_pp") or 0) / 100
        food = c["food_pct"] + float(ov.get("food_pp") or 0) / 100
        other = c["other_pct"] + float(ov.get("other_pp") or 0) / 100
        labor0 = c["labor"] * (1 + float(ov.get("labor_pct") or 0) / 100)
        rent0, da0 = c["rent"], c["da"]
        c["effective"] = {"runrate": rr, "runrate_annual": rr * 12, "growth_pct": growth * 100, "food_pct": food * 100, "other_pct": other * 100,
                          "labor": labor0, "rent": rent0, "da": da0, "entity": s.get("entity"), "name": s.get("name"), "opened": s.get("opened"),
                          "preopening_months": int(s.get("preopening_months") or 0), "ramp_months": int(s.get("ramp_months") or 0),
                          "ramp_start_pct": float(s.get("ramp_start_pct") or 100), "rent_period": int(s.get("rent_period") or 1), "note": s.get("note") or ""}
        rows = {k: [0.0] * horizon for k in ("revenue", "food", "labor", "rent", "other", "ebitda", "da", "rent_cash")}
        opened, closed = s.get("opened"), s.get("closed")
        pre_n = int(s.get("preopening_months") or 0)
        ramp_n = int(s.get("ramp_months") or 0)
        ramp_start = float(s.get("ramp_start_pct") or 100) / 100
        period = int(s.get("rent_period") or 1)
        first_pay = s.get("rent_first_month")
        for i, m in enumerate(months):
            k = i + 1
            is_open = (not opened or m >= opened) and (not closed or m < closed)
            preopen = bool(opened) and m < opened and 0 < month_diff(m, opened) <= pre_n
            rev = 0.0
            if is_open and rr > 0:
                rev = rr * c["season"][int(m[5:7]) - 1] * (1 + growth) ** (k / 12)
                if opened and ramp_n > 0:
                    j = month_diff(opened, m)
                    if 0 <= j < ramp_n:
                        rev *= ramp_start + (1 - ramp_start) * j / ramp_n
                for e in events:
                    if e.get("store") != code:
                        continue
                    if e.get("category") == "fermeture" and e.get("month") == m:
                        rev = 0.0
                    elif e.get("category") == "ca_pct":
                        if (e.get("permanent") and m >= e.get("month", "")) or (not e.get("permanent") and e.get("month") == m):
                            rev *= 1 + float(e.get("pct") or 0) / 100
            carry = is_open or preopen
            food_v, other_v = rev * food, rev * other
            labor_v = labor0 * (1 + wage_infl) ** (k / 12) if carry else 0.0
            rent_v = rent0 * (1 + cost_infl) ** (k / 12) if carry else 0.0
            rows["revenue"][i], rows["food"][i], rows["labor"][i], rows["rent"][i], rows["other"][i] = rev, food_v, labor_v, rent_v, other_v
            rows["ebitda"][i] = rev - food_v - labor_v - rent_v - other_v
            rows["da"][i] = da0 if is_open else 0.0
            if period > 1 and rent_v and first_pay:
                rows["rent_cash"][i] = rent_v * period if month_diff(first_pay, m) % period == 0 else 0.0
            else:
                rows["rent_cash"][i] = rent_v
        rows["entity"] = s.get("entity")
        rows["name"] = s.get("name")
        res_stores[code] = rows
        calib[code] = c

    # ---- siège, prêts, événements
    ho_monthly, ho_src = calibrate_ho(actuals, g, as_of)
    ho_monthly += float(ov.get("ga_delta") or 0)
    ho_entity = g.get("ho_entity") or "JZ"
    loans = [dict(l) for l in (cfg.get("loans") or []) if l.get("active")]
    sched = {l["id"]: loan_schedule(l, as_of, months, ov.get("loan_gap"), ov.get("loan_renew"), int(g.get("repay_lead", 1) or 0)) for l in loans}
    ev = {e: {m: {"capex": 0.0, "cca": 0.0, "loan": 0.0, "interco": 0.0, "other": 0.0} for m in months} for e in entities}
    applied = []
    capex_scale = float(ov.get("capex_scale", 1.0))
    for e in events:
        cat, m, ent = e.get("category"), e.get("month"), e.get("entity")
        if cat not in ("capex", "cca", "loan", "interco", "other") or m not in ev.get(ent, {}):
            continue
        amt = float(e.get("amount") or 0) * (capex_scale if cat == "capex" else 1.0)
        ev[ent][m][cat] += amt
        if cat == "interco" and e.get("counterparty") in ev:
            ev[e["counterparty"]][m]["interco"] -= amt
        applied.append({"month": m, "entity": ent, "category": cat, "amount": amt, "label": e.get("label") or "", "store": e.get("store") or ""})
    if ov.get("cca_amount") and ov.get("cca_month") in ev.get(ov.get("cca_entity") or "JZ", {}):
        ent = ov.get("cca_entity") or "JZ"
        ev[ent][ov["cca_month"]]["cca"] += float(ov["cca_amount"])
        applied.append({"month": ov["cca_month"], "entity": ent, "category": "cca", "amount": float(ov["cca_amount"]), "label": "Shareholder contribution (simulation)", "store": ""})

    # ---- entités
    res_ent = {}
    for e in entities:
        bal = (actuals.get("entities", {}).get(e) or {}).get(as_of) or {}
        opening = (bal.get("cash") or 0) + (bal.get("cash_on_hand") or 0)
        r = {k: [0.0] * horizon for k in ENTITY_KEYS}
        r["opening"] = opening
        r["opening_src"] = f"trial balance {as_of}" if bal else "no trial balance at the closing month (0)"
        fric = float((g.get("friction") or {}).get(e) or 0)
        q_acc = 0.0
        for i, m in enumerate(months):
            k = i + 1
            mine = [rows for rows in res_stores.values() if rows["entity"] == e]
            rev = sum(rows["revenue"][i] for rows in mine)
            eb = sum(rows["ebitda"][i] for rows in mine)
            da = sum(rows["da"][i] for rows in mine)
            rent_adj = sum(rows["rent"][i] - rows["rent_cash"][i] for rows in mine)
            ga = ho_monthly * (1 + cost_infl) ** (k / 12) if e == ho_entity else 0.0
            fees = rev * float(g.get("bank_fees_pct") or 0) / 100
            tax_ops = rev * float(g.get("tax_ops_pct") or 0) / 100
            interest = sum(sched[l["id"]]["outstanding"][m] * float(l.get("rate_pct") or 0) / 100 / 12 for l in loans if l.get("entity") == e)
            repay = sum(sched[l["id"]]["repay"].get(m, 0.0) for l in loans if l.get("entity") == e)
            drawn = sum(sched[l["id"]]["draw"].get(m, 0.0) for l in loans if l.get("entity") == e)
            ebitda_after = eb - ga
            pbt = ebitda_after - da - fees - interest - tax_ops
            q_acc += pbt
            cit = 0.0
            if int(m[5:7]) in (1, 4, 7, 10):     # impôt du trimestre écoulé, comptabilisé et payé le mois suivant
                cit = max(0.0, q_acc) * float(g.get("cit_rate_pct") or 0) / 100
                q_acc = 0.0
            pat = pbt - cit
            evm = ev[e][m]
            loans_total = repay + drawn + evm["loan"]
            delta = pat + da + rent_adj + fric + evm["capex"] + evm["cca"] + loans_total + evm["interco"] + evm["other"]
            vals = {"revenue": rev, "ebitda_stores": eb, "ga": ga, "ebitda_after_ga": ebitda_after, "da": da, "fees": fees, "interest": interest,
                    "fin": fees + interest, "tax_ops": tax_ops, "pbt": pbt, "cit": cit, "pat": pat, "rent_adj": rent_adj, "friction": fric,
                    "capex": evm["capex"], "loan_repay": repay + min(evm["loan"], 0.0), "loan_draw": drawn + max(evm["loan"], 0.0), "loans": loans_total,
                    "cca": evm["cca"], "interco": evm["interco"], "other": evm["other"], "delta": delta}
            for kk, v in vals.items():
                r[kk][i] = v
            r["cash"][i] = (r["cash"][i - 1] if i else opening) + delta
        res_ent[e] = r

    # ---- plan de financement automatique (apports d'associés en comptes ronds, remboursés dès que possible)
    floor = float(g.get("alert_group") or 0)
    rnd = float(g.get("contribution_round") or 300000) or 300000.0
    partners = int(g.get("partners") or 3)
    plan = []
    if g.get("auto_funding", True) and not ov.get("no_funding"):
        import math
        cash_g = sum(res_ent[e]["opening"] for e in entities)
        outstanding_by = {e: 0.0 for e in entities}
        for i, m in enumerate(months):
            cash_g += sum(res_ent[e]["delta"][i] for e in entities)
            if cash_g < floor:
                need = math.ceil((floor - cash_g) / rnd) * rnd
                # l'apport va à l'entité qui rembourse un prêt ce mois-ci (ou le suivant), sinon à la plus basse
                reason = next((x for x in loan_events_pre(sched, loans) if x["type"] == "repayment" and x["month"] in (m, month_add(m, 1))), None)
                ent = reason["entity"] if reason and reason.get("entity") in entities else min(entities, key=lambda e: res_ent[e]["cash"][i])
                plan.append({"month": m, "entity": ent, "amount": need, "type": "contribution",
                             "reason": (f"to repay the {reason['loan']} ahead of its renewal" if reason else "to keep the minimum cash position")})
                outstanding_by[ent] += need
                cash_g += need
            elif any(v > 0 for v in outstanding_by.values()):
                avail = cash_g - floor
                for ent in sorted(entities, key=lambda e: -outstanding_by[e]):
                    if outstanding_by[ent] <= 0 or avail <= 0:
                        continue
                    rep = min(outstanding_by[ent], math.floor(avail / rnd) * rnd)
                    if rep <= 0:
                        continue
                    # pas d'aller-retour : on ne rembourse que si, une fois remboursé, la trésorerie reste au-dessus
                    # du minimum pendant les 6 mois suivants (sinon on attend de pouvoir rembourser pour de bon)
                    look, ok = cash_g - rep, True
                    for j in range(i + 1, min(i + 7, horizon)):
                        look += sum(res_ent[e]["delta"][j] for e in entities)
                        if look < floor:
                            ok = False
                            break
                    if not ok:
                        continue
                    plan.append({"month": m, "entity": ent, "amount": -rep, "type": "repayment", "reason": "cash position restored"})
                    outstanding_by[ent] -= rep
                    cash_g -= rep
                    avail -= rep
        for x in plan:
            r = res_ent[x["entity"]]
            i = months.index(x["month"])
            r["cca"][i] += x["amount"]
            r["delta"][i] += x["amount"]
            applied.append({"month": x["month"], "entity": x["entity"], "category": "cca", "amount": x["amount"], "store": "",
                            "label": (f"Shareholder contribution — funding plan ({partners} × {x['amount']/partners/1000:,.0f} k)" if x["amount"] > 0
                                      else "Repayment of shareholder contribution — funding plan")})
        for e in entities:
            r = res_ent[e]
            for i in range(horizon):
                r["cash"][i] = (r["cash"][i - 1] if i else r["opening"]) + r["delta"][i]
        applied.sort(key=lambda x: x["month"])

    # ---- groupe & indicateurs
    grp = {k: [sum(res_ent[e][k][i] for e in entities) for i in range(horizon)] for k in ENTITY_KEYS}
    grp["opening"] = sum(res_ent[e]["opening"] for e in entities)
    grp["ebitda"] = list(grp["ebitda_after_ga"])
    grp["op_cash"] = [grp["pat"][i] + grp["da"][i] + grp["rent_adj"][i] + grp["friction"][i] for i in range(horizon)]
    imin = min(range(horizon), key=lambda i: grp["cash"][i])
    alerts = []
    thr_g = floor
    low_g = [months[i] for i in range(horizon) if grp["cash"][i] < thr_g - 0.5]
    if low_g:
        alerts.append({"level": "warn" if min(grp["cash"]) >= 0 else "danger", "scope": "group",
                       "text": f"Group cash below the {thr_g/1000:,.0f} k minimum for {len(low_g)} month(s) — low point {grp['cash'][imin]/1000:,.0f} k in {mlabel_en(months[imin], True)}."})
    loan_events = sorted([x for l in loans for x in sched[l["id"]]["events"]], key=lambda x: x["month"])
    for x in loan_events:
        if x["type"] == "repayment":
            alerts.append({"level": "info", "scope": "loan",
                           "text": f"{mlabel_en(x['month'], True)}: {x['loan']} ({x['entity']}) — {-x['amount']/1000:,.0f} k to repay"
                                   + (f"; renewal expected in {mlabel_en(x['renewal'], True)}." if x.get("renewal") else "; no renewal assumed.")})
    conclusion = []
    if not g.get("auto_funding", True) or ov.get("no_funding"):
        if low_g:
            short = floor - grp["cash"][imin]
            reason = next((x for x in loan_events if x["type"] == "repayment" and x["month"] in (months[imin], month_add(months[imin], 1), month_add(months[imin], -1))), None)
            conclusion.append(f"Without shareholder contributions, group cash falls below the {floor/1000:,.0f} k minimum for {len(low_g)} month(s): "
                              f"low point {grp['cash'][imin]/1000:,.0f} k in {mlabel_en(months[imin], True)}, i.e. {short/1000:,.0f} k short of the minimum"
                              + (f" — the {reason['loan']} repayment cannot be made while keeping the minimum cash position." if reason else "."))
            conclusion.append("Months below the minimum: " + ", ".join(mlabel_en(m, True) for m in low_g) + ".")
        else:
            conclusion.append(f"Group cash stays above the {floor/1000:,.0f} k minimum over the whole horizon (low point {grp['cash'][imin]/1000:,.0f} k in "
                              f"{mlabel_en(months[imin], True)}): no shareholder contribution is needed.")
    elif plan:
        for x in plan:
            if x["type"] == "contribution":
                conclusion.append(f"Shareholder contribution of {x['amount']/1000:,.0f} k ({partners} × {x['amount']/partners/1000:,.0f} k) in {mlabel_en(x['month'], True)} {x['reason']}.")
            else:
                conclusion.append(f"Repayment of {-x['amount']/1000:,.0f} k to the shareholders in {mlabel_en(x['month'], True)} ({x['reason']}).")
        tot_c = sum(x["amount"] for x in plan if x["amount"] > 0)
        tot_r = -sum(x["amount"] for x in plan if x["amount"] < 0)
        if tot_c > tot_r + 0.5:
            conclusion.append(f"{(tot_c - tot_r)/1000:,.0f} k of contributions remain outstanding at the end of the horizon.")
    else:
        conclusion.append(f"No shareholder contribution needed over the horizon: group cash stays above the {floor/1000:,.0f} k minimum "
                          f"(low point {grp['cash'][imin]/1000:,.0f} k in {mlabel_en(months[imin], True)}).")
    if g.get("auto_funding", True) and not ov.get("no_funding"):
        conclusion.append(f"Basis: minimum cash position of {floor/1000:,.0f} k, contributions in round amounts of {rnd/1000:,.0f} k ({partners} equal shares), "
                          f"repaid as soon as the cash position allows and no further need arises within six months.")
    n12 = min(12, horizon)
    kpis = {"cash_open": grp["opening"], "cash_min": grp["cash"][imin], "cash_min_month": months[imin], "cash_end": grp["cash"][-1],
            "revenue_12m": sum(grp["revenue"][:n12]), "ebitda_12m": sum(grp["ebitda_after_ga"][:n12]), "pat_12m": sum(grp["pat"][:n12]),
            "op_cash_12m": sum(grp["op_cash"][:n12]), "capex_total": sum(grp["capex"]), "cca_total": sum(grp["cca"]),
            "entity_end": {e: res_ent[e]["cash"][-1] for e in entities}, "entity_min": {e: min(res_ent[e]["cash"]) for e in entities}}
    return {"label": label or "", "as_of": as_of, "horizon": horizon, "months": months, "stores": res_stores, "entities": res_ent,
            "group": grp, "calibration": calib, "ho_monthly": ho_monthly, "ho_src": ho_src, "loan_schedule": loan_events,
            "loans": loans, "events_applied": sorted(applied, key=lambda x: x["month"]), "kpis": kpis, "alerts": alerts, "overrides": ov,
            "funding_plan": plan, "conclusion": conclusion, "general": g, "generated_at": datetime.utcnow().isoformat(timespec="seconds")}


def pl_rows(res: dict, with_entities: bool = True) -> List[dict]:
    """Lignes du tableau mensuel (libellés du Management Report) partagées par le PDF et la page web.
    Chaque ligne : label, values (signe d'affichage : charges en négatif), kind (store / total / line / grey), total (bool)."""
    grp, ents, H = res["group"], res["entities"], res["horizon"]
    neg = lambda xs: [-v for v in xs]
    add = lambda a, b: [a[i] + b[i] for i in range(H)]
    rows = []
    for c, s in res["stores"].items():
        rows.append({"label": f"Main Business Income — {c}", "values": s["revenue"], "kind": "store", "total": True})
    rows.append({"label": "Main Business Income (All Stores)", "values": grp["revenue"], "kind": "total", "total": True})
    for c, s in res["stores"].items():
        rows.append({"label": f"EBITDA (Store) — {c}", "values": s["ebitda"], "kind": "store", "total": True})
    rows.append({"label": "EBITDA (All Stores)", "values": grp["ebitda_stores"], "kind": "total", "total": True})
    rows.append({"label": "G&A Expenses", "values": neg(grp["ga"]), "kind": "line", "total": True})
    rows.append({"label": "EBITDA (After G&A)", "values": grp["ebitda_after_ga"], "kind": "total", "total": True})
    rows.append({"label": "Amortization & Depreciation", "values": neg(grp["da"]), "kind": "line", "total": True})
    rows.append({"label": "Financial Expenses", "values": neg(grp["fin"]), "kind": "line", "total": True})
    rows.append({"label": "Operation Taxes & Surcharges", "values": neg(grp["tax_ops"]), "kind": "line", "total": True})
    rows.append({"label": "Profit (After G&A, Before Tax)", "values": grp["pbt"], "kind": "total", "total": True})
    rows.append({"label": "Corporate Income Tax", "values": neg(grp["cit"]), "kind": "line", "total": True})
    rows.append({"label": "Company Profit (After Tax)", "values": grp["pat"], "kind": "total", "total": True})
    rows.append({"label": "Add back: Amortization & Depreciation", "values": grp["da"], "kind": "line", "total": True, "section": "cash"})
    adj = add(grp["rent_adj"], grp["friction"])
    if any(abs(v) > 0.5 for v in adj):
        rows.append({"label": "Rent payment timing / other adjustments", "values": adj, "kind": "line", "total": True})
    rows.append({"label": "Capital Expenditure", "values": grp["capex"], "kind": "line", "total": True})
    rows.append({"label": "Bank Loan Repayment", "values": grp["loan_repay"], "kind": "line", "total": True})
    rows.append({"label": "Bank Loan Drawdown", "values": grp["loan_draw"], "kind": "line", "total": True})
    if any(abs(v) > 0.5 for v in grp["cca"]):
        rows.append({"label": "Shareholder Contributions / Repayments", "values": grp["cca"], "kind": "line", "total": True})
    other = add(grp["interco"], grp["other"])
    rows.append({"label": "Deposits & Other Cash Items", "values": other, "kind": "line", "total": True})
    rows.append({"label": "Net Cash Flow", "values": grp["delta"], "kind": "total", "total": True})
    if with_entities:
        for e, r in ents.items():
            rows.append({"label": f"Cash — {config.ENTITIES.get(e, {}).get('latin', e).split(' — ')[0]}", "values": r["cash"], "kind": "grey", "total": False})
    rows.append({"label": "Cash (Group, End of Month)", "values": grp["cash"], "kind": "total", "total": False})
    return rows


_OV = [("revenue_pct", "revenue all stores {:+.0f} %"), ("growth_pp", "trend {:+.1f} pt/yr"), ("food_pp", "food cost {:+.1f} pt"),
       ("other_pp", "other opex {:+.1f} pt"), ("labor_pct", "labor cost {:+.0f} %"), ("ga_delta", "G&A {:+,.0f} RMB/month"),
       ("loan_gap", "loan renewal delayed by {:.0f} month(s)"), ("horizon", "horizon {:.0f} months")]


def describe_overrides(ov: dict) -> str:
    """Variantes d'une simulation, lisibles (vide si aucune)."""
    if not ov:
        return ""
    parts = []
    for key, fmt in _OV:
        v = ov.get(key)
        if v not in (None, "", 0, 0.0):
            parts.append(fmt.format(float(v)))
    if ov.get("capex_scale") not in (None, "", 1, 1.0):
        parts.append(f"capital expenditure × {float(ov['capex_scale']):.2f}")
    if ov.get("no_funding"):
        parts.append("no shareholder contribution")
    if ov.get("loan_renew") is True:
        parts.append("all bank loans renewed")
    elif ov.get("loan_renew") is False:
        parts.append("no bank loan renewed (stress case)")
    sa = ov.get("stores_active") or {}
    if sa:
        on = [c for c, v in sa.items() if v]
        parts.append("stores included: " + ", ".join(on) if on else "no store included")
    if ov.get("cca_amount"):
        parts.append(f"shareholder contribution {float(ov['cca_amount'])/1000:,.0f} k in {mlabel_en(ov.get('cca_month', ''))} ({ov.get('cca_entity', 'JZ')})")
    return " · ".join(parts)


# ----------------------------------------------------------------- écarts prévu / réalisé
def truncate(actuals: dict, upto: str) -> dict:
    return {"stores": {c: {m: v for m, v in d.items() if m <= upto} for c, d in (actuals.get("stores") or {}).items()},
            "entities": {c: {m: v for m, v in d.items() if m <= upto} for c, d in (actuals.get("entities") or {}).items()}}


def backtest_reference(cfg: dict, actuals: dict, month: str, horizon: int = 3) -> dict:
    prev = month_add(month, -1)
    ref = forecast(cfg, truncate(actuals, prev), as_of=prev, horizon=horizon, label=f"model recomputed at end of {mlabel_en(prev, True)} (data up to {prev})")
    ref["backtest"] = True
    return ref


def _pct(a, b):
    return (a / b - 1) if b else None


def variance(cfg: dict, actuals: dict, month: str, ref: dict) -> dict:
    if month not in ref.get("months", []):
        raise ValueError(f"Month {month} is not covered by the reference forecast.")
    i = ref["months"].index(month)
    prev = month_add(month, -1)
    g = cfg.get("general") or {}
    ho_entity = g.get("ho_entity") or "JZ"
    out = {"month": month, "reference": ref.get("label") or "", "backtest": bool(ref.get("backtest")), "as_of": ref.get("as_of"),
           "stores": [], "entities": {}, "group": {}, "proposals": [], "diagnostic": []}
    group_hist = group_history(actuals)
    stores_cfg = {s["code"]: s for s in cfg.get("stores") or []}
    tot = {"revenue_F": 0.0, "revenue_A": 0.0, "ebitda_stores_F": 0.0, "ebitda_stores_A": 0.0, "da_F": 0.0, "da_A": 0.0, "fin_F": 0.0, "fin_A": 0.0, "tax_F": 0.0, "tax_A": 0.0}
    for code, rows in ref["stores"].items():
        a = (actuals.get("stores", {}).get(code) or {}).get(month)
        F = {k: rows[k][i] for k in ("revenue", "food", "labor", "rent", "other", "ebitda", "da")}
        row = {"code": code, "name": rows.get("name") or code, "entity": rows.get("entity"), "F": F, "A": None, "D": None}
        tot["revenue_F"] += F["revenue"]; tot["ebitda_stores_F"] += F["ebitda"]; tot["da_F"] += F["da"]
        if a:
            A = {"revenue": a.get("revenue") or 0, "food": a.get("food") or 0, "labor": a.get("labor") or 0, "rent": a.get("rent") or 0,
                 "other": other_of(a), "ebitda": ebitda_of(a), "da": da_of(a)}
            row["A"] = A
            row["D"] = {k: A[k] - F[k] for k in F}
            row["Dpct"] = {k: _pct(A[k], F[k]) for k in F}
            row["ratios"] = {"food_F": F["food"] / F["revenue"] if F["revenue"] else None, "food_A": A["food"] / A["revenue"] if A["revenue"] else None,
                             "other_F": F["other"] / F["revenue"] if F["revenue"] else None, "other_A": A["other"] / A["revenue"] if A["revenue"] else None}
            margin_F = 1 - (row["ratios"]["food_F"] or 0) - (row["ratios"]["other_F"] or 0)
            row["bridge"] = {"ca": row["D"]["revenue"] * margin_F,
                             "food": -((row["ratios"]["food_A"] or 0) - (row["ratios"]["food_F"] or 0)) * A["revenue"],
                             "other": -((row["ratios"]["other_A"] or 0) - (row["ratios"]["other_F"] or 0)) * A["revenue"],
                             "labor": -row["D"]["labor"], "rent": -row["D"]["rent"]}
            c3F = c3A = 0.0
            n3 = 0
            for j in range(max(0, i - 2), i + 1):
                aj = (actuals.get("stores", {}).get(code) or {}).get(ref["months"][j])
                if aj:
                    c3F += rows["revenue"][j]; c3A += aj.get("revenue") or 0; n3 += 1
            row["cum3"] = {"n": n3, "F": c3F, "A": c3A, "pct": _pct(c3A, c3F)}
            tot["revenue_A"] += A["revenue"]; tot["ebitda_stores_A"] += A["ebitda"]; tot["da_A"] += A["da"]
            tot["fin_A"] += a.get("fin") or 0; tot["tax_A"] += a.get("tax_ops") or 0
        out["stores"].append(row)
    ho = (actuals.get("stores", {}).get("HO") or {}).get(month) or {}
    G = dict(tot)
    G["ga_F"] = sum(ref["entities"][e]["ga"][i] for e in ref["entities"])
    G["ga_A"] = ho.get("ga") or 0
    G["fin_F"] = sum(ref["entities"][e]["fin"][i] for e in ref["entities"])
    G["fin_A"] += ho.get("fin") or 0
    G["tax_F"] = sum(ref["entities"][e]["tax_ops"][i] for e in ref["entities"])
    G["cit_F"] = sum(ref["entities"][e]["cit"][i] for e in ref["entities"])
    G["cit_A"] = sum((((actuals.get("stores", {}).get(c) or {}).get(month) or {}).get("income_tax") or 0) for c in list(ref["stores"]) + ["HO"])
    G["ebitda_F"] = G["ebitda_stores_F"] - G["ga_F"]
    G["ebitda_A"] = G["ebitda_stores_A"] - G["ga_A"]
    G["pbt_F"] = G["ebitda_F"] - G["da_F"] - G["fin_F"] - G["tax_F"]
    G["pbt_A"] = G["ebitda_A"] - G["da_A"] - G["fin_A"] - G["tax_A"]
    G["pat_F"] = G["pbt_F"] - G["cit_F"]
    G["pat_A"] = G["pbt_A"] - G["cit_A"]
    G["cash_F"] = ref["group"]["cash"][i]
    G["cash_prev_F"] = ref["group"]["cash"][i - 1] if i else ref["group"]["opening"]
    out["group"] = G
    cashA_tot = cashPrev_tot = 0.0
    have_cash = True
    for e, r in ref["entities"].items():
        b = (actuals.get("entities", {}).get(e) or {}).get(month)
        bp = (actuals.get("entities", {}).get(e) or {}).get(prev)
        ent = {"cash_F": r["cash"][i], "delta_F": r["delta"][i], "cash_prev_F": r["cash"][i - 1] if i else r["opening"],
               "op_F": r["pat"][i] + r["da"][i] + r["rent_adj"][i] + r["friction"][i]}
        if b and bp:
            cashA = (b.get("cash") or 0) + (b.get("cash_on_hand") or 0)
            cashP = (bp.get("cash") or 0) + (bp.get("cash_on_hand") or 0)
            mine = [c for c, rows in ref["stores"].items() if rows.get("entity") == e]
            ebA = sum(ebitda_of((actuals["stores"].get(c) or {}).get(month) or {}) for c in mine)
            gaAe = G["ga_A"] if e == ho_entity else 0.0
            finAe = sum((((actuals["stores"].get(c) or {}).get(month) or {}).get("fin") or 0) for c in mine) + ((ho.get("fin") or 0) if e == ho_entity else 0.0)
            taxAe = sum((((actuals["stores"].get(c) or {}).get(month) or {}).get("tax_ops") or 0) + (((actuals["stores"].get(c) or {}).get(month) or {}).get("income_tax") or 0) for c in mine)
            amortA = sum((((actuals["stores"].get(c) or {}).get(month) or {}).get("amort") or 0) for c in mine)
            d = lambda k: (b.get(k) or 0) - (bp.get(k) or 0)
            d_loans, d_cca, d_interco = -d("loans"), -(d("cca_14") + d("other_03")), -(d("interco_recv") - d("interco_pay"))
            capexA = -(d("fixed_assets") + amortA)
            opA = ebA - gaAe - finAe - taxAe
            explained = opA + d_loans + d_cca + d_interco + capexA
            wc = {"Accounts payable (2121)": -d("ap"), "Accrued salaries (2151)": -d("wages_payable"), "Tax payable (2171)": -d("tax_payable"),
                  "Accounts receivable (1131)": -d("ar"), "Inventory (1211+1243)": -(d("inventory_food") + d("inventory_other")), "Deposits (1133.01)": -d("deposits"),
                  "Prepayments (1133.06)": -d("prepaid"), "Advance receipts (2181.13)": -d("advances")}
            ent.update({"cash_A": cashA, "cash_prev_A": cashP, "delta_A": cashA - cashP, "op_A": opA, "ebitda_A": ebA, "ga_A": gaAe, "fin_A": finAe,
                        "d_loans": d_loans, "d_cca": d_cca, "d_interco": d_interco, "capex_A": capexA, "residual": cashA - cashP - explained, "wc": wc,
                        "wc_total": sum(wc.values()), "loans_F": r["loans"][i], "cca_F": r["cca"][i], "interco_F": r["interco"][i],
                        "capex_F": r["capex"][i], "other_F": r["other"][i]})
            cashA_tot += cashA; cashPrev_tot += cashP
        else:
            have_cash = False
        out["entities"][e] = ent
    if have_cash:
        G["cash_A"] = cashA_tot
        G["cash_prev_A"] = cashPrev_tot

    # ---- diagnostic (anglais, destiné aux associés)
    diag = out["diagnostic"]
    for row in out["stores"]:
        if not row["A"]:
            diag.append({"level": "info", "text": f"{row['name']}: no actuals imported for {mlabel_en(month, True)}."})
            continue
        dp = row["Dpct"]["revenue"]
        if dp is not None and abs(dp) >= 0.05:
            txt = f"{row['name']}: revenue {abs(dp):.0%} {'above' if dp > 0 else 'below'} forecast ({row['A']['revenue']/1000:,.0f} k vs {row['F']['revenue']/1000:,.0f} k)."
            if row["cum3"]["n"] >= 2 and row["cum3"]["pct"] is not None:
                txt += f" Cumulative over {row['cum3']['n']} months: {row['cum3']['pct']:+.0%}."
                txt += (" Persistent gap: the activity level (run-rate) needs revisiting, not only the seasonality." if abs(row["cum3"]["pct"]) >= 0.05
                        else " One-off gap (the cumulative stays close to forecast): mostly a timing / seasonality effect.")
            diag.append({"level": "warn" if abs(dp) >= 0.10 else "info", "text": txt})
        rf, ra = row["ratios"]["food_F"], row["ratios"]["food_A"]
        if rf is not None and ra is not None and abs(ra - rf) >= 0.015:
            diag.append({"level": "warn", "text": f"{row['name']}: food cost at {ra:.1%} vs {rf:.1%} forecast ({row['bridge']['food']/1000:+,.0f} k EBITDA)."})
        of_, oa = row["ratios"]["other_F"], row["ratios"]["other_A"]
        if of_ is not None and oa is not None and abs(oa - of_) >= 0.02:
            diag.append({"level": "info", "text": f"{row['name']}: other operation expenses at {oa:.1%} of income vs {of_:.1%} forecast ({row['bridge']['other']/1000:+,.0f} k)."})
        if row["F"]["labor"] and abs(row["D"]["labor"] / row["F"]["labor"]) >= 0.10:
            diag.append({"level": "info", "text": f"{row['name']}: labor cost {row['A']['labor']/1000:,.0f} k vs {row['F']['labor']/1000:,.0f} k forecast ({row['D']['labor']/row['F']['labor']:+.0%})."})
        if row["F"]["rent"] and abs(row["D"]["rent"] / row["F"]["rent"]) >= 0.05:
            diag.append({"level": "info", "text": f"{row['name']}: rent {row['A']['rent']/1000:,.0f} k vs {row['F']['rent']/1000:,.0f} k forecast."})
    if G["ga_F"] and abs(G["ga_A"] / G["ga_F"] - 1) >= 0.05:
        diag.append({"level": "info", "text": f"Head office: G&A expenses {G['ga_A']/1000:,.0f} k vs {G['ga_F']/1000:,.0f} k forecast ({G['ga_A']/G['ga_F']-1:+.0%})."})
    for e, ent in out["entities"].items():
        name = config.ENTITIES.get(e, {}).get("latin", e).split(" — ")[0]
        if "residual" in ent:
            if abs(ent["residual"]) >= 100000:
                top = sorted(ent["wc"].items(), key=lambda kv: -abs(kv[1]))[:3]
                diag.append({"level": "warn", "text": f"{name}: {ent['residual']/1000:+,.0f} k of cash movement not explained by the P&L, capital expenditure, bank loans, "
                             "shareholder accounts and intercompany flows. Main balance-sheet movements: " + ", ".join(f"{k} {v/1000:+,.0f} k" for k, v in top) + "."})
            dv = ent["delta_A"] - ent["delta_F"]
            if abs(dv) >= 100000:
                diag.append({"level": "info", "text": f"{name}: actual cash movement {ent['delta_A']/1000:+,.0f} k vs {ent['delta_F']/1000:+,.0f} k forecast (gap {dv/1000:+,.0f} k)."})
    trunc = truncate(actuals, month)
    for code, rows in ref["stores"].items():
        s = stores_cfg.get(code)
        eff = (ref.get("calibration") or {}).get(code, {}).get("effective")
        if not s or not eff or not (trunc["stores"].get(code) or {}).get(month):
            continue
        rec = calibrate_store(s, trunc["stores"].get(code) or {}, g, month, group_hist)

        def prop(param, label, used, new, fmt, thr, manual):
            if used and abs(new / used - 1) >= thr:
                out["proposals"].append({"store": code, "name": rows.get("name") or code, "param": param, "label": label, "used": used, "new": new,
                                         "setting": "entered" if manual else "auto",
                                         "text": (f"{label} used: {fmt(used)} → recalibrated on recent months: {fmt(new)}. "
                                                  + ("Entered value: apply the new one or switch back to auto." if manual
                                                     else "Auto parameter: the next forecast will use the recalibrated value unless a value is entered."))})
        prop("runrate_annual", "Annual revenue", eff["runrate_annual"], rec["runrate"] * 12, lambda v: f"{v/1e6:,.2f} M", 0.03, s.get("runrate_annual") is not None)
        prop("food_pct", "Food cost", eff["food_pct"], rec["food_pct"] * 100, lambda v: f"{v:.1f} %", 0.04, s.get("food_pct") is not None)
        prop("other_pct", "Other opex (% income)", eff["other_pct"], rec["other_pct"] * 100, lambda v: f"{v:.1f} %", 0.08, s.get("other_pct") is not None)
        prop("labor", "Labor cost (RMB/month)", eff["labor"], rec["labor"], lambda v: f"{v/1000:,.0f} k", 0.06, s.get("labor") is not None)
        prop("rent", "Rent (RMB/month)", eff["rent"], rec["rent"], lambda v: f"{v/1000:,.0f} k", 0.04, s.get("rent") is not None)
    ho_new, _ = calibrate_ho(trunc, g, month)
    if G["ga_F"] and ho_new and abs(ho_new / G["ga_F"] - 1) >= 0.05:
        out["proposals"].append({"store": "HO", "name": "Head office", "param": "ho_monthly", "label": "G&A expenses (RMB/month)", "used": G["ga_F"], "new": ho_new,
                                 "setting": "entered" if g.get("ho_monthly") else "auto",
                                 "text": f"G&A used: {G['ga_F']/1000:,.0f} k/month → recent median: {ho_new/1000:,.0f} k/month."})
    if not diag:
        diag.append({"level": "ok", "text": "No significant variance: the month is in line with the model."})
    return out
