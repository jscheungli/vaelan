"""Reconstitution du livre des mouvements OWINE : achats (factures Pennylane) + BLV LMB + ventes ; comparaison avec Shopify.
Usage : reconstitution.py dry | apply"""
import sys, json, os
from datetime import date, datetime
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core import localenv; localenv.load(db=True)
from app.packs.owine import service
D = os.path.dirname(os.path.abspath(__file__)); MODE = sys.argv[1] if len(sys.argv) > 1 else "dry"
ach = json.load(open(D + "/achats.json")); data = json.load(open(D + "/stock_data.json")); items = data["items"]; shop = data["shopify"]
imap = service.item_map()
# ---- ventes manquantes (commandes payées et expédiées sans mouvement)
MISSING_SALES = [("OW1016", "2025-10-22", "PYCMSTPLCHBB15", 4, "LMB", 130.0), ("OW1022", "2025-11-20", "PYCCSMPCNVBB22", 2, "OWINE", 62.0)] if MODE != "final" else []
# ---- état courant
moves = service.moves(limit=100000)
agg = defaultdict(lambda: defaultdict(float))
for m in moves:
    if m.location != "ALIX": continue
    agg[m.sku][(m.owner, m.kind)] += m.qty
for ref, d, sku, q, owner, cost in MISSING_SALES:
    agg[sku][(owner, "sale")] -= q
pur = defaultdict(float); pur_val = defaultdict(float)
if MODE == "final":
    for m in moves:
        if m.kind == "purchase" and m.location == "ALIX": pur[m.sku] += m.qty; pur_val[m.sku] += m.qty * (m.unit_cost or 0)
else:
    for a in ach:
        for l in a["lignes"]:
            pur[l[0]] += l[1]; pur_val[l[0]] += l[1] * l[2]
rows = []
for sku in sorted(set(agg) | set(pur) | {s for s, v in shop.items() if (v or {}).get("on_hand")}):
    it = imap.get(sku)
    if it and it.kind != "wine": continue
    a = agg[sku]
    own_sales = a[("OWINE", "sale")] + a[("OWINE", "pickup")]; lmb_in = a[("LMB", "deposit_in")]; lmb_sales = a[("LMB", "sale")] + a[("LMB", "pickup")]
    theo_ow = pur[sku] + own_sales; theo_lmb = lmb_in + lmb_sales
    onh = (shop.get(sku) or {}).get("on_hand"); onh = None if onh is None else float(onh)
    rows.append({"sku": sku, "title": it.title if it else sku, "achats": pur[sku], "ventes_owine": -own_sales, "theo_owine": theo_ow, "depot_lmb": lmb_in, "ventes_lmb": -lmb_sales, "theo_lmb": theo_lmb,
                 "theo_total": theo_ow + theo_lmb, "shopify": onh, "ecart": None if onh is None else onh - (theo_ow + theo_lmb), "initial": a[("OWINE", "initial")]})
tot = lambda k: sum((r[k] or 0) for r in rows)
print(f"achats {tot('achats'):.0f} btl ({sum(pur_val.values()):.2f} € HT) | ventes OWINE {tot('ventes_owine'):.0f} | théorique OWINE {tot('theo_owine'):.0f} || dépôt LMB {tot('depot_lmb'):.0f} | ventes LMB {tot('ventes_lmb'):.0f} | théorique LMB {tot('theo_lmb'):.0f} || Shopify {tot('shopify'):.0f} | écart {tot('ecart'):+.0f}")
print("\n=== écarts Shopify − théorique (vins) ===")
for r in sorted(rows, key=lambda r: (r["ecart"] is None, -(abs(r["ecart"] or 0)))):
    if r["ecart"] not in (0, None):
        print(f"  {r['sku']:16} {r['title'][:60]:60} achats {r['achats']:>3.0f} ventes {r['ventes_owine']:>3.0f} | LMB {r['depot_lmb']:>3.0f}-{r['ventes_lmb']:>3.0f} | théo {r['theo_total']:>4.0f} Shopify {r['shopify']:>4.0f} → {r['ecart']:+.0f}")
