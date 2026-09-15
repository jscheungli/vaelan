"""OWINE — export et international : zone de destination, règles Chrono Viti par pays, état des formalités (contrôles automatiques
et étapes cochées), calcul des valeurs hors taxes, données de la facture commerciale, formulaire public « informations douanières »,
réglages de l'exportateur (EORI, signature), données douanières des vins et écriture des codes SH dans Shopify.

Zones : FR (territoire fiscal français : rien de particulier) · UE (lettre de transport seule ; TVA du pays de destination et accises à
traiter) · EXPORT (hors UE et DROM-COM : facture commerciale en 3 exemplaires sur le colis A, EORI, incoterm DAP, déclaration d'origine)."""
import json
import math
import os
import re
import secrets
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import config, export_config as X, service

INV_KEY = "owine:export_invoice:{name}"
SETTINGS_KEY = "owine:export:settings"
SIGN_KEY = "owine:export:signature"

PO_BOX = re.compile(r"\b(b\.?p\.?\s*\d|bo[iî]te postale|p\.?o\.?\s*box|postfach|cedex\s*\d*\s*bp|casella postale|apartado)\b", re.I)


# ================================================================== zone, règles
def zone(country: Optional[str]) -> str:
    c = (country or "FR").upper()
    if c in X.FR_FISCAL:
        return "FR"
    if c in X.EU:
        return "UE"
    return "EXPORT"


def zone_label(z: str) -> str:
    return {"FR": "France", "UE": "Union européenne (intracommunautaire)", "EXPORT": "Hors UE (export, douane)"}.get(z, z)


def rule(country: Optional[str]) -> Optional[dict]:
    return X.COUNTRY_RULES.get((country or "").upper())


def dest_kind(o) -> str:
    """Société seulement si le client l'a dit (attribut du panier, n° de TVA ou EORI) ou si JS l'a choisi ; un nom de société dans l'adresse (livraison au bureau) ne suffit pas."""
    if o.customer_type in ("societe", "particulier"):
        return o.customer_type
    return "societe" if (o.vat_number or o.eori) else "particulier"


def kind_label(k: str) -> str:
    return "société (B2B)" if k == "societe" else "particulier (B2C)"


def product_status(country: str, kind: str, product: str) -> Tuple[str, str]:
    """(état, libellé) de l'offre Chronopost pour ce pays et ce type de client : ok / warn / ko."""
    c = (country or "").upper()
    if c in X.FORBIDDEN:
        return "ko", "destination interdite par Chrono Viti"
    if zone(c) == "FR":
        return ("ok", "France : Chrono 13") if product == "chrono13" else ("warn", "en France, utiliser Chrono 13")
    r = rule(c)
    zo = X.zoning(product, c)
    if not r:
        return ("warn", f"pas de fiche pays Viti ({zo[0]}, zone {zo[1]}, J+{zo[2]}) : à confirmer avec Chronopost") if zo else ("ko", "pays non desservi par cette offre (zoning Chronopost)")
    v = (r.get("b2b" if kind == "societe" else "b2c") or {}).get(product)
    if v is None:
        return "ko", f"{X.PRODUCTS[product]['label']} interdit vers ce pays pour un {kind_label(kind)}"
    if v == "n.c.":
        return ("warn", f"{X.PRODUCTS[product]['label']} : délai non communiqué dans le guide" + (f" (zoning : J+{zo[2]})" if zo else "") + " — à confirmer")
    return "ok", f"{X.PRODUCTS[product]['label']} {v}" + (f" · zone tarifaire {zo[1]}" if zo else "")


def default_product(country: str, kind: str) -> str:
    c = (country or "").upper()
    if zone(c) == "FR":
        return "chrono13"
    for p in ("classic", "express"):
        if product_status(c, kind, p)[0] == "ok":
            return p
    for p in ("classic", "express"):
        if product_status(c, kind, p)[0] == "warn":
            return p
    return "express"


def delay_days(country: str, kind: str, product: str) -> int:
    """Borne haute du délai indicatif (jours ouvrés) : fiche pays sinon zoning, sinon 4."""
    r = rule(country); v = ((r or {}).get("b2b" if kind == "societe" else "b2c") or {}).get(product)
    zo = X.zoning(product, country)
    m = re.findall(r"\d+", v or "") if isinstance(v, str) else []
    if m:
        return int(m[-1])
    return int(zo[2]) if zo and zo[2] else 4


def delay_text(country: str, kind: str, product: str, lang: str = "fr") -> str:
    """Délai annoncé au client : fiche pays si connue, sinon zoning ; en jours ouvrés après l'enlèvement."""
    r = rule(country)
    v = ((r or {}).get("b2b" if kind == "societe" else "b2c") or {}).get(product)
    zo = X.zoning(product, country)
    if v and v not in ("n.c.",):
        d = v.replace("J+", "")
    elif zo:
        d = str(zo[2])
    else:
        return "" if lang == "en" else ""
    if lang == "en":
        return f"{d.replace('/', ' to ')} working day{'s' if d not in ('1',) else ''} after collection"
    return f"{d.replace('/', ' à ')} jour{'s' if d not in ('1',) else ''} ouvré{'s' if d not in ('1',) else ''} après l'enlèvement"


# ================================================================== état export d'une commande (JSON)
def get_state(o) -> dict:
    try:
        d = json.loads(o.export_json or "{}") or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def set_state(o, st: dict) -> None:
    o.export_json = json.dumps(st, ensure_ascii=False, default=str)
    service.save_order(o)


def set_info(o, by: str = None, **fields) -> None:
    """Réglages export de la commande (produit, incoterm, n° et date de facture, port HT forcé, notes) et identité du destinataire."""
    st = get_state(o)
    for k in ("product", "incoterm", "invoice_no", "invoice_date", "shipping_ht", "insurance_ht", "notes", "content_desc"):
        if k in fields:
            st[k] = fields[k]
    for k in ("customer_type", "vat_number", "eori", "tax_id", "billing_company"):
        if k in fields:
            setattr(o, k, (fields[k] or "").strip() or None)
    st.setdefault("log", []).append({"at": service.now_local().isoformat(timespec="minutes"), "by": by, "what": "infos"})
    set_state(o, st)


MANUAL_CHECK_KEYS = {"recipient_ok", "eur1", "signed", "vat", "vat_oss", "excise", "cleared", "proof", "vat_checked", "perimeter_ok"}


def toggle_check(o, key: str, done: bool, by: str = None, note: str = None) -> None:
    if key not in MANUAL_CHECK_KEYS:
        return
    st = get_state(o)
    ch = st.setdefault("checks", {})
    if done:
        ch[key] = {"done": True, "at": service.now_local().isoformat(timespec="minutes"), "by": by, "note": note or (ch.get(key) or {}).get("note")}
    else:
        ch[key] = {"done": False, "note": note or (ch.get(key) or {}).get("note")}
    set_state(o, st)


def product_of(o) -> str:
    st = get_state(o)
    return st.get("product") or default_product(o.country, dest_kind(o))


def incoterm_of(o) -> str:
    return get_state(o).get("incoterm") or "DAP"


# ================================================================== montants hors taxes
def ht(amount_ttc: float, o) -> float:
    """Shopify facture TTC (TVA française) tant que la boutique n'exclut pas la TVA à l'export ; si aucune TVA n'a été prélevée, les prix sont déjà HT."""
    if (o.tax_total or 0) > 0:
        return round(float(amount_ttc or 0) / (1 + (o.tax_rate or 0.2)), 2)
    return round(float(amount_ttc or 0), 2)


def line_values(o) -> List[dict]:
    """Lignes de la commande valorisées HT : [{sku, title, qty, unit_ttc, unit_ht, total_ht, item}]."""
    imap = service.item_map()
    out = []
    for l in service.order_lines(o):
        unit = float(l["net"]) if l.get("net") is not None else float(l.get("price") or 0)
        if unit <= 0:                                            # bouteille offerte ou remise 100 % : la douane exige une valeur réaliste → prix catalogue
            it0 = imap.get(l.get("sku"))
            unit = float(l.get("price") or 0) or float((it0.price if it0 and it0.price else 0) or 0)
        u = ht(unit, o)
        out.append({"sku": l.get("sku"), "title": l.get("title"), "qty": int(l.get("qty") or 0), "unit_ttc": unit, "unit_ht": u, "total_ht": round(u * int(l.get("qty") or 0), 2),
                    "item": imap.get(l.get("sku"))})
    return out


def _expand_selection_lines(o, lines: List[dict]) -> List[dict]:
    """Une « sélection » (SKU SEL-…) n'est pas une bouteille : pour la douane on déclare les bouteilles qu'elle contient. Les cartons
    portent déjà les bouteilles réelles ; on prend donc les lignes des cartons quand la commande contient une sélection."""
    return lines


def totals(o, cs) -> dict:
    st = get_state(o)
    lines = line_values(o)
    goods = round(sum(l["total_ht"] for l in lines), 2)
    ship = st.get("shipping_ht")
    ship = float(ship) if ship not in (None, "") else ht(o.shipping_paid or 0, o)
    ins = float(st.get("insurance_ht") or 0)
    bottles = sum(int(l["qty"]) for c in cs for l in service.carton_lines(c)) if cs else sum(int(l["qty"]) for l in service.order_lines(o))
    gross = round(sum(c.weight_kg for c in cs), 1) if cs else round(bottles * config.BOTTLE_KG, 1)
    pack = round(sum((config.PACKAGING.get(c.box_sku) or {}).get("kg", 0) for c in cs), 2) if cs else 0
    return {"lines": lines, "goods_ht": goods, "shipping_ht": round(ship, 2), "insurance_ht": round(ins, 2), "total_ht": round(goods + ship + ins, 2),
            "bottles": bottles, "gross_kg": gross, "net_kg": round(max(gross - pack, bottles * X.NET_KG_PER_BOTTLE), 1) if cs else round(bottles * X.NET_KG_PER_BOTTLE, 1),
            "eur1": goods > X.EUR1_THRESHOLD, "vat_charged": (o.tax_total or 0) > 0}


# ================================================================== envois (Suisse particuliers : 6 bouteilles / 10 kg par envoi → un envoi par carton)
def shipments(o, cs) -> List[dict]:
    """[{suffix, cartons}] : un seul envoi multi-colis, sauf si la fiche pays limite l'envoi (ex. CH particuliers 6 btl / 10 kg) et que la commande dépasse la limite."""
    r = rule(o.country); k = dest_kind(o); p = product_of(o)
    lim = ((r or {}).get("b2b" if k == "societe" else "b2c") or {})
    maxb, maxkg = lim.get("max_bottles"), lim.get("max_kg")
    nb = sum(int(l["qty"]) for c in cs for l in service.carton_lines(c))
    kg = sum(c.weight_kg for c in cs)
    if cs and p == "classic" and ((maxb and nb > maxb) or (maxkg and kg > maxkg)) and len(cs) > 1:
        return [{"suffix": c.ref, "cartons": [c]} for c in cs]
    return [{"suffix": "", "cartons": list(cs)}]


# ================================================================== réglages exportateur, signature, données douanières des vins
def settings() -> dict:
    d = dict(X.EXPORTER)
    d["box_dims"] = dict(X.BOX_DIMS_DEFAULT)
    saved = service._setting_get(SETTINGS_KEY) or {}
    for k, v in saved.items():
        if k == "box_dims" and isinstance(v, dict):
            d["box_dims"].update(v)
        elif v not in (None, ""):
            d[k] = v
    return d


def save_settings(**kw) -> None:
    saved = service._setting_get(SETTINGS_KEY) or {}
    for k, v in kw.items():
        if k == "box_dims":
            saved.setdefault("box_dims", {}).update({kk: (vv or "").strip() for kk, vv in (v or {}).items()})
        else:
            saved[k] = (v or "").strip()
    service._setting_set(SETTINGS_KEY, saved)


def signature() -> Optional[bytes]:
    import base64
    d = service._setting_get(SIGN_KEY)
    return base64.b64decode(d["b64"]) if d and d.get("b64") else None


def set_signature(data: Optional[bytes], name: str = "") -> None:
    import base64
    service._setting_set(SIGN_KEY, {"b64": base64.b64encode(data).decode(), "name": name, "at": datetime.utcnow().isoformat(timespec="seconds")} if data else {})


def colour_from_sku(sku: str) -> Optional[str]:
    """Convention des références oWine : lettre de couleur puis format puis millésime (…RB14 = rouge bouteille 2014, …BB23 = blanc, …RM19 = rouge magnum) ;
    nouvelles références « D…-…-R23 » / « -B23 »."""
    u = (sku or "").upper()
    m = re.search(r"([RB])[BM]\d{2}$", u) or re.search(r"-([RB])\d{2}$", u)
    return {"R": "rouge", "B": "blanc"}.get(m.group(1)) if m else None


RED_WORDS = ("rouge", "pinot noir", "red", "gevrey", "chambertin", "morey-saint-denis", "morey saint denis", "chambolle", "vosne", "nuits-saint-georges", "nuits saint georges", "pommard", "volnay",
             "clos de la roche", "clos saint-denis", "clos vougeot", "musigny", "échezeaux", "echezeaux", "richebourg", "romanée", "romanee", "bonnes-mares", "clos des lambrays", "corton bressandes", "aloxe")
WHITE_WORDS = ("blanc", "chardonnay", "aligoté", "aligote", "white", "meursault", "corton-charlemagne", "bâtard-montrachet", "batard-montrachet", "chevalier-montrachet", "criots-bâtard", "bienvenues-bâtard", "le montrachet")


def colour_from_title(title: str) -> Optional[str]:
    """Couleur déduite du nom ; « Chassagne-Montrachet », « Puligny-Montrachet », Saint-Aubin, Santenay… existent en rouge et en blanc : pas de déduction (à confirmer)."""
    t = " " + (title or "").lower() + " "
    if any(w in t for w in RED_WORDS):
        return "rouge"
    if any(w in t for w in WHITE_WORDS):
        return "blanc"
    return None


def customs_row(it) -> dict:
    """Données douanières d'un vin (valeurs saisies, sinon déduites) + ce qui manque."""
    colour = (it.couleur or "").lower().strip() or colour_from_sku(it.sku) or colour_from_title(it.title)
    saved_hs = (it.hs_code or "").strip()
    hs = saved_hs if saved_hs and saved_hs != X.HS_GENERIC else X.CN_BY_COLOUR.get(colour or "", X.HS_GENERIC)   # le code générique est re-précisé dès que la couleur est connue
    missing = []
    if not it.abv:
        missing.append("degré")
    if not colour:
        missing.append("couleur")
    if it.millesime is None:
        missing.append("millésime")                              # 0 = sans millésime (NV), accepté
    return {"sku": it.sku, "title": it.title, "colour": colour, "colour_saved": bool(it.couleur), "abv": it.abv, "abv_status": it.abv_status or ("estime" if it.abv else None), "abv_source": it.abv_source,
            "confirmed_at": it.customs_confirmed_at, "confirmed_by": it.customs_confirmed_by, "volume_cl": it.volume_cl or 75, "hs": hs, "hs_saved": bool(saved_hs and saved_hs != X.HS_GENERIC), "nv": it.millesime == 0,
            "origin": (it.origin or X.ORIGIN_DEFAULT).upper(), "vintage": it.millesime, "vigneron": it.vigneron, "appellation": it.appellation, "missing": missing, "kind": it.kind, "status": it.status}


def customs_rows(active_only: bool = True) -> List[dict]:
    rows = [customs_row(it) for it in service.items("wine") if not active_only or it.status == "ACTIVE"]
    rows.sort(key=lambda r: (0 if r["missing"] else (1 if r.get("abv_status") != "confirme" else 2), r["title"] or ""))
    return rows


def save_customs_rows(form: dict) -> int:
    """Formulaire de masse de la page Douane : champs abv_<sku>, colour_<sku>, hs_<sku>, origin_<sku>, vol_<sku>."""
    n = 0
    skus = {k.split("_", 1)[1] for k in form if k.startswith(("abv_", "colour_", "hs_", "origin_", "vol_"))}
    for sku in skus:
        fields = {}
        vint = (form.get(f"vintage_{sku}") or "").strip().upper()
        if vint in ("NV", "SM", "S.M.", "0"):
            fields["millesime"] = 0
        elif re.fullmatch(r"(19|20)\d{2}", vint):
            fields["millesime"] = int(vint)
        v = re.sub(r"[^0-9.,]", "", (form.get(f"abv_{sku}") or "")).replace(",", ".").strip()
        try:
            fields["abv"] = float(v) if v else None
        except ValueError:
            fields["abv"] = None
        if fields["abv"] is not None and not (5 <= fields["abv"] <= 25):
            fields["abv"] = None
        cur = service.get_item(sku)
        if cur and fields["abv"] is not None and (cur.abv or 0) != fields["abv"]:
            fields["abv_source"] = "saisi à la main"; fields["abv_status"] = "estime" if cur.abv_status != "confirme" else "confirme"
            if cur.abv_status == "confirme":
                log = service._setting_get(LOG_KEY) or []
                log.insert(0, {"at": service.now_local().strftime("%d/%m/%Y %H:%M"), "by": form.get("_by") or "?", "sku": sku, "title": cur.title, "abv": fields["abv"], "colour": cur.couleur, "hs": cur.hs_code, "source": f"degré confirmé modifié à la main ({cur.abv} → {fields['abv']})"})
                service._setting_set(LOG_KEY, log[:500])
        if cur and fields["abv"] is None and cur.abv_status == "confirme":
            fields["abv_status"] = "estime"; fields["customs_confirmed_at"] = None; fields["customs_confirmed_by"] = None   # degré effacé : plus confirmé
        c = (form.get(f"colour_{sku}") or "").strip().lower()
        if c:
            fields["couleur"] = c
        h = re.sub(r"[^0-9]", "", form.get(f"hs_{sku}") or "")
        fields["hs_code"] = h or None
        og = (form.get(f"origin_{sku}") or "").strip().upper()
        if og:
            fields["origin"] = og
        vol = (form.get(f"vol_{sku}") or "").strip()
        if vol.isdigit():
            fields["volume_cl"] = int(vol)
        it = service.get_item(sku)
        if it:
            from sqlmodel import Session
            from app.core.db import engine
            from app.models import OwItem
            with Session(engine) as s:
                row = s.get(OwItem, it.id)
                for k, val in fields.items():
                    setattr(row, k, val)
                row.updated_at = datetime.utcnow(); s.add(row); s.commit()
            n += 1
    return n


