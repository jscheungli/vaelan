"""Modèle de données du socle Vaelan.

La machine à états de l'import vit dans DailyState ; chaque écriture poussée
est rattachée à un ImportBatch (réversibilité / traçabilité). Le métier des
packs s'appuiera sur ces tables + ses propres tables au besoin.
"""
from datetime import datetime, date
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
