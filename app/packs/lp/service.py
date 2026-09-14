"""LP — accès base : configuration (Setting `lp:config`), historique importé, prévisionnels et analyses d'écarts."""
import copy
import io
import json
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from app.core.db import engine as _engine
from app.models import Setting, LpStoreMonth, LpEntityMonth, LpForecast, LpVariance
from . import config, engine, importer, report

CODE = config.COMPANY_CODE
STORE_FIELDS = ["revenue", "food", "labor", "rent", "opex_5501", "marketing", "utilities", "delivery", "amort", "depr", "ga", "fin",
                "tax_ops", "income_tax", "ebitda_report", "ebitda_after_ga", "profit_before_tax", "profit_after_tax"]
ENTITY_FIELDS = ["cash", "cash_on_hand", "ar", "deposits", "interco_recv", "prepaid", "inventory_food", "inventory_other", "fixed_assets",
                 "loans", "ap", "wages_payable", "tax_payable", "other_03", "interco_pay", "advances", "cca_14"]


# ----------------------------------------------------------------- configuration
def _setting(code: str, key: str) -> Optional[str]:
    with Session(_engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == code, Setting.key == key)).first()
        return st.value if st else None


def _save_setting(code: str, key: str, value: str) -> None:
    with Session(_engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == code, Setting.key == key)).first()
        if not st:
            st = Setting(company_code=code, key=key, value=value)
        else:
            st.value = value
            st.updated_at = datetime.utcnow()
        s.add(st)
        s.commit()


def get_config(code: str = CODE) -> dict:
    cfg = config.default_config()
    raw = _setting(code, "lp:config")
    if raw:
        try:
            saved = json.loads(raw)
        except Exception:
            saved = {}
        for k, v in (saved.get("general") or {}).items():
            cfg["general"][k] = v
        for key in ("stores", "loans", "events", "cca"):
            if key in saved:
                cfg[key] = saved[key]
        # nouveaux magasins / prêts / événements par défaut absents de la sauvegarde -> ajoutés (jamais écrasés)
        for key, idk in (("stores", "code"), ("loans", "id"), ("events", "id")):
            have = {x.get(idk) for x in cfg[key]}
            for d in config.default_config()[key]:
                if d.get(idk) not in have:
                    cfg[key].append(d)
    return cfg


def save_config(code: str, cfg: dict) -> None:
    _save_setting(code, "lp:config", json.dumps(cfg, ensure_ascii=False))


# ----------------------------------------------------------------- historique
def load_actuals(code: str = CODE) -> dict:
    out = {"stores": {}, "entities": {}}
    with Session(_engine) as s:
        for r in s.exec(select(LpStoreMonth).where(LpStoreMonth.company_code == code)).all():
            out["stores"].setdefault(r.store, {})[r.month] = {k: getattr(r, k) for k in STORE_FIELDS}
        for r in s.exec(select(LpEntityMonth).where(LpEntityMonth.company_code == code)).all():
            out["entities"].setdefault(r.entity, {})[r.month] = {k: getattr(r, k) for k in ENTITY_FIELDS}
    return out


def import_report(code: str, filename: str, data: bytes) -> dict:
    pl, bs, notes = importer.extract(io.BytesIO(data))
    n_pl = n_bs = 0
    months = []
    with Session(_engine) as s:
        for store, d in pl.items():
            for m, v in d.items():
                row = s.exec(select(LpStoreMonth).where(LpStoreMonth.company_code == code, LpStoreMonth.store == store, LpStoreMonth.month == m)).first()
                if not row:
                    row = LpStoreMonth(company_code=code, store=store, month=m)
                for k in STORE_FIELDS:
                    setattr(row, k, float(v.get(k) or 0))
                row.source, row.imported_at = filename, datetime.utcnow()
                s.add(row)
                n_pl += 1
                months.append(m)
        for ent, d in bs.items():
            for m, v in d.items():
                row = s.exec(select(LpEntityMonth).where(LpEntityMonth.company_code == code, LpEntityMonth.entity == ent, LpEntityMonth.month == m)).first()
                if not row:
                    row = LpEntityMonth(company_code=code, entity=ent, month=m)
                for k in ENTITY_FIELDS:
                    setattr(row, k, float(v.get(k) or 0))
                row.source, row.imported_at = filename, datetime.utcnow()
                s.add(row)
                n_bs += 1
                months.append(m)
        s.commit()
    return {"stores": {st: len(d) for st, d in pl.items()}, "entities": {e: len(d) for e, d in bs.items()}, "n_pl": n_pl, "n_bs": n_bs,
            "months": (min(months), max(months)) if months else None, "notes": notes}


