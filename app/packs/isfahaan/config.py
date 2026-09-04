"""Groupe ISFAHAAN — sociétés Odoo × Pennylane (AUCUN lien avec le Groupe FDF/TopOrder).

Registre des sociétés du groupe. Ajouter une société = une entrée ici + une ligne dans
COMPANY_MODULES (security.py) + le seed + ses clés (Pennylane et, si besoin, Odoo).
"""

COMPANIES = {
    "LACORP": {"name": "Lacorp", "siren": "882566128"},
    "JBIBFOOD": {"name": "JB & IB FOOD", "siren": "877519371"},
    "JBFOOD": {"name": "JB FOOD", "siren": "833093875"},
    "OTCSTPIERRE": {"name": "OTC SAINT-PIERRE", "siren": "899890651"},
    "OTCRESERVE": {"name": "OTC LA RESERVE", "siren": "895133940"},
    "OTCBRASFUSIL": {"name": "OTC BRAS FUSIL", "siren": "981608151"},
    "GLDSTDENIS": {"name": "GLD SAINT-DENIS", "siren": "910919174"},
    "GLDCASABONA": {"name": "GLD CASABONA", "siren": "952182764"},
    "ISFAHAAN": {"name": "Isfahaan (holding)", "siren": "981355589"},
    "GONGCHA": {"name": "GONG CHA", "siren": "953142080"},
}

# ---- Inqom (justificatifs) ----
INQOM_KEY = "ISFAHAAN"          # clé credentials du cabinet (INQOM_ISFAHAAN_* / credentials.json)
# entreprise Inqom -> société Vaelan (id relevés en réel le 03/09/2026 ; ⚠️ 118003 « LACORP »
# est une coquille vide, la vraie est 123449). On ajoute les autres sociétés du groupe ici
# au fur et à mesure de leur création dans Vaelan :
#   OTC LA RESERVE 118010 · OTC SAINT-PIERRE 118022 · GLD SAINT-DENIS 118024 ·
#   GLD CASABONA 118027 · JB FOOD 118694 · ISFAHAAN 118695 · OTC BRAS FUSIL 118696 ·
#   JB & IB FOOD 122482 · GLD CHAUDRON 140136
INQOM_ENTERPRISES = {
    "LACORP": 123449,
    "JBIBFOOD": 122482,
    "JBFOOD": 118694,
    "OTCSTPIERRE": 118022,
    "OTCRESERVE": 118010,
    "OTCBRASFUSIL": 118696,
    "GLDSTDENIS": 118024,
    "GLDCASABONA": 118027,
    "ISFAHAAN": 118695,
    "GONGCHA": 118698,      # accès ouvert le 04/09/2026 (SIREN 953142080 vérifié)
}


def is_isfahaan(company_code: str) -> bool:
    return company_code in COMPANIES
