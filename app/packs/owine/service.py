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


def propose_cartons(lines: List[dict], stock_map: dict = None, intl: bool = False) -> List[dict]:
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
    caps = [6] * math.ceil(n / 6) if intl and n else box_plan(n)      # international : uniquement des cartons de 6 (réf. 2036)
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
      id sku title price product{ id title status vendor productType } inventoryItem{ unitCost{amount} harmonizedSystemCode countryCodeOfOrigin } } } } }"""
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
        ii = v.get("inventoryItem") or {}
        cur = get_item(sku)
        if ii.get("harmonizedSystemCode") and not (cur and cur.hs_code):
            fields["hs_code"] = re.sub(r"[^0-9]", "", ii["harmonizedSystemCode"]) or None
        if ii.get("countryCodeOfOrigin") and not (cur and cur.origin and cur.origin != "FR"):
            fields["origin"] = ii["countryCodeOfOrigin"]
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


ORDER_NODE = """id name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus note tags customerLocale taxesIncluded
  shippingLine{ title } totalPriceSet{shopMoney{amount}} totalTaxSet{shopMoney{amount}} totalShippingPriceSet{shopMoney{amount}} taxLines{ rate }
  customAttributes{ key value } billingAddress{ company name countryCodeV2 }
  customer{ displayName email phone metafields(first:10, namespace:"custom"){ edges{ node{ key value } } } } shippingAddress{ name company address1 address2 zip city countryCodeV2 phone }
  lineItems(first:50){ edges{ node{ title quantity sku originalUnitPriceSet{shopMoney{amount}} discountedUnitPriceAfterAllDiscountsSet{shopMoney{amount}} variant{ price inventoryItem{ unitCost{amount} } } } } }
  fulfillments{ status trackingInfo{ number company } }"""
ORDER_Q = "query($first:Int!,$after:String){ orders(first:$first, after:$after, sortKey:CREATED_AT, reverse:true) { pageInfo{hasNextPage endCursor} edges{ node{ " + ORDER_NODE + " } } } }"
ONE_ORDER_Q = "query($id:ID!){ order(id:$id) { " + ORDER_NODE + " } }"
ORDER_BY_NAME_Q = "query($q:String!){ orders(first:5, query:$q) { edges{ node{ " + ORDER_NODE + " } } } }"

# champs « en-tête » (client, adresse, livraison) rafraîchis par le bouton « Synchroniser avec Shopify » quel que soit l'avancement dans Vaelan
ORDER_HEAD = ("customer", "email", "phone", "company", "address1", "address2", "zip", "city", "country", "shipping_title", "total")
FIELD_LABELS = {"customer": "nom", "email": "e-mail", "phone": "téléphone", "company": "société", "address1": "adresse", "address2": "complément d'adresse", "zip": "code postal",
                "city": "ville", "country": "pays", "shipping_title": "mode de livraison Shopify", "total": "total", "note": "note", "financial_status": "statut de paiement",
                "fulfillment_status": "statut de traitement Shopify", "lines": "lignes", "status": "statut Vaelan", "mode": "mode d'expédition"}


def _merge_line_prices(old_json: str, new_lines: List[dict]) -> str:
    """Reporte prix catalogue du jour (« price ») et prix net encaissé (« net ») de Shopify sur les lignes existantes, sans toucher au reste."""
    try:
        old = json.loads(old_json or "[]")
    except Exception:
        return old_json
    by: Dict[str, List[dict]] = defaultdict(list)
    for l in new_lines:
        by[l.get("sku") or ""].append(l)
    for l in old:
        c = by.get(l.get("sku") or "")
        if c:
            n = c.pop(0); l["price"] = n["price"]; l["net"] = n.get("net")
    return json.dumps(old, ensure_ascii=False)


def _order_payload(o: dict, imap: Dict[str, OwItem]) -> Tuple[dict, List[dict]]:
    """Nœud commande Shopify (ORDER_NODE) → (champs OwOrder, lignes)."""
    sa = o.get("shippingAddress") or {}
    cu = o.get("customer") or {}
    lines = []
    for e in o["lineItems"]["edges"]:
        li = e["node"]; v = li.get("variant") or {}
        sku = (li.get("sku") or "").strip()
        cost = float(((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount") or 0) or (imap[sku].cost if sku in imap and imap[sku].cost else None)
        orig = float(((li.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get("amount") or 0) or float(v.get("price") or 0)    # prix catalogue au jour de la commande
        netp = float(((li.get("discountedUnitPriceAfterAllDiscountsSet") or {}).get("shopMoney") or {}).get("amount") or 0)                # prix réellement encaissé (toutes remises)
        lines.append({"sku": sku, "title": li["title"], "qty": int(li["quantity"]), "price": orig, "net": netp, "cost": cost})
    ba = o.get("billingAddress") or {}
    attrs = [{"key": a.get("key"), "value": a.get("value")} for a in (o.get("customAttributes") or []) if a.get("key")]
    mf = {e["node"]["key"]: e["node"]["value"] for e in ((cu.get("metafields") or {}).get("edges") or [])}

    def attr(*names):
        for a in attrs:
            k = (a["key"] or "").lower()
            if any(n in k for n in names) and (a.get("value") or "").strip():
                return a["value"].strip()
        return None
    money = lambda k: float((((o.get(k) or {}).get("shopMoney") or {}).get("amount")) or 0)
    rates = [float(t.get("rate") or 0) for t in (o.get("taxLines") or [])]
    vat = attr("tva", "vat") or (mf.get("vat_number") or mf.get("tva") or "").strip() or None
    eori = attr("eori") or (mf.get("eori") or "").strip() or None
    is_company = bool(sa.get("company") or ba.get("company") or vat or eori or (attr("société", "societe", "company", "entreprise", "professionnel") or "").lower() in ("oui", "yes", "1", "true", "société", "societe"))
    fields = dict(shopify_id=o["id"], created_at=datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).replace(tzinfo=None),
                  customer=sa.get("name") or cu.get("displayName"), email=cu.get("email"), phone=sa.get("phone") or cu.get("phone"), company=sa.get("company"),
                  address1=sa.get("address1"), address2=sa.get("address2"), zip=sa.get("zip"), city=sa.get("city"), country=sa.get("countryCodeV2") or "FR",
                  shipping_title=(o.get("shippingLine") or {}).get("title"), financial_status=o.get("displayFinancialStatus"), fulfillment_status=o.get("displayFulfillmentStatus"),
                  total=float(o["totalPriceSet"]["shopMoney"]["amount"]), lines=json.dumps(lines, ensure_ascii=False), note=o.get("note"),
                  # international : langue du client, TVA et port prélevés par Shopify, attributs de commande (n° TVA / EORI saisis au panier), société de facturation
                  locale=o.get("customerLocale"), tax_total=money("totalTaxSet"), tax_rate=max(rates) if rates else 0.0, shipping_paid=money("totalShippingPriceSet"),
                  attributes=json.dumps(attrs, ensure_ascii=False) if attrs else None, billing_company=ba.get("company") or None,
                  vat_number=vat, eori=eori, customer_type="societe" if is_company else None)
    return fields, lines


IDENTITY_FIELDS = ("vat_number", "eori", "customer_type", "tax_id")     # jamais écrasés par Shopify une fois saisis dans Vaelan (formulaire douane, saisie manuelle)
LIVE_FIELDS = ("locale", "tax_total", "tax_rate", "shipping_paid", "attributes", "billing_company")   # toujours rafraîchis


def _apply_shopify_order(existing: OwOrder, o: dict, fields: dict, lines: List[dict], refresh_head: bool = False) -> None:
    """Reporte un nœud Shopify sur une commande existante.
    « À traiter » : tout est repris de Shopify. Ensuite : statuts Shopify et prix des lignes seulement (les cartons sont posés sur les lignes de Vaelan),
    sauf `refresh_head` (bouton de la commande) qui reprend aussi client, adresse, livraison et total ; la note n'est remplacée que si Shopify en porte une."""
    if existing.status == "a_traiter":
        for k, v in fields.items():
            if k in IDENTITY_FIELDS and getattr(existing, k, None) and not v:
                continue                                   # n° TVA / EORI / type déjà saisis dans Vaelan : Shopify ne les efface pas
            setattr(existing, k, v)
        existing.mode = existing.mode if existing.mode != "manuel" else mode_of(fields["shipping_title"], fields["zip"])
    else:
        existing.financial_status, existing.fulfillment_status = fields["financial_status"], fields["fulfillment_status"]
        existing.lines = _merge_line_prices(existing.lines, lines)   # prix historiques (catalogue du jour, net après remises) rafraîchis sur les commandes déjà traitées
        for k in LIVE_FIELDS:
            if fields.get(k) is not None:
                setattr(existing, k, fields[k])
        for k in IDENTITY_FIELDS:
            if fields.get(k) and not getattr(existing, k, None):
                setattr(existing, k, fields[k])
        if refresh_head:
            for k in ORDER_HEAD:
                setattr(existing, k, fields[k])
            if fields.get("note"):
                existing.note = fields["note"]
            existing.mode = existing.mode if existing.mode != "manuel" else mode_of(fields["shipping_title"], fields["zip"])
    if o.get("cancelledAt") and existing.status in ("a_traiter", "cartons"):
        existing.status = "annulee"


