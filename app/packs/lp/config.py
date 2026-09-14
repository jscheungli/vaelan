"""La Parisienne — valeurs par défaut du modèle. Tout est surchargeable dans la page Hypothèses
(Setting `lp:config`). Une valeur None = « auto » (calibrée sur l'historique importé)."""

COMPANY_CODE = "LAPARISIENNE"

MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
MONTHS_FR_LONG = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

ENTITIES = {
    "JZ": {"name": "JIANZAN 健赞餐饮", "short": "JZ", "latin": "JIANZAN (JZ) — ZHY, BFC + Jingqiao, siège"},
    "LBL": {"name": "LEBLANC 乐博朗臻", "short": "LBL", "latin": "LEBLANC (LBL) — Qingpu"},
}

# Profils de saisonnalité (12 coefficients, moyenne 1). « auto » = calculé sur l'historique du
# magasin (années civiles complètes) ; « group » = calculé sur ZHY + BFC ; « school » = calendrier
# scolaire (kiosques près des écoles : juillet/août creux) ; « custom » = 12 valeurs saisies.
SEASON_PROFILES = {
    "school": [1.10, 0.75, 1.15, 1.10, 1.20, 1.20, 0.70, 0.65, 0.95, 1.05, 1.10, 1.05],
    "flat": [1.0] * 12,
}
SEASON_LABELS = {"auto": "Auto (historique du magasin)", "group": "Groupe (ZHY + BFC)", "school": "Calendrier scolaire",
                 "flat": "Aucune", "custom": "Personnalisée"}

GENERAL = {
    "horizon": 24,                 # mois projetés
    "calib_months": 6,             # fenêtre de calibrage des ratios (médiane des N derniers mois)
    "season_years": 2,             # années civiles complètes utilisées pour la saisonnalité auto
    "bank_fees_pct": 0.7,          # frais bancaires + commissions Meituan/POS (% CA) — compte 5503 hors intérêts
    "tax_ops_pct": 0.1,            # taxes d'exploitation 5402 (% CA)
    "cit_rate_pct": 5.0,           # impôt sur les sociétés (régime small & micro) sur le résultat trimestriel positif
    "wage_inflation_pct": 3.0,     # dérive annuelle de la masse salariale
    "cost_inflation_pct": 1.0,     # dérive annuelle des coûts fixes (loyers, siège)
    "alert_group": 500000,         # seuil d'alerte trésorerie groupe
    "alert_entity": 100000,        # seuil d'alerte trésorerie par entité
    "ho_entity": "JZ",             # le siège (5502) est porté par JIANZAN
    "ho_monthly": None,            # G&A siège mensuel (None = médiane des N derniers mois)
    "friction": {"JZ": 0, "LBL": 0},   # écart structurel P&L -> trésorerie (RMB/mois, négatif = fuite) ; voir Historique
    "branding": "anvael",
}

# Fashion Park : saisonnalité du P&L année 1 de Raphaël (sept. 2026), normalisée.
_FP_SEASON = [0.75, 0.72, 1.10, 1.19, 1.19, 1.19, 0.69, 0.75, 0.94, 1.19, 1.19, 1.10]

