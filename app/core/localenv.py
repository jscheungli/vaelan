"""Clés LOCALES (dev / vérifications) : ~/.config/vaelan/credentials.json.

En PRODUCTION (Render), les clés viennent des variables d'environnement — ce module ne
sert qu'en local : il remplit les variables attendues par les connecteurs, sans JAMAIS
écraser une variable déjà posée. Usage :

    from app.core import localenv
    localenv.load()            # toutes les clés API (Pennylane, TopOrder, Odoo)
    localenv.load(db=True)     # + DATABASE_URL (base PROD, lecture seule par convention)

Structure du fichier (groupée PAR CONNECTEUR ; ajouter une société = une entrée) :

{
  "pennylane": { "STERNA": {"apiToken": "…", "baseUrl": "…"}, "LACORP": {…} },
  "toporder":  { "baseUrl": "…", "establishments": [{"name": "…", "apiKey": "…"}] },
  "odoo":      { "LACORP": {"url": "…", "db": "…", "login": "…", "apiKey": "…"} }
}

L'ancien emplacement (~/.config/toporder-pennylane/credentials.json, clés plates
« pennylane_XXX ») reste lu en repli, pour ne rien casser.
"""
import json
import os
import re

PATH = os.path.expanduser("~/.config/vaelan/credentials.json")
_LEGACY = os.path.expanduser("~/.config/toporder-pennylane/credentials.json")
_DB_URL = os.path.expanduser("~/.config/vaelan/db_url")


def _env_key(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(code).upper())


def _set(k, v):
    if v and not os.getenv(k):
        os.environ[k] = v


def load(path: str = None, db: bool = False) -> str:
    """Charge les clés en variables d'environnement ; renvoie le chemin utilisé."""
    p = path or (PATH if os.path.exists(PATH) else _LEGACY)
    with open(p) as f:
        d = json.load(f)

    # ---- format groupé par connecteur (canonique) ----
    pn = d.get("pennylane") or {}
    if "tokens" in pn:
        # format factorisé : {"baseUrl": "...", "tokens": {CODE: "token" | {apiToken, baseUrl}}}
        common = pn.get("baseUrl")
        for code, v in (pn.get("tokens") or {}).items():
            k = _env_key(code)
            tok = v.get("apiToken") if isinstance(v, dict) else v
            if tok and str(tok).startswith("COLLE_ICI"):
                continue                     # placeholder pas encore rempli
            _set(f"PENNYLANE_{k}_TOKEN", tok)
            _set(f"PENNYLANE_{k}_BASEURL", (v.get("baseUrl") if isinstance(v, dict) else None) or common)
    else:
        # ancien format : {CODE: {apiToken, baseUrl}}
        for code, c in pn.items():
            if not isinstance(c, dict):
                continue
            k = _env_key(code)
            _set(f"PENNYLANE_{k}_TOKEN", c.get("apiToken"))
            _set(f"PENNYLANE_{k}_BASEURL", c.get("baseUrl"))
    to = d.get("toporder") or {}
    _set("TOPORDER_BASEURL", to.get("baseUrl"))
    if to.get("establishments"):
        from app.core.connectors import toporder as _to
        for e in to["establishments"]:
            key = e.get("apiKey") or e.get("key") or e.get("token")
            if key and e.get("name"):
                _set(_to.env_var_for(e["name"]), key)
    for code, c in (d.get("odoo") or {}).items():
        k = _env_key(code)
        _set(f"ODOO_{k}_URL", c.get("url"))
        _set(f"ODOO_{k}_DB", c.get("db"))
        _set(f"ODOO_{k}_LOGIN", c.get("login"))
        _set(f"ODOO_{k}_APIKEY", c.get("apiKey"))
    for code, c in (d.get("inqom") or {}).items():
        k = _env_key(code)
        _set(f"INQOM_{k}_CLIENT_ID", c.get("clientId"))
        _set(f"INQOM_{k}_CLIENT_SECRET", c.get("clientSecret"))
        _set(f"INQOM_{k}_USER", c.get("user"))
        _set(f"INQOM_{k}_PASSWORD", c.get("password"))

    # ---- ancien format plat (rétrocompatibilité) ----
    for key, c in d.items():
        if not isinstance(c, dict):
            continue
        if key.startswith("pennylane_"):
            k = _env_key(key[len("pennylane_"):])
            _set(f"PENNYLANE_{k}_TOKEN", c.get("apiToken"))
            _set(f"PENNYLANE_{k}_BASEURL", c.get("baseUrl"))
        elif key.startswith("odoo_"):
            k = _env_key(key[len("odoo_"):])
            _set(f"ODOO_{k}_URL", c.get("url"))
            _set(f"ODOO_{k}_DB", c.get("db"))
            _set(f"ODOO_{k}_LOGIN", c.get("login"))
            _set(f"ODOO_{k}_APIKEY", c.get("apiKey"))

    if db and os.path.exists(_DB_URL):
        # base PROD — jamais par défaut : uniquement à la demande explicite (db=True)
        _set("DATABASE_URL", open(_DB_URL).read().strip())
    return p
