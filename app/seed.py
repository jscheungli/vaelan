"""Amorçage initial : admin + sociétés, créés seulement si la base est vide."""
from sqlmodel import Session, select
from app.core.db import engine
from app.core.config import settings
from app.core.security import hash_password
from app.models import User, Company

_COMPANIES = [
    ("STERNA", "Sterna"),
    ("KOOKABURA", "Kookabura"),
    ("PP126.23", "PP126.23"),      # société « paie seule » (module Salaires / comptes 421 uniquement)
    ("LACORP", "Lacorp"),          # groupe ISFAHAAN (Odoo × Pennylane — pas de TopOrder)
    ("JBIBFOOD", "JB & IB FOOD"),  # groupe ISFAHAAN (SIREN 877519371)
    ("JBFOOD", "JB FOOD"),         # groupe ISFAHAAN (SIREN 833093875)
    ("OTCSTPIERRE", "OTC SAINT-PIERRE"),   # groupe ISFAHAAN (SIREN 899890651)
    ("OTCRESERVE", "OTC LA RESERVE"),      # groupe ISFAHAAN (SIREN 895133940)
    ("OTCBRASFUSIL", "OTC BRAS FUSIL"),    # groupe ISFAHAAN (SIREN 981608151)
    ("GLDSTDENIS", "GLD SAINT-DENIS"),     # groupe ISFAHAAN (SIREN 910919174)
    ("GLDCASABONA", "GLD CASABONA"),       # groupe ISFAHAAN (SIREN 952182764)
    ("ISFAHAAN", "Isfahaan"),      # holding du groupe (SIREN 981355589)
    ("GONGCHA", "GONG CHA"),       # groupe ISFAHAAN (SIREN 953142080)
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