def status(code: str = CODE) -> dict:
    a = load_actuals(code)
    as_of = engine.last_actual_month(a)
    cash = {e: ((a["entities"].get(e) or {}).get(as_of) or {}) for e in config.ENTITIES} if as_of else {}
    last_store = {st: max(d) for st, d in a["stores"].items() if d}
    return {"as_of": as_of, "cash": {e: (b.get("cash") or 0) + (b.get("cash_on_hand") or 0) for e, b in cash.items()},
            "loans": {e: -(b.get("loans") or 0) for e, b in cash.items()}, "last_store": last_store,
            "n_months": len({m for d in a["stores"].values() for m in d}), "actuals": a}


# ----------------------------------------------------------------- prévisionnels
def _row_to_result(row: LpForecast) -> dict:
    return json.loads(row.result or "{}")


def make_forecast(code: str, label: str = "", kind: str = "previsionnel", overrides: dict = None, user: str = None,
                  as_of: str = None, horizon: int = None, note: str = None) -> LpForecast:
    cfg = get_config(code)
    actuals = load_actuals(code)
    res = engine.forecast(cfg, actuals, as_of=as_of, horizon=horizon, overrides=overrides, label=label)
    if not label:
        label = f"Prévisionnel au {engine.mlabel(res['as_of'])}" if kind == "previsionnel" else f"Simulation au {engine.mlabel(res['as_of'])}"
        res["label"] = label
    pdf = report.forecast_pdf(cfg, res, kind=kind)
    row = LpForecast(company_code=code, kind=kind, label=label, created_by=user, as_of=res["as_of"], horizon=res["horizon"],
                     params=json.dumps({"config": cfg, "overrides": overrides or {}}, ensure_ascii=False),
                     result=json.dumps(res, ensure_ascii=False), pdf=pdf, note=note)
    with Session(_engine) as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    return row


def preview_forecast(code: str, overrides: dict = None, horizon: int = None) -> dict:
    return engine.forecast(get_config(code), load_actuals(code), horizon=horizon, overrides=overrides)


def list_forecasts(code: str = CODE, kind: str = None, n: int = 30) -> List[LpForecast]:
    with Session(_engine) as s:
        q = select(LpForecast).where(LpForecast.company_code == code)
        if kind:
            q = q.where(LpForecast.kind == kind)
        rows = s.exec(q.order_by(LpForecast.id.desc()).limit(n)).all()
        for r in rows:            # ne pas trimballer les PDF dans les listes
            r.pdf = None
        return rows


def get_forecast(code: str, fid: int, with_pdf: bool = False) -> Tuple[Optional[LpForecast], dict]:
    with Session(_engine) as s:
        row = s.exec(select(LpForecast).where(LpForecast.company_code == code, LpForecast.id == fid)).first()
        if not row:
            return None, {}
        res = _row_to_result(row)
        if not with_pdf:
            row.pdf = None
        return row, res


def delete_forecast(code: str, fid: int) -> bool:
    with Session(_engine) as s:
        row = s.exec(select(LpForecast).where(LpForecast.company_code == code, LpForecast.id == fid)).first()
        if not row:
            return False
        s.delete(row)
        s.commit()
        return True


