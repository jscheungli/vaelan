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


def get_session():
    with Session(engine) as session:
        yield session
