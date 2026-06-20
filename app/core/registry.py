"""Registre des packs de contrôle.

Un pack = un module métier autonome pour un couple (société, domaine). Il
déclare ses tuiles de dashboard, ses contrôles (anomalies) et ses actions.
Le socle ne sait rien du métier ; il se contente d'appeler ces hooks.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class Tile:
    """Une tuile de dashboard (indicateur à code couleur)."""
    key: str
    label: str
    status: str = "neutral"        # ok / warn / error / neutral
    value: Optional[str] = None    # ex. "12 / 3 480,00 €"
    detail_url: Optional[str] = None
    hint: Optional[str] = None


@dataclass
class Action:
    key: str
    label: str
    danger: bool = False


@dataclass
class Pack:
    company_code: str              # STERNA, KOOKABURA
    domain: str                    # caisse, paie, ...
    title: str
    # hooks (ctx = contexte d'exécution : société, session DB, paramètres)
    tiles_fn: Optional[Callable] = None
    actions: List[Action] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"{self.company_code.lower()}.{self.domain}"

    def tiles(self, ctx) -> List[Tile]:
        if self.tiles_fn:
            try:
                return self.tiles_fn(ctx)
            except Exception as e:  # un pack qui plante ne casse pas le dashboard
                return [Tile(key="error", label=self.title, status="error", value=str(e)[:80])]
        return []


_REGISTRY: List[Pack] = []


def register(pack: Pack) -> None:
    _REGISTRY.append(pack)


def packs_for(company_code: str) -> List[Pack]:
    return [p for p in _REGISTRY if p.company_code == company_code]


def all_packs() -> List[Pack]:
    return list(_REGISTRY)
