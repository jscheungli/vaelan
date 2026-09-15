"""OWINE — export : zones, règles par pays (guide export Chrono Viti 2024, fiches pays), zoning et délais Chronopost,
produits, codes douaniers. Source : dossier Dropbox « OWINE SAS (FR)/CHRONO VITI/20250320 - DOCS » (guide-export-chronoviti.pdf,
kit de démarrage, « Bien remplir ses factures pour douane », zoning Europe / Export, mémo Viti BtoC).
Les fiches pays du guide sont des calques Acrobat : lues une à une le 15/09/2026 ; « n.c. » = non communiqué dans le guide."""

# ---------------------------------------------------------------- zones
EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
FR_FISCAL = {"FR", "MC"}                                            # territoire fiscal français : pas d'export, pas de formalités
DROM = {"GP", "MQ", "GF", "RE", "YT", "BL", "MF", "PM", "NC", "PF", "WF"}   # hors territoire fiscal de l'UE : documents douaniers
# destinations interdites Chrono Viti (guide export 2024, p. 7)
FORBIDDEN = {"AF", "SA", "BY", "KP", "CU", "HT", "IR", "MM", "RU", "RW", "SD", "TJ", "TD", "TM", "UA", "VE", "YE"}

COUNTRY_NAMES = {"FR": "France", "MC": "Monaco", "CH": "Suisse", "GB": "Royaume-Uni", "NO": "Norvège", "IS": "Islande", "LI": "Liechtenstein", "US": "États-Unis", "CA": "Canada",
                 "JP": "Japon", "KR": "Corée du Sud", "CN": "Chine", "HK": "Hong Kong", "SG": "Singapour", "AU": "Australie", "NZ": "Nouvelle-Zélande", "AE": "Émirats arabes unis",
                 "IL": "Israël", "MY": "Malaisie", "TW": "Taïwan", "TH": "Thaïlande", "ZA": "Afrique du Sud", "MX": "Mexique", "AR": "Argentine", "CL": "Chili", "CO": "Colombie",
                 "PE": "Pérou", "KH": "Cambodge", "BR": "Brésil", "IN": "Inde", "AD": "Andorre",
                 "AT": "Autriche", "BE": "Belgique", "BG": "Bulgarie", "HR": "Croatie", "CY": "Chypre", "CZ": "République tchèque", "DK": "Danemark", "EE": "Estonie", "FI": "Finlande",
                 "DE": "Allemagne", "GR": "Grèce", "HU": "Hongrie", "IE": "Irlande", "IT": "Italie", "LV": "Lettonie", "LT": "Lituanie", "LU": "Luxembourg", "MT": "Malte", "NL": "Pays-Bas",
                 "PL": "Pologne", "PT": "Portugal", "RO": "Roumanie", "SK": "Slovaquie", "SI": "Slovénie", "ES": "Espagne", "SE": "Suède",
                 "GP": "Guadeloupe", "MQ": "Martinique", "GF": "Guyane", "RE": "La Réunion", "YT": "Mayotte", "BL": "Saint-Barthélemy", "MF": "Saint-Martin", "PM": "Saint-Pierre-et-Miquelon",
                 "NC": "Nouvelle-Calédonie", "PF": "Polynésie française", "WF": "Wallis-et-Futuna"}
COUNTRY_NAMES_EN = {"FR": "France", "CH": "Switzerland", "GB": "United Kingdom", "NO": "Norway", "IS": "Iceland", "US": "United States", "CA": "Canada", "JP": "Japan", "KR": "South Korea",
                    "CN": "China", "HK": "Hong Kong", "SG": "Singapore", "AU": "Australia", "NZ": "New Zealand", "AE": "United Arab Emirates", "IL": "Israel", "MY": "Malaysia", "TW": "Taiwan",
                    "TH": "Thailand", "ZA": "South Africa", "MX": "Mexico", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru", "KH": "Cambodia", "BR": "Brazil", "IN": "India",
                    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia", "CY": "Cyprus", "CZ": "Czech Republic", "DK": "Denmark", "EE": "Estonia", "FI": "Finland", "DE": "Germany",
                    "GR": "Greece", "HU": "Hungary", "IE": "Ireland", "IT": "Italy", "LV": "Latvia", "LT": "Lithuania", "LU": "Luxembourg", "MT": "Malta", "NL": "Netherlands", "PL": "Poland",
                    "PT": "Portugal", "RO": "Romania", "SK": "Slovakia", "SI": "Slovenia", "ES": "Spain", "SE": "Sweden", "GP": "Guadeloupe", "MQ": "Martinique", "GF": "French Guiana",
                    "RE": "Réunion", "YT": "Mayotte", "MC": "Monaco", "AD": "Andorra", "LI": "Liechtenstein"}


def country_name(code: str, lang: str = "fr") -> str:
    code = (code or "").upper()
    if lang == "en":
        return COUNTRY_NAMES_EN.get(code) or COUNTRY_NAMES.get(code) or code
    return COUNTRY_NAMES.get(code) or code


