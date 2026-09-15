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
    "ALTER TABLE pl_employees ADD COLUMN IF NOT EXISTS email VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS label VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS step VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS progress_current INTEGER",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS progress_total INTEGER",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS report VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS app_version VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS user_email VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN DEFAULT FALSE",
    "ALTER TABLE imports ADD COLUMN IF NOT EXISTS run_id INTEGER",
    "ALTER TABLE step_declarations ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP",
    "ALTER TABLE step_declarations ADD COLUMN IF NOT EXISTS verify_ok BOOLEAN",
    "ALTER TABLE step_declarations ADD COLUMN IF NOT EXISTS verify_run_id INTEGER",
    "ALTER TABLE step_declarations ADD COLUMN IF NOT EXISTS done_at TIMESTAMP",
    "ALTER TABLE step_declarations ADD COLUMN IF NOT EXISTS declared_by VARCHAR",
    "ALTER TABLE payment_reports ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'saisi'",
    "ALTER TABLE client_accounts ADD COLUMN IF NOT EXISTS pennylane_name VARCHAR",
    "ALTER TABLE client_accounts ADD COLUMN IF NOT EXISTS pennylane_reg_no VARCHAR",
    "ALTER TABLE client_accounts ADD COLUMN IF NOT EXISTS pennylane_external_ref VARCHAR",
    # certains IDs clients Pennylane dépassent l'INTEGER 32 bits -> BIGINT
    "ALTER TABLE client_accounts ALTER COLUMN pennylane_customer_id TYPE BIGINT",
    "ALTER TABLE vds_messages ADD COLUMN IF NOT EXISTS sender VARCHAR",
    "ALTER TABLE vds_messages ADD COLUMN IF NOT EXISTS by_user VARCHAR",
    "ALTER TABLE tiers_matches ADD COLUMN IF NOT EXISTS siren_src_detail VARCHAR",
    "ALTER TABLE tiers_matches ADD COLUMN IF NOT EXISTS candidates VARCHAR",
    "ALTER TABLE lp_store_months ADD COLUMN IF NOT EXISTS profit_before_tax FLOAT DEFAULT 0",
    "ALTER TABLE lp_store_months ADD COLUMN IF NOT EXISTS profit_after_tax FLOAT DEFAULT 0",
    "ALTER TABLE lp_forecasts ADD COLUMN IF NOT EXISTS set_id INTEGER",
    "ALTER TABLE lp_forecasts ADD COLUMN IF NOT EXISTS scenario VARCHAR",
    # OWINE export (v0.1.195)
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS abv FLOAT",
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS volume_cl INTEGER DEFAULT 75",
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS hs_code VARCHAR",
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS origin VARCHAR DEFAULT 'FR'",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS locale VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS customer_type VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS billing_company VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS vat_number VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS eori VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS tax_id VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS tax_total FLOAT DEFAULT 0",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS tax_rate FLOAT DEFAULT 0",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS shipping_paid FLOAT DEFAULT 0",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS attributes VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS export_json VARCHAR",
    "ALTER TABLE ow_orders ADD COLUMN IF NOT EXISTS customs_token VARCHAR",
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS abv_status VARCHAR",
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS abv_source VARCHAR",
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS customs_confirmed_at TIMESTAMP",
    "ALTER TABLE ow_items ADD COLUMN IF NOT EXISTS customs_confirmed_by VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_ow_orders_customs_token ON ow_orders (customs_token)",
]


def _ensure_columns() -> None:
    from sqlalchemy import text
    sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        for stmt in _COLUMN_ADDS:
            try:
                if sqlite:
                    if "ADD COLUMN IF NOT EXISTS" not in stmt and not stmt.startswith("CREATE INDEX"):
                        continue                              # ALTER TYPE… : Postgres seulement
                    stmt = stmt.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")   # SQLite n'a pas IF NOT EXISTS : « duplicate column » avalé ci-dessous
                conn.execute(text(stmt))
            except Exception:
                pass  # colonne déjà présente (ou base fraîche où la table n'existe pas encore)


def get_session():
    with Session(engine) as session:
        yield session
