"""Moteur du prévisionnel La Parisienne — fonctions pures (aucun accès base).

Entrées : cfg (dict, voir config.py) et actuals = {"stores": {code: {mois: {...}}}, "entities": {code: {mois: {...}}}}.

Modèle (volontairement court) :
  CA magasin[m]   = run-rate désaisonnalisé × coefficient saisonnier[m] × (1 + tendance)^(k/12)
                    (run-rate = moyenne désaisonnalisée des 12 derniers mois hors extrêmes, ou CA annuel saisi / 12)
  Food, autres    = ratios (% CA) — médiane des N derniers mois, ou valeur saisie
  Masse salariale = montant mensuel (médiane des N derniers mois, ou saisi) + dérive annuelle — lissé, pas saisonnier
  Loyer           = montant mensuel (médiane / saisi) + dérive ; option de paiement trimestriel/semestriel
  EBITDA magasin  = CA − food − masse salariale − loyer − autres charges
  Siège (G&A)     = montant mensuel (médiane / saisi) porté par JZ
  Trésorerie[m]   = trésorerie[m−1] + Σ EBITDA magasins − G&A − intérêts − frais bancaires − taxes − IS
                    ± prêts (échéances / renouvellements) ± événements (capex, CCA, interco, autres) ± décalage loyer ± friction
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


def median(xs: List[float], default: float = 0.0) -> float:
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else default


def other_of(v: dict) -> float:
    """Autres charges d'exploitation (hors loyer et amortissements) = 5501 − 5501.09 − 5501.14 − 5501.15."""
    return (v.get("opex_5501") or 0) - (v.get("rent") or 0) - (v.get("amort") or 0) - (v.get("depr") or 0)


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
        return _normalize([float(x) for x in store["season_custom"]]), "personnalisée"
    if mode in config.SEASON_PROFILES:
        return list(config.SEASON_PROFILES[mode]), config.SEASON_LABELS.get(mode, mode).lower()
    if mode == "auto":
        s = season_from_history(hist, years, as_of)
        if s:
            return s, f"auto ({years} dernières années civiles complètes)"
    s = season_from_history(group_hist, years, as_of)
    if s:
        return s, "groupe (ZHY + BFC)" + ("" if mode == "group" else " — historique du magasin insuffisant")
    return list(config.SEASON_PROFILES["school"]), "calendrier scolaire (repli)"


# ----------------------------------------------------------------- calibrage
def calibrate_store(store: dict, hist: dict, general: dict, as_of: str, group_hist: dict) -> dict:
    season, season_src = resolve_season(store, hist, general, group_hist, as_of)
    months = sorted(m for m, v in hist.items() if m <= as_of and (v.get("revenue") or 0) > 0)
    n = int(general.get("calib_months") or 6)
    recent = months[-n:]
    c = {"season": season, "season_src": season_src, "months_used": recent, "n_history": len(months)}
    # run-rate (CA mensuel désaisonnalisé)
    if store.get("runrate_annual"):
        c["runrate"], c["runrate_src"] = float(store["runrate_annual"]) / 12, "saisi (CA annuel)"
    elif months:
        last12 = months[-12:]
        sa = sorted((hist[m]["revenue"] or 0) / season[int(m[5:7]) - 1] for m in last12)
        used = sa[1:-1] if len(sa) >= 6 else sa
        c["runrate"] = st.mean(used)
        c["runrate_src"] = f"auto : moyenne désaisonnalisée des {len(last12)} derniers mois" + (" hors extrêmes" if len(sa) >= 6 else "")
    else:
        c["runrate"], c["runrate_src"] = 0.0, "aucun historique — saisir un CA annuel"
    c["last12_revenue"] = sum(hist[m]["revenue"] or 0 for m in months[-12:]) if months else 0.0

    def ratio(key, fn):
        if store.get(key) is not None and store.get(key) != "":
            return float(store[key]) / 100, "saisi"
        vals = [fn(hist[m]) / hist[m]["revenue"] for m in recent if (hist[m].get("revenue") or 0) > 0]
        return (median(vals), f"auto : médiane {len(vals)} mois") if vals else (0.0, "aucun historique")

    def amount(key, fn):
        if store.get(key) is not None and store.get(key) != "":
            return float(store[key]), "saisi"
        vals = [fn(hist[m]) for m in recent]
        return (median(vals), f"auto : médiane {len(vals)} mois") if vals else (0.0, "aucun historique")

    c["food_pct"], c["food_src"] = ratio("food_pct", lambda v: v.get("food") or 0)
    c["other_pct"], c["other_src"] = ratio("other_pct", other_of)
    c["labor"], c["labor_src"] = amount("labor", lambda v: v.get("labor") or 0)
    c["rent"], c["rent_src"] = amount("rent", lambda v: v.get("rent") or 0)
    c["labor_pct_equiv"] = c["labor"] / c["runrate"] if c["runrate"] else 0.0
    c["ebitda_pct_equiv"] = (1 - c["food_pct"] - c["other_pct"] - (c["labor"] + c["rent"]) / c["runrate"]) if c["runrate"] else 0.0
    return c