STORES = [
    {"code": "ZHY", "name": "Zhangyang Road", "entity": "JZ", "active": True, "opened": "2014-04", "closed": None,
     "season": "auto", "season_custom": None, "runrate_annual": None, "growth_pct": 0.0,
     "food_pct": None, "labor": None, "rent": None, "other_pct": None, "rent_period": 1, "rent_first_month": None,
     "ramp_months": 0, "ramp_start_pct": 100, "preopening_months": 0, "note": "Magasin mature (2014)."},
    {"code": "BFC", "name": "BFC + kiosque Jingqiao", "entity": "JZ", "active": True, "opened": "2018-10", "closed": None,
     "season": "auto", "season_custom": None, "runrate_annual": None, "growth_pct": 0.0,
     "food_pct": None, "labor": None, "rent": None, "other_pct": None, "rent_period": 1, "rent_first_month": None,
     "ramp_months": 0, "ramp_start_pct": 100, "preopening_months": 0,
     "note": "Jingqiao intégré depuis juin 2025 ; fermé en mai 2026 (travaux), rouvert en juin avec cuisine chaude."},
    {"code": "QPLFS", "name": "Qingpu (kiosque école française)", "entity": "LBL", "active": True, "opened": "2025-09", "closed": None,
     "season": "school", "season_custom": None, "runrate_annual": None, "growth_pct": 0.0,
     "food_pct": None, "labor": None, "rent": None, "other_pct": None, "rent_period": 1, "rent_first_month": None,
     "ramp_months": 0, "ramp_start_pct": 100, "preopening_months": 0, "note": "Kiosque depuis septembre 2025 (historique < 24 mois : profil scolaire)."},
    {"code": "FP", "name": "Fashion Park (Minhang)", "entity": "LBL", "active": False, "opened": "2027-01", "closed": None,
     "season": "custom", "season_custom": _FP_SEASON, "runrate_annual": 3070000, "growth_pct": 0.0,
     "food_pct": 30.5, "labor": 60800, "rent": 19889, "other_pct": 18.0, "rent_period": 1, "rent_first_month": None,
     "ramp_months": 3, "ramp_start_pct": 70, "preopening_months": 1,
     "note": "Projet (P&L année 1 de Raphaël, sept. 2026, calé sur Qingpu) : CA 3,07 M HT/an, food 30,5 %, "
             "masse salariale 60,8 k/mois, loyer ~20 k/mois. Le G&A 13 % du P&L n'est pas repris (siège existant). "
             "Activer le magasin pour l'inclure dans le prévisionnel."},
    {"code": "TLQ", "name": "Taikoo Li Qiantan (fermé sept. 2025)", "entity": "LBL", "active": False, "opened": "2021-10", "closed": "2025-09",
     "season": "auto", "season_custom": None, "runrate_annual": None, "growth_pct": 0.0,
     "food_pct": None, "labor": None, "rent": None, "other_pct": None, "rent_period": 1, "rent_first_month": None,
     "ramp_months": 0, "ramp_start_pct": 100, "preopening_months": 0, "note": "Historique seulement."},
    {"code": "HSF", "name": "HSF (fermé mai 2025)", "entity": "LBL", "active": False, "opened": "2023-09", "closed": "2025-05",
     "season": "auto", "season_custom": None, "runrate_annual": None, "growth_pct": 0.0,
     "food_pct": None, "labor": None, "rent": None, "other_pct": None, "rent_period": 1, "rent_first_month": None,
     "ramp_months": 0, "ramp_start_pct": 100, "preopening_months": 0, "note": "Historique seulement."},
]

# Prêts bancaires : bullet à 1 an, roulés (renouvelés) chaque année. renew_gap = mois entre le
# remboursement et le nouveau tirage (0 = même mois, pas de trou de trésorerie en fin de mois).
LOANS = [
    {"id": "cmbc", "label": "CMBC — prêt court terme", "entity": "JZ", "principal": 2000000, "rate_pct": 4.2,
     "maturity": "2027-06", "term_months": 12, "renew": True, "renew_gap": 0, "renew_amount": None, "active": True,
     "note": "2,3 M jusqu'en 2025, 2,0 M depuis juin 2025 ; remboursé et retiré en juin 2026 (balance 2101)."},
    {"id": "abc", "label": "ABC Bank — prêt 500 k (LBL)", "entity": "LBL", "principal": 500000, "rate_pct": 4.0,
     "maturity": "2026-05", "term_months": 12, "renew": False, "renew_gap": 0, "renew_amount": None, "active": False,
     "note": "Tiré en mai 2025, remboursé en mai 2026, non renouvelé (relayé par un apport de 500 k en 2181.03 LBL en juin 2026)."},
]