# ---------------------------------------------------------------- produits Chronopost (codes du contrat)
PRODUCTS = {
    "classic": {"code": "44", "label": "Chrono Classic", "mode": "Route / Road", "desc": "transport routier, 27 pays et territoires d'Europe, 2 à 4 jours ouvrés (marchandises et particuliers)"},
    "express": {"code": "17", "label": "Chrono Express", "mode": "Avion / Air", "desc": "transport aérien, 230 pays et territoires, 1 à 3 jours ouvrés dans les grands centres, 2 à 6 ailleurs"},
    "chrono13": {"code": "01", "label": "Chrono 13", "mode": "Route / Road", "desc": "France métropolitaine et Monaco, livraison le lendemain avant 13 h"},
}
INCOTERMS = {"DAP": "DAP — Delivered At Place : transport payé par oWine, droits et taxes à l'import payés par le destinataire",
             "DDP": "DDP — Delivered Duty Paid : droits et taxes avancés par Chronopost et refacturés à oWine (compte à ouvrir à cet incoterm avec le chargé d'affaires)"}

# ---------------------------------------------------------------- règles par pays (fiches pays du guide export Chrono Viti 2024)
# b2b / b2c : {"classic": délai | None (interdit) | "n.c.", "express": idem, "max_bottles": par envoi (Classic), "max_kg": par envoi, "notes": [...]}
# customs : documents douaniers (facture commerciale en 3 exemplaires) ; invoice_desc : mentions exigées dans la description


def _r(classic, express, **kw):
    d = {"classic": classic, "express": express}
    d.update(kw)
    return d