def sync_orders(log=None, max_orders: int = 500) -> dict:
    """Commandes Shopify → OwOrder (création ; mise à jour des statuts Shopify, adresse et lignes tant que la commande est « à traiter »)."""
    log = log or (lambda m: None)
    c = _shopify()
    if not c:
        raise RuntimeError("Shopify non configuré pour OWINE")
    imap = item_map()
    created, updated = 0, 0
    for o in c.pages(ORDER_Q, "orders", first=50)[:max_orders]:
        fields, lines = _order_payload(o, imap)
        existing = get_order(o["name"].lstrip("#"))
        if existing:
            _apply_shopify_order(existing, o, fields, lines)
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


def fetch_shopify_order(o: OwOrder) -> Optional[dict]:
    """Relit cette seule commande dans Shopify : par identifiant global, sinon par nom (commande importée avant que l'identifiant ne soit stocké)."""
    c = _shopify()
    if not c:
        raise RuntimeError("Shopify non configuré pour OWINE")
    node = None
    if o.shopify_id:
        node = (c.gql(ONE_ORDER_Q, {"id": o.shopify_id}) or {}).get("order")
    if not node:
        edges = ((c.gql(ORDER_BY_NAME_Q, {"q": f"name:{o.name}"}) or {}).get("orders") or {}).get("edges") or []
        node = next((e["node"] for e in edges if (e["node"].get("name") or "").lstrip("#") == o.name), None)
    return node


