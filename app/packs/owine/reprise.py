"""Reprise du 14/09/2026, lancée depuis le tableau de bord OWINE (idempotente) :
  • livre des mouvements reconstitué : les lignes « Reprise Shopify 13/09/2026 » (alignement provisoire sur Shopify) sont remplacées par les achats réels
    (14 factures vignerons Pennylane, lignes relues sur les PDF), deux ventes absentes du Sheet sont ajoutées, quatre anciennes références Shopify sont
    remappées, le 2e lot d'Abbaye de Morgeot 2017 (BLV 2) est rattaché au produit Shopify PYCMCM1MB17 et daté de sa réception chez Alix ;
  • corrections Shopify validées par JS : coûts alignés sur les factures, vins du BLV 2 revalorisés au prix le plus élevé, doublon PYCCSMPADMBB17 archivé.
Données : scripts/owine_reconstitution/achats.json (contrôle de cohérence « 20260914 02 »)."""
import json, os
from datetime import date, datetime
from sqlmodel import Session, select
from app.core.db import engine
from app.models import OwMove, OwCarton, OwOrder
from . import service

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ACHATS = os.path.join(ROOT, "scripts", "owine_reconstitution", "achats.json")
PLACEHOLDER_REF = "Reprise Shopify 13/09/2026"
REMAP = {"FDCSTNVCCORB23": "FDCSTNVCCORB22", "MIMALCVXXXRB22": "MIMLDXVLCRRB22", "PYC-CHAMPLOTS-2023": "DPYCM-SA1CC-B23", "PYC-CHENEVOTTES-2023": "DPYCM-CM1CCC-B23"}
MISSING_SALES = [("OW1016", "2025-10-22", "PYCMSTPLCHBB15", 4, "LMB", 130.0), ("OW1022", "2025-11-20", "PYCCSMPCNVBB22", 2, "OWINE", 62.0)]
COSTS = {"DPYCM-CM1CCC-B23": 65.0, "DCM-SV-R23": 19.0, "FDCSTNVCGERB22": 14.5,                                                                  # #11 facture ≠ Shopify
         "CAMCSMPCAIBB15": 240.0, "PYCCSMPCNVBB15": 220.0, "PYCMSTPLCHBB15": 260.0, "PYCMSTPPRZBB15": 245.0, "PYCPUMPLGRBB15": 110.0}      # #12 prix BLV le plus élevé
LMB_SKUS = {"CAMCSMPCAIBB15", "PYCCSMPCNVBB15", "PYCMSTPLCHBB15", "PYCMSTPPRZBB15", "PYCPUMPLGRBB15"}
DUPLICATE = "PYCCSMPADMBB17"


def state() -> dict:
    ms = service.moves(limit=100000); imap = service.item_map()
    n_ph = sum(1 for m in ms if m.kind == "initial" and m.ref == PLACEHOLDER_REF)
    n_pur = sum(1 for m in ms if m.kind == "purchase" and m.owner == "OWINE" and m.location == "ALIX")   # achats OWINE chez Alix (pas les achats LMB rue de Chaux)
    pending = [sku for sku, c in COSTS.items() if sku in imap and abs((imap[sku].cost or 0) - c) > 0.005]
    dup = imap.get(DUPLICATE)
    return {"placeholder": n_ph, "purchases": n_pur, "livre_done": n_ph == 0 and n_pur > 0, "shopify_pending": pending, "dup_pending": bool(dup and dup.status != "ARCHIVED"),
            "shopify_done": not pending and not (dup and dup.status != "ARCHIVED")}


