"""SCI Les Sables du Lagon (VDS) — villa en location saisonnière à La Réunion, gérée sous Lodgify.

Module « check-in voyageurs » : remplace les 3 formulaires SurveySparrow « VDS Terms & Conditions
Check » (Airbnb / Booking / Lodgify) par un formulaire hébergé dans Vaelan, décliné par canal,
avec suivi des réservations, invitations, relances et alertes. Réglages persistants : Setting
(company_code VDS, clés checkin:*), surchargés depuis la page de configuration.
"""

COMPANY_CODE = "VDS"

VILLA = {
    "name": "La Villa des Sables du Lagon",
    "legal": "SCI LES SABLES DU LAGON",
    "rcs": "RCS Saint-Denis de La Réunion 821 948 163",
    "address": "6 ruelle Mazeau, 97400 Saint-Denis, La Réunion",
    "rgpd_email": "rgpd@villa-des-sables-du-lagon.com",
    "contact_email": "contact@villa-des-sables-du-lagon.com",
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
    "airbnb":  {"label": "Airbnb",        "id_required": True,  "platform": True},
    "booking": {"label": "Booking.com",   "id_required": True,  "platform": True},
    "abritel": {"label": "Abritel / Vrbo", "id_required": True, "platform": True},
    "lodgify": {"label": "Site direct",   "id_required": True,  "platform": False},
}