def sync_order(o: OwOrder) -> str:
    """Bouton « Synchroniser avec Shopify » d'une commande : relit cette seule commande et rafraîchit client, adresse, livraison, statuts et note
    sans attendre la synchronisation globale (client choisi après coup dans Shopify, adresse corrigée…). Renvoie ce qui a changé, en clair."""
    node = fetch_shopify_order(o)
    if not node:
        raise RuntimeError("commande introuvable dans Shopify")
    fields, lines = _order_payload(node, item_map())
    watched = [k for k in fields if k not in ("shopify_id", "created_at")] + ["status", "mode"]
    snap = lambda k: (json.loads(getattr(o, k) or "[]") if k == "lines" else getattr(o, k))
    before = {k: snap(k) for k in watched}
    _apply_shopify_order(o, node, fields, lines, refresh_head=True)
    save_order(o)
    changed = [FIELD_LABELS.get(k, k) for k in watched if before[k] != snap(k)]
    if not changed:
        return "synchronisée avec Shopify, aucune différence."
    return "synchronisée avec Shopify — mis à jour : " + ", ".join(changed) + "."


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
    if any(m.kind in ("sale", "pickup") for m in moves(ref=o.name, limit=500)):
        return  # ventes déjà enregistrées (reprise de l'historique) : pas de double décompte
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
    from . import export
    if export.zone(o.country) == "EXPORT":
        base = o.pickup_date or date.today()
        add_task("customs", f"{o.name} : suivre le dédouanement ({export.X.country_name(o.country)}) puis cocher « dédouané et livré » sur la commande", ref=o.name, due=base + timedelta(days=2), key=f"cleared:{o.name}",
                 details="Chronotrace : douane export puis import ; en DAP le destinataire règle droits et taxes au transporteur avant la remise.")
        add_task("export_proof", f"{o.name} : archiver le justificatif d'exportation (déclaration Chronopost / MRN) — preuve de l'exonération de TVA", ref=o.name, due=base + timedelta(days=15), key=f"proof:{o.name}")
        close_tasks_by_key(f"customs:{o.name}"); close_tasks_by_key(f"customs_data:{o.name}")


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
    for pfx in ("customs", "customs_data", "customs_info", "cleared", "proof"):
        close_tasks_by_key(f"{pfx}:{o.name}")


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


# ---------------------------------------------------------------- réglages JSON du module (Setting)

def _setting_get(key: str):
    from app.models import Setting
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == key)).first()
        try:
            return json.loads(st.value) if st and st.value else None
        except Exception:
            return None


def _setting_set(key: str, value) -> None:
    from app.models import Setting
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == key)).first() or Setting(company_code=CODE, key=key, value="")
        st.value = json.dumps(value, ensure_ascii=False, default=str)
        s.add(st); s.commit()


# ---------------------------------------------------------------- dépôt-vente LMB : facturation LMB → OWINE dans Pennylane

