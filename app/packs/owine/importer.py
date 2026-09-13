"""OWINE — reprise de l'historique : Sheet « Traitement Commandes OWINE » (commandes, cartons, étiquettes, coûts, emballages),
Sheet « Suivi Stock La Mémoire de Bourgogne » (achats rue de Chaux) et bons de livraison valorisés (dépôt-vente LMB → OWINE chez Alix)."""
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional

from . import config, service

PACK = set(config.PACKAGING)


def _sku(v) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _f(v) -> float:
    try:
        return float(re.sub(r"[\s\u00a0\u202f€]", "", str(v)).replace(",", ".") or 0)
    except Exception:
        return 0.0


# ------------------------------------------------------------------ BLV (PDF) : dépôts LMB → Alix
def parse_blv(pdf_path: str) -> dict:
    import fitz
    t = "\n".join(p.get_text() for p in fitz.open(pdf_path))
    no = re.search(r"No\.\s*\n?\s*(\d{10})", t)
    m = re.search(r"Date\s*\n?\s*(\d{1,2})\s+(\w+)\s+(\d{4})", t)
    months = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}
    d = date(int(m.group(3)), months.get(m.group(2).lower(), 1), int(m.group(1))) if m else None
    lines = []
    # blocs : SKU … millésime format … PU HT PU TTC Qté PT HT PT TTC
    for mm in re.finditer(r"\n([A-Z]{3}[A-Z0-9\-]{6,})\s*\n?(.*?)(\d{4}) 75CL\s*\n([\d\s,]+) €\s*\n?([\d\s,]+) €\s*\n?(\d+)\s+([\d\s,]+) €", t, re.S):
        sku, block, vint, pu_ht, pu_ttc, qty, pt_ht = mm.groups()
        parts = [x.strip() for x in block.split("\n") if x.strip()]
        lines.append({"sku": sku.strip(), "vigneron": parts[0] if parts else "", "appellation": parts[1] if len(parts) > 1 else "", "niveau": parts[2] if len(parts) > 2 else "",
                      "climat": parts[3] if len(parts) > 4 else "", "couleur": parts[-1] if parts else "", "millesime": int(vint), "pu_ht": _f(pu_ht), "qty": int(qty), "pt_ht": _f(pt_ht)})
    seen = {}
    for l in lines:
        if l["sku"] in seen and seen[l["sku"]] != l["millesime"]:
            fixed = l["sku"][:-2] + str(l["millesime"])[-2:]
            l["anomaly"] = f"SKU {l['sku']} en double avec millésime {l['millesime']} : corrigé en {fixed}"
            l["sku"] = fixed
        seen.setdefault(l["sku"], l["millesime"])
    total = re.search(r"Total général\s*\n(\d+)\s*\n([\d\s\u00a0,]+) €", t)
    return {"no": no.group(1) if no else None, "date": d, "lines": lines, "total_qty": int(total.group(1)) if total else sum(l["qty"] for l in lines),
            "total_ht": _f(total.group(2)) if total else sum(l["pt_ht"] for l in lines)}