def latest_forecast(code: str = CODE) -> Tuple[Optional[LpForecast], dict]:
    rows = list_forecasts(code, kind="previsionnel", n=1)
    return get_forecast(code, rows[0].id) if rows else (None, {})


# ----------------------------------------------------------------- écarts
def reference_for(code: str, month: str, forecast_id: int = None) -> Tuple[Optional[int], dict]:
    """Prévisionnel de référence pour un mois : celui demandé, sinon le dernier prévisionnel établi AVANT le
    début du mois et qui couvre ce mois, sinon le modèle recalculé au mois précédent (back-test)."""
    cfg, actuals = get_config(code), load_actuals(code)
    if forecast_id:
        row, res = get_forecast(code, forecast_id)
        if row and month in res.get("months", []):
            return row.id, res
    first_day = datetime(int(month[:4]), int(month[5:7]), 1)
    with Session(_engine) as s:
        rows = s.exec(select(LpForecast).where(LpForecast.company_code == code, LpForecast.kind == "previsionnel",
                                              LpForecast.created_at < first_day).order_by(LpForecast.id.desc()).limit(10)).all()
        for r in rows:
            res = _row_to_result(r)
            if month in res.get("months", []):
                return r.id, res
    return None, engine.backtest_reference(cfg, actuals, month)


def make_variance(code: str, month: str, forecast_id: int = None, user: str = None) -> LpVariance:
    cfg, actuals = get_config(code), load_actuals(code)
    fid, ref = reference_for(code, month, forecast_id)
    v = engine.variance(cfg, actuals, month, ref)
    v["forecast_id"] = fid
    pdf = report.variance_pdf(cfg, v)
    row = LpVariance(company_code=code, month=month, forecast_id=fid, reference=v["reference"], created_by=user,
                     result=json.dumps(v, ensure_ascii=False), pdf=pdf)
    with Session(_engine) as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    return row


def list_variances(code: str = CODE, n: int = 30) -> List[LpVariance]:
    with Session(_engine) as s:
        rows = s.exec(select(LpVariance).where(LpVariance.company_code == code).order_by(LpVariance.id.desc()).limit(n)).all()
        for r in rows:
            r.pdf = None
        return rows


def get_variance(code: str, vid: int, with_pdf: bool = False) -> Tuple[Optional[LpVariance], dict]:
    with Session(_engine) as s:
        row = s.exec(select(LpVariance).where(LpVariance.company_code == code, LpVariance.id == vid)).first()
        if not row:
            return None, {}
        v = json.loads(row.result or "{}")
        if not with_pdf:
            row.pdf = None
        return row, v


def apply_proposal(code: str, store: str, param: str, value, mode: str = "set") -> None:
    cfg = get_config(code)
    v = None
    if mode != "auto":
        v = float(value or 0)
        v = round(v, 1) if param.endswith("_pct") else (round(v / 1000) * 1000 if param == "runrate_annual" else round(v / 100) * 100)
    if store == "HO" and param == "ho_monthly":
        cfg["general"]["ho_monthly"] = v
    else:
        for s in cfg["stores"]:
            if s["code"] == store:
                s[param] = v
    save_config(code, cfg)


