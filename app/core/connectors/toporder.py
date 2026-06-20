"""Connecteur TopOrder (une clé par établissement).

Les clés viennent de la variable TOPORDER_KEYS au format
"NOM_ETAB=cle,NOM_ETAB2=cle2". Auth = header Authorization brut (sans Bearer).
"""
import os
from typing import Dict, Optional
import httpx

DEFAULT_BASEURL = "https://publicproxy.toporder.fr"


def _parse_keys() -> Dict[str, str]:
    raw = os.getenv("TOPORDER_KEYS", "")
    out: Dict[str, str] = {}
    for part in raw.split(","):
        if "=" in part:
            name, key = part.split("=", 1)
            out[name.strip()] = key.strip()
    return out


class TopOrderClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASEURL):
        self.base_url = base_url.rstrip("/")
        self._h = {"Authorization": api_key, "Accept": "application/json"}

    def get(self, path: str, **params):
        with httpx.Client(timeout=60) as c:
            r = c.get(self.base_url + path, headers=self._h, params=params)
            r.raise_for_status()
            return r.json()


def for_establishment(name: str) -> Optional[TopOrderClient]:
    key = _parse_keys().get(name)
    if not key:
        return None
    return TopOrderClient(key, os.getenv("TOPORDER_BASEURL", DEFAULT_BASEURL))
