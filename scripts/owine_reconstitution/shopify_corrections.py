"""Corrections Shopify validées par JS le 14/09/2026 (onglet « Points à vérifier » du contrôle de cohérence) :
  #11 coûts d'achat alignés sur les factures (Chenevottes 2023, Santenay 2023 Caroline Morey, Clos Genet 2022) ;
  #12 vins en dépôt-vente présents dans les deux BLV : coût = prix du BLV le plus élevé (valeur assurée) ;
  #8  produit Shopify PYCCSMPADMBB17 (doublon d'Abbaye de Morgeot 2017, sans stock) archivé.
Usage : .venv/bin/python scripts/owine_reconstitution/shopify_corrections.py [dry|apply]"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core import localenv; localenv.load(db=True)
from app.packs.owine import service
from app.packs.owine.service import _shopify

MODE = sys.argv[1] if len(sys.argv) > 1 else "dry"
COSTS = {"DPYCM-CM1CCC-B23": 65.0, "DCM-SV-R23": 19.0, "FDCSTNVCGERB22": 14.5,                                   # #11
         "CAMCSMPCAIBB15": 240.0, "PYCCSMPCNVBB15": 220.0, "PYCMSTPLCHBB15": 260.0, "PYCMSTPPRZBB15": 245.0, "PYCPUMPLGRBB15": 110.0}  # #12
LMB_SKUS = {"CAMCSMPCAIBB15", "PYCCSMPCNVBB15", "PYCMSTPLCHBB15", "PYCMSTPPRZBB15", "PYCPUMPLGRBB15"}
DUPLICATE = "PYCCSMPADMBB17"
c = _shopify()
Q = """query($q:String!){ productVariants(first:5, query:$q){ edges{ node{ id sku product{ id title status } inventoryItem{ id unitCost{ amount } } } } } }"""


def variant(sku):
    d = c.gql(Q, {"q": f"sku:{sku}"})
    return next((e["node"] for e in d["productVariants"]["edges"] if e["node"]["sku"] == sku), None)


for sku, cost in COSTS.items():
    v = variant(sku)
    if not v:
        print(sku, "INTROUVABLE dans Shopify"); continue
    old = (v["inventoryItem"].get("unitCost") or {}).get("amount")
    print(f"{sku:16} {v['product']['title'][:55]:55} coût {old} → {cost:.2f}", "" if MODE == "apply" else "(simulation)")
    if MODE == "apply":
        r = c.gql("""mutation($id:ID!,$input:InventoryItemInput!){ inventoryItemUpdate(id:$id, input:$input){ inventoryItem{ unitCost{ amount } } userErrors{ field message } } }""",
                  {"id": v["inventoryItem"]["id"], "input": {"cost": f"{cost:.2f}"}})
        res = r["inventoryItemUpdate"]
        if res.get("userErrors"):
            print("   ERREUR", res["userErrors"])
        else:
            service.upsert_item(sku, cost=cost, cost_source="shopify", **({"lmb_price": cost} if sku in LMB_SKUS else {}))
            print("   OK", (res.get("inventoryItem") or {}).get("unitCost"))
v = variant(DUPLICATE)
print(f"\n{DUPLICATE} :", (v["product"]["id"], v["product"]["title"], v["product"]["status"]) if v else "introuvable")
if v and v["product"]["status"] != "ARCHIVED" and MODE == "apply":
    try:
        r = c.gql("""mutation($p:ProductUpdateInput!){ productUpdate(product:$p){ product{ id status } userErrors{ field message } } }""", {"p": {"id": v["product"]["id"], "status": "ARCHIVED"}})
    except Exception as e:  # anciennes versions de l'API : argument « input »
        print("   productUpdate(product) refusé :", str(e)[:120])
        r = c.gql("""mutation($i:ProductInput!){ productUpdate(input:$i){ product{ id status } userErrors{ field message } } }""", {"i": {"id": v["product"]["id"], "status": "ARCHIVED"}})
    print("   archivage :", r["productUpdate"])
    if (r["productUpdate"].get("product") or {}).get("status") == "ARCHIVED":
        service.upsert_item(DUPLICATE, status="ARCHIVED")
print("\nterminé —", "modifications appliquées" if MODE == "apply" else "aucune modification (relancer avec « apply »)")
