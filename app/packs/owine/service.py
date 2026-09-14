"""OWINE — articles, stock (livre des mouvements), commandes, cartons, tâches, synchronisation Shopify."""
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from app.core.db import engine
from app.models import OwItem, OwMove, OwOrder, OwCarton, OwTask
from . import config

CODE = config.COMPANY


def now_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


# ================================================================== articles
def items(kind: Optional[str] = None) -> List[OwItem]:
    with Session(engine) as s:
        q = select(OwItem)
        if kind:
            q = q.where(OwItem.kind == kind)
        rows = s.exec(q.order_by(OwItem.title)).all()
        for r in rows:
            s.expunge(r)
        return rows


def item_map() -> Dict[str, OwItem]:
    return {i.sku: i for i in items()}


def get_item(sku: str) -> Optional[OwItem]:
    with Session(engine) as s:
        r = s.exec(select(OwItem).where(OwItem.sku == sku)).first()
        if r:
            s.expunge(r)
        return r


def upsert_item(sku: str, **fields) -> OwItem:
    sku = (sku or "").strip()
    with Session(engine) as s:
        it = s.exec(select(OwItem).where(OwItem.sku == sku)).first() or OwItem(sku=sku)
        for k, v in fields.items():
            if v is not None and hasattr(it, k):
                setattr(it, k, v)
        it.updated_at = datetime.utcnow()
        s.add(it)
        s.commit()
        s.refresh(it)
        s.expunge(it)
        return it


def ensure_packaging() -> None:
    for sku, p in config.PACKAGING.items():
        if not get_item(sku):
            upsert_item(sku, title=p["title"], kind="packaging", weight_kg=0, status="ACTIVE")


def vintage_of(title: str) -> Optional[int]:
    m = re.search(r"\b(19|20)\d{2}\b", title or "")
    return int(m.group(0)) if m else None


# ================================================================== stock
def add_move(sku: str, qty: float, kind: str, d: date = None, location: str = "ALIX", owner: str = "OWINE", ref: str = None,
             unit_cost: float = None, note: str = None, source: str = "manual", by: str = None) -> OwMove:
    m = OwMove(date=d or date.today(), sku=sku, qty=float(qty), location=location, owner=owner, kind=kind, ref=ref, unit_cost=unit_cost,
               note=note, source=source, by_user=by)
    with Session(engine) as s:
        s.add(m)
        s.commit()
        s.refresh(m)
        s.expunge(m)
        return m


def moves(sku: str = None, ref: str = None, location: str = None, owner: str = None, limit: int = 500) -> List[OwMove]:
    with Session(engine) as s:
        q = select(OwMove)
        if sku:
            q = q.where(OwMove.sku == sku)
        if ref:
            q = q.where(OwMove.ref == ref)
        if location:
            q = q.where(OwMove.location == location)
        if owner:
            q = q.where(OwMove.owner == owner)
        rows = s.exec(q.order_by(OwMove.date.desc(), OwMove.id.desc()).limit(limit)).all()
        for r in rows:
            s.expunge(r)
        return rows


def delete_moves(ref: str, source: str = None, kinds=None) -> int:
    with Session(engine) as s:
        q = select(OwMove).where(OwMove.ref == ref)
        if source:
            q = q.where(OwMove.source == source)
        rows = [m for m in s.exec(q).all() if not kinds or m.kind in kinds]
        for m in rows:
            s.delete(m)
        s.commit()
        return len(rows)


