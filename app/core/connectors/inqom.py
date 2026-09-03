"""Connecteur Inqom (« Fred Api ») — OAuth2 password grant, lecture des dossiers comptables.

Auth : POST https://auth.inqom.com/identity/connect/token, grant_type=password avec
ClientID + ClientSecret (l'application) et User + Password (l'utilisateur cabinet),
scope « apidata ». Le token (Bearer) est mis en cache et renouvelé avant expiration.

Clés en variables d'environnement (clé = groupe/cabinet, assainie comme les autres) :
  INQOM_<KEY>_CLIENT_ID / INQOM_<KEY>_CLIENT_SECRET / INQOM_<KEY>_USER / INQOM_<KEY>_PASSWORD

API : https://api.inqom.com (spec : https://api.inqom.com/swagger/v1/swagger.json).
Vaelan reste en LECTURE (GET) sauf action explicitement demandée.
"""
import os
import re
import time
from typing import Optional

import httpx

AUTH_URL = "https://auth.inqom.com/identity/connect/token"
BASE_URL = "https://api.inqom.com"
SCOPE = "apidata offline_access"


def _env_key(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(code).upper())


class InqomClient:
    def __init__(self, client_id, client_secret, user, password):
        self._cid = client_id
        self._csec = client_secret
        self._user = user
        self._pwd = password
        self._token = None
        self._exp = 0.0

    # ---- auth ----
    def _authenticate(self):
        data = {"grant_type": "password", "client_id": self._cid, "client_secret": self._csec,
                "username": self._user, "password": self._pwd, "scope": SCOPE}
        with httpx.Client(timeout=30) as c:
            r = c.post(AUTH_URL, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"auth Inqom refusée (HTTP {r.status_code}) : {r.text[:120]}")
        d = r.json()
        self._token = d["access_token"]
        self._exp = time.time() + int(d.get("expires_in") or 3600) - 60   # marge 1 min
        return d

    def token(self) -> str:
        if not self._token or time.time() >= self._exp:
            self._authenticate()
        return self._token

    # ---- HTTP ----
    def get(self, path: str, **params):
        h = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
        for attempt in range(5):
            with httpx.Client(timeout=60) as c:
                r = c.get(BASE_URL + path, headers=h, params=params or None)
            if r.status_code == 401 and attempt == 0:   # token invalidé côté serveur -> re-auth
                self._token = None
                h["Authorization"] = f"Bearer {self.token()}"
                continue
            if r.status_code == 429 and attempt < 4:
                time.sleep(float(r.headers.get("Retry-After") or 0) or 1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            if not r.content:
                return None
            return r.json()
        return None

    # ---- helpers ----
    def folders(self):
        """Dossiers comptables accessibles au compte (GET /api/app/companies/accounting-folders)."""
        return self.get("/api/app/companies/accounting-folders")

    def health(self) -> dict:
        try:
            self._authenticate()
            return {"ok": True, "expires_in_s": int(self._exp - time.time())}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}


def for_key(key: str) -> Optional[InqomClient]:
    k = _env_key(key)
    cid = os.getenv(f"INQOM_{k}_CLIENT_ID")
    csec = os.getenv(f"INQOM_{k}_CLIENT_SECRET")
    user = os.getenv(f"INQOM_{k}_USER")
    pwd = os.getenv(f"INQOM_{k}_PASSWORD")
    if not (cid and csec and user and pwd):
        return None
    return InqomClient(cid, csec, user, pwd)