# ----------------------------------------------------------------- vues (historique, CCA)
def history_view(code: str = CODE) -> dict:
    a = load_actuals(code)
    cfg = get_config(code)
    stores = {}
    for st, d in a["stores"].items():
        rows = []
        for m in sorted(d):
            v = d[m]
            rev = v.get("revenue") or 0
            other = engine.other_of(v)
            rows.append({"month": m, "revenue": rev, "food": v.get("food") or 0, "food_pct": (v.get("food") or 0) / rev if rev else None,
                         "labor": v.get("labor") or 0, "labor_pct": (v.get("labor") or 0) / rev if rev else None, "rent": v.get("rent") or 0,
                         "other": other, "other_pct": other / rev if rev else None, "ebitda": engine.ebitda_of(v), "ebitda_report": v.get("ebitda_report") or 0,
                         "ga": v.get("ga") or 0, "fin": v.get("fin") or 0, "amort": v.get("amort") or 0, "depr": v.get("depr") or 0})
        stores[st] = rows
    # pont de trésorerie historique par entité
    bridges = {}
    ho = a["stores"].get("HO") or {}
    ho_entity = cfg["general"].get("ho_entity") or "JZ"
    store_entity = {s["code"]: s.get("entity") for s in cfg["stores"]}
    for e, d in a["entities"].items():
        rows, prev = [], None
        for m in sorted(d):
            b = d[m]
            if prev is not None:
                pm = prev
                bp = d[pm]
                eb = sum(engine.ebitda_of((a["stores"].get(st) or {}).get(m) or {}) for st, ent in store_entity.items() if ent == e)
                fin = sum((((a["stores"].get(st) or {}).get(m) or {}).get("fin") or 0) for st, ent in store_entity.items() if ent == e)
                ga = (ho.get(m) or {}).get("ga") or 0 if e == ho_entity else 0
                fin += ((ho.get(m) or {}).get("fin") or 0) if e == ho_entity else 0
                dd = lambda k: (b.get(k) or 0) - (bp.get(k) or 0)
                dcash = dd("cash") + dd("cash_on_hand")
                d_loans, d_cca, d_interco = -dd("loans"), -(dd("cca_14") + dd("other_03")), -(dd("interco_recv") - dd("interco_pay"))
                amort = sum((((a["stores"].get(st) or {}).get(m) or {}).get("amort") or 0) for st, ent in store_entity.items() if ent == e)
                capex = -(dd("fixed_assets") + amort)
                wc = -(dd("ap") + dd("wages_payable") + dd("tax_payable") + dd("advances")) - (dd("ar") + dd("inventory_food") + dd("inventory_other") + dd("deposits") + dd("prepaid"))
                residual = dcash - (eb - ga - fin + d_loans + d_cca + d_interco + capex + wc)
                rows.append({"month": m, "dcash": dcash, "ebitda": eb, "ga": ga, "fin": fin, "loans": d_loans, "cca": d_cca, "interco": d_interco,
                             "capex": capex, "wc": wc, "residual": residual, "cash": (b.get("cash") or 0) + (b.get("cash_on_hand") or 0)})
            prev = m
        bridges[e] = rows
    order = [s["code"] for s in cfg["stores"] if s.get("active")] + [s["code"] for s in cfg["stores"] if not s.get("active")] + ["HO"]
    stores = {**{c: stores[c] for c in order if c in stores}, **{c: v for c, v in stores.items() if c not in order}}
    return {"stores": stores, "entities": {e: [{"month": m, **d[m]} for m in sorted(d)] for e, d in a["entities"].items()}, "bridges": bridges,
            "as_of": engine.last_actual_month(a)}


def cca_view(code: str = CODE) -> dict:
    cfg = get_config(code)
    a = load_actuals(code)
    as_of = engine.last_actual_month(a)
    acc = {}
    for e in config.ENTITIES:
        b = (a["entities"].get(e) or {}).get(as_of) or {}
        acc[e] = {"cca_14": -(b.get("cca_14") or 0), "other_03": -(b.get("other_03") or 0)}
    series = {}
    for e, d in a["entities"].items():
        series[e] = [{"month": m, "cca_14": -(d[m].get("cca_14") or 0), "other_03": -(d[m].get("other_03") or 0)} for m in sorted(d)]
    reg = cfg.get("cca") or {}
    by_entity = {}
    for p in reg.get("positions", []):
        by_entity.setdefault(p.get("entity"), 0.0)
        by_entity[p["entity"]] += float(p.get("amount") or 0)
    return {"as_of": as_of, "accounts": acc, "series": series, "registry": reg, "by_entity": by_entity,
            "planned": [e for e in cfg.get("events", []) if e.get("category") == "cca"]}