def import_blv(pdf_path: str, log=None) -> dict:
    """Un BLV = sortie rue de Chaux (LMB) + entrée chez Alix (propriétaire LMB, dépôt-vente), prix de cession mémorisé sur l'article."""
    log = log or (lambda m: None)
    b = parse_blv(pdf_path)
    if not b["no"] or not b["lines"]:
        raise RuntimeError(f"BLV illisible : {pdf_path}")
    ref = f"BLV {b['no']}"
    service.delete_moves(ref, source="blv")
    for l in b["lines"]:
        it = service.get_item(l["sku"])
        title = it.title if it else f"{l['vigneron'].title()} {l['appellation']} {l['niveau']} {l['climat']} {l['millesime']}".replace("  ", " ").strip()
        service.upsert_item(l["sku"], title=title, vigneron=l["vigneron"].title() if not it or not it.vigneron else None, appellation=l["appellation"], niveau=l["niveau"],
                            climat=l["climat"] or None, couleur=l["couleur"], millesime=l["millesime"], lmb_price=l["pu_ht"], kind="wine")
        service.add_move(l["sku"], -l["qty"], "deposit_out", d=b["date"], location="CHAUX", owner="LMB", ref=ref, unit_cost=l["pu_ht"], source="blv", note="dépôt-vente")
        service.add_move(l["sku"], l["qty"], "deposit_in", d=b["date"], location="ALIX", owner="LMB", ref=ref, unit_cost=l["pu_ht"], source="blv", note="dépôt-vente")
    check = sum(l["qty"] for l in b["lines"]) == b["total_qty"] and abs(sum(l["pt_ht"] for l in b["lines"]) - b["total_ht"]) < 0.01 and b["total_ht"] > 0
    log(f"{ref} du {b['date']:%d/%m/%Y} : {len(b['lines'])} lignes, {b['total_qty']} bouteilles, {b['total_ht']:,.2f} € HT — cadrage {'OK' if check else 'À VÉRIFIER'}")
    for l in b["lines"]:
        if l.get("anomaly"):
            log(f"   anomalie {ref} : {l['anomaly']}")
    return {**b, "check": check}


# ------------------------------------------------------------------ Sheet LMB : achats rue de Chaux
def import_lmb_sheet(xlsx_path: str, log=None) -> dict:
    """Onglet « Suivi Stock La Memoire de Bourg » : lignes Achat = entrées rue de Chaux (propriétaire LMB), au coût d'achat.
    Les lignes Dépôt vente / Vente ne sont PAS reprises (les BLV et les commandes OWINE font foi) mais servent de contrôle."""
    from openpyxl import load_workbook
    log = log or (lambda m: None)
    wb = load_workbook(xlsx_path, data_only=True)
    ws = wb["Suivi Stock La Memoire de Bourg"]
    hdr = [c.value for c in ws[1]]
    H = {h: i for i, h in enumerate(hdr) if h}
    # correspondance désignation → SKU via les BLV déjà importés (mêmes vins) sinon SKU synthétique LMB-…
    by_key: Dict[str, str] = {}
    for it in service.items("wine"):
        if it.vigneron and it.appellation and it.millesime:
            by_key[_key(it.vigneron, it.appellation, it.niveau, it.climat, it.millesime)] = it.sku
    service.delete_moves("Sheet LMB", source="sheet_lmb")
    n, qty, unknown, ctrl = 0, 0, [], defaultdict(float)
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not any(v is not None for v in r):
            continue
        ev = str(r[H["Evènement"]] or "")
        k = _key(r[H["Vigneron"]], r[H["Appelation"]], r[H["Niveau"]], r[H["Climat"]], r[H["Millésime"]])
        sku = by_key.get(k)
        if ev == "Achat":
            if not sku:
                sku = "LMB-" + re.sub(r"[^A-Z0-9]", "", k.upper())[:28]
                unknown.append(k)
                service.upsert_item(sku, title=str(r[H["Désignation"]] or k)[:120], kind="wine", vigneron=str(r[H["Vigneron"]] or "").title(), appellation=r[H["Appelation"]],
                                    niveau=r[H["Niveau"]], climat=r[H["Climat"]] or None, couleur=r[H["Type"]], millesime=int(_f(r[H["Millésime"]])) or None, status="LMB")
            q = _f(r[H["Qté"]])
            service.add_move(sku, q, "purchase", d=date(2019, 12, 31), location="CHAUX", owner="LMB", ref="Sheet LMB", unit_cost=_f(r[H["PU HT"]]), source="sheet_lmb",
                             note="achat historique LMB (Sheet, date exacte dans l'onglet Données)")
            n += 1; qty += q
        else:
            ctrl[(ev, str(r[H["Stock"]]))] += _f(r[H["Qté"]])
    log(f"LMB : {n} ligne(s) d'achat, {qty:.0f} bouteilles entrées rue de Chaux ; {len(unknown)} vin(s) sans SKU BLV (SKU LMB-… créés) ; contrôle Sheet : {dict(ctrl)}")
    return {"purchases": n, "bottles": qty, "unknown": unknown, "control": dict(ctrl)}


