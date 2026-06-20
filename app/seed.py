"""Amorçage initial : admin + sociétés, créés seulement si la base est vide."""
from sqlmodel import Session, select
from app.core.db import engine
from app.core.config import settings
from app.core.security import hash_password
from app.models import User, Company

_COMPANIES = [
    ("STERNA", "Sterna"),
    ("KOOKABURA", "Kookabura"),
]


def seed_if_empty() -> None:
    with Session(engine) as s:
        if not s.exec(select(User)).first():
            s.add(User(
                email=settings.admin_email.lower().strip(),
                name="Admin",
                password_hash=hash_password(settings.admin_password),
                is_superuser=True,
                active=True,
            ))
        for code, name in _COMPANIES:
            if not s.exec(select(Company).where(Company.code == code)).first():
                s.add(Company(code=code, name=name, active=True))
        s.commit()
