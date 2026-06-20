"""Découverte des packs de contrôle.

Importer un module de pack suffit à l'enregistrer (il appelle registry.register
au chargement). On liste ici les packs actifs ; à terme on pourra scanner
automatiquement le dossier app/packs/.
"""
import importlib

# packs à charger (module Python)
_PACK_MODULES = [
    "app.packs.sterna_caisse.pack",
    # "app.packs.kookabura_caisse.pack",
]


def load() -> None:
    for mod in _PACK_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as e:  # ne bloque pas le démarrage du socle
            print(f"[packs_loader] échec chargement {mod}: {e}")
