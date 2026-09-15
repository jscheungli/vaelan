"""OWINE — constantes métier."""
COMPANY = "OWINE"
LMB = "LMB"                                   # La Mémoire de Bourgogne SARL (dépôt-vente)
LMB_COMPANY = "LAMEMOIREDEBOURGOGNE"          # code société Pennylane de LMB (factures dépôt-vente LMB → OWINE)
LMB_CUSTOMER = {"name": "OWINE SAS", "reg_no": "928409887", "vat_number": "FR94928409887", "emails": ["js@owine.co"], "external_reference": "OWINE", "payment_conditions": "30_days",
                "billing_address": {"address": "Parc d'activité, 14 E rue Coubertin", "postal_code": "21000", "city": "DIJON", "country_alpha2": "FR"}}
# fournisseurs Pennylane OWINE qui ne sont pas des vignerons (exclus du contrôle « facture sans entrée en stock »)
NON_WINE_SUPPLIERS = ("CHRONOPOST", "ALIX", "PENNYLANE", "SHOPIFY", "GOOGLE", "INPI", "INFOGREFFE", "LEGAL2DIGITAL", "ADMINISTRO", "NAMECHEAP", "ROSEAU", "OLINDA", "QONTO", "CENSEA", "MODULO",
                      "ARBELET", "FRAIS", "TRANSPORT", "PÉAGE", "PEAGE", "TAXI", "RESTAURANT", "PARKING", "HÔTEL", "HOTEL", "AUTRE", "FOURNISSEURS -")

LOCATIONS = {"ALIX": "Entrepôt Alix Transport, Beaune", "CHAUX": "La Mémoire de Bourgogne, rue de Chaux"}
OWNERS = {"OWINE": "OWINE SAS", "LMB": "La Mémoire de Bourgogne (dépôt-vente)"}

# emballages (références Chronopost Viti) — suivis comme des articles, lieu ALIX, propriétaire OWINE
PACKAGING = {   # kg = poids de l'emballage vide (kit de démarrage Chrono Viti) → poids net déclaré en douane = brut − emballage
    "2030": {"title": "Carton 2 btl. Réf. 2030", "bottles": 2, "kg": 0.405},
    "2031": {"title": "Carton 1 btl. Réf. 2031", "bottles": 1, "kg": 0.277},
    "2033": {"title": "Carton 3 btl. Réf. 2033", "bottles": 3, "kg": 0.714},
    "2036": {"title": "Carton 6 btl. Réf. 2036", "bottles": 6, "kg": 1.258},
    "8025": {"title": "Feuille A4 adhésive Réf. 8025", "bottles": 0, "kg": 0},
    "2010": {"title": "Pochette Chronopost Réf. 2010", "bottles": 0, "kg": 0},
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
    "cost_missing": "Coût d'achat manquant", "stock": "Écart de stock à vérifier", "purchase_pending": "Achat facturé, livraison à confirmer / à saisir", "other": "Autre",
    "customs": "Export : formalités douanières", "customs_info": "Export : informations à obtenir du client", "customs_data": "Export : données douanières des vins",
    "export_proof": "Export : justificatif d'exportation à archiver", "customs_setup": "Export : mise en place (à faire une fois)",
}
ALIX_EMAIL = "beaune@transportsalix.com"
ALIX_CC = ["remi.sery@gmail.com"]
CLIENT_CC = ["remi.sery@gmail.com"]         # Rémi en copie de chaque e-mail client (aperçu modifiable, envoi, zip)
ALIX_ADDRESS = "OWINE chez ALIX TRANSPORT, 6 Rue JF Champollion, 21200 BEAUNE"
DIGEST_TO = ["js@owine.co"]

OWINE_ADDRESS = {"name": "OWINE SAS", "address1": "Parc d'activité", "address2": "14 E rue Coubertin", "zip": "21000", "city": "DIJON", "country": "FR", "email": "contact@owine.co"}
CONTACT_EMAIL = "contact@owine.co"            # adresse affichée aux clients (documents, e-mails)
MOTTO = "L'amitié & l'émotion"
GMAIL_USER = "js@owine.co"                    # compte d'envoi SMTP des e-mails Alix / client
GMAIL_PASSWORD_ENV = "GMAIL_JS_AT_OWINE_CO_APPPWD"   # variable Render créée par JS (mot de passe d'application)