def lmb_lots() -> Dict[str, List[dict]]:
    """Lots déposés par vin, dans l'ordre des BLV : [{lot, qty, price}]."""
    lots: Dict[str, List[dict]] = defaultdict(list)
    for m in sorted([m for m in moves(owner="LMB", limit=100000) if m.kind == "deposit_in" and m.location == "ALIX"], key=lambda m: (m.date, m.id)):
        lots[m.sku].append({"lot": _blv_no(m.ref), "qty": m.qty, "left": m.qty, "price": float(m.unit_cost or 0)})
    return lots


def lmb_allocation() -> Dict[Tuple[str, str], List[dict]]:
    """(commande, sku) → [{qty, price, lot}] : chaque vente en dépôt-vente est imputée au lot le plus ancien encore disponible
    (premier déposé, premier vendu) et facturée au prix de cession de CE lot — règle validée le 14/09/2026 (le prix du BLV 2 appliqué
    aux ventes antérieures mettrait la plupart des commandes à perte)."""
    lots = lmb_lots(); imap = item_map()
    out: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for m in sorted([m for m in moves(owner="LMB", limit=100000) if m.kind in ("sale", "pickup") and m.location == "ALIX"], key=lambda m: (m.date, m.id)):
        q = -m.qty
        for lot in lots.get(m.sku, []):
            if q <= 0:
                break
            take = min(q, lot["left"])
            if take > 0:
                out[(m.ref, m.sku)].append({"qty": take, "price": lot["price"], "lot": lot["lot"]}); lot["left"] -= take; q -= take
        if q > 0:
            it = imap.get(m.sku); out[(m.ref, m.sku)].append({"qty": q, "price": float(it.lmb_price or 0) if it and it.lmb_price else 0.0, "lot": "?"})
    return out


def lmb_prices() -> Dict[str, float]:
    """Prix de cession courant par vin = prix du lot le plus ancien encore disponible (sinon dernier lot, sinon fiche)."""
    p: Dict[str, float] = {}
    for sku, lots in lmb_lots().items():
        sold = sum(-m.qty for m in moves(owner="LMB", limit=100000) if m.sku == sku and m.kind in ("sale", "pickup") and m.location == "ALIX")
        cur = None
        for lot in lots:
            if sold < lot["qty"]:
                cur = lot["price"]; break
            sold -= lot["qty"]
        p[sku] = cur if cur is not None else lots[-1]["price"]
    for it in items("wine"):
        if it.lmb_price and it.sku not in p:
            p[it.sku] = float(it.lmb_price)
    return p


def lmb_lines(name: str) -> List[dict]:
    """Bouteilles en dépôt-vente vendues dans une commande, au prix du lot imputé : [{sku, qty, title, price, lot, list, net_ht, margin}]."""
    alloc = lmb_allocation(); o = get_order(name)
    sold: Dict[str, dict] = {}
    for l in (order_lines(o) if o else []):
        if l.get("sku"):
            sold.setdefault(l["sku"], {"list": float(l.get("price") or 0), "net": float(l["net"]) if l.get("net") is not None else float(l.get("price") or 0)})
    out = []
    for (ref, sku), parts in alloc.items():
        if ref != name:
            continue
        for p in parts:
            same = next((x for x in out if x["sku"] == sku and x["price"] == p["price"] and x["lot"] == p["lot"]), None)
            if same:
                same["qty"] += int(p["qty"]); continue
            s = sold.get(sku, {"list": 0.0, "net": 0.0}); net_ht = s["net"] / 1.2
            out.append({"sku": sku, "qty": int(p["qty"]), "title": wine_title(sku), "price": p["price"], "lot": p["lot"], "list": s["list"], "net_ht": net_ht, "margin": net_ht - p["price"]})
    return out


def lmb_draft_info(name: str) -> Optional[dict]:
    return _setting_get(f"owine:lmb_invoice:{name}")


def _lmb_client():
    from app.core.connectors.pennylane import for_company
    c = for_company(config.LMB_COMPANY)
    if not c:
        raise RuntimeError("Pennylane La Mémoire de Bourgogne non configuré (jeton LAMEMOIREDEBOURGOGNE)")
    return c


def _lmb_customer_id(c) -> int:
    cid = _setting_get("owine:lmb_customer_id")
    if cid:
        return int(cid)
    for cu in (c.get("/customers", limit=100).get("items") or []):
        if (cu.get("name") or "").strip().upper() == config.LMB_CUSTOMER["name"].upper():
            _setting_set("owine:lmb_customer_id", cu["id"]); return int(cu["id"])
    st, r = c.send_json("POST", "/company_customers", config.LMB_CUSTOMER)
    if st not in (200, 201):
        raise RuntimeError(f"création du client OWINE dans Pennylane LMB refusée : {st} {str(r)[:200]}")
    cid = (r.get("customer") or r)["id"]; _setting_set("owine:lmb_customer_id", cid)
    return int(cid)


