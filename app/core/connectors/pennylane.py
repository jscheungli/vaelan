"""Connecteur Pennylane (une clé par société, en variable d'environnement)."""
import os
from typing import Optional
import httpx

DEFAULT_BASEURL = "https://app.pennylane.com/api/external/v2"


class PennylaneClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASEURL):
        self.base_url = base_url.rstrip("/")
        self._h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(self, path: str, **params):
        with httpx.Client(timeout=60) as c:
            r = c.get(self.base_url + path, headers=self._h, params=params)
            r.raise_for_status()
            return r.json()

    def health(self) -> dict:
        """Vérifie que le token répond (lecture d'une ressource légère)."""
        try:
            self.get("/journals", limit=1)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}


def for_company(code: str) -> Optional[PennylaneClient]:
    token = os.getenv(f"PENNYLANE_{code.upper()}_TOKEN")
    if not token:
        return None
    base = os.getenv(f"PENNYLANE_{code.upper()}_BASEURL", DEFAULT_BASEURL)
    return PennylaneClient(token, base)
