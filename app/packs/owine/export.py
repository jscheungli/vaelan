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
from datetime import date, datetime
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
    if o.customer_type in ("societe", "particulier"):
        return o.customer_type
    return "societe" if (o.company or o.billing_company or o.vat_number or o.eori) else "particulier"


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
        return f"{d} working day{'s' if d not in ('1',) else ''} after collection"
    return f"J+{d} jour{'s' if d not in ('1',) else ''} ouvré{'s' if d not in ('1',) else ''} après l'enlèvement"


# ================================================================== état export d'une commande (JSON)
def get_state(o) -> dict:
    try:
        return json.loads(o.export_json or "{}") or {}
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


def toggle_check(o, key: str, done: bool, by: str = None, note: str = None) -> None:
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
    """Convention des références oWine : lettre de couleur avant « B<millésime> » (…RB14 = rouge, …BB23 = blanc)."""
    m = re.search(r"([RB])B\d{2}$", (sku or "").upper())
    return {"R": "rouge", "B": "blanc"}.get(m.group(1)) if m else None


def colour_from_title(title: str) -> Optional[str]:
    t = (title or "").lower()
    if any(w in t for w in ("blanc", "chardonnay", "aligoté", "aligote", "montrachet", "meursault", "corton-charlemagne", "white")):
        return "blanc"
    if any(w in t for w in ("rouge", "pinot noir", "red")):
        return "rouge"
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
    if not it.millesime:
        missing.append("millésime")
    return {"sku": it.sku, "title": it.title, "colour": colour, "colour_saved": bool(it.couleur), "abv": it.abv, "abv_status": it.abv_status or ("estime" if it.abv else None), "abv_source": it.abv_source,
            "confirmed_at": it.customs_confirmed_at, "confirmed_by": it.customs_confirmed_by, "volume_cl": it.volume_cl or 75, "hs": hs, "hs_saved": bool(saved_hs and saved_hs != X.HS_GENERIC),
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
        v = (form.get(f"abv_{sku}") or "").replace(",", ".").strip()
        fields["abv"] = float(v) if v else None
        cur = service.get_item(sku)
        if cur and fields["abv"] is not None and (cur.abv or 0) != fields["abv"]:
            fields["abv_source"] = "saisi à la main"; fields["abv_status"] = "estime" if cur.abv_status != "confirme" else "confirme"
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
        if vies.get("valid") is False:
            per_issues.append(f"n° de TVA intracommunautaire invalide ({vies.get('error') or o.vat_number})")
        elif vies.get("valid") is None and not vies.get("na"):
            per_issues.append(f"VIES injoignable : {vies.get('error')} — vérifier à la main puis cocher « TVA vérifiée »")
    vat_manual = bool((checks_done.get("vat_checked") or {}).get("done"))
    add("perimeter", f"Périmètre ouvert : {zlabel}" if zopen else "Périmètre de vente", ok=not per_issues or (vat_manual and all("TVA" in x or "VIES" in x for x in per_issues)),
        detail=" · ".join(per_issues) if per_issues else ((f"TVA {o.vat_number} valide sur VIES : {vies.get('name') or ''}" if vies and vies.get('valid') else "règles de la zone respectées")))
    add("vat_checked", "N° de TVA vérifié à la main (VIES indisponible)", manual=True, blocking=False, when=bool(vies) and vies.get("valid") is None)
    rec = _recipient_issues(o)
    add("recipient", "Coordonnées du destinataire complètes", ok=not rec, detail=" · ".join(rec) if rec else "adresse, téléphone, e-mail" + (", EORI / TVA" if k == "societe" else ""))
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
        uw = unconfirmed_wines(o, cs)
        add("abv", "Degrés d'alcool confirmés (étiquette) pour tous les vins de la commande", ok=not uw,
            detail=("à confirmer : " + " · ".join(f"{w['title']} ({w['abv']:g} % estimé)" if w.get("abv") else w["title"] for w in uw) + " — demander à Alix (bouton) ou confirmer sur la page Douane") if uw else "tous confirmés"
            + (f" · demande envoyée à Alix le {st.get('abv_request_sent_at')}" if st.get("abv_request_sent_at") and uw else ""))
        final = bool(inv.get("final"))
        add("invoice", "Facture commerciale définitive générée (3 exemplaires, numéros de colis)", ok=final,
            detail=(f"version {inv.get('version')} du {inv.get('at')}" + ("" if inv.get("final") else " — PROVISOIRE : regénérer après les étiquettes")) if inv else "à générer une fois les étiquettes reçues")
        add("signature", "Signature de l'exportateur apposée (image enregistrée dans Réglages douane)", ok=bool(signature()), blocking=False, when=not (checks_done.get("signed") or {}).get("done"),
            detail="sans image, les 3 exemplaires imprimés par Alix ne sont pas signés : charger une signature, ou signer à la main et cocher ci-dessous")
        add("signed", "Facture signée à la main (si pas de signature enregistrée)", manual=True, when=not signature(), blocking=False)
        add("vat", "Facture Pennylane sans TVA (exonération art. 262 I CGI, mention portée) vérifiée", manual=True, blocking=False, detail="le connecteur Shopify crée la facture au paiement : contrôler le taux 0 % export et la mention d'exonération")
    if z == "UE":
        add("vat_oss", "TVA du pays de destination appliquée (guichet OSS)", manual=True, blocking=False, detail="vente à distance intracommunautaire de produits soumis à accise : TVA du pays de destination dès le 1er euro, déclarée via l'OSS")
        add("excise", "Accises : représentant fiscal / document d'accompagnement", manual=True, blocking=False, detail="particulier : accises dues dans le pays de destination via un représentant fiscal ; société : e-DSA (GAMMA) — noter la référence")
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
        lv = {l["sku"]: l for l in tot_all["lines"]}
        for sku, a in agg.items():
            l = lv.get(sku)
            a["unit_ht"] = l["unit_ht"] if l else ht(float((imap.get(sku).price if imap.get(sku) and imap.get(sku).price else 0) or 0), o)
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
        gross = round(sum(c.weight_kg for c in cartons), 1)
        pack = round(sum((config.PACKAGING.get(c.box_sku) or {}).get("kg", 0) for c in cartons), 2)
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
    g = lambda k: (data.get(k) or "").strip()
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
    else:
        o.tax_id = g("tax_id") or o.tax_id
    if g("address2") and g("address2") not in (o.address2 or ""):
        o.address2 = ((o.address2 or "") + " " + g("address2")).strip()
    st = get_state(o)
    st.setdefault("customs_form", {}).update({"submitted_at": service.now_local().strftime("%d/%m/%Y %H:%M"), "ip": ip, "dap_accepted": g("dap") == "1", "lang": g("lang") or "fr"})
    st.setdefault("checks", {})["recipient_ok"] = {"done": True, "at": service.now_local().isoformat(timespec="minutes"), "by": "client (formulaire)", "note": "accord DAP : " + ("oui" if g("dap") == "1" else "non")}
    set_state(o, st)
    service.close_tasks_by_key(f"customs_info:{o.name}")


# ================================================================== tâches export (synchro) et Shopify : codes SH, origine, poids des sélections
def export_tasks(log=None) -> int:
    """Commandes internationales à traiter → tâche « formalités » ; vins sans données douanières utilisés par ces commandes → tâche « données douanières »."""
    log = log or (lambda m: None); n = 0
    for o in service.orders():
        if o.status in ("cloturee", "annulee") or zone(o.country) == "FR":
            continue
        if o.status in ("a_traiter", "cartons", "etiquettes"):
            z = zone(o.country)
            if service.add_task("customs", f"{o.name} : commande {'export (douane)' if z == 'EXPORT' else 'intracommunautaire'} vers {X.country_name(o.country)} — suivre les étapes export sur la commande",
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
        maxb = z.get("max_bottles") or 18
        steps = [n for n in (6, 12, 18) if n <= maxb]
        kind = "particulier" if z.get("particulier") else "societe"
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
                         "window": (round(n * config.BOTTLE_KG - 0.1, 2), round(n * config.BOTTLE_KG + 0.1, 2))})
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
        current.append({"id": zz["id"], "name": zz["name"], "countries": codes, "keep": set(codes) <= {"FR", "MC"} or codes == ["FR"],
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
        zones_create.append({"name": zc["name"], "countries": [{"code": cc} for cc in zc["countries"]],
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
                it.abv = float(r["abv"]); it.abv_source = f"{'fiche publique' if r.get('match') == 'exact' else 'millésime voisin'} : {r.get('source', '')}"[:200]; it.abv_status = "estime"
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


def vies_check(vat: str, force: bool = False) -> dict:
    """{valid, name, address, checked_at, error} ; résultat gardé 30 jours (Setting). Hors UE (CHE-…, GB…) : non applicable."""
    import httpx
    v = re.sub(r"[^A-Z0-9]", "", (vat or "").upper())
    if len(v) < 4:
        return {"valid": False, "error": "numéro vide ou trop court", "vat": v}
    cc, num = v[:2], v[2:]
    if cc == "EL":
        pass
    if cc not in X.EU or cc == "FR" and False:
        if cc not in X.EU:
            return {"valid": None, "error": f"{cc} : hors VIES (pays non membre de l'UE)", "vat": v, "na": True}
    key = f"owine:vies:{v}"
    cached = service._setting_get(key)
    if cached and not force:
        try:
            if (datetime.utcnow() - datetime.fromisoformat(cached["checked_at"])).days < 30:
                return cached
        except Exception:
            pass
    try:
        r = httpx.post(VIES_URL, json={"countryCode": cc, "vatNumber": num}, timeout=15, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            return {"valid": None, "error": f"VIES indisponible (HTTP {r.status_code})", "vat": v}
        d = r.json()
        out = {"vat": v, "valid": bool(d.get("valid")), "name": (d.get("name") or "").strip() or None, "address": (d.get("address") or "").strip().replace("\n", ", ") or None,
               "checked_at": datetime.utcnow().isoformat(timespec="seconds"), "error": None if d.get("userError") in (None, "VALID", "INVALID") else d.get("userError")}
        if d.get("userError") in ("MS_UNAVAILABLE", "SERVICE_UNAVAILABLE", "TIMEOUT", "MS_MAX_CONCURRENT_REQ", "GLOBAL_MAX_CONCURRENT_REQ"):
            out["valid"] = None; out["error"] = f"VIES : {d['userError']} (réessayer)"
        else:
            service._setting_set(key, out)
        return out
    except Exception as e:
        return {"valid": None, "error": f"VIES injoignable : {type(e).__name__}", "vat": v}


# ================================================================== mise en place : liste des actions JS (tâches), textes du site, extrait de thème
def setup_items() -> List[dict]:
    s = settings(); rs = rate_settings()
    return [
        {"id": "eori", "title": "Obtenir le n° EORI d'oWine et le saisir dans Réglages douane", "done": bool(s.get("eori")),
         "how": "douane.gouv.fr › Services en ligne › EORI › « Demander un numéro EORI » (gratuit, réponse sous quelques jours ; format FR + SIRET). Puis page Douane › Exportateur › N° EORI. Sans EORI, aucune facture commerciale n'est valable."},
        {"id": "signature", "title": "Charger une image de votre signature (PNG)", "done": bool(signature()),
         "how": "Page Douane › Exportateur › Signature. Elle est apposée sur les 3 exemplaires de chaque facture commerciale ; sans elle, Alix imprime des factures non signées."},
        {"id": "legal", "title": "Compléter SIRET et capital social (pied de facture)", "done": bool(s.get("siret") and s.get("capital")),
         "how": "Page Douane › Exportateur : SIRET (extrait Kbis) et capital (50 000 € au contrat Chronopost). Le RCS et le n° de TVA sont déjà renseignés."},
        {"id": "abv", "title": "Valider les degrés d'alcool des vins (ou laisser Alix les confirmer à la première commande)", "done": all(r.get("abv") for r in customs_rows()),
         "how": "Page Douane › Vins : les degrés sont pré-remplis (fiches publiques ou estimation). Cochez « Confirmer » pour les vins dont vous connaissez l'étiquette ; pour les autres, l'e-mail « degrés à confirmer » est envoyé à Alix à la première commande internationale."},
        {"id": "scopes", "title": "Accorder à l'application Vaelan les droits Shopify manquants", "done": False,
         "how": "shopify.dev › Dev Dashboard › app Vaelan › Configuration › Access scopes : ajouter write_shipping, read_markets, write_markets, read_locales, write_locales, read_translations, write_translations, read_themes, write_themes, read_legal_policies. Enregistrer puis réinstaller l'app sur la boutique (Home › Install). Vaelan pourra alors écrire la grille de port, publier l'anglais et traduire."},
        {"id": "customs_shopify", "title": "Écrire dans Shopify les codes SH, l'origine et le poids des sélections", "done": False,
         "how": "Page Douane › Shopify › « Écrire dans Shopify » (après les degrés). Les codes 22042113 / 22042143 et l'origine FR figurent sur les documents Shopify ; les sélections passent de 0 kg à 1,5 kg par bouteille (sinon la grille de port au poids est fausse)."},
        {"id": "zones", "title": "Vérifier les zones ouvertes et écrire la grille de port dans Shopify", "done": False,
         "how": "Page Douane › Zones : particuliers en Suisse (6 bouteilles), sociétés UE Ouest (TVA intracommunautaire). Vérifier la marge et la surcharge carburant du mois, puis « Écrire dans Shopify » : les zones UE (22 €) et International (29 €) sont remplacées par des tarifs par carton de 6 (fenêtre de poids ± 0,1 kg : 9 kg, 18 kg…) — un panier de 9 bouteilles ou de 2 cartons en Suisse n'a plus aucun tarif et ne peut pas être payé. Sans write_shipping : reproduire la grille à la main dans Paramètres › Expédition et livraison."},
        {"id": "checkout", "title": "Paiement : téléphone obligatoire, société et adresse ligne 2 affichées", "done": False,
         "how": "Shopify › Paramètres › Paiement › Informations client : « Numéro de téléphone de l'adresse d'expédition : Obligatoire » ; « Nom de l'entreprise : Facultatif » ; « Adresse ligne 2 : Facultatif ». Chronopost exige le téléphone à l'international."},
        {"id": "taxes", "title": "Taxes : exclure la TVA française selon le pays du client", "done": False,
         "how": "Shopify › Paramètres › Taxes et droits › « Inclure ou exclure les taxes selon le pays du client » : activer. Un client suisse ou une société européenne paie alors hors TVA française ; la facture douanière n'a plus de conversion."},
        {"id": "markets", "title": "Marchés : Suisse et Union européenne en euros, pays fermés désactivés", "done": False,
         "how": "Shopify › Paramètres › Marchés : créer « Suisse » (CH) et « Union européenne » (BE LU NL DE IT ES PT AT IE) ; laisser les autres pays inactifs (AE AU CA HK IL JP KR MY NZ SG US GB NO…) : ils n'ont aucun tarif de port et ne peuvent pas commander."},
        {"id": "languages", "title": "Publier l'anglais et traduire la boutique", "done": False,
         "how": "Après les droits Shopify : page Douane › Traduction › « Traduire en anglais » (Vaelan active la langue en, traduit produits, collections, pages et politiques par Claude et enregistre les traductions). Sinon : Paramètres › Langues › Ajouter l'anglais › Publier, puis l'application gratuite Translate & Adapt. Le sélecteur de langue et de pays s'active dans Boutique en ligne › Thème › Personnaliser › En-tête › Localisation."},
        {"id": "snippet", "title": "Installer l'extrait de panier (règles d'expédition, n° de TVA vérifié)", "done": False,
         "how": "Page Douane › Textes du site › télécharger « owine-international.liquid ». Boutique en ligne › Thème › Modifier le code › Snippets › Ajouter un extrait « owine-international », coller le contenu, puis dans sections/main-cart-footer.liquid (ou main-cart-items.liquid) ajouter {% render 'owine-international' %} juste avant le bouton de paiement. Désactiver les boutons de paiement dynamiques sur le panier (Personnaliser › Panier). L'extrait affiche les options par pays, impose 6 bouteilles (Suisse) ou des multiples de 6 (UE) et un n° de TVA vérifié VIES pour les sociétés UE avant d'autoriser le paiement."},
        {"id": "policies", "title": "Politique d'expédition, FAQ et note de paiement", "done": False,
         "how": "Page Douane › Textes du site : copier les textes FR et EN dans Paramètres › Politiques › Politique d'expédition, dans la page FAQ et dans Paramètres › Paiement › Contenu (note « hors UE : droits et taxes à régler au transporteur »)."},
        {"id": "age", "title": "Vérification d'âge à l'entrée du site", "done": False,
         "how": "Application gratuite « Age verification » (Shopify App Store) ou module du thème : fenêtre « Avez-vous l'âge légal pour acheter de l'alcool ? » en français et en anglais."},
        {"id": "chronopost", "title": "Écrire à Corentin Menard (Chronopost)", "done": False,
         "how": "Points à confirmer : Chrono Classic et Chrono Express bien ouverts sur le contrat 84048903 ; supplément douane Suisse 15 € par expédition et prestation de dédouanement export (21 € TTC ?) ; surcharge carburant du mois ; activation du ShippingServiceWS (étiquettes et enlèvement depuis Vaelan) ; fiches pays Norvège, Hong Kong, Singapour ; avenant Chrono Viti B2C US pour plus tard."},
        {"id": "alix", "title": "Alix : régime d'accise du stock et documents pour les sociétés UE", "done": False,
         "how": "Demander à Alix (entrepositaire agréé, n° d'accise FR 107859E0476 à Beaune) si le stock oWine est en droits acquittés ou en suspension, et confirmer qu'Alix émet le document d'accompagnement (DAE ou e-DSA, 15 € HT au tarif) pour chaque expédition à une société de l'UE. Le pôle d'action économique de la douane de Dijon peut confirmer le régime (expéditeur certifié)."},
        {"id": "vat_check", "title": "Sociétés UE : décider la conduite si le n° de TVA n'est pas valide", "done": False,
         "how": "Vaelan vérifie chaque n° sur VIES à la synchronisation ; si invalide ou absent, la commande est bloquée sur la page (contrôle rouge) : demander au client le bon numéro (formulaire douane) ou rembourser. Sur la facture Pennylane : mention « autoliquidation, art. 262 ter I CGI » et n° de TVA du client."},
        {"id": "test", "title": "Commande d'essai en Suisse et commande d'essai société UE", "done": False,
         "how": "Passer une commande test sur le site (adresse suisse, 6 bouteilles ; puis adresse belge avec un n° de TVA valide) pour vérifier de bout en bout : port affiché, paiement, synchronisation Vaelan, carte International, facture commerciale, e-mails. Annuler et rembourser ensuite."},
    ]


def setup_tasks(log=None) -> int:
    """Une tâche Vaelan par action de mise en place (clé setup:<id>) ; les actions détectées comme faites ferment leur tâche."""
    log = log or (lambda m: None); n = 0
    for it in setup_items():
        key = f"setup:{it['id']}"
        if it["done"]:
            service.close_tasks_by_key(key); continue
        if service.add_task("customs_setup", it["title"], ref="export", key=key, details=it["how"]):
            n += 1
    log(f"mise en place export : {n} nouvelle(s) tâche(s)")
    return n


def site_texts() -> dict:
    """Textes FR / EN dérivés des zones ouvertes : options de livraison (panier, FAQ), politique d'expédition, note de paiement."""
    zs = [z for z in zones() if z.get("particulier") or z.get("societe")]
    fr, en = [], []
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
    options_fr = "Nous livrons actuellement :\n" + "\n".join("• " + x for x in fr) + "\nLes autres destinations et les particuliers dans l'Union européenne : prochainement. Sociétés hors Europe : sur devis à orders@owine.co."
    options_en = "We currently ship to:\n" + "\n".join("• " + x for x in en) + "\nOther destinations and private customers in the EU: coming soon. Companies outside Europe: quote on request at orders@owine.co."
    policy_fr = (options_fr + "\n\nSuisse — Vos vins partent de notre entrepôt de Beaune par Chronopost Chrono Classic, livraison en 2 à 4 jours ouvrés après l'enlèvement, à une adresse physique (pas de boîte postale) ; un numéro de téléphone est indispensable. "
                 "La réglementation limite chaque envoi à 6 bouteilles : une commande = un carton de 6. Nos prix s'entendent hors TVA française. Votre commande est livrée « DAP » : la TVA suisse (8,1 %), le droit de douane sur le vin et les frais de dédouanement du transporteur ne sont pas compris dans le prix et vous seront demandés par Chronopost avant la livraison. Vous devez avoir l'âge légal pour acheter de l'alcool.\n\n"
                 "Union européenne (sociétés) — Livraison par Chronopost Chrono Classic en 2 à 4 jours ouvrés selon le pays, par carton de 6. Facture hors TVA sur présentation d'un numéro de TVA intracommunautaire valide (autoliquidation) ; les accises du pays de destination restent dues par l'acheteur selon la réglementation locale.\n\n"
                 "À la livraison — Ouvrez les cartons devant le livreur, notez toute réserve sur le bon de livraison avant de signer, photographiez et écrivez-nous à contact@owine.co : nous prenons le relais auprès de Chronopost.")
    policy_en = (options_en + "\n\nSwitzerland — Your wines leave our Beaune warehouse with Chronopost Chrono Classic, delivered in 2 to 4 working days after collection, to a physical address (no PO box); a phone number is required. "
                 "Regulations limit each shipment to 6 bottles: one order = one case of 6. Our prices exclude French VAT. Your order is delivered “DAP”: Swiss VAT (8.1%), the customs duty on wine and the carrier's clearance fee are not included and will be requested by Chronopost before delivery. You must be of legal drinking age.\n\n"
                 "European Union (companies) — Chronopost Chrono Classic, 2 to 4 working days depending on the country, in cases of 6. Invoice without VAT against a valid EU VAT number (reverse charge); excise duties in the destination country remain payable by the buyer under local rules.\n\n"
                 "On delivery — Open the cases in front of the driver, write any reservation on the delivery note before signing, take photos and e-mail contact@owine.co: we take over with Chronopost.")
    checkout_fr = "Livraison hors Union européenne : les droits de douane, la TVA et les frais de dédouanement de votre pays ne sont pas compris et vous seront demandés par le transporteur avant la livraison (incoterm DAP)."
    checkout_en = "Delivery outside the European Union: import duties, VAT and clearance fees of your country are not included and will be requested by the carrier before delivery (Incoterm DAP)."
    return {"options_fr": options_fr, "options_en": options_en, "policy_fr": policy_fr, "policy_en": policy_en, "checkout_fr": checkout_fr, "checkout_en": checkout_en}


def theme_snippet() -> str:
    """Extrait Liquid + JS pour la page panier : options par pays, règles de bouteilles (max / multiples de 6), n° de TVA vérifié (VIES via Vaelan) pour les sociétés UE, attributs de commande lus par Vaelan."""
    zs = zones(); base = service.admin_url()
    rules = {z["key"]: {"countries": z["countries"], "particulier": bool(z.get("particulier")), "societe": bool(z.get("societe")), "max": z.get("max_bottles"), "multiple": z.get("multiple"), "vat": bool(z.get("vat_required")), "label": z["label"].split(" (")[0]} for z in zs if z.get("particulier") or z.get("societe")}
    t = site_texts()
    return r'''{%- comment -%} oWine — règles d'expédition internationale (généré par Vaelan, page Douane › Textes du site). À rendre dans le panier, avant le bouton de paiement. {%- endcomment -%}
{%- if cart.item_count > 0 -%}
<div id="ow-intl" class="ow-intl" data-country="{{ localization.country.iso_code }}" data-lang="{{ request.locale.iso_code }}" data-items="{{ cart.items | map: 'quantity' | join: ',' }}" data-weight="{{ cart.total_weight }}"
     data-company="{{ cart.attributes['Société'] | escape }}" data-vat="{{ cart.attributes['N° TVA'] | escape }}" style="margin:16px 0;padding:14px 16px;border:1px solid #d9c9c5;border-radius:8px;font-size:.95rem;">
  <div class="ow-intl__options" style="white-space:pre-line;color:#4a0d1f;"></div>
  <div class="ow-intl__company" style="display:none;margin-top:10px;">
    <label style="display:block;margin-bottom:6px;"><input type="checkbox" id="ow-company"> <span data-fr="Je commande pour une société" data-en="I am ordering for a company"></span></label>
    <div id="ow-vat-wrap" style="display:none;">
      <label for="ow-vat" style="display:block;font-size:.9rem;"><span data-fr="N° de TVA intracommunautaire (vérifié automatiquement)" data-en="EU VAT number (checked automatically)"></span></label>
      <input id="ow-vat" type="text" placeholder="BE0123456789 / DE123456789" style="width:100%;max-width:320px;padding:8px;border:1px solid #bbb;border-radius:6px;">
      <div id="ow-vat-msg" style="font-size:.88rem;margin-top:4px;"></div>
    </div>
  </div>
  <div class="ow-intl__msg" style="margin-top:8px;font-weight:600;color:#8c1a1a;"></div>
</div>
<script>
(function(){
  var R = ''' + json.dumps(rules, ensure_ascii=False) + r''';
  var T = ''' + json.dumps({"fr": t["options_fr"], "en": t["options_en"]}, ensure_ascii=False) + r''';
  var VIES = "''' + base + r'''/api/owine/vies?vat=";
  var el = document.getElementById('ow-intl'); if (!el) return;
  var lang = (el.dataset.lang || 'fr').slice(0,2) === 'en' ? 'en' : 'fr', cc = el.dataset.country || 'FR';
  var bottles = Math.round((parseInt(el.dataset.weight || '0', 10) / 1000) / 1.5);   // 1,5 kg par bouteille (sélections comprises)
  var zone = null; Object.keys(R).forEach(function(k){ if (R[k].countries.indexOf(cc) >= 0) zone = R[k]; });
  document.querySelectorAll('#ow-intl [data-fr]').forEach(function(s){ s.textContent = s.dataset[lang]; });
  el.querySelector('.ow-intl__options').textContent = T[lang];
  var msg = el.querySelector('.ow-intl__msg'), companyBox = el.querySelector('.ow-intl__company'), chk = document.getElementById('ow-company'), vatWrap = document.getElementById('ow-vat-wrap'), vat = document.getElementById('ow-vat'), vatMsg = document.getElementById('ow-vat-msg');
  var checkoutBtns = document.querySelectorAll('button[name="checkout"], [name="checkout"], .cart__checkout-button, #checkout');
  function lock(on, text){ msg.textContent = text || ''; checkoutBtns.forEach(function(b){ b.disabled = !!on; b.style.opacity = on ? .5 : 1; }); }
  function setAttr(obj){ return fetch('/cart/update.js', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({attributes: obj})}); }
  var msgs = {
    closed: {fr: "Nous ne livrons pas encore ce pays : livraison prochainement. Choisissez la France, la Suisse (particuliers) ou une société dans l'Union européenne.", en: "We do not ship to this country yet: coming soon. Choose France, Switzerland (private customers) or a company in the European Union."},
    max: {fr: "Suisse : 6 bouteilles maximum par commande (un carton). Retirez des bouteilles pour continuer.", en: "Switzerland: up to 6 bottles per order (one case). Remove bottles to continue."},
    mult: {fr: "Expédition par carton de 6 : ajustez la quantité à un multiple de 6 bouteilles.", en: "Shipped in cases of 6: adjust the quantity to a multiple of 6 bottles."},
    company_only: {fr: "Dans l'Union européenne, nous livrons pour l'instant les sociétés (n° de TVA intracommunautaire). Cochez « je commande pour une société » et indiquez votre numéro.", en: "In the European Union we currently deliver to companies only (EU VAT number). Tick “I am ordering for a company” and enter your number."},
    vat_bad: {fr: "Numéro de TVA non reconnu par VIES : vérifiez-le (format pays + chiffres, sans espaces).", en: "VAT number not recognised by VIES: please check it (country code + digits, no spaces)."},
    vat_ok: {fr: "Numéro de TVA valide : ", en: "Valid VAT number: "},
    vat_wait: {fr: "Vérification VIES…", en: "Checking VIES…"}
  };
  if (cc === 'FR' || cc === 'MC') { lock(false); return; }
  if (!zone) { lock(true, msgs.closed[lang]); return; }
  if (zone.max && bottles > zone.max) { lock(true, msgs.max[lang]); return; }
  if (zone.multiple && bottles % zone.multiple !== 0) { lock(true, msgs.mult[lang]); return; }
  if (zone.societe) {
    companyBox.style.display = 'block';
    chk.checked = (el.dataset.company === 'oui'); vat.value = el.dataset.vat || '';
    function refresh(){
      vatWrap.style.display = chk.checked ? 'block' : 'none';
      if (!chk.checked) { setAttr({'Société': '', 'N° TVA': '', 'TVA vérifiée': ''}); if (!zone.particulier) lock(true, msgs.company_only[lang]); else lock(false); return; }
      setAttr({'Société': 'oui'});
      if (!zone.vat) { lock(false); return; }
      var v = (vat.value || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (v.length < 4) { lock(true, msgs.company_only[lang]); vatMsg.textContent = ''; return; }
      vatMsg.textContent = msgs.vat_wait[lang]; lock(true, '');
      fetch(VIES + encodeURIComponent(v)).then(function(r){ return r.json(); }).then(function(d){
        if (d.valid) { vatMsg.textContent = msgs.vat_ok[lang] + (d.name || v); vatMsg.style.color = '#2e6f4b'; setAttr({'N° TVA': v, 'TVA vérifiée': 'oui ' + (d.name || '')}).then(function(){ lock(false); }); }
        else { vatMsg.textContent = msgs.vat_bad[lang] + (d.error ? ' (' + d.error + ')' : ''); vatMsg.style.color = '#a8261d'; setAttr({'N° TVA': v, 'TVA vérifiée': ''}); lock(true, ''); }
      }).catch(function(){ vatMsg.textContent = msgs.vat_bad[lang]; lock(true, ''); });
    }
    chk.addEventListener('change', refresh); vat.addEventListener('change', refresh); vat.addEventListener('blur', refresh); refresh();
  } else { lock(false); }
})();
</script>
{%- endif -%}
'''


# ================================================================== traduction anglaise de la boutique (Claude → translationsRegister) ; nécessite read/write_translations et write_locales
TRANSLATABLE = [("PRODUCT", ["title", "body_html"]), ("COLLECTION", ["title", "body_html"]), ("ONLINE_STORE_PAGE", ["title", "body_html"]), ("SHOP_POLICY", ["body"])]


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
                if tc["key"] in keys and (tc.get("value") or "").strip() and (tc["key"] not in done or done[tc["key"]].get("outdated")):
                    plan["resources"].append({"type": rtype, "id": n["resourceId"], "key": tc["key"], "digest": tc["digest"], "value": tc["value"]})
    plan["resources"] = plan["resources"][:limit]
    return plan


def _translate_fr_en(texts: List[str]) -> List[str]:
    """Traduction FR → EN par Claude, HTML conservé, vocabulaire du vin (appellations, climats, cépages, millésimes inchangés)."""
    from app.core import assistant
    if not assistant.configured():
        raise RuntimeError("clé Anthropic absente (ANTHROPIC_API_KEY)")
    import anthropic
    client = anthropic.Anthropic()
    out = []
    system = ("You translate French e-commerce content for oWine, a Burgundy wine merchant in Dijon, into natural British English for wine lovers. Keep HTML tags and structure exactly, keep proper nouns, appellations, climats, "
              "producers, vintages and wine terms (Premier Cru, Grand Cru, climat, lieu-dit) untranslated, keep prices and units. Return only the translation, nothing else.")
    for t in texts:
        r = client.messages.create(model="claude-sonnet-5", max_tokens=4000, system=system, messages=[{"role": "user", "content": t}])
        out.append("".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip())
    return out


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
