"""Connecteur Shopify — Admin API GraphQL (une boutique par société).

Clés : SHOPIFY_<CODE>_SHOP (ex. « 2ac587-75.myshopify.com »), puis AU CHOIX :
  - SHOPIFY_<CODE>_CLIENT_ID + SHOPIFY_<CODE>_CLIENT_SECRET : identifiants de l'app du Dev Dashboard (même organisation
    que la boutique, app installée) → jeton obtenu par « client credentials grant », valable 24 h, renouvelé tout seul ;
  - SHOPIFY_<CODE>_TOKEN : jeton d'accès Admin API fixe (« shpat_… », ancien flux).
SHOPIFY_<CODE>_APIVERSION est optionnel (défaut ci-dessous). Variables Render, ou section "shopify" de
~/.config/vaelan/credentials.json en local (chargée par localenv).
Le jeton ne circule jamais dans les journaux ni les pages : seul health()/whoami() renvoie des informations de boutique."""
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_API_VERSION = "2026-07"


class ShopifyError(RuntimeError):
    pass


class ShopifyClient:
    def __init__(self, shop: str, token: Optional[str] = None, api_version: str = DEFAULT_API_VERSION,
                 client_id: Optional[str] = None, client_secret: Optional[str] = None):
        shop = shop.strip().lower().replace("https://", "").rstrip("/")
        if not shop.endswith(".myshopify.com"):
            shop = f"{shop}.myshopify.com"
        self.shop = shop
        self.api_version = api_version or DEFAULT_API_VERSION
        self.url = f"https://{shop}/admin/api/{self.api_version}/graphql.json"
        self._static = token
        self._client_id, self._client_secret = client_id, client_secret
        self._token, self._expires = token, (float("inf") if token else 0.0)

    def _access_token(self) -> str:
        """Jeton courant : fixe (shpat_…) ou obtenu par client credentials grant (24 h, renouvelé 5 min avant expiration)."""
        if self._static:
            return self._static
        if self._token and time.time() < self._expires - 300:
            return self._token
        with httpx.Client(timeout=30) as c:
            r = c.post(f"https://{self.shop}/admin/oauth/access_token",
                       data={"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret},
                       headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if r.status_code >= 400:
            raise ShopifyError(f"jeton refusé (HTTP {r.status_code}) : app installée sur la boutique et même organisation ? {r.text[:200]}")
        d = r.json()
        self._token, self._expires = d.get("access_token"), time.time() + float(d.get("expires_in") or 86399)
        self.granted_scope = d.get("scope") or ""
        return self._token

    def gql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Requête GraphQL ; respecte le seau de points (429 / THROTTLED → attente puis nouvel essai)."""
        for attempt in range(6):
            h = {"X-Shopify-Access-Token": self._access_token(), "Content-Type": "application/json", "Accept": "application/json"}
            with httpx.Client(timeout=60) as c:
                r = c.post(self.url, headers=h, json={"query": query, "variables": variables or {}})
            if r.status_code == 429 and attempt < 5:
                time.sleep(float(r.headers.get("Retry-After") or 2))
                continue
            if r.status_code >= 400:
                raise ShopifyError(f"HTTP {r.status_code} : {r.text[:300]}")
            d = r.json()
            errs = d.get("errors") or []
            if errs and all((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs) and attempt < 5:
                time.sleep(2.0 * (attempt + 1))
                continue
            if errs:
                raise ShopifyError("; ".join(str(e.get("message")) for e in errs)[:400])
            return d.get("data") or {}
        raise ShopifyError("API saturée (THROTTLED), réessayer plus tard")

    def pages(self, query: str, path: str, variables: Optional[Dict[str, Any]] = None, first: int = 100) -> List[dict]:
        """Parcourt une connexion paginée : `query` doit accepter $first et $after et renvoyer {edges{node}, pageInfo} au chemin `path` (ex. « products »)."""
        out, after = [], None
        while True:
            d = self.gql(query, {**(variables or {}), "first": first, "after": after})
            conn = d
            for k in path.split("."):
                conn = (conn or {}).get(k) or {}
            out += [e["node"] for e in conn.get("edges") or []]
            pi = conn.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                return out
            after = pi.get("endCursor")

    def whoami(self) -> dict:
        """Identité de la boutique (contrôle d'embarquement) : nom, domaine, plan, devise, e-mail, fuseau."""
        d = self.gql("""{ shop { name myshopifyDomain primaryDomain { host } plan { displayName partnerDevelopment shopifyPlus }
                                 currencyCode email ianaTimezone billingAddress { country } } }""")
        s = d.get("shop") or {}
        return {"name": s.get("name"), "domain": s.get("myshopifyDomain"), "site": (s.get("primaryDomain") or {}).get("host"),
                "plan": (s.get("plan") or {}).get("displayName"), "currency": s.get("currencyCode"), "email": s.get("email"),
                "timezone": s.get("ianaTimezone"), "country": (s.get("billingAddress") or {}).get("country")}

    def scopes(self) -> List[str]:
        """Droits accordés au jeton (pour vérifier qu'un module a ce qu'il faut avant d'écrire)."""
        d = self.gql("{ currentAppInstallation { accessScopes { handle } } }")
        return sorted(x["handle"] for x in ((d.get("currentAppInstallation") or {}).get("accessScopes") or []))

    def health(self) -> dict:
        try:
            w = self.whoami()
            return {"ok": True, "shop": w.get("name"), "domain": w.get("domain"), "plan": w.get("plan")}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}


def _env_key(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", code.upper())


def for_company(code: str) -> Optional[ShopifyClient]:
    k = _env_key(code)
    shop, token = os.getenv(f"SHOPIFY_{k}_SHOP"), os.getenv(f"SHOPIFY_{k}_TOKEN")
    cid, csec = os.getenv(f"SHOPIFY_{k}_CLIENT_ID"), os.getenv(f"SHOPIFY_{k}_CLIENT_SECRET")
    if not shop or not (token or (cid and csec)):
        return None
    return ShopifyClient(shop, token, os.getenv(f"SHOPIFY_{k}_APIVERSION") or DEFAULT_API_VERSION, client_id=cid, client_secret=csec)
