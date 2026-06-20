"""Pack de contrôle : Sterna — Caisse (squelette Phase 0).

Les tuiles renvoient pour l'instant un statut « neutre / à brancher » : la
logique métier (cadrage tickets↔synthèse, justificatifs, anomalies) sera
implémentée en Phase 1+ en réutilisant les moteurs migrés depuis les scripts.
"""
from app.core.registry import Pack, Tile, Action, register

# clés de contrôle prévues pour ce pack (chacune deviendra une tuile « vivante »)
_PLANNED = [
    ("cadrage", "Cadrage CA = synthèse (par date)"),
    ("justificatifs", "Justificatifs manquants"),
    ("pro_non_facturees", "Commandes pro non facturées (M-1)"),
    ("b2c_non_soldees", "Commandes B2C livrées non soldées"),
    ("lettrage_411", "Ancienneté du non-lettrage 411"),
    ("creances", "Créances ouvertes"),
]


def tiles(ctx):
    return [Tile(key=k, label=label, status="neutral", value="à brancher") for k, label in _PLANNED]


register(Pack(
    company_code="STERNA",
    domain="caisse",
    title="Caisse",
    tiles_fn=tiles,
    actions=[
        Action(key="import", label="Générer un import (CSV)"),
        Action(key="attach_pj", label="Attacher les PJ manquantes"),
    ],
))