def calibrate_ho(actuals: dict, general: dict, as_of: str) -> (float, str):
    if general.get("ho_monthly"):
        return float(general["ho_monthly"]), "saisi"
    hist = actuals.get("stores", {}).get("HO") or {}
    months = sorted(m for m in hist if m <= as_of)[-int(general.get("calib_months") or 6):]
    vals = [hist[m].get("ga") or 0 for m in months]
    return (median(vals), f"auto : médiane {len(vals)} mois") if vals else (0.0, "aucun historique")


# ----------------------------------------------------------------- prêts
def loan_schedule(loan: dict, as_of: str, months: List[str], gap_override=None, renew_override=None) -> dict:
    """-> {"flows": {mois: montant}, "outstanding": {mois: encours en début de mois}, "events": [...]}"""
    principal = float(loan.get("principal") or 0)
    term = int(loan.get("term_months") or 12)
    renew = bool(loan.get("renew")) if renew_override is None else bool(renew_override)
    gap = int(loan.get("renew_gap") or 0) if gap_override is None else int(gap_override)
    amt_renew = float(loan.get("renew_amount") or principal)
    maturity = loan.get("maturity") or months[0]
    outstanding = principal
    # échéances déjà passées à la date d'arrêté : on suppose le prêt roulé (sinon remboursé)
    while maturity <= as_of:
        if not renew:
            outstanding = 0.0
            break
        outstanding = amt_renew
        maturity = month_add(maturity, term)
    flows, outs, evs = {}, {}, []
    pending = []          # tirages programmés (mois, montant)
    for m in months:
        outs[m] = outstanding
        for pm, pa in list(pending):
            if pm == m:
                flows[m] = flows.get(m, 0) + pa
                outstanding = pa
                evs.append({"loan": loan.get("label"), "month": m, "type": "tirage", "amount": pa})
                pending.remove((pm, pa))
        if m == maturity and outstanding > 0:
            flows[m] = flows.get(m, 0) - outstanding
            evs.append({"loan": loan.get("label"), "month": m, "type": "remboursement", "amount": -outstanding})
            outstanding = 0.0
            if renew:
                rm = month_add(m, gap)
                if gap == 0:
                    flows[m] += amt_renew
                    outstanding = amt_renew
                    evs.append({"loan": loan.get("label"), "month": m, "type": "renouvellement", "amount": amt_renew})
                else:
                    pending.append((rm, amt_renew))
            maturity = month_add(maturity, term)
    return {"flows": flows, "outstanding": outs, "events": evs}