def create_lmb_draft(name: str, by: str = None) -> dict:
    """Crée (ou recrée) dans Pennylane LMB le brouillon de facture LMB → OWINE d'une commande. L'ancien brouillon est retiré s'il l'est encore."""
    o = get_order(name)
    if not o:
        raise RuntimeError(f"commande {name} inconnue")
    lines = lmb_lines(name)
    if not lines:
        raise RuntimeError(f"{name} : aucune bouteille en dépôt-vente")
    if any(l["price"] is None for l in lines):
        raise RuntimeError(f"{name} : prix de dépôt manquant pour " + ", ".join(l["sku"] for l in lines if l["price"] is None))
    c = _lmb_client(); cid = _lmb_customer_id(c)
    prev = lmb_draft_info(name)
    if prev and prev.get("id"):
        try:
            cur = c.get(f"/customer_invoices/{prev['id']}")
        except Exception as e:
            cur = None if "404" in str(e) else {"draft": True}
        if cur and not cur.get("draft"):
            raise RuntimeError(f"{name} : la facture {cur.get('invoice_number') or prev['id']} n'est plus un brouillon, elle ne peut pas être recréée")
        if cur:
            st, r = c.send_json("DELETE", f"/customer_invoices/{prev['id']}", {})
            if st not in (200, 202, 204, 404):
                raise RuntimeError(f"retrait de l'ancien brouillon refusé ({st}) : supprimez-le dans Pennylane puis recommencez")
    d = (o.created_at.date() if o.created_at else date.today())
    body = {"customer_id": cid, "date": date.today().isoformat(), "deadline": (date.today() + timedelta(days=30)).isoformat(), "draft": True, "external_reference": f"LMB-{name}",
            "pdf_invoice_subject": f"Dépôt-vente OWINE — commande {name} du {d:%d/%m/%Y}",
            "invoice_lines": [{"label": f"{l['title']} — réf. {l['sku']} — vendue par OWINE le {d:%d/%m/%Y} (commande {name}), dépôt-vente BLV n° {l.get('lot') or '?'}", "quantity": l["qty"],
                               "unit": "piece", "raw_currency_unit_price": f"{l['price']:.2f}", "vat_rate": "FR_200"} for l in lines]}
    st, r = c.send_json("POST", "/customer_invoices", body)
    if st not in (200, 201):
        raise RuntimeError(f"Pennylane a refusé le brouillon ({st}) : {str(r)[:200]}")
    info = {"id": r.get("id"), "created_at": datetime.utcnow().isoformat(timespec="seconds"), "by": by, "amount": r.get("amount"), "ht": r.get("currency_amount_before_tax"), "pdf": r.get("public_file_url"),
            "lines": [(l["sku"], l["qty"], l["price"]) for l in lines], "status": "draft", "number": ""}
    _setting_set(f"owine:lmb_invoice:{name}", info)
    return info


def lmb_drafts_sync(log=None) -> int:
    """Brouillons LMB → OWINE finalisés dans Pennylane → tâche « facture LMB » fermée, n° mémorisé."""
    log = log or (lambda m: None); n = 0
    try:
        c = _lmb_client()
    except Exception as e:
        log(f"LMB : {e}"); return 0
    for t in tasks("open"):
        if t.kind != "lmb_invoice" or not t.ref:
            continue
        info = lmb_draft_info(t.ref)
        if not info or not info.get("id"):
            continue
        try:
            cur = c.get(f"/customer_invoices/{info['id']}")
        except Exception:
            continue
        if not cur.get("draft"):
            info.update(status=cur.get("status"), number=cur.get("invoice_number") or "", pdf=cur.get("public_file_url") or info.get("pdf"))
            _setting_set(f"owine:lmb_invoice:{t.ref}", info); close_tasks_by_key(f"lmb_invoice:{t.ref}"); n += 1
            log(f"{t.ref} : facture LMB {info['number']} finalisée → tâche fermée")
    return n


# ---------------------------------------------------------------- revue des achats Pennylane : facture vigneron sans entrée en stock

