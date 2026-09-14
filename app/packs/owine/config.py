"""OWINE — constantes métier."""
COMPANY = "OWINE"
LMB = "LMB"                                   # La Mémoire de Bourgogne SARL (dépôt-vente)

LOCATIONS = {"ALIX": "Entrepôt Alix Transport, Beaune", "CHAUX": "La Mémoire de Bourgogne, rue de Chaux"}
OWNERS = {"OWINE": "OWINE SAS", "LMB": "La Mémoire de Bourgogne (dépôt-vente)"}

# emballages (références Chronopost Viti) — suivis comme des articles, lieu ALIX, propriétaire OWINE
PACKAGING = {
    "2030": {"title": "Carton 2 btl. Réf. 2030", "bottles": 2},
    "2031": {"title": "Carton 1 btl. Réf. 2031", "bottles": 1},
    "2033": {"title": "Carton 3 btl. Réf. 2033", "bottles": 3},
    "2036": {"title": "Carton 6 btl. Réf. 2036", "bottles": 6},
    "8025": {"title": "Feuille A4 adhésive Réf. 8025", "bottles": 0},
    "2010": {"title": "Pochette Chronopost Réf. 2010", "bottles": 0},
}
BOX_FOR = {6: "2036", 3: "2033", 1: "2031"}      # carton choisi selon le nombre de bouteilles qu'il contient
LABEL_SHEET = "8025"                            # une feuille adhésive par carton

BOTTLE_KG = 1.5                                 # règle JS : 1,5 kg par bouteille de 75 cl (carton compris)
MAX_BOTTLES_PER_BOX = 6

MOVE_KINDS = {
    "initial": "Stock initial", "purchase": "Achat (entrée)", "deposit_out": "Dépôt-vente : sortie rue de Chaux", "deposit_in": "Dépôt-vente : entrée chez Alix",
    "sale": "Vente (expédition)", "pickup": "Vente (retrait sur place)", "adjustment": "Ajustement", "inventory": "Inventaire",
    "packaging_in": "Réception d'emballages", "packaging_out": "Consommation d'emballages", "return": "Retour",
}
ORDER_STATUS = {
    "a_traiter": ("À traiter", "warning"), "cartons": ("Cartons validés", "info"), "etiquettes": ("Étiquettes reçues", "info"),
    "envoye": ("Envoyé à Alix et au client", "primary"), "attente_reception": ("En attente de confirmation de réception", "secondary"),
    "a_facturer": ("Réception confirmée · facture à valider dans Pennylane", "info"),
    "cloturee": ("Clôturée (livrée et facturée)", "success"), "annulee": ("Annulée", "dark"),
}
MODES = {"chronopost": "Chronopost", "retrait": "Retrait chez Alix", "manuel": "À qualifier"}
TASK_KINDS = {
    "reception": "Confirmer la réception avec le client", "lmb_invoice": "Facture LMB → OWINE à établir (dépôt-vente)",
    "pennylane_invoice": "Facture client Pennylane à vérifier et valider", "packaging": "Réception d'emballages à confirmer",
    "cost_missing": "Coût d'achat manquant", "stock": "Écart de stock à vérifier", "other": "Autre",
}
ALIX_EMAIL = "beaune@transportsalix.com"
ALIX_CC = ["remi.sery@gmail.com"]
ALIX_ADDRESS = "OWINE chez ALIX TRANSPORT, 6 Rue JF Champollion, 21200 BEAUNE"
DIGEST_TO = ["js@owine.co"]

OWINE_ADDRESS = {"name": "OWINE SAS", "address1": "Parc d'activité", "address2": "14 E rue Coubertin", "zip": "21000", "city": "DIJON", "country": "FR", "email": "contact@owine.co"}
CONTACT_EMAIL = "contact@owine.co"            # adresse affichée aux clients (documents, e-mails)
MOTTO = "L'amitié & l'émotion"
GMAIL_USER = "js@owine.co"                    # boîte dans laquelle les brouillons sont déposés
GMAIL_PASSWORD_ENV = "GMAIL_JS_AT_OWINE_CO_APPPWD"   # variable Render créée par JS (mot de passe d'application)
