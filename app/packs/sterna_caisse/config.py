"""Config du pack Sterna — Caisse : établissements et comptes Pennylane.

Migré depuis les scripts (est_config). Les ID de comptes/journaux servent à la
génération CSV et au rapprochement ; le shop_id sert au pull TopOrder.
"""

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
TVA_ACCOUNT = {"2.1": "44571005", "8.5": "44571007", "5.5": "44571006", "20": "44571009"}

# journaux Pennylane par établissement : (TOSLT tickets, TOSLF factures), cat analytique
JOURNALS = {
    "SL": {"tickets": 78512312320, "factures": 84363386880, "analytic": 10174227},
    "LP": {"tickets": 84391071744, "factures": 84391256064, "analytic": 10174226},
    "SM": {"tickets": 84392247296, "factures": 84392374272, "analytic": 10174225},
}


def caisse_accounts(pfx: str) -> dict:
    """Comptes d'encaissement / écart de caisse de l'établissement."""
    x = _X[pfx]
    return {
        "especes": f"41125{x}01", "cb": f"41125{x}02", "ticket_resto": f"41125{x}03",
        "autres": f"41125{x}04", "ecart": f"41125{x}06", "b2c_commun": f"41125{x}09",
    }
