"""ISFAHAAN — Trésorerie groupe : scan des balances Pennylane de toutes les sociétés.

Pour chaque société du groupe disposant d'un token Pennylane, lit la balance
générale CUMULÉE (GET /trial_balance, du 01/01/1990 à une date d'arrêté) à
plusieurs fins de mois, et en tire les grandes masses :

  trésorerie   comptes 51 + 53 (concours bancaires 519 inclus, en négatif)
  dettes frs   401 (solde créditeur)
  créances cli 411 (solde débiteur)
  emprunts     16  (obligations convertibles, prêts — solde créditeur)
  liaison      45 + 46 + 47 (comptes courants groupe, débiteurs/créditeurs divers,
               comptes d'attente — là où circulent les flux intra-groupe)

LECTURE SEULE. Le token doit porter le scope « trial_balance:readonly » (sinon la
société est signalée et sautée). ⚠️ Tant que la migration Inqom → Pennylane n'est
pas soldée par le cabinet, les niveaux peuvent différer de la comptabilité Inqom
(double alimentation constatée sur 2026) : les VARIATIONS mois par mois restent
le bon indicateur de pilotage.
"""
import csv
import io
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx

from app.core.connectors import pennylane
from . import config

_TZ = timedelta(hours=4)
_EPOCH = "1990-01-01"
MASSES = ["tresorerie", "dettes_frs_401", "creances_cli_411", "emprunts_16", "liaison_45_46_47"]


def _month_ends(n):
    today = (datetime.utcnow() + _TZ).date()
    out, y, m = [], today.year, today.month
    for _ in range(n):
        end = date(y, m, 1) - timedelta(days=1)
        out.append(end.isoformat())
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return sorted(out) + [today.isoformat()]


def _trial_balance(pl, day):
    """Balance cumulée au soir de `day` (liste des comptes, paginée)."""
    items, cursor = [], None
    for _ in range(30):
        params = {"period_start": _EPOCH, "period_end": day, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        with httpx.Client(timeout=120) as c:
            r = c.get(pl.base_url + "/trial_balance", headers=pl._h, params=params)
        r.raise_for_status()
        d = r.json()
        items += d.get("items") or []
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")
        time.sleep(0.1)
    return items


def _masses(items):
    m = dict.fromkeys(MASSES, 0.0)
    for i in items:
        n = str(i.get("number") or "")
        sol = float(i.get("debits") or 0) - float(i.get("credits") or 0)   # solde débiteur
        if n[:2] in ("51", "53"):
            m["tresorerie"] += sol
        elif n.startswith("401"):
            m["dettes_frs_401"] += -sol
        elif n.startswith("411"):
            m["creances_cli_411"] += sol
        elif n.startswith("16"):
            m["emprunts_16"] += -sol
        elif n[:2] in ("45", "46", "47"):
            m["liaison_45_46_47"] += sol
    return m


def run_treso_scan(ctx, months=6):
    dates = _month_ends(int(months))
    codes = list(config.COMPANIES)
    ctx.log(f"Trésorerie groupe ISFAHAAN — balances Pennylane aux dates : {', '.join(dates)}")

    data = {}                                    # (code, date) -> masses
    warns = []
    for k, code in enumerate(codes):
        ctx.progress(k, len(codes), step=f"balance {code}…")
        pl = pennylane.for_company(code)
        if not pl:
            warns.append(f"{code} : token Pennylane absent — sautée")
            continue
        try:
            for d in dates:
                data[(code, d)] = _masses(_trial_balance(pl, d))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                warns.append(f"{code} : token sans scope « trial_balance:readonly » — sautée "
                             "(régénérer le token en cochant Balance générale)")
            else:
                warns.append(f"{code} : HTTP {e.response.status_code} — sautée")

    ok_codes = sorted({c for c, _ in data})

    # ---- compte rendu ----
    now = datetime.utcnow() + _TZ
    L = [f"TRÉSORERIE GROUPE ISFAHAAN — balances Pennylane — tâche #{ctx.run_id} du {now.strftime('%d/%m/%Y %H:%M')}", ""]
    for w in warns:
        L.append(f"⚠️ {w}")
    if warns:
        L.append("")
    for masse, titre in [("tresorerie", "TRÉSORERIE (51+53)"), ("dettes_frs_401", "DETTES FOURNISSEURS (401)"),
                         ("creances_cli_411", "CRÉANCES CLIENTS (411)"), ("emprunts_16", "EMPRUNTS (16)")]:
        L.append(f"== {titre} — en k€ ==")
        L.append("  " + f"{'société':<16}" + "".join(f"{d[5:]:>10}" for d in dates) + f"{'Δ période':>11}")
        tot = defaultdict(float)
        for code in ok_codes:
            vals = [data[(code, d)][masse] for d in dates]
            for d, v in zip(dates, vals):
                tot[d] += v
            L.append("  " + f"{code:<16}" + "".join(f"{v/1000:>10,.0f}" for v in vals)
                     + f"{(vals[-1]-vals[0])/1000:>+11,.0f}")
        tv = [tot[d] for d in dates]
        L.append("  " + f"{'GROUPE':<16}" + "".join(f"{v/1000:>10,.0f}" for v in tv)
                 + f"{(tv[-1]-tv[0])/1000:>+11,.0f}")
        L.append("")
    L.append("Liaison/attente (45+46+47) : voir le CSV — les flux intra-groupe y transitent ;")
    L.append("un solde qui gonfle = argent « en route » ou avances entre sociétés non soldées.")
    L.append("")
    L.append("⚠️ Tant que la migration Inqom → Pennylane n'est pas soldée, les NIVEAUX peuvent")
    L.append("différer de l'ancienne comptabilité ; piloter sur les VARIATIONS mois par mois.")
    ctx.set_report("\n".join(L))

    # ---- CSV ----
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["societe", "date_arrete"] + MASSES)
    for code in ok_codes:
        for d in dates:
            m = data[(code, d)]
            w.writerow([code, d] + [f"{m[k]:.2f}" for k in MASSES])
    ctx.add_artifact("csv", f"{now.strftime('%Y%m%d %H%M')} tresorerie_groupe T{ctx.run_id}.csv",
                     buf.getvalue().encode("utf-8-sig"), "text/csv")

    dtre = sum(data[(c, dates[-1])]["tresorerie"] for c in ok_codes) - \
           sum(data[(c, dates[0])]["tresorerie"] for c in ok_codes)
    return (f"{'✅' if not warns else '⚠️'} Trésorerie groupe — {len(ok_codes)}/{len(codes)} sociétés, "
            f"Δ trésorerie {dates[0]} → {dates[-1]} : {dtre/1000:+,.0f} k€")
