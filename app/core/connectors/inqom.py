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

    def post_json(self, path: str, body: dict):
        h = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
             "Accept": "application/json"}
        for attempt in range(5):
            with httpx.Client(timeout=120) as c:
                r = c.post(BASE_URL + path, headers=h, json=body)
            if r.status_code == 401 and attempt == 0:
                self._token = None
                h["Authorization"] = f"Bearer {self.token()}"
                continue
            if r.status_code == 429 and attempt < 4:
                time.sleep(float(r.headers.get("Retry-After") or 0) or 1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json() if r.content else None
        return None

    # ---- helpers ----
    def search_entries(self, enterprise_id, journal_ids, with_lines=True):
        """Écritures d'un ou plusieurs journaux (POST entries/search-multiple)."""
        out = {"WithEntryRef": True, "WithEntryLabel": True, "WithDocDate": True}
        if not with_lines:
            out["WithMetadataOnly"] = True
        return self.post_json(f"/api/app/enterprises/{enterprise_id}/entries/search-multiple",
                              {"Search": {"JournalIds": list(journal_ids)}, "Output": out}) or []

    def journals(self, enterprise_id):
        return self.get(f"/api/app/enterprises/{enterprise_id}/journals") or []

    def file_info(self, enterprise_id, file_id, public=True):
        return self.get(f"/api/app/enterprises/{enterprise_id}/Files/{file_id}",
                        withPublicUrl="true" if public else "false")

    def download_file(self, enterprise_id, file_id):
        """Télécharge le document accroché -> (nom, bytes) ou (None, None)."""
        info = self.file_info(enterprise_id, file_id) or {}
        url = info.get("FileUrl")
        if not url:
            return None, None
        with httpx.Client(timeout=120) as c:
            r = c.get(url)
        if r.status_code != 200 or not r.content:
            return None, None
        data = r.content
        # RÉPARATION : certains fichiers Inqom (connecteur ZEOP) sont des réponses HTTP
        # BRUTES (en-têtes réseau + PDF). Pennylane les refuse (422) -> on extrait le PDF.
        if data[:5] == b"HTTP/":
            i = data.find(b"%PDF")
            if i > 0:
                data = data[i:]
        return (info.get("Name") or f"inqom_{file_id}.pdf"), data

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
