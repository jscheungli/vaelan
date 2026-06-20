"""Connecteur TopOrder (une clé par établissement).

Une variable d'environnement par établissement, dérivée de son nom :
  "OCOPAIN SAINT-LEU"  -> TOPORDER_OCOPAIN_SAINT_LEU_KEY
  "KOOKABURA"          -> TOPORDER_KOOKABURA_KEY
Auth = header Authorization brut (sans Bearer).
"""
import os
import re
import unicodedata
from typing import Optional
import httpx

DEFAULT_BASEURL = "https://publicproxy.toporder.fr"


def env_var_for(name: str) -> str:
    """Nom de la variable d'environnement portant la clé de cet établissement."""
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_").upper()
    slug = re.sub(r"_+", "_", slug)
    return f"TOPORDER_{slug}_KEY"


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
    key = os.getenv(env_var_for(name))
    if not key:
        return None
    return TopOrderClient(key, os.getenv("TOPORDER_BASEURL", DEFAULT_BASEURL))
