"""Connecteur Pennylane (une clé par société, en variable d'environnement)."""
import os
import time
from typing import Optional
import httpx

DEFAULT_BASEURL = "https://app.pennylane.com/api/external/v2"


class PennylaneClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASEURL):
        self.base_url = base_url.rstrip("/")
        self._h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(self, path: str, **params):
        # Retry sur 429 (rate limit) : la vérif fait beaucoup d'appels rapprochés
        # (N+1 sur les lignes d'écriture). On respecte Retry-After + backoff progressif.
        for attempt in range(6):
            with httpx.Client(timeout=60) as c:
                r = c.get(self.base_url + path, headers=self._h, params=params)
            if r.status_code == 429 and attempt < 5:
                wait = float(r.headers.get("Retry-After") or 0) or (1.5 * (attempt + 1))
                time.sleep(min(wait, 10))
                continue
            r.raise_for_status()
            return r.json()

    def ledger_entries(self, journal_id, date_from, date_to):
        """Liste les écritures d'un journal sur une période (sans les lignes)."""
        import json
        flt = json.dumps([
            {"field": "journal_id", "operator": "eq", "value": journal_id},
            {"field": "date", "operator": "gteq", "value": date_from},
            {"field": "date", "operator": "lteq", "value": date_to},
        ])
        out, cur = [], None
        while True:
            params = {"filter": flt, "limit": 100}
            if cur:
                params["cursor"] = cur
            d = self.get("/ledger_entries", **params)
            out += d.get("items") or []
            if not d.get("has_more"):
                break
            cur = d.get("next_cursor")
        return out

    def entry_lines(self, entry_id):
        """Lignes détaillées d'une écriture (debit/credit + ledger_account.number)."""
        return self.get(f"/ledger_entries/{entry_id}").get("ledger_entry_lines") or []

    def upload_attachment(self, filename: str, data: bytes) -> Optional[int]:
        """Téléverse un fichier (PDF) comme pièce → renvoie l'id du file_attachment.
        En v2, la pièce est une ressource autonome (POST /file_attachments)."""
        for attempt in range(6):
            with httpx.Client(timeout=90) as c:
                r = c.post(self.base_url + "/file_attachments", headers=self._h,
                           files={"file": (filename, data, "application/pdf")})
            if r.status_code == 429 and attempt < 5:
                wait = float(r.headers.get("Retry-After") or 0) or (1.5 * (attempt + 1))
                time.sleep(min(wait, 10))
                continue
            r.raise_for_status()
            d = r.json()
            return d.get("id") if isinstance(d, dict) else None
        return None

    def attach_to_entry(self, entry_id, file_attachment_id) -> bool:
        """Rattache une pièce déjà téléversée à une écriture EXISTANTE
        (PUT /ledger_entries/{id} ; file_attachment_id remplace ledger_attachment_id)."""
        import json
        body = json.dumps({"file_attachment_id": file_attachment_id}).encode()
        h = {**self._h, "Content-Type": "application/json"}
        for attempt in range(6):
            with httpx.Client(timeout=90) as c:
                r = c.put(f"{self.base_url}/ledger_entries/{entry_id}", headers=h, content=body)
            if r.status_code == 429 and attempt < 5:
                wait = float(r.headers.get("Retry-After") or 0) or (1.5 * (attempt + 1))
                time.sleep(min(wait, 10))
                continue
            r.raise_for_status()
            return True
        return False

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