def wine_description(row: dict, lang: str = "both") -> str:
    """Description exigée par Chronopost : domaine, appellation, couleur, millésime, contenance, degré, origine — en anglais pour les pays non francophones."""
    col = {"rouge": "Red wine / Vin rouge", "blanc": "White wine / Vin blanc", "rosé": "Rosé wine", "rose": "Rosé wine"}.get(row.get("colour") or "", "Wine / Vin")
    parts = [f"{col} — {row.get('title') or row.get('sku')}"]
    if row.get("vigneron") and (row.get("vigneron") or "").lower() not in (row.get("title") or "").lower():
        parts.append(f"Domaine {row['vigneron']}")
    parts.append("AOP Bourgogne (PDO)")
    parts.append(f"{row.get('volume_cl') or 75} cl")
    if row.get("abv"):
        parts.append(f"{row['abv']:g} % vol")
    return " · ".join(parts)


# ================================================================== contrôles (checklist) d'une commande internationale
def _recipient_issues(o) -> List[str]:
    miss = []
    if not (o.address1 and o.zip and o.city and o.country):
        miss.append("adresse complète")
    if not o.phone:
        miss.append("téléphone (obligatoire pour le dédouanement et la livraison)")
    if not o.email:
        miss.append("e-mail")
    if PO_BOX.search(" ".join(x for x in (o.address1, o.address2) if x)):
        miss.append("boîte postale : une adresse physique est exigée")
    k = dest_kind(o)
    if zone(o.country) == "EXPORT" and k == "societe" and not (o.vat_number or o.eori):
        miss.append("société : n° EORI (ou n° de TVA / identifiant d'entreprise) du destinataire")
    if zone(o.country) == "UE" and k == "societe" and not o.vat_number:
        miss.append("société UE : n° de TVA intracommunautaire")
    if k == "particulier" and (o.country or "").upper() in X.TAX_ID_B2C and not o.tax_id:
        miss.append(f"particulier : {X.TAX_ID_B2C[(o.country or '').upper()]} exigé par le pays")
    return miss


def _wine_issues(o, cs) -> List[str]:
    imap = service.item_map(); out = []
    lines = [l for c in cs for l in service.carton_lines(c)] if cs else service.order_lines(o)
    seen = set()
    for l in lines:
        sku = l.get("sku")
        if not sku or sku in seen:
            continue
        seen.add(sku)
        it = imap.get(sku)
        if not it:
            out.append(f"{sku} : article inconnu"); continue
        if it.kind == "selection":
            out.append(f"{it.title} : sélection — ventiler les bouteilles réelles dans les cartons"); continue
        r = customs_row(it)
        if r["missing"]:
            out.append(f"{it.title} : {', '.join(r['missing'])}")
    return out


def state(o, cs) -> dict:
    """Tout ce que la page de commande affiche : zone, type, produit, règles, contrôles (auto + cochés), points bloquants, avertissements."""
    z = zone(o.country); k = dest_kind(o); p = product_of(o); st = get_state(o); checks_done = st.get("checks") or {}
    r = rule(o.country); c = (o.country or "").upper()
    ps, plabel = product_status(c, k, p)
    tot = totals(o, cs)
    ships = shipments(o, cs)
    inv = st.get("invoice") or {}
    checks: List[dict] = []

    def add(key, label, ok=None, detail="", manual=False, blocking=True, when=True):
        if not when:
            return
        d = checks_done.get(key) or {}
        done = bool(d.get("done")) if manual else bool(ok)
        checks.append({"key": key, "label": label, "ok": done, "detail": detail, "manual": manual, "blocking": blocking, "at": d.get("at"), "by": d.get("by"), "note": d.get("note")})

    add("destination", f"Destination {X.country_name(c)} · {kind_label(k)} · {X.PRODUCTS[p]['label']}", ok=(ps == "ok"), detail=plabel, blocking=(ps == "ko"))
    zopen, zlabel = zone_open(c, k); zn = zone_of(c) or {}
    nb_all = tot["bottles"]; per_issues = []
    if not zopen:
        per_issues.append(zlabel)
    if zn.get("max_bottles") and nb_all > zn["max_bottles"]:
        per_issues.append(f"{nb_all} bouteilles pour {zn['max_bottles']} maximum par commande")
    if zn.get("multiple") and nb_all % zn["multiple"]:
        per_issues.append(f"{nb_all} bouteilles : expédition par carton de {zn['multiple']}")
    if zn.get("min_order") and tot["goods_ht"] < zn["min_order"]:
        per_issues.append(f"marchandise {tot['goods_ht']:.0f} € HT sous le minimum de {zn['min_order']:.0f} €")
    vies = None
    if zn.get("vat_required") and k == "societe":
        vies = vies_check(o.vat_number) if o.vat_number else {"valid": False, "error": "n° de TVA absent"}
        if vies.get("na"):
            per_issues.append(f"n° de TVA non européen ({o.vat_number}) : une société de l'UE doit avoir un n° intracommunautaire")
        elif vies.get("valid") is False:
            per_issues.append(f"n° de TVA intracommunautaire invalide ({vies.get('error') or o.vat_number})")
        elif vies.get("valid") is None:
            per_issues.append(f"VIES injoignable : {vies.get('error')} — vérifier à la main puis cocher « TVA vérifiée »")
    vat_manual = bool((checks_done.get("vat_checked") or {}).get("done")) and bool((checks_done.get("vat_checked") or {}).get("note"))
    per_manual = bool((checks_done.get("perimeter_ok") or {}).get("done")) and bool((checks_done.get("perimeter_ok") or {}).get("note"))
    vat_issues = [x for x in per_issues if "TVA" in x or "VIES" in x]; other_issues = [x for x in per_issues if x not in vat_issues]
    per_ok = (not other_issues or per_manual) and (not vat_issues or vat_manual)
    add("perimeter", f"Périmètre ouvert : {zlabel}" if zopen else "Périmètre de vente", ok=per_ok,
        detail=" · ".join(per_issues) + (" — dérogation notée" if per_ok and per_issues else "") if per_issues else ((f"TVA {o.vat_number} valide sur VIES" + (f" : {vies.get('name')}" if vies and vies.get('name') else "")) if vies and vies.get('valid') else "règles de la zone respectées"))
    add("perimeter_ok", "Dérogation : commande acceptée hors périmètre (motif obligatoire)", manual=True, blocking=False, when=bool(other_issues))
    add("vat_checked", "N° de TVA vérifié à la main (motif obligatoire : VIES indisponible, immatriculation récente…)", manual=True, blocking=False, when=bool(vies) and vies.get("valid") is not True)
    if (o.company or o.billing_company) and k == "particulier":
        add("company_hint", f"Nom de société dans l'adresse ({o.company or o.billing_company}) mais commande traitée en particulier : confirmer le type (réglages ci-dessous)", ok=bool(o.customer_type), blocking=False)
    if z != "FR" and (o.tax_total or 0) > 0:
        add("vat_collected", f"TVA française encaissée par Shopify : {o.tax_total:.2f} € — à rembourser au client ou à régulariser sur la facture Pennylane (réglage Taxes « exclure la TVA selon le pays »)", ok=False, blocking=False)
    rec = _recipient_issues(o)
    rec_manual = bool((checks_done.get("recipient_ok") or {}).get("done"))
    pobox_only = bool(rec) and all("boîte postale" in x for x in rec)
    add("recipient", "Coordonnées du destinataire complètes", ok=not rec or (pobox_only and rec_manual), detail=" · ".join(rec) if rec else "adresse, téléphone, e-mail" + (", EORI / TVA" if k == "societe" else ""))
    add("recipient_ok", "Coordonnées confirmées (client ou formulaire douane)", manual=True, blocking=False, detail="formulaire public envoyé le " + str(st.get("customs_form", {}).get("sent_at") or "—") + (", rempli le " + str(st["customs_form"]["submitted_at"]) if st.get("customs_form", {}).get("submitted_at") else ""))
    if z == "EXPORT":
        wi = _wine_issues(o, cs)
        add("wines", "Données douanières des vins (couleur, millésime, degré, code SH, origine)", ok=not wi, detail=" · ".join(wi) if wi else "toutes renseignées")
        zeros = [l["title"] for l in tot["lines"] if l["qty"] and l["unit_ht"] <= 0]
        add("values", f"Valeurs hors taxes : marchandises {tot['goods_ht']:.2f} EUR + port {tot['shipping_ht']:.2f} EUR = {tot['total_ht']:.2f} EUR", ok=not zeros,
            detail=("prix nul interdit en douane : " + ", ".join(zeros)) if zeros else ("TVA française prélevée par Shopify, prix convertis HT (÷ 1,2)" if tot["vat_charged"] else "vente hors taxes (aucune TVA prélevée)"))
        add("eur1", f"Valeur supérieure à {X.EUR1_THRESHOLD:.0f} EUR : certificat d'origine EUR.1 joint", manual=True, when=tot["eur1"], detail="au-delà de 6 000 € la déclaration d'origine sur facture ne suffit plus")
    if cs and z != "FR":
        bad = [x.ref for x in cs if x.box_sku != X.INTL_BOX or sum(int(l["qty"]) for l in service.carton_lines(x)) != 6]
        add("box6", "International : uniquement des cartons de 6 pleins (réf. 2036, 38 × 28 × 40 cm)", ok=not bad, blocking=False, detail=("cartons hors règle : " + ", ".join(bad)) if bad else "respecté")
    add("cartons", "Cartons validés" + (f" · {len(ships)} envois séparés (limite {((r or {}).get('b2b' if k == 'societe' else 'b2c') or {}).get('max_bottles')} bouteilles / envoi)" if len(ships) > 1 else ""), ok=bool(cs),
        detail=f"{len(cs)} carton(s), {tot['bottles']} bouteille(s), {tot['gross_kg']} kg brut" if cs else "ventiler la commande")
    lim = ((r or {}).get("b2b" if k == "societe" else "b2c") or {})
    if cs and lim.get("max_bottles") and p == "classic":
        bad = [x.ref for x in cs if sum(int(l["qty"]) for l in service.carton_lines(x)) > lim["max_bottles"]]
        add("limit", f"Chaque envoi ≤ {lim['max_bottles']} bouteilles" + (f" et ≤ {lim['max_kg']} kg" if lim.get("max_kg") else ""), ok=not bad, detail=("cartons trop lourds : " + ", ".join(bad)) if bad else "respecté")
    add("labels", "Étiquettes Chronopost (n° de colis) sur tous les cartons", ok=bool(cs) and all(x.tracking for x in cs), detail="saisir le produit " + X.PRODUCTS[p]["label"] + " sur chronopost.fr ; les numéros figurent sur la facture")
    if z == "EXPORT":
        s_ = settings()
        add("eori", "N° EORI d'oWine renseigné (Réglages douane)", ok=bool(re.fullmatch(r"FR[0-9A-Z]{9,17}", (s_.get("eori") or "").replace(" ", "").upper())), detail=s_.get("eori") or "obligatoire : sans EORI aucune exportation n'est possible")
        uw = unconfirmed_wines(o, cs)
        sent = st.get("abv_request_sent_at")
        add("abv", "Degrés d'alcool confirmés (étiquette) pour tous les vins de la commande", ok=not uw, blocking=False,
            detail=(("à confirmer : " + " · ".join(f"{w['title']} ({w['abv']:g} % estimé)" if w.get("abv") else w["title"] for w in uw)
                     + (f" — demande incluse dans l'e-mail Alix / envoyée le {sent}" if sent else " — la demande part dans l'e-mail de préparation à Alix ; saisir la réponse ci-dessous")) if uw else "tous confirmés"))
        final = bool(inv.get("final"))
        add("invoice", "Facture commerciale définitive générée (numéros de colis, degrés confirmés)", ok=final and not uw, blocking=False,
            detail=(f"version {inv.get('version')} du {inv.get('at')}" + ("" if inv.get("final") else " — PROVISOIRE : regénérer après les étiquettes et les degrés")) if inv else "à générer une fois les étiquettes reçues ; une version provisoire part avec l'e-mail de préparation")
        add("invoice_alix", "Facture définitive envoyée à Alix (3 exemplaires à imprimer avant l'enlèvement)", ok=bool(st.get("final_invoice_sent_at")) or (bool(o.sent_alix_at) and final and not uw and not st.get("final_invoice_pending")), blocking=False,
            detail=(f"envoyée le {st['final_invoice_sent_at']}" if st.get("final_invoice_sent_at") else ("partie avec l'e-mail de préparation" if o.sent_alix_at and final and not uw and not st.get("final_invoice_pending") else "bouton « Envoyer la facture définitive à Alix » une fois les degrés saisis et les étiquettes reçues")))
        add("signature", "Signature de l'exportateur apposée (image enregistrée dans Réglages douane)", ok=bool(signature()), blocking=False, when=not (checks_done.get("signed") or {}).get("done"),
            detail="l'image signe la facture ; la déclaration d'origine préférentielle exige une signature manuscrite originale (sauf statut d'exportateur agréé) : faire signer les 3 exemplaires à la main ou obtenir le statut EA")
        add("signed", "Facture signée à la main (si pas de signature enregistrée)", manual=True, when=not signature(), blocking=False)
        add("vat", "Facture Pennylane sans TVA (exonération art. 262 I CGI, mention portée) vérifiée", manual=True, blocking=False, detail="le connecteur Shopify crée la facture au paiement : contrôler le taux 0 % export et la mention d'exonération")
    if z == "UE":
        add("vat_oss", "TVA : autoliquidation (société avec n° de TVA valide) ou TVA du pays de destination (particulier, guichet OSS)", manual=True, blocking=False,
            detail="société : facture Pennylane HT, mention « autoliquidation, art. 262 ter I CGI » et n° de TVA du client ; particulier : TVA française tant que les ventes à distance UE cumulées restent sous 10 000 € HT par an, puis TVA du pays de destination via l'OSS — ce seuil ne vaut que pour la TVA")
        add("excise", "Accises : référence obtenue avant le départ (CRA du DAES, ou référence de garantie) — notée", manual=True, blocking=False,
            detail="stock en droits acquittés (CRD) : société → DAES émis par un expéditeur certifié (agrément EC d'Alix) vers un destinataire certifié (le client ou le prestataire accises), apurement sous 5 jours ; particulier → représentant fiscal et garantie dans le pays avant l'envoi. Aucun seuil : l'accise est due dès la première bouteille")
    add("alix", "E-mail Alix envoyé (facture ×3 sur le colis A, stickers multi-pièces)", ok=bool(o.sent_alix_at), blocking=False)
    add("client", "E-mail client envoyé (DAP, délais, suivi)", ok=bool(o.sent_client_at), blocking=False)
    if z == "EXPORT":
        add("cleared", "Colis dédouané et livré (suivi Chronotrace)", manual=True, blocking=False, when=bool(o.sent_client_at))
        add("proof", "Justificatif d'exportation archivé (déclaration d'export Chronopost / MRN)", manual=True, blocking=False, when=bool(o.sent_client_at), detail="preuve de l'exonération de TVA : à demander à Chronopost (cellule export) si non reçu")
    blocking = [c_ for c_ in checks if not c_["ok"] and c_["blocking"] and not c_["manual"]]
    warnings = [c_ for c_ in checks if not c_["ok"] and not c_["blocking"]]
    return {"zone": z, "zone_label": zone_label(z), "customs": z == "EXPORT", "kind": k, "product": p, "product_status": ps, "product_label": plabel, "incoterm": incoterm_of(o),
            "rule": r, "country": c, "country_name": X.country_name(c), "checks": checks, "blocking": blocking, "warnings": warnings, "totals": tot, "shipments": ships,
            "invoice": inv, "st": st, "products": {k_: product_status(c, k, k_) for k_ in ("classic", "express")}, "delay": delay_text(c, k, p),
            "notes": ((r or {}).get("notes") or []) + (((r or {}).get("b2b" if k == "societe" else "b2c") or {}).get("notes") or []),
            "invoice_desc": (r or {}).get("invoice_desc") or [], "tax_id_label": X.TAX_ID_B2C.get(c), "ready": not blocking and bool(cs) and all(x.tracking for x in cs),
            "done": sum(1 for c_ in checks if c_["ok"]), "total": len(checks), "settings": settings(), "has_signature": bool(signature())}


# ================================================================== facture commerciale : données
def invoice_number(o) -> str:
    st = get_state(o)
    return (st.get("invoice_no") or "").strip() or f"FC-{o.name}"


def invoice_date(o) -> date:
    st = get_state(o)
    try:
        return date.fromisoformat(str(st.get("invoice_date"))[:10]) if st.get("invoice_date") else date.today()
    except Exception:
        return date.today()