COUNTRY_RULES = {
    # ---- Union européenne : lettre de transport seule (pas de douane) ; accises et TVA du pays de destination à traiter en amont
    "DE": {"b2b": _r("J+2/3", "J+1", max_bottles=6), "b2c": _r("J+2/3", "J+1", max_bottles=6), "notes": ["Büsingen et Helgoland : formalités douanières"]},
    "AT": {"b2b": _r("J+3", "J+1/2"), "b2c": _r("J+3", "J+1/2")},
    "BE": {"b2b": _r("J+2", "J+1", max_bottles=6), "b2c": _r("J+2", "J+1", max_bottles=6)},
    "BG": {"b2b": _r("J+5", "J+1/2", max_bottles=6), "b2c": _r("J+5", "J+1/2", max_bottles=6)},
    "HR": {"b2b": _r("J+4/5", "J+1/3"), "b2c": _r("J+4/5", "J+1/3")},
    "CZ": {"b2b": _r(None, "J+1/2"), "b2c": _r(None, "J+1/2")},
    "DK": {"b2b": _r(None, "J+1/2"), "b2c": _r(None, "J+1/2"), "notes": ["Îles Féroé et Groenland : formalités douanières (fiche pays Danemark)"]},
    "EE": {"b2b": _r(None, "J+1/2"), "b2c": _r(None, "J+1/2")},
    "FI": {"b2b": _r(None, "J+1/3"), "b2c": _r(None, "J+1/3"), "notes": ["Îles Åland : formalités douanières"]},
    "GR": {"b2b": _r("J+6", "J+1/4"), "b2c": _r("J+6", "J+1/4"), "notes": ["Mont Athos : formalités douanières"]},
    "HU": {"b2b": _r("J+3/4", "J+1/2"), "b2c": _r("J+3/4", "J+1/2")},
    "IE": {"b2b": _r("J+3/4", "J+1/2"), "b2c": _r("J+3/4", "J+1/2")},
    "IT": {"b2b": _r("J+2/3", "J+1/3"), "b2c": _r("J+3", "n.c."), "notes": ["Campione d'Italia, Livigno, Saint-Marin, Vatican : formalités douanières"]},
    "LV": {"b2b": _r(None, "J+1/2"), "b2c": _r(None, "J+1/2")},
    "LT": {"b2b": _r(None, "J+1/2"), "b2c": _r(None, "J+1/2")},
    "LU": {"b2b": _r("J+2", "J+1", max_bottles=6), "b2c": _r("J+2", "J+1", max_bottles=6)},
    "NL": {"b2b": _r("J+2", "J+1/2"), "b2c": _r("J+2", "J+1/2", max_bottles=6)},
    "PL": {"b2b": _r("J+3", "J+1/2"), "b2c": _r("J+2", "J+1/2")},
    "PT": {"b2b": _r("J+2/3", "J+1/2"), "b2c": _r("J+2/3", "J+1/2")},
    "RO": {"b2b": _r("J+4", "J+1/3"), "b2c": _r("J+4", "J+1/3")},
    "SK": {"b2b": _r(None, "J+1/2"), "b2c": _r(None, "J+1/2")},
    "SI": {"b2b": _r("J+3/4", "J+1/2", max_bottles=12), "b2c": _r("J+3/4", "J+1/2", max_bottles=12)},
    "ES": {"b2b": _r("J+2/3", "J+1/2"), "b2c": _r("J+2/3", "J+1/2"), "notes": ["Canaries, Ceuta, Melilla : formalités douanières (7 % IGIC, 10 % AIEM)"]},
    "SE": {"b2b": _r(None, "J+1/4", notes=["Uniquement entre entrepositaires agréés, licence d'importation nécessaire (monopole)"]),
           "b2c": _r(None, None, notes=["Particuliers : interdit hors cadeaux de fin d'année entre particuliers (1 bouteille, destinataire de plus de 20 ans)"])},
    "CY": {"b2b": _r("n.c.", "n.c."), "b2c": _r("n.c.", "n.c."), "notes": ["Pas de fiche pays dans le guide Viti : à confirmer avec Chronopost"]},
    "MT": {"b2b": _r("n.c.", "n.c."), "b2c": _r("n.c.", "n.c."), "notes": ["Pas de fiche pays dans le guide Viti : à confirmer avec Chronopost"]},
    # ---- Europe hors UE : douane (facture commerciale ×3)
    "CH": {"customs": True, "b2b": _r("J+2/4", "J+1/2", max_bottles=6, max_kg=10), "b2c": _r("J+2/4", None, max_bottles=6, max_kg=10),
           "invoice_desc": ["type de boisson", "degré d'alcool", "nombre de bouteilles", "quantité (contenance)", "origine"],
           "notes": ["Particuliers : Chrono Classic seulement, 6 bouteilles et 10 kg par envoi → une commande de plus de 6 bouteilles = plusieurs envois, chacun avec sa facture ×3",
                     "Chrono Express (pros) : préciser sur la facture si le vin est en CRD ou non ; certificat d'origine contrôlé",
                     "Droits et taxes suisses payés par le destinataire (DAP) : TVA 8,1 % sur marchandise + transport, droit de douane au litre, frais de dédouanement du transporteur"]},
    "GB": {"customs": True, "b2b": _r(None, "J+1/2"), "b2c": _r(None, "J+1/2"),
           "notes": ["Chrono Classic interdit (une solution en dédouanement existe : voir le chargé d'affaires) ; Chrono Express seul",
                     "Produits soumis à accise : la règle des 135 £ (TVA collectée par le vendeur) ne s'applique pas ; droits d'accise (≈ 3 £ par bouteille à 13 %) et TVA 20 % payés par le destinataire à l'arrivée (DAP)",
                     "Destinataire société : EORI GB au minimum, n° de TVA GB si possible"]},
    "IS": {"customs": True, "b2b": _r(None, "J+2/3"), "b2c": _r(None, "J+2/3"), "invoice_desc": ["type de boisson", "degré d'alcool", "nombre de bouteilles", "quantité par bouteille", "origine"],
           "notes": ["Facture commerciale uniquement (pas de pro forma)"]},
    "NO": {"customs": True, "b2b": _r("n.c.", "n.c."), "b2c": _r("n.c.", "n.c."),
           "notes": ["Pas de fiche pays dans le guide Viti (zoning : Classic zone 4 J+5, Express zone 4 J+1) : monopole Vinmonopolet, importation privée réglementée → à confirmer avec Chronopost avant d'ouvrir"]},
    "LI": {"customs": True, "b2b": _r("n.c.", "n.c."), "b2c": _r("n.c.", "n.c."), "notes": ["Union douanière avec la Suisse : règles suisses a priori, à confirmer"]},
    "AD": {"customs": True, "b2b": _r("n.c.", "n.c."), "b2c": _r("n.c.", "n.c."), "notes": ["Pas de fiche pays : à confirmer"]},
    # ---- DROM-COM : hors territoire fiscal de l'UE, facture ×3, Chrono Express
    "GP": {"customs": True, "b2b": _r(None, "J+2/4", notes=["indiquer le n° de TVA intracommunautaire du destinataire"]), "b2c": _r(None, "J+2/4"),
           "invoice_desc": ["type de boisson", "degré d'alcool", "nombre de bouteilles", "quantité", "origine"]},
    "MQ": {"customs": True, "b2b": _r(None, "J+2", notes=["indiquer le n° de TVA intracommunautaire du destinataire"]), "b2c": _r(None, "J+2"),
           "invoice_desc": ["type de boisson", "degré d'alcool", "nombre de bouteilles", "quantité", "origine"]},
    "RE": {"customs": True, "b2b": _r(None, "J+2/3", notes=["indiquer le n° de TVA intracommunautaire du destinataire"]), "b2c": _r(None, "J+2/3"),
           "invoice_desc": ["type de boisson", "degré d'alcool", "nombre de bouteilles", "quantité", "origine"]},
    "GF": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Voir la fiche pays Guyane"]},
    "YT": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Voir la fiche pays Mayotte"]},
    "BL": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Voir la fiche pays Saint-Barthélemy"]},
    "MF": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Voir la fiche pays Saint-Martin"]},
    "PM": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Voir la fiche pays Saint-Pierre-et-Miquelon"]},
    # ---- Amériques
    "US": {"customs": True,
           "b2b": _r(None, "J+2", max_bottles=3, notes=["3 bouteilles du même type, 50 kg, 3 000 $ maximum", "expéditeur entrepositaire agréé (ce qu'oWine n'est pas) ; destinataire titulaire du Basic Permit ATF ; COLA"]),
           "b2c": _r(None, "J+4", notes=["Avenant « Chrono Viti B2C US » à signer", "facture commerciale envoyée par e-mail à Chronopost pour contrôle (pas jointe au colis)",
                                         "vins vérifiés dans la liste TTB (sinon PDF des étiquettes, 10 à 15 jours) ; Chronopost édite le COLA et la Prior Notice"])},
    "CA": {"customs": True, "b2b": _r(None, "J+2/4", notes=["expéditeur et importateur agréés par la commission provinciale ; licence d'importation"]),
           "b2c": _r(None, None, notes=["envois à un particulier interdits (sauf Colombie-Britannique, Alberta, Québec, Ontario avec accord d'exportateur agréé)"])},
    "MX": {"customs": True, "b2b": _r(None, "J+3/6", notes=["RFC du destinataire sur la facture ; Padrón de importadores au-delà de 1 000 $ ; licence MARBETE"]), "b2c": _r(None, "n.c.")},
    "AR": {"customs": True, "b2b": _r(None, "J+3/5", max_bottles=3, notes=["3 bouteilles du même type, 50 kg, 3 000 $ ; n° de TVA du destinataire ; licence d'importation"]), "b2c": _r(None, None, notes=["cadeaux d'alcool interdits"])},
    "CL": {"customs": True, "b2b": _r(None, "J+4/6", notes=["licence d'importation du destinataire"]), "b2c": _r(None, None)},
    "CO": {"customs": True, "b2b": _r(None, "J+3/5", notes=["enregistrement sanitaire INVIMA ; 2 000 $ maximum"]), "b2c": _r(None, "n.c.")},
    "PE": {"customs": True, "b2b": _r(None, "J+4/7", notes=["2 000 $ maximum"]), "b2c": _r(None, "n.c.")},
    "BR": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Pas de fiche pays Viti : à confirmer (CPF du destinataire exigé au Brésil)"]},
    # ---- Asie, Océanie, Afrique
    "JP": {"customs": True, "b2b": _r(None, "J+4/5", notes=["notification d'importation de denrées alimentaires par l'importateur ; emballages approuvés ; frais d'inspection"]), "b2c": _r(None, None)},
    "KR": {"customs": True, "b2b": _r(None, "J+3/6", notes=["400 $ maximum ; certificats (registre, sanitaire, origine, quarantaine) ; n° fiscal du destinataire si > 100 $ ; étiquettes en coréen"]), "b2c": _r(None, None)},
    "CN": {"customs": True, "b2b": _r(None, "J+3/6", notes=["licence d'importation ; certificat d'origine et sanitaire ; taxe 29 %"]), "b2c": _r(None, None)},
    "AU": {"customs": True, "b2b": _r(None, "J+4/6", notes=["licence d'importation du destinataire ; taxe 29 %"]), "b2c": _r(None, None)},
    "TW": {"customs": True, "b2b": _r(None, "J+2/5", notes=["licence si > 5 L ou > 1 000 $ ; année, couleur, cépage, degré sur la facture ; COA possible"]), "b2c": _r(None, None)},
    "TH": {"customs": True, "b2b": _r(None, "J+3/5", notes=["licence d'importation d'alcool ; n° fiscal du destinataire"]), "b2c": _r(None, None)},
    "NZ": {"customs": True, "b2b": _r(None, "J+4/7", notes=["licence d'importation du destinataire"]), "b2c": _r(None, None)},
    "ZA": {"customs": True, "b2b": _r(None, "J+3/5", notes=["permis importateur (Liquor Products) ; EUR.1 et certificat d'origine au-delà de 6 000 €"]),
           "b2c": _r(None, "J+3/5", max_bottles=2, notes=["2 bouteilles, 2 L et 24 € de valeur maximum ; copie de pièce d'identité et permis du ministère de l'Agriculture"])},
    "KH": {"customs": True, "b2b": _r(None, "J+4/8", notes=["licence d'importation"]), "b2c": _r(None, None)},
    # ---- pays présents dans la zone « International » Shopify mais absents du guide Viti
    "HK": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Pas de fiche pays Viti : à confirmer avec Chronopost avant d'ouvrir"]},
    "SG": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Pas de fiche pays Viti : à confirmer"]},
    "MY": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Pas de fiche pays Viti : à confirmer"]},
    "IL": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, "n.c."), "notes": ["Pas de fiche pays Viti : à confirmer"]},
    "AE": {"customs": True, "b2b": _r(None, "n.c."), "b2c": _r(None, None), "notes": ["Alcool : importation par un particulier interdite sans licence → retirer des pays livrés"]},
}

