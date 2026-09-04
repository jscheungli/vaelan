from typing import Optional, List
from fastapi import Request, HTTPException, status
from sqlmodel import Session, select
from passlib.context import CryptContext

from .db import engine
from app.models import User, Company, UserCompanyAccess

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(p: str) -> str:
    return _pwd.hash(p)


def verify_password(p: str, h: str) -> bool:
    try:
        return _pwd.verify(p, h)
    except Exception:
        return False


def authenticate(email: str, password: str) -> Optional[User]:
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == email.lower().strip())).first()
        if u and u.active and verify_password(password, u.password_hash):
            return u
    return None


def current_user(request: Request) -> Optional[User]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    with Session(engine) as s:
        u = s.get(User, uid)
        return u if (u and u.active) else None


def require_user(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return u


def user_companies(user: User) -> List[Company]:
    """Sociétés accessibles à l'utilisateur (toutes si superuser)."""
    with Session(engine) as s:
        if user.is_superuser:
            return list(s.exec(select(Company).where(Company.active == True)).all())  # noqa: E712
        ids = s.exec(select(UserCompanyAccess.company_id).where(UserCompanyAccess.user_id == user.id)).all()
        if not ids:
            return []
        return list(s.exec(select(Company).where(Company.id.in_(ids), Company.active == True)).all())  # noqa: E712


def role_for(user: User, company: Company) -> Optional[str]:
    if user.is_superuser:
        return "admin"
    with Session(engine) as s:
        a = s.exec(
            select(UserCompanyAccess).where(
                UserCompanyAccess.user_id == user.id, UserCompanyAccess.company_id == company.id
            )
        ).first()
        return a.role if a else None


# ---- Permissions par fonctionnalité (rôle -> features) ----
# Personas : « comptable » (compta courante), « gestion » (= personne TopOrder : Clients +
# Paiements), « salaires » (comptable PAIE, dédiée : voit UNIQUEMENT les comptes 421 salariés).
# La paie est ISOLÉE : ni « gestion » ni « comptable » ne la voient — seule la rôle « salaires »
# (et l'admin/superuser pour supervision).
_CAISSE = {"suivi", "jobs", "clients", "paiements", "config"}   # modules Groupe FDF (TopOrder)
_ODOO = {"odoo", "inqom", "jobs"}                               # modules Groupe ISFAHAAN (Odoo/Inqom × Pennylane)
_FULL = _CAISSE                                                 # alias historique
_ALL = _CAISSE | {"salaires"} | _ODOO

# ---- Registre SOCIÉTÉ -> MODULES ----
# Chaque société n'expose QUE les modules de son groupe : rien n'est partagé par défaut.
# Ajouter une société = une ligne ici (+ seed) : ses tuiles/routes sont gardées partout
# via can()/company_features(), même pour l'admin (pas de module « caisse » sur une
# société Odoo, pas de module Odoo sur une boulangerie, etc.).
COMPANY_MODULES = {
    # Groupe FDF (boulangeries + labo — TopOrder)
    "STERNA":    _CAISSE | {"salaires"},
    "KOOKABURA": _CAISSE | {"salaires"},
    "PP126.23":  {"salaires"},                 # paie seule
    # Groupe ISFAHAAN (Odoo × Pennylane — PAS de TopOrder)
    "LACORP":    set(_ODOO),
    "JBIBFOOD":  {"inqom", "jobs"},            # pas d'Odoo raccordé (à ajouter si clés fournies)
}


def company_features(company_code: Optional[str]) -> set:
    """Modules réellement disponibles sur cette société (registre ci-dessus).
    Société inconnue du registre -> AUCUN module (opt-in explicite)."""
    return set(COMPANY_MODULES.get(company_code, set()))


def features_for(role: Optional[str], is_superuser: bool = False) -> set:
    if is_superuser:
        return set(_ALL)
    if role == "salaires":
        return {"salaires"}
    if role in ("gestion", "viewer"):
        return {"clients", "paiements"}
    if role in ("comptable", "admin", "operator"):
        # le périmètre réel est ENSUITE intersecté avec les modules de la société :
        # un comptable ISFAHAAN ne voit que « odoo », un comptable FDF que la caisse.
        return _CAISSE | _ODOO
    return set()


def can(user: Optional[User], company: Company, feature: str) -> bool:
    """L'utilisateur a-t-il accès à cette fonctionnalité sur cette société ?"""
    if not user:
        return False
    if feature not in company_features(company.code):   # la société n'expose pas cette feature
        return False
    if user.is_superuser:
        return True
    return feature in features_for(role_for(user, company))


def require_company(request: Request, code: str) -> Company:
    user = require_user(request)
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == code)).first()
    if not company or role_for(user, company) is None:
        raise HTTPException(status_code=403, detail="Accès refusé à cette société")
    return company
