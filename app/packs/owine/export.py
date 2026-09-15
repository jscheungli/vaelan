"""OWINE — export et international : zone de destination, règles Chrono Viti par pays, état des formalités (contrôles automatiques
et étapes cochées), calcul des valeurs hors taxes, données de la facture commerciale, formulaire public « informations douanières »,
réglages de l'exportateur (EORI, signature), données douanières des vins et écriture des codes SH dans Shopify.

Zones : FR (territoire fiscal français : rien de particulier) · UE (lettre de transport seule ; TVA du pays de destination et accises à
traiter) · EXPORT (hors UE et DROM-COM : facture commerciale en 3 exemplaires sur le colis A, EORI, incoterm DAP, déclaration d'origine)."""
import json
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
    return {"sku": it.sku, "title": it.title, "colour": colour, "colour_saved": bool(it.couleur), "abv": it.abv, "volume_cl": it.volume_cl or 75, "hs": hs, "hs_saved": bool(saved_hs and saved_hs != X.HS_GENERIC),
            "origin": (it.origin or X.ORIGIN_DEFAULT).upper(), "vintage": it.millesime, "vigneron": it.vigneron, "appellation": it.appellation, "missing": missing, "kind": it.kind, "status": it.status}


def customs_rows(active_only: bool = True) -> List[dict]:
    rows = [customs_row(it) for it in service.items("wine") if not active_only or it.status == "ACTIVE"]
    rows.sort(key=lambda r: (0 if r["missing"] else 1, r["title"] or ""))
    return rows


def save_customs_rows(form: dict) -> int:
    """Formulaire de masse de la page Douane : champs abv_<sku>, colour_<sku>, hs_<sku>, origin_<sku>, vol_<sku>."""
    n = 0
    skus = {k.split("_", 1)[1] for k in form if k.startswith(("abv_", "colour_", "hs_", "origin_", "vol_"))}
    for sku in skus:
        fields = {}
        v = (form.get(f"abv_{sku}") or "").replace(",", ".").strip()
        fields["abv"] = float(v) if v else None
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
    add("cartons", "Cartons validés" + (f" · {len(ships)} envois séparés (limite {((r or {}).get('b2b' if k == 'societe' else 'b2c') or {}).get('max_bottles')} bouteilles / envoi)" if len(ships) > 1 else ""), ok=bool(cs),
        detail=f"{len(cs)} carton(s), {tot['bottles']} bouteille(s), {tot['gross_kg']} kg brut" if cs else "ventiler la commande")
    lim = ((r or {}).get("b2b" if k == "societe" else "b2c") or {})
    if cs and lim.get("max_bottles") and p == "classic":
        bad = [x.ref for x in cs if sum(int(l["qty"]) for l in service.carton_lines(x)) > lim["max_bottles"]]
        add("limit", f"Chaque envoi ≤ {lim['max_bottles']} bouteilles" + (f" et ≤ {lim['max_kg']} kg" if lim.get("max_kg") else ""), ok=not bad, detail=("cartons trop lourds : " + ", ".join(bad)) if bad else "respecté")
    add("labels", "Étiquettes Chronopost (n° de colis) sur tous les cartons", ok=bool(cs) and all(x.tracking for x in cs), detail="saisir le produit " + X.PRODUCTS[p]["label"] + " sur chronopost.fr ; les numéros figurent sur la facture")
    if z == "EXPORT":
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
