"""Contrôle de cohérence stock Alix : livre des mouvements reconstitué en mémoire (sans écrire en base) + comparaison Shopify → Excel."""
import sys, json, os
from datetime import date
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core import localenv; localenv.load(db=True)
from app.packs.owine import service
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
D = os.path.dirname(os.path.abspath(__file__)); OUT = sys.argv[1]
ach = json.load(open(D + "/achats.json")); data = json.load(open(D + "/stock_data.json")); shop = data["shopify"]
imap = service.item_map()
REMAP = {"FDCSTNVCCORB23": "FDCSTNVCCORB22", "MIMALCVXXXRB22": "MIMLDXVLCRRB22", "PYC-CHAMPLOTS-2023": "DPYCM-SA1CC-B23", "PYC-CHENEVOTTES-2023": "DPYCM-CM1CCC-B23"}
MISSING_SALES = [("OW1016", "2025-10-22", "PYCMSTPLCHBB15", 4, "LMB", 130.0), ("OW1022", "2025-11-20", "PYCCSMPCNVBB22", 2, "OWINE", 62.0)]
L = []  # (date, kind, sku, qty, owner, location, ref, cost, note)
for m in service.moves(limit=100000):
    if m.kind == "initial" and m.ref == "Reprise Shopify 13/09/2026": continue
    if m.kind.startswith("packaging"): continue
    sku, note, d = m.sku, m.note or "", m.date
    if sku in REMAP: note = f"réf. Shopify de l'époque : {sku}"; sku = REMAP[sku]
    if m.ref == "BLV 2026012901":
        if sku == "PYCCSMPADMBB17": sku = "PYCMCM1MB17"; note = "réf. BLV : PYCCSMPADMBB17 (2e lot à 135 €, produit Shopify distinct)"
        if m.kind == "deposit_in": d = date(2026, 2, 2)
    L.append((d, m.kind, sku, m.qty, m.owner, m.location, m.ref or "", m.unit_cost, note))
for a in ach:
    for l in a["lignes"]:
        L.append((date.fromisoformat(a["recu"]), "purchase", l[0], float(l[1]), "OWINE", "ALIX", f"F {a['facture'].split(' ')[0]}", l[2], f"{a['fournisseur']} — facture {a['facture']} du {a['date'][8:]}/{a['date'][5:7]}/{a['date'][:4]}, reçue chez Alix le {a['recu'][8:]}/{a['recu'][5:7]}/{a['recu'][:4]}" + (f" — libellé facture : {l[3]}" if len(l) > 3 else "")))
for ref, d, sku, q, owner, cost in MISSING_SALES:
    L.append((date.fromisoformat(d), "sale", sku, -float(q), owner, "ALIX", ref, cost, "vente reconstituée (commande Shopify payée et expédiée, absente du Sheet)"))
KINDS = __import__("app.packs.owine.config", fromlist=["x"]).MOVE_KINDS
ORDER = {"purchase": 0, "deposit_out": 1, "deposit_in": 2, "sale": 3, "pickup": 3}
L.sort(key=lambda r: (r[0], ORDER.get(r[1], 9), r[6], r[2]), reverse=True)
# --- synthèse
agg = defaultdict(lambda: defaultdict(float))
for d, k, sku, q, owner, loc, ref, cost, note in L:
    if loc == "ALIX": agg[sku][(owner, k)] += q
title = lambda s: imap[s].title if s in imap else s
rows = []
for sku in sorted(set(agg) | {s for s, v in shop.items() if (v or {}).get("on_hand")}, key=title):
    it = imap.get(sku)
    if it and it.kind != "wine": continue
    a = agg[sku]; ach_q = a[("OWINE", "purchase")]; v_ow = -(a[("OWINE", "sale")] + a[("OWINE", "pickup")]); dep = a[("LMB", "deposit_in")]; v_lmb = -(a[("LMB", "sale")] + a[("LMB", "pickup")])
    sh = shop.get(sku) or {}; onh = sh.get("on_hand"); com = sh.get("committed") or 0; av = sh.get("available"); theo = ach_q - v_ow + dep - v_lmb
    rows.append([sku, title(sku), ach_q, v_ow, ach_q - v_ow, dep, v_lmb, dep - v_lmb, theo, onh, com, av, None if av is None else av - theo])
