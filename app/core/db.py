from sqlmodel import SQLModel, Session, create_engine
from .config import settings

_url = settings.database_url
# Render fournit parfois des URL "postgres://" ; SQLAlchemy attend "postgresql://".
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if _url.startswith("sqlite") else {}
engine = create_engine(_url, connect_args=connect_args, pool_pre_ping=True)


def init_db() -> None:
    import app.models  # noqa: F401 — enregistre les tables
    SQLModel.metadata.create_all(engine)
    _ensure_columns()


# Mini-migration : ajouts de colonnes idempotents en attendant Alembic.
# (create_all ne modifie pas une table existante ; on ajoute les colonnes manquantes.)
_COLUMN_ADDS = [
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS label VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS step VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS progress_current INTEGER",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS progress_total INTEGER",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS report VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS app_version VARCHAR",
    "ALTER TABLE imports ADD COLUMN IF NOT EXISTS run_id INTEGER",
    "ALTER TABLE client_accounts ADD COLUMN IF NOT EXISTS pennylane_name VARCHAR",
    "ALTER TABLE client_accounts ADD COLUMN IF NOT EXISTS pennylane_reg_no VARCHAR",
    "ALTER TABLE client_accounts ADD COLUMN IF NOT EXISTS pennylane_external_ref VARCHAR",
    # certains IDs clients Pennylane dépassent l'INTEGER 32 bits -> BIGINT
    "ALTER TABLE client_accounts ALTER COLUMN pennylane_customer_id TYPE BIGINT",
]


def _ensure_columns() -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for stmt in _COLUMN_ADDS:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass  # SQLite (dev, base fraîche) ou colonne déjà présente


def get_session():
    with Session(engine) as session:
        yield session