EVENT_CATEGORIES = {
    "capex": "Investissement (décaissement)",
    "cca": "Compte courant d'associé (+ apport / − remboursement)",
    "loan": "Prêt hors registre (+ tirage / − remboursement)",
    "interco": "Transfert entre entités (+ reçu par l'entité, − pour la contrepartie)",
    "other": "Autre encaissement / décaissement",
    "fermeture": "Fermeture d'un magasin ce mois (CA = 0, salaires et loyer maintenus)",
    "ca_pct": "Variation de CA d'un magasin (% ; permanente ou ponctuelle)",
}

EVENTS = [
    {"id": "fp_capex1", "month": "2026-11", "entity": "LBL", "store": "FP", "category": "capex", "amount": -700000, "pct": 0,
     "permanent": False, "counterparty": "", "active": True, "label": "Fashion Park : travaux et matériel (1/2)",
     "note": "CAPEX brut ~1,2 M (Raphaël) ; matériel réutilisable à déduire une fois le plan capex établi."},
    {"id": "fp_capex2", "month": "2026-12", "entity": "LBL", "store": "FP", "category": "capex", "amount": -500000, "pct": 0,
     "permanent": False, "counterparty": "", "active": True, "label": "Fashion Park : travaux et matériel (2/2)", "note": ""},
    {"id": "fp_deposit", "month": "2026-11", "entity": "LBL", "store": "FP", "category": "other", "amount": -60000, "pct": 0,
     "permanent": False, "counterparty": "", "active": True, "label": "Fashion Park : dépôt de garantie (hypothèse 3 mois de loyer)", "note": ""},
]

# Comptes courants d'associés : registre par associé et par entité (détail à confirmer par JS).
CCA = {
    "shareholders": ["JS", "Raphaël & Li Dan", "Bruno"],
    "positions": [
        {"shareholder": "JS", "entity": "JZ", "amount": 960000, "source": "suivi CCA avril 2026 (post-transfert Miyi) — à confirmer"},
        {"shareholder": "Raphaël & Li Dan", "entity": "JZ", "amount": 955000, "source": "1 455 k post-transfert Miyi − 500 k remboursés en mai 2026 — à confirmer"},
        {"shareholder": "Bruno", "entity": "JZ", "amount": 360000, "source": "suivi CCA avril 2026 (post-transfert Miyi) — à confirmer"},
        {"shareholder": "JS", "entity": "GHHL", "amount": 50000, "source": "USD — suivi CCA avril 2026"},
        {"shareholder": "Raphaël & Li Dan", "entity": "GHHL", "amount": 50000, "source": "USD — suivi CCA avril 2026"},
        {"shareholder": "Bruno", "entity": "GHHL", "amount": 150000, "source": "USD — suivi CCA avril 2026"},
        {"shareholder": "?", "entity": "LBL", "amount": 500000, "source": "apport de 500 k comptabilisé en 2181.03 LBL en juin 2026 — associé à identifier"},
    ],
    "accounts": {"JZ": ["cca_14", "other_03"], "LBL": ["other_03"]},   # comptes de la balance rapprochés du registre
}

# Feuilles du Board Management Report -> magasin. La feuille « 004 » a porté HSF (jusqu'en mai 2025)
# puis QPLFS (depuis septembre 2025).
SHEET_MAP = {"001-ZHY": "ZHY", "002-BFC": "BFC", "003-TLQ": "TLQ", "004-HSF": "HSF", "004-QPLFS": "QPLFS", "Head Office": "HO"}
SHEET_004_SWITCH = "2025-06"   # feuille 004-QPLFS : mois < switch -> HSF


def default_config() -> dict:
    import copy
    return copy.deepcopy({"general": GENERAL, "stores": STORES, "loans": LOANS, "events": EVENTS, "cca": CCA})
