"""Config du pack Sterna — Caisse : établissements et comptes Pennylane.

Migré depuis les scripts (est_config). Les numéros de compte/journaux servent à
la génération CSV ; le shop_id sert au pull TopOrder.
NB : le CSV d'import manuel Pennylane référence les comptes par NUMÉRO.
"""
import json
import os

# établissement -> shop_id TopOrder + préfixe court
# Ordre d'affichage habituel : Sainte-Marie, La Possession, Saint-Leu (menus + tableau de suivi).
ESTABLISHMENTS = {
    "OCOPAIN SAINTE-MARIE":  {"pfx": "SM", "shop_id": "08de7f5d-bc8d-407f-850c-e5c0dded9eea"},
    "OCOPAIN LA POSSESSION": {"pfx": "LP", "shop_id": "08de7f5e-5e84-444a-8c9a-f7e86ed0e785"},
    "OCOPAIN SAINT-LEU":     {"pfx": "SL", "shop_id": "08de7f5e-07cb-4114-8211-0a8c8b7e012a"},
}

# Comptes Pennylane STERNA (numéros), par préfixe d'établissement.
#   X = 2e chiffre des comptes 41125X.. : SM=1, LP=2, SL=3
_X = {"SM": "1", "LP": "2", "SL": "3"}

CA_ANONYME = "70101"      # caisse anonyme
CA_B2C = "70102"          # particuliers (différé)
CA_B2B = "7012"           # B2B (compte unique ; le taux est porté par la colonne TVA)
TVA_ACCOUNT = {"2.1": "44571005", "8.5": "44571007", "5.5": "44571006", "20": "44571009"}

# Analytique Pennylane (catégories par établissement) : matching par NOM dans le CSV.
# Groupe « Etablissement » (id 819366) ; catégories SAINT-LEU/LA POSSESSION/SAINTE-MARIE.
ANALYTIC_FAMILY = "Etablissement"
ANALYTIC_CATEGORY = {"SL": "SAINT-LEU", "LP": "LA POSSESSION", "SM": "SAINTE-MARIE"}

# Mapping client (companyId/customerId -> compte 411), migré et validé (overrides inclus).
# TODO : à terme, remplacer par un module « résolution client » (job + table DB).
with open(os.path.join(os.path.dirname(__file__), "data", "clients.json"), encoding="utf-8") as _f:
    CLIENTS = json.load(_f)

# ============================ KOOKABURA (société Pennylane SÉPARÉE) ============================
# Labo interne 100% B2B : KK facture les 3 boulangeries STERNA. Modèle FACTURE (pas de caisse) :
# CA depuis les factures (totalPriceHTByVATRate), pas les tickets. Pas d'analytique.
ESTABLISHMENTS_KK = {
    "KOOKABURA": {"pfx": "KK", "shop_id": "08de7f5f-18c7-4c5c-8547-3102b7617acd"},
}
JOURNALS_KK = {"KK": {"tickets": 84717481984, "factures": 84717486080}}  # TOKKT, TOKKF
# Clients B2B = les 3 boulangeries (companyId KK -> compte 411 dans la société KOOKABURA)
CLIENTS_KK = {"b2b": {
    "KK:08dea426-2813-448b-8590-949870c0adae": {"account": "411100022", "name": "OCOPAIN LA POSSESSION"},
    "KK:08dea426-64ac-4f40-8d30-fe5cc51fec58": {"account": "411100019", "name": "OCOPAIN SAINTE-MARIE"},
    "KK:08dea425-1895-4263-8703-45073e145fd2": {"account": "411100021", "name": "OCOPAIN SAINT-LEU"},
}, "b2c_commun": {}}

_FACTURE_COMPANIES = {"KOOKABURA"}   # sociétés au modèle « facture » (pas de caisse)


def is_facture_model(company_code: str) -> bool:
    return company_code in _FACTURE_COMPANIES


def establishments(company_code: str) -> dict:
    return ESTABLISHMENTS_KK if company_code == "KOOKABURA" else ESTABLISHMENTS


def clients(company_code: str) -> dict:
    return CLIENTS_KK if company_code == "KOOKABURA" else CLIENTS


def _kk_defaults() -> dict:
    est = {"KK": {"category": None,
                  "journal_tickets": "TOKKT", "journal_tickets_id": "84717481984",
                  "journal_factures": "TOKKF", "journal_factures_id": "84717486080"}}
    return {"ca_anonyme": "70101", "ca_b2c": None, "ca_b2b": "7012",
            "analytic_family": None, "tva": dict(TVA_ACCOUNT), "est": est}


def resolve_411(pfx: str, company_id, customer_id):
    """Renvoie (numéro de compte 411, libellé client) pour router une créance."""
    if company_id:
        e = CLIENTS["b2b"].get(f"{pfx}:{company_id}")
        if e and e.get("account"):
            return e["account"], (e.get("name") or "client")
    if customer_id:
        return CLIENTS["b2c_commun"].get(pfx), "Particulier"
    return None, None

