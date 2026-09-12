"""Valeurs par défaut du module Planning — déduites de 12 mois d'exports Skello de Copain Saint-Leu
(septembre 2025 → août 2026). Tout est ajustable dans le questionnaire de configuration."""
from datetime import date

COMPANY_CODE = "STERNA"
SITES = {"SL": "O'Copain Saint-Leu", "LP": "O'Copain La Possession", "SM": "O'Copain Sainte-Marie"}
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
DAYS_SHORT = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]

# Postes (gabarits) : clé, libellé, département, couleur, début, fin, pause, actif, ordre
POSTS = [
    ("VENTE_MATIN",      "VENTE Matin",           "vente",       "#f5d90a", "05:15", "14:00", 0.50, True,  10),
    ("VENTE_APREM",      "VENTE Après-Midi",      "vente",       "#7c83f0", "12:00", "20:15", 0.50, True,  11),
    ("VENTE_JOURNEE",    "VENTE Journée",         "vente",       "#8fe37a", "07:00", "16:00", 0.75, True,  12),
    ("VENTE_RESP",       "VENTE Responsable",     "vente",       "#e8b400", "05:15", "14:00", 0.50, False, 13),
    ("VENDEUR_APP",      "VENDEUR Apprenti(e)",   "vente",       "#d9e37a", "08:00", "16:00", 0.75, False, 14),
    ("BOULANGERIE_3H",   "BOULANGERIE 3H",        "boulangerie", "#f2e14b", "03:15", "11:45", 0.50, True,  20),
    ("BOULANGERIE_5H",   "BOULANGERIE 5H",        "boulangerie", "#f7c99b", "05:45", "13:45", 0.25, True,  21),
    ("BOULANGERIE_10H",  "BOULANGERIE 10H",       "boulangerie", "#4a9be6", "10:00", "18:15", 0.75, True,  22),
    ("BOULANGERIE_APP",  "BOULANGERIE Apprenti(e)", "boulangerie", "#f9dcc0", "06:15", "14:15", 0.50, True, 23),
    ("BOULANGERIE_STAG", "BOULANGERIE Stagiaire", "boulangerie", "#f9e6d0", "05:15", "13:00", 0.50, False, 24),
    ("PATISSERIE_5H",    "PATISSERIE 5H",         "patisserie",  "#3cb8ea", "05:00", "13:45", 0.50, True,  30),
    ("PATISSERIE_7H",    "PATISSERIE 7H",         "patisserie",  "#7ed0f0", "06:45", "15:30", 0.50, True,  31),
    ("PATISSERIE_RESP",  "PATISSERIE Responsable", "patisserie", "#1f9fd4", "05:10", "14:10", 0.50, False, 32),
    ("PATISSERIE_STAG",  "PATISSERIE Stagiaire",  "patisserie",  "#bfe8f7", "05:15", "12:45", 0.50, True,  33),
    ("TRAITEUR",         "TRAITEUR",              "traiteur",    "#c8e63c", "05:00", "13:45", 0.50, True,  40),
    ("TRAITEUR_RESP",    "TRAITEUR Responsable",  "traiteur",    "#6fd64a", "04:45", "13:45", 0.50, True,  41),
    ("TRAITEUR_APP",     "TRAITEUR Apprenti(e)",  "traiteur",    "#e0f0a0", "05:00", "13:00", 0.50, False, 42),
    ("ADMINISTRATIF",    "ADMINISTRATIF",         "admin",       "#f0857b", "06:30", "16:00", 0.50, True,  50),
    ("AGENT_POLYVALENT", "AGENT POLYVALENT",      "autre",       "#3ec48a", "07:15", "15:15", 0.50, True,  60),
    ("INVENTAIRE",       "INVENTAIRE",            "autre",       "#b0b7c3", "04:00", "13:30", 0.25, False, 61),
    ("FORMATION",        "FORMATION",             "autre",       "#d8d8d8", "08:00", "16:00", 1.00, False, 70),
]