def apply_livre(log=None) -> str:
    log = log or (lambda m: None)
    if state()["livre_done"]:
        log("livre déjà reconstitué : rien à faire"); return "déjà appliquée"
    ach = json.load(open(ACHATS, encoding="utf-8"))
    with Session(engine) as s:
        rows = s.exec(select(OwMove).where(OwMove.ref == PLACEHOLDER_REF, OwMove.kind == "initial")).all()
        for m in rows:
            s.delete(m)
        log(f"{len(rows)} ligne(s) « {PLACEHOLDER_REF} » retirée(s)")
        n = 0
        for m in s.exec(select(OwMove).where(OwMove.sku.in_(list(REMAP)))).all():
            m.note = f"réf. Shopify de l'époque : {m.sku}" + (f" ; {m.note}" if m.note else ""); m.sku = REMAP[m.sku]; s.add(m); n += 1
        for c in s.exec(select(OwCarton)).all():
            if any(k in (c.lines or "") for k in REMAP):
                for k, v in REMAP.items():
                    c.lines = c.lines.replace(f'"{k}"', f'"{v}"')
                s.add(c); n += 1
        for o in s.exec(select(OwOrder)).all():
            if any(k in (o.lines or "") for k in REMAP):
                for k, v in REMAP.items():
                    o.lines = o.lines.replace(f'"{k}"', f'"{v}"')
                s.add(o); n += 1
        log(f"{n} référence(s) Shopify d'époque remappée(s) (mouvements, cartons, lignes)")
        for m in s.exec(select(OwMove).where(OwMove.ref == "BLV 2026012901")).all():
            if m.sku == DUPLICATE:
                m.sku = "PYCMCM1MB17"; m.note = "réf. BLV : PYCCSMPADMBB17 (2e lot à 135 €, produit Shopify PYCMCM1MB17)"
            if m.kind == "deposit_in":
                m.date = date(2026, 2, 2)
            s.add(m)
        s.commit()
    refs = {(m.ref or "") for m in service.moves(limit=100000) if m.kind == "purchase"}
    n = 0
    for a in ach:
        ref = f"F {a['facture'].split(' ')[0]}"
        if ref in refs:
            continue
        for l in a["lignes"]:
            service.add_move(l[0], l[1], "purchase", d=date.fromisoformat(a["recu"]), location="ALIX", owner="OWINE", ref=ref, unit_cost=l[2],
                             note=f"{a['fournisseur']} — facture {a['facture']} du {a['date'][8:]}/{a['date'][5:7]}/{a['date'][:4]}, reçue chez Alix le {a['recu'][8:]}/{a['recu'][5:7]}/{a['recu'][:4]}"
                                  + (f" — libellé facture : {l[3]}" if len(l) > 3 else ""), source="pennylane"); n += 1
    log(f"{n} ligne(s) d'achat ajoutée(s) ({len(ach)} factures)")
    k = 0
    for ref, d, sku, q, owner, cost in MISSING_SALES:
        if any(m.kind in ("sale", "pickup") and m.sku == sku for m in service.moves(ref=ref, limit=200)):
            continue
        service.add_move(sku, -q, "sale", d=date.fromisoformat(d), location="ALIX", owner=owner, ref=ref, unit_cost=cost, note="vente reconstituée (commande Shopify payée et expédiée, absente du Sheet)", source="reconstitution"); k += 1
    log(f"{k} vente(s) manquante(s) ajoutée(s) (OW1016, OW1022)")
    service._setting_set("owine:reprise:livre", {"at": datetime.utcnow().isoformat(timespec="seconds"), "purchases": n, "sales": k})
    return f"livre reconstitué : {n} achats, {k} ventes ajoutées"


def apply_shopify(log=None) -> str:
    log = log or (lambda m: None)
    c = service._shopify()
    if not c:
        raise RuntimeError("Shopify non configuré")
    q = """query($q:String!){ productVariants(first:5, query:$q){ edges{ node{ id sku product{ id title status } inventoryItem{ id unitCost{ amount } } } } } }"""

    def variant(sku):
        d = c.gql(q, {"q": f"sku:{sku}"})
        return next((e["node"] for e in d["productVariants"]["edges"] if e["node"]["sku"] == sku), None)
    st = state(); n = 0
    for sku in st["shopify_pending"]:
        cost = COSTS[sku]; v = variant(sku)
        if not v:
            log(f"{sku} : introuvable dans Shopify"); continue
        r = c.gql("""mutation($id:ID!,$input:InventoryItemInput!){ inventoryItemUpdate(id:$id, input:$input){ inventoryItem{ unitCost{ amount } } userErrors{ field message } } }""",
                  {"id": v["inventoryItem"]["id"], "input": {"cost": f"{cost:.2f}"}})
        res = r["inventoryItemUpdate"]
        if res.get("userErrors"):
            log(f"{sku} : {res['userErrors']}"); continue
        service.upsert_item(sku, cost=cost, cost_source="shopify", **({"lmb_price": cost} if sku in LMB_SKUS else {}))
        log(f"{sku} : coût {((v['inventoryItem'].get('unitCost') or {}).get('amount'))} → {cost:.2f} €"); n += 1
    if st["dup_pending"]:
        v = variant(DUPLICATE)
        if v and v["product"]["status"] != "ARCHIVED":
            try:
                r = c.gql("""mutation($p:ProductUpdateInput!){ productUpdate(product:$p){ product{ id status } userErrors{ field message } } }""", {"p": {"id": v["product"]["id"], "status": "ARCHIVED"}})
            except Exception:
                r = c.gql("""mutation($i:ProductInput!){ productUpdate(input:$i){ product{ id status } userErrors{ field message } } }""", {"i": {"id": v["product"]["id"], "status": "ARCHIVED"}})
            res = r["productUpdate"]
            if (res.get("product") or {}).get("status") == "ARCHIVED":
                service.upsert_item(DUPLICATE, status="ARCHIVED"); log(f"{DUPLICATE} : produit Shopify archivé"); n += 1
            else:
                log(f"{DUPLICATE} : archivage refusé {res.get('userErrors')}")
        elif v:
            service.upsert_item(DUPLICATE, status="ARCHIVED"); log(f"{DUPLICATE} : déjà archivé dans Shopify")
        else:
            log(f"{DUPLICATE} : introuvable dans Shopify")
    service._setting_set("owine:reprise:shopify", {"at": datetime.utcnow().isoformat(timespec="seconds"), "n": n})
    return f"{n} correction(s) Shopify appliquée(s)"
