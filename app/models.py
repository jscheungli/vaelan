"""Modèle de données du socle Vaelan.

La machine à états de l'import vit dans DailyState ; chaque écriture poussée
est rattachée à un ImportBatch (réversibilité / traçabilité). Le métier des
packs s'appuiera sur ces tables + ses propres tables au besoin.
"""
from datetime import datetime, date
import datetime as _dt
from typing import Optional
from sqlalchemy import BigInteger, LargeBinary, Column
from sqlmodel import SQLModel, Field


class Company(SQLModel, table=True):
    __tablename__ = "companies"
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)     # STERNA, KOOKABURA
    name: str
    active: bool = True


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    password_hash: str
    is_superuser: bool = False
    active: bool = True


class UserCompanyAccess(SQLModel, table=True):
    __tablename__ = "user_company_access"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    role: str = "operator"   # admin / operator / viewer


class Run(SQLModel, table=True):
    """Trace d'exécution riche (import, cadrage, contrôle, remédiation…)."""
    __tablename__ = "runs"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: Optional[int] = Field(default=None, foreign_key="companies.id")
    pack: Optional[str] = None
    kind: str
    label: Optional[str] = None   # description lisible (ex. "Import caisse · Saint-Leu · 01/05→17/06")
    status: str = "running"   # running / ok / error / interrupted
    cancel_requested: bool = False   # l'utilisateur a demandé l'arrêt -> le job s'interrompt
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    summary: Optional[str] = None
    log: Optional[str] = None  # journal détaillé (texte/JSON), pensé pour être relu
    report: Optional[str] = None  # compte rendu téléchargeable (rapprochement détaillé)
    app_version: Optional[str] = None  # version de Vaelan active au moment de la tâche (debug)
    user_email: Optional[str] = None   # utilisateur qui a lancé la tâche
    # progression (pour l'affichage live de la page Jobs)
    step: Optional[str] = None
    progress_current: Optional[int] = None
    progress_total: Optional[int] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DailyState(SQLModel, table=True):
    """Machine à états par (établissement, jour)."""
    __tablename__ = "daily_states"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    establishment: str = Field(index=True)
    day: date = Field(index=True)
    # pending -> computed -> cadre -> validated -> pushed -> settled
    status: str = "pending"
    synthese_total: Optional[float] = None
    computed_total: Optional[float] = None
    diff: Optional[float] = None
    import_id: Optional[int] = Field(default=None, foreign_key="imports.id")
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClientAccount(SQLModel, table=True):
    """Table de correspondance client PRO : TopOrder ↔ Pennylane (clé = SIRET).

    Statut : ok / no_siret / no_pennylane / incoherent. Vaelan ne fait que
    détecter et alerter ; la correction se fait dans TopOrder / Pennylane.
    """
    __tablename__ = "client_accounts"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    establishment: str = Field(index=True)            # SL / LP / SM
    toporder_company_id: str = Field(index=True)
    toporder_name: Optional[str] = None
    siret: Optional[str] = None
    # ID Pennylane : BIGINT obligatoire (certains IDs dépassent l'INTEGER 32 bits)
    pennylane_customer_id: Optional[int] = Field(default=None, sa_column=Column(BigInteger))
    pennylane_name: Optional[str] = None              # nom côté Pennylane (pour voir la correspondance)
    pennylane_reg_no: Optional[str] = None            # SIREN (reg_no) côté Pennylane — sert au matching
    pennylane_external_ref: Optional[str] = None      # « Identifiant client » Pennylane (external_reference = SIRET, champ d'unicité)
    account_411: Optional[str] = None                 # numéro de compte Pennylane
    status: str = "unknown"                            # ok / no_siret / no_pennylane / incoherent
    note: Optional[str] = None
    last_synced: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Setting(SQLModel, table=True):
    """Réglage éditable d'une société : surcharge une valeur de config par défaut.

    Clé hiérarchique séparée par « : » (ex. ca_anonyme, tva:2.1, est:SL:cb). Seules
    les valeurs RÉELLEMENT modifiées sont stockées (sinon = défaut du code).
    """
    __tablename__ = "settings"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_code: str = Field(index=True)
    key: str = Field(index=True)
    value: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class JobArtifact(SQLModel, table=True):
    """Fichier rattaché à une tâche, stocké EN BASE (durable, survit aux redéploys
    Render qui réinitialisent le disque). kind = input (synthèse PDF) / csv (export)."""
    __tablename__ = "job_artifacts"
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="runs.id", index=True)
    kind: str = Field(index=True)                 # input / csv
    name: str
    content_type: str = "application/octet-stream"
    data: bytes = Field(sa_column=Column(LargeBinary))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StepDeclaration(SQLModel, table=True):
    """Suivi de clôture : état déclaré/vérifié d'une étape pour un (établissement, mois).

    Pattern en deux temps : l'utilisateur DÉCLARE qu'il a fait l'étape (ex. import
    manuel dans Pennylane), puis Vaelan VÉRIFIE (cadré / sans écart). `covered_to`
    porte la date « fait jusqu'au » pour le travail intra-mois (ex. à la semaine).
    """
    __tablename__ = "step_declarations"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    establishment: str = Field(index=True)        # SL / LP / SM
    period: str = Field(default="", index=True)   # (obsolète : suivi global, plus par mois)
    step: str = Field(index=True)                  # import_tickets / verify_tickets / …
    state: str = "declared"                        # declared / verified
    covered_to: Optional[date] = None              # fait JUSQU'AU (date de couverture)
    done_at: Optional[datetime] = None             # réalisé/déclaré le (heure Réunion)
    declared_by: Optional[str] = None              # email de l'utilisateur qui a déclaré
    note: Optional[str] = None
    verified_at: Optional[datetime] = None         # dernière vérification Pennylane
    verify_ok: Optional[bool] = None               # cohérent (True) / écart (False)
    verify_run_id: Optional[int] = None            # tâche de vérification (compte rendu)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClientPayment(SQLModel, table=True):
    """Instantané de l'encours d'un client (B2B = un compte 411 ; B2C = compte commun).

    Produit par le job de synchro paiements ; la page le lit pour un affichage rapide.
    Réconcilie l'encours côté Pennylane (facturé impayé) et côté TopOrder (facturé non
    payé + en attente de facturation). Clé stable = compte 411 (company-wide)."""
    __tablename__ = "client_payments"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    account: str = Field(index=True)                 # n° de compte 411 (clé client)
    name: Optional[str] = None
    kind: str = "b2b"                                 # b2b / b2c
    # Pennylane (compta)
    encours_pennylane: float = 0.0                    # solde 411 ouvert (facturé impayé)
    nb_open: int = 0                                  # nb de créances ouvertes
    oldest_age: int = 0                               # ancienneté (jours) de la + vieille créance
    virements_a_reporter: float = 0.0                 # somme des virements non encore reportés dans TopOrder
    nb_virements_a_reporter: int = 0
    # TopOrder (gestion)
    encours_toporder: float = 0.0                     # facturé non payé côté TopOrder
    attente_fact_courant: float = 0.0                 # en attente de facturation, mois courant
    attente_fact_anterieur: float = 0.0               # en attente de facturation, antérieur
    ecart: float = 0.0                                # encours_toporder - encours_pennylane
    exposition: float = 0.0                           # encours_pennylane + attente de facturation (tri)
    synced_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PaymentReport(SQLModel, table=True):
    """Tag manuel posé sur une écriture du compte client Pennylane (traçabilité).

    status : « saisi » (reporté/rapproché dans TopOrder) ou « ignore » (non pertinent /
    déjà traité). Clé = ligne Pennylane (ledger_entry_line_id, idempotent). On garde QUI
    a tagué et QUAND."""
    __tablename__ = "payment_reports"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    account: str = Field(index=True)
    ledger_entry_line_id: int = Field(sa_column=Column(BigInteger, index=True, unique=True))
    status: str = "saisi"                              # saisi / ignore
    amount: Optional[float] = None
    op_date: Optional[str] = None
    label: Optional[str] = None
    reported_by: Optional[str] = None                 # email de l'utilisateur
    reported_at: datetime = Field(default_factory=datetime.utcnow)


