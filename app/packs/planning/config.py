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

# Jours fériés : on configure des TYPES, Vaelan calcule la date chaque année (Pâques, Ascension, Pentecôte sont mobiles).
HOLIDAY_TYPES = [
    ("jour_an", "Jour de l'an", "fixe", (1, 1)), ("paques", "Lundi de Pâques", "mobile", 1), ("mai_1", "Fête du Travail", "fixe", (5, 1)),
    ("mai_8", "Victoire 1945", "fixe", (5, 8)), ("ascension", "Ascension", "mobile", 39), ("pentecote", "Lundi de Pentecôte", "mobile", 50),
    ("juillet_14", "Fête nationale", "fixe", (7, 14)), ("aout_15", "Assomption", "fixe", (8, 15)), ("toussaint", "Toussaint", "fixe", (11, 1)),
    ("nov_11", "Armistice", "fixe", (11, 11)), ("dec_20", "Abolition de l'esclavage (La Réunion)", "fixe", (12, 20)), ("noel", "Noël", "fixe", (12, 25)),
]


def easter(year: int) -> date:
    """Dimanche de Pâques (algorithme de Meeus/Jones/Butcher)."""
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) / 451
    month = (h + l - 7 * int(m) + 114) // 31; day = (h + l - 7 * int(m) + 114) % 31 + 1
    return date(year, month, day)


def holiday_dates(year: int, types=None) -> dict:
    """{clé: date} pour l'année demandée, limité aux types donnés (tous par défaut)."""
    from datetime import timedelta
    out = {}
    for key, label, kind, spec in HOLIDAY_TYPES:
        if types is not None and key not in types:
            continue
        out[key] = date(year, *spec) if kind == "fixe" else easter(year) + timedelta(days=spec)
    return out


HOLIDAY_LABELS = {k: l for k, l, _, _ in HOLIDAY_TYPES}

ABSENCE_TYPES = ["Repos hebdomadaire", "Congé payé", "Arrêt maladie", "École - CFA", "Jour férié", "Récupération",
                 "Absence injustifiée", "Absence autorisée", "Repos compensateur", "Formation", "Visite médicale"]

DEFAULT_CONFIG = {
    "site": "SL",
    "coverage": COVERAGE,                       # Saint-Leu (déduit) ; les autres établissements dans coverage_by_site
    "coverage_by_site": {},                     # {site: {saison: {poste: [7]}}} — déduit de l'historique à l'import, modifiable
    "rules": {"day_max_hours": 10.0, "week_max_hours": 48.0, "avg12_max_hours": 44.0, "rest_min_hours": 11.0,
              "consecutive_max_days": 6, "overtime_tolerance": 3.0},
    "sunday": {"max_share": 0.5, "consecutive_max": 2, "premium_pct": 20},      # CCN 843 art. 28 : +20 % minimum (Skello : 0 % → à corriger)
    "night": {"start": "20:00", "end": "06:00", "premium_pct": 25},              # CCN 843 : 20h-6h +25 %
    "overtime": {"t1_from": 36, "t1_pct": 25, "t2_from": 44, "t2_pct": 50, "annual_quota": 220},   # CCN 843 (information)
    "holidays": {"types": [k for k, _, _, _ in HOLIDAY_TYPES], "closed_types": ["noel", "jour_an"], "premium_pct": 100, "compensation": True, "confirm_days": 21},
    "supervision": {"manager_posts": ["VENTE_RESP", "VENTE_MATIN"], "manager_required": True},
    "publication": {"horizon_weeks": 2},
    # responsables à prévenir à chaque changement validé, par établissement : [{role, name, email, phone}]
    "managers": {},
    "alerts": {"enabled": True, "emails": "jscheungli@gmail.com", "daily": True, "weekly": True, "horizon_days": 14,
               "rules": {k: True for k in ["day_max", "rest", "week_max", "avg12", "consecutive", "days_week", "sunday_consecutive",
                                           "sunday_share", "cfa", "days_off", "sunday_off", "overtime", "coverage", "unassigned", "manager"]}},
    "validated_at": None, "wizard_step": 1,
}

# Niveau sur un poste : 0 = n'intervient pas ; 1 = préféré ; 2, 3… = de moins en moins prioritaire (jusqu'à 10).
LEVEL_MAX = 10


def level_weight(lvl) -> int:
    """Poids dans les scores : 10 pour le niveau 1, 9 pour le 2 … 1 pour le 10, 0 si absent."""
    try:
        l = int(lvl or 0)
    except Exception:
        return 0
    return max(0, LEVEL_MAX + 1 - l) if 1 <= l <= LEVEL_MAX else 0


# Questionnaire de configuration : étapes
MANAGER_ROLES = ["Responsable d'équipe", "Responsable planning", "Responsable d'établissement", "Direction"]

WIZARD_STEPS = [
    (1, "Postes", "Gabarits de plages : horaires, pause, couleur"),
    (2, "Couverture", "Personnes attendues par poste et par jour, selon la saison"),
    (3, "Règles", "Temps de travail, dimanches, nuit, jours fériés, encadrement"),
    (4, "Salariés", "Postes tenus (1 = préféré), habitudes, mobilité, ordre d'appel"),
    (5, "Récapitulatif", "Validation et passage en mode automatique"),
]

# Règles contrôlées : clé -> (libellé, référence, niveau)
RULES_CATALOG = [
    ("day_max", "Durée quotidienne maximale (10 h de travail effectif)", "Code du travail L3121-18 · CCN 843", "danger"),
    ("rest", "Repos quotidien de 11 h entre deux journées", "Code du travail L3131-1 · CCN 843", "danger"),
    ("week_max", "Durée hebdomadaire maximale (48 h)", "Code du travail L3121-20 · CCN 843", "danger"),
    ("avg12", "Moyenne maximale sur 12 semaines (44 h)", "CCN 843 (avenant n° 57)", "danger"),
    ("consecutive", "Jours de travail consécutifs (6 maximum)", "Code du travail L3132-1", "danger"),
    ("days_week", "Jours travaillés au-delà du maximum du salarié", "règle interne (contrat)", "warning"),
    ("sunday_consecutive", "Dimanches consécutifs au-delà du maximum", "règle interne (rotation)", "warning"),
    ("sunday_share", "Part de dimanches travaillés au-delà du maximum (8 dernières semaines)", "règle interne (rotation)", "warning"),
    ("cfa", "Plage posée un jour de CFA", "contrat d'apprentissage", "danger"),
    ("days_off", "Plage posée un jour habituellement non travaillé", "habitude du salarié", "warning"),
    ("sunday_off", "Plage un dimanche pour un salarié exempté", "règle interne", "danger"),
    ("overtime", "Heures au-delà du contrat + tolérance (heures supplémentaires)", "CCN 843 : 36e-43e h +25 %, 44e+ +50 %", "warning"),
    ("coverage", "Couverture d'un poste inférieure au besoin", "règle interne (couverture)", "danger"),
    ("unassigned", "Plage sans personne assignée", "règle interne", "danger"),
    ("manager", "Aucun responsable de vente un jour d'ouverture", "règle interne (encadrement)", "warning"),
]