# Règles à reconnaître EXPLICITEMENT une à une (Oui / Non). `confirm` = case de confirmation
# supplémentaire (sanction) pour les règles dissuasives (fêtes, bruit).
RULES = [
    {"key": "horaires", "confirm": False,
     "fr": "Les horaires : arrivée entre 16h et 20h, départ entre 8h et 9h. En dehors de ces plages, il faut notre accord écrit préalable.",
     "en": "Times: check-in between 4 pm and 8 pm, check-out between 8 am and 9 am. Outside these windows, our prior written agreement is required.",
     "more_q_fr": "Besoin d'arriver plus tôt ou de partir plus tard ?",
     "more_q_en": "Need to arrive earlier or leave later?",
     "more_fr": "Nous louons la villa à la nuitée. Lorsqu'un départ a lieu le jour de votre arrivée, ou une arrivée le jour de votre départ, "
                "notre équipe a besoin de ce créneau pour remettre en état une grande villa : c'est la raison de ces horaires. "
                "S'il n'y a ni départ le jour de votre arrivée, ni arrivée le jour de votre départ, nous sommes beaucoup plus souples, et gratuitement : "
                "vous pourrez prendre possession des lieux plus tôt ou les quitter plus tard. Comme nous acceptons des réservations jusqu'à 3 jours avant l'arrivée, "
                "nous pourrons vous le confirmer 3 jours avant votre séjour. Et même s'il y a un départ le jour de votre arrivée, contactez-nous : "
                "il est en général possible de déposer vos bagages, ou de profiter des espaces extérieurs en fin de matinée ou en début d'après-midi, "
                "le temps que nous terminions la préparation des pièces intérieures.",
     "more_en": "The villa is rented per night. When a departure takes place on the day of your arrival, or an arrival on the day of your departure, "
                "our team needs that window to get a large villa ready again: that is the reason for these times. "
                "If there is neither a departure on your arrival day nor an arrival on your departure day, we are much more flexible, free of charge: "
                "you can take possession of the villa earlier or leave later. As we accept bookings up to 3 days before arrival, "
                "we can confirm it to you 3 days before your stay. And even if there is a departure on your arrival day, get in touch: "
                "it is usually possible to drop off your luggage, or to enjoy the outdoor areas from late morning or early afternoon, "
                "while we finish preparing the indoor rooms."},
    {"key": "capacite", "confirm": False,
     "fr": "La capacité maximale est de 12 personnes, visiteurs compris. Aucune personne supplémentaire ne peut occuper ou dormir dans la villa sans notre accord écrit.",
     "en": "Maximum occupancy is 12 people, visitors included. No additional person may stay or sleep at the villa without our written agreement."},
    {"key": "fetes", "confirm": True,
     "fr": "Aucun événement, réception, fête ou soirée n'est autorisé, et aucune sono ou enceinte à l'extérieur. La villa est louée pour un séjour de vacances, pas pour un événement.",
     "en": "No events, receptions, parties or gatherings are allowed, and no sound system or speakers outdoors. The villa is rented for a holiday stay, not for an event.",
     "confirm_fr": "Je comprends qu'une fête ou un événement organisé dans la villa entraîne la retenue de la caution de 1 000 € et la fin immédiate du séjour, sans remboursement.",
     "confirm_en": "I understand that a party or event held at the villa results in the 1,000 € deposit being withheld and the immediate end of the stay, without refund.",
     "more_q_fr": "Vous envisagez un événement ?",
     "more_q_en": "Are you planning an event?",
     # variante « site direct »
     "more_fr": "La règle générale est l'interdiction : ce que nous voulons éviter avant tout, ce sont les nuisances sonores pour le voisinage. "
                "Si vous êtes certain qu'un événement se déroulerait sans aucune nuisance sonore, contactez-nous avant votre séjour : "
                "chaque demande est étudiée au cas par cas et peut faire l'objet d'une acceptation, avec un forfait événement qui s'ajoute au tarif de la location. "
                "Ce forfait dépend du nombre de personnes, du moment (journée ou soirée), du type d'événement et de la date.",
     "more_en": "The general rule is that events are prohibited: what we want to avoid above all is noise disturbance for the neighbourhood. "
                "If you are certain that an event would take place without any noise disturbance, contact us before your stay: "
                "each request is reviewed case by case and may be accepted, with an event package added to the rental price. "
                "This package depends on the number of people, the time (daytime or evening), the type of event and the date.",
     # variante « plateformes » (Airbnb / Booking / Abritel)
     "more_platform_fr": "La règle générale est l'interdiction : ce que nous voulons éviter avant tout, ce sont les nuisances sonores pour le voisinage. "
                         "Les événements ne sont pas autorisés pour les réservations effectuées via une plateforme ({platform}). "
                         "Ils ne peuvent être étudiés, au cas par cas et avec un forfait événement qui s'ajoute au tarif de la location, "
                         "que pour les réservations passées directement sur notre site {site_link}.",
     "more_platform_en": "The general rule is that events are prohibited: what we want to avoid above all is noise disturbance for the neighbourhood. "
                         "Events are not allowed for bookings made through a platform ({platform}). "
                         "They can only be considered, case by case and with an event package added to the rental price, "
                         "for bookings made directly on our website www.villa-des-sables-du-lagon.com."},
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
    {"key": "piscine", "confirm": False,
     "fr": "Dans la piscine : douche avant la baignade, et uniquement des crèmes solaires à filtres minéraux (respectueuses de l'eau). Pas d'huile solaire, pas de spray. Un produit inadapté trouble l'eau et encrasse la filtration.",
     "en": "In the pool: shower before swimming, and only mineral-filter (water-friendly) sunscreens. No tanning oil, no spray. An unsuitable product clouds the water and clogs the filtration.",
     "more_q_fr": "Quelle crème solaire choisir ?",
     "more_q_en": "Which sunscreen should I choose?",
     "more_fr": "Regardez la liste des ingrédients : choisissez une crème à filtres minéraux, c'est-à-dire à base d'oxyde de zinc (zinc oxide) ou de dioxyde de titane (titanium dioxide), souvent signalée par les mentions « filtres minéraux », « reef safe » ou « respectueuse des océans ». "
                "Évitez les filtres chimiques oxybenzone (benzophenone-3), octinoxate (ethylhexyl methoxycinnamate), octocrylène, homosalate et avobenzone, ainsi que les huiles et sprays, qui forment un film gras à la surface de l'eau. "
                "Appliquez la crème 20 à 30 minutes avant la baignade, prenez une douche avant d'entrer dans la piscine, et pensez aux tee-shirts anti-UV pour les enfants : c'est la meilleure protection, sans aucun produit dans l'eau.",
     "more_en": "Check the ingredient list: choose a mineral-filter sunscreen, i.e. based on zinc oxide or titanium dioxide, often labelled \"mineral filters\", \"reef safe\" or \"ocean friendly\". "
                "Avoid the chemical filters oxybenzone (benzophenone-3), octinoxate (ethylhexyl methoxycinnamate), octocrylene, homosalate and avobenzone, as well as oils and sprays, which leave a greasy film on the water. "
                "Apply sunscreen 20 to 30 minutes before swimming, shower before entering the pool, and consider UV-protective shirts for children: the best protection, with nothing in the water."},
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
     "No, I do not wish to receive promotional offers from La Villa des Sables du Lagon"),
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
    "beta": True,              # MODE BÊTA : tous les emails voyageurs sont redirigés vers test_email (rien ne part aux clients)
    "test_email": "jscheungli@gmail.com",
    "alert_emails": "jscheungli@gmail.com",   # destinataires des alertes internes (virgules)
    # nouvelle réservation à inviter À LA MAIN (Airbnb / Booking / Abritel, ou site direct sans email / sans invitation auto)
    "new_booking_emails": "contact@villa-des-sables-du-lagon.com",
    "reply_to": "",            # adresse de réponse des emails voyageurs
    "from_email": "",          # adresse d'envoi (vide = FROM_ADDRESS, domaine de la villa vérifié chez Postmark)
    "max_upload_mb": 12,
    "base_url": "",            # racine des liens publics (vide = https://vaelan.com) ; cible : https://checkin.villa-des-sables-du-lagon.com
}
FROM_ADDRESS = "contact@villa-des-sables-du-lagon.com"   # boîte de la villa (Google Workspace) ; domaine vérifié chez Postmark (DKIM + Return-Path)
# Sous-domaine du formulaire, hébergé par Vaelan : DNS Namecheap CNAME checkin -> vaelan.onrender.com + Render Custom Domain.
# Sur cet hôte, /<token> et /nouveau/<canal> sont acceptés en raccourci (voir main.py) ; le back-office reste sur vaelan.com.
PUBLIC_HOST = "checkin.villa-des-sables-du-lagon.com"