class ImportBatch(SQLModel, table=True):
    """Un lot d'import (= un CSV = un identifiant compact dans les libellés)."""
    __tablename__ = "imports"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    run_id: Optional[int] = Field(default=None, foreign_key="runs.id")  # tâche qui l'a produit
    establishment: str
    code: str = Field(index=True)         # identifiant compact, ex. A37
    kind: str                             # toslt / toslf / ...
    date_from: date
    date_to: date
    status: str = "generated"             # generated / imported / attached
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    n_entries: Optional[int] = None
    amount: Optional[float] = None
    csv_path: Optional[str] = None


class OdooClientMatch(SQLModel, table=True):
    """Correspondance clients Odoo ↔ Pennylane (groupe ISFAHAAN — sociétés sans TopOrder).

    Une ligne par client Odoo (odoo_id renseigné) OU par client Pennylane orphelin
    (odoo_id vide, status absent_odoo). Statuts : ok / conflit_nom / doublon_odoo /
    doublon_pennylane / sans_cle / absent_pennylane / absent_odoo. Vaelan détecte et
    alerte ; la correction (fusion des doublons, saisie SIREN/TVA) se fait dans
    Odoo / Pennylane, puis on resynchronise.
    """
    __tablename__ = "odoo_client_matches"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    odoo_id: Optional[int] = Field(default=None, index=True)
    odoo_name: Optional[str] = None
    odoo_ref: Optional[str] = None                    # référence interne Odoo
    odoo_vat: Optional[str] = None                    # n° TVA saisi dans Odoo
    odoo_siret: Optional[str] = None                  # SIRET Odoo (module l10n_fr) si présent
    siren: Optional[str] = Field(default=None, index=True)   # clé de matching calculée
    pennylane_customer_id: Optional[int] = Field(default=None, sa_column=Column(BigInteger))
    pennylane_name: Optional[str] = None
    pennylane_reg_no: Optional[str] = None
    status: str = "unknown"
    note: Optional[str] = None
    dup_group: Optional[str] = None                   # clé partagée par les doublons (pour regrouper)
    last_synced: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TiersMatch(SQLModel, table=True):
    """Tiers Odoo ↔ Pennylane (clients OU fournisseurs) : diagnostic + plan d'actions.

    Une ligne par fiche Odoo de tête (odoo_id) OU par tiers Pennylane orphelin (odoo_id vide).
    Le moteur reproduit les critères de rapprochement du connecteur Pennylane (email, numéro de
    compte tiers, SIREN/SIRET/TVA) pour PRÉDIRE les doublons, choisit le tiers Pennylane canonique
    (celui qui porte l'historique) et prépare les écritures des deux côtés (plan_pl / plan_odoo,
    JSON). L'application est un acte séparé et tracé (applied_*_at). Jamais d'IBAN.
    """
    __tablename__ = "tiers_matches"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    kind: str = Field(default="client", index=True)          # client / fournisseur
    odoo_id: Optional[int] = Field(default=None, index=True)
    odoo_name: Optional[str] = None
    odoo_ref: Optional[str] = None
    odoo_vat: Optional[str] = None
    odoo_registry: Optional[str] = None
    odoo_email: Optional[str] = None
    odoo_inv_total: int = 0
    odoo_inv_2026: int = 0
    siren: Optional[str] = Field(default=None, index=True)   # SIREN retenu
    siren_source: Optional[str] = None                       # odoo / pennylane / annuaire / validé
    siret: Optional[str] = None
    tva: Optional[str] = None                                # n° TVA FR calculé depuis le SIREN
    annuaire: Optional[str] = None                           # proposition annuaire (nom · ville · similarité)
    pl_id: Optional[int] = Field(default=None, sa_column=Column(BigInteger))   # tiers Pennylane canonique
    pl_name: Optional[str] = None
    pl_account: Optional[str] = None
    pl_reg_no: Optional[str] = None
    pl_vat: Optional[str] = None
    pl_emails: Optional[str] = None
    pl_reference: Optional[str] = None
    pl_lines: int = 0
    pl_lines_2026: int = 0
    pl_solde: Optional[float] = None
    pl_dups: Optional[str] = None                            # autres tiers PL du même client (à fusionner)
    match_via: Optional[str] = None                          # identifiants communs ACTUELS (critères connecteur)
    status: str = "unknown"
    mode: str = "RIEN"                                       # RIEN / AUTO / A_VALIDER / SAISIE / MANUEL
    siren_src_detail: Optional[str] = None                   # traçabilité : qui a validé le SIREN, quel fichier, quelle date
    candidates: Optional[str] = None                         # traçabilité : base de l'appariement + autres candidats proches
    action_user: Optional[str] = None
    action_ia: Optional[str] = None
    plan_pl: Optional[str] = None                            # JSON : champs à écrire côté Pennylane
    plan_odoo: Optional[str] = None                          # JSON : champs à écrire côté Odoo
    applied_pl_at: Optional[datetime] = None
    applied_odoo_at: Optional[datetime] = None
    last_synced: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ============================ VDS — check-in voyageurs ============================
