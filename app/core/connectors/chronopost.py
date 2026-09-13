"""Connecteur Chronopost — web services SOAP (expédition / étiquettes, devis et offres du contrat, suivi, annulation,
points relais). Un contrat par société.

Clés : CHRONOPOST_<CODE>_ACCOUNT (n° de contrat à 8 chiffres), CHRONOPOST_<CODE>_PASSWORD (mot de passe Chronopost,
lié au contrat), CHRONOPOST_<CODE>_SUBACCOUNT (facultatif) — variables Render, ou section "chronopost" de
~/.config/vaelan/credentials.json (localenv). Compte de test public documenté par Chronopost : 19869502 / 255562
(étiquettes « SPECIMEN », jamais facturées)."""
import base64
import os
import re
import html
from datetime import date, datetime
from typing import Dict, List, Optional

import httpx

WS = {
    "shipping": ("https://ws.chronopost.fr/shipping-cxf/ShippingServiceWS", "http://cxf.shipping.soap.chronopost.fr/"),
    "quickcost": ("https://ws.chronopost.fr/quickcost-cxf/QuickcostServiceWS", "http://cxf.quickcost.soap.chronopost.fr/"),
    "tracking": ("https://ws.chronopost.fr/tracking-cxf/TrackingServiceWS", "http://cxf.tracking.soap.chronopost.fr/"),
    "relais": ("https://ws.chronopost.fr/recherchebt-ws-cxf/PointRelaisServiceWS", "http://cxf.rechercheBt.soap.chronopost.fr/"),
}
TEST_ACCOUNT, TEST_PASSWORD = "19869502", "255562"

# Codes produit Chronopost (guide Web Services / modules officiels). Les codes absents ici sont affichés tels quels.
PRODUCT_LABELS = {
    "1": "Chrono 13", "01": "Chrono 13", "2": "Chrono 10", "02": "Chrono 10", "16": "Chrono 18", "86": "Chrono Relais",
    "44": "Chrono Classic (Europe)", "17": "Chrono Express (international)", "4I": "Chrono Sameday", "5X": "Chrono 2Shop",
    "49": "Chrono Relais Europe", "3P": "Chrono Relais DOM", "0": "Chrono 13 (pré-affranchi)", "2R": "Chronofresh",
    "1V": "Chrono Viti (à confirmer)", "1O": "Chrono 13 Relais Pickup (à confirmer)", "1S": "Chrono 13 Samedi (à confirmer)",
}


class ChronopostError(RuntimeError):
    pass


def _esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=False)


def _tags(fields) -> str:
    return "".join(f"<{k}>{_esc(v)}</{k}>" for k, v in fields if v is not None)


def _find(t: str, tag: str) -> List[str]:
    return [html.unescape(x) for x in re.findall(rf"<{tag}>(.*?)</{tag}>", t, re.S)]


def _first(t: str, tag: str, default: str = "") -> str:
    f = _find(t, tag)
    return f[0] if f else default