wb = Workbook(); H = Font(bold=True, color="FFFFFF"); F = PatternFill("solid", fgColor="0A2540"); B = Font(bold=True)
def sheet(ws, head, data, widths):
    ws.append(head)
    for c in ws[1]: c.font = H; c.fill = F; c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in data: ws.append(r)
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
ws = wb.active; ws.title = "Synthèse par vin"
sheet(ws, ["Réf. Shopify", "Vin", "Achats OWINE (btl)", "Ventes OWINE", "Théorique OWINE", "Dépôt LMB (BLV)", "Ventes LMB", "Théorique LMB", "Théorique total chez Alix", "Shopify : en stock (on hand)", "Shopify : engagé (commandes non expédiées)", "Shopify : disponible", "Écart disponible − théorique", "À vérifier avec Alix"],
      [r + [("" if r[12] in (0, None) else "OUI")] for r in rows], [18, 62, 10, 10, 10, 10, 10, 10, 12, 12, 14, 12, 12, 10])
for row in ws.iter_rows(min_row=2):
    if row[12].value not in (0, None):
        for c in row: c.fill = PatternFill("solid", fgColor="FFF3CD")
n = len(rows) + 2
ws.cell(row=n, column=2, value="TOTAL").font = B
for col in range(3, 14):
    ws.cell(row=n, column=col, value=f"=SUM({get_column_letter(col)}2:{get_column_letter(col)}{n-1})").font = B
ws2 = wb.create_sheet("Livre des mouvements")
sheet(ws2, ["Date", "Opération", "Réf. Shopify", "Vin", "Quantité", "Propriétaire", "Lieu", "Référence", "Coût / prix unitaire HT", "Note"],
      [[d, KINDS.get(k, k), sku, title(sku), q, owner, loc, ref, cost, note] for d, k, sku, q, owner, loc, ref, cost, note in L], [11, 30, 18, 62, 9, 11, 8, 22, 12, 80])
for c in ws2["A"][1:]: c.number_format = "DD/MM/YYYY"
ws3 = wb.create_sheet("Achats (factures)")
sheet(ws3, ["Date facture", "Reçu chez Alix", "Fournisseur", "N° facture", "Réf. Shopify", "Vin", "Quantité", "PU HT", "Total HT", "Libellé facture"],
      [[a["date"], a["recu"], a["fournisseur"], a["facture"], l[0], title(l[0]), l[1], l[2], l[1] * l[2], l[3] if len(l) > 3 else ""] for a in ach for l in a["lignes"]], [12, 12, 34, 30, 18, 62, 9, 9, 11, 50])
