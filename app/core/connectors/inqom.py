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
import ijson

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

    def search_entries_stream(self, enterprise_id, journal_ids):
        """Écritures d'un ou plusieurs journaux, en STREAMING (générateur).

        search-multiple renvoie TOUJOURS toutes les écritures avec leurs lignes — aucun
        filtre de dates, exercice ou pagination n'est honoré côté serveur (vérifié le
        04/09/2026). Sur un gros journal (bancaire : >50 000 écritures) la réponse
        parsée d'un bloc sature la RAM (OOM Render 512 Mo) : ici on parse objet par
        objet (ijson) et l'appelant ne garde que ce qui l'intéresse."""
        path = f"/api/app/enterprises/{enterprise_id}/entries/search-multiple"
        body = {"Search": {"JournalIds": list(journal_ids)},
                "Output": {"WithEntryRef": True, "WithEntryLabel": True, "WithDocDate": True}}
        for attempt in range(5):
            h = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
                 "Accept": "application/json"}
            with httpx.Client(timeout=httpx.Timeout(300, connect=30)) as c:
                with c.stream("POST", BASE_URL + path, headers=h, json=body) as r:
                    if r.status_code == 401 and attempt == 0:
                        self._token = None
                        continue
                    if r.status_code == 429 and attempt < 4:
                        time.sleep(float(r.headers.get("Retry-After") or 0) or 1.5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    yield from ijson.items(_IterReader(r.iter_bytes()), "item")
                    return

    def entries_by_ids(self, enterprise_id, entry_ids):
        """Écritures précises AVEC leurs lignes (POST entries/search-multiple par EntryIds).
        À appeler par LOTS restreints : c'est le complément « lourd » d'une première
        passe en métadonnées seules (search_entries with_lines=False)."""
        return self.post_json(f"/api/app/enterprises/{enterprise_id}/entries/search-multiple",
                              {"Search": {"EntryIds": list(entry_ids)},
                               "Output": {"WithEntryRef": True, "WithEntryLabel": True,
                                          "WithDocDate": True}}) or []

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


class _IterReader:
    """Adaptateur file-like (read) sur un itérateur de bytes, pour ijson."""
    def __init__(self, it):
        self._it = it
        self._buf = b""

    def read(self, n=-1):
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


def for_key(key: str) -> Optional[InqomClient]:
    k = _env_key(key)
    cid = os.getenv(f"INQOM_{k}_CLIENT_ID")
    csec = os.getenv(f"INQOM_{k}_CLIENT_SECRET")
    user = os.getenv(f"INQOM_{k}_USER")
    pwd = os.getenv(f"INQOM_{k}_PASSWORD")
    if not (cid and csec and user and pwd):
        return None
    return InqomClient(cid, csec, user, pwd)