def invoice_data(o, cs) -> dict:
    """Tout le contenu de la facture commerciale (modèle Chrono Viti) : un bloc par envoi (Suisse particuliers : un envoi par carton)."""
    s = settings(); k = dest_kind(o); p = product_of(o); imap = service.item_map(); st = get_state(o)
    lang_en = not (o.locale or "fr").lower().startswith("fr")
    out = {"no": invoice_number(o), "date": invoice_date(o), "kind": k, "product": X.PRODUCTS[p], "incoterm": incoterm_of(o), "incoterm_place": f"{o.city or ''} ({X.country_name(o.country, 'en')})".strip(),
           "sender": {"company": s["name"], "contact": s["contact"], "address1": config.OWINE_ADDRESS["address1"], "address2": config.OWINE_ADDRESS["address2"], "zip": config.OWINE_ADDRESS["zip"],
                      "city": config.OWINE_ADDRESS["city"], "country": "France", "phone": s["phone"], "email": s["email"], "eori": s.get("eori") or "", "vat": s["vat"], "rcs": s["rcs"],
                      "siret": s.get("siret") or "", "capital": s.get("capital") or "", "sign_place": s.get("sign_place") or "Dijon"},
           "recipient": {"name": o.customer or "", "company": o.company or o.billing_company or "", "address1": o.address1 or "", "address2": o.address2 or "", "zip": o.zip or "", "city": o.city or "",
                         "country": X.country_name(o.country, "en"), "country_code": (o.country or "").upper(), "phone": o.phone or "", "email": o.email or "", "eori": o.eori or "", "vat": o.vat_number or "",
                         "tax_id": o.tax_id or "", "tax_id_label": X.TAX_ID_B2C.get((o.country or "").upper(), "")},
           "final_use": X.FINAL_USE[k], "vat_mention": X.VAT_EXEMPTION_EXPORT, "origin_fr": X.ORIGIN_DECLARATION_FR, "origin_en": X.ORIGIN_DECLARATION_EN,
           "order": o.name, "order_date": o.created_at.date() if o.created_at else None, "shipments": [], "lang_en": lang_en, "notes": st.get("notes") or "",
           "content_desc": st.get("content_desc") or "Vin de Bourgogne AOP en bouteilles / Burgundy PDO wine in bottles"}
    ships = shipments(o, cs)
    tot_all = totals(o, cs)
    for i, sh in enumerate(ships):
        cartons = sh["cartons"]
        agg: Dict[str, dict] = {}
        for c in cartons:
            for l in service.carton_lines(c):
                it = imap.get(l["sku"])
                row = customs_row(it) if it else {"sku": l["sku"], "title": l.get("title"), "colour": None, "abv": None, "volume_cl": 75, "hs": X.HS_GENERIC, "origin": "FR", "vintage": None, "vigneron": None}
                a = agg.setdefault(l["sku"], {**row, "qty": 0, "unit_ht": 0.0})
                a["qty"] += int(l["qty"])
        # prix unitaire HT : ligne de commande correspondante (sinon coût ÷ 0 interdit → prix catalogue)
        # prix unitaire HT par SKU = moyenne pondérée des lignes de commande (un même vin peut figurer sur plusieurs lignes à des prix différents)
        agg_ht: Dict[str, List[float]] = {}
        for l in tot_all["lines"]:
            if l.get("sku"):
                a_ = agg_ht.setdefault(l["sku"], [0.0, 0]); a_[0] += l["total_ht"]; a_[1] += l["qty"]
        lv = {k: (v[0] / v[1] if v[1] else 0.0) for k, v in agg_ht.items()}
        for sku, a in agg.items():
            a["unit_ht"] = round(lv[sku], 2) if sku in lv else ht(float((imap.get(sku).price if imap.get(sku) and imap.get(sku).price else 0) or 0), o)
            a["total_ht"] = round(a["unit_ht"] * a["qty"], 2)
            a["desc"] = wine_description(a)
            col_ = {"rouge": "Vin rouge / Red wine", "blanc": "Vin blanc / White wine", "rosé": "Vin rosé / Rosé wine", "rose": "Vin rosé / Rosé wine"}.get(a.get("colour") or "", "Vin / Wine")
            a["desc_lines"] = [a.get("title") or a.get("sku"), f"AOP Bourgogne (PDO) - {col_}"]     # couleur, contenance, degré, millésime : colonnes dédiées
        lines = sorted(agg.values(), key=lambda x: x["title"] or "")
        goods = round(sum(x["total_ht"] for x in lines), 2)
        # port : réparti au prorata des bouteilles quand la commande est scindée en plusieurs envois
        nb = sum(x["qty"] for x in lines)
        share = (nb / tot_all["bottles"]) if tot_all["bottles"] else 1
        ship_ht = round(tot_all["shipping_ht"] * share, 2); ins = round(tot_all["insurance_ht"] * share, 2)
        if i == len(ships) - 1:                                    # le résidu d'arrondi du prorata va sur le dernier envoi
            ship_ht = round(tot_all["shipping_ht"] - sum(x["shipping_ht"] for x in out["shipments"]), 2)
            ins = round(tot_all["insurance_ht"] - sum(x["insurance_ht"] for x in out["shipments"]), 2)
        gross = round(sum(c.weight_kg for c in cartons), 1)
        pack = round(sum((config.PACKAGING.get(c.box_sku) or {}).get("kg", 0) for c in cartons), 2)
        if i == len(ships) - 1 and lines and abs(tot_all["bottles"] - sum(x["qty"] for s_ in out["shipments"] for x in s_["lines"]) - nb) < 1e-9:
            resid = round(tot_all["goods_ht"] - sum(s_["goods_ht"] for s_ in out["shipments"]) - goods, 2)   # résidu d'arrondi des prix unitaires : porté sur la dernière ligne
            if abs(resid) <= 0.05 * max(1, len(lines)):
                lines[-1]["total_ht"] = round(lines[-1]["total_ht"] + resid, 2); goods = round(goods + resid, 2)
        out["shipments"].append({"no": out["no"] + (f"-{sh['suffix']}" if sh["suffix"] else ""), "suffix": sh["suffix"], "parcels": [{"ref": c.ref, "tracking": c.tracking or "", "pos": f"{j + 1}/{len(cartons)}",
                                 "bottles": sum(int(l['qty']) for l in service.carton_lines(c)), "kg": c.weight_kg, "box": c.box_sku} for j, c in enumerate(cartons)],
                                 "lines": lines, "goods_ht": goods, "shipping_ht": ship_ht, "insurance_ht": ins, "total_ht": round(goods + ship_ht + ins, 2), "bottles": nb,
                                 "gross_kg": gross, "net_kg": round(max(gross - pack, nb * X.NET_KG_PER_BOTTLE), 1), "eur1": goods > X.EUR1_THRESHOLD})
    return out


def store_invoice(o, pdf: bytes, by: str = None, final: bool = True) -> dict:
    import base64
    st = get_state(o); prev = st.get("invoice") or {}
    meta = {"version": int(prev.get("version") or 0) + 1, "at": service.now_local().strftime("%d/%m/%Y %H:%M"), "by": by, "no": invoice_number(o), "final": final, "size": len(pdf)}
    service._setting_set(INV_KEY.format(name=o.name), {"b64": base64.b64encode(pdf).decode(), "name": f"Facture commerciale {invoice_number(o)}.pdf", **meta})
    st["invoice"] = meta; set_state(o, st)
    return meta


def invalidate_invoice(o) -> None:
    """Cartons ou numéros de colis modifiés : la facture archivée n'est plus définitive (à regénérer avant l'envoi)."""
    st = get_state(o)
    if (st.get("invoice") or {}).get("final"):
        st["invoice"]["final"] = False; st["invoice"]["stale"] = True; set_state(o, st)


def stored_invoice(o) -> Optional[Tuple[str, bytes]]:
    import base64
    d = service._setting_get(INV_KEY.format(name=o.name))
    return (d.get("name") or f"Facture commerciale {o.name}.pdf", base64.b64decode(d["b64"])) if d and d.get("b64") else None


# ================================================================== formulaire public « informations douanières »
def customs_token(o) -> str:
    if not o.customs_token:
        o.customs_token = secrets.token_urlsafe(18); service.save_order(o)
    return o.customs_token


def customs_url(o) -> str:
    return f"{service.admin_url()}/douane/{customs_token(o)}"


def by_token(token: str):
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import OwOrder
    if not token or len(token) < 10:
        return None
    with Session(engine) as s:
        r = s.exec(select(OwOrder).where(OwOrder.customs_token == token)).first()
        if r:
            s.expunge(r)
        return r


def mark_form_sent(o, by: str = None) -> None:
    st = get_state(o); st.setdefault("customs_form", {})["sent_at"] = service.now_local().strftime("%d/%m/%Y %H:%M"); st["customs_form"]["by"] = by
    set_state(o, st)
    service.add_task("customs_info", f"{o.name} : informations douanières demandées au client ({X.country_name(o.country)})", ref=o.name, key=f"customs_info:{o.name}",
                     details="Le client complète le formulaire public (téléphone, société, n° TVA / EORI, identifiant fiscal, accord DAP). La tâche se ferme à la réception.")


def apply_customs_form(o, data: dict, ip: str = "") -> None:
    g = lambda k: str(data.get(k) or "").strip()[:200]
    kind = "societe" if g("kind") == "societe" else "particulier"
    o.customer_type = kind
    if g("phone"):
        o.phone = g("phone")
    if g("name"):
        o.customer = g("name")
    if kind == "societe":
        o.company = g("company") or o.company
        o.vat_number = g("vat") or o.vat_number
        o.eori = g("eori") or o.eori
        if g("excise_no"):
            st0 = get_state(o); st0["excise_no"] = g("excise_no"); o.export_json = json.dumps(st0, ensure_ascii=False)
    else:
        o.tax_id = g("tax_id") or o.tax_id
    if g("address2") and g("address2") not in (o.address2 or ""):
        o.address2 = ((o.address2 or "") + " " + g("address2")).strip()
    st = get_state(o)
    st.setdefault("customs_form", {}).update({"submitted_at": service.now_local().strftime("%d/%m/%Y %H:%M"), "ip": ip, "dap_accepted": g("dap") == "1", "lang": g("lang") or "fr"})
    st.setdefault("checks", {})["recipient_ok"] = {"done": True, "at": service.now_local().isoformat(timespec="minutes"), "by": "client (formulaire)", "note": "accord DAP : " + ("oui" if g("dap") == "1" else "non")}
    set_state(o, st)
    service.close_tasks_by_key(f"customs_info:{o.name}")


def refuse_order(o, by: str = None, reason: str = "") -> dict:
    """Commande hors périmètre refusée : annulée dans Vaelan, tâche « rembourser dans Shopify », e-mail FR/EN au client (texte renvoyé)."""
    st = get_state(o); st["refused"] = {"at": service.now_local().strftime("%d/%m/%Y %H:%M"), "by": by, "reason": reason}
    o.status = "annulee"; set_state(o, st)
    for pfx in ("customs", "customs_data", "customs_info"):
        service.close_tasks_by_key(f"{pfx}:{o.name}")
    service.add_task("other", f"{o.name} : rembourser la commande dans Shopify (refusée : {reason or 'hors périmètre'})", ref=o.name, key=f"refund:{o.name}",
                     details="Shopify › Commandes › Rembourser (montant total, restockage). La commande est annulée dans Vaelan ; rien n'a été décompté du stock.")
    lang = "en" if (o.locale or "fr").lower()[:2] != "fr" else "fr"
    first = (o.customer or "").split(" ")[0]; cname = X.country_name(o.country, lang)
    if lang == "en":
        subject = f"Your oWine order {o.name}: we cannot ship it yet"
        body = (f"Hello {first},\n\nThank you for your order {o.name}. Unfortunately we cannot ship it to {cname} as placed" + (f" ({reason})" if reason else "") +
                ".\n\nWe currently deliver to private customers in Switzerland (up to 6 bottles, one case) and to companies in the European Union with a valid EU VAT number; other destinations are coming soon.\n\n"
                "We are refunding your payment in full today. If you would like to adjust your order to fit these options, reply to this e-mail and we will help you.\n\nWith our apologies,\nJean-Sébastien CHEUNG-AH-SEUNG · oWine")
    else:
        subject = f"Votre commande oWine {o.name} : nous ne pouvons pas encore l'expédier"
        body = (f"Bonjour {first},\n\nMerci pour votre commande {o.name}. Nous ne pouvons malheureusement pas l'expédier en {cname} telle qu'elle a été passée" + (f" ({reason})" if reason else "") +
                ".\n\nNous livrons aujourd'hui les particuliers en Suisse (6 bouteilles au plus, un carton) et les sociétés de l'Union européenne disposant d'un n° de TVA intracommunautaire valide ; les autres destinations arrivent prochainement.\n\n"
                "Nous vous remboursons intégralement dès aujourd'hui. Si vous souhaitez adapter votre commande à ces options, répondez à cet e-mail et nous vous aiderons.\n\nAvec nos excuses,\nJean-Sébastien CHEUNG-AH-SEUNG · oWine")
    return {"subject": subject, "body": body, "lang": lang}


# ================================================================== tâches export (synchro) et Shopify : codes SH, origine, poids des sélections
def export_tasks(log=None) -> int:
    """Commandes internationales à traiter → tâche « formalités » ; vins sans données douanières utilisés par ces commandes → tâche « données douanières »."""
    log = log or (lambda m: None); n = 0
    for o in service.orders():
        if zone(o.country) == "FR":
            continue
        if o.status in ("cloturee", "annulee"):
            for pfx in ("customs", "customs_data", "customs_info"):
                service.close_tasks_by_key(f"{pfx}:{o.name}")
            continue
        if o.status in ("a_traiter", "cartons", "etiquettes"):
            z = zone(o.country); zopen_, _ = zone_open(o.country, dest_kind(o))
            if service.add_task("customs", ("HORS PÉRIMÈTRE — " if not zopen_ else "") + f"{o.name} : commande {'export (douane)' if z == 'EXPORT' else 'intracommunautaire'} vers {X.country_name(o.country)} — " + ("accepter par dérogation ou refuser et rembourser" if not zopen_ else "suivre les étapes export sur la commande"),
                                ref=o.name, key=f"customs:{o.name}", details="Zone " + zone_label(z) + ". Contrôles automatiques et étapes à cocher sur la page de la commande."):
                n += 1
            if z == "EXPORT":
                wi = _wine_issues(o, service.cartons(o.id))
                if wi and service.add_task("customs_data", f"{o.name} : données douanières manquantes — " + " ; ".join(wi)[:180], ref=o.name, key=f"customs_data:{o.name}",
                                            details="Page Douane → Données douanières des vins : couleur, degré (% vol), millésime, code SH."):
                    n += 1
                elif not wi:
                    service.close_tasks_by_key(f"customs_data:{o.name}")
        else:
            service.close_tasks_by_key(f"customs:{o.name}"); service.close_tasks_by_key(f"customs_data:{o.name}")
    log(f"export : {n} nouvelle(s) tâche(s)")
    return n


def shopify_customs_plan() -> dict:
    """Ce que Vaelan écrirait dans Shopify : code SH + origine FR sur chaque variante vin, poids réel des sélections (1,5 kg × bouteilles)."""
    c = service._shopify()
    if not c:
        raise RuntimeError("Shopify non configuré")
    q = """query($first:Int!,$after:String){ productVariants(first:$first, after:$after) { pageInfo{hasNextPage endCursor} edges{ node{
      id sku product{ id title metafields(first:5, namespace:"custom"){ edges{ node{ key value } } } } inventoryItem{ id harmonizedSystemCode countryCodeOfOrigin measurement{ weight{ value unit } } } } } } }"""
    imap = service.item_map(); plan = {"hs": [], "weight": [], "skipped": []}
    for v in c.pages(q, "productVariants", first=100):
        sku = (v.get("sku") or "").strip(); ii = v.get("inventoryItem") or {}
        if not sku or sku in config.PACKAGING:
            continue
        it = imap.get(sku)
        if sku.upper().startswith("SEL-"):
            mf = {e["node"]["key"]: e["node"]["value"] for e in ((v.get("product") or {}).get("metafields") or {}).get("edges", [])}
            try:
                qty = sum(int(x) for x in json.loads(mf.get("custom_bundle_quantities") or "[]"))
            except Exception:
                qty = 0
            w = ((ii.get("measurement") or {}).get("weight") or {})
            cur = float(w.get("value") or 0) * (0.001 if w.get("unit") == "GRAMS" else 1)
            if qty and abs(cur - qty * config.BOTTLE_KG) > 0.01:
                plan["weight"].append({"sku": sku, "title": (v.get("product") or {}).get("title"), "variant_id": v["id"], "product_id": (v.get("product") or {}).get("id"), "bottles": qty, "from": cur, "to": round(qty * config.BOTTLE_KG, 2)})
            continue
        if not it or it.kind != "wine":
            plan["skipped"].append(sku); continue
        row = customs_row(it)
        want_hs, want_o = row["hs"], row["origin"]
        if (ii.get("harmonizedSystemCode") or "") != want_hs or (ii.get("countryCodeOfOrigin") or "") != want_o:
            plan["hs"].append({"sku": sku, "title": it.title, "inventory_item_id": ii.get("id"), "hs": want_hs, "origin": want_o, "from": (ii.get("harmonizedSystemCode") or "—", ii.get("countryCodeOfOrigin") or "—")})
    return plan