n3 = ws3.max_row + 1; ws3.cell(row=n3, column=6, value="TOTAL").font = B; ws3.cell(row=n3, column=7, value=f"=SUM(G2:G{n3-1})").font = B; ws3.cell(row=n3, column=9, value=f"=SUM(I2:I{n3-1})").font = B
ws4 = wb.create_sheet("Points à vérifier")
sheet(ws4, ["#", "Point", "Détail", "Décision / réponse"], [
    [1, "Aloxe-Corton Mallard : 2020 ou 2022 ?", "Facture 250476 du 05/05/2025 : « ALOXE CORTON 2022 » ; bon de livraison Alix n° 38 (manuscrit) : « Aloxe-Corton 2020 » ; Shopify : « Aloxe Corton Les Crapousuets 2020 » (MIMALCVLCRRB20). Retenu : 2020 (BL + Shopify).", ""],
    [2, "Bachelet-Monnot : facture 24250196 (10/03/2025, sans TVA) réémise en 25260019 (09/09/2025, avec TVA)", "Même commande n° 32425048, mêmes 45 bouteilles (24 Puligny, 3 Bâtard, 18 Santenay Prarons). Les deux sont enregistrées comme factures fournisseurs dans Pennylane (607 débité deux fois : 2 046 € + 2 046 €). Une seule livraison retenue dans le stock. À voir avec le comptable (avoir sur la première ?).", ""],
    [3, "Bachelet-Monnot 24250205 : doublon dans Pennylane", "La facture 24250205 (Maranges 2023, 1 728 € TTC) apparaît deux fois dans Pennylane (ids 2099444857 et 2031851866). Une seule livraison (BL 32425211 reçu chez Alix le 07/07/2025).", ""],
    [4, "Pièce « PAUL PILLOT 003 » 130,67 € HT (05/05/2025)", "Le PDF joint dans Pennylane est le bon de livraison Clair n° 25 + le BL Mallard n° 38, pas une facture Paul Pillot. Nature du montant à préciser (transport ?). Sans effet sur le stock.", ""],
    [5, "Pièce « BACHELET-MONNOT 32425211 » 111,80 € HT (23/05/2025)", "Numéro = celui du bon de livraison Geodis ; aucune ligne d'écriture. Probablement les frais de port. Sans effet sur le stock.", ""],
    [6, "Colin-Morey mars 2026 : proforma 260756 et offre 260757 enregistrées comme factures", "Pennylane contient la proforma (8 643 € HT, n° 260756) et l'offre Caroline Morey (4 284 € HT, saisie sous le n° « 2023 »). Les factures définitives ont-elles été reçues ? Réception Alix confirmée le 12/03/2026 (bons signés).", ""],
    [7, "Acomptes Colin-Morey du 07/09/2026 (2 663,33 + 1 150,00 € HT)", "Factures d'acompte (règlements 12405/12406) : réservation millésime 2024 ? Aucune bouteille livrée à ce jour — rien dans le stock.", ""],
    [8, "Abbaye de Morgeot 2017 Colin-Morey : deux produits Shopify", "BLV 1 (24 btl à 100 €) et BLV 2 (24 btl à 135 €) portent la même réf. PYCCSMPADMBB17. Dans Shopify, le 2e lot est le produit PYCMCM1MB17 (coût 135, 24 en stock) ; PYCCSMPADMBB17 (sans coût, sans stock) est un doublon à archiver. Les 24 du 1er lot sont toutes vendues (OW1021, 1030, 1035, 1036, 1038, 1039).", ""],
    [9, "Anciennes références Shopify dans les commandes OW1013 et OW1040", "FDCSTNVCCORB23 → FDCSTNVCCORB22 (Clos de la Comme : seul le 2022 a été acheté) ; MIMALCVXXXRB22 → MIMALCVLCRRB20 ; PYC-CHAMPLOTS-2023 → DPYCM-SA1CC-B23 ; PYC-CHENEVOTTES-2023 → DPYCM-CM1CCC-B23.", ""],
    [10, "Commandes OW1016 et OW1022 absentes du Google Sheet", "OW1016 (22/10/2025, Brice Faivre) : 4 × Meursault 1er Cru Les Charmes 2015 Colin-Morey (dépôt LMB, 130 € HT/btl) → ajoutée aux ventes LMB (98 btl, comme le Sheet LMB). OW1022 (20/11/2025, Basile Chautard) : 2 × Chenevottes 2022 → ajoutée. Les deux sont payées et expédiées dans Shopify.", ""],
    [11, "Coûts Shopify ≠ facture", "Chenevottes 2023 Colin-Morey : Shopify 62 € / facture 65 € ; Santenay 2023 Caroline Morey : 17 € / 19 € ; Clos Genet 2022 Clair : 14,40 € / 14,50 €. À corriger dans Shopify (valeur assurée).", ""],
    [12, "BLV 2 : prix de dépôt différents du BLV 1 pour un même vin", "Ex. Caillerets 2015 Caroline Morey 145 € (BLV 1) / 240 € (BLV 2) ; Chenevottes 2015 125 € / 220 € ; Corton-Charlemagne 2015 240 € / 240 €. Pour les factures LMB → OWINE : quel lot est vendu en premier (BLV 1 puis BLV 2) ?", ""],
    [13, "Écarts Shopify − théorique restants (voir onglet Synthèse)", "À faire confirmer par Alix (inventaire contradictoire).", ""],
], [4, 48, 120, 30])
for row in ws4.iter_rows(min_row=2):
    for c in row: c.alignment = Alignment(wrap_text=True, vertical="top")
wb.save(OUT)
tot = lambda i: sum((r[i] or 0) for r in rows)
print(f"{len(L)} mouvements | vins {len(rows)} | achats {tot(2):.0f} | ventes OWINE {tot(3):.0f} | théo OWINE {tot(4):.0f} | dépôt {tot(5):.0f} | ventes LMB {tot(6):.0f} | théo LMB {tot(7):.0f} | théo total {tot(8):.0f} | Shopify en stock {tot(9):.0f} / engagé {tot(10):.0f} / disponible {tot(11):.0f} | écart disponible {tot(12):+.0f}")
print("écarts (disponible − théorique) :"); [print(f"  {r[0]:16} {r[1][:58]:58} théo {r[8]:>4.0f} dispo {r[11]:>4.0f} → {r[12]:+.0f}") for r in rows if r[12] not in (0, None)]
print("sans stock Shopify avec théorique ≠ 0 :", [(r[0], r[8]) for r in rows if r[11] is None and r[8]])
print("les 8 mouvements les plus récents :", [(str(r[0]), r[1], r[2], r[3], r[6]) for r in L[:8]])