def pennylane_purchase_tasks(log=None) -> int:
    """Chaque facture fournisseur vigneron (non archivée) sans mouvement d'achat « F <n°> » → tâche « achat facturé, livraison à confirmer / à saisir » (acomptes compris).
    Exception (JS, 14/09/2026) : les factures reconstituées à la reprise (achats.json, stock déjà constaté et recoupé avec Shopify) ne génèrent jamais de tâche ;
    seules les factures postérieures sans entrée en stock — livraison réellement en attente — en créent une."""
    from app.core.connectors.pennylane import for_company
    from . import reprise
    log = log or (lambda m: None)
    c = for_company(CODE)
    if not c:
        return 0
    done_ids, done_nums = reprise.reconstituted_invoices()
    sup, cur = {}, None
    for _ in range(5):
        d = c.get("/suppliers", limit=100, **({"cursor": cur} if cur else {}))
        for x in d.get("items") or []:
            sup[x["id"]] = x.get("name") or ""
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    refs = {(m.ref or "") for m in moves(limit=100000) if m.kind == "purchase"}
    invs, cur = [], None
    for _ in range(5):
        d = c.get("/supplier_invoices", limit=100, **({"cursor": cur} if cur else {}))
        invs += d.get("items") or []
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    n, still = 0, set()
    for i in invs:
        if i.get("archived_at") or (i.get("date") or "") < "2025-01-01":
            continue
        name = sup.get((i.get("supplier") or {}).get("id"), "")
        if not name or any(k in name.upper() for k in config.NON_WINE_SUPPLIERS):
            continue
        num = str(i.get("invoice_number") or "").strip()
        if int(i.get("id") or 0) in done_ids or num in done_nums or not num or f"F {num}" in refs or f"F {num.split(' ')[0]}" in refs:
            continue
        key = f"purchase:{i.get('id')}"; still.add(key)
        acompte = "ACOMPTE" in (i.get("label") or "").upper()
        if not acompte:                                   # libellé Pennylane générique → on regarde les lignes (« ACOMPTE » = commande passée, livraison à venir)
            try:
                r = c.get(f"/supplier_invoices/{i.get('id')}/invoice_lines")
                acompte = any("ACOMPTE" in str(l.get("label") or "").upper() for l in ((r.get("items") if isinstance(r, dict) else r) or []))
            except Exception:
                pass
        ht = float(i.get("currency_amount_before_tax") or 0)
        title = (f"Acompte {name} n°{num} du {i.get('date')} ({ht:.2f} € HT) : livraison en attente" if acompte
                 else f"Facture {name} n°{num} du {i.get('date')} ({ht:.2f} € HT) : livraison à confirmer et à saisir dans le stock")
        if add_task("purchase_pending", title, ref=num, key=key, details="Revue automatique Pennylane ↔ stock : aucune entrée « F n° » dans le livre des mouvements. À la réception chez Alix, saisir les bouteilles (Mouvements → entrée d'achat, réf. « F n° »)."):
            n += 1
    for t in tasks("open"):
        if t.kind == "purchase_pending" and t.key and t.key not in still:
            close_tasks_by_key(t.key)
    log(f"Pennylane achats : {n} nouvelle(s) tâche(s) de livraison à confirmer")
    return n


# ---------------------------------------------------------------- Shopify : commande « traitée » (n° Chronopost) à la date d'enlèvement

def shopify_fulfill(o: OwOrder, log=None) -> str:
    """Marque la commande comme traitée dans Shopify avec les n° Chronopost des cartons, sans e-mail Shopify au client."""
    log = log or (lambda m: None)
    c = _shopify()
    if not c:
        raise RuntimeError("Shopify non configuré")
    d = c.gql("""query($q:String!){ orders(first:1, query:$q){ edges{ node{ id name displayFulfillmentStatus fulfillmentOrders(first:10){ edges{ node{ id status } } } } } } }""", {"q": f"name:{o.name}"})
    nodes = [e["node"] for e in d["orders"]["edges"] if e["node"]["name"] == o.name]
    if not nodes:
        raise RuntimeError(f"{o.name} introuvable dans Shopify")
    node = nodes[0]
    if node["displayFulfillmentStatus"] == "FULFILLED":
        o.fulfillment_status = "FULFILLED"; save_order(o); return "déjà traitée dans Shopify"
    fos = [e["node"]["id"] for e in node["fulfillmentOrders"]["edges"] if e["node"]["status"] in ("OPEN", "IN_PROGRESS", "SCHEDULED")]
    if not fos:
        raise RuntimeError(f"{o.name} : aucun ordre de traitement ouvert dans Shopify")
    nums = [x.tracking for x in cartons(o.id) if x.tracking]
    fulfillment = {"lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": f} for f in fos], "notifyCustomer": False}
    if nums:
        fulfillment["trackingInfo"] = {"company": "Chronopost", "numbers": nums, "url": f"https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT={nums[0]}"}
    r = c.gql("""mutation($f:FulfillmentInput!){ fulfillmentCreate(fulfillment:$f){ fulfillment{ id status } userErrors{ field message } } }""", {"f": fulfillment})
    res = r["fulfillmentCreate"]
    if res.get("userErrors"):
        raise RuntimeError(f"Shopify : {res['userErrors']}")
    o.fulfillment_status = "FULFILLED"; save_order(o)
    log(f"{o.name} : traitée dans Shopify ({', '.join(nums) or 'sans n° de suivi'})")
    return f"traitée dans Shopify ({', '.join(nums) or 'sans n° de suivi'})"