def shopify_customs_apply(log=None) -> str:
    log = log or (lambda m: None)
    c = service._shopify(); plan = shopify_customs_plan(); n1 = n2 = 0
    for x in plan["hs"]:
        r = c.gql("""mutation($id:ID!,$in:InventoryItemInput!){ inventoryItemUpdate(id:$id, input:$in){ inventoryItem{ id } userErrors{ field message } } }""",
                  {"id": x["inventory_item_id"], "in": {"harmonizedSystemCode": x["hs"], "countryCodeOfOrigin": x["origin"]}})
        errs = (r.get("inventoryItemUpdate") or {}).get("userErrors") or []
        if errs:
            log(f"{x['sku']} : {errs}")
        else:
            n1 += 1
    for x in plan["weight"]:
        r = c.gql("""mutation($pid:ID!,$vars:[ProductVariantsBulkInput!]!){ productVariantsBulkUpdate(productId:$pid, variants:$vars){ userErrors{ field message } } }""",
                  {"pid": x["product_id"], "vars": [{"id": x["variant_id"], "inventoryItem": {"measurement": {"weight": {"value": x["to"], "unit": "KILOGRAMS"}}}}]})
        errs = (r.get("productVariantsBulkUpdate") or {}).get("userErrors") or []
        if errs:
            log(f"{x['sku']} : {errs}")
        else:
            n2 += 1
    msg = f"Shopify : {n1} variante(s) avec code SH + origine, {n2} sélection(s) repesée(s)"
    log(msg)
    return msg


# ================================================================== ZONES OUVERTES (périmètre de vente), coût logistique, grille de port Shopify
ZONES_KEY = "owine:export:zones"
RATE_KEY = "owine:export:rates"          # réglages de la grille : marge, carburant, frais Alix


def zones() -> List[dict]:
    """Zones de vente avec les réglages enregistrés (ouverture particuliers / sociétés, limites)."""
    saved = service._setting_get(ZONES_KEY) or {}
    out = []
    for z in X.ZONES_DEFAULT:
        d = dict(z); d.update({k: v for k, v in (saved.get(z["key"]) or {}).items() if k in ("particulier", "societe", "max_bottles", "multiple", "min_order", "vat_required", "product")})
        if z["key"] == "FR":
            d.update(particulier=True, societe=True, product="chrono13")        # la France reste toujours ouverte, circuit Chrono 13
        out.append(d)
    return out


def save_zones(form: dict) -> None:
    saved = {}
    for z in X.ZONES_DEFAULT:
        k = z["key"]
        def num(name):
            v = (form.get(f"{name}_{k}") or "").replace(",", ".").strip()
            try:
                return float(v) if v else None
            except ValueError:
                return None
        mb = num("max"); mult = num("multiple"); mo = num("min")
        if k == "FR":
            saved[k] = {"particulier": True, "societe": True}; continue         # la France reste toujours ouverte
        saved[k] = {"particulier": form.get(f"particulier_{k}") == "1", "societe": form.get(f"societe_{k}") == "1", "max_bottles": int(mb) if mb else None,
                    "multiple": int(mult) if mult else None, "min_order": mo, "vat_required": form.get(f"vat_{k}") == "1", "product": (form.get(f"product_{k}") or z["product"])}
    service._setting_set(ZONES_KEY, saved)


def zone_of(country: str) -> Optional[dict]:
    c = (country or "").upper()
    for z in zones():
        if c in z["countries"]:
            return z
    return None


def zone_open(country: str, kind: str) -> Tuple[bool, str]:
    z = zone_of(country)
    if not z:
        return False, f"{X.country_name(country)} : hors des zones de vente ouvertes"
    if not z.get("particulier" if kind == "particulier" else "societe"):
        return False, f"{z['label']} : fermée aux {'particuliers' if kind == 'particulier' else 'sociétés'} pour l'instant"
    return True, z["label"]


def rate_settings() -> dict:
    d = {"margin_pct": 18.0, "fuel_pct": X.SUPPLEMENTS["fuel_pct_default"], "alix_prep_min": X.ALIX["prep_min"], "alix_prep_per_bottle": X.ALIX["prep_per_bottle"], "alix_dae": X.ALIX["dae_out"],
         "export_declaration": X.SUPPLEMENTS["export_declaration_ht"], "round_to": 1.0}
    saved = service._setting_get(RATE_KEY) or {}
    for k, v in saved.items():
        try:
            d[k] = float(v)
        except (TypeError, ValueError):
            pass
    return d


def save_rate_settings(form: dict) -> None:
    saved = service._setting_get(RATE_KEY) or {}
    for k in ("margin_pct", "fuel_pct", "alix_prep_min", "alix_prep_per_bottle", "alix_dae", "export_declaration", "round_to"):
        v = (form.get(k) or "").replace(",", ".").strip()
        if v:
            try:
                saved[k] = float(v)
            except ValueError:
                pass
    service._setting_set(RATE_KEY, saved)


def _bracket_price(product: str, zone_no, weight: float) -> Optional[float]:
    t = X.TARIFFS.get(product)
    if not t or zone_no not in t["zones"]:
        return None
    prices, per_kg = t["zones"][zone_no]
    for cap, p in zip(t["brackets"], prices):
        if weight <= cap + 1e-9:
            return p
    return prices[-1] + per_kg * (weight - t["brackets"][-1])


def chronopost_cost(country: str, product: str, bottles: int, parcels: int = None, kind: str = "particulier") -> dict:
    """Coût contrat d'une expédition (multi-colis) : tarif de la tranche de poids + suppléments (groupage, douane zone 4, zone 2 hors UE, éco, dédouanement export) + carburant.
    Suisse particuliers (6 btl / envoi) : autant d'expéditions que de cartons de 6."""
    rs = rate_settings(); c = (country or "").upper(); z = zone(c)
    parcels = parcels or max(1, math.ceil(bottles / 6))
    if product == "chrono13":
        zone_no = "FR"
    else:
        zo = X.zoning(product, c)
        zone_no = zo[1] if zo else None
    if zone_no is None:
        return {"error": f"{X.country_name(c)} : pas de zone {X.PRODUCTS.get(product, {}).get('label', product)}", "total": None}
    r = rule(c); lim = ((r or {}).get("b2b" if kind == "societe" else "b2c") or {})
    per_shipment = lim.get("max_bottles") if (product == "classic" and lim.get("max_bottles") and bottles > lim["max_bottles"]) else None
    shipments_ = []
    if per_shipment:
        left = bottles
        while left > 0:
            n = min(per_shipment, left); shipments_.append((n, 1)); left -= n
    else:
        shipments_.append((bottles, parcels))
    lines = []; total = 0.0
    for nb, np_ in shipments_:
        w = round(nb * config.BOTTLE_KG, 1)
        base = _bracket_price(product, zone_no, w) or 0.0
        sup = 0.0; det = []
        if product == "classic" and np_ > 1:
            g = X.SUPPLEMENTS["groupage_classic"].get(zone_no, 6.0) * (np_ - 1); sup += g; det.append(f"groupage {g:.2f}")
        if product == "classic" and zone_no == 4:
            sup += X.SUPPLEMENTS["customs_classic_zone4"]; det.append(f"douane zone 4 {X.SUPPLEMENTS['customs_classic_zone4']:.2f}")
        if c == "GB":
            sup += X.SUPPLEMENTS["zone2_non_eu"]; det.append(f"zone 2 hors UE {X.SUPPLEMENTS['zone2_non_eu']:.2f}")
        if z == "EXPORT":
            sup += rs["export_declaration"]; det.append(f"déclaration export {rs['export_declaration']:.2f}")
        eco = X.SUPPLEMENTS["eco"] * np_; sup += eco; det.append(f"éco {eco:.2f}")
        fuel = round((base + sup) * rs["fuel_pct"] / 100, 2)
        sub = round(base + sup + fuel, 2); total += sub
        lines.append({"bottles": nb, "parcels": np_, "weight": w, "base": base, "supplements": round(sup, 2), "supplements_detail": ", ".join(det), "fuel": fuel, "total": sub})
    return {"product": product, "zone_no": zone_no, "shipments": lines, "total": round(total, 2), "fuel_pct": rs["fuel_pct"]}


def alix_cost(bottles: int, dae: bool = False) -> dict:
    rs = rate_settings()
    prep = max(rs["alix_prep_min"], rs["alix_prep_per_bottle"] * bottles)
    d = rs["alix_dae"] if dae else 0.0
    return {"prep": round(prep, 2), "dae": d, "total": round(prep + d, 2)}


def logistics_cost(o, cs) -> dict:
    """Coût logistique estimé d'une commande (Chronopost + Alix) face au port facturé au client."""
    bottles = sum(int(l["qty"]) for c in cs for l in service.carton_lines(c)) if cs else sum(int(l["qty"]) for l in service.order_lines(o))
    k = dest_kind(o); p = product_of(o) if zone(o.country) != "FR" else "chrono13"
    ch = chronopost_cost(o.country, p, bottles, len(cs) if cs else None, kind=k)
    al = alix_cost(bottles, dae=(zone(o.country) == "UE" and k == "societe"))
    charged = ht(o.shipping_paid or 0, o)
    total = (ch.get("total") or 0) + al["total"]
    return {"bottles": bottles, "chronopost": ch, "alix": al, "total": round(total, 2), "charged_ht": charged, "margin": round(charged - total, 2)}


def rate_grid() -> List[dict]:
    """Grille de port proposée par zone ouverte (hors France), par nombre de cartons de 6 : coût contrat + Alix (+ DAE pour les sociétés UE) + marge, arrondi."""
    rs = rate_settings(); out = []
    for z in zones():
        if z["key"] == "FR" or not (z.get("particulier") or z.get("societe")):
            continue
        maxb = int(z.get("max_bottles") or 18)
        steps = list(range(6, min(maxb, 36) + 1, 6)) or [6]
        kind = "societe" if z.get("societe") else "particulier"     # coût le plus élevé (DAE pour une société UE) quand la zone est ouverte aux deux
        rows = []
        for n in steps:
            costs = [chronopost_cost(c, z["product"], n, kind=kind) for c in z["countries"]]
            costs = [c for c in costs if c.get("total") is not None]
            if not costs:
                continue
            worst = max(costs, key=lambda c: c["total"])
            al = alix_cost(n, dae=(z["key"].startswith("UE") and kind == "societe"))
            cost = worst["total"] + al["total"]
            price = cost * (1 + rs["margin_pct"] / 100)
            step = rs["round_to"] or 1.0
            price = math.ceil(price / step) * step
            rows.append({"bottles": n, "weight": n * config.BOTTLE_KG, "chronopost": worst["total"], "alix": al["total"], "cost": round(cost, 2), "price": round(price, 2),
                         "window": (round(n * config.BOTTLE_KG - 0.1, 2), round(n * config.BOTTLE_KG + 0.4, 2))})     # + 0,4 kg : tolère le poids du colis par défaut de Shopify
        out.append({"zone": z, "kind": kind, "rows": rows})
    return out


def shopify_shipping_plan() -> dict:
    """Ce que Vaelan écrirait dans le profil de livraison Shopify : zones internationales actuelles supprimées, une zone par zone ouverte
    avec un tarif par nombre de cartons de 6 (fenêtre de poids ± 0,1 kg : un panier hors multiple de 6 n'a aucun tarif et ne peut pas être payé)."""
    c = service._shopify()
    if not c:
        raise RuntimeError("Shopify non configuré")
    d = c.gql("""{ deliveryProfiles(first: 5) { edges { node { id name default
        profileLocationGroups {
          locationGroup { id }
          locationGroupZones(first: 30) { edges { node {
            zone { id name countries { code { countryCode restOfWorld } } }
            methodDefinitions(first: 30) { edges { node { id name active
              rateProvider { ... on DeliveryRateDefinition { price { amount } } }
              methodConditions { field operator conditionCriteria { ... on Weight { value unit } } } } } } } } } } } } } }""")
    prof = next((e["node"] for e in d["deliveryProfiles"]["edges"] if e["node"]["default"]), None)
    if not prof:
        raise RuntimeError("profil de livraison par défaut introuvable")
    lg = prof["profileLocationGroups"][0]
    current = []
    for e in lg["locationGroupZones"]["edges"]:
        zz = e["node"]["zone"]
        codes = [x["code"]["countryCode"] or ("ROW" if x["code"]["restOfWorld"] else "?") for x in zz["countries"]]
        current.append({"id": zz["id"], "name": zz["name"], "countries": codes, "keep": bool({"FR", "MC"} & set(codes)),        # toute zone contenant la France est conservée
                        "methods": [{"name": m["node"]["name"], "price": ((m["node"]["rateProvider"] or {}).get("price") or {}).get("amount"),
                                     "conditions": [(x["field"], x["operator"], (x["conditionCriteria"] or {}).get("value")) for x in m["node"]["methodConditions"]]} for m in e["node"]["methodDefinitions"]["edges"]]})
    grid = rate_grid()
    create = []
    for g in grid:
        z = g["zone"]; label = X.PRODUCTS[z["product"]]["label"]
        methods = []
        for r in g["rows"]:
            methods.append({"name": f"Chronopost {label} — {r['bottles']} bouteille{'s' if r['bottles'] > 1 else ''} ({r['bottles'] // 6} carton{'s' if r['bottles'] > 6 else ''})", "price": r["price"], "window": r["window"]})
        create.append({"name": f"{z['label']}", "countries": z["countries"], "methods": methods, "kind": g["kind"]})
    return {"profile_id": prof["id"], "location_group_id": lg["locationGroup"]["id"], "current": current, "delete": [zc for zc in current if not zc["keep"]], "create": create}


