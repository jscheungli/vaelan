"""Connecteur Lodgify (API publique v2) — lecture des réservations d'une propriété.

Clé : variable LODGIFY_<CODE>_APIKEY (Render) ou section "lodgify" de credentials.json (local).
Doc : https://docs.lodgify.com — en-tête `X-ApiKey`. Endpoints utilisés :
  GET /v2/reservations/bookings?page=&size=&stayFilter=Upcoming|Current|Historic|All
  GET /v2/reservations/bookings/{id}
  GET /v2/properties
Les champs sont lus de façon tolérante (get) : la structure exacte sera confirmée à la première
synchro réelle (cf. artefact JSON brut de la tâche « Synchro Lodgify »).
"""
import os
import re
from typing import Optional

import httpx

BASE = "https://api.lodgify.com"


def _env_key(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(code).upper())


class LodgifyClient:
    def __init__(self, api_key: str):
        self._h = {"X-ApiKey": api_key, "Accept": "application/json"}

    def get(self, path: str, params: dict = None):
        with httpx.Client(timeout=60) as c:
            r = c.get(BASE + path, headers=self._h, params=params or {})
        r.raise_for_status()
        return r.json()

    def health(self) -> dict:
        try:
            props = self.get("/v2/properties", {"page": 1, "size": 5})
            items = props.get("items") if isinstance(props, dict) else props
            names = [p.get("name") for p in (items or []) if isinstance(p, dict)]
            return {"ok": True, "properties": names}
        except httpx.HTTPStatusError as e:
            return {"ok": False, "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def bookings(self, stay: str = "Upcoming", size: int = 50, max_pages: int = 20):
        """Toutes les réservations du filtre de séjour (Upcoming / Current / Historic / All)."""
        out = []
        for page in range(1, max_pages + 1):
            d = self.get("/v2/reservations/bookings",
                         {"page": page, "size": size, "stayFilter": stay, "includeCount": "true",
                          "includeExternal": "true"})
            items = d.get("items") if isinstance(d, dict) else d
            if not items:
                break
            out += items
            if len(items) < size:
                break
        return out

    def booking(self, booking_id: int):
        return self.get(f"/v2/reservations/bookings/{booking_id}")


# noms alternatifs de variable (ex. clé posée sur Render sous le nom long de la société)
_ALIASES = {"VDS": ["LESSABLESDULAGON", "SABLESDULAGON"]}


def for_company(code: str) -> Optional[LodgifyClient]:
    for k in [_env_key(code)] + _ALIASES.get(code, []):
        key = os.getenv(f"LODGIFY_{k}_APIKEY")
        if key:
            return LodgifyClient(key)
    return None


# ---- normalisation d'une réservation Lodgify -> dict Vaelan ----
def channel_of(b: dict) -> str:
    src = " ".join(str(b.get(k) or "") for k in ("source", "source_text", "sourceText", "channel")).lower()
    if "airbnb" in src:
        return "airbnb"
    if "booking" in src:
        return "booking"
    if "homeaway" in src or "abritel" in src or "vrbo" in src:
        return "abritel"
    return "lodgify"           # site direct (OH = own home page) / manuel / autres


def booking_ref_of(b: dict) -> Optional[str]:
    """Référence « connue du voyageur » : code Airbnb HM…, n° Booking.com, sinon B<id Lodgify>."""
    src, txt = str(b.get("source") or ""), str(b.get("source_text") or "")
    if src == "AirbnbIntegration":
        try:
            import json as _j
            code = (_j.loads(txt) or {}).get("confirmationCode")
            if code:
                return code
        except Exception:
            pass
    elif src == "BookingCom" and txt.split("|")[0].strip().isdigit():
        return txt.split("|")[0].strip()
    return f"B{b.get('id')}" if b.get("id") else None


def normalize(b: dict) -> dict:
    g = b.get("guest") or {}
    if isinstance(g, list):
        g = g[0] if g else {}
    name = g.get("name") or " ".join(x for x in [g.get("first_name"), g.get("last_name")] if x) or None
    people = 0
    for r in (b.get("rooms") or []):
        try:
            people += int(r.get("people") or 0)
        except Exception:
            pass
    if not people:
        try:
            people = int(b.get("total_guests") or b.get("people") or 0) or None
        except Exception:
            people = None
    lang = (b.get("language") or g.get("language") or "fr")
    return {
        "lodgify_id": b.get("id"),
        "channel": channel_of(b),
        "booking_ref": booking_ref_of(b),
        "guest_name": name,
        "guest_email": g.get("email"),
        "guest_phone": g.get("phone") or g.get("phone_number"),
        "arrival": (b.get("arrival") or "")[:10] or None,
        "departure": (b.get("departure") or "")[:10] or None,
        "guests": people,
        "status": str(b.get("status") or ""),
        "lang": "en" if str(lang).lower().startswith("en") else "fr",
        "property_id": b.get("property_id"),
        "notes": b.get("notes"),
        "created_at": b.get("created_at"),
        "canceled": bool(b.get("canceled_at") or b.get("is_deleted")),
    }
