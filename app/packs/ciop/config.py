"""CIOP — valeurs par défaut par société (identité, associés, établissements, déclarant) et paramètres.
Tout est surchargeable dans le menu de configuration (Setting `ciop:config`)."""

PARAMS = {
    "code_invest": "ART",                 # code utilisé depuis toujours par JS (artisanal) — jamais contesté
    "article": "244 quater W",
    "rate_pct": 35,
    "fy_end_month": 6, "fy_end_day": 30,  # exercices clos le 30 juin
    "purchase_journals": ["HA"],          # journaux d'achats : seuls mouvements retenus (pas les OD ni les à-nouveaux)
    "account_keyword": "CIOP",            # comptes de classe 2 dont le libellé contient ce mot (hors 28x amortissements)
    "cerfa_version": "2026",
}

# aliases : indices (minuscules, sans accent) cherchés dans le texte ou le nom de fichier de la facture
COMPANIES = {
    "STERNA": {
        "identity": {"name": "STERNA", "address": "6 RUELLE MAZEAU,\n97400 SAINT-DENIS", "siren": "492194337", "legal_form": "SAS", "ape": "1071C"},
        "partners": [{"name": "FDF SAS", "address": "6 RUELLE MAZEAU, 97400 SAINT-DENIS", "siren": "792961286", "share": "100"}],
        "sites": [
            {"code": "SM", "label": "Sainte-Marie", "address": "143 rue Louis Lagourgue, 97438 Sainte-Marie", "aliases": ["sainte-marie", "sainte marie", "ste marie", "ste-marie", "copain sm"]},
            {"code": "SL", "label": "Saint-Leu", "address": "123 rue Général Lambert, 97436 Saint-Leu", "aliases": ["saint-leu", "saint leu", "st leu", "st-leu", "general lambert"]},
            {"code": "LP", "label": "La Possession", "address": "44B rue Mahatma Gandhi, 97419 La Possession", "aliases": ["possession", "mahatma gandhi"]},
        ],
        "declarant": {"name": "JEAN-CHARLES CHEUNG-AH-SEUNG", "quality": "Gérant de BEELI EURL, Présidente de STERNA SAS",
                      "address": "126 CD 41, 97419 LA POSSESSION", "place": "SAINT-DENIS", "signature_note": "P/O Jean-Sébastien CHEUNG-AH-SEUNG, Associé"},
    },
    "KOOKABURA": {
        "identity": {"name": "KOOKABURA", "address": "6 RUELLE MAZEAU,\n97400 SAINT-DENIS", "siren": "833988322", "legal_form": "SARL", "ape": "1071C"},
        "partners": [{"name": "STERNA SAS", "address": "6 RUELLE MAZEAU, 97400 SAINT-DENIS", "siren": "492194337", "share": "100"}],
        "sites": [
            {"code": "LP", "label": "La Possession (laboratoire)", "address": "44A Rue Mahatma Gandhi, 97419 La Possession", "aliases": ["possession", "mahatma gandhi", "kooka"]},
        ],
        "declarant": {"name": "JEAN-CHARLES CHEUNG-AH-SEUNG", "quality": "GERANT",
                      "address": "126 CD 41, 97419 LA POSSESSION", "place": "SAINT-DENIS", "signature_note": "P/O Jean-Sébastien CHEUNG-AH-SEUNG, Associé"},
    },
}

# Indices d'inéligibilité (matériel d'occasion, enchères, reconditionné) : la ligne est proposée ÉCARTÉE avec une note
INELIGIBLE_HINTS = [(r"adjudication|ench[eè]res|commissaire[- ]priseur", "vente aux enchères : matériel d'occasion, non éligible"),
                    (r"reconditionn|d'occasion|occasion\b", "matériel d'occasion / reconditionné, non éligible")]

