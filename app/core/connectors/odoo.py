"""Connecteur Odoo (XML-RPC standard, une instance par société).

Clés en variables d'environnement (comme Pennylane, code société assaini) :
  ODOO_<CODE>_URL     ex. https://lacorp.odoo.com
  ODOO_<CODE>_DB      nom de la base Odoo
  ODOO_<CODE>_LOGIN   email de l'utilisateur d'intégration
  ODOO_<CODE>_APIKEY  clé API (Paramètres > Sécurité > Clés API dans Odoo)

Lecture seule dans Vaelan : search_read / fields_get uniquement.
"""
import os
import re
import xmlrpc.client
from typing import Optional


def _env_key(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(code).upper())


class OdooClient:
    def __init__(self, url: str, db: str, login: str, api_key: str):
        self.url = url.rstrip("/")
        self.db = db
        self.login = login
        self._key = api_key
        self._uid = None

    def _common(self):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)

    def _models(self):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    def uid(self) -> int:
        if self._uid is None:
            self._uid = self._common().authenticate(self.db, self.login, self._key, {})
            if not self._uid:
                raise RuntimeError("authentification Odoo refusée (login/clé API/base ?)")
        return self._uid

    def execute(self, model: str, method: str, args, kw=None):
        return self._models().execute_kw(self.db, self.uid(), self._key, model, method,
                                         args, kw or {})

    def search_read(self, model: str, domain, fields, batch: int = 500, limit: int = 100000):
        """search_read paginé (offset/limit) — renvoie TOUTES les fiches du domaine."""
        out, offset = [], 0
        while offset < limit:
            b = self.execute(model, "search_read", [domain],
                             {"fields": fields, "offset": offset, "limit": batch, "order": "id"})
            out += b
            if len(b) < batch:
                break
            offset += len(b)
        return out

    def has_field(self, model: str, field: str) -> bool:
        try:
            d = self.execute(model, "fields_get", [[field]], {"attributes": ["type"]})
            return field in (d or {})
        except Exception:
            return False

    def health(self) -> dict:
        try:
            v = self._common().version()
            uid = self.uid()
            return {"ok": True, "server_version": (v or {}).get("server_version"), "uid": uid}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}


def for_company(code: str) -> Optional[OdooClient]:
    k = _env_key(code)
    url = os.getenv(f"ODOO_{k}_URL")
    db = os.getenv(f"ODOO_{k}_DB")
    login = os.getenv(f"ODOO_{k}_LOGIN")
    key = os.getenv(f"ODOO_{k}_APIKEY")
    if not (url and db and login and key):
        return None
    return OdooClient(url, db, login, key)