def shopify_shipping_apply(log=None) -> str:
    log = log or (lambda m: None)
    plan = shopify_shipping_plan(); c = service._shopify()
    zones_create = []
    for zc in plan["create"]:
        zones_create.append({"name": zc["name"], "countries": [{"code": cc, "includeAllProvinces": True} for cc in zc["countries"]],
                             "methodDefinitionsToCreate": [{"name": m["name"], "active": True, "rateDefinition": {"price": {"amount": m["price"], "currencyCode": "EUR"}},
                                                            "weightConditionsToCreate": [{"criteria": {"value": m["window"][0], "unit": "KILOGRAMS"}, "operator": "GREATER_THAN_OR_EQUAL_TO"},
                                                                                         {"criteria": {"value": m["window"][1], "unit": "KILOGRAMS"}, "operator": "LESS_THAN_OR_EQUAL_TO"}]} for m in zc["methods"]]})
    inp = {"locationGroupsToUpdate": [{"id": plan["location_group_id"], "zonesToDelete": [zc["id"] for zc in plan["delete"]], "zonesToCreate": zones_create}]}
    r = c.gql("""mutation($id:ID!,$p:DeliveryProfileInput!){ deliveryProfileUpdate(id:$id, profile:$p){ profile{ id } userErrors{ field message } } }""", {"id": plan["profile_id"], "p": inp})
    errs = (r.get("deliveryProfileUpdate") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError(f"Shopify : {errs}")
    msg = f"profil de livraison mis à jour : {len(plan['delete'])} zone(s) supprimée(s), {len(zones_create)} zone(s) créée(s)"
    log(msg); return msg


# ================================================================== degrés d'alcool : estimation, confirmation vin par vin (historique), demande à Alix
LOG_KEY = "owine:export:customs_log"
APPELLATION_ABV = [("grand cru", 13.5), ("1er cru", 13.0), ("premier cru", 13.0), ("bourgogne", 12.5)]


def estimate_abv(it) -> Tuple[float, str]:
    """Estimation prudente pour une commande en attente de confirmation : blancs de Bourgogne récents ~13 %, rouges ~13 %, grands crus 13,5 %, régionaux 12,5 %."""
    t = (it.title or "").lower()
    for k, v in APPELLATION_ABV:
        if k in t and not (k == "bourgogne" and any(x in t for x in ("cru", "chassagne", "puligny", "meursault", "gevrey", "morey", "chambolle", "vosne", "nuits", "pommard", "volnay", "beaune", "santenay", "saint-aubin", "aloxe", "ladoix", "corton", "montrachet", "clos"))):
            return v, "estimation (appellation)"
    return 13.0, "estimation (défaut Bourgogne)"


ABV_PUBLIC = os.path.join(os.path.dirname(__file__), "data", "abv_public.json")   # degrés relevés sur les fiches publiques des domaines (recherche du 15/09/2026)


def public_abv() -> dict:
    try:
        return json.load(open(ABV_PUBLIC))
    except Exception:
        return {}


def fill_abv_estimates(results: Optional[dict] = None) -> int:
    """Pré-remplit les degrés non confirmés : résultats de la recherche publique (fiches techniques) quand ils existent, sinon estimation ; statut « estimé »."""
    from sqlmodel import Session
    from app.core.db import engine
    from app.models import OwItem
    results = results if results is not None else public_abv()
    n = 0
    with Session(engine) as s:
        for it in s.exec(__import__("sqlmodel").select(OwItem).where(OwItem.kind == "wine")).all():
            if it.abv and it.abv_status == "confirme":
                continue
            r = (results or {}).get(it.sku) or {}
            if r.get("abv"):
                note = f" — {r['note']}" if r.get("note") else ""
                it.abv = float(r["abv"]); it.abv_source = f"{'fiche publique' if r.get('match') == 'exact' else 'millésime voisin'} : {r.get('source', '')}{note}"[:300]; it.abv_status = "estime"
            elif not it.abv:
                it.abv, it.abv_source = estimate_abv(it); it.abv_status = "estime"
            elif not it.abv_status:
                it.abv_status = "estime"; it.abv_source = it.abv_source or "saisie"
            it.updated_at = datetime.utcnow(); s.add(it); n += 1
        s.commit()
    return n


def confirm_wine(sku: str, by: str, abv: Optional[float] = None, colour: Optional[str] = None, hs: Optional[str] = None, source: str = None) -> bool:
    """Confirme les données douanières d'un vin (degré, couleur, code SH) ; trace qui et quand, et garde l'historique."""
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import OwItem
    with Session(engine) as s:
        it = s.exec(select(OwItem).where(OwItem.sku == sku)).first()
        if not it:
            return False
        if abv:
            it.abv = float(abv)
        if colour:
            it.couleur = colour
        if hs:
            it.hs_code = hs
        if not it.couleur:
            it.couleur = colour_from_sku(it.sku) or colour_from_title(it.title)
        if not it.hs_code or it.hs_code == X.HS_GENERIC:
            it.hs_code = X.CN_BY_COLOUR.get((it.couleur or "").lower(), X.HS_GENERIC)
        if not it.abv or not it.couleur:
            return False                                           # pas de confirmation sans degré ni couleur
        it.abv_status = "confirme"; it.abv_source = source or f"confirmé par {by}"
        it.customs_confirmed_at = datetime.utcnow(); it.customs_confirmed_by = by; it.updated_at = datetime.utcnow()
        s.add(it); s.commit()
        entry = {"at": service.now_local().strftime("%d/%m/%Y %H:%M"), "by": by, "sku": sku, "title": it.title, "abv": it.abv, "colour": it.couleur, "hs": it.hs_code, "source": it.abv_source}
    log = service._setting_get(LOG_KEY) or []
    log.insert(0, entry); service._setting_set(LOG_KEY, log[:500])
    return True


def unconfirm_wine(sku: str, by: str) -> None:
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import OwItem
    with Session(engine) as s:
        it = s.exec(select(OwItem).where(OwItem.sku == sku)).first()
        if it:
            it.abv_status = "estime"; it.customs_confirmed_at = None; it.customs_confirmed_by = None; it.updated_at = datetime.utcnow(); s.add(it); s.commit()
    log = service._setting_get(LOG_KEY) or []
    log.insert(0, {"at": service.now_local().strftime("%d/%m/%Y %H:%M"), "by": by, "sku": sku, "title": "", "abv": None, "colour": None, "hs": None, "source": "confirmation retirée"}); service._setting_set(LOG_KEY, log[:500])


def customs_log() -> List[dict]:
    return service._setting_get(LOG_KEY) or []


def unconfirmed_wines(o, cs) -> List[dict]:
    """Vins de la commande dont le degré n'est pas confirmé (à demander à Alix, qui lit l'étiquette en préparant)."""
    imap = service.item_map(); seen = set(); out = []
    for l in ([l for c in cs for l in service.carton_lines(c)] if cs else service.order_lines(o)):
        sku = l.get("sku"); it = imap.get(sku)
        if not it or sku in seen or it.kind != "wine":
            continue
        seen.add(sku)
        if it.abv_status != "confirme":
            out.append({"sku": sku, "title": it.title, "abv": it.abv, "source": it.abv_source})
    return out


def abv_request_email(o, cs) -> dict:
    """E-mail à Alix : confirmer les degrés d'alcool (étiquette) des vins de la commande pas encore confirmés, avant la facture commerciale."""
    ws = unconfirmed_wines(o, cs)
    lines = "\n".join(f"  • {w['title']} — réf. {w['sku']}" + (f" (nous avons {w['abv']:g} % vol, à confirmer)" if w.get("abv") else "") for w in ws)
    body = (f"Bonjour,\n\nLa commande OWINE #{o.name} part à l'international ({X.country_name(o.country)}) : la facture douanière doit indiquer le degré d'alcool exact de chaque vin, tel qu'il figure sur l'étiquette.\n\n"
            f"Pourriez-vous nous confirmer, en préparant la commande, le degré (% vol) lu sur l'étiquette des vins suivants ?\n\n{lines}\n\n"
            "Une simple réponse à cet e-mail suffit (vin : degré). Les étiquettes Chronopost et la facture à joindre au colis suivront dès réception.\n\nMerci d'avance,")
    return {"to": [config.ALIX_EMAIL], "cc": list(config.ALIX_CC), "subject": f"Commande OWINE #{o.name} — degrés d'alcool à confirmer ({len(ws)} vin{'s' if len(ws) > 1 else ''})", "body": body, "wines": ws}


# ================================================================== VIES : n° de TVA intracommunautaire (sociétés UE)
VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
_VIES_DOWN: Dict[str, datetime] = {}                                 # panne VIES mémorisée 5 min : pas de rafale d'appels à chaque affichage


def vies_check(vat: str, force: bool = False) -> dict:
    """{valid, name, address, checked_at, error} ; résultat gardé 30 jours (Setting). Hors UE (CHE-…, GB…) : non applicable."""
    import httpx
    v = re.sub(r"[^A-Z0-9]", "", (vat or "").upper())
    if len(v) < 4:
        return {"valid": False, "error": "numéro vide ou trop court", "vat": v}
    cc, num = v[:2], v[2:]
    if cc == "GR":
        cc = "EL"                                                   # VIES attend le préfixe fiscal grec
    if cc not in (X.EU | {"EL", "XI"}) or cc == "GR":
        return {"valid": None, "error": f"{cc} : n° hors VIES (pays non membre de l'UE)", "vat": v, "na": True}
    if not re.fullmatch(r"[A-Z0-9]{2,13}", num):
        return {"valid": False, "error": "format invalide", "vat": v}
    key = f"owine:vies:{v}"
    cached = service._setting_get(key)
    if cached and not force:
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(cached["checked_at"])).days
            if age < (30 if cached.get("valid") else 1):
                return cached
        except Exception:
            pass
    down = _VIES_DOWN.get("until")
    if down and datetime.utcnow() < down and not force:
        return {"valid": None, "error": "VIES indisponible (réessai dans quelques minutes)", "vat": v}
    try:
        r = httpx.post(VIES_URL, json={"countryCode": cc, "vatNumber": num}, timeout=15, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            return {"valid": None, "error": f"VIES indisponible (HTTP {r.status_code})", "vat": v}
        d = r.json()
        errs = [e.get("error") for e in (d.get("errorWrappers") or []) if isinstance(e, dict)]
        err = errs[0] if errs else (d.get("userError") if d.get("userError") not in (None, "VALID", "INVALID") else None)
        clean = lambda x: None if (x or "").strip() in ("", "---") else (x or "").strip()
        out = {"vat": v, "valid": bool(d.get("valid")) if not err else None, "name": clean(d.get("name")), "address": (clean(d.get("address")) or "").replace("\n", ", ") or None,
               "checked_at": datetime.utcnow().isoformat(timespec="seconds"), "error": None}
        if err == "INVALID_INPUT":
            out["valid"] = False; out["error"] = "format refusé par VIES"
        elif err:
            out["valid"] = None; out["error"] = f"VIES : {err} (réessayer)"          # panne ou saturation : rien n'est mis en cache
            _VIES_DOWN["until"] = datetime.utcnow() + timedelta(minutes=5)
        else:
            service._setting_set(key, out)
        return out
    except Exception as e:
        _VIES_DOWN["until"] = datetime.utcnow() + timedelta(minutes=5)
        return {"valid": None, "error": f"VIES injoignable : {type(e).__name__}", "vat": v}


# ================================================================== mise en place : liste des actions JS (tâches), textes du site, extrait de thème
# ================================================================== jalons d'ouverture et garde-fou du périmètre Shopify (v0.1.198)
JALONS = [
    {"n": 1, "label": "Jalon 1", "tasks": True, "title": "Fermer la vente hors France",
     "goal": "Priorité immédiate, environ 20 minutes, sans dépendance. Les zones de livraison Shopify « UE (Union Européenne) » (26 pays, 22 €) et « International » (14 pays dont la Suisse, 29 €), "
             "antérieures aux travaux, laissent commander 40 pays sans accise, sans douane et à un port inférieur au coût. Aucune commande étrangère reçue à ce jour (49 commandes relues le 15/09/2026)."},
    {"n": 2, "label": "Jalon 2", "tasks": True, "title": "Ouvrir la Suisse aux particuliers (une commande = un carton de 6)",
     "goal": "Exportation simple : pas d'accise suisse sur le vin, aucun régime accises UE, Chrono Classic, facture commerciale ×3, livraison DAP (le client règle TVA suisse 8,1 %, droit de douane et frais "
             "du partenaire Chronopost avant la livraison). Ordre : lancer A tout de suite, faire B et C pendant l'attente des réponses, D en dernier. Rien n'est visible des clients avant J2·D2."},
    {"n": 9, "label": "Ensuite", "tasks": False, "title": "Jalons suivants, à préparer à la fin du jalon 2",
     "goal": "Ils dépendent de réponses attendues : Chronopost (limite par colis ou par envoi), Alix (agrément d'expéditeur certifié), prestataires accises (Eurotax, ASD Group)."},
]
ACTIVE_JALONS = {j["n"] for j in JALONS if j["tasks"]}
PERIMETER_TASK = "export:perimeter_leak"
_DZ_CACHE: dict = {"at": 0.0, "data": None, "failed_at": 0.0}
_WEIGHT_KG = {"KILOGRAMS": 1.0, "GRAMS": 0.001, "POUNDS": 0.45359237, "OUNCES": 0.0283495}


def shopify_delivery_zones(force: bool = False) -> Optional[List[dict]]:
    """Zones de livraison de tous les profils Shopify : pays, tarifs (actif, prix, fenêtre de poids en kg). Cache 10 min (échec : 2 min) ; None si Shopify est injoignable."""
    import time as _time
    now = _time.time()
    if not force:
        if _DZ_CACHE["data"] is not None and now - _DZ_CACHE["at"] < 600:
            return _DZ_CACHE["data"]
        if now - _DZ_CACHE["failed_at"] < 120:
            return None
    c = service._shopify()
    try:
        if not c:
            raise RuntimeError("Shopify non configuré")
        d = c.gql("""{ deliveryProfiles(first: 10) { edges { node { name default
            profileLocationGroups { locationGroupZones(first: 50) { edges { node {
              zone { name countries { code { countryCode restOfWorld } } }
              methodDefinitions(first: 50) { edges { node { name active
                rateProvider { ... on DeliveryRateDefinition { price { amount } } }
                methodConditions { field operator conditionCriteria { ... on Weight { value unit } } } } } } } } } } } } } }""")
    except Exception:
        _DZ_CACHE["failed_at"] = now
        return None
    out = []
    for pe in d["deliveryProfiles"]["edges"]:
        prof = pe["node"]
        for lg in prof["profileLocationGroups"]:
            for ze in lg["locationGroupZones"]["edges"]:
                zn = ze["node"]; methods = []
                for me in zn["methodDefinitions"]["edges"]:
                    m = me["node"]; lo = hi = None
                    for cond in m["methodConditions"]:
                        crit = cond.get("conditionCriteria") or {}
                        if cond.get("field") != "TOTAL_WEIGHT" or crit.get("value") is None:
                            continue
                        kg = float(crit["value"]) * _WEIGHT_KG.get(crit.get("unit"), 1.0)
                        if cond["operator"] == "GREATER_THAN_OR_EQUAL_TO":
                            lo = kg
                        elif cond["operator"] == "LESS_THAN_OR_EQUAL_TO":
                            hi = kg
                    methods.append({"name": m["name"], "active": bool(m["active"]), "price": ((m.get("rateProvider") or {}).get("price") or {}).get("amount"), "min_kg": lo, "max_kg": hi})
                out.append({"profile": prof["name"], "zone": zn["zone"]["name"], "methods": methods,
                            "countries": [x["code"]["countryCode"] or ("ROW" if x["code"]["restOfWorld"] else "?") for x in zn["zone"]["countries"]]})
    _DZ_CACHE.update(at=now, data=out, failed_at=0.0)
    return out


def _rate_issue(m: dict, vz: dict) -> Optional[str]:
    """Motif si ce tarif laisse payer un panier hors règle de la zone (None = conforme) : pas de fenêtre de poids, ou fenêtre qui admet un nombre de bouteilles non autorisé."""
    if m["min_kg"] is None or m["max_kg"] is None:
        return "tarif sans fenêtre de poids"
    bk = config.BOTTLE_KG
    for b in range(1, int(m["max_kg"] // bk) + 1):
        if m["min_kg"] - 1e-6 <= b * bk <= m["max_kg"] + 1e-6 and ((vz.get("max_bottles") and b > vz["max_bottles"]) or (vz.get("multiple") and b % vz["multiple"])):
            return f"tarif « {m['name']} » payable avec {b} bouteilles"
    return None


def perimeter_leaks(force: bool = False) -> Optional[List[dict]]:
    """Pays que la boutique laisse commander hors du périmètre ouvert dans Vaelan : pays d'une zone fermée (ou « reste du monde »), ou pays ouvert dont un tarif actif
    accepte un panier hors règle. [] = boutique conforme ; None = Shopify injoignable. La France et Monaco ne sont jamais signalés."""
    zs = shopify_delivery_zones(force=force)
    if zs is None:
        return None
    out = []
    for z in zs:
        active = [m for m in z["methods"] if m["active"]]
        if not active:
            continue                                           # zone sans tarif actif : aucun paiement possible
        for cc in z["countries"]:
            if cc in ("FR", "MC"):
                continue
            vz = zone_of(cc) if cc not in ("ROW", "?") else None
            if not vz or not (vz.get("particulier") or vz.get("societe")):
                out.append({"country": cc, "profile": z["profile"], "zone": z["zone"], "reason": "pays non ouvert dans Vaelan"})
                continue
            issues = [i for i in (_rate_issue(m, vz) for m in active) if i]
            if issues:
                out.append({"country": cc, "profile": z["profile"], "zone": z["zone"], "reason": issues[0]})
    return out


def _refresh_open_task(key: str, title: str, details: str) -> None:
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import OwTask
    with Session(engine) as s_:
        t = s_.exec(select(OwTask).where(OwTask.key == key, OwTask.status == "open")).first()
        if t and (t.title != title or (t.details or "") != (details or "")):
            t.title, t.details = title, details
            s_.add(t); s_.commit()


def perimeter_guard(log=None) -> int:
    """Synchronisation : tâche « ALERTE » tant que Shopify laisse commander hors périmètre (rouverte si la fuite revient), fermée dès que la boutique est conforme."""
    log = log or (lambda m: None)
    leaks = perimeter_leaks(force=True)
    if leaks is None:
        log("garde-fou périmètre : zones Shopify illisibles"); return 0
    if not leaks:
        service.close_tasks_by_key(PERIMETER_TASK); return 0
    cc = sorted({l["country"] for l in leaks})
    title = f"ALERTE — Shopify accepte des commandes vers {len(cc)} pays hors périmètre : " + ", ".join(cc[:12]) + ("…" if len(cc) > 12 else "")
    details = ("Zones de livraison en cause : " + " ; ".join(sorted({f"{l['zone']} (profil {l['profile']})" for l in leaks})) + ". Motifs : " + " ; ".join(sorted({l["reason"] for l in leaks})) + ".\n"
               "Corriger dans Shopify › Paramètres › Expédition et livraison : supprimer la zone ou ses tarifs, ou poser la fenêtre de poids de la grille Vaelan (Douane › Mise en place, jalon 1). "
               "Une commande passée entre-temps est bloquée par le contrôle « périmètre » de Vaelan, mais le client a payé : la refuser et la rembourser depuis la commande.")
    if not service.add_task("customs", title, ref="export", key=PERIMETER_TASK, details=details):
        _refresh_open_task(PERIMETER_TASK, title, details)
    log(f"garde-fou périmètre : {len(cc)} pays hors périmètre")
    return 1


def _eur(v: float) -> str:
    return f"{float(v or 0):,.2f} €".replace(",", " ").replace(".", ",")


def setup_items() -> List[dict]:
    """Actions de mise en place rangées par jalon (JALONS) : réf. « J2·B5 », groupe, consignes pas à pas, état constaté quand Vaelan peut le lire (zones Shopify, réglages, degrés, droits)."""
    s = settings(); rows = customs_rows(); sc = _scopes()
    leaks = perimeter_leaks(); dz = shopify_delivery_zones() or []
    shop = {cc for z in dz if any(m["active"] for m in z["methods"]) for cc in z["countries"]}
    ch_live = leaks is not None and "CH" in shop and not any(l["country"] in ("CH", "LI") for l in leaks)
    if leaks is None:
        leak_txt = "État : zones Shopify illisibles pour l'instant."
    elif leaks:
        lc = sorted({l["country"] for l in leaks})
        leak_txt = f"État actuel : {len(lc)} pays hors France encore livrables ({', '.join(lc[:10])}{'…' if len(lc) > 10 else ''})."
    else:
        leak_txt = "État actuel : conforme, aucun pays hors périmètre."
    rs = rate_settings(); ch = chronopost_cost("CH", "classic", 6); al = alix_cost(6)
    g_ch = next((r for g in rate_grid() if g["zone"]["key"] == "CH" for r in g["rows"] if r["bottles"] == 6), None)
    if ch.get("shipments"):
        sh = ch["shipments"][0]
        cost_txt = (f"tarif Classic zone 4, tranche 7-12 kg {_eur(sh['base'])} + supplément douane {_eur(X.SUPPLEMENTS['customs_classic_zone4'])} + dédouanement export {_eur(rs['export_declaration'])} "
                    f"+ éco {_eur(X.SUPPLEMENTS['eco'])} + carburant {rs['fuel_pct']:g} % {_eur(sh['fuel'])} = {_eur(ch['total'])} Chronopost, + préparation Alix {_eur(al['total'])} = {_eur(ch['total'] + al['total'])} HT"
                    + (f" ; avec la marge de {rs['margin_pct']:g} % : {g_ch['price']:.0f} €" if g_ch else " (zone Suisse fermée dans Vaelan : pas de prix proposé)"))
    else:
        cost_txt = "incalculable (pas de tarif Classic pour la Suisse dans la configuration)"
    price_txt = f"{g_ch['price']:.0f} €" if g_ch else "le prix décidé en J2·B5"
    n_abv = sum(1 for r in rows if r.get("abv"))
    A = "A. Réponses à obtenir — à lancer tout de suite, elles fixent le prix et les textes"
    B = "B. Vaelan et comptabilité"
    C = "C. Boutique Shopify — préparée fermée : rien n'est visible des clients tant que la zone Suisse n'existe pas"
    D = "D. Envoi réel de test, puis ouverture"
    F = "Facultatif — améliore, ne bloque pas l'ouverture"
    E = "Contenu prévu (sera détaillé à la préparation du jalon)"
    items = [
        # ------------------------------------------------------------ jalon 1 : fermer la vente hors France
        {"id": "close_zones", "jalon": 1, "ref": "J1·1", "auto": True, "done": leaks is not None and not leaks,
         "title": "Supprimer les zones de livraison « UE » et « International » dans Shopify",
         "how": "1. Shopify › Paramètres › Expédition et livraison › Expédition › « Tarifs d'expédition généraux » (profil général, le seul de la boutique).\n"
                "2. Zone « UE (Union Européenne) » (26 pays, tarif 22 €) : bouton « ⋯ » › « Supprimer la zone ». Même chose pour la zone « International » (14 pays dont la Suisse, tarif 29 €).\n"
                "3. Ne pas toucher la zone « France » (tarifs au poids de 20 à 60 €) ni le retrait chez Alix.\n"
                "4. « Enregistrer ».\n"
                "Contrôle : Vaelan relit les zones à chaque synchronisation (ou « Actualiser la liste ») et coche cette action tout seul ; le bandeau rouge et la tâche « ALERTE » disparaissent. " + leak_txt},
        {"id": "close_markets", "jalon": 1, "ref": "J1·2", "done": False,
         "title": "Désactiver les marchés internationaux dans Shopify",
         "how": "1. Shopify › Paramètres › Marchés.\n"
                "2. Garder actif le marché principal (France).\n"
                "3. Chaque autre marché actif (« International », « Union européenne »…) : l'ouvrir › « ⋯ » › « Désactiver ». Le sélecteur de pays et de devise ne propose plus ces pays.\n"
                "4. Ne rien supprimer : le marché « Suisse » servira au jalon 2.\n"
                "Vaelan ne peut pas le vérifier (droit read_markets non accordé) : cliquer « Fait »."},
        {"id": "close_texts", "jalon": 1, "ref": "J1·3", "done": False,
         "title": "Retirer du site toute promesse de livraison à l'étranger",
         "how": "Vaelan ne peut pas lire les politiques (droit read_legal_policies non accordé) : vérification à la main.\n"
                "1. Shopify › Paramètres › Politiques › Politique d'expédition : retirer les tarifs Europe (22 €) et International (29 €) s'ils y figurent ; écrire « Nous livrons en France métropolitaine. Livraison à l'étranger : prochainement. »\n"
                "2. Boutique en ligne › Pages : FAQ, Livraison, CGV : même correction.\n"
                "3. Boutique en ligne › Thèmes › Personnaliser : bandeau d'annonce et pied de page (« livraison dans toute l'Europe »…).\n"
                "4. Même correction dans la version anglaise si des textes anglais sont déjà en ligne."},
        {"id": "close_check", "jalon": 1, "ref": "J1·4", "done": False,
         "title": "Vérifier sur le site qu'une commande hors France est impossible",
         "how": "1. Navigation privée sur owine.co : une bouteille au panier › « Paiement ».\n"
                "2. Adresse de livraison en Belgique, puis en Suisse, puis aux États-Unis : le pays n'est plus proposé, ou Shopify indique qu'aucun mode d'expédition n'est disponible. Ne pas payer.\n"
                "3. Même essai avec un bouton de paiement rapide (Shop Pay, PayPal, « Acheter maintenant ») s'il est affiché.\n"
                "4. Adresse en France : les tarifs habituels s'affichent (20 € jusqu'à 3 kg…).\n"
                "5. Le « Retrait » chez Alix peut rester proposé à un client étranger : la vente a lieu en France, sans question d'accises ni de douane.\n"
                "6. Vaelan › Douane › Mise en place : plus de bandeau rouge, J1·1 coché."},

        # ------------------------------------------------------------ jalon 2 : Suisse, particuliers, un carton de 6
        {"id": "chronopost", "jalon": 2, "group": A, "ref": "J2·A1", "done": False,
         "title": "Chronopost : questions écrites sur la Suisse",
         "how": "E-mail à Corentin Menard (corentin.menard@chronopost.fr, 02 42 28 00 33), objet « Contrat Chrono Viti Easy 84048903 — vin vers la Suisse », réponse écrite demandée :\n"
                "1. Particuliers en Suisse : le vin part-il bien en Chrono Classic (code produit 44) sur notre compte, Chrono Express étant exclu ?\n"
                "2. Frais réclamés au destinataire par votre partenaire suisse (dédouanement, avance de TVA et de droits) : montant exact, et contact par e-mail ou SMS avant la livraison ?\n"
                "3. Déclaration d'exportation : la déposez-vous ? Est-elle comprise dans le supplément douane de 15 € par expédition ou facturée en plus (21 € TTC par déclaration au contrat) ? Qui nous transmet le justificatif (MRN) ?\n"
                "4. Surcharge carburant applicable ce mois-ci ?\n"
                "5. La limite de 6 bouteilles et 10 kg vaut-elle par colis ou par envoi : peut-on grouper deux cartons de 6 sous une seule lettre de transport avec une seule facture commerciale ?\n"
                "Reporter : réponses 3 et 4 dans Douane › Zones (« Dédouanement export » : 0 si compris dans les 15 € ; « Surcharge carburant »), puis J2·B5 ; réponse 2 dans les textes du site (J2·D3). "
                "Réponse 1 négative = jalon suspendu. La réponse 5 ne bloque pas ce jalon (6 bouteilles au plus) : elle prépare la suite."},
        {"id": "alix", "jalon": 2, "group": A, "ref": "J2·A2", "done": False,
         "title": "Alix : organisation d'un envoi vers la Suisse",
         "how": "E-mail à Sébastien Poulet (direction@alixlogistique.com), copie Rémi Séry, en complément du mail du 15/09 :\n"
                "1. Pour chaque commande suisse, Vaelan envoie une facture commerciale en 3 exemplaires : les imprimer et les glisser dans la pochette Chronopost réf. 2010 collée sur le carton. D'accord ?\n"
                "2. Au premier envoi d'un vin, lire le degré sur l'étiquette et le donner par retour d'e-mail (Vaelan le mémorise et ne le redemande plus). D'accord ?\n"
                "3. Pochettes 2010 et stickers multi-pièces 1068 : bien reçus ?\n"
                "4. Stock en droits acquittés exporté hors UE : confirmer qu'aucun document d'accises n'est à émettre de leur côté, et ce qu'ils facturent pour une commande export (la ligne « BL / DAE Sortie » à 15,50 € ou 22 € s'applique-t-elle ?).\n"
                "Reporter la réponse 4 dans Douane › Zones : si cette ligne s'ajoute à chaque commande, l'ajouter au « minimum préparation » d'Alix."},
        {"id": "eori", "jalon": 2, "group": B, "ref": "J2·B1", "auto": True, "done": bool(s.get("eori")),
         "title": "N° EORI saisi dans Vaelan", "how": "Douane › Exportateur › N° EORI" + (f" : {s.get('eori')}." if s.get("eori") else " : à saisir (au SIREN, FR928409887).")},
        {"id": "legal", "jalon": 2, "group": B, "ref": "J2·B2", "auto": True, "done": bool(s.get("siret") and s.get("capital")),
         "title": "SIRET et capital social (pied de la facture commerciale)", "how": "Douane › Exportateur : SIRET (extrait Kbis) et capital (50 000 €)."},
        {"id": "signature", "jalon": 2, "group": B, "ref": "J2·B3", "auto": True, "done": bool(signature()),
         "title": "Charger l'image de votre signature",
         "how": "1. Signer sur une feuille blanche, photographier ou scanner, recadrer au plus près (PNG, moins de 1 Mo).\n"
                "2. Vaelan › Douane › onglet « Exportateur » › Signature › charger.\n"
                "Elle signe la facture commerciale. La déclaration d'origine imprimée sur la facture exige en plus une signature manuscrite, sauf statut d'exportateur agréé (J2·F1). "
                "Sans l'un ni l'autre, cette déclaration n'est pas valable : la douane suisse applique alors le droit normal, payé par le client (DAP) — rien d'illégal, un surcoût pour le client."},
        {"id": "abv", "jalon": 2, "group": B, "ref": "J2·B4", "auto": True, "done": bool(rows) and n_abv == len(rows),
         "title": "Degrés d'alcool : pré-remplir en un clic, confirmer ceux dont vous avez l'étiquette",
         "how": "1. Vaelan › Douane › onglet « Vins » › lien « Pré-remplir les degrés manquants » : 38 degrés publics (fiches des domaines) et une estimation pour les autres, au statut « estimé ».\n"
                "2. Vérifier d'abord les deux valeurs basses à 12 % : Corton-Charlemagne 2015 et Saint-Aubin 2022 de Pierre-Yves Colin-Morey.\n"
                "3. Cocher « Confirmer » seulement pour les vins dont vous avez l'étiquette sous les yeux : inutile de tout confirmer.\n"
                "4. Les autres sont confirmés par Alix à la première commande suisse qui les contient : la question part dans l'e-mail de préparation, vous saisissez la réponse sur la commande, "
                "puis « Envoyer la facture définitive à Alix » avant l'enlèvement (la facture suisse exige le type de boisson et le degré).\n"
                f"Se coche seul quand tous les vins ont un degré ({n_abv}/{len(rows)} aujourd'hui)."},
        {"id": "prices", "jalon": 2, "group": B, "ref": "J2·B5", "done": False,
         "title": "Port Suisse : valider les coûts et choisir le prix",
         "how": "1. Vaelan › Douane › onglet « Zones ouvertes & grille de port » : Suisse ouverte aux particuliers, 6 bouteilles au plus, multiple de 6 ; toutes les autres zones fermées "
                "(« UE Ouest » est fermée par défaut depuis la v0.1.198 : ne pas la rouvrir).\n"
                f"2. Coût actuel d'un carton de 6 : {cost_txt}. Le chiffre de 39,28 € du document du 15/09 ne comptait que le tarif et le supplément douane.\n"
                "3. Après les réponses de Chronopost (J2·A1) et d'Alix (J2·A2) : corriger « Surcharge carburant », « Dédouanement export » et « minimum préparation », ajuster la marge, « Enregistrer ».\n"
                "4. Décider le prix du port payé par le client (prix de la grille ou prix rond) : il sert au test J2·D1 et à l'ouverture J2·D2. "
                "Le client paiera en plus, à la livraison, la TVA suisse (8,1 %), le droit de douane et les frais du partenaire Chronopost."},
        {"id": "pennylane", "jalon": 2, "group": B, "ref": "J2·B6", "done": False,
         "title": "Pennylane : facture export sans TVA, avec la mention d'exonération",
         "how": "1. Demander à l'expert-comptable le compte de vente et le code TVA « export hors UE » à utiliser dans Pennylane.\n"
                "2. À la première commande suisse (dès le test J2·D1), le connecteur Shopify → Pennylane crée la facture en brouillon : vérifier TVA 0 % sur les vins et le port, client domicilié en Suisse, "
                "et ajouter la mention « Exonération de TVA, article 262 I du CGI » avant de finaliser.\n"
                "3. Archiver le justificatif d'exportation (MRN transmis par Chronopost) : c'est la preuve de l'exonération ; Vaelan crée la tâche « justificatif d'exportation » après l'envoi."},
        {"id": "customs_shopify", "jalon": 2, "group": C, "ref": "J2·C1", "done": False,
         "title": "Écrire dans Shopify les codes SH, l'origine et le poids des sélections",
         "how": "1. Vaelan › Douane › onglet « Shopify produits » : relire le plan (relevé du 15/09 : 94 articles à coder 22042113 blanc / 22042143 rouge, origine FR ; 48 sélections à 0 kg).\n"
                "2. « Écrire dans Shopify » (droit déjà accordé).\n"
                "⚠ Effet en France, à accepter avant de cliquer : la grille France est au poids. Une sélection de 6 bouteilles passe de 0 kg (port 20 €) à 9 kg (30 €), une sélection de 3 bouteilles à 4,5 kg (25 €). "
                "C'est le port juste, mais la hausse se voit.\n"
                "Sans ce poids, une sélection commandée depuis la Suisse n'a aucun tarif : le client ne peut pas payer."},
        {"id": "weights", "jalon": 2, "group": C, "ref": "J2·C2", "done": False,
         "title": "Poids du colis par défaut à 0 kg, cas du magnum",
         "how": "1. Shopify › Paramètres › Expédition et livraison › « Colis » : le colis par défaut doit peser 0 kg, sinon ce poids s'ajoute au panier et peut sortir de la fenêtre 8,9 – 9,4 kg du tarif suisse.\n"
                "2. Magnum Gevrey-Chambertin 1er Cru Aux Combottes 2019 (Hubert Lignier, 3 kg) : pas de carton de 6 pour lui à l'international. Seul au panier suisse, aucun tarif : commande impossible, c'est voulu. "
                "Avec 4 bouteilles (9 kg au total) le tarif s'afficherait : Vaelan bloque alors la commande (5 bouteilles) et vous la refusez ou l'arrangez avec le client.\n"
                "3. Tout article vendable hors vin doit avoir un poids réaliste ; une carte cadeau : « Ne nécessite pas d'expédition »."},
        {"id": "taxes", "jalon": 2, "group": C, "ref": "J2·C3", "done": False,
         "title": "Taxes : ne pas facturer la TVA française aux clients suisses",
         "how": "1. Shopify › Paramètres › Taxes et droits.\n"
                "2. Activer « Inclure ou exclure les taxes selon le pays du client » (libellé variable : prix incluant les taxes dynamiques).\n"
                "3. Ne pas collecter de taxe pour la Suisse (oWine n'est pas immatriculé en Suisse : livraison DAP).\n"
                "Effet : une bouteille à 120 € TTC en France s'affiche 100 € pour un client suisse, TVA 0 € sur la commande. Vérifié au test J2·D1 ; à défaut Vaelan signale « TVA française encaissée » sur la commande, à rembourser."},
        {"id": "checkout", "jalon": 2, "group": C, "ref": "J2·C4", "done": False,
         "title": "Paiement : e-mail et téléphone obligatoires, paiement rapide masqué",
         "how": "1. Shopify › Paramètres › Paiement › « Méthode de contact du client » : « E-mail » (et non « Numéro de téléphone ou e-mail »).\n"
                "2. Même page › « Informations client » : « Numéro de téléphone de l'adresse de livraison » = Obligatoire ; « Nom de l'entreprise » et « Adresse ligne 2 » = Facultatif. Cela vaut aussi pour la France (utile au livreur).\n"
                "3. Boutique en ligne › Thèmes › Personnaliser › modèle produit et panier : décocher « Afficher les boutons de paiement dynamiques » (Shop Pay, PayPal, Acheter maintenant) pour que le client passe par le panier, où l'extrait J2·C6 explique la règle.\n"
                "Pourquoi : le partenaire suisse de Chronopost contacte le client par e-mail ou SMS avant la livraison pour les droits et taxes ; sans téléphone le colis reste bloqué."},
        {"id": "markets", "jalon": 2, "group": C, "ref": "J2·C5", "done": False,
         "title": "Préparer le marché « Suisse » dans Shopify",
         "how": "1. Shopify › Paramètres › Marchés › « Ajouter un marché » (ou réactiver celui désactivé en J1·2) : nom « Suisse », pays Suisse et Liechtenstein.\n"
                "2. Devise : euro (pas de conversion en francs : grille de port et facture sont en euros). Langue : français (anglais quand il sera publié, J2·F3).\n"
                "3. Activer, puis refaire le test J1·4 avec une adresse suisse : toujours aucun mode d'expédition tant que la zone de livraison Suisse (J2·D2) n'existe pas. Si un tarif apparaît, désactiver le marché et me le signaler."},
        {"id": "snippet", "jalon": 2, "group": C, "ref": "J2·C6", "done": False,
         "title": "Installer l'extrait de panier (règle du carton de 6 expliquée au client)",
         "how": "1. Vaelan › Douane › onglet « Textes du site » › télécharger « owine-international.liquid ».\n"
                "2. Shopify › Boutique en ligne › Thèmes › « ⋯ » › « Modifier le code » › dossier « Snippets » › « Ajouter un snippet » nommé owine-international › coller le contenu › Enregistrer.\n"
                "3. Fichier sections/main-cart-footer.liquid : juste avant le bouton de paiement (name=\"checkout\"), ajouter la ligne {% render 'owine-international' %} › Enregistrer. Même ajout dans snippets/cart-drawer.liquid (tiroir panier).\n"
                "4. Panier en France : l'encadré s'affiche, le paiement reste possible.\n"
                "L'extrait lit les règles en direct dans Vaelan et n'annonce une destination que lorsqu'elle est réellement livrable dans Shopify : la Suisse apparaîtra d'elle-même après J2·D2. "
                "Pays fermé ou 7 bouteilles vers la Suisse : bouton de paiement désactivé avec un message clair. C'est un guide ; le verrou reste la fenêtre de poids du tarif."},
        {"id": "ch_test", "jalon": 2, "group": D, "ref": "J2·D1", "done": False,
         "title": "Envoi réel de test en Suisse, site toujours fermé (commande brouillon)",
         "how": "Prérequis : réponses J2·A1 et J2·A2, actions B et C faites.\n"
                "1. Shopify › Commandes › Brouillons › « Créer une commande » : un vrai destinataire en Suisse (proche ou client fidèle), adresse physique (pas de case postale), téléphone et e-mail ; 6 bouteilles ; "
                "« Ajouter l'expédition » › « Personnalisée » › « Chronopost Chrono Classic — 6 bouteilles », au prix décidé en J2·B5 ; TVA à 0 ; « Collecter le paiement » (lien envoyé au client, ou « Marquer comme payée »). Jamais de vin à 0 € : interdit en douane.\n"
                "2. Vaelan › la commande › « Synchroniser avec Shopify » : carte « International », contrôles verts (destination Chrono Classic, périmètre, coordonnées, EORI).\n"
                "3. Cartons : un carton de 6 (réf. 2036). chronopost.fr › Expédier : Chrono Classic vers la Suisse, 9 kg, 38 × 28 × 40 cm ; saisir le n° de colis dans Vaelan ; réserver l'enlèvement.\n"
                "4. « Relire les e-mails » puis « Valider et envoyer » : Alix reçoit la facture PROVISOIRE et la question des degrés.\n"
                "5. Réponse d'Alix : saisir les degrés sur la commande › « Envoyer la facture définitive à Alix » avant l'enlèvement (3 exemplaires dans la pochette 2010).\n"
                "6. Suivre : dédouanement (Chronotrace), montant réellement demandé au destinataire et par quel canal, délai, état du carton ; réception confirmée ; facture Pennylane (J2·B6) ; justificatif d'export.\n"
                "7. Noter le montant constaté des frais à la livraison pour les textes du site (J2·D3)."},
        {"id": "ch_open", "jalon": 2, "group": D, "ref": "J2·D2", "auto": True, "done": ch_live,
         "title": "Ouvrir : créer la zone de livraison Suisse dans Shopify",
         "how": "1. Shopify › Paramètres › Expédition et livraison › « Tarifs d'expédition généraux » › « Créer une zone » : nom « Suisse (Chrono Classic) », pays Suisse et Liechtenstein › « Terminé ».\n"
                f"2. Dans cette zone : « Ajouter un tarif » › « Configurer les tarifs vous-même » › nom « Chronopost Chrono Classic — 6 bouteilles (1 carton) », prix {price_txt} › « Ajouter des conditions » › « En fonction du poids de l'article » : "
                "minimum 8,9 kg, maximum 9,4 kg › « Terminé » › « Enregistrer ». Un seul tarif : 7 ou 12 bouteilles n'ont aucun tarif et ne peuvent pas être payées.\n"
                "   Variante : droit write_shipping accordé (J2·F2) → Vaelan › Douane › Zones › « Écrire dans Shopify » crée la même zone.\n"
                "3. Vaelan › « Actualiser la liste » : cette action se coche seule, sans bandeau rouge (sinon la fenêtre de poids du tarif n'est pas la bonne)."},
        {"id": "policies", "jalon": 2, "group": D, "ref": "J2·D3", "done": False,
         "title": "Le jour de l'ouverture : textes du site",
         "how": "Vaelan › Douane › onglet « Textes du site » (générés depuis les zones ouvertes) :\n"
                "1. Politique d'expédition → Shopify › Paramètres › Politiques › Politique d'expédition (remplace le texte « France seulement » de J1·3).\n"
                "2. Options de livraison → page FAQ / Livraison.\n"
                "3. Note DAP → éditeur du paiement (Paramètres › Paiement › Personnaliser) et Paramètres › Notifications › « Confirmation de commande ».\n"
                "4. Y ajouter le montant des frais demandés à la livraison (réponse J2·A1 ou constat J2·D1).\n"
                "5. Versions anglaises dans l'éditeur de langue, quand l'anglais est publié (J2·F3)."},
        {"id": "ch_check", "jalon": 2, "group": D, "ref": "J2·D4", "done": False,
         "title": "Contrôle public après l'ouverture, puis remboursement de la commande d'essai",
         "how": "1. Navigation privée, adresse suisse, 6 bouteilles : le tarif Chrono Classic s'affiche au prix prévu, TVA 0 €, téléphone exigé.\n"
                "2. 7 bouteilles, 12 bouteilles, une sélection de 3 : aucun mode d'expédition, et message de l'extrait au panier.\n"
                "3. Commande de 6 bouteilles payée jusqu'au bout : elle arrive dans Vaelan (carte International, périmètre vert) ; l'annuler et la rembourser dans Shopify.\n"
                "4. Adresses en Belgique et aux États-Unis : toujours impossibles.\n"
                "Jalon 2 terminé : préparer les suivants."},
        {"id": "ea", "jalon": 2, "group": F, "ref": "J2·F1", "optional": True, "done": False,
         "title": "Statut d'exportateur agréé (douane de Dijon)",
         "how": "Gratuit ; plus de signature manuscrite sur la déclaration d'origine et plus de plafond de 6 000 €.\n"
                "1. douane.gouv.fr › « pôle d'action économique » de la direction régionale de Dijon : demander par écrit le statut d'exportateur agréé pour des exportations de vins d'origine UE vers la Suisse, "
                "avec Kbis et n° EORI (FR928409887) ; ils envoient le formulaire et la liste des pièces (preuves d'origine : factures des vignerons).\n"
                "2. Le numéro d'autorisation obtenu doit figurer dans la déclaration d'origine : un champ sera ajouté aux réglages douane de Vaelan à ce moment-là."},
        {"id": "scopes", "jalon": 2, "group": F, "ref": "J2·F2", "optional": True, "auto": True, "done": "write_shipping" in sc,
         "title": "Droit Shopify write_shipping (grille écrite par Vaelan)",
         "how": "Seulement pour laisser Vaelan créer la zone Suisse (variante de J2·D2) et les zones futures.\n"
                "1. dev.shopify.com › Dev Dashboard › application Vaelan › Configuration (ou nouvelle version) › Access scopes : ajouter write_shipping (et pour l'anglais J2·F3 : read_locales, write_locales, read_translations, "
                "write_translations, read_themes, write_themes, read_markets, write_markets ; pour lire les politiques : read_legal_policies) › enregistrer ou publier la version.\n"
                "2. Shopify admin › Applications › Vaelan : accepter la mise à jour des autorisations (sinon réinstaller depuis le Dev Dashboard).\n"
                "Se coche seul quand write_shipping est accordé."},
        {"id": "languages", "jalon": 2, "group": F, "ref": "J2·F3", "optional": True, "done": False,
         "title": "Publier l'anglais et traduire la boutique",
         "how": "Pas indispensable pour la Suisse romande. Après les droits de J2·F2 et la clé ANTHROPIC_API_KEY ajoutée dans Render (variables d'environnement) : Vaelan › Douane › onglet « Anglais » › « Traduire en anglais », relire, "
                "puis « Publier l'anglais ». Sélecteur de langue : Thèmes › Personnaliser › En-tête › Localisation."},
        {"id": "age", "jalon": 2, "group": F, "ref": "J2·F4", "optional": True, "done": False,
         "title": "Vérification d'âge à l'entrée du site",
         "how": "Application gratuite « Age verification » (Shopify App Store) ou module du thème : « Avez-vous l'âge légal pour acheter de l'alcool ? », en français et en anglais. Utile aussi pour la France."},
        {"id": "insurance", "jalon": 2, "group": F, "ref": "J2·F5", "optional": True, "done": False,
         "title": "Décider de l'assurance ad valorem Chronopost",
         "how": "Responsabilité limitée à 23 € par kg en Chrono Classic, soit environ 207 € pour un carton de 9 kg. Pour une commande de valeur, souscrire l'option ad valorem à l'édition de l'étiquette et reporter la prime dans « Assurance HT » sur la commande."},

        # ------------------------------------------------------------ ensuite : pas encore de tâches
        {"id": "later_ch_split", "jalon": 9, "group": E, "ref": "E·1", "done": False,
         "title": "Suisse au-delà de 6 bouteilles",
         "how": "Attend la réponse 5 de Chronopost (limite par colis ou par envoi). Développement Vaelan : découpage d'une commande en envois (factures FC-OWxxxx-A, -B…), port recalculé (un supplément douane de 15 € par envoi, "
                "ou groupage), paramètre « limite par envoi / par colis » par pays, grille Shopify à 12 et 18 bouteilles. Permis général d'importation suisse seulement pour une société au-delà de 20 kg bruts."},
        {"id": "later_ue_b2b", "jalon": 9, "group": E, "ref": "E·2", "done": False,
         "title": "Union européenne : sociétés",
         "how": "Attend : réponse d'Alix sur l'agrément d'expéditeur certifié (stock en droits acquittés → DAES ; sans EC chez Alix, aucun envoi possible) et devis Eurotax / ASD Group (destinataire certifié dans chaque pays, "
                "couverture des 12 pays retenus, minimum mensuel, coût par envoi ; vérifier l'arrêt annoncé de becompliant.tax/wine au 1er octobre). Développement Vaelan : état « conformité accise » par commande "
                "(non requis → à déclarer → référence reçue → expédiable → apuré sous 5 jours) avec verrou « pas d'étiquette tant que non expédiable », référentiel accises par pays (taux, régime, statut), port par groupe "
                "(taux zéro / Benelux / Nord) et accise chiffrée au panier. Comptabilité : autoliquidation (art. 262 ter I CGI), état récapitulatif TVA mensuel ; contrôle VIES déjà en place."},
        {"id": "later_ue_b2c", "jalon": 9, "group": E, "ref": "E·3", "done": False,
         "title": "Union européenne : particuliers (vente à distance)",
         "how": "Représentant fiscal et garantie dans chaque pays avant la première bouteille (aucun seuil en accises) ; document commercial « vente à distance de produits soumis à accise » portant la référence de garantie ; "
                "TVA française tant que les ventes à distance UE cumulées restent sous 10 000 € HT par an, puis OSS ; un restaurant ou une société n'est jamais traité en particulier."},
        {"id": "later_gb_us", "jalon": 9, "group": E, "ref": "E·4", "done": False,
         "title": "Royaume-Uni et États-Unis",
         "how": "Royaume-Uni : Chrono Express seul, accise et TVA britanniques à l'import (la règle des 135 £ ne vise pas l'alcool). États-Unis : avenant Chrono Viti B2C US à signer. Degré exact indispensable (droits calculés sur le degré)."},
    ]
    for it in items:
        it["active"] = it["jalon"] in ACTIVE_JALONS
        it.setdefault("group", None); it.setdefault("optional", False); it.setdefault("auto", False)
    return items


def _setup_title(it: dict) -> str:
    return f"{it['ref']} — {it['title']}" + (" (facultatif)" if it.get("optional") else "")


def setup_tasks(log=None) -> int:
    """Une tâche Vaelan par action des jalons en cours (clé setup:<id>, titre « J2·B5 — … ») : titre et consignes tenus à jour, action constatée fermée,
    tâche cochée « fait » jamais rouverte ; une tâche encore ouverte d'une action sortie des jalons en cours est retirée (elle reviendra avec son jalon)."""
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import OwTask
    log = log or (lambda m: None); n = 0
    by_key = {f"setup:{it['id']}": it for it in setup_items() if it["active"]}
    seen = set()
    with Session(engine) as s_:
        for t in s_.exec(select(OwTask).where(OwTask.kind == "customs_setup")).all():
            it = by_key.get(t.key or "")
            if it is None:
                if t.status == "open" and (t.key or "").startswith("setup:"):
                    s_.delete(t)
                continue
            seen.add(t.key)
            if t.status != "open":
                continue                                           # faite (à la main ou constatée) : jamais rouverte
            if it["done"]:
                t.status, t.done_at = "done", datetime.utcnow()
            elif t.title != _setup_title(it) or (t.details or "") != it["how"]:
                t.title, t.details = _setup_title(it), it["how"]
            s_.add(t)
        s_.commit()
    for key, it in by_key.items():
        if key in seen or it["done"]:
            continue
        if service.add_task("customs_setup", _setup_title(it), ref="export", key=key, details=it["how"]):
            n += 1
    log(f"mise en place export : {n} nouvelle(s) tâche(s)")
    return n


def _announced_zones(live_only: bool = False) -> List[dict]:
    """Zones ouvertes (France comprise) ; live_only : zones hors France seulement si elles sont réellement livrables dans Shopify, sans fuite —
    le panier n'annonce rien avant l'ouverture de la zone (Shopify injoignable : France seule)."""
    zs = [z for z in zones() if z.get("particulier") or z.get("societe")]
    if not live_only:
        return zs
    dz = shopify_delivery_zones(); leaks = perimeter_leaks()
    if dz is None or leaks is None:
        return [z for z in zs if z["key"] == "FR"]
    bad = {l["country"] for l in leaks}
    shop = {cc for z in dz if any(m["active"] for m in z["methods"]) for cc in z["countries"]}
    return [z for z in zs if z["key"] == "FR" or any(cc in shop and cc not in bad for cc in z["countries"])]


def site_texts(live_only: bool = False) -> dict:
    """Textes FR / EN dérivés des zones ouvertes (live_only : seulement celles déjà livrables dans Shopify, ce que lit le panier) :
    options de livraison, message « pays fermé », politique d'expédition, note de paiement. Adresse client : contact@owine.co."""
    zs = _announced_zones(live_only)
    fr, en, short_fr, short_en = [], [], [], []
    for z in zs:
        who = " et ".join([w for w, ok in (("particuliers", z.get("particulier")), ("sociétés", z.get("societe"))) if ok])
        who_en = " and ".join([w for w, ok in (("private individuals", z.get("particulier")), ("companies", z.get("societe"))) if ok])
        names = ", ".join(X.country_name(c) for c in z["countries"]); names_en = ", ".join(X.country_name(c, "en") for c in z["countries"])
        lim = []; lim_en = []
        if z.get("max_bottles"):
            lim.append(f"{z['max_bottles']} bouteilles maximum par commande"); lim_en.append(f"up to {z['max_bottles']} bottles per order")
        if z.get("multiple"):
            lim.append(f"par carton de {z['multiple']}"); lim_en.append(f"in cases of {z['multiple']}")
        if z.get("vat_required"):
            lim.append("n° de TVA intracommunautaire valide obligatoire"); lim_en.append("a valid EU VAT number is required")
        if z.get("min_order"):
            lim.append(f"minimum {z['min_order']:.0f} € de vin"); lim_en.append(f"minimum €{z['min_order']:.0f} of wine")
        prod = X.PRODUCTS[z["product"]]["label"]
        fr.append(f"{z['label'].split(' (')[0]} ({names}) : {who} — Chronopost {prod}" + (f", {', '.join(lim)}" if lim else "") + ".")
        en.append(f"{names_en}: {who_en} — Chronopost {prod}" + (f", {', '.join(lim_en)}" if lim_en else "") + ".")
        short_fr.append(z["label"].split(" (")[0] + ("" if z["key"] == "FR" else f" ({who})")); short_en.append(names_en + ("" if z["key"] == "FR" else f" ({who_en})"))
    abroad = [z for z in zs if z["key"] != "FR"]
    ch = any(z["key"] == "CH" and z.get("particulier") for z in abroad)
    ue_b2b = any(z["key"].startswith("UE") and z.get("societe") for z in abroad)
    options_fr = "Nous livrons actuellement :\n" + "\n".join("• " + x for x in fr) + "\nLes autres destinations : prochainement. Une demande particulière : contact@owine.co."
    options_en = "We currently ship to:\n" + "\n".join("• " + x for x in en) + "\nOther destinations: coming soon. Special requests: contact@owine.co."
    closed_fr = "Nous ne livrons pas encore ce pays : prochainement. Destinations ouvertes : " + " ; ".join(short_fr) + "."
    closed_en = "We do not ship to this country yet: coming soon. Available destinations: " + "; ".join(short_en) + "."
    policy_fr = options_fr; policy_en = options_en
    if ch:
        policy_fr += ("\n\nSuisse — Vos vins partent de notre entrepôt de Beaune par Chronopost Chrono Classic, livraison en 2 à 4 jours ouvrés après l'enlèvement, à une adresse physique (pas de case postale). "
                      "Notre transporteur limite chaque envoi à 6 bouteilles : une commande = un carton de 6. Nos prix s'entendent hors TVA française. Votre commande est livrée « DAP » : la TVA suisse (8,1 %), le droit de douane "
                      "sur le vin et les frais de dédouanement du transporteur ne sont pas compris ; avant la livraison, le partenaire suisse de Chronopost vous les demande par e-mail ou SMS — un numéro de téléphone et une adresse "
                      "e-mail sont donc indispensables. Vous devez avoir l'âge légal pour acheter de l'alcool.")
        policy_en += ("\n\nSwitzerland — Your wines leave our Beaune warehouse with Chronopost Chrono Classic, delivered in 2 to 4 working days after collection, to a physical address (no PO box). "
                      "Our carrier limits each shipment to 6 bottles: one order = one case of 6. Our prices exclude French VAT. Your order is delivered “DAP”: Swiss VAT (8.1%), the customs duty on wine and the carrier's "
                      "clearance fee are not included; before delivery, Chronopost's Swiss partner will ask you to pay them by e-mail or text message — a phone number and an e-mail address are therefore required. "
                      "You must be of legal drinking age.")
    if ue_b2b:
        policy_fr += ("\n\nUnion européenne (sociétés) — Livraison par Chronopost Chrono Classic en 2 à 4 jours ouvrés selon le pays, par carton de 6. Facture hors TVA sur présentation d'un numéro de TVA "
                      "intracommunautaire valide (autoliquidation) ; les accises du pays de destination sont traitées avant le départ.")
        policy_en += ("\n\nEuropean Union (companies) — Chronopost Chrono Classic, 2 to 4 working days depending on the country, in cases of 6. Invoice without VAT against a valid EU VAT number (reverse charge); "
                      "excise duties of the destination country are handled before dispatch.")
    policy_fr += "\n\nÀ la livraison — Ouvrez les cartons devant le livreur, notez toute réserve sur le bon de livraison avant de signer, photographiez et écrivez-nous à contact@owine.co : nous prenons le relais auprès de Chronopost."
    policy_en += "\n\nOn delivery — Open the cases in front of the driver, write any reservation on the delivery note before signing, take photos and e-mail contact@owine.co: we take over with Chronopost."
    export = any(zone(c) == "EXPORT" for z in abroad for c in z["countries"])
    checkout_fr = ("Livraison hors Union européenne : les droits de douane, la TVA et les frais de dédouanement de votre pays ne sont pas compris et vous seront demandés par le transporteur avant la livraison (incoterm DAP)."
                   if export else "")
    checkout_en = ("Delivery outside the European Union: import duties, VAT and clearance fees of your country are not included and will be requested by the carrier before delivery (Incoterm DAP)."
                   if export else "")
    return {"options_fr": options_fr, "options_en": options_en, "policy_fr": policy_fr, "policy_en": policy_en, "checkout_fr": checkout_fr, "checkout_en": checkout_en,
            "closed_fr": closed_fr, "closed_en": closed_en}


def public_rules() -> dict:
    """Règles lues par l'extrait de thème (JSON public) : zones réellement livrables, limites, textes FR/EN (options, pays fermé)."""
    t = site_texts(live_only=True)
    return {"zones": {z["key"]: {"countries": z["countries"], "particulier": bool(z.get("particulier")), "societe": bool(z.get("societe")), "max": z.get("max_bottles"), "multiple": z.get("multiple"),
                                 "vat": bool(z.get("vat_required")), "label": z["label"].split(" (")[0]} for z in _announced_zones(live_only=True)},
            "texts": {"fr": t["options_fr"], "en": t["options_en"]}, "closed": {"fr": t["closed_fr"], "en": t["closed_en"]}, "bottle_kg": config.BOTTLE_KG}


def theme_snippet() -> str:
    """Extrait Liquid + JS pour la page panier : options par pays, règles de bouteilles (max / multiples de 6), n° de TVA vérifié (VIES via Vaelan) pour les sociétés UE, attributs de commande lus par Vaelan."""
    zs = zones(); base = service.admin_url()
    rules = {z["key"]: {"countries": z["countries"], "particulier": bool(z.get("particulier")), "societe": bool(z.get("societe")), "max": z.get("max_bottles"), "multiple": z.get("multiple"), "vat": bool(z.get("vat_required")), "label": z["label"].split(" (")[0]} for z in zs if z.get("particulier") or z.get("societe")}
    t = site_texts()
    return r'''{%- comment -%} oWine — règles d'expédition internationale (généré par Vaelan, page Douane › Textes du site). À rendre dans le panier (main-cart-footer.liquid) et le tiroir panier (cart-drawer.liquid), avant le bouton de paiement. Les règles sont lues en direct sur Vaelan. {%- endcomment -%}
{%- if cart.item_count > 0 -%}
<div class="ow-intl" data-country="{{ localization.country.iso_code }}" data-lang="{{ request.locale.iso_code }}" data-company="{{ cart.attributes['Société'] | escape }}" data-vat="{{ cart.attributes['N° TVA'] | escape }}"
     style="margin:16px 0;padding:14px 16px;border:1px solid #d9c9c5;border-radius:8px;font-size:.95rem;">
  <div class="ow-intl__options" style="white-space:pre-line;color:#4a0d1f;"></div>
  <div class="ow-intl__company" style="display:none;margin-top:10px;">
    <label style="display:block;margin-bottom:6px;"><input type="checkbox" class="ow-company"> <span data-fr="Je commande pour une société" data-en="I am ordering for a company"></span></label>
    <div class="ow-vat-wrap" style="display:none;">
      <label style="display:block;font-size:.9rem;"><span data-fr="N° de TVA intracommunautaire (vérifié automatiquement)" data-en="EU VAT number (checked automatically)"></span></label>
      <input class="ow-vat" type="text" placeholder="BE0123456789 / DE123456789" style="width:100%;max-width:320px;padding:8px;border:1px solid #bbb;border-radius:6px;">
      <div class="ow-vat-msg" style="font-size:.88rem;margin-top:4px;"></div>
    </div>
  </div>
  <div class="ow-intl__msg" style="margin-top:8px;font-weight:600;color:#8c1a1a;"></div>
</div>
<script>
(function(){
  if (window.__owIntl) return; window.__owIntl = true;
  var BASE = "''' + base + r'''";
  var msgs = {
    closed: {fr: "Nous ne livrons pas encore ce pays : livraison prochainement. Choisissez la France, la Suisse (particuliers) ou une société dans l'Union européenne.", en: "We do not ship to this country yet: coming soon. Choose France, Switzerland (private customers) or a company in the European Union."},
    max: {fr: "{n} bouteilles maximum par commande vers ce pays (un carton de 6). Retirez des bouteilles pour continuer.", en: "Up to {n} bottles per order to this country (one case of 6). Remove bottles to continue."},
    mult: {fr: "Expédition par carton de 6 : ajustez la quantité à un multiple de 6 bouteilles.", en: "Shipped in cases of 6: adjust the quantity to a multiple of 6 bottles."},
    company_only: {fr: "Dans l'Union européenne, nous livrons pour l'instant les sociétés (n° de TVA intracommunautaire). Cochez « je commande pour une société » et indiquez votre numéro.", en: "In the European Union we currently deliver to companies only (EU VAT number). Tick “I am ordering for a company” and enter your number."},
    vat_bad: {fr: "Numéro de TVA non reconnu par VIES : vérifiez-le (code pays + chiffres, sans espaces).", en: "VAT number not recognised by VIES: please check it (country code + digits, no spaces)."},
    vat_ok: {fr: "Numéro de TVA valide", en: "Valid VAT number"}, vat_wait: {fr: "Vérification VIES…", en: "Checking VIES…"}
  };
  var rules = null, cart = null;
  function setAttr(obj){ return fetch('/cart/update.js', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({attributes: obj})}); }
  function lock(on, text){ document.querySelectorAll('.ow-intl__msg').forEach(function(m){ m.textContent = text || ''; });
    document.querySelectorAll('button[name="checkout"], [name="checkout"], .cart__checkout-button, #checkout, #CartDrawer-Checkout').forEach(function(b){ b.disabled = !!on; b.style.opacity = on ? .5 : 1; }); }
  function evaluate(){
    var el = document.querySelector('.ow-intl'); if (!el || !rules || !cart) return;
    var lang = (el.dataset.lang || 'fr').slice(0,2) === 'en' ? 'en' : 'fr', cc = el.dataset.country || 'FR';
    var bottles = Math.round((cart.total_weight / 1000) / (rules.bottle_kg || 1.5));
    var zone = null; Object.keys(rules.zones).forEach(function(k){ if (rules.zones[k].countries.indexOf(cc) >= 0) zone = rules.zones[k]; });
    document.querySelectorAll('.ow-intl [data-fr]').forEach(function(s){ s.textContent = s.dataset[lang]; });
    document.querySelectorAll('.ow-intl__options').forEach(function(o){ o.textContent = rules.texts[lang]; });
    if (cc === 'FR' || cc === 'MC') { lock(false); return; }
    if (!zone) { lock(true, (rules.closed && rules.closed[lang]) || msgs.closed[lang]); return; }
    if (zone.max && bottles > zone.max) { lock(true, msgs.max[lang].replace('{n}', zone.max)); return; }
    if (zone.multiple && bottles % zone.multiple !== 0) { lock(true, msgs.mult[lang]); return; }
    if (!zone.societe) { lock(false); return; }
    var box = el.querySelector('.ow-intl__company'), chk = el.querySelector('.ow-company'), wrap = el.querySelector('.ow-vat-wrap'), vat = el.querySelector('.ow-vat'), vmsg = el.querySelector('.ow-vat-msg');
    box.style.display = 'block';
    if (!chk.dataset.bound) { chk.dataset.bound = '1'; chk.checked = ((cart.attributes || {})['Société'] === 'oui'); vat.value = (cart.attributes || {})['N° TVA'] || '';
      chk.addEventListener('change', refresh); vat.addEventListener('change', refresh); vat.addEventListener('blur', refresh); }
    function refresh(){
      wrap.style.display = chk.checked ? 'block' : 'none';
      if (!chk.checked) { setAttr({'Société': '', 'N° TVA': '', 'TVA vérifiée': ''}); if (!zone.particulier) lock(true, msgs.company_only[lang]); else lock(false); return; }
      setAttr({'Société': 'oui'});
      if (!zone.vat) { lock(false); return; }
      var v = (vat.value || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (v.length < 4) { lock(true, msgs.company_only[lang]); vmsg.textContent = ''; return; }
      vmsg.textContent = msgs.vat_wait[lang]; lock(true, '');
      fetch(BASE + '/api/owine/vies?vat=' + encodeURIComponent(v)).then(function(r){ return r.json(); }).then(function(d){
        if (d.valid) { vmsg.textContent = msgs.vat_ok[lang] + (d.name ? ' : ' + d.name : ''); vmsg.style.color = '#2e6f4b'; setAttr({'N° TVA': v, 'TVA vérifiée': 'oui ' + (d.name || '')}).then(function(){ lock(false); }); }
        else { vmsg.textContent = msgs.vat_bad[lang] + (d.error ? ' (' + d.error + ')' : ''); vmsg.style.color = '#a8261d'; setAttr({'N° TVA': v, 'TVA vérifiée': ''}); lock(true, ''); }
      }).catch(function(){ vmsg.textContent = msgs.vat_bad[lang]; lock(true, ''); });
    }
    refresh();
  }
  function loadCart(){ return fetch('/cart.js').then(function(r){ return r.json(); }).then(function(c){ cart = c; evaluate(); }); }
  fetch(BASE + '/api/owine/rules').then(function(r){ return r.json(); }).then(function(r){ rules = r; return loadCart(); }).catch(function(){});
  if (window.subscribe) { try { subscribe('cart-update', loadCart); } catch (e) {} }
  setInterval(loadCart, 5000);
})();
</script>
{%- endif -%}
'''


# ================================================================== traduction anglaise de la boutique (Claude → translationsRegister) ; nécessite read/write_translations et write_locales
TRANSLATABLE = [("PRODUCT", ["title", "body_html"]), ("COLLECTION", ["title", "body_html"]), ("PAGE", ["title", "body_html"]), ("SHOP_POLICY", ["body"]),
                ("MENU", None), ("LINK", None), ("PRODUCT_OPTION", None), ("PRODUCT_OPTION_VALUE", None), ("DELIVERY_METHOD_DEFINITION", None),
                ("ONLINE_STORE_THEME_LOCALE_CONTENT", None), ("ONLINE_STORE_THEME_JSON_TEMPLATE", None), ("ONLINE_STORE_THEME_SECTION_GROUP", None), ("ONLINE_STORE_THEME_SETTINGS_DATA_SECTIONS", None)]
                # None = toutes les clés textuelles de la ressource (chaînes du thème, menus, options)


def _scopes() -> set:
    c = service._shopify()
    try:
        return set(c.scopes())
    except Exception:
        return set()


def translation_plan(limit: int = 400) -> dict:
    """Ressources dont le contenu anglais manque (ou est périmé : digest différent)."""
    c = service._shopify()
    if not c:
        raise RuntimeError("Shopify non configuré")
    sc = _scopes(); missing = [s for s in ("read_translations", "write_translations", "read_locales", "write_locales") if s not in sc]
    plan = {"missing_scopes": missing, "resources": [], "locale_en": None}
    if "read_locales" in sc:
        try:
            loc = c.gql("{ shopLocales { locale published primary } }")["shopLocales"]
            plan["locale_en"] = next((l for l in loc if l["locale"] == "en"), None)
        except Exception as e:
            plan["locale_error"] = str(e)[:120]
    if "read_translations" not in sc:
        return plan
    for rtype, keys in TRANSLATABLE:
        q = """query($first:Int!,$after:String,$t:TranslatableResourceType!){ translatableResources(first:$first, after:$after, resourceType:$t) { pageInfo{hasNextPage endCursor}
              edges{ node{ resourceId translatableContent{ key value digest locale } translations(locale:"en"){ key value outdated } } } } }"""
        try:
            nodes = c.pages(q, "translatableResources", variables={"t": rtype}, first=50)
        except Exception as e:
            plan.setdefault("errors", []).append(f"{rtype} : {str(e)[:120]}"); continue
        for n in nodes:
            done = {t["key"]: t for t in n.get("translations") or []}
            for tc in n.get("translatableContent") or []:
                val = (tc.get("value") or "").strip()
                if (keys is None or tc["key"] in keys) and val and not re.fullmatch(r"[\d\W_]*", val) and (tc["key"] not in done or done[tc["key"]].get("outdated")):
                    plan["resources"].append({"type": rtype, "id": n["resourceId"], "key": tc["key"], "digest": tc["digest"], "value": tc["value"]})
    plan["resources"] = plan["resources"][:limit]
    return plan


def _translate_fr_en(texts: List[str]) -> List[str]:
    """Traduction FR → EN par Claude : textes longs un par un (HTML conservé), textes courts par lots JSON (chaînes du thème, menus, options).
    Vocabulaire du vin inchangé (appellations, climats, cépages, millésimes), variables Liquid {{ … }} conservées."""
    from app.core import assistant
    if not assistant.configured():
        raise RuntimeError("clé Anthropic absente (ANTHROPIC_API_KEY)")
    import anthropic
    client = anthropic.Anthropic()
    system = ("You translate French e-commerce content for oWine, a Burgundy wine merchant in Dijon, into natural British English for wine lovers. Keep HTML tags, Liquid placeholders like {{ count }} and structure exactly; "
              "keep proper nouns, appellations, climats, producers, vintages and wine terms (Premier Cru, Grand Cru, climat, lieu-dit) untranslated; keep prices and units.")
    out: List[Optional[str]] = [None] * len(texts)
    short = [i for i, t in enumerate(texts) if len(t) <= 400]
    for k in range(0, len(short), 40):
        idx = short[k:k + 40]
        payload = json.dumps({str(i): texts[i] for i in idx}, ensure_ascii=False)
        r = client.messages.create(model="claude-sonnet-5", max_tokens=8000, system=system + " You receive a JSON object of id → French text and return ONLY a JSON object with the same ids → English text.",
                                   messages=[{"role": "user", "content": payload}])
        txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        try:
            m = re.search(r"\{.*\}", txt, re.S); d = json.loads(m.group(0)) if m else {}
        except Exception:
            d = {}
        for i in idx:
            v = d.get(str(i))
            out[i] = v.strip() if isinstance(v, str) and v.strip() else ""
    for i, t in enumerate(texts):
        if out[i] is not None:
            continue
        r = client.messages.create(model="claude-sonnet-5", max_tokens=12000, system=system + " Return only the translation, nothing else.", messages=[{"role": "user", "content": t}])
        if getattr(r, "stop_reason", "") == "max_tokens":
            out[i] = ""                                           # tronqué : on n'enregistre pas une traduction incomplète
            continue
        v = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        out[i] = v if v.count("<") == t.count("<") else ""        # balises HTML déséquilibrées : rejeté
    return [x or "" for x in out]


def translation_apply(log=None, batch: int = 20) -> str:
    log = log or (lambda m: None)
    c = service._shopify(); plan = translation_plan()
    if plan["missing_scopes"]:
        raise RuntimeError("droits Shopify manquants : " + ", ".join(plan["missing_scopes"]))
    if not plan.get("locale_en"):
        r = c.gql("""mutation{ shopLocaleEnable(locale:"en"){ shopLocale{ locale published } userErrors{ message } } }""")
        errs = (r.get("shopLocaleEnable") or {}).get("userErrors") or []
        if errs:
            raise RuntimeError(f"activation de l'anglais refusée : {errs}")
        log("langue anglaise activée (non publiée)")
    by_res: Dict[str, List[dict]] = {}
    for x in plan["resources"]:
        by_res.setdefault(x["id"], []).append(x)
    n = 0
    for rid, items in by_res.items():
        try:
            vals = _translate_fr_en([x["value"] for x in items])
        except Exception as e:
            log(f"{rid} : traduction impossible — {e}"); continue
        tr = [{"locale": "en", "key": x["key"], "value": v, "translatableContentDigest": x["digest"]} for x, v in zip(items, vals) if v]
        r = c.gql("""mutation($id:ID!,$t:[TranslationInput!]!){ translationsRegister(resourceId:$id, translations:$t){ userErrors{ field message } } }""", {"id": rid, "t": tr})
        errs = (r.get("translationsRegister") or {}).get("userErrors") or []
        if errs:
            log(f"{rid} : {errs}")
        else:
            n += len(tr)
    msg = f"{n} champ(s) traduit(s) en anglais sur {len(by_res)} ressource(s) ; publier la langue dans Paramètres › Langues (ou bouton « Publier l'anglais »)"
    log(msg); return msg


def publish_english(log=None) -> str:
    c = service._shopify()
    r = c.gql("""mutation{ shopLocaleUpdate(locale:"en", shopLocale:{published:true}){ shopLocale{ locale published } userErrors{ message } } }""")
    errs = (r.get("shopLocaleUpdate") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError(f"publication refusée : {errs}")
    return "anglais publié : owine.co/en"
