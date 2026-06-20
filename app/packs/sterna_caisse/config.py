"""Config du pack Sterna — Caisse : établissements et comptes Pennylane.

Migré depuis les scripts (est_config). Les numéros de compte/journaux servent à
la génération CSV ; le shop_id sert au pull TopOrder.
NB : le CSV d'import manuel Pennylane référence les comptes par NUMÉRO.
"""
import json
import os

# établissement -> shop_id TopOrder + préfixe court
ESTABLISHMENTS = {
    "OCOPAIN SAINT-LEU":     {"pfx": "SL", "shop_id": "08de7f5e-07cb-4114-8211-0a8c8b7e012a"},
    "OCOPAIN LA POSSESSION": {"pfx": "LP", "shop_id": "08de7f5e-5e84-444a-8c9a-f7e86ed0e785"},
    "OCOPAIN SAINTE-MARIE":  {"pfx": "SM", "shop_id": "08de7f5d-bc8d-407f-850c-e5c0dded9eea"},
}

# Comptes Pennylane STERNA (numéros), par préfixe d'établissement.
#   X = 2e chiffre des comptes 41125X.. : SM=1, LP=2, SL=3
_X = {"SM": "1", "LP": "2", "SL": "3"}

CA_ANONYME = "70101"      # caisse anonyme
CA_B2C = "70102"          # particuliers (différé)
CA_B2B = "7012"           # B2B (compte unique ; le taux est porté par la colonne TVA)
TVA_ACCOUNT = {"2.1": "44571005", "8.5": "44571007", "5.5": "44571006", "20": "44571009"}

# Analytique Pennylane (catégories par établissement) : matching par NOM dans le CSV.
# Groupe « Etablissement » (id 819366) ; catégories SAINT-LEU/LA POSSESSION/SAINTE-MARIE.
ANALYTIC_FAMILY = "Etablissement"
ANALYTIC_CATEGORY = {"SL": "SAINT-LEU", "LP": "LA POSSESSION", "SM": "SAINTE-MARIE"}

# Mapping client (companyId/customerId -> compte 411), migré et validé (overrides inclus).
# TODO : à terme, remplacer par un module « résolution client » (job + table DB).
with open(os.path.join(os.path.dirname(__file__), "data", "clients.json"), encoding="utf-8") as _f:
    CLIENTS = json.load(_f)


def resolve_411(pfx: str, company_id, customer_id):
    """Renvoie (numéro de compte 411, libellé client) pour router une créance."""
    if company_id:
        e = CLIENTS["b2b"].get(f"{pfx}:{company_id}")
        if e and e.get("account"):
            return e["account"], (e.get("name") or "client")
    if customer_id:
        return CLIENTS["b2c_commun"].get(pfx), "Particulier"
    return None, None

# journaux Pennylane par établissement : (TOSLT tickets, TOSLF factures), cat analytique
# Les ids numériques servent à l'API ; le CSV d'import manuel référence le journal
# par son CODE (TOSLT/TOSLF) -> voir journal_code().
JOURNALS = {
    "SL": {"tickets": 78512312320, "factures": 84363386880, "analytic": 10174227},
    "LP": {"tickets": 84391071744, "factures": 84391256064, "analytic": 10174226},
    "SM": {"tickets": 84392247296, "factures": 84392374272, "analytic": 10174225},
}


def journal_code(pfx: str, kind: str) -> str:
    """Code journal Pennylane (pour le CSV) : tickets -> TO<PFX>T, factures -> TO<PFX>F."""
    return f"TO{pfx}{'T' if kind == 'tickets' else 'F'}"


def caisse_accounts(pfx: str) -> dict:
    """Comptes d'encaissement / écart de caisse de l'établissement (défauts)."""
    x = _X[pfx]
    return {
        "especes": f"41125{x}01", "cb": f"41125{x}02", "ticket_resto": f"41125{x}03",
        "autres": f"41125{x}04", "ecart": f"41125{x}06", "b2c_commun": f"41125{x}09",
    }