# ----------------------------------------------------------------- prévisionnel
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
        rent0 = c["rent"]
        c["effective"] = {"runrate": rr, "runrate_annual": rr * 12, "growth_pct": growth * 100, "food_pct": food * 100,
                          "other_pct": other * 100, "labor": labor0, "rent": rent0, "entity": s.get("entity"), "name": s.get("name")}
        rows = {k: [0.0] * horizon for k in ("revenue", "food", "labor", "rent", "other", "ebitda", "rent_cash")}
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
    sched = {l["id"]: loan_schedule(l, as_of, months, ov.get("loan_gap"), ov.get("loan_renew")) for l in loans}
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
        applied.append({"month": ov["cca_month"], "entity": ent, "category": "cca", "amount": float(ov["cca_amount"]), "label": "Apport CCA (simulation)", "store": ""})

    # ---- entités
    res_ent = {}
    keys = ("revenue", "ebitda_stores", "ga", "fees", "tax_ops", "interest", "cit", "rent_adj", "friction", "capex", "cca", "loans", "interco", "other", "delta", "cash")
    for e in entities:
        bal = (actuals.get("entities", {}).get(e) or {}).get(as_of) or {}
        opening = (bal.get("cash") or 0) + (bal.get("cash_on_hand") or 0)
        r = {k: [0.0] * horizon for k in keys}
        r["opening"] = opening
        r["opening_src"] = f"balance {as_of}" if bal else "balance absente à la date d'arrêté (0)"
        fric = float((g.get("friction") or {}).get(e) or 0)
        q_acc = 0.0
        for i, m in enumerate(months):
            k = i + 1
            rev = sum(rows["revenue"][i] for rows in res_stores.values() if rows["entity"] == e)
            eb = sum(rows["ebitda"][i] for rows in res_stores.values() if rows["entity"] == e)
            rent_adj = sum(rows["rent"][i] - rows["rent_cash"][i] for rows in res_stores.values() if rows["entity"] == e)
            ga = ho_monthly * (1 + cost_infl) ** (k / 12) if e == ho_entity else 0.0
            fees = rev * float(g.get("bank_fees_pct") or 0) / 100
            tax_ops = rev * float(g.get("tax_ops_pct") or 0) / 100
            interest = sum(sched[l["id"]]["outstanding"][m] * float(l.get("rate_pct") or 0) / 100 / 12 for l in loans if l.get("entity") == e)
            loan_flow = sum(sched[l["id"]]["flows"].get(m, 0.0) for l in loans if l.get("entity") == e)
            q_acc += eb - ga - fees - tax_ops - interest
            cit = 0.0
            if int(m[5:7]) in (1, 4, 7, 10):     # paiement le mois suivant la fin de trimestre
                cit = max(0.0, q_acc) * float(g.get("cit_rate_pct") or 0) / 100
                q_acc = 0.0
            evm = ev[e][m]
            delta = eb - ga - fees - tax_ops - interest - cit + rent_adj + fric + evm["capex"] + evm["cca"] + loan_flow + evm["loan"] + evm["interco"] + evm["other"]
            vals = {"revenue": rev, "ebitda_stores": eb, "ga": ga, "fees": fees, "tax_ops": tax_ops, "interest": interest, "cit": cit,
                    "rent_adj": rent_adj, "friction": fric, "capex": evm["capex"], "cca": evm["cca"], "loans": loan_flow + evm["loan"],
                    "interco": evm["interco"], "other": evm["other"], "delta": delta}
            for kk, v in vals.items():
                r[kk][i] = v
            r["cash"][i] = (r["cash"][i - 1] if i else opening) + delta
        res_ent[e] = r

    # ---- groupe & indicateurs
    grp = {k: [sum(res_ent[e][k][i] for e in entities) for i in range(horizon)] for k in keys}
    grp["opening"] = sum(res_ent[e]["opening"] for e in entities)
    grp["ebitda"] = [grp["ebitda_stores"][i] - grp["ga"][i] for i in range(horizon)]
    grp["op_cash"] = [grp["ebitda"][i] - grp["fees"][i] - grp["tax_ops"][i] - grp["interest"][i] - grp["cit"][i] + grp["rent_adj"][i] + grp["friction"][i] for i in range(horizon)]
    imin = min(range(horizon), key=lambda i: grp["cash"][i])
    alerts = []
    thr_g, thr_e = float(g.get("alert_group") or 0), float(g.get("alert_entity") or 0)
    low_g = [months[i] for i in range(horizon) if grp["cash"][i] < thr_g]
    if low_g:
        alerts.append({"level": "warn" if min(grp["cash"]) >= 0 else "danger",
                       "text": f"Trésorerie groupe sous le seuil de {thr_g/1000:,.0f} k sur {len(low_g)} mois (point bas {grp['cash'][imin]/1000:,.0f} k en {mlabel(months[imin])})."})
    for e in entities:
        low = [months[i] for i in range(horizon) if res_ent[e]["cash"][i] < thr_e]
        if low:
            j = min(range(horizon), key=lambda i: res_ent[e]["cash"][i])
            alerts.append({"level": "warn" if res_ent[e]["cash"][j] >= 0 else "danger",
                           "text": f"{e} : trésorerie sous {thr_e/1000:,.0f} k sur {len(low)} mois (point bas {res_ent[e]['cash'][j]/1000:,.0f} k en {mlabel(months[j])})."})
    loan_events = [x for l in loans for x in sched[l["id"]]["events"]]
    for x in sorted(loan_events, key=lambda x: x["month"]):
        if x["type"] == "remboursement":
            alerts.append({"level": "info", "text": f"{mlabel(x['month'])} : échéance {x['loan']} — {-x['amount']/1000:,.0f} k à rembourser" +
                           (" (renouvellement supposé le même mois)" if any(y["month"] == x["month"] and y["type"] == "renouvellement" and y["loan"] == x["loan"] for y in loan_events) else " (pas de renouvellement le même mois)")})
    n12 = min(12, horizon)
    kpis = {"cash_open": grp["opening"], "cash_min": grp["cash"][imin], "cash_min_month": months[imin], "cash_end": grp["cash"][-1],
            "revenue_12m": sum(grp["revenue"][:n12]), "ebitda_12m": sum(grp["ebitda"][:n12]), "op_cash_12m": sum(grp["op_cash"][:n12]),
            "capex_total": sum(grp["capex"]), "cca_total": sum(grp["cca"]),
            "entity_end": {e: res_ent[e]["cash"][-1] for e in entities}, "entity_min": {e: min(res_ent[e]["cash"]) for e in entities}}
    return {"label": label or "", "as_of": as_of, "horizon": horizon, "months": months, "stores": res_stores, "entities": res_ent,
            "group": grp, "calibration": calib, "ho_monthly": ho_monthly, "ho_src": ho_src, "loan_schedule": sorted(loan_events, key=lambda x: x["month"]),
            "loans": loans, "events_applied": sorted(applied, key=lambda x: x["month"]), "kpis": kpis, "alerts": alerts, "overrides": ov,
            "general": g, "generated_at": datetime.utcnow().isoformat(timespec="seconds")}


