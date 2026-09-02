"""Groupe ISFAHAAN — sociétés Odoo × Pennylane (AUCUN lien avec le Groupe FDF/TopOrder).

Registre des sociétés du groupe. Ajouter une société = une entrée ici + une ligne dans
COMPANY_MODULES (security.py) + le seed + ses clés (Pennylane et, si besoin, Odoo).
"""

COMPANIES = {
    "LACORP": {"name": "Lacorp"},
}


def is_isfahaan(company_code: str) -> bool:
    return company_code in COMPANIES