# pays où un identifiant fiscal du particulier est exigé sur la facture (fiches pays / pratique douanière)
TAX_ID_B2C = {"KR": "n° d'identification fiscale (PCCC)", "BR": "CPF", "TW": "n° d'identité", "TH": "n° d'identification fiscale", "MX": "RFC", "AR": "n° de TVA / CUIT", "CN": "n° de carte d'identité",
              "ZA": "n° de pièce d'identité", "NO": "n° d'identité (D-number)", "IS": "kennitala"}

# ---------------------------------------------------------------- douane : classement tarifaire (nomenclature combinée UE)
HS_GENERIC = "220421"                        # vins en récipients ≤ 2 l — code SH à 6 chiffres, valable partout
CN_BY_COLOUR = {"blanc": "22042113", "rouge": "22042143", "rosé": "22042143", "rose": "22042143"}   # NC UE : vins AOP de Bourgogne ≤ 15 % vol, blanc / autres (rouge, rosé)
ORIGIN_DEFAULT = "FR"
NET_KG_PER_BOTTLE = 1.2                      # bouteille pleine 75 cl sans emballage (poids net déclaré ; brut = 1,5 kg avec emballage Chrono Viti)
EUR1_THRESHOLD = 6000.0                      # au-delà : certificat EUR.1 au lieu de la déclaration d'origine sur facture

ORIGIN_DECLARATION_FR = ("L'exportateur des produits couverts par le présent document déclare que, sauf indication claire du contraire, "
                         "ces produits ont l'origine préférentielle UE.")
ORIGIN_DECLARATION_EN = ("The exporter of the products covered by this document declares that, except where otherwise clearly indicated, "
                         "these products are of EU preferential origin.")
VAT_EXEMPTION_EXPORT = "Exonération de TVA — article 262 I du CGI (livraison à l'exportation) / VAT exempt: export supply (art. 262 I French Tax Code)"
FINAL_USE = {"societe": "Marchandises destinées à la vente / Final use: resale",
             "particulier": "Vente à un particulier pour son usage personnel, non destinée à la revente / Sold to a private individual for personal use, not for resale"}