_OV = [("revenue_pct", "CA tous magasins {:+.0f} %"), ("growth_pp", "tendance {:+.1f} pt/an"), ("food_pp", "food cost {:+.1f} pt"),
       ("other_pp", "autres charges {:+.1f} pt"), ("labor_pct", "masse salariale {:+.0f} %"), ("ga_delta", "siège {:+,.0f} RMB/mois"),
       ("loan_gap", "renouvellement des prêts décalé de {:.0f} mois"), ("horizon", "horizon {:.0f} mois")]


def describe_overrides(ov: dict) -> str:
    """Variantes d'une simulation en français lisible (vide si aucune)."""
    if not ov:
        return ""
    parts = []
    for key, fmt in _OV:
        v = ov.get(key)
        if v not in (None, "", 0, 0.0):
            parts.append(fmt.format(float(v)).replace(",", " "))
    if ov.get("capex_scale") not in (None, "", 1, 1.0):
        parts.append(f"investissements × {float(ov['capex_scale']):.2f}".replace(".", ","))
    if ov.get("loan_renew") is True:
        parts.append("tous les prêts renouvelés")
    elif ov.get("loan_renew") is False:
        parts.append("aucun prêt renouvelé (stress)")
    sa = ov.get("stores_active") or {}
    if sa:
        on = [c for c, v in sa.items() if v]
        parts.append("magasins projetés : " + ", ".join(on) if on else "aucun magasin projeté")
    if ov.get("cca_amount"):
        parts.append(f"apport CCA {float(ov['cca_amount'])/1000:,.0f} k en {mlabel(ov.get('cca_month', ''))} ({ov.get('cca_entity', 'JZ')})".replace(",", " "))
    return " · ".join(parts)


# ----------------------------------------------------------------- écarts prévu / réalisé
def truncate(actuals: dict, upto: str) -> dict:
    return {"stores": {c: {m: v for m, v in d.items() if m <= upto} for c, d in (actuals.get("stores") or {}).items()},
            "entities": {c: {m: v for m, v in d.items() if m <= upto} for c, d in (actuals.get("entities") or {}).items()}}


def backtest_reference(cfg: dict, actuals: dict, month: str, horizon: int = 3) -> dict:
    prev = month_add(month, -1)
    ref = forecast(cfg, truncate(actuals, prev), as_of=prev, horizon=horizon, label=f"modèle recalculé au {mlabel(prev)} (données ≤ {prev})")
    ref["backtest"] = True
    return ref


def _pct(a, b):
    return (a / b - 1) if b else None