def shopify_fulfill_due(log=None) -> int:
    """Commandes envoyées (enlèvement Chronopost passé, ou retrait) pas encore traitées dans Shopify → traitement créé avec les n° de suivi."""
    log = log or (lambda m: None); n = 0
    for o in orders():
        if o.status not in ("attente_reception", "a_facturer", "cloturee") or o.fulfillment_status == "FULFILLED" or not o.shopify_id:
            continue
        if o.mode not in ("chronopost", "retrait") or (o.mode == "chronopost" and (not o.pickup_date or o.pickup_date > date.today())):
            continue
        try:
            shopify_fulfill(o, log=log); n += 1
        except Exception as e:
            log(f"{o.name} : Shopify — {e}")
    return n


# ---------------------------------------------------------------- dépôt-vente LMB : vue d'ensemble, bons de livraison valorisés, historique des ventes

def blv_meta(no: str) -> dict:
    return _setting_get(f"owine:blv:{no}") or {}


def blv_meta_set(no: str, **kw) -> None:
    m = blv_meta(no); m.update({k: v for k, v in kw.items() if v is not None}); _setting_set(f"owine:blv:{no}", m)


def blv_pdf(no: str) -> Optional[Tuple[str, bytes]]:
    import base64
    d = _setting_get(f"owine:blv_pdf:{no}")
    return (d.get("name") or f"BLV {no}.pdf", base64.b64decode(d["b64"])) if d and d.get("b64") else None


def blv_pdf_set(no: str, name: str, data: bytes) -> None:
    import base64
    _setting_set(f"owine:blv_pdf:{no}", {"name": name, "b64": base64.b64encode(data).decode(), "size": len(data), "at": datetime.utcnow().isoformat(timespec="seconds")})


def _blv_no(ref: str) -> str:
    return (ref or "").replace("BLV", "").strip()


