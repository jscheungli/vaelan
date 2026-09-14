"""La Parisienne (Shanghai) — prévisionnel de trésorerie : import des management reports, hypothèses, prévisionnels,
écarts, simulations, historique, comptes courants d'associés, méthode."""
import json
from typing import List

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import current_user
from app.packs.lp import service, engine, config as lpcfg, report
from app.web.routes import templates, _ctx, _company_or_redirect

router = APIRouter()
P = "/c/{code}/previsionnel"


def _guard(request: Request, code: str):
    return _company_or_redirect(request, code, feature="previsionnel")


def _who(request: Request) -> str:
    u = current_user(request)
    return (u.name or u.email) if u else "?"


def _fx():
    """Helpers de formatage passés aux templates."""
    return {"k": lambda v, s=False: report.k(v, s), "mio": report.mio, "pct": report.pct, "ml": engine.mlabel, "mle": engine.mlabel_en,
            "cat": lambda c: lpcfg.EVENT_CATEGORIES.get(c, c), "season_labels": lpcfg.SEASON_LABELS, "entities": lpcfg.ENTITIES,
            "categories": lpcfg.EVENT_CATEGORIES, "months_fr": lpcfg.MONTHS_FR}


def _num(v, default=None):
    if v is None:
        return default
    v = str(v).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _ym(v):
    v = (v or "").strip()
    return v[:7] if len(v) >= 7 and v[4] == "-" else None