# Libellés « lisibles par un inspecteur » : mot-clé (sans accent, minuscules) -> libellé orienté production.
# Le premier motif trouvé dans la désignation de la facture l'emporte ; toujours modifiable à la main.
LABEL_RULES = [
    ("armoire fermentation|armoire de fermentation|chambre de pousse|etuve", "ARMOIRE DE FERMENTATION PRODUCTION BOULANGERIE"),
    ("coupe legumes|coupe-legumes|cutter", "COMBINE CUTTER COUPE-LEGUMES PRODUCTION"),
    ("injecteur|bar a croissant", "INJECTEURS DE GARNISSAGE PRODUCTION"),
    ("lave batterie|lave-batterie", "LAVE-BATTERIE PRODUCTION"),
    ("lave verre|lave-verre|lave vaisselle|lave-vaisselle", "LAVE-VERRES PRODUCTION"),
    ("vitrine", "VITRINE REFRIGEREE PRODUCTION"),
    ("armoire vitr|armoire refrig|armoire froide|armoire positive|armoire negative", "ARMOIRE REFRIGEREE PRODUCTION"),
    ("trancheuse", "TRANCHEUSE PRODUCTION PAIN"),
    ("chariot", "CHARIOT PRODUCTION ET ACCESSOIRES"),
    ("presse agrume|presse-agrume", "PRESSE AGRUMES PRODUCTION"),
    ("banneton", "BANNETONS PRODUCTION PAIN"),
    (r"\bmoules?\b", "MOULES DE PRODUCTION"),
    ("cercle|cadre a mousse|cadre inox", "PETIT MATERIEL DE PRODUCTION (CERCLES, CADRES)"),
    (r"\bbalances?\b", "BALANCE CONTROLE PESEE PRODUCTION"),
    ("cellule de refroidissement|surgelation|surgélation", "CELLULE DE REFROIDISSEMENT RAPIDE PRODUCTION"),
    ("refroidisseur d'eau|refroidisseurs d'eau|refroidisseur", "REFROIDISSEUR D'EAU PRODUCTION PAIN"),
    ("doseur", "DOSEUR D'EAU PRODUCTION PAIN"),
    ("pulverisateur de dorure|pulverisateur", "PULVERISATEUR DE DORURE PRODUCTION"),
    ("congelateur|congélateur|conservateur", "CONGELATEUR PRODUCTION"),
    ("chambre froide negative", "CHAMBRE FROIDE NEGATIVE PRODUCTION"),
    ("chambre froide", "CHAMBRE FROIDE POSITIVE PRODUCTION"),
    ("crumbler", "CRUMBLER RECYCLAGE DU PAIN INVENDU PRODUCTION"),
    ("plonge", "PLONGE INOX PRODUCTION"),
    ("petrin", "PETRIN DE PRODUCTION"),
    ("batteur|melangeur", "BATTEUR MELANGEUR PRODUCTION"),
    ("laminoir", "LAMINOIR PRODUCTION"),
    ("diviseuse bouleuse", "DIVISEUSE BOULEUSE PRODUCTION"),
    ("diviseuse", "DIVISEUSE PRODUCTION"),
    ("tour panimatic|tour refriger|tour patiss|tour positi", "TOUR REFRIGEREE PRODUCTION"),
    ("faconneuse", "FACONNEUSE PRODUCTION"),
    ("enfourneur", "ENFOURNEUR PRODUCTION"),
    (r"\bfours?\b|four ventil|four electr|four a sole|four rotatif|four mixte", "FOUR DE PRODUCTION"),
    ("plaque induction|plaque a induction", "PLAQUE A INDUCTION PRODUCTION"),
    ("blender", "BLENDER PRODUCTION"),
    ("friteuse", "FRITEUSE PRODUCTION"),
    ("etagere|rayonnage", "RAYONNAGE INOX PRODUCTION"),
    ("table inox|table de travail", "TABLE INOX PRODUCTION"),
    (r"\bclim", "CLIMATISATION LABORATOIRE DE PRODUCTION"),
    (r"\bgrilles?\b", "GRILLES INOX PRODUCTION"),
    (r"\bcaisses?\b|\bbacs?\b", "CAISSES ET BACS DE STOCKAGE PRODUCTION"),
    ("ph-metre|ph metre|phmetre", "KIT PH-METRE PRODUCTION"),
]