# ---------------------------------------------------------------- identité de l'exportateur (compléments réglables dans Réglages douane : EORI, SIRET, capital, signataire)
EXPORTER = {"name": "OWINE SAS", "contact": "Jean-Sébastien CHEUNG-AH-SEUNG", "vat": "FR94928409887", "siren": "928409887", "rcs": "RCS Dijon 928 409 887",
            "phone": "+33 7 84 50 86 19", "email": "contact@owine.co", "eori": "", "siret": "", "capital": "", "sign_place": "Dijon"}

# dimensions (cm) des cartons Chrono Viti, à mesurer une fois pour toutes (Réglages douane) — exigées sur chronopost.fr pour l'international

# ---------------------------------------------------------------- zoning Chronopost (zone tarifaire, meilleur délai en jours ouvrés) — PDF « Zoning Europe » (Chrono Classic) et « Zoning Export » (Chrono Express)
ZONING = {
    "classic": {
        "AT": ("Autriche", 2, 3),
        "BE": ("Belgique", 1, 2),
        "BG": ("Bulgarie", 3, 4),
        "CH": ("Suisse", 4, 2),
        "CZ": ("République Tchèque", 3, 3),
        "DE": ("Allemagne", 1, 2),
        "DK": ("Danemark", 2, 3),
        "EE": ("Estonie", 3, 4),
        "ES": ("Espagne", 2, 2),
        "FI": ("Finlande", 2, 4),
        "GB": ("Royaume-Uni", 2, 2),
        "GR": ("Grèce", 2, 6),
        "HR": ("Croatie", 3, 4),
        "HU": ("Hongrie", 3, 3),
        "IE": ("Irlande", 2, 3),
        "IT": ("Italie", 2, 2),
        "LI": ("Liechtenstein", 4, 2),
        "LT": ("Lituanie", 3, 4),
        "LU": ("Luxembourg", 1, 2),
        "LV": ("Lettonie", 3, 4),
        "NL": ("Pays-Bas", 1, 2),
        "NO": ("Norvège", 4, 5),
        "PL": ("Pologne", 3, 3),
        "PT": ("Portugal", 2, 2),
        "RO": ("Roumanie", 3, 4),
        "SE": ("Suède", 2, 3),
        "SI": ("Slovénie", 3, 3),
        "SK": ("Slovaquie", 3, 3),
    },
    "express": {
        "AD": ("Andorre", 4, 1),
        "AE": ("Emirats Arabes Unis", 6, 2),
        "AF": ("Afghanistan", 7, 5),
        "AG": ("Antigua et Barbuda", 5, 3),
        "AI": ("Anguilla", 5, 3),
        "AL": ("Albanie", 4, 2),
        "AM": ("Arménie", 4, 2),
        "AN": ("Antilles néerlandaises", 5, 3),
        "AO": ("Angola", 6, 7),
        "AR": ("Argentine", 5, 2),
        "AS": ("Iles Samoa (US)", 7, 5),
        "AT": ("Autriche", 2, 1),
        "AU": ("Australie", 7, 3),
        "AW": ("Aruba", 5, 3),
        "AZ": ("Azerbaïdjan", 6, 2),
        "BA": ("Bosnie", 4, 2),
        "BB": ("Barbade", 5, 3),
        "BD": ("Bangladesh", 7, 3),
        "BE": ("Belgique", 1, 1),
        "BF": ("Burkina Faso", 6, 2),
        "BG": ("Bulgarie", 3, 1),
        "BH": ("Bahrein", 6, 2),
        "BI": ("Burundi", 6, 5),
        "BJ": ("Bénin", 6, 4),
        "BM": ("Bermudes", 5, 3),
        "BN": ("Brunei", 7, 3),
        "BO": ("Bolivie", 5, 4),
        "BR": ("Brésil", 5, 2),
        "BS": ("Bahamas", 5, 2),
        "BT": ("Bhoutan", 7, 5),
        "BW": ("Botswana", 6, 3),
        "BY": ("Biélorussie", 4, 2),
        "BZ": ("Belize", 5, 2),
        "CA": ("Canada", 5, 2),
        "CC": ("Iles Cocos", 7, 4),
        "CD": ("démocratique du Congo", 6, 4),
        "CF": ("Centrafricaine", 6, 4),
        "CG": ("Congo", 6, 3),
        "CH": ("Suisse", 4, 1),
        "CI": ("Côte d'Ivoire", 6, 2),
        "CK": ("Iles Cook", 7, 5),
        "CL": ("Chili", 5, 2),
        "CM": ("Cameroun", 6, 3),
        "CN": ("Chine", 7, 3),
        "CO": ("Colombie", 5, 2),
        "CR": ("Costa Rica", 5, 3),
        "CU": ("Cuba", 5, 4),
        "CV": ("Cap vert", 6, 4),
        "CX": ("Iles Christmas", 7, 4),
        "CY": ("Chypre", 3, 3),
        "CZ": ("République Tchèque", 3, 1),
        "DE": ("Allemagne", 1, 1),
        "DJ": ("Djibouti", 6, 3),
        "DK": ("Danemark", 2, 1),
        "DM": ("Dominique", 5, 3),
        "DO": ("République Dominicaine", 5, 4),
        "DZ": ("Algérie", 6, 2),
        "EC": ("Equateur", 5, 3),
        "EE": ("Estonie", 3, 1),
        "EG": ("Egypte", 6, 2),
        "ER": ("Erythrée", 6, 5),
        "ES": ("Espagne", 2, 1),
        "ET": ("Ethiopie", 6, 5),
        "FI": ("Finlande", 2, 1),
        "FJ": ("Fidji", 7, 4),
        "FM": ("Micronésie", 7, 5),
        "FO": ("Iles Féroe", 4, 4),
        "GA": ("Gabon", 6, 3),
        "GB": ("Royaume-Uni", 2, 1),
        "GD": ("Grenade", 5, 3),
        "GE": ("Georgie", 4, 3),
        "GF": ("Guyane", 9, 2),
        "GH": ("Ghana", 6, 3),
        "GI": ("Gibraltar", 4, 2),
        "GL": ("Groenland", 5, 5),
        "GM": ("Gambie", 6, 4),
        "GN": ("Guinée", 6, 3),
        "GP": ("française", 8, 2),
        "GQ": ("Guinée Equatoriale", 6, 4),
        "GR": ("Grèce", 2, 1),
        "GS": ("Guernesey", 4, 2),
        "GT": ("Guatemala", 5, 3),
        "GU": ("Guam", 7, 4),
        "GW": ("Guinée Bissau", 6, 6),
        "GY": ("Guyana", 5, 3),
        "HK": ("Hong Kong", 7, 2),
        "HN": ("Honduras", 5, 3),
        "HR": ("Croatie", 3, 2),
        "HT": ("Haïti", 5, 2),
        "HU": ("Hongrie", 3, 1),
        "IC": ("Iles Canaries", 4, 2),
        "ID": ("Indonésie", 7, 2),
        "IE": ("Irlande", 2, 1),
        "IL": ("Israël", 6, 2),
        "IN": ("Inde", 7, 2),
        "IQ": ("Iraq", 6, 5),
        "IR": ("Iran", 6, 3),
        "IS": ("Islande", 4, 1),
        "IT": ("Italie", 2, 1),
        "JE": ("Jersey", 4, 3),
        "JM": ("Jamaïque", 5, 2),
        "JO": ("Jordanie", 6, 2),
        "JP": ("Japon", 7, 3),
        "KE": ("Kenya", 6, 3),
        "KG": ("Kirghizistan", 7, 4),
        "KH": ("Cambodge", 7, 3),
        "KI": ("Iles Kiribati", 7, 5),
        "KM": ("Comores", 6, 4),
        "KP": ("Corée du Nord", None, None),
        "KR": ("Corée du sud", 7, 2),
        "KW": ("Koweït", 6, 3),
        "KY": ("Iles Cayman", 5, 2),
        "KZ": ("Kazakhstan", 7, 2),
        "LA": ("Laos", 7, 4),
        "LB": ("Liban", 6, 2),
        "LC": ("Saint Lucia", 5, 3),
        "LI": ("Liechtenstein", 4, 1),
        "LK": ("Skri Lanka", 7, 3),
        "LR": ("Liberia", 6, 4),
        "LS": ("Lesotho", 6, 3),
        "LT": ("Lituanie", 3, 1),
        "LU": ("Luxembourg", 1, 1),
        "LV": ("Lettonie", 3, 1),
        "LY": ("Lybie", 6, 4),
        "MA": ("Maroc", 6, 1),
        "MD": ("Moldavie", 4, 2),
        "ME": ("Monténégro", 4, 4),
        "MG": ("Madagascar", 6, 3),
        "MH": ("Iles Marshall", 7, 4),
        "MI": ("Saint Martin (NL)", 8, 2),
        "MK": ("Macédoine", 4, 2),
        "ML": ("Mali", 6, 3),
        "MM": ("Myanmar", 7, 3),
        "MN": ("Mongolie", 7, 5),
        "MO": ("Macao", 7, 3),
        "MP": ("Iles Mariannes", 9, 5),
        "MQ": ("Martinique", 8, 2),
        "MR": ("Mauritanie", 6, 3),
        "MS": ("Monserrat", 5, 3),
        "MT": ("Malte", 3, 2),
        "MU": ("Ile Maurice", 6, 3),
        "MV": ("Maldives", 7, 4),
        "MX": ("Mexique", 5, 2),
        "MY": ("Malaisie", 7, 2),
        "MZ": ("Mozambique", 6, 4),
        "NA": ("Namibie", 6, 3),
        "NC": ("Nouvelle-Calédonie", 9, 3),
        "NE": ("Niger", 6, 3),
        "NF": ("Iles Norfolk", 7, 4),
        "NG": ("Nigéria", 6, 2),
        "NI": ("Nicaragua", 5, 3),
        "NL": ("Pays-Bas", 1, 1),
        "NO": ("Norvège", 4, 1),
        "NP": ("Népal", 7, 4),
        "NR": ("Nauru", 7, 6),
        "NZ": ("Nouvelle-Zélande", 7, 4),
        "OM": ("Oman", 6, 2),
        "PA": ("Panama", 5, 2),
        "PE": ("Pérou", 5, 3),
        "PF": ("Polynésie Française", 9, 2),
        "PG": ("Nouvelle Guinée", 7, 5),
        "PH": ("Philippines", 7, 2),
        "PK": ("Pakistan", 7, 3),
        "PL": ("Pologne", 3, 1),
        "PR": ("Puerto Rico", 5, 2),
        "PS": ("Palestine", 6, 5),
        "PT": ("Portugal", 2, 1),
        "PW": ("Palaos", 7, 6),
        "PY": ("Paraguay", 5, 3),
        "QA": ("Qatar", 6, 2),
        "RE": ("Réunion", 8, 2),
        "RO": ("Roumanie", 3, 2),
        "RS": ("Serbie", 4, 2),
        "RU": ("Russie", 4, 3),
        "RW": ("Rwanda", 6, 3),
        "SA": ("Arabie Saoudite", 6, 4),
        "SB": ("Iles Salomon", 7, 7),
        "SC": ("Seychelles", 6, 4),
        "SD": ("Soudan", 6, 4),
        "SE": ("Suède", 2, 1),
        "SG": ("Singapour", 7, 2),
        "SI": ("Slovénie", 3, 1),
        "SK": ("Slovaquie", 3, 1),
        "SL": ("Sierra Leone", 6, 4),
        "SM": ("San Marin", 4, 1),
        "SN": ("Sénégal", 6, 3),
        "SO": ("Somalie", 6, 4),
        "SR": ("Surinam", 5, 3),
        "ST": ("Sao Tome et Principe", 6, 4),
        "SV": ("El Salvador", 5, 3),
        "SY": ("Syrie", 6, 2),
        "SZ": ("Swaziland", 6, 4),
        "TC": ("Turques et Caiques", 5, 3),
        "TD": ("Tchad", 6, 3),
        "TG": ("Togo", 6, 3),
        "TH": ("Thaïlande", 7, 3),
        "TJ": ("Tadjikistan", 7, 3),
        "TL": ("Timor", 7, 5),
        "TM": ("Turkmenistan", None, None),
        "TN": ("Tunisie", 6, 2),
        "TO": ("Tonga", 7, 4),
        "TR": ("Turquie", 4, 2),
        "TT": ("Trinité et Tobago", 5, 3),
        "TV": ("Tuvalu", 7, 6),
        "TW": ("Taïwan", 7, 3),
        "TZ": ("Tanzanie", 6, 4),
        "UA": ("Ukraine", 4, 2),
        "UG": ("Ouganda", 6, 3),
        "US": ("Etats-Unis", 5, 1),
        "UY": ("Uruguay", 5, 3),
        "UZ": ("Ouzbékistan", 7, 3),
        "VA": ("Vatican", 4, 1),
        "VC": ("et Grenadines", 5, 3),
        "VE": ("Venezuela", 5, 3),
        "VG": ("Iles Vierges (GB)", 5, 3),
        "VI": ("Iles Vierges (US)", 5, 3),
        "VN": ("Vietnam", 7, 3),
        "VU": ("Vanuatu", 7, 5),
        "WF": ("Wallis et Futuna", 9, 5),
        "WS": ("Samoa", 7, 5),
        "YE": ("Yémen", 6, 3),
        "YT": ("Mayotte", 9, 3),
        "ZA": ("Afrique du sud", 6, 2),
        "ZM": ("Zambie", 6, 4),
        "ZW": ("Zimbabwe", 6, 3),
    },
}