# ----------------------------------------------------------------- accueil
@router.get(P, response_class=HTMLResponse)
def lp_home(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    st = service.status(code)
    latest, res = service.latest_forecast(code)
    forecasts = service.list_forecasts(code, n=15)
    variants = {}
    for r in forecasts:
        if r.set_id:
            variants[r.id] = service.set_variants(code, r.set_id)
    variances = service.list_variances(code, n=10)
    cfg = service.get_config(code)
    months = sorted({m for d in st["actuals"]["stores"].values() for m in d}, reverse=True)[:18]
    return templates.TemplateResponse(request, "lp_home.html", _ctx(request, company=company, st=st, latest=latest, res=res, forecasts=forecasts, variants=variants,
                                                                    variances=variances, cfg=cfg, months=months, msg=msg, **_fx()))


@router.post(P + "/import")
async def lp_import(request: Request, code: str, files: List[UploadFile] = File(...)):
    company, redir = _guard(request, code)
    if redir:
        return redir
    parts = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        try:
            r = service.import_report(code, f.filename or "rapport.xlsm", data)
            span = f" ({r['months'][0]} → {r['months'][1]})" if r.get("months") else ""
            parts.append(f"{f.filename} : {r['n_pl']} mois de P&L ({', '.join(f'{s} {n}' for s, n in r['stores'].items())}), "
                         f"{r['n_bs']} balances ({', '.join(f'{e} {n}' for e, n in r['entities'].items())}){span}" + (" — " + " ".join(r["notes"]) if r["notes"] else ""))
        except Exception as e:  # fichier illisible : on le dit, on continue
            parts.append(f"{f.filename} : échec de lecture ({e})")
    return RedirectResponse(f"{P.format(code=code)}?msg=" + " · ".join(parts), status_code=303)


@router.post(P + "/generer")
def lp_generate(request: Request, code: str, label: str = Form(""), horizon: str = Form("")):
    company, redir = _guard(request, code)
    if redir:
        return redir
    try:
        row = service.make_forecast_set(code, label=label.strip(), horizon=int(horizon) if horizon.strip() else None, user=_who(request))
    except Exception as e:
        return RedirectResponse(f"{P.format(code=code)}?msg=Impossible de générer : {e}", status_code=303)
    return RedirectResponse(f"{P.format(code=code)}/p/{row.id}", status_code=303)


# ----------------------------------------------------------------- prévisionnels
@router.get(P + "/p/{fid}", response_class=HTMLResponse)
def lp_forecast_view(request: Request, code: str, fid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    row, res = service.get_forecast(code, fid)
    if not row:
        return RedirectResponse(f"{P.format(code=code)}?msg=Prévisionnel introuvable.", status_code=303)
    cfg = service.get_config(code)
    variants = service.set_variants(code, row.set_id) if row.set_id else []
    return templates.TemplateResponse(request, "lp_forecast.html", _ctx(request, company=company, row=row, res=res, cfg=cfg, chart=json.dumps(_chart_data(res)),
                                                                        rows=engine.pl_rows(res), ov_text=engine.describe_overrides(res.get("overrides") or {}),
                                                                        variants=variants, **_fx()))


def _chart_data(res: dict) -> dict:
    return {"labels": [engine.mlabel_en(m, short_year=True) for m in res["months"]], "group": [round(v / 1000) for v in res["group"]["cash"]],
            "entities": {e: [round(v / 1000) for v in r["cash"]] for e, r in res["entities"].items()},
            "ebitda": [round(v / 1000) for v in res["group"]["ebitda"]], "revenue": [round(v / 1000) for v in res["group"]["revenue"]]}


@router.get(P + "/p/{fid}/pdf")
def lp_forecast_pdf(request: Request, code: str, fid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    row, res = service.get_forecast(code, fid, with_pdf=True)
    if not row:
        return Response("Introuvable", status_code=404)
    pdf = row.pdf or report.forecast_pdf(service.get_config(code), res, kind="previsionnel" if row.kind != "simulation" else "simulation")
    name = f"{row.created_at:%Y%m%d} LP {'Cash flow forecast' if row.kind == 'previsionnel' else ('Scenario' if row.kind == 'variante' else 'Simulation')} {row.as_of} n{row.id}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.post(P + "/p/{fid}/supprimer")
def lp_forecast_delete(request: Request, code: str, fid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    service.delete_forecast(code, fid)
    return RedirectResponse(f"{P.format(code=code)}?msg=Prévisionnel n°{fid} supprimé.", status_code=303)


# ----------------------------------------------------------------- hypothèses
@router.get(P + "/hypotheses", response_class=HTMLResponse)
def lp_hypotheses(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    st = service.status(code)
    calib = {}
    if st["as_of"]:
        gh = engine.group_history(st["actuals"])
        for s in cfg["stores"]:
            try:
                calib[s["code"]] = engine.calibrate_store(s, st["actuals"]["stores"].get(s["code"]) or {}, cfg["general"], st["as_of"], gh)
            except Exception:
                calib[s["code"]] = {}
        ho, ho_src = engine.calibrate_ho(st["actuals"], cfg["general"], st["as_of"])
        calib["HO"] = {"value": ho, "src": ho_src}
    return templates.TemplateResponse(request, "lp_hypotheses.html", _ctx(request, company=company, cfg=cfg, st=st, calib=calib, msg=msg, **_fx()))


@router.post(P + "/hypotheses")
async def lp_hypotheses_save(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    cfg = service.get_config(code)
    g = cfg["general"]
    for key in ("horizon", "calib_months", "season_years"):
        g[key] = int(_num(f.get("g_" + key), g.get(key)) or g.get(key))
    for key in ("bank_fees_pct", "tax_ops_pct", "cit_rate_pct", "wage_inflation_pct", "cost_inflation_pct", "alert_group", "contribution_round"):
        g[key] = _num(f.get("g_" + key), g.get(key))
    g["partners"] = int(_num(f.get("g_partners"), 3) or 3)
    g["repay_lead"] = int(_num(f.get("g_repay_lead"), 1) if _num(f.get("g_repay_lead")) is not None else 1)
    g["auto_funding"] = bool(f.get("g_auto_funding"))
    g["ho_monthly"] = _num(f.get("g_ho_monthly"))
    g["ho_entity"] = (f.get("g_ho_entity") or "JZ").strip()
    g["friction"] = {e: _num(f.get(f"g_friction_{e}"), 0) or 0 for e in lpcfg.ENTITIES}
    g["branding"] = (f.get("g_branding") or "anvael").strip()
    # magasins
    stores = []
    i = 0
    while f.get(f"s{i}_code") is not None:
        c = (f.get(f"s{i}_code") or "").strip().upper()
        if c:
            custom = [_num(x, 1.0) for x in (f.get(f"s{i}_season_custom") or "").replace(";", ",").split(",") if x.strip()]
            stores.append({"code": c, "name": (f.get(f"s{i}_name") or c).strip(), "entity": (f.get(f"s{i}_entity") or "JZ").strip(),
                           "active": bool(f.get(f"s{i}_active")), "opened": _ym(f.get(f"s{i}_opened")), "closed": _ym(f.get(f"s{i}_closed")),
                           "season": (f.get(f"s{i}_season") or "auto").strip(), "season_custom": custom if len(custom) == 12 else None,
                           "runrate_annual": _num(f.get(f"s{i}_runrate_annual")), "growth_pct": _num(f.get(f"s{i}_growth_pct"), 0) or 0,
                           "food_pct": _num(f.get(f"s{i}_food_pct")), "labor": _num(f.get(f"s{i}_labor")), "rent": _num(f.get(f"s{i}_rent")),
                           "other_pct": _num(f.get(f"s{i}_other_pct")), "da_monthly": _num(f.get(f"s{i}_da_monthly")), "rent_period": int(_num(f.get(f"s{i}_rent_period"), 1) or 1),
                           "rent_first_month": _ym(f.get(f"s{i}_rent_first_month")), "ramp_months": int(_num(f.get(f"s{i}_ramp_months"), 0) or 0),
                           "ramp_start_pct": _num(f.get(f"s{i}_ramp_start_pct"), 100) or 100, "preopening_months": int(_num(f.get(f"s{i}_preopening_months"), 0) or 0),
                           "note": (f.get(f"s{i}_note") or "").strip()})
        i += 1
    if stores:
        cfg["stores"] = stores
    # prêts
    loans = []
    i = 0
    while f.get(f"l{i}_label") is not None:
        lab = (f.get(f"l{i}_label") or "").strip()
        if lab and not f.get(f"l{i}_delete"):
            loans.append({"id": (f.get(f"l{i}_id") or f"loan{i}").strip(), "label": lab, "entity": (f.get(f"l{i}_entity") or "JZ").strip(),
                          "principal": _num(f.get(f"l{i}_principal"), 0) or 0, "rate_pct": _num(f.get(f"l{i}_rate_pct"), 0) or 0,
                          "maturity": _ym(f.get(f"l{i}_maturity")) or "", "term_months": int(_num(f.get(f"l{i}_term_months"), 12) or 12),
                          "renew": bool(f.get(f"l{i}_renew")), "renew_gap": int(_num(f.get(f"l{i}_renew_gap"), 0) or 0),
                          "renew_amount": _num(f.get(f"l{i}_renew_amount")), "active": bool(f.get(f"l{i}_active")), "note": (f.get(f"l{i}_note") or "").strip()})
        i += 1
    cfg["loans"] = loans
    # événements
    events = []
    i = 0
    while f.get(f"e{i}_label") is not None:
        lab = (f.get(f"e{i}_label") or "").strip()
        m = _ym(f.get(f"e{i}_month"))
        if lab and m and not f.get(f"e{i}_delete"):
            events.append({"id": (f.get(f"e{i}_id") or f"ev{i}_{m}").strip(), "month": m, "entity": (f.get(f"e{i}_entity") or "JZ").strip(),
                           "store": (f.get(f"e{i}_store") or "").strip().upper(), "category": (f.get(f"e{i}_category") or "other").strip(),
                           "amount": _num(f.get(f"e{i}_amount"), 0) or 0, "pct": _num(f.get(f"e{i}_pct"), 0) or 0, "permanent": bool(f.get(f"e{i}_permanent")),
                           "counterparty": (f.get(f"e{i}_counterparty") or "").strip(), "active": bool(f.get(f"e{i}_active")), "label": lab,
                           "note": (f.get(f"e{i}_note") or "").strip()})
        i += 1
    cfg["events"] = sorted(events, key=lambda e: e["month"])
    service.save_config(code, cfg)
    return RedirectResponse(f"{P.format(code=code)}/hypotheses?msg=Hypothèses enregistrées.", status_code=303)


# ----------------------------------------------------------------- simulation
def _overrides_from_form(f) -> dict:
    ov = {"revenue_pct": _num(f.get("revenue_pct"), 0) or 0, "growth_pp": _num(f.get("growth_pp"), 0) or 0, "food_pp": _num(f.get("food_pp"), 0) or 0,
          "other_pp": _num(f.get("other_pp"), 0) or 0, "labor_pct": _num(f.get("labor_pct"), 0) or 0, "ga_delta": _num(f.get("ga_delta"), 0) or 0,
          "capex_scale": _num(f.get("capex_scale"), 1) if _num(f.get("capex_scale")) is not None else 1.0,
          "loan_renew": None if (f.get("loan_renew") or "") == "" else (f.get("loan_renew") == "1"),
          "loan_gap": None if (f.get("loan_gap") or "").strip() == "" else int(_num(f.get("loan_gap"), 0) or 0),
          "cca_amount": _num(f.get("cca_amount")), "cca_month": _ym(f.get("cca_month")), "cca_entity": (f.get("cca_entity") or "JZ").strip(),
          "horizon": int(_num(f.get("horizon"), 0) or 0) or None}
    act = {}
    for key in f.keys():
        if key.startswith("store_"):
            act[key[6:]] = f.get(key) == "1"
    ov["stores_active"] = act
    return {k: v for k, v in ov.items() if v not in (None, {}, "")}


@router.get(P + "/simulation", response_class=HTMLResponse)
def lp_simulation(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    cfg = service.get_config(code)
    return templates.TemplateResponse(request, "lp_simulation.html", _ctx(request, company=company, cfg=cfg, ov={}, base=None, sim=None, chart="null", **_fx()))


@router.post(P + "/simulation", response_class=HTMLResponse)
async def lp_simulation_run(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    cfg = service.get_config(code)
    ov = _overrides_from_form(f)
    try:
        base = service.preview_forecast(code, horizon=ov.get("horizon"))
        sim = service.preview_forecast(code, overrides=ov)
    except Exception as e:
        return templates.TemplateResponse(request, "lp_simulation.html", _ctx(request, company=company, cfg=cfg, ov=ov, base=None, sim=None, chart="null", error=str(e), **_fx()))
    if f.get("save"):
        row = service.make_forecast(code, label=(f.get("label") or "Simulation").strip(), kind="simulation", overrides=ov, user=_who(request),
                                    note=json.dumps(ov, ensure_ascii=False))
        return RedirectResponse(f"{P.format(code=code)}/p/{row.id}", status_code=303)
    chart = {"labels": [engine.mlabel(m) for m in sim["months"]], "base": [round(v / 1000) for v in base["group"]["cash"]], "sim": [round(v / 1000) for v in sim["group"]["cash"]],
             "sim_entities": {e: [round(v / 1000) for v in r["cash"]] for e, r in sim["entities"].items()}}
    return templates.TemplateResponse(request, "lp_simulation.html", _ctx(request, company=company, cfg=cfg, ov=ov, base=base, sim=sim, chart=json.dumps(chart), **_fx()))


# ----------------------------------------------------------------- écarts
@router.get(P + "/ecarts", response_class=HTMLResponse)
def lp_ecarts(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    st = service.status(code)
    months = sorted({m for d in st["actuals"]["stores"].values() for m in d}, reverse=True)[:24]
    return templates.TemplateResponse(request, "lp_ecarts.html", _ctx(request, company=company, st=st, months=months, forecasts=service.list_forecasts(code, kind="previsionnel", n=20),
                                                                      variances=service.list_variances(code, n=40), msg=msg, **_fx()))


@router.post(P + "/ecarts")
def lp_ecarts_run(request: Request, code: str, month: str = Form(...), forecast_id: str = Form("")):
    company, redir = _guard(request, code)
    if redir:
        return redir
    try:
        row = service.make_variance(code, month.strip()[:7], int(forecast_id) if forecast_id.strip() else None, user=_who(request))
    except Exception as e:
        return RedirectResponse(f"{P.format(code=code)}/ecarts?msg=Impossible d'analyser {month} : {e}", status_code=303)
    return RedirectResponse(f"{P.format(code=code)}/ecarts/{row.id}", status_code=303)


@router.get(P + "/ecarts/{vid}", response_class=HTMLResponse)
def lp_ecart_view(request: Request, code: str, vid: int, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    row, v = service.get_variance(code, vid)
    if not row:
        return RedirectResponse(f"{P.format(code=code)}/ecarts?msg=Analyse introuvable.", status_code=303)
    return templates.TemplateResponse(request, "lp_ecart.html", _ctx(request, company=company, row=row, v=v, msg=msg, **_fx()))


@router.get(P + "/ecarts/{vid}/pdf")
def lp_ecart_pdf(request: Request, code: str, vid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    row, v = service.get_variance(code, vid, with_pdf=True)
    if not row:
        return Response("Introuvable", status_code=404)
    pdf = row.pdf or report.variance_pdf(service.get_config(code), v)
    name = f"{row.created_at:%Y%m%d} LP Analyse des ecarts {row.month} n{row.id}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.post(P + "/ecarts/{vid}/appliquer")
def lp_ecart_apply(request: Request, code: str, vid: int, store: str = Form(...), param: str = Form(...), value: str = Form(""), mode: str = Form("set")):
    company, redir = _guard(request, code)
    if redir:
        return redir
    service.apply_proposal(code, store, param, _num(value, 0), mode)
    return RedirectResponse(f"{P.format(code=code)}/ecarts/{vid}?msg=Paramètre {param} de {store} " + ("remis en auto." if mode == "auto" else "mis à jour.") + " Regénérez un prévisionnel pour l'appliquer.", status_code=303)


# ----------------------------------------------------------------- historique, CCA, méthode
@router.get(P + "/historique", response_class=HTMLResponse)
def lp_historique(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return templates.TemplateResponse(request, "lp_historique.html", _ctx(request, company=company, h=service.history_view(code), cfg=service.get_config(code), **_fx()))


@router.get(P + "/cca", response_class=HTMLResponse)
def lp_cca(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return templates.TemplateResponse(request, "lp_cca.html", _ctx(request, company=company, c=service.cca_view(code), msg=msg, **_fx()))


@router.post(P + "/cca")
async def lp_cca_save(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    cfg = service.get_config(code)
    positions = []
    i = 0
    while f.get(f"p{i}_shareholder") is not None:
        sh = (f.get(f"p{i}_shareholder") or "").strip()
        if sh and not f.get(f"p{i}_delete"):
            positions.append({"shareholder": sh, "entity": (f.get(f"p{i}_entity") or "JZ").strip(), "amount": _num(f.get(f"p{i}_amount"), 0) or 0,
                              "source": (f.get(f"p{i}_source") or "").strip()})
        i += 1
    cfg["cca"]["positions"] = positions
    cfg["cca"]["shareholders"] = sorted({p["shareholder"] for p in positions} | set(cfg["cca"].get("shareholders") or []))
    service.save_config(code, cfg)
    return RedirectResponse(f"{P.format(code=code)}/cca?msg=Registre enregistré.", status_code=303)


@router.get(P + "/methode", response_class=HTMLResponse)
def lp_methode(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return templates.TemplateResponse(request, "lp_methode.html", _ctx(request, company=company, cfg=service.get_config(code), **_fx()))