def _key(vig, app, niv, cli, mil) -> str:
    norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x or "").lower())
    return "|".join([norm(vig), norm(app), norm(niv), norm(cli), str(int(_f(mil))) if mil else ""])


# ------------------------------------------------------------------ Sheet OWINE : commandes, cartons, étiquettes, emballages
def import_owine_sheet(xlsx_path: str, log=None, lmb_skus: Optional[set] = None) -> dict:
    """Onglet « Détails » : lignes de commande (SKU, qté, coût, valeur assurée, n° colis, étiquette, Mémoire = dépôt-vente LMB),
    lignes « Ajust. initial » et « Cde du … » = stock initial et réceptions d'emballages.
    Crée / complète les commandes (OwOrder), leurs cartons (OwCarton) et les mouvements de stock (source « sheet_owine »)."""
    from openpyxl import load_workbook
    log = log or (lambda m: None)
    wb = load_workbook(xlsx_path, data_only=True)
    ws = wb["Détails"]
    hdr = [c.value for c in ws[1]]
    H = {h: i for i, h in enumerate(hdr) if h}
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if any(v is not None for v in r)]
    orders: Dict[str, List[dict]] = defaultdict(list)
    n_pack = 0
    # emballages : mouvements hors commandes
    for kind_ref in {str(r[H["No. Commande"]]) for r in rows if not str(r[H["No. Commande"]]).startswith("#OW")}:
        service.delete_moves(kind_ref, source="sheet_owine")
    for r in rows:
        cmd = str(r[H["No. Commande"]] or "").strip()
        sku = _sku(r[H["SKU"]])
        if cmd.startswith("#OW"):
            orders[cmd.lstrip("#")].append(r)
            continue
        if sku in PACK:
            q = -_f(r[H["Qté"]])                             # convention du Sheet : négatif = entrée
            if q:
                d = _date_of(cmd)
                service.add_move(sku, q, "initial" if cmd.startswith("Ajust") else "packaging_in", d=d, location="ALIX", owner="OWINE", ref=cmd, source="sheet_owine")
                n_pack += 1
    # commandes
    imap = service.item_map()
    stats = {"orders": 0, "cartons": 0, "lines": 0, "lmb_lines": 0, "cost_missing": []}
    for name, rs in orders.items():
        o = service.get_order(name)
        if not o:
            from app.models import OwOrder
            o = OwOrder(name=name, status="cloturee", mode="retrait" if any(str(x[H["Etiquette"]]) == "Retrait client" for x in rs) else "chronopost", source="sheet")
            o = service.save_order(o)
        service.delete_moves(name, source="order")
        # regroupement par carton (No Colis, sinon par étiquette)
        boxes: Dict[str, dict] = {}
        pack_lines = defaultdict(float)
        for x in rs:
            sku = _sku(x[H["SKU"]])
            if sku in PACK:
                pack_lines[sku] += _f(x[H["Qté"]])
                continue
            ref = str(x[H["No Colis"]] or "").strip() or None
            lab = str(x[H["Etiquette"]] or "").strip()
            key = ref or (lab if lab and lab != "Retrait client" else "-")
            b = boxes.setdefault(key, {"ref": ref or ("" if key == "-" else key[-3:]), "tracking": lab if lab.startswith("X") else None, "lines": [], "insured": 0.0})
            qty = _f(x[H["Qté"]]); cost = _f(x[H["Coût unitaire"]]) or (imap[sku].cost if sku in imap and imap[sku].cost else 0.0)
            owner = "LMB" if str(x[H["Mémoire"]] or "").upper() == "OUI" or (lmb_skus and sku in lmb_skus and str(x[H["Mémoire"]] or "").upper() != "NON") else "OWINE"
            b["lines"].append({"sku": sku, "title": str(x[H["Designation"]] or sku), "qty": int(qty), "cost": cost, "owner": owner})
            b["insured"] += _f(x[H["Valeur assurée"]]) or qty * cost
            if not cost:
                stats["cost_missing"].append(f"{name} {sku}")
            if owner == "LMB":
                stats["lmb_lines"] += 1
            stats["lines"] += 1
            if not sku or sku == "None":
                continue
        plan = []
        for i, (k, b) in enumerate(sorted(boxes.items(), key=lambda kv: kv[1]["ref"] or kv[0])):
            nb = sum(l["qty"] for l in b["lines"])
            plan.append({"ref": b["ref"] or chr(65 + i), "box_sku": _box_from(pack_lines, nb), "lines": b["lines"], "weight_kg": round(nb * config.BOTTLE_KG, 1),
                         "insured_value": round(b["insured"]), "tracking": b["tracking"]})
        service.replace_cartons(o.id, plan)
        stats["cartons"] += len(plan)
        # mouvements de stock de la commande (vins par propriétaire + emballages du Sheet)
        d = o.created_at.date() if o.created_at else _date_guess(name)
        kind = "pickup" if o.mode == "retrait" else "sale"
        for b in plan:
            for l in b["lines"]:
                if l["sku"] and l["sku"] != "None" and l["qty"]:
                    service.add_move(l["sku"], -l["qty"], kind, d=d, location="ALIX", owner=l["owner"], ref=name, unit_cost=l["cost"], source="order")
        for sku, q in pack_lines.items():
            if q:
                service.add_move(sku, -q, "packaging_out", d=d, location="ALIX", owner="OWINE", ref=name, source="order")
        # lignes de la commande si elle n'est pas connue de Shopify
        if not o.lines or o.lines == "[]":
            agg = defaultdict(lambda: {"qty": 0, "title": "", "cost": 0.0})
            for b in plan:
                for l in b["lines"]:
                    a = agg[l["sku"]]; a["qty"] += l["qty"]; a["title"] = l["title"]; a["cost"] = l["cost"]
            import json
            o.lines = json.dumps([{"sku": k, **v, "price": None} for k, v in agg.items()], ensure_ascii=False)
            service.save_order(o)
        lmb_lines = [(l["sku"], l["qty"]) for b in plan for l in b["lines"] if l["owner"] == "LMB"]
        if lmb_lines:
            service.add_task("lmb_invoice", f"{name} : {sum(q for _, q in lmb_lines)} bouteille(s) en dépôt-vente à facturer par LMB à OWINE (régularisation)", ref=name,
                             details=", ".join(f"{q} × {s}" for s, q in lmb_lines), key=f"lmb_invoice:{name}")
        stats["orders"] += 1
    log(f"OWINE : {stats['orders']} commandes, {stats['cartons']} cartons, {stats['lines']} lignes ({stats['lmb_lines']} en dépôt-vente LMB), {n_pack} mouvements d'emballages, "
        f"{len(stats['cost_missing'])} ligne(s) sans coût")
    return stats


def _box_from(pack_lines: dict, nb: int) -> Optional[str]:
    if nb <= 0:
        return None
    if nb <= 1 and pack_lines.get("2031"):
        return "2031"
    if nb <= 3 and pack_lines.get("2033"):
        return "2033"
    return "2036" if pack_lines.get("2036") or nb > 3 else ("2033" if nb <= 3 else "2036")


def _date_of(label: str) -> date:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", label)
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else date(2025, 3, 1)


def _date_guess(name: str) -> date:
    return date(2025, 6, 1)