def zoning(product: str, country: str):
    """(nom, zone tarifaire, délai indicatif en jours ouvrés) ou None si non desservi / inconnu."""
    v = ZONING.get(product, {}).get((country or "").upper())
    return v if v and v[1] is not None else None


# ---------------------------------------------------------------- tarifs du contrat Chrono Viti Easy n° 84048903 (effet 01/04/2025), € HT par expédition, hors surcharge carburant
# tranches de poids (borne haute incluse, kg) ; au-delà de la dernière tranche : + prix par kg supplémentaire
TARIFF_BRACKETS_ROAD = [1, 3, 7, 12, 17, 22, 27, 28, 29, 30]
TARIFF_BRACKETS_AIR = [0.5, 1, 3, 7, 12, 17, 22, 27, 28, 29, 30, 31, 32, 33]
TARIFFS = {
    "chrono13": {"brackets": TARIFF_BRACKETS_ROAD, "zones": {"FR": ([11.29, 15.30, 18.40, 21.10, 26.73, 29.43, 37.75, 38.39, 39.03, 39.67], 0.64)}},
    "classic": {"brackets": TARIFF_BRACKETS_ROAD, "zones": {
        1: ([11.29, 15.30, 19.46, 24.28, 32.04, 36.86, 45.18, 45.82, 46.46, 47.10], 0.64),
        2: ([11.29, 15.30, 19.46, 24.28, 32.04, 36.86, 45.18, 45.82, 46.46, 47.10], 0.64),
        3: ([13.42, 18.50, 24.79, 31.74, 40.56, 47.51, 55.83, 56.89, 57.95, 59.01], 1.06),
        4: ([11.29, 15.30, 19.46, 24.28, 32.04, 36.86, 51.56, 52.20, 52.84, 53.48], 0.64)}},
    "express": {"brackets": TARIFF_BRACKETS_AIR, "zones": {
        1: ([12.35, 12.35, 19.55, 27.96, 42.35, 60.75, 74.08, 108.99, 111.12, 113.25, 115.38, 117.51, 119.64, 121.77], 2.13),
        2: ([12.35, 12.35, 19.55, 27.96, 42.35, 60.75, 74.08, 108.99, 111.12, 113.25, 115.38, 117.51, 119.64, 121.77], 2.13),
        3: ([16.61, 16.61, 25.94, 39.67, 63.64, 83.10, 106.00, 140.91, 143.56, 146.21, 148.86, 151.51, 154.16, 156.81], 2.65),
        4: ([23.54, 23.54, 30.74, 43.41, 68.44, 87.90, 110.80, 145.71, 148.69, 151.67, 154.65, 157.63, 160.61, 163.59], 2.98),
        5: ([26.73, 26.73, 40.31, 61.49, 89.71, 125.12, 153.34, 188.25, 192.19, 196.13, 200.07, 204.01, 207.95, 211.89], 3.94),
        6: ([26.73, 26.73, 41.38, 66.81, 95.03, 130.44, 158.66, 193.57, 197.82, 202.07, 206.32, 210.57, 214.82, 219.07], 4.25),
        7: ([26.73, 26.73, 42.44, 68.93, 100.34, 135.75, 163.97, 198.88, 203.13, 207.38, 211.63, 215.88, 220.13, 224.38], 4.25),
        8: ([23.67, 23.67, 31.94, 54.18, 79.21, 103.99, 116.26, 151.17, 154.57, 157.97, 161.37, 164.77, 168.17, 171.57], 3.40),
        9: ([32.05, 32.05, 49.89, 77.45, 110.99, 146.40, 179.94, 214.85, 220.17, 225.49, 230.81, 236.13, 241.45, 246.77], 5.32)}},
}
SUPPLEMENTS = {"groupage_classic": {1: 6.0, 2: 6.0, 3: 8.0, 4: 5.0},   # par colis supplémentaire d'une expédition Chrono Classic
               "customs_classic_zone4": 15.0,                            # par expédition Chrono Classic zone 4 (Suisse)
               "zone2_non_eu": 5.0,                                      # par expédition Classic / Express vers la zone 2 hors UE (Royaume-Uni)
               "eco": 0.18,                                              # participation éco-responsable par colis
               "export_declaration_ht": 17.5,                            # prestation de dédouanement (21 € TTC) par déclaration d'export — hypothèse à confirmer avec Chronopost
               "fuel_pct_default": 12.0}                                 # surcharge carburant (variable chaque mois, réglable dans Réglages)