def lmb_overview() -> dict:
    """Tout le dépôt-vente en une passe : stock en dépôt, BLV, ventes à facturer, historique des ventes (mois et détail)."""
    imap = item_map(); prices = lmb_prices(); alloc = lmb_allocation(); lots = lmb_lots()
    custs = {o.name: (o.customer or "") for o in orders()}
    pv: Dict[Tuple[str, str], Tuple[float, float]] = {}                      # (commande, sku) → (prix catalogue du jour TTC, prix net encaissé TTC)
    for o in orders():
        for l in order_lines(o):
            if l.get("sku"):
                pv[(o.name, l["sku"])] = (float(l.get("price") or 0), float(l["net"]) if l.get("net") is not None else float(l.get("price") or 0))
    dep = defaultdict(lambda: {"in": 0.0, "sold": 0.0, "price": None, "title": ""})
    blvs: Dict[str, dict] = {}
    sales = []
    for m in moves(owner="LMB", limit=100000):
        if m.location != "ALIX":
            continue
        if m.kind == "deposit_in":
            dep[m.sku]["in"] += m.qty
            no = _blv_no(m.ref); b = blvs.setdefault(no, {"no": no, "ref": m.ref, "date": m.date, "qty": 0, "ht": 0.0, "lines": []})
            b["qty"] += m.qty; b["ht"] += m.qty * (m.unit_cost or 0)
            b["lines"].append({"sku": m.sku, "title": wine_title(m.sku), "qty": m.qty, "pu": m.unit_cost or 0, "total": m.qty * (m.unit_cost or 0)})
        elif m.kind in ("sale", "pickup"):
            dep[m.sku]["sold"] += -m.qty
            list_ttc, net_ttc = pv.get((m.ref, m.sku), (0.0, 0.0))
            parts = alloc.get((m.ref, m.sku)) or [{"qty": -m.qty, "price": prices.get(m.sku) or 0.0, "lot": "?"}]
            dep_unit = sum(p["qty"] * p["price"] for p in parts) / max(sum(p["qty"] for p in parts), 1)
            sales.append({"date": m.date, "ref": m.ref, "customer": custs.get(m.ref, ""), "sku": m.sku, "title": wine_title(m.sku), "qty": -m.qty, "pv_ht": net_ttc / 1.2, "pv_ttc": net_ttc, "list_ttc": list_ttc,
                          "dep": dep_unit, "lot": " + ".join(sorted({p["lot"] for p in parts})), "margin": net_ttc / 1.2 - dep_unit, "kind": m.kind})
    for sku, d in dep.items():
        d["title"] = wine_title(sku); d["price"] = prices.get(sku) or (imap[sku].lmb_price if sku in imap else None)
        d["lots"] = " / ".join(f"{l['price']:.0f} € (BLV {l['lot']}, {l['qty']:.0f} btl)" for l in lots.get(sku, [])) if sku in lots else ""
    for b in blvs.values():
        b["meta"] = blv_meta(b["no"]); b["has_pdf"] = bool(_setting_get(f"owine:blv_pdf:{b['no']}")); b["lines"].sort(key=lambda l: l["title"])
    open_inv = {t.ref for t in tasks("open") if t.kind == "lmb_invoice"}
    by_order: Dict[str, dict] = {}
    for s in sales:
        x = by_order.setdefault(s["ref"], {"ref": s["ref"], "date": s["date"], "customer": s["customer"], "lines": [], "ht": 0.0, "sales_ht": 0.0, "margin": 0.0, "loss": False, "open": s["ref"] in open_inv})
        x["lines"].append(s); x["ht"] += s["qty"] * s["dep"]; x["sales_ht"] += s["qty"] * s["pv_ht"]; x["margin"] += s["qty"] * s["margin"]; x["loss"] = x["loss"] or s["margin"] <= 0
        s["invoiced"] = s["ref"] not in open_inv
    months: Dict[str, dict] = {}
    for s in sales:
        k = s["date"].strftime("%Y-%m"); mo = months.setdefault(k, {"month": k, "label": s["date"].strftime("%m/%Y"), "btl": 0, "pv_ht": 0.0, "dep": 0.0, "orders": set()})
        mo["btl"] += s["qty"]; mo["pv_ht"] += s["qty"] * s["pv_ht"]; mo["dep"] += s["qty"] * s["dep"]; mo["orders"].add(s["ref"])
    for mo in months.values():
        mo["margin"] = mo["pv_ht"] - mo["dep"]; mo["orders"] = len(mo["orders"])
    sales.sort(key=lambda s: (s["date"], s["ref"]), reverse=True)
    return {"dep": sorted(dep.items(), key=lambda kv: kv[1]["title"]), "blvs": sorted(blvs.values(), key=lambda b: b["date"], reverse=True),
            "to_invoice": sorted(by_order.values(), key=lambda x: x["ref"]), "total_to_invoice": sum(x["ht"] for x in by_order.values() if x["open"]),
            "months": sorted(months.values(), key=lambda m: m["month"], reverse=True), "sales": sales,
            "totals": {"btl": sum(s["qty"] for s in sales), "pv_ht": sum(s["qty"] * s["pv_ht"] for s in sales), "dep": sum(s["qty"] * s["dep"] for s in sales)}}


def blv_detail(no: str) -> Optional[dict]:
    """Un BLV avec, par vin, les bouteilles vendues imputées à ce lot (premier déposé, premier vendu) et les restantes."""
    ov = lmb_overview()
    b = next((x for x in ov["blvs"] if x["no"] == no), None)
    if not b:
        return None
    lots: Dict[str, List[dict]] = defaultdict(list)
    for x in sorted(ov["blvs"], key=lambda x: x["date"]):
        for l in x["lines"]:
            lots[l["sku"]].append({"no": x["no"], "left": l["qty"], "sold": 0.0})
    for s in sorted(ov["sales"], key=lambda s: s["date"]):
        q = s["qty"]
        for lot in lots.get(s["sku"], []):
            take = min(q, lot["left"]); lot["left"] -= take; lot["sold"] += take; q -= take
            if q <= 0:
                break
    for l in b["lines"]:
        lot = next((x for x in lots[l["sku"]] if x["no"] == no), None)
        l["sold"] = lot["sold"] if lot else 0; l["left"] = lot["left"] if lot else l["qty"]
    b["sold"] = sum(l["sold"] for l in b["lines"]); b["left"] = sum(l["left"] for l in b["lines"]); b["ttc"] = b["ht"] * 1.2
    return b