# Couverture attendue : personnes par poste et par jour (lun … dim), par saison.
# « default » = médiane de l'année ; les saisons remplacent poste par poste.
COVERAGE = {
    "default": {
        "VENTE_MATIN": [2, 2, 2, 2, 2, 2, 2], "VENTE_APREM": [2, 2, 2, 2, 2, 2, 0], "VENTE_JOURNEE": [1, 1, 1, 1, 1, 2, 2],
        "PATISSERIE_5H": [2, 2, 2, 1, 2, 2, 1], "BOULANGERIE_3H": [1, 1, 1, 1, 1, 1, 1], "BOULANGERIE_5H": [1, 1, 1, 0, 1, 1, 0],
        "TRAITEUR": [1, 1, 1, 1, 1, 1, 0], "TRAITEUR_RESP": [1, 1, 1, 0, 1, 0, 0],
    },
    "dec_jan": {
        "VENTE_MATIN": [2, 2, 2, 2, 2, 2, 2], "VENTE_APREM": [2, 2, 2, 2, 2, 2, 0], "VENTE_JOURNEE": [2, 1, 1, 1, 1, 1, 2],
        "PATISSERIE_5H": [2, 2, 1, 1, 1, 1, 1], "BOULANGERIE_3H": [1, 1, 1, 1, 1, 1, 1], "BOULANGERIE_5H": [0, 0, 1, 0, 0, 1, 0],
        "BOULANGERIE_APP": [1, 1, 1, 1, 1, 1, 0], "TRAITEUR": [2, 1, 1, 1, 1, 1, 0], "TRAITEUR_RESP": [1, 1, 1, 0, 1, 0, 0],
    },
    "jul_aug": {
        "VENTE_MATIN": [2, 2, 2, 2, 2, 2, 2], "VENTE_APREM": [2, 2, 2, 2, 2, 3, 0], "VENTE_JOURNEE": [1, 1, 1, 1, 1, 2, 2],
        "PATISSERIE_5H": [1, 2, 2, 1, 2, 2, 1], "BOULANGERIE_3H": [1, 1, 1, 1, 1, 1, 1], "BOULANGERIE_5H": [1, 1, 0, 1, 1, 0, 0],
        "TRAITEUR": [2, 1, 2, 2, 2, 1, 0], "TRAITEUR_RESP": [1, 0, 1, 0, 0, 0, 0],
    },
    "mar_mai": {
        "VENTE_MATIN": [1, 1, 2, 2, 1, 2, 2], "VENTE_APREM": [2, 2, 2, 2, 2, 2, 0], "VENTE_JOURNEE": [1, 1, 1, 1, 1, 1, 2],
        "PATISSERIE_5H": [2, 2, 2, 2, 2, 2, 1], "BOULANGERIE_3H": [1, 1, 1, 1, 1, 1, 1], "BOULANGERIE_5H": [1, 1, 1, 0, 0, 1, 0],
        "TRAITEUR": [1, 1, 1, 1, 1, 0, 0], "TRAITEUR_RESP": [1, 1, 1, 0, 1, 0, 0],
    },
}
SEASONS = {"default": "Reste de l'année", "dec_jan": "Décembre – janvier", "jul_aug": "Juillet – août", "mar_mai": "Mars – mai"}
SEASON_OF_MONTH = {12: "dec_jan", 1: "dec_jan", 7: "jul_aug", 8: "jul_aug", 3: "mar_mai", 4: "mar_mai", 5: "mar_mai"}

HOLIDAYS_2026 = ["2026-01-01", "2026-04-06", "2026-05-01", "2026-05-08", "2026-05-14", "2026-05-25", "2026-07-14",
                 "2026-08-15", "2026-11-01", "2026-11-11", "2026-12-20", "2026-12-25"]

ABSENCE_TYPES = ["Repos hebdomadaire", "Congé payé", "Arrêt maladie", "École - CFA", "Jour férié", "Récupération",
                 "Absence injustifiée", "Absence autorisée", "Repos compensateur", "Formation", "Visite médicale"]

DEFAULT_CONFIG = {
    "site": "SL",
    "opening": {  # amplitude de présence vente par jour (première prise → dernière fin)
        "0": ["05:15", "20:30"], "1": ["05:15", "20:30"], "2": ["05:15", "20:30"], "3": ["05:15", "20:30"],
        "4": ["05:15", "20:30"], "5": ["05:15", "20:30"], "6": ["05:00", "14:30"]},
    "closed_sunday_afternoon": True,
    "other_sites": ["LP", "SM"],
    "coverage": COVERAGE,
    "rules": {
        "day_max_hours": 10.0, "day_max_span": 13.0, "week_max_hours": 48.0, "avg12_max_hours": 44.0,
        "modulation_min": 24.0, "modulation_max": 46.0, "rest_min_hours": 11.0, "rest_days_per_week": 2,
        "consecutive_max_days": 6, "default_pause": 0.5, "split_days_allowed": True, "overtime_tolerance": 3.0,
    },
    "sunday": {"max_share": 0.5, "consecutive_max": 2, "premium_pct": 0},
    "night": {"start": "20:00", "end": "06:00", "premium_pct": 25},
    "holidays": {"dates": HOLIDAYS_2026, "closed": ["2026-12-25", "2026-01-01"], "premium_pct": 100, "compensation": True},
    "supervision": {"manager_posts": ["VENTE_RESP", "VENTE_MATIN"], "manager_required": True, "manager_at_opening": False,
                    "apprentice_never_alone": True},
    "replacement": {"immediate_if_days": 1, "delay_hours": 24, "parallel": 3, "answer_minutes": 30},
    "publication": {"notice_days": 7, "horizon_weeks": 2},
    "counters": {"period": "annuelle", "alert_hours": 20},
    "validated_at": None, "wizard_step": 1,
}

# Questionnaire de configuration : étapes
WIZARD_STEPS = [
    (1, "Établissement", "Horaires d'ouverture, dimanche, autres établissements"),
    (2, "Postes", "Gabarits de plages : horaires, pause, couleur"),
    (3, "Couverture", "Personnes attendues par poste et par jour, selon la saison"),
    (4, "Temps de travail", "Durées maximales, repos, modulation, pauses"),
    (5, "Dimanches, nuit, fériés", "Rotation des dimanches, majorations, jours fériés"),
    (6, "Encadrement et apprentis", "Responsable présent, apprentis, jours de CFA"),
    (7, "Salariés", "Postes tenus, mobilité, flexibilité, ordre d'appel"),
    (8, "Remplacements et publication", "Délais, sollicitations, prévenance, compteurs"),
    (9, "Récapitulatif", "Validation et passage en mode automatique"),
]