def stock() -> Dict[str, Dict[Tuple[str, str], float]]:
    """{sku: {(location, owner): qty}} à partir du livre des mouvements."""
    out: Dict[str, Dict[Tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
    with Session(engine) as s:
        for m in s.exec(select(OwMove)).all():
            out[m.sku][(m.location, m.owner)] += m.qty
    return out


def stock_of(sku: str) -> Dict[Tuple[str, str], float]:
    return dict(stock().get(sku, {}))


def available(sku: str, st: dict = None) -> Tuple[float, float]:
    """(OWINE chez Alix, dépôt-vente LMB chez Alix) : ce qui peut être expédié depuis Beaune."""
    st = st if st is not None else stock_of(sku)
    return st.get(("ALIX", "OWINE"), 0.0), st.get(("ALIX", "LMB"), 0.0)


# ================================================================== commandes
def orders(status: str = None, limit: int = 300) -> List[OwOrder]:
    with Session(engine) as s:
        q = select(OwOrder)
        if status:
            q = q.where(OwOrder.status == status)
        rows = s.exec(q.order_by(OwOrder.created_at.desc()).limit(limit)).all()
        for r in rows:
            s.expunge(r)
        return rows


def get_order(name: str) -> Optional[OwOrder]:
    with Session(engine) as s:
        r = s.exec(select(OwOrder).where(OwOrder.name == name)).first()
        if r:
            s.expunge(r)
        return r


def save_order(o: OwOrder) -> OwOrder:
    o.updated_at = datetime.utcnow()
    with Session(engine) as s:
        s.add(o)
        s.commit()
        s.refresh(o)
        s.expunge(o)
        return o


_TITLES = {"at": None, "map": {}}


def wine_title(sku: str, title: str = None) -> str:
    """Libellé complet d'un vin, millésime compris (règle JS : le millésime identifie la bouteille, il figure partout)."""
    now = datetime.utcnow()
    if not _TITLES["at"] or (now - _TITLES["at"]).total_seconds() > 120:
        _TITLES.update(at=now, map={i.sku: (i.title, i.millesime) for i in items()})
    t, mill = _TITLES["map"].get(sku, (None, None))
    if t and (not mill or str(mill) in t):
        return t
    base = title or t or sku
    return f"{base}, {mill}" if mill and str(mill) not in base else base


def _titled(lines: List[dict]) -> List[dict]:
    for l in lines:
        if l.get("sku"):
            l["title"] = wine_title(l["sku"], l.get("title"))
    return lines


def order_lines(o: OwOrder) -> List[dict]:
    try:
        return _titled(json.loads(o.lines or "[]"))
    except Exception:
        return []


def cartons(order_id: int) -> List[OwCarton]:
    with Session(engine) as s:
        rows = s.exec(select(OwCarton).where(OwCarton.order_id == order_id).order_by(OwCarton.sort, OwCarton.ref)).all()
        for r in rows:
            s.expunge(r)
        return rows


def carton_lines(c: OwCarton) -> List[dict]:
    try:
        return _titled(json.loads(c.lines or "[]"))
    except Exception:
        return []


def replace_cartons(order_id: int, plan: List[dict]) -> List[OwCarton]:
    """plan = [{ref, box_sku, lines:[{sku,title,qty,cost,owner}], weight_kg, insured_value, tracking}]"""
    with Session(engine) as s:
        for c in s.exec(select(OwCarton).where(OwCarton.order_id == order_id)).all():
            s.delete(c)
        out = []
        for i, p in enumerate(plan):
            c = OwCarton(order_id=order_id, ref=p["ref"], box_sku=p.get("box_sku"), lines=json.dumps(p.get("lines", []), ensure_ascii=False),
                         weight_kg=float(p.get("weight_kg") or 0), insured_value=float(p.get("insured_value") or 0), tracking=p.get("tracking"), sort=i)
            s.add(c)
            out.append(c)
        s.commit()
        for c in out:
            s.refresh(c)
            s.expunge(c)
        return out


def mode_of(shipping_title: Optional[str], address_zip: Optional[str]) -> str:
    t = (shipping_title or "").lower()
    if "alix" in t or "entrep" in t or "retrait" in t or "collect" in t:
        return "retrait"
    if t:
        return "chronopost"
    return "chronopost" if address_zip else "manuel"


# ---------------------------------------------------------------- ventilation en cartons
def box_plan(n: int) -> List[int]:
    """Capacités des cartons pour n bouteilles : cartons de 6 d'abord, un carton de 3 si le dernier ne dépasse pas 3 bouteilles
    (7 → 6 + 1 dans un carton de 3 ; 23 → 6, 6, 6, 5 ; 3 → 3 ; 4 → 6)."""
    if n <= 0:
        return []
    k = math.ceil(n / 6)
    last = n - 6 * (k - 1)
    caps = [6] * (k - 1) + [3 if last <= 3 else 6]
    if n == 1:
        caps = [1]
    return caps


def propose_cartons(lines: List[dict], stock_map: dict = None) -> List[dict]:
    """Répartit les bouteilles (une par une, les plus chères d'abord) dans les cartons pour équilibrer la valeur assurée.
    Chaque bouteille porte son propriétaire (dépôt-vente LMB en priorité si le stock OWINE ne suffit pas)."""
    bottles = []
    for l in lines:
        qty = int(l.get("qty") or 0)
        cost = float(l.get("cost") or 0)
        own_ow, own_lmb = (None, None)
        if stock_map is not None:
            own_ow, own_lmb = available(l["sku"], stock_map.get(l["sku"], {}))
        for i in range(qty):
            owner = "OWINE"
            if own_ow is not None:
                if own_ow >= 1:
                    own_ow -= 1
                elif own_lmb is not None and own_lmb >= 1:
                    own_lmb -= 1
                    owner = "LMB"
            bottles.append({"sku": l["sku"], "title": l.get("title", ""), "cost": cost, "price": float(l.get("price") or 0), "owner": owner})
    n = len(bottles)
    caps = box_plan(n)
    boxes = [{"cap": c, "bottles": [], "value": 0.0} for c in caps]
    for b in sorted(bottles, key=lambda x: -x["cost"]):
        cands = [bx for bx in boxes if len(bx["bottles"]) < bx["cap"]]
        # carton de moindre valeur, à égalité le moins rempli
        bx = min(cands, key=lambda x: (x["value"], len(x["bottles"])))
        bx["bottles"].append(b)
        bx["value"] += b["cost"]
    plan = []
    for i, bx in enumerate(boxes):
        agg: Dict[Tuple[str, str], dict] = {}
        for b in bx["bottles"]:
            k = (b["sku"], b["owner"])
            agg.setdefault(k, {"sku": b["sku"], "title": b["title"], "qty": 0, "cost": b["cost"], "price": b.get("price") or 0, "owner": b["owner"]})["qty"] += 1
        nb = len(bx["bottles"])
        plan.append({"ref": chr(65 + i), "box_sku": config.BOX_FOR.get(bx["cap"], "2036"), "lines": list(agg.values()),
                     "weight_kg": round(nb * config.BOTTLE_KG, 1), "insured_value": round(bx["value"]), "bottles": nb, "cap": bx["cap"],
                     "cost_total": round(bx["value"], 2), "sale_total": round(sum(b.get("price") or 0 for b in bx["bottles"]), 2)})
    return plan


# ================================================================== tâches
def add_task(kind: str, title: str, ref: str = None, details: str = None, due: date = None, key: str = None, company_code: str = CODE) -> Optional[OwTask]:
    with Session(engine) as s:
        if key:
            ex = s.exec(select(OwTask).where(OwTask.key == key)).first()
            if ex and ex.status == "open":
                return None
            if ex:  # tâche déjà clôturée sous cette clé → on la rouvre (ex. réception confirmée puis facture à revalider)
                ex.status, ex.done_at, ex.title, ex.details, ex.due_date = "open", None, title, details or ex.details, due or ex.due_date
                s.add(ex); s.commit(); s.refresh(ex); s.expunge(ex)
                return ex
        t = OwTask(company_code=company_code, kind=kind, title=title, ref=ref, details=details, due_date=due, key=key)
        s.add(t)
        s.commit()
        s.refresh(t)
        s.expunge(t)
        return t


def tasks(status: str = "open", company_code: str = CODE) -> List[OwTask]:
    with Session(engine) as s:
        q = select(OwTask).where(OwTask.company_code == company_code)
        if status:
            q = q.where(OwTask.status == status)
        rows = s.exec(q.order_by(OwTask.created_at.desc(), OwTask.id.desc())).all()
        for r in rows:
            s.expunge(r)
        return rows


def close_task(tid: int) -> None:
    with Session(engine) as s:
        t = s.get(OwTask, tid)
        if t:
            t.status, t.done_at = "done", datetime.utcnow()
            s.add(t)
            s.commit()


def close_tasks_by_key(prefix: str) -> int:
    with Session(engine) as s:
        rows = [t for t in s.exec(select(OwTask).where(OwTask.status == "open")).all() if (t.key or "").startswith(prefix)]
        for t in rows:
            t.status, t.done_at = "done", datetime.utcnow()
            s.add(t)
        s.commit()
        return len(rows)


# ================================================================== Shopify
def _shopify():
    from app.core.connectors.shopify import for_company
    return for_company(CODE)


def sync_items(log=None) -> dict:
    """Variantes Shopify → articles (titre, prix, coût, millésime, emballages « UNLISTED »)."""
    log = log or (lambda m: None)
    c = _shopify()
    if not c:
        raise RuntimeError("Shopify non configuré pour OWINE")
    q = """query($first:Int!,$after:String){ productVariants(first:$first, after:$after) { pageInfo{hasNextPage endCursor} edges{ node{
      id sku title price product{ id title status vendor productType } inventoryItem{ unitCost{amount} } } } } }"""
    n, missing = 0, []
    for v in c.pages(q, "productVariants", first=100):
        sku = (v.get("sku") or "").strip()
        if not sku:
            continue
        p = v["product"]
        cost = float(((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount") or 0) or None
        kind = "packaging" if sku in config.PACKAGING else ("selection" if sku.upper().startswith("SEL-") else "wine")
        fields = dict(title=p["title"], price=float(v.get("price") or 0), shopify_variant_id=v["id"], shopify_product_id=p["id"], status=p["status"],
                      vigneron=p.get("vendor") or None, millesime=vintage_of(p["title"]), kind=kind)
        if cost:
            fields.update(cost=cost, cost_source="shopify")
        elif kind == "wine":
            missing.append(sku)
        upsert_item(sku, **fields)
        n += 1
    ensure_packaging()
    log(f"{n} variante(s) synchronisée(s), {len(missing)} vin(s) sans coût dans Shopify")
    return {"items": n, "missing_cost": missing}


def shopify_inventory() -> Dict[str, dict]:
    """{sku: {on_hand, available, committed}} à l'emplacement Alix (pour le rapprochement)."""
    c = _shopify()
    q = """query($first:Int!,$after:String){ productVariants(first:$first, after:$after) { pageInfo{hasNextPage endCursor} edges{ node{
      sku inventoryItem{ inventoryLevels(first:3){ edges{ node{ location{name} quantities(names:["available","committed","on_hand"]){ name quantity } } } } } } } } }"""
    out = {}
    for v in c.pages(q, "productVariants", first=100):
        sku = (v.get("sku") or "").strip()
        lv = [e["node"] for e in ((v.get("inventoryItem") or {}).get("inventoryLevels") or {}).get("edges", [])]
        qty = {x["name"]: x["quantity"] for l in lv for x in l["quantities"]}
        if sku:
            out[sku] = qty
    return out


ORDER_Q = """query($first:Int!,$after:String){ orders(first:$first, after:$after, sortKey:CREATED_AT, reverse:true) { pageInfo{hasNextPage endCursor} edges{ node{
  id name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus note tags
  shippingLine{ title } totalPriceSet{shopMoney{amount}}
  customer{ displayName email phone } shippingAddress{ name company address1 address2 zip city countryCodeV2 phone }
  lineItems(first:50){ edges{ node{ title quantity sku variant{ price inventoryItem{ unitCost{amount} } } } } }
  fulfillments{ status trackingInfo{ number company } }
} } } }"""


def sync_orders(log=None, max_orders: int = 500) -> dict:
    """Commandes Shopify → OwOrder (création ; mise à jour des statuts Shopify, adresse et lignes tant que la commande est « à traiter »)."""
    log = log or (lambda m: None)
    c = _shopify()
    if not c:
        raise RuntimeError("Shopify non configuré pour OWINE")
    imap = item_map()
    created, updated = 0, 0
    for o in c.pages(ORDER_Q, "orders", first=50)[:max_orders]:
        sa = o.get("shippingAddress") or {}
        cu = o.get("customer") or {}
        lines = []
        for e in o["lineItems"]["edges"]:
            li = e["node"]; v = li.get("variant") or {}
            sku = (li.get("sku") or "").strip()
            cost = float(((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount") or 0) or (imap[sku].cost if sku in imap and imap[sku].cost else None)
            lines.append({"sku": sku, "title": li["title"], "qty": int(li["quantity"]), "price": float(v.get("price") or 0), "cost": cost})
        existing = get_order(o["name"].lstrip("#"))
        fields = dict(shopify_id=o["id"], created_at=datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).replace(tzinfo=None),
                      customer=sa.get("name") or cu.get("displayName"), email=cu.get("email"), phone=sa.get("phone") or cu.get("phone"), company=sa.get("company"),
                      address1=sa.get("address1"), address2=sa.get("address2"), zip=sa.get("zip"), city=sa.get("city"), country=sa.get("countryCodeV2") or "FR",
                      shipping_title=(o.get("shippingLine") or {}).get("title"), financial_status=o.get("displayFinancialStatus"), fulfillment_status=o.get("displayFulfillmentStatus"),
                      total=float(o["totalPriceSet"]["shopMoney"]["amount"]), lines=json.dumps(lines, ensure_ascii=False), note=o.get("note"))
        if existing:
            if existing.status == "a_traiter":
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.mode = existing.mode if existing.mode != "manuel" else mode_of(fields["shipping_title"], fields["zip"])
            else:
                existing.financial_status, existing.fulfillment_status = fields["financial_status"], fields["fulfillment_status"]
            if o.get("cancelledAt") and existing.status in ("a_traiter", "cartons"):
                existing.status = "annulee"
            save_order(existing)
            updated += 1
        else:
            st = "a_traiter"
            if o.get("cancelledAt"):
                st = "annulee"
            elif o.get("displayFulfillmentStatus") == "FULFILLED":
                st = "cloturee"                          # historique déjà traité hors Vaelan
            new = OwOrder(name=o["name"].lstrip("#"), status=st, mode=mode_of(fields["shipping_title"], fields["zip"]), **fields)
            save_order(new)
            created += 1
    log(f"commandes Shopify : {created} créée(s), {updated} mise(s) à jour")
    return {"created": created, "updated": updated}


# ================================================================== cycle de vie d'une commande
def validate_cartons(o: OwOrder, plan: List[dict], by: str = None) -> None:
    """Enregistre les cartons et passe la commande en « cartons validés » (les mouvements de stock sont posés à l'envoi)."""
    replace_cartons(o.id, plan)
    o.status = "cartons"
    save_order(o)


def apply_labels(o: OwOrder, mapping: Dict[str, str]) -> int:
    """mapping {ref carton: n° Chronopost}. Passe en « étiquettes reçues » si tous les cartons ont un numéro."""
    cs = cartons(o.id)
    n = 0
    with Session(engine) as s:
        for c in cs:
            if c.ref in mapping and mapping[c.ref]:
                cc = s.get(OwCarton, c.id)
                cc.tracking = mapping[c.ref].strip().upper()
                s.add(cc)
                n += 1
        s.commit()
    if all((mapping.get(c.ref) or c.tracking) for c in cs):
        o.status = "etiquettes"
        save_order(o)
    return n


def post_stock_moves(o: OwOrder, by: str = None) -> int:
    """Sorties de stock (vins, par propriétaire) et consommation d'emballages ; idempotent par commande."""
    delete_moves(o.name, source="order")
    n = 0
    kind = "pickup" if o.mode == "retrait" else "sale"
    d = (o.pickup_date or (o.created_at.date() if o.created_at else date.today()))
    boxes = defaultdict(int)
    for c in cartons(o.id):
        for l in carton_lines(c):
            add_move(l["sku"], -int(l["qty"]), kind, d=d, location="ALIX", owner=l.get("owner", "OWINE"), ref=o.name, unit_cost=l.get("cost"), source="order", by=by)
            n += 1
        if o.mode != "retrait" and c.box_sku:
            boxes[c.box_sku] += 1
            boxes[config.LABEL_SHEET] += 1
    for sku, q in boxes.items():
        add_move(sku, -q, "packaging_out", d=d, location="ALIX", owner="OWINE", ref=o.name, source="order", by=by)
        n += 1
    # dépôt-vente : tâche de facturation LMB → OWINE
    lmb = [(l["sku"], l["qty"]) for c in cartons(o.id) for l in carton_lines(c) if l.get("owner") == "LMB"]
    if lmb:
        add_task("lmb_invoice", f"{o.name} : {sum(q for _, q in lmb)} bouteille(s) en dépôt-vente à facturer par LMB à OWINE", ref=o.name,
                 details=", ".join(f"{q} × {s}" for s, q in lmb), key=f"lmb_invoice:{o.name}")
    return n


def mark_sent(o: OwOrder, by: str = None) -> None:
    """Après envoi des deux e-mails (Alix et client) : mouvements de stock, statut « envoyé », puis « attente de réception » avec tâche de suivi."""
    post_stock_moves(o, by=by)
    o.sent_alix_at = o.sent_alix_at or datetime.utcnow()
    o.sent_client_at = o.sent_client_at or datetime.utcnow()
    if o.mode == "retrait":
        o.status = "attente_reception"
        add_task("reception", f"{o.name} : confirmer la collecte par le client chez Alix", ref=o.name, due=(o.pickup_date or date.today()) + timedelta(days=1), key=f"reception:{o.name}")
    else:
        o.status = "attente_reception"
        due = (o.delivery_date or (o.pickup_date + timedelta(days=1) if o.pickup_date else date.today() + timedelta(days=3))) + timedelta(days=1)
        add_task("reception", f"{o.name} : vérifier avec le client que tout est bien arrivé", ref=o.name, due=due, key=f"reception:{o.name}")
    save_order(o)


def confirm_reception(o: OwOrder, by: str = None) -> None:
    """Réception confirmée par le client → il reste à valider la facture (brouillon du connecteur Shopify) dans Pennylane."""
    o.status = "a_facturer"
    save_order(o)
    close_tasks_by_key(f"reception:{o.name}")
    inv = pennylane_invoice_for(o.name)
    if inv and not inv.get("draft"):
        close_order(o, by=by)
    else:
        add_task("pennylane_invoice", f"{o.name} : vérifier et valider la facture dans Pennylane (brouillon du connecteur Shopify)", ref=o.name, key=f"pennylane_invoice:{o.name}",
                 details="Contrôler adresses de facturation / livraison et lignes, puis finaliser la facture. Vaelan clôture la commande dès que la facture n'est plus en brouillon.")


def close_order(o: OwOrder, by: str = None) -> None:
    o.status, o.closed_at = "cloturee", datetime.utcnow()
    save_order(o)
    close_tasks_by_key(f"reception:{o.name}")
    close_tasks_by_key(f"pennylane_invoice:{o.name}")


_INV_CACHE = {"at": None, "items": []}


def pennylane_invoices(force: bool = False) -> List[dict]:
    """Factures clients Pennylane OWINE (200 dernières), mises en cache 10 minutes."""
    from app.core.connectors.pennylane import for_company
    if not force and _INV_CACHE["at"] and (datetime.utcnow() - _INV_CACHE["at"]).total_seconds() < 600:
        return _INV_CACHE["items"]
    c = for_company(CODE)
    if not c:
        return []
    items, cur = [], None
    for _ in range(2):
        d = c.get("/customer_invoices", limit=100, **({"cursor": cur} if cur else {}))
        items += d.get("items") or []
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    _INV_CACHE.update(at=datetime.utcnow(), items=items)
    return items


def pennylane_invoice_for(name: str, force: bool = False) -> Optional[dict]:
    """La facture Pennylane d'une commande : objet PDF « Commande #OW1049 … » (connecteur Shopify) ou n° de facture = n° de commande (anciennes)."""
    import re
    pat = re.compile(rf"#{re.escape(name)}(?!\d)")
    for inv in pennylane_invoices(force):
        if pat.search(inv.get("pdf_invoice_subject") or "") or pat.search(inv.get("label") or "") or inv.get("invoice_number") == name:
            return {"id": inv.get("id"), "number": inv.get("invoice_number") or "", "status": inv.get("status"), "draft": bool(inv.get("draft")), "paid": bool(inv.get("paid")),
                    "amount": float(inv.get("amount") or 0), "date": inv.get("date"), "pdf": inv.get("public_file_url"), "label": inv.get("label")}
    return None


def close_invoiced_orders(log=None) -> int:
    """Commandes « à facturer » dont la facture Pennylane n'est plus en brouillon → clôturées."""
    log = log or (lambda m: None)
    n = 0
    for o in orders("a_facturer"):
        inv = pennylane_invoice_for(o.name, force=(n == 0))
        if inv and not inv["draft"]:
            close_order(o); n += 1
            log(f"{o.name} : facture {inv['number'] or inv['id']} validée → commande clôturée")
    return n


def admin_url() -> str:
    import os
    return (os.getenv("VAELAN_BASE_URL") or "https://vaelan.com").rstrip("/")


def pennylane_draft_tasks(log=None) -> int:
    """Factures clients en brouillon dans Pennylane OWINE (créées par le connecteur Shopify) → une tâche chacune."""
    from app.core.connectors.pennylane import for_company
    log = log or (lambda m: None)
    c = for_company(CODE)
    if not c:
        return 0
    d = c.get("/customer_invoices", limit=100)
    n = 0
    for inv in d.get("items") or []:
        if inv.get("draft") or inv.get("status") == "draft":
            key = f"pennylane_invoice:{inv.get('id')}"
            lab = inv.get("label") or inv.get("invoice_number") or str(inv.get("id"))
            if add_task("pennylane_invoice", f"Facture Pennylane en brouillon à vérifier et valider : {lab} ({inv.get('amount')} €, {inv.get('date')})", ref=lab, key=key,
                        details=f"Créée par le connecteur Shopify. Vérifier adresses de facturation / livraison puis finaliser dans Pennylane."):
                n += 1
    # les brouillons validés entre-temps ferment leur tâche
    open_keys = {t.key for t in tasks("open") if t.kind == "pennylane_invoice"}
    still = {f"pennylane_invoice:{inv.get('id')}" for inv in d.get("items") or [] if inv.get("draft") or inv.get("status") == "draft"}
    for k in open_keys - still:
        close_tasks_by_key(k)
    log(f"Pennylane : {n} nouvelle(s) tâche(s) de facture brouillon")
    return n


def cost_alerts() -> List[str]:
    return [i.sku for i in items("wine") if not i.cost and i.status == "ACTIVE"]
