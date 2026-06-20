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
    """Comptes d'encaissement / écart de caisse de l'établissement."""
    x = _X[pfx]
    return {
        "especes": f"41125{x}01", "cb": f"41125{x}02", "ticket_resto": f"41125{x}03",
        "autres": f"41125{x}04", "ecart": f"41125{x}06", "b2c_commun": f"41125{x}09",
    }