def variance(cfg: dict, actuals: dict, month: str, ref: dict) -> dict:
    if month not in ref.get("months", []):
        raise ValueError(f"Le mois {month} n'est pas couvert par le prévisionnel de référence.")
    i = ref["months"].index(month)
    prev = month_add(month, -1)
    g = cfg.get("general") or {}
    out = {"month": month, "reference": ref.get("label") or "", "backtest": bool(ref.get("backtest")), "as_of": ref.get("as_of"),
           "stores": [], "entities": {}, "group": {}, "proposals": [], "diagnostic": []}
    group_hist = group_history(actuals)
    stores_cfg = {s["code"]: s for s in cfg.get("stores") or []}
    totF = {"revenue": 0.0, "ebitda": 0.0}
    totA = {"revenue": 0.0, "ebitda": 0.0}
    for code, rows in ref["stores"].items():
        a = (actuals.get("stores", {}).get(code) or {}).get(month)
        F = {k: rows[k][i] for k in ("revenue", "food", "labor", "rent", "other", "ebitda")}
        row = {"code": code, "name": rows.get("name") or code, "entity": rows.get("entity"), "F": F, "A": None, "D": None}
        if a:
            A = {"revenue": a.get("revenue") or 0, "food": a.get("food") or 0, "labor": a.get("labor") or 0, "rent": a.get("rent") or 0,
                 "other": other_of(a), "ebitda": ebitda_of(a)}
            row["A"] = A
            row["D"] = {k: A[k] - F[k] for k in F}
            row["Dpct"] = {k: _pct(A[k], F[k]) for k in F}
            row["ratios"] = {"food_F": F["food"] / F["revenue"] if F["revenue"] else None, "food_A": A["food"] / A["revenue"] if A["revenue"] else None,
                             "other_F": F["other"] / F["revenue"] if F["revenue"] else None, "other_A": A["other"] / A["revenue"] if A["revenue"] else None}
            # contributions à l'écart d'EBITDA : CA (à ratios prévus), food (effet ratio), autres (effet ratio), salaires, loyer
            margin_F = 1 - (row["ratios"]["food_F"] or 0) - (row["ratios"]["other_F"] or 0)
            row["bridge"] = {"ca": row["D"]["revenue"] * margin_F,
                             "food": -((row["ratios"]["food_A"] or 0) - (row["ratios"]["food_F"] or 0)) * A["revenue"],
                             "other": -((row["ratios"]["other_A"] or 0) - (row["ratios"]["other_F"] or 0)) * A["revenue"],
                             "labor": -row["D"]["labor"], "rent": -row["D"]["rent"]}
            # cumul 3 mois (CA)
            c3F = c3A = 0.0
            n3 = 0
            for j in range(max(0, i - 2), i + 1):
                mj = ref["months"][j]
                aj = (actuals.get("stores", {}).get(code) or {}).get(mj)
                if aj:
                    c3F += rows["revenue"][j]
                    c3A += aj.get("revenue") or 0
                    n3 += 1
            row["cum3"] = {"n": n3, "F": c3F, "A": c3A, "pct": _pct(c3A, c3F)}
            totF["revenue"] += F["revenue"]; totA["revenue"] += A["revenue"]
            totF["ebitda"] += F["ebitda"]; totA["ebitda"] += A["ebitda"]
        out["stores"].append(row)
    # siège
    ho = (actuals.get("stores", {}).get("HO") or {}).get(month) or {}
    gaF = sum(ref["entities"][e]["ga"][i] for e in ref["entities"])
    gaA = ho.get("ga") or 0
    finF = sum(ref["entities"][e]["interest"][i] + ref["entities"][e]["fees"][i] for e in ref["entities"])
    finA = sum(((actuals.get("stores", {}).get(c) or {}).get(month) or {}).get("fin") or 0 for c in list(ref["stores"]) + ["HO"])
    out["group"] = {"revenue_F": totF["revenue"], "revenue_A": totA["revenue"], "ebitda_stores_F": totF["ebitda"], "ebitda_stores_A": totA["ebitda"],
                    "ga_F": gaF, "ga_A": gaA, "fin_F": finF, "fin_A": finA,
                    "ebitda_F": totF["ebitda"] - gaF, "ebitda_A": totA["ebitda"] - gaA,
                    "cash_F": ref["group"]["cash"][i], "cash_prev_F": ref["group"]["cash"][i - 1] if i else ref["group"]["opening"]}
    # entités : trésorerie et pont
    cashA_tot = cashPrev_tot = 0.0
    have_cash = True
    for e, r in ref["entities"].items():
        b = (actuals.get("entities", {}).get(e) or {}).get(month)
        bp = (actuals.get("entities", {}).get(e) or {}).get(prev)
        ent = {"cash_F": r["cash"][i], "delta_F": r["delta"][i], "cash_prev_F": r["cash"][i - 1] if i else r["opening"]}
        if b and bp:
            cashA = (b.get("cash") or 0) + (b.get("cash_on_hand") or 0)
            cashP = (bp.get("cash") or 0) + (bp.get("cash_on_hand") or 0)
            ebA = sum(ebitda_of((actuals["stores"].get(c) or {}).get(month) or {}) for c, rows in ref["stores"].items() if rows.get("entity") == e)
            gaAe = gaA if e == (g.get("ho_entity") or "JZ") else 0.0
            finAe = sum((((actuals["stores"].get(c) or {}).get(month) or {}).get("fin") or 0) for c, rows in ref["stores"].items() if rows.get("entity") == e) \
                + ((ho.get("fin") or 0) if e == (g.get("ho_entity") or "JZ") else 0.0)
            d = lambda k: (b.get(k) or 0) - (bp.get(k) or 0)
            d_loans = -d("loans")
            d_cca = -(d("cca_14") + d("other_03"))
            d_interco = -(d("interco_recv") - d("interco_pay"))
            amortA = sum((((actuals["stores"].get(c) or {}).get(month) or {}).get("amort") or 0) for c, rows in ref["stores"].items() if rows.get("entity") == e)
            capexA = -(d("fixed_assets") + amortA)      # Δ(1501 + 1901) + dotation 5501.14 = investissements décaissés (approx.)
            explained = ebA - gaAe - finAe + d_loans + d_cca + d_interco + capexA
            wc = {"Fournisseurs (2121)": -d("ap"), "Salaires à payer (2151)": -d("wages_payable"), "Taxes à payer (2171)": -d("tax_payable"),
                  "Clients (1131)": -d("ar"), "Stocks (1211+1243)": -(d("inventory_food") + d("inventory_other")), "Dépôts (1133.01)": -d("deposits"),
                  "Charges payées d'avance (1133.06)": -d("prepaid"), "Avances clients (2181.13)": -d("advances")}
            ent.update({"cash_A": cashA, "cash_prev_A": cashP, "delta_A": cashA - cashP, "ebitda_A": ebA, "ga_A": gaAe, "fin_A": finAe,
                        "d_loans": d_loans, "d_cca": d_cca, "d_interco": d_interco, "capex_A": capexA, "residual": cashA - cashP - explained, "wc": wc,
                        "wc_total": sum(wc.values()), "loans_F": r["loans"][i], "cca_F": r["cca"][i], "interco_F": r["interco"][i],
                        "capex_F": r["capex"][i], "other_F": r["other"][i]})
            cashA_tot += cashA; cashPrev_tot += cashP
        else:
            have_cash = False
        out["entities"][e] = ent
    if have_cash:
        out["group"]["cash_A"] = cashA_tot
        out["group"]["cash_prev_A"] = cashPrev_tot

    # ---- diagnostic & propositions
    diag = out["diagnostic"]
    for row in out["stores"]:
        if not row["A"]:
            diag.append({"level": "info", "text": f"{row['name']} : pas de réalisé importé pour {mlabel(month)}."})
            continue
        dp = row["Dpct"]["revenue"]
        if dp is not None and abs(dp) >= 0.05:
            sens = "au-dessus" if dp > 0 else "en dessous"
            txt = f"{row['name']} : CA {sens} du prévu de {abs(dp):.0%} ({row['A']['revenue']/1000:,.0f} k contre {row['F']['revenue']/1000:,.0f} k)."
            if row["cum3"]["n"] >= 2 and row["cum3"]["pct"] is not None:
                txt += f" Sur {row['cum3']['n']} mois cumulés : {row['cum3']['pct']:+.0%}."
                if abs(row["cum3"]["pct"]) >= 0.05:
                    txt += " Écart persistant : le niveau d'activité (run-rate) est à revoir, pas seulement la saisonnalité."
                else:
                    txt += " Écart ponctuel (le cumul reste proche du prévu) : plutôt un décalage de saisonnalité."
            diag.append({"level": "warn" if abs(dp) >= 0.10 else "info", "text": txt})
        rf, ra = row["ratios"]["food_F"], row["ratios"]["food_A"]
        if rf is not None and ra is not None and abs(ra - rf) >= 0.015:
            diag.append({"level": "warn", "text": f"{row['name']} : food cost à {ra:.1%} contre {rf:.1%} prévu ({row['bridge']['food']/1000:+,.0f} k d'EBITDA)."})
        of_, oa = row["ratios"]["other_F"], row["ratios"]["other_A"]
        if of_ is not None and oa is not None and abs(oa - of_) >= 0.02:
            diag.append({"level": "info", "text": f"{row['name']} : autres charges à {oa:.1%} du CA contre {of_:.1%} prévu ({row['bridge']['other']/1000:+,.0f} k)."})
        if row["F"]["labor"] and abs(row["D"]["labor"] / row["F"]["labor"]) >= 0.10:
            diag.append({"level": "info", "text": f"{row['name']} : masse salariale {row['A']['labor']/1000:,.0f} k contre {row['F']['labor']/1000:,.0f} k prévu ({row['D']['labor']/row['F']['labor']:+.0%})."})
        if row["F"]["rent"] and abs(row["D"]["rent"] / row["F"]["rent"]) >= 0.05:
            diag.append({"level": "info", "text": f"{row['name']} : loyer {row['A']['rent']/1000:,.0f} k contre {row['F']['rent']/1000:,.0f} k prévu."})
    if gaF and abs(gaA / gaF - 1) >= 0.05:
        diag.append({"level": "info", "text": f"Siège : G&A {gaA/1000:,.0f} k contre {gaF/1000:,.0f} k prévu ({gaA/gaF-1:+.0%})."})
    for e, ent in out["entities"].items():
        if "residual" in ent:
            if abs(ent["residual"]) >= 100000:
                top = sorted(ent["wc"].items(), key=lambda kv: -abs(kv[1]))[:3]
                diag.append({"level": "warn", "text": f"{e} : {ent['residual']/1000:+,.0f} k de variation de trésorerie non expliqués par le P&L, les investissements, les prêts, les CCA et les intercos. "
                             "Principaux mouvements de bilan : " + ", ".join(f"{k} {v/1000:+,.0f} k" for k, v in top) + "."})
            dv = ent["delta_A"] - ent["delta_F"]
            if abs(dv) >= 100000:
                diag.append({"level": "info", "text": f"{e} : variation de trésorerie réelle {ent['delta_A']/1000:+,.0f} k contre {ent['delta_F']/1000:+,.0f} k prévue (écart {dv/1000:+,.0f} k)."})
    # propositions de recalibrage
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
                                         "setting": "saisi" if manual else "auto",
                                         "text": (f"{label} utilisé : {fmt(used)} → recalibré sur les derniers mois : {fmt(new)}. "
                                                  + ("Paramètre saisi à la main : appliquer la nouvelle valeur ou repasser en auto." if manual
                                                     else "Paramètre en auto : le prochain prévisionnel utilisera cette valeur ; fixer une valeur pour s'en écarter."))})
        prop("runrate_annual", "CA annuel", eff["runrate_annual"], rec["runrate"] * 12, lambda v: f"{v/1e6:,.2f} M", 0.03, s.get("runrate_annual") is not None)
        prop("food_pct", "Food cost", eff["food_pct"], rec["food_pct"] * 100, lambda v: f"{v:.1f} %", 0.04, s.get("food_pct") is not None)
        prop("other_pct", "Autres charges (% CA)", eff["other_pct"], rec["other_pct"] * 100, lambda v: f"{v:.1f} %", 0.08, s.get("other_pct") is not None)
        prop("labor", "Masse salariale (RMB/mois)", eff["labor"], rec["labor"], lambda v: f"{v/1000:,.0f} k", 0.06, s.get("labor") is not None)
        prop("rent", "Loyer (RMB/mois)", eff["rent"], rec["rent"], lambda v: f"{v/1000:,.0f} k", 0.04, s.get("rent") is not None)
    ho_new, _ = calibrate_ho(trunc, g, month)
    if gaF and ho_new and abs(ho_new / gaF - 1) >= 0.05:
        out["proposals"].append({"store": "HO", "name": "Siège", "param": "ho_monthly", "label": "G&A siège (RMB/mois)", "used": gaF, "new": ho_new,
                                 "setting": "saisi" if g.get("ho_monthly") else "auto",
                                 "text": f"G&A siège utilisé : {gaF/1000:,.0f} k/mois → médiane récente : {ho_new/1000:,.0f} k/mois."})
    if not diag:
        diag.append({"level": "ok", "text": "Aucun écart significatif : le mois est conforme au modèle."})
    return out