# journaux Pennylane par établissement : (TOSLT tickets, TOSLF factures), cat analytique
# Les ids numériques servent à l'API ; le CSV d'import manuel référence le journal
# par son CODE (TOSLT/TOSLF) -> voir journal_code().
JOURNALS = {
    "SL": {"tickets": 78512312320, "factures": 84363386880, "analytic": 10174227},
    "LP": {"tickets": 84391071744, "factures": 84391256064, "analytic": 10174226},
    "SM": {"tickets": 84392247296, "factures": 84392374272, "analytic": 10174225},
}


def journal_code(pfx: str, kind: str) -> str:
    """Code journal Pennylane (pour le CSV) : tickets -> TO<PFX>T, factures -> TO<PFX>F."""
    return f"TO{pfx}{'T' if kind == 'tickets' else 'F'}"


def caisse_accounts(pfx: str) -> dict:
    """Comptes d'encaissement / écart de caisse de l'établissement (défauts)."""
    x = _X[pfx]
    return {
        "especes": f"41125{x}01", "cb": f"41125{x}02", "ticket_resto": f"41125{x}03",
        "autres": f"41125{x}04", "ecart": f"41125{x}06", "b2c_commun": f"41125{x}09",
    }


# ------------------------------------------------------------------ config résolue
# Schéma plat éditable : groupes -> {clé: (libellé, valeur par défaut)}. La page
# Configuration lit/écrit ces clés ; resolve() applique les surcharges DB (Setting).
def _defaults() -> dict:
    est = {}
    for name, e in ESTABLISHMENTS.items():
        pfx = e["pfx"]
        j = JOURNALS[pfx]
        est[pfx] = {
            "category": ANALYTIC_CATEGORY[pfx],
            "journal_tickets": journal_code(pfx, "tickets"),
            "journal_tickets_id": str(j["tickets"]),
            "journal_factures": journal_code(pfx, "factures"),
            "journal_factures_id": str(j["factures"]),
            **caisse_accounts(pfx),
        }
    return {"ca_anonyme": CA_ANONYME, "ca_b2c": CA_B2C, "ca_b2b": CA_B2B,
            "analytic_family": ANALYTIC_FAMILY, "tva": dict(TVA_ACCOUNT), "est": est}


# libellés lisibles pour la page Configuration (clé « : » -> libellé)
ACCOUNT_FIELDS = [("especes", "Espèces"), ("cb", "CB"), ("ticket_resto", "Ticket restaurant"),
                  ("autres", "Autres / compte client"), ("ecart", "Écart de caisse"),
                  ("b2c_commun", "Particuliers (compte commun)")]
JOURNAL_FIELDS = [("journal_tickets", "Code journal Tickets (CSV)"),
                  ("journal_tickets_id", "ID journal Tickets (API)"),
                  ("journal_factures", "Code journal Factures (CSV)"),
                  ("journal_factures_id", "ID journal Factures (API)"),
                  ("category", "Catégorie analytique")]


def _set_path(d, key, value):
    parts = key.split(":")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            return
        cur = cur[p]
    if parts[-1] in cur:
        cur[parts[-1]] = value


def resolve(company_code: str) -> dict:
    """Config effective d'une société : défauts du code + surcharges DB (Setting)."""
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import Setting
    cfg = _kk_defaults() if company_code == "KOOKABURA" else _defaults()
    with Session(engine) as s:
        for st in s.exec(select(Setting).where(Setting.company_code == company_code)).all():
            _set_path(cfg, st.key, st.value)
    return cfg


def config_sections(cfg: dict) -> list:
    """Structure la page Configuration : sections -> champs {key, label, value}."""
    sections = [
        {"title": "Comptes de vente (CA)", "fields": [
            {"key": "ca_anonyme", "label": "CA caisse anonyme (Tickets)", "value": cfg["ca_anonyme"]},
            {"key": "ca_b2c", "label": "CA particuliers / B2C (différé)", "value": cfg["ca_b2c"]},
            {"key": "ca_b2b", "label": "CA B2B", "value": cfg["ca_b2b"]},
        ]},
        {"title": "Comptes de TVA collectée", "fields": [
            {"key": f"tva:{r}", "label": f"TVA {r}%", "value": v} for r, v in cfg["tva"].items()
        ]},
        {"title": "Analytique", "fields": [
            {"key": "analytic_family", "label": "Famille de catégories", "value": cfg["analytic_family"]},
        ]},
    ]
    for pfx, e in cfg["est"].items():
        # KK (modèle facture) n'a pas de comptes de caisse -> on n'affiche que les champs présents
        sections.append({"title": f"Établissement {pfx}", "fields": [
            {"key": f"est:{pfx}:{k}", "label": lbl, "value": e[k]}
            for k, lbl in JOURNAL_FIELDS + ACCOUNT_FIELDS if k in e
        ]})
    return sections


def flat_defaults() -> dict:
    """Toutes les clés éditables -> valeur par défaut (pour comparer/réinitialiser)."""
    out = {"ca_anonyme": CA_ANONYME, "ca_b2c": CA_B2C, "ca_b2b": CA_B2B,
           "analytic_family": ANALYTIC_FAMILY}
    for r, acc in TVA_ACCOUNT.items():
        out[f"tva:{r}"] = acc
    d = _defaults()["est"]
    for pfx, e in d.items():
        for k, v in e.items():
            out[f"est:{pfx}:{k}"] = v
    return out