class ChronopostClient:
    def __init__(self, account: str, password: str, sub_account: str = ""):
        self.account, self.password, self.sub_account = str(account).strip(), password, (sub_account or "").strip()

    # ------------------------------------------------------------- transport SOAP
    def call(self, service: str, op: str, body_xml: str) -> str:
        url, ns = WS[service]
        env = (f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cxf="{ns}">'
               f'<soapenv:Header/><soapenv:Body><cxf:{op}>{body_xml}</cxf:{op}></soapenv:Body></soapenv:Envelope>')
        try:
            r = httpx.post(url, content=env.encode("utf-8"), headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""}, timeout=90)
        except Exception as e:
            raise ChronopostError(f"Chronopost injoignable : {type(e).__name__}")
        if r.status_code >= 500 and "<faultstring>" in r.text:
            raise ChronopostError("SOAP fault : " + _first(r.text, "faultstring")[:200])
        if r.status_code >= 400:
            raise ChronopostError(f"HTTP {r.status_code}")
        t = r.text
        code = _first(t, "errorCode", "0")
        if code not in ("0", ""):
            raise ChronopostError(f"erreur Chronopost {code} : {_first(t, 'errorMessage')[:200]}")
        return t

    # ------------------------------------------------------------- offres et devis
    def products(self, dep_zip: str, arr_zip: str, arr_city: str = "", arr_country: str = "FR", weight: float = 3.0,
                 dims=(40, 30, 20), ship_date: date = None) -> List[str]:
        """Codes produit disponibles sur le contrat pour ce trajet (contrôle d'identifiants sans étiquette)."""
        d = (ship_date or date.today()).strftime("%d/%m/%Y")
        t = self.call("quickcost", "getProducts", _tags([("accountNumber", self.account), ("password", self.password), ("depCountryCode", "FR"), ("depZipCode", dep_zip),
                                                          ("arrCountryCode", arr_country), ("arrZipCode", arr_zip), ("arrCity", arr_city), ("type", "M"), ("weight", weight),
                                                          ("height", dims[2]), ("length", dims[0]), ("width", dims[1]), ("shippingDate", d)]))
        return _find(t, "productCode")

    def quickcost(self, dep_zip: str, arr_zip: str, weight: float, product_code: str) -> dict:
        t = self.call("quickcost", "quickCostV3", _tags([("accountNumber", self.account), ("password", self.password), ("depCode", dep_zip), ("arrCode", arr_zip),
                                                          ("weight", weight), ("productCode", product_code), ("type", "M")]))
        ret = t.split("<return>", 1)[-1]
        services = [{"code": _first(s, "codeService"), "label": _first(s, "label"), "ht": float(_first(s, "amount", "0") or 0)} for s in _find(ret, "service")]
        return {"ht": float(_first(ret.split("<service>")[0], "amount", "0") or 0), "ttc": float(_first(ret.split("<service>")[0], "amountTTC", "0") or 0), "services": services}

    # ------------------------------------------------------------- étiquettes
    def create_label(self, shipper: dict, recipient: dict, parcels: List[dict], product_code: str = "1", ref: str = "",
                     mode: str = "PDF", ship_date: datetime = None, service: str = "0", content: str = "Vin") -> dict:
        """Une expédition (1..n colis) → numéros de colis + étiquette(s) (PDF ou ZPL, base64 décodé).
        shipper / recipient : {name, name2, address1, address2, zip, city, country ('FR'), phone, mobile, email, contact, civility}.
        parcels : [{weight (kg), length, width, height (cm), content}]. product_code : voir PRODUCT_LABELS."""
        d = ship_date or datetime.now()
        product_code = str(product_code).strip().zfill(2)          # le service d'expédition exige un code sur 2 caractères (« 01 »)
        def party(prefix, p, extra=""):
            f = [(f"{prefix}Adress1", p.get("address1")), (f"{prefix}Adress2", p.get("address2", "")), (f"{prefix}City", p.get("city")),
                 (f"{prefix}Civility", p.get("civility", "M")), (f"{prefix}ContactName", p.get("contact") or p.get("name")), (f"{prefix}Country", p.get("country", "FR")),
                 (f"{prefix}CountryName", p.get("country_name", "FRANCE")), (f"{prefix}Email", p.get("email", "")), (f"{prefix}MobilePhone", p.get("mobile") or p.get("phone", "")),
                 (f"{prefix}Name", p.get("name")), (f"{prefix}Name2", p.get("name2", "")), (f"{prefix}Phone", p.get("phone") or p.get("mobile", "")),
                 (f"{prefix}PreAlert", "0"), (f"{prefix}ZipCode", p.get("zip"))]
            if prefix == "recipient":
                f = [x for x in f if x[0] != "recipientCivility"] + [("recipientType", p.get("type", "1"))]
            return f"<{prefix}Value>{_tags(f)}{extra}</{prefix}Value>"
        header = f"<headerValue>{_tags([('accountNumber', self.account), ('idEmet', 'FR'), ('identWebPro', ''), ('subAccount', self.sub_account)])}</headerValue>"
        customer = party("customer", shipper, "<printAsSender>N</printAsSender>")
        refs = f"<refValue>{_tags([('customerSkybillNumber', ref), ('recipientRef', ref), ('shipperRef', ref)])}</refValue>"
        sky = ""
        for i, pc in enumerate(parcels, 1):
            sky += "<skybillValue>" + _tags([("bulkNumber", len(parcels)), ("codCurrency", "EUR"), ("codValue", 0), ("content1", pc.get("content", content)),
                                              ("customsCurrency", "EUR"), ("customsValue", 0), ("evtCode", "DC"), ("height", pc.get("height", 0)), ("insuredCurrency", "EUR"),
                                              ("insuredValue", 0), ("length", pc.get("length", 0)), ("objectType", "MAR"), ("productCode", product_code), ("service", service),
                                              ("shipDate", d.strftime("%Y-%m-%dT%H:%M:%S")), ("shipHour", d.hour), ("weight", pc.get("weight", 1)), ("weightUnit", "KGM"),
                                              ("width", pc.get("width", 0)), ("skybillRank", i)]) + "</skybillValue>"
        params = f"<skybillParamsValue>{_tags([('mode', mode), ('withReservation', 0)])}</skybillParamsValue>"
        body = (header + party("shipper", shipper) + customer + party("recipient", recipient) + refs + sky + params +
                _tags([("password", self.password), ("modeRetour", "2"), ("numberOfParcel", len(parcels)), ("version", "2.0"), ("multiParcel", "Y" if len(parcels) > 1 else "N")]))
        t = self.call("shipping", "shippingMultiParcelV4", body)
        numbers = _find(t, "skybillNumber")
        labels = [base64.b64decode(x) for x in _find(t, "pdfEtiquette")] if mode == "PDF" else [base64.b64decode(x) for x in _find(t, "pdfEtiquette")]
        return {"numbers": numbers, "labels": labels, "mode": mode, "reservation": _first(t, "reservationNumber")}

    def cancel(self, skybill: str) -> bool:
        t = self.call("tracking", "cancelSkybill", _tags([("accountNumber", self.account), ("password", self.password), ("language", "fr_FR"), ("skybillNumber", skybill)]))
        return _first(t, "errorCode", "0") == "0"

    # ------------------------------------------------------------- suivi
    def track(self, skybill: str) -> dict:
        """Événements de suivi d'un colis (Chronotrace)."""
        t = self.call("tracking", "trackSkybillV2", _tags([("language", "fr_FR"), ("skybillNumber", skybill)]))
        events = []
        for ev in _find(t, "events"):
            events.append({"code": _first(ev, "code"), "date": _first(ev, "eventDate"), "label": _first(ev, "eventLabel"), "office": _first(ev, "officeLabel"), "zip": _first(ev, "zipCode")})
        return {"number": skybill, "events": events, "status": events[-1]["label"] if events else ""}

    # ------------------------------------------------------------- points relais
    def relais(self, zip_code: str, city: str = "", country: str = "FR", product_code: str = "86", max_points: int = 10, ship_date: date = None) -> List[dict]:
        d = (ship_date or date.today()).strftime("%d/%m/%Y")
        t = self.call("relais", "recherchePointChronopostInter", _tags([("accountNumber", self.account), ("password", self.password), ("address", ""), ("zipCode", zip_code), ("city", city),
                                                                        ("countryCode", country), ("type", "P"), ("productCode", product_code), ("service", "L"), ("weight", 3000),
                                                                        ("shippingDate", d), ("maxPointChronopost", max_points), ("maxDistanceSearch", 20), ("holidayTolerant", 1), ("language", "FR")]))
        out = []
        for p in _find(t, "listePointRelais"):
            out.append({"id": _first(p, "identifiant"), "name": _first(p, "nom"), "address": _first(p, "adresse1"), "zip": _first(p, "codePostal"), "city": _first(p, "localite"), "distance_m": _first(p, "distanceEnMetre")})
        return out

    def health(self) -> dict:
        try:
            codes = self.products("21000", "75001", "PARIS")
            return {"ok": True, "products": codes}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}


def _env_key(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", code.upper())


def for_company(code: str) -> Optional[ChronopostClient]:
    k = _env_key(code)
    acc, pwd = os.getenv(f"CHRONOPOST_{k}_ACCOUNT"), os.getenv(f"CHRONOPOST_{k}_PASSWORD")
    if not (acc and pwd):
        return None
    return ChronopostClient(acc, pwd, os.getenv(f"CHRONOPOST_{k}_SUBACCOUNT") or "")


def test_client() -> ChronopostClient:
    return ChronopostClient(TEST_ACCOUNT, TEST_PASSWORD)