class VdsReservation(SQLModel, table=True):
    """Réservation de la villa (SCI Les Sables du Lagon) suivie pour le formulaire d'arrivée.

    Créée à la main, par import SurveySparrow (historique) ou par la synchro Lodgify.
    `token` = clé du lien public /checkin/<token> (imprévisible). Statuts :
    pending (à inviter) / sent (invitation envoyée) / reminded (relancé) / completed
    (formulaire signé) / refused (règles refusées → annulation demandée) / cancelled.
    """
    __tablename__ = "vds_reservations"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_code: str = Field(default="VDS", index=True)
    channel: str = Field(index=True)                       # airbnb / booking / lodgify
    booking_ref: Optional[str] = Field(default=None, index=True)   # n° de réservation (Lodgify B1234567, Airbnb HMxxxx…)
    lodgify_id: Optional[int] = Field(default=None, index=True)
    source: str = "manual"                                 # manual / lodgify / surveysparrow
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    arrival: Optional[date] = Field(default=None, index=True)
    departure: Optional[date] = None
    guests: Optional[int] = None
    lang: str = "fr"
    token: str = Field(index=True, unique=True)
    status: str = Field(default="pending", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    invited_at: Optional[datetime] = None
    reminded_at: Optional[datetime] = None
    reminder_count: int = 0
    alerted_at: Optional[datetime] = None                  # dernière alerte interne « arrivée proche sans formulaire »
    completed_at: Optional[datetime] = None
    erp_sent_at: Optional[datetime] = None                 # état des risques envoyé (J-1)
    notes: Optional[str] = None
    raw: Optional[str] = None                              # JSON brut Lodgify (debug)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VdsResponse(SQLModel, table=True):
    """Réponse au formulaire d'arrivée (une par réservation, la dernière fait foi)."""
    __tablename__ = "vds_responses"
    id: Optional[int] = Field(default=None, primary_key=True)
    reservation_id: int = Field(foreign_key="vds_reservations.id", index=True)
    channel: str
    lang: str = "fr"
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    rules_ok: bool = True                                  # toutes les règles acceptées
    refused_rules: Optional[str] = None                    # clés des règles refusées (virgules)
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    birth_date: Optional[str] = None
    birth_place: Optional[str] = None
    address: Optional[str] = None
    marketing: Optional[str] = None                        # none / email / sms / both
    data: str = "{}"                                       # toutes les réponses (JSON)
    signed: bool = False


class VdsFile(SQLModel, table=True):
    """Fichier lié au check-in, stocké en base : signature, pièce d'identité (réduite),
    récapitulatif PDF signé, état des risques (reservation_id vide = document de référence)."""
    __tablename__ = "vds_files"
    id: Optional[int] = Field(default=None, primary_key=True)
    reservation_id: Optional[int] = Field(default=None, foreign_key="vds_reservations.id", index=True)
    response_id: Optional[int] = Field(default=None, index=True)
    kind: str = Field(index=True)                          # signature / id_document / recap / erp
    name: str
    content_type: str = "application/octet-stream"
    size: int = 0
    data: bytes = Field(sa_column=Column(LargeBinary))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VdsMessage(SQLModel, table=True):
    """Journal des envois (invitations, relances, confirmations, alertes internes, ERP)."""
    __tablename__ = "vds_messages"
    id: Optional[int] = Field(default=None, primary_key=True)
    reservation_id: Optional[int] = Field(default=None, foreign_key="vds_reservations.id", index=True)
    kind: str = Field(index=True)                          # invitation / reminder / confirmation / alert / erp / refusal
    channel: str = "email"                                 # email / sms / whatsapp
    to: Optional[str] = None
    sender: Optional[str] = None                           # expéditeur tel qu'envoyé (« Société via Vaelan <…> »)
    by_user: Optional[str] = None                          # utilisateur Vaelan à l'origine d'un envoi manuel (SMS, WhatsApp, messagerie)
    subject: Optional[str] = None
    body: Optional[str] = None
    status: str = "sent"                                   # sent / error / skipped
    error: Optional[str] = None
    sent_at: datetime = Field(default_factory=datetime.utcnow)


# =============================== Planning des équipes (boulangeries) ===============================
class PlEmployee(SQLModel, table=True):
    """Salarié planifiable. `posts` = {clé de poste: niveau 1-3} (3 = poste principal), `days_off` = jours fixes
    non travaillés (0=lundi), `cfa_days` = jours de CFA (apprentis), `pattern` = présence observée par jour (%)."""
    __tablename__ = "pl_employees"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_code: str = Field(index=True)
    site: str = Field(index=True)                          # SL / LP / SM
    first_name: str
    last_name: str
    contract_type: str = "CDI"                             # CDI / CDD / Apprenti / Stage
    weekly_hours: float = 35.0
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    active: bool = True
    posts: str = "{}"                                      # JSON {post_key: level}
    days_off: str = "[]"                                   # JSON [weekday]
    cfa_days: str = "[]"                                   # JSON [weekday]
    pattern: str = "{}"                                    # JSON {weekday: % présence observée}
    sunday: str = "oui"                                    # oui / non
    max_days: int = 5                                      # jours travaillés max par semaine
    mobility: str = "[]"                                   # JSON [site] où la personne accepte d'aller
    flexibility: int = 2                                   # 1 peu · 2 normal · 3 très flexible
    priority: int = 5                                      # ordre d'appel pour un remplacement (1 = en premier)
    phone: Optional[str] = None
    telegram: Optional[str] = None
    note: Optional[str] = None
    source: str = "manual"                                 # manual / skello
    external_key: Optional[str] = Field(default=None, index=True)   # « Prénom NOM » Skello


class PlPost(SQLModel, table=True):
    """Poste de travail (gabarit de plage) : horaires par défaut, pause, couleur."""
    __tablename__ = "pl_posts"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_code: str = Field(index=True)
    site: str = Field(index=True)
    key: str = Field(index=True)
    label: str
    department: str = "vente"                              # vente / boulangerie / patisserie / traiteur / admin / autre
    color: str = "#ffd23f"
    start: str = "05:15"
    end: str = "14:00"
    pause: float = 0.5
    active: bool = True                                    # proposé dans les gabarits / la génération
    sort: int = 100


class PlShift(SQLModel, table=True):
    """Plage planifiée (travail), absence ou tâche. employee_id vide = plage non assignée."""
    __tablename__ = "pl_shifts"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_code: str = Field(index=True)
    site: str = Field(index=True)
    employee_id: Optional[int] = Field(default=None, foreign_key="pl_employees.id", index=True)
    date: _dt.date = Field(index=True)                     # (annotation via le module : le nom du champ masque le type)
    kind: str = "work"                                     # work / absence / task
    post_key: Optional[str] = None                         # travail : poste ; absence : type d'absence ; tâche : libellé
    start: Optional[str] = None                            # "05:15" (travail / tâche)
    end: Optional[str] = None
    pause: float = 0.0
    hours: float = 0.0                                     # heures valorisées (travail effectif, ou valeur de l'absence)
    note: Optional[str] = None
    status: str = "draft"                                  # draft / published
    source: str = "manual"                                 # manual / auto / import / replacement
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PlIncident(SQLModel, table=True):
    """Absence imprévue → recherche de remplaçant guidée (plans A, B, C…)."""
    __tablename__ = "pl_incidents"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_code: str = Field(index=True)
    site: str = Field(index=True)
    employee_id: int = Field(foreign_key="pl_employees.id", index=True)
    date_from: date
    date_to: date
    reason: str = "maladie"
    status: str = "open"                                   # open / resolved / closed
    plan: str = "{}"                                       # JSON : plages touchées, candidats par plan, réponses
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
