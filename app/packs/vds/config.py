"""SCI Les Sables du Lagon (VDS) — villa en location saisonnière à La Réunion, gérée sous Lodgify.

Module « check-in voyageurs » : remplace les 3 formulaires SurveySparrow « VDS Terms & Conditions
Check » (Airbnb / Booking / Lodgify) par un formulaire hébergé dans Vaelan, décliné par canal,
avec suivi des réservations, invitations, relances et alertes. Réglages persistants : Setting
(company_code VDS, clés checkin:*), surchargés depuis la page de configuration.
"""

COMPANY_CODE = "VDS"

VILLA = {
    "name": "Villa des Sables du Lagon",
    "legal": "SCI LES SABLES DU LAGON",
    "rcs": "RCS Saint-Denis de La Réunion 821 948 163",
    "address": "6 ruelle Mazeau, 97400 Saint-Denis, La Réunion",
    "rgpd_email": "rgpd@villa-des-sables-du-lagon.com",
    "site": "https://www.villa-des-sables-du-lagon.com",
    "conditions_url": "https://www.villa-des-sables-du-lagon.com/fr/conditions-de-reservation",
    "conditions_url_en": "https://www.villa-des-sables-du-lagon.com/en/booking-conditions",
    "capacity": 12,
    "deposit": 1000,
    "checkin": "16:00 – 20:00",
    "checkout": "08:00 – 09:00",
    "lodgify_property_id": 495678,
    "color": "#234159",
}

# Canaux de réservation. id_required : pièce d'identité demandée (Airbnb vérifie déjà l'identité).
CHANNELS = {
    "airbnb":  {"label": "Airbnb",        "id_required": False, "platform": True},
    "booking": {"label": "Booking.com",   "id_required": True,  "platform": True},
    "abritel": {"label": "Abritel / Vrbo", "id_required": True, "platform": True},
    "lodgify": {"label": "Site direct",   "id_required": True,  "platform": False},
}

# Règles à reconnaître EXPLICITEMENT une à une (Oui / Non). `confirm` = case de confirmation
# supplémentaire (sanction) pour les règles dissuasives (fêtes, bruit).
RULES = [
    {"key": "horaires", "confirm": False,
     "fr": "Les horaires : arrivée entre 16h et 20h, départ entre 8h et 9h. En dehors de ces plages, il faut notre accord écrit préalable.",
     "en": "Times: check-in between 4 pm and 8 pm, check-out between 8 am and 9 am. Outside these windows, our prior written agreement is required."},
    {"key": "capacite", "confirm": False,
     "fr": "La capacité maximale est de 12 personnes, visiteurs compris. Aucune personne supplémentaire ne peut occuper ou dormir dans la villa sans notre accord écrit.",
     "en": "Maximum occupancy is 12 people, visitors included. No additional person may stay or sleep at the villa without our written agreement."},
    {"key": "fetes", "confirm": True,
     "fr": "Aucun événement, réception, fête ou soirée n'est autorisé, et aucune sono ou enceinte à l'extérieur. La villa est louée pour un séjour de vacances, pas pour un événement.",
     "en": "No events, receptions, parties or gatherings are allowed, and no sound system or speakers outdoors. The villa is rented for a holiday stay, not for an event.",
     "confirm_fr": "Je comprends qu'une fête ou un événement organisé dans la villa entraîne la retenue de la caution de 1 000 € et la fin immédiate du séjour, sans remboursement.",
     "confirm_en": "I understand that a party or event held at the villa results in the 1,000 € deposit being withheld and the immediate end of the stay, without refund."},
    {"key": "bruit", "confirm": True,
     "fr": "Aucune nuisance sonore, de jour comme de nuit : la villa se trouve dans un quartier résidentiel calme, avec des voisins proches. Musique à volume modéré à l'intérieur uniquement, silence complet à l'extérieur après 22h.",
     "en": "No noise disturbance, day or night: the villa is in a quiet residential area with close neighbours. Music at moderate volume indoors only, complete silence outdoors after 10 pm.",
     "confirm_fr": "Je comprends qu'une nuisance sonore constatée (plainte du voisinage, intervention) entraîne la retenue de la caution de 1 000 € et peut mettre fin immédiatement au séjour, sans remboursement.",
     "confirm_en": "I understand that a reported noise disturbance (neighbour complaint, intervention) results in the 1,000 € deposit being withheld and may end the stay immediately, without refund."},
    {"key": "visite", "confirm": False,
     "fr": "Le propriétaire ou son représentant peut se rendre sur place à tout moment pour vérifier le respect des règles de sécurité et de location, en particulier en cas de nuisance sonore.",
     "en": "The owner or their representative may visit the premises at any time to check compliance with safety and rental rules, in particular in case of noise disturbance."},
    {"key": "autres", "confirm": False,
     "fr": "Interdictions complémentaires : fumer ou vapoter à l'intérieur ; animaux sans accord écrit ; drone ; feux d'artifice et pétards ; camping (tentes, matelas d'appoint) ; objets jetés dans la piscine ou les canalisations ; consommation d'eau excessive.",
     "en": "Additional prohibitions: smoking or vaping indoors; pets without written agreement; drones; fireworks and firecrackers; camping (tents, extra mattresses); objects thrown into the pool or drains; excessive water use."},
    {"key": "caution", "confirm": False,
     "fr": "Une caution de 1 000 € (pré-autorisation bancaire ou dépôt) est demandée avant le séjour. Elle peut être retenue en tout ou partie en cas de non-respect des règles ou de dégradation.",
     "en": "A 1,000 € security deposit (bank pre-authorisation or deposit) is required before the stay. It may be withheld in full or in part in case of breach of the rules or damage."},
    {"key": "conditions", "confirm": False,
     "fr": "J'ai lu l'ensemble des conditions de location et je les accepte. Si, après relecture, certaines règles ne me conviennent pas, je peux demander l'annulation de la réservation.",
     "en": "I have read all the rental conditions and I accept them. If, after reading them again, some rules do not suit me, I can ask for the booking to be cancelled."},
]

MARKETING = [
    ("email", "Oui, uniquement par email", "Yes, by email only"),
    ("sms", "Oui, uniquement par SMS", "Yes, by SMS only"),
    ("both", "Oui, par email et par SMS", "Yes, by email and SMS"),
    ("none", "Non, je ne souhaite pas recevoir d'offres promotionnelles de la Villa des Sables du Lagon",
     "No, I do not wish to receive promotional offers from Villa des Sables du Lagon"),
]

# Réglages par défaut (surchargés par Setting VDS / checkin:params)
DEFAULT_PARAMS = {
    "reminder_days": 3,        # délai entre invitation/relance et relance suivante
    "max_reminders": 3,        # relances « au fil de l'eau » après l'invitation
    "pre_arrival_days": [14, 7, 3],   # relances supplémentaires avant l'arrivée si toujours incomplet
    "alert_days": [7, 2],      # alertes internes (email) avant l'arrivée si formulaire incomplet
    "purge_id_days": 180,      # suppression des pièces d'identité N jours après le départ
    "auto_invite": False,      # invitation automatique dès la synchro Lodgify (statut Booked) — à activer une fois le SMTP en place
    "sync_statuses": ["Booked"],
    "alert_emails": "",        # destinataires des alertes internes (virgules)
    "reply_to": "",            # adresse de réponse des emails voyageurs
    "max_upload_mb": 12,
}