# Alix Logistique — tarifs 2024 (V2 du 18/03/2024, « REMI SERY V2.pdf ») : ce qu'Alix facture par commande préparée
ALIX = {"prep_per_bottle": 0.10, "prep_min": 8.0, "dae_out": 15.0, "dae_extra_ref": 0.50, "ex_douane": 120.0, "storage_week_per_bottle": 0.033, "insurance_pct": 0.0008,
        "accise": {"BEAUNE_6": "FR 107859E0476", "BEAUNE_10": "FR 207859E0476", "CORPEAU": "FR 007859E0476"}}

# ---------------------------------------------------------------- zones de vente : périmètre ouvert par défaut (décision JS du 15/09/2026 : particuliers en Suisse ≤ 6 btl, sociétés en UE)
ZONES_DEFAULT = [
    {"key": "FR", "label": "France métropolitaine et Monaco", "countries": ["FR", "MC"], "product": "chrono13", "particulier": True, "societe": True, "max_bottles": None, "multiple": None, "min_order": None, "vat_required": False,
     "note": "circuit actuel : Chrono 13, grille de port au poids existante"},
    {"key": "CH", "label": "Suisse (Chrono Classic, douane)", "countries": ["CH", "LI"], "product": "classic", "particulier": True, "societe": False, "max_bottles": 6, "multiple": 6, "min_order": None, "vat_required": False,
     "note": "particuliers : 6 bouteilles et 10 kg par envoi (fiche pays), un carton de 6 par commande ; sociétés fermées pour l'instant"},
    {"key": "UE_OUEST", "label": "Union européenne — Ouest (Chrono Classic)", "countries": ["BE", "LU", "NL", "DE", "IT", "ES", "PT", "AT", "IE"], "product": "classic", "particulier": False, "societe": True, "max_bottles": 18, "multiple": 6, "min_order": None, "vat_required": True,
     "note": "sociétés avec n° de TVA intracommunautaire valide (VIES) ; particuliers fermés tant que l'OSS et un représentant fiscal ne sont pas en place"},
    {"key": "UE_EST", "label": "Union européenne — Nord et Est (Chrono Express)", "countries": ["DK", "FI", "EE", "LV", "LT", "CZ", "SK", "PL", "HU", "HR", "RO", "GR", "SI", "BG", "CY", "MT"], "product": "express", "particulier": False, "societe": False, "max_bottles": 18, "multiple": 6, "min_order": None, "vat_required": True,
     "note": "Chrono Classic interdit dans les pays nordiques et baltes : Chrono Express ; fermé au démarrage"},
    {"key": "GB", "label": "Royaume-Uni (Chrono Express, douane)", "countries": ["GB"], "product": "express", "particulier": False, "societe": False, "max_bottles": 12, "multiple": 6, "min_order": None, "vat_required": False,
     "note": "droits d'accise et TVA payés à l'arrivée (DAP) ; fermé au démarrage"},
    {"key": "US", "label": "États-Unis (Chrono Express, avenant Viti US)", "countries": ["US"], "product": "express", "particulier": False, "societe": False, "max_bottles": 12, "multiple": 6, "min_order": None, "vat_required": False,
     "note": "avenant Chrono Viti B2C US à signer ; fermé"},
]
BOX_DIMS_DEFAULT = {"2031": "", "2033": "", "2036": "38x28x40"}       # carton 6 bouteilles Chrono Viti : 38 × 28 × 40 cm, 1,258 kg (mesure JS)
INTL_BOX = "2036"                                                     # règle JS : à l'international, uniquement des cartons de 6