# ------------------------------------------------------------------ config résolue
# Schéma plat éditable : groupes -> {clé: (libellé, valeur par défaut)}. La page
# Configuration lit/écrit ces clés ; resolve() applique les surcharges DB (Setting).
def _defaults() -> dict:
    est = {}
    for name, e in ESTABLISHMENTS.items():
        pfx = e["pfx"]
        j = JOURNALS[pfx]
        est[pfx] = {
            "category": ANALYTIC_CATEGORY[pfx],
            "journal_tickets": journal_code(pfx, "tickets"),
            "journal_tickets_id": str(j["tickets"]),
            "journal_factures": journal_code(pfx, "factures"),
            "journal_factures_id": str(j["factures"]),
            **caisse_accounts(pfx),
        }
    return {"ca_anonyme": CA_ANONYME, "ca_b2c": CA_B2C, "ca_b2b": CA_B2B,
            "analytic_family": ANALYTIC_FAMILY, "tva": dict(TVA_ACCOUNT), "est": est}


# libellés lisibles pour la page Configuration (clé « : » -> libellé)
ACCOUNT_FIELDS = [("especes", "Espèces"), ("cb", "CB"), ("ticket_resto", "Ticket restaurant"),
                  ("autres", "Autres / compte client"), ("ecart", "Écart de caisse"),
                  ("b2c_commun", "Particuliers (compte commun)")]
JOURNAL_FIELDS = [("journal_tickets", "Code journal Tickets (CSV)"),
                  ("journal_tickets_id", "ID journal Tickets (API)"),
                  ("journal_factures", "Code journal Factures (CSV)"),
                  ("journal_factures_id", "ID journal Factures (API)"),
                  ("category", "Catégorie analytique")]


def _set_path(d, key, value):
    parts = key.split(":")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            return
        cur = cur[p]
    if parts[-1] in cur:
        cur[parts[-1]] = value


def resolve(company_code: str) -> dict:
    """Config effective d'une société : défauts du code + surcharges DB (Setting)."""
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import Setting
    cfg = _defaults()
    with Session(engine) as s:
        for st in s.exec(select(Setting).where(Setting.company_code == company_code)).all():
            _set_path(cfg, st.key, st.value)
    return cfg


def config_sections(cfg: dict) -> list:
    """Structure la page Configuration : sections -> champs {key, label, value}."""
    sections = [
        {"title": "Comptes de vente (CA)", "fields": [
            {"key": "ca_anonyme", "label": "CA caisse anonyme (Tickets)", "value": cfg["ca_anonyme"]},
            {"key": "ca_b2c", "label": "CA particuliers / B2C (différé)", "value": cfg["ca_b2c"]},
            {"key": "ca_b2b", "label": "CA B2B", "value": cfg["ca_b2b"]},
        ]},
        {"title": "Comptes de TVA collectée", "fields": [
            {"key": f"tva:{r}", "label": f"TVA {r}%", "value": v} for r, v in cfg["tva"].items()
        ]},
        {"title": "Analytique", "fields": [
            {"key": "analytic_family", "label": "Famille de catégories", "value": cfg["analytic_family"]},
        ]},
    ]
    for pfx, e in cfg["est"].items():
        sections.append({"title": f"Établissement {pfx}", "fields": [
            {"key": f"est:{pfx}:{k}", "label": lbl, "value": e[k]}
            for k, lbl in JOURNAL_FIELDS + ACCOUNT_FIELDS
        ]})
    return sections


def flat_defaults() -> dict:
    """Toutes les clés éditables -> valeur par défaut (pour comparer/réinitialiser)."""
    out = {"ca_anonyme": CA_ANONYME, "ca_b2c": CA_B2C, "ca_b2b": CA_B2B,
           "analytic_family": ANALYTIC_FAMILY}
    for r, acc in TVA_ACCOUNT.items():
        out[f"tva:{r}"] = acc
    d = _defaults()["est"]
    for pfx, e in d.items():
        for k, v in e.items():
            out[f"est:{pfx}:{k}"] = v
    return out