print("\n=== sans inventaire Shopify (None) ===", [(r["sku"], r["theo_total"]) for r in rows if r["ecart"] is None and r["theo_total"]])
print("=== négatifs théoriques ===", [(r["sku"], r["theo_owine"], r["theo_lmb"]) for r in rows if r["theo_owine"] < 0 or r["theo_lmb"] < 0])
print("=== écart nul :", sum(1 for r in rows if r["ecart"] == 0), "vins sur", len(rows))
json.dump(rows, open(D + "/reconstitution_rows.json", "w"), ensure_ascii=False, indent=1)
if MODE == "apply":
    from sqlmodel import Session, select
    from app.core.db import engine
    from app.models import OwMove, OwCarton, OwOrder, OwItem, OwTask
    REMAP = {"FDCSTNVCCORB23": "FDCSTNVCCORB22", "MIMALCVXXXRB22": "MIMLDXVLCRRB22", "PYC-CHAMPLOTS-2023": "DPYCM-SA1CC-B23", "PYC-CHENEVOTTES-2023": "DPYCM-CM1CCC-B23"}
    with Session(engine) as s:
        # 1. les 68 lignes « Reprise Shopify 13/09/2026 » (placeholder posé le 13/09 en attendant les achats) sont remplacées par les achats réels
        rows = s.exec(select(OwMove).where(OwMove.ref == "Reprise Shopify 13/09/2026", OwMove.kind == "initial")).all()
        for m in rows: s.delete(m)
        print("lignes 'Reprise Shopify' retirées :", len(rows))
        # 2. anciennes références Shopify → références actuelles (mouvements, cartons, lignes de commande)
        n = 0
        for m in s.exec(select(OwMove).where(OwMove.sku.in_(list(REMAP)))).all():
            m.note = f"réf. Shopify de l'époque : {m.sku}" + (f" ; {m.note}" if m.note else ""); m.sku = REMAP[m.sku]; s.add(m); n += 1
        for c in s.exec(select(OwCarton)).all():
            if any(k in (c.lines or "") for k in REMAP):
                for k, v in REMAP.items(): c.lines = c.lines.replace(f'"{k}"', f'"{v}"')
                s.add(c); n += 1
        for o in s.exec(select(OwOrder)).all():
            if any(k in (o.lines or "") for k in REMAP):
                for k, v in REMAP.items(): o.lines = o.lines.replace(f'"{k}"', f'"{v}"')
                s.add(o); n += 1
        print("références remappées :", n)
        # 3. BLV 2 : Abbaye de Morgeot 2017 (2e lot, 135 €) = produit Shopify PYCMCM1MB17 ; entrée chez Alix le 02/02/2026
        for m in s.exec(select(OwMove).where(OwMove.ref == "BLV 2026012901")).all():
            if m.sku == "PYCCSMPADMBB17":
                m.sku = "PYCMCM1MB17"; m.note = "réf. BLV : PYCCSMPADMBB17 (2e lot, produit Shopify distinct)"
            if m.kind == "deposit_in":
                m.date = date(2026, 2, 2)
            s.add(m)
        # 4. sélections : type d'article « selection », tâches de coût fermées
        for it in s.exec(select(OwItem).where(OwItem.sku.like("SEL-%"))).all():
            it.kind = "selection"; s.add(it)
        for t in s.exec(select(OwTask).where(OwTask.kind == "cost_missing", OwTask.status == "open")).all():
            if (t.ref or "").upper().startswith("SEL-"):
                t.status = "done"; t.done_at = datetime.utcnow(); s.add(t)
        s.commit()
    # 5. achats (factures Pennylane) et ventes manquantes
    n = 0
    for a in ach:
        for l in a["lignes"]:
            service.add_move(l[0], l[1], "purchase", d=date.fromisoformat(a["recu"]), location="ALIX", owner="OWINE", ref=f"F {a['facture'].split(' ')[0]}", unit_cost=l[2],
                             note=f"{a['fournisseur']} — facture {a['facture']} du {a['date'][8:]}/{a['date'][5:7]}/{a['date'][:4]}, reçue chez Alix le {a['recu'][8:]}/{a['recu'][5:7]}/{a['recu'][:4]}" + (f" — libellé facture : {l[3]}" if len(l) > 3 else ""), source="pennylane"); n += 1
    for ref, d, sku, q, owner, cost in MISSING_SALES:
        service.add_move(sku, -q, "sale", d=date.fromisoformat(d), location="ALIX", owner=owner, ref=ref, unit_cost=cost, note="vente reconstituée (commande Shopify payée et expédiée, absente du Sheet)", source="reconstitution"); n += 1
    print("mouvements ajoutés :", n)
