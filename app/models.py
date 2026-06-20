"""Modèle de données du socle Vaelan.

La machine à états de l'import vit dans DailyState ; chaque écriture poussée
est rattachée à un ImportBatch (réversibilité / traçabilité). Le métier des
packs s'appuiera sur ces tables + ses propres tables au besoin.
"""
from datetime import datetime, date
from typing import Optional
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
    pennylane_customer_id: Optional[int] = None
    account_411: Optional[str] = None                 # numéro de compte Pennylane
    status: str = "unknown"                            # ok / no_siret / no_pennylane / incoherent
    note: Optional[str] = None
    last_synced: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ImportBatch(SQLModel, table=True):
    """Un lot d'import (= un CSV = un identifiant compact dans les libellés)."""
    __tablename__ = "imports"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
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
