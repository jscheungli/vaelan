"""OWINE : tableau de bord, commandes (cartons → Chronopost → Alix / client → réception), stock, emballages, tâches, dépôt-vente LMB."""
import json
import re
from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app.core.db import engine
from app.core.jobs import start_job
from app.core.security import current_user
from app.core import gmail_imap
from app.models import Run, OwOrder, Setting
from app.packs.owine import config as cfg, service, docs, jobs as ow_jobs, reprise
from app.web.routes import templates, _ctx, _company_or_redirect

router = APIRouter()
CODE = cfg.COMPANY


def _guard(request: Request, code: str):
    return _company_or_redirect(request, code, feature="owine")


def _who(request: Request) -> str:
    u = current_user(request)
    return (u.name or u.email) if u else "?"


def _d(s: str):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except Exception:
        return None


def _base(request, company, **extra):
    return _ctx(request, company=company, cfg=cfg, status_labels=cfg.ORDER_STATUS, modes=cfg.MODES, task_kinds=cfg.TASK_KINDS, eur=lambda x: f"{(x or 0):,.2f}".replace(",", " ").replace(".", ",") + " €", **extra)


# ============================== tableau de bord
@router.get("/c/{code}/owine", response_class=HTMLResponse)
def owine_home(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    orders = [o for o in service.orders() if o.status not in ("cloturee", "annulee")]
    tasks = service.tasks("open")
    st = service.stock()
    pack = {sku: st.get(sku, {}).get(("ALIX", "OWINE"), 0) for sku in cfg.PACKAGING}
    with Session(engine) as s:
        runs = s.exec(select(Run).where(Run.company_id == company.id).order_by(Run.id.desc()).limit(5)).all()
    try:
        rep = reprise.state()
    except Exception:
        rep = None
    return templates.TemplateResponse(request, "owine_home.html", _base(request, company, orders=orders, tasks=tasks, pack=pack, runs=runs, msg=msg, today=date.today(),
                                                                       cost_missing=service.cost_alerts(), reprise=rep))


@router.post("/c/{code}/owine/sync")
def owine_sync(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    start_job("owine_sync", ow_jobs.run_sync, company_id=company.id, pack="owine", label="OWINE — synchronisation Shopify et tâches Pennylane", user=current_user(request))
    return RedirectResponse(f"/c/{code}/owine?msg=Synchronisation lancée (quelques secondes).", status_code=303)


@router.post("/c/{code}/owine/reprise/{what}")
def owine_reprise(request: Request, code: str, what: str):
    """Reprise du 14/09/2026 (tableau de bord) : « livre » = livre des mouvements reconstitué ; « shopify » = corrections Shopify validées."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    if what == "livre":
        start_job("owine_reprise_livre", ow_jobs.run_reprise_livre, company_id=company.id, pack="owine", label="OWINE — reprise : livre des mouvements reconstitué", user=current_user(request))
    elif what == "shopify":
        start_job("owine_reprise_shopify", ow_jobs.run_reprise_shopify, company_id=company.id, pack="owine", label="OWINE — reprise : corrections Shopify", user=current_user(request))
    else:
        return RedirectResponse(f"/c/{code}/owine?msg=Reprise inconnue.", status_code=303)
    return RedirectResponse(f"/c/{code}/owine?msg=Reprise lancée : le résultat s'affiche dans « Dernières tâches » (quelques secondes).", status_code=303)


# ============================== commandes
@router.get("/c/{code}/owine/commandes", response_class=HTMLResponse)
def owine_orders(request: Request, code: str, all: int = 0):
    company, redir = _guard(request, code)
    if redir:
        return redir
    orders = service.orders()
    if not all:
        orders = [o for o in orders if o.status not in ("cloturee", "annulee")] or orders[:30]
    return templates.TemplateResponse(request, "owine_orders.html", _base(request, company, orders=orders, all=all))


def _order_ctx(request, company, o, msg=""):
    lines = service.order_lines(o)
    st = service.stock()
    imap = service.item_map()
    for l in lines:
        ow, lmb = service.available(l["sku"], st.get(l["sku"], {}))
        l["avail_owine"], l["avail_lmb"] = ow, lmb
        it = imap.get(l["sku"])
        if (not l.get("cost")) and it and it.cost:
            l["cost"] = it.cost
        l["cost_missing"] = not l.get("cost")
        l["short"] = (ow + lmb) < l["qty"]
    cs = service.cartons(o.id)
    from app.packs.owine import export as _owx0
    proposal = service.propose_cartons(lines, st, intl=(_owx0.zone(o.country) != "FR")) if not cs else None
    sheet = docs.chronopost_sheet(o, cs) if cs else None
    initial = [{"ref": p["ref"], "box_sku": p["box_sku"], "lines": p["lines"]} for p in proposal] if proposal else \
              [{"ref": c.ref, "box_sku": c.box_sku or "2036", "lines": service.carton_lines(c)} for c in cs]
    editor = {"lines": [{"sku": l["sku"], "title": l["title"], "qty": int(l["qty"]), "cost": float(l.get("cost") or 0), "price": float(l.get("price") or 0)} for l in lines],
              "cartons": initial, "boxes": {k: v for k, v in cfg.PACKAGING.items() if v["bottles"]}, "bottle_kg": cfg.BOTTLE_KG}
    invoice = None
    if o.status in ("attente_reception", "a_facturer", "cloturee"):
        try:
            invoice = service.pennylane_invoice_for(o.name, force=(o.status == "a_facturer"))
        except Exception:
            invoice = None
    from app.packs.owine import export as _owx
    exp = _owx.state(o, cs) if (_owx.zone(o.country) != "FR" and o.mode != "retrait") else None
    xsheet = docs.export_sheet(o, cs, exp) if (exp and cs) else None
    if exp:
        exp["cost"] = _owx.logistics_cost(o, cs); exp["unconfirmed"] = _owx.unconfirmed_wines(o, cs); exp["abv_email"] = _owx.abv_request_email(o, cs) if exp["unconfirmed"] else None
    return _base(request, company, o=o, lines=lines, cartons=cs, carton_lines=service.carton_lines, proposal=proposal, sheet=sheet, msg=msg, editor=json.dumps(editor, ensure_ascii=False),
                 exp=exp, xsheet=xsheet, products=_owx.X.PRODUCTS, incoterms=_owx.X.INCOTERMS, stored_invoice=(_owx.stored_invoice(o) is not None),
                 missing=docs.missing_vars(o, cs) if cs else [], invoice=invoice,
                 email_alix=docs.email_alix(o, cs) if cs else None, email_client=docs.email_client(o, cs) if cs else None,
                 tasks=[t for t in service.tasks("open") if t.ref == o.name], moves=service.moves(ref=o.name))


@router.get("/c/{code}/owine/commandes/{name}", response_class=HTMLResponse)
def owine_order(request: Request, code: str, name: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    if not o:
        return RedirectResponse(f"/c/{code}/owine/commandes", status_code=303)
    return templates.TemplateResponse(request, "owine_order.html", _order_ctx(request, company, o, msg))


@router.post("/c/{code}/owine/commandes/{name}/infos")
async def owine_order_infos(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    g = lambda k: (f.get(k) or "").strip() or None
    o.mode = g("mode") or o.mode
    o.pickup_no, o.pickup_slot, o.note = g("pickup_no"), g("pickup_slot"), g("note")
    o.pickup_date, o.delivery_date = _d(g("pickup_date")), _d(g("delivery_date"))
    if o.pickup_date and o.mode == "chronopost":
        o.delivery_date = docs.delivery_of(o.pickup_date)
    for k in ("customer", "company", "address1", "address2", "zip", "city", "phone", "email"):
        if g(k) is not None or k in f:
            setattr(o, k, g(k))
    if g("cancel"):
        o.status = "annulee"
    service.save_order(o)
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Informations enregistrées.", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/cartons")
async def owine_order_cartons(request: Request, code: str, name: str):
    """Valide la ventilation : soit la proposition automatique, soit les quantités saisies carton par carton."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    lines = service.order_lines(o); st = service.stock(); imap = service.item_map()
    for l in lines:
        it = imap.get(l["sku"])
        if not l.get("cost") and it and it.cost:
            l["cost"] = it.cost
    if f.get("auto") or not f.get("n_boxes"):
        from app.packs.owine import export as _owx0
        plan = service.propose_cartons(lines, st, intl=(_owx0.zone(o.country) != "FR"))
    else:
        n = int(f.get("n_boxes") or 1)
        plan = []
        for i in range(n):
            ref = chr(65 + i); box = f.get(f"box_{ref}") or "2036"
            blines = []
            for l in lines:
                q = int(f.get(f"q_{ref}_{l['sku']}") or 0)
                if q > 0:
                    ow, lmb = service.available(l["sku"], st.get(l["sku"], {}))
                    owner = "OWINE" if ow >= q else "LMB"
                    blines.append({"sku": l["sku"], "title": l["title"], "qty": q, "cost": float(l.get("cost") or 0), "price": float(l.get("price") or 0), "owner": owner})
            if blines:
                nb = sum(x["qty"] for x in blines)
                plan.append({"ref": ref, "box_sku": box, "lines": blines, "weight_kg": round(nb * cfg.BOTTLE_KG, 1), "insured_value": round(sum(x["qty"] * x["cost"] for x in blines))})
        # contrôle : toutes les bouteilles ventilées
        want = {l["sku"]: l["qty"] for l in lines}; got = defaultdict(int)
        for p in plan:
            for x in p["lines"]:
                got[x["sku"]] += x["qty"]
        if any(got[k] != v for k, v in want.items()):
            return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Ventilation incomplète : les quantités par carton ne correspondent pas à la commande.", status_code=303)
    service.validate_cartons(o, plan, by=_who(request))
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Cartons validés : {len(plan)}.", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/cartons/json")
async def owine_order_cartons_json(request: Request, code: str, name: str):
    """Ventilation faite en glisser-déposer : {cartons: [{ref, box_sku, skus: [sku, sku…]}]}. Contrôles : toutes les bouteilles de la
    commande placées une fois et une seule, capacité des cartons respectée, aucun carton vide."""
    from fastapi.responses import JSONResponse
    company, redir = _guard(request, code)
    if redir:
        return JSONResponse({"ok": False, "error": "accès refusé"}, status_code=403)
    o = service.get_order(name)
    if not o or o.status not in ("a_traiter", "cartons", "etiquettes"):
        return JSONResponse({"ok": False, "error": "commande introuvable ou déjà envoyée"})
    body = await request.json()
    lines = service.order_lines(o); st = service.stock(); imap = service.item_map()
    by_sku = {}
    for l in lines:
        it = imap.get(l["sku"])
        if not l.get("cost") and it and it.cost:
            l["cost"] = it.cost
        by_sku[l["sku"]] = l
    want = {l["sku"]: int(l["qty"]) for l in lines}
    got = defaultdict(int); errors = []
    plan = []
    for i, c in enumerate(body.get("cartons") or []):
        skus = [x for x in (c.get("skus") or []) if x]
        box = c.get("box_sku") or "2036"
        cap = (cfg.PACKAGING.get(box) or {}).get("bottles") or 6
        ref = (c.get("ref") or chr(65 + i)).strip().upper()[:2]
        if not skus:
            errors.append(f"carton {ref} vide"); continue
        if len(skus) > cap:
            errors.append(f"carton {ref} : {len(skus)} bouteilles pour un carton de {cap}")
        counts = defaultdict(int)
        for sku in skus:
            counts[sku] += 1; got[sku] += 1
        blines = []
        avail = {sku: service.available(sku, st.get(sku, {})) for sku in counts}
        for sku, q in counts.items():
            l = by_sku.get(sku)
            if not l:
                errors.append(f"carton {ref} : {sku} n'est pas dans la commande"); continue
            ow, lmb = avail[sku]
            owner = "OWINE" if ow >= q else "LMB"
            blines.append({"sku": sku, "title": l["title"], "qty": q, "cost": float(l.get("cost") or 0), "price": float(l.get("price") or 0), "owner": owner})
        nb = len(skus)
        plan.append({"ref": ref, "box_sku": box, "lines": blines, "weight_kg": round(nb * cfg.BOTTLE_KG, 1), "insured_value": round(sum(x["qty"] * x["cost"] for x in blines))})
    for sku, q in want.items():
        if got.get(sku, 0) != q:
            errors.append(f"{by_sku[sku]['title']} : {got.get(sku, 0)} placée(s) sur {q}")
    for sku in got:
        if sku not in want:
            errors.append(f"{sku} : bouteille en trop")
    refs = [p["ref"] for p in plan]
    if len(set(refs)) != len(refs):
        errors.append("références de cartons en double")
    if not plan:
        errors.append("aucun carton")
    if errors:
        return JSONResponse({"ok": False, "error": " · ".join(errors)})
    # conservation des numéros Chronopost déjà saisis pour les mêmes références
    old = {c.ref: c.tracking for c in service.cartons(o.id)}
    for p_ in plan:
        p_["tracking"] = old.get(p_["ref"])
    service.validate_cartons(o, plan, by=_who(request))
    return JSONResponse({"ok": True, "cartons": len(plan)})


@router.post("/c/{code}/owine/commandes/{name}/cartons/reset")
def owine_order_cartons_reset(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    if o.status in ("cartons", "etiquettes"):
        service.replace_cartons(o.id, []); o.status = "a_traiter"; service.save_order(o)
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/etiquettes")
async def owine_order_labels(request: Request, code: str, name: str):
    """Étiquettes Chronopost : PDF déposés (numéros lus) et/ou numéros saisis par carton ; les PDF sont conservés pour l'envoi."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    cs = service.cartons(o.id)
    files = []
    for up in f.getlist("files"):
        if hasattr(up, "filename") and up.filename:
            files.append((up.filename, await up.read()))
    mapping = docs.parse_labels(files, [c.ref for c in cs]) if files else {}
    for c in cs:
        v = (f.get(f"tracking_{c.ref}") or "").strip()
        if v:
            mapping[c.ref] = v
    n = service.apply_labels(o, mapping)
    if files:
        _store_labels(o, files)
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg={n} numéro(s) de colis enregistré(s).", status_code=303)


def _store_labels(o, files):
    """PDF d'étiquettes stockés en base (Setting) pour les documents et les e-mails."""
    import base64
    cs = service.cartons(o.id)
    by_num = {c.tracking: c.ref for c in cs if c.tracking}
    stored = []
    for fname, data in files:
        num = next((n for n in by_num if n in fname or n.encode() in data[:200000]), None)
        ref = by_num.get(num)
        stored.append({"name": f"{num} ({ref}).pdf" if num and ref else fname, "b64": base64.b64encode(data).decode()})
    with Session(engine) as s:
        key = f"owine:labels:{o.name}"
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == key)).first() or Setting(company_code=CODE, key=key, value="[]")
        st.value = json.dumps(stored)
        s.add(st); s.commit()


def _labels(o):
    import base64
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == f"owine:labels:{o.name}")).first()
    return [(x["name"], base64.b64decode(x["b64"])) for x in json.loads(st.value)] if st else []


@router.get("/c/{code}/owine/commandes/{name}/doc/{what}")
def owine_order_doc(request: Request, code: str, name: str, what: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); cs = service.cartons(o.id)
    if what == "colisage":
        return Response(docs.packing_list_pdf(o, cs), media_type="application/pdf", headers={"Content-Disposition": f"inline; filename=\"Detail {o.name}.pdf\"; filename*=UTF-8''D%C3%A9tail%20{o.name}.pdf"})
    if what == "alix":
        return Response(docs.alix_xlsx(o, cs), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{o.name}.xlsx"'})
    if what == "facture-commerciale":
        inv = owx.stored_invoice(o) if not request.query_params.get("live") else None
        data = inv[1] if inv else docs.commercial_invoice_pdf(o, cs)
        fname = (inv[0] if inv else f"Facture commerciale {owx.invoice_number(o)} (apercu).pdf").encode("ascii", "ignore").decode()
        return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{fname}"'})
    if what.startswith("etiquette"):
        return Response("?", status_code=404)
    if what == "zip":
        return Response(docs.bundle_zip(o, cs, _labels(o)), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{o.name} - envoi.zip"'})
    return Response("?", status_code=404)


def _email_edits(o) -> dict:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == f"owine:emails:{o.name}")).first()
    return json.loads(st.value) if st else {}


def _save_email_edits(o, d: dict) -> None:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == f"owine:emails:{o.name}")).first() or Setting(company_code=CODE, key=f"owine:emails:{o.name}", value="{}")
        st.value = json.dumps(d, ensure_ascii=False); s.add(st); s.commit()


def _emails_of(o, cs, ed: dict) -> dict:
    ea, ec = docs.email_alix(o, cs), docs.email_client(o, cs)
    return {"alix": {"to": ea["to"], "cc": [x.strip() for x in (ed.get("cc_alix") or ", ".join(ea["cc"])).split(",") if x.strip()], "subject": ed.get("subject_alix") or ea["subject"],
                     "html": docs.email_alix_html(o, cs, extra=ed.get("extra_alix") or ""), "text": ea["body"] + ("\n\n" + ed["extra_alix"] if ed.get("extra_alix") else "") + "\n\n" + _signature()},
            "client": {"to": [ed.get("to_client") or (ec["to"][0] if ec["to"] else "")], "cc": [x.strip() for x in (ed.get("cc_client") or ", ".join(ec["cc"])).split(",") if x.strip()], "subject": ed.get("subject_client") or ec["subject"],
                       "html": docs.email_client_html(o, cs, extra=ed.get("extra_client") or ""), "text": ec["body"] + ("\n\n" + ed["extra_client"] if ed.get("extra_client") else "") + "\n\n" + _signature()}}


@router.get("/c/{code}/owine/commandes/{name}/emails", response_class=HTMLResponse)
def owine_order_emails(request: Request, code: str, name: str, msg: str = ""):
    """Aperçu HTML des deux e-mails, retouches (objet, destinataires, paragraphe libre), envoi."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); cs = service.cartons(o.id)
    miss = docs.missing_vars(o, cs)
    if miss:
        return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Avant les e-mails, renseignez : {', '.join(miss)}.", status_code=303)
    ed = _email_edits(o)
    em = _emails_of(o, cs, ed)
    import base64
    logo_uri = "data:image/png;base64," + base64.b64encode(open(docs.LOGO, "rb").read()).decode()
    for k in ("alix", "client"):
        em[k]["preview"] = em[k]["html"].replace("cid:logo", logo_uri)
    labels = _labels(o)
    def size(b): return f"{len(b) / 1024:.0f} Ko"
    pl = docs.packing_list_pdf(o, cs); xl = docs.alix_xlsx(o, cs)
    base = f"/c/{code}/owine/commandes/{name}/doc"
    att_alix = [(f"Détail {o.name}.pdf", f"{base}/colisage", size(pl)), (f"{o.name}.xlsx", f"{base}/alix", size(xl))] + [(n, f"{base}/etiquette/{i}", size(d)) for i, (n, d) in enumerate(labels)]
    att_client = [(f"Détail {o.name}.pdf", f"{base}/colisage", size(pl))] + [(n, f"{base}/etiquette/{i}", size(d)) for i, (n, d) in enumerate(labels)]
    from app.packs.owine import export as _owx
    if _owx.zone(o.country) == "EXPORT":
        inv = _owx.stored_invoice(o); final = bool((_owx.get_state(o).get("invoice") or {}).get("final")) and not _owx.unconfirmed_wines(o, cs)
        name_ = (inv[0] if inv else f"Facture commerciale {_owx.invoice_number(o)}.pdf") + (" (3 exemplaires)" if final else " (PROVISOIRE, degrés ou numéros à confirmer)")
        att_alix.append((name_, f"{base}/facture-commerciale" + ("" if inv else "?live=1"), size(inv[1]) if inv else "—"))
        if final:
            att_client.append((inv[0], f"{base}/facture-commerciale", size(inv[1])))
    return templates.TemplateResponse(request, "owine_emails.html", _base(request, company, o=o, em=em, ed=ed, msg=msg, gmail_ok=gmail_imap.configured(CODE), labels=[n for n, _ in labels],
                                                                         att_alix=att_alix, att_client=att_client, sent=o.status in ("envoye", "attente_reception", "a_facturer", "cloturee")))


@router.post("/c/{code}/owine/commandes/{name}/emails")
async def owine_order_emails_save(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    ed = _email_edits(o)
    for k in ("subject_alix", "subject_client", "extra_alix", "extra_client", "cc_alix", "to_client", "cc_client"):
        if k in f:
            ed[k] = (f.get(k) or "").strip()
    _save_email_edits(o, ed)
    if f.get("action") == "send":
        return _send_emails(request, code, o, ed)
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}/emails?msg=Aperçu mis à jour.", status_code=303)


def _send_emails(request, code, o, ed):
    cs = service.cartons(o.id)
    miss = docs.missing_vars(o, cs)
    if miss:
        return RedirectResponse(f"/c/{code}/owine/commandes/{o.name}?msg=Envoi impossible, il manque : {', '.join(miss)}.", status_code=303)
    if o.status not in ("cartons", "etiquettes"):
        return RedirectResponse(f"/c/{code}/owine/commandes/{o.name}/emails?msg=Cette commande a déjà été envoyée.", status_code=303)
    em = _emails_of(o, cs, ed)
    pl = docs.packing_list_pdf(o, cs); xl = docs.alix_xlsx(o, cs); labels = _labels(o)
    logo = [("logo", open(docs.LOGO, "rb").read(), "image/png")]
    att_alix = [(f"Détail {o.name}.pdf", pl, "application/pdf"), (f"{o.name}.xlsx", xl, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")] + [(n, d, "application/pdf") for n, d in labels]
    att_cli = [(f"Détail {o.name}.pdf", pl, "application/pdf")] + [(n, d, "application/pdf") for n, d in labels]
    from app.packs.owine import export as _owx
    if _owx.zone(o.country) == "EXPORT":
        inv = _owx.stored_invoice(o); final = bool((_owx.get_state(o).get("invoice") or {}).get("final")) and not _owx.unconfirmed_wines(o, cs)
        if not inv:
            data = docs.commercial_invoice_pdf(o, cs); _owx.store_invoice(o, data, by=_who(request), final=False); inv = (f"Facture commerciale {_owx.invoice_number(o)} (provisoire).pdf", data)
        att_alix.append(((inv[0] if final else inv[0].replace(".pdf", " (PROVISOIRE).pdf")), inv[1], "application/pdf"))
        if final:
            att_cli.append((inv[0], inv[1], "application/pdf"))
    ok1, m1 = gmail_imap.send_mail(CODE, em["alix"]["to"], em["alix"]["subject"], em["alix"]["text"], cc=em["alix"]["cc"], attachments=att_alix, html=em["alix"]["html"], inline=logo,
                                   from_name="Jean-Sébastien CHEUNG-AH-SEUNG · oWine", reply_to=cfg.CONTACT_EMAIL)
    ok2, m2 = gmail_imap.send_mail(CODE, em["client"]["to"], em["client"]["subject"], em["client"]["text"], cc=em["client"]["cc"], attachments=att_cli, html=em["client"]["html"], inline=logo,
                                   from_name="Jean-Sébastien CHEUNG-AH-SEUNG · oWine", reply_to=cfg.CONTACT_EMAIL)
    if ok1 and ok2:
        service.mark_sent(o, by=_who(request))
        return RedirectResponse(f"/c/{code}/owine/commandes/{o.name}?msg=Les deux e-mails sont partis (Alix et client) : stock décompté, suivi de réception créé.", status_code=303)
    return RedirectResponse(f"/c/{code}/owine/commandes/{o.name}/emails?msg=Envoi incomplet — Alix : {m1} · client : {m2}. Rien n'a été décompté.", status_code=303)


@router.get("/c/{code}/owine/commandes/{name}/doc/etiquette/{idx}")
def owine_order_label(request: Request, code: str, name: str, idx: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); labels = _labels(o)
    if idx < 0 or idx >= len(labels):
        return Response("Étiquette introuvable", status_code=404)
    n, d = labels[idx]
    return Response(d, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{n.encode("ascii", "ignore").decode()}"'})


def _signature() -> str:
    return "Jean-Sébastien CHEUNG-AH-SEUNG\n\noWine SAS · Parc d'activité, 14 E rue Coubertin, 21000 Dijon\nwww.owine.co · contact@owine.co"


@router.post("/c/{code}/owine/commandes/{name}/envoye")
def owine_order_sent(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    if o.status not in ("cartons", "etiquettes"):
        return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Cette commande n'est pas au bon stade.", status_code=303)
    service.mark_sent(o, by=_who(request))
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Commande marquée envoyée : stock et emballages décomptés, suivi de réception créé.", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/reception")
def owine_order_reception(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    service.confirm_reception(o, by=_who(request))
    o = service.get_order(name)
    msg = "Réception confirmée. La facture Pennylane est déjà validée : commande clôturée." if o.status == "cloturee" else "Réception confirmée. Reste à valider la facture dans Pennylane (tâche créée)."
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg={msg}", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/cloturer")
def owine_order_close(request: Request, code: str, name: str):
    """Clôture : facture validée dans Pennylane (détectée, ou confirmée à la main)."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    if o.status == "attente_reception":
        return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Confirmez d'abord la réception.", status_code=303)
    service.close_order(o, by=_who(request))
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Commande clôturée.", status_code=303)


# ============================== stock
@router.get("/c/{code}/owine/stock", response_class=HTMLResponse)
def owine_stock(request: Request, code: str, q: str = "", tout: int = 0):
    company, redir = _guard(request, code)
    if redir:
        return redir
    st = service.stock()
    shop = _shopify_snapshot()
    rows = []
    for it in service.items("wine"):
        s = st.get(it.sku, {})
        r = {"it": it, "ow": s.get(("ALIX", "OWINE"), 0), "lmb": s.get(("ALIX", "LMB"), 0), "chaux": s.get(("CHAUX", "LMB"), 0), "shop": (shop.get(it.sku) or {}).get("on_hand")}
        r["ecart"] = (r["ow"] + r["lmb"]) - (r["shop"] if r["shop"] is not None else (r["ow"] + r["lmb"]))
        if q and q.lower() not in (it.title or "").lower() + " " + it.sku.lower():
            continue
        if not tout and not (r["ow"] or r["lmb"] or r["chaux"] or r["shop"]):
            continue
        rows.append(r)
    tot = {k: sum(r[k] or 0 for r in rows) for k in ("ow", "lmb", "chaux")}
    tot["shop"] = sum(r["shop"] or 0 for r in rows)
    return templates.TemplateResponse(request, "owine_stock.html", _base(request, company, rows=rows, tot=tot, q=q, tout=tout, snapshot_at=shop.get("_at")))


def _shopify_snapshot() -> dict:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == "owine:shopify_stock")).first()
    return json.loads(st.value) if st else {}


@router.post("/c/{code}/owine/stock/shopify")
def owine_stock_shopify(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    snap = service.shopify_inventory(); snap["_at"] = service.now_local().strftime("%d/%m/%Y %H:%M")
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == CODE, Setting.key == "owine:shopify_stock")).first() or Setting(company_code=CODE, key="owine:shopify_stock", value="{}")
        st.value = json.dumps(snap); s.add(st); s.commit()
    return RedirectResponse(f"/c/{code}/owine/stock", status_code=303)


@router.get("/c/{code}/owine/stock/{sku}", response_class=HTMLResponse)
def owine_item(request: Request, code: str, sku: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    it = service.get_item(sku)
    if not it:
        return RedirectResponse(f"/c/{code}/owine/stock", status_code=303)
    st = service.stock_of(sku)
    return templates.TemplateResponse(request, "owine_item.html", _base(request, company, it=it, st=st, moves=service.moves(sku=sku), kinds=cfg.MOVE_KINDS, msg=msg,
                                                                       shop=(_shopify_snapshot().get(sku) or {})))


@router.post("/c/{code}/owine/stock/{sku}/mouvement")
async def owine_item_move(request: Request, code: str, sku: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    qty = float((f.get("qty") or "0").replace(",", "."))
    if qty:
        service.add_move(sku, qty, f.get("kind") or "adjustment", d=_d(f.get("date")) or date.today(), location=f.get("location") or "ALIX", owner=f.get("owner") or "OWINE",
                         ref=(f.get("ref") or "").strip() or None, unit_cost=float(f.get("unit_cost") or 0) or None, note=(f.get("note") or "").strip() or None, source="manual", by=_who(request))
    return RedirectResponse(f"/c/{code}/owine/stock/{sku}?msg=Mouvement enregistré.", status_code=303)


@router.post("/c/{code}/owine/stock/{sku}/infos")
async def owine_item_infos(request: Request, code: str, sku: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    fields = {}
    for k in ("cost", "lmb_price", "price"):
        v = (f.get(k) or "").replace(",", ".").strip()
        if v:
            fields[k] = float(v)
    if "cost" in fields:
        fields["cost_source"] = "manual"
    service.upsert_item(sku, **fields)
    service.close_tasks_by_key(f"cost:{sku}") if "cost" in fields else None
    return RedirectResponse(f"/c/{code}/owine/stock/{sku}?msg=Fiche mise à jour.", status_code=303)


@router.get("/c/{code}/owine/mouvements", response_class=HTMLResponse)
def owine_moves(request: Request, code: str, ref: str = "", owner: str = "", location: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return templates.TemplateResponse(request, "owine_moves.html", _base(request, company, moves=service.moves(ref=ref or None, owner=owner or None, location=location or None, limit=800),
                                                                        kinds=cfg.MOVE_KINDS, items=service.item_map(), ref=ref, owner=owner, location=location))


# ============================== emballages
@router.get("/c/{code}/owine/emballages", response_class=HTMLResponse)
def owine_packaging(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    st = service.stock()
    rows = [{"sku": sku, "p": p, "stock": st.get(sku, {}).get(("ALIX", "OWINE"), 0)} for sku, p in cfg.PACKAGING.items()]
    mv = [m for m in service.moves(limit=2000) if m.sku in cfg.PACKAGING][:200]
    return templates.TemplateResponse(request, "owine_packaging.html", _base(request, company, rows=rows, moves=mv, kinds=cfg.MOVE_KINDS, msg=msg, today=date.today()))


@router.post("/c/{code}/owine/emballages/reception")
async def owine_packaging_reception(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    d = _d(f.get("date")) or date.today(); ref = (f.get("ref") or f"Réception {d:%d/%m/%Y}").strip(); n = 0
    for sku in cfg.PACKAGING:
        q = float((f.get(f"q_{sku}") or "0").replace(",", "."))
        if q:
            service.add_move(sku, q, "packaging_in" if q > 0 else "adjustment", d=d, location="ALIX", owner="OWINE", ref=ref, note=(f.get("note") or "").strip() or None, source="manual", by=_who(request))
            n += 1
    return RedirectResponse(f"/c/{code}/owine/emballages?msg={n} mouvement(s) enregistré(s).", status_code=303)


# ============================== tâches
@router.get("/c/{code}/owine/taches", response_class=HTMLResponse)
def owine_tasks(request: Request, code: str, done: int = 0):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return templates.TemplateResponse(request, "owine_tasks.html", _base(request, company, tasks=service.tasks("done" if done else "open"), done=done, today=date.today()))


@router.post("/c/{code}/owine/taches/{tid}/fait")
def owine_task_done(request: Request, code: str, tid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    service.close_task(tid)
    return RedirectResponse(f"/c/{code}/owine/taches", status_code=303)


@router.post("/c/{code}/owine/taches")
async def owine_task_add(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    if (f.get("title") or "").strip():
        service.add_task(f.get("kind") or "other", f.get("title").strip(), ref=(f.get("ref") or "").strip() or None, due=_d(f.get("due")), details=(f.get("details") or "").strip() or None)
    return RedirectResponse(f"/c/{code}/owine/taches", status_code=303)


# ============================== dépôt-vente LMB
@router.get("/c/{code}/owine/lmb", response_class=HTMLResponse)
def owine_lmb(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    ov = service.lmb_overview()
    return templates.TemplateResponse(request, "owine_lmb.html", _base(request, company, msg=msg, drafts={x["ref"]: service.lmb_draft_info(x["ref"]) for x in ov["to_invoice"]}, **ov))


@router.get("/c/{code}/owine/lmb/blv/{no}", response_class=HTMLResponse)
def owine_lmb_blv(request: Request, code: str, no: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    b = service.blv_detail(no)
    if not b:
        return RedirectResponse(f"/c/{code}/owine/lmb?msg=BLV {no} inconnu#t-blv", status_code=303)
    return templates.TemplateResponse(request, "owine_lmb_blv.html", _base(request, company, b=b, msg=msg))


@router.get("/c/{code}/owine/lmb/blv/{no}/pdf")
def owine_lmb_blv_pdf(request: Request, code: str, no: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = service.blv_pdf(no)
    if not f:
        return RedirectResponse(f"/c/{code}/owine/lmb/blv/{no}?msg=Aucun PDF joint à ce bon.", status_code=303)
    return Response(content=f[1], media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="BLV {no}.pdf"'})


@router.post("/c/{code}/owine/lmb/blv/{no}/pdf")
async def owine_lmb_blv_pdf_upload(request: Request, code: str, no: str, file: UploadFile = File(...)):
    company, redir = _guard(request, code)
    if redir:
        return redir
    data = await file.read()
    if not data or not data[:5].startswith(b"%PDF"):
        return RedirectResponse(f"/c/{code}/owine/lmb/blv/{no}?msg=Le fichier doit être un PDF.", status_code=303)
    service.blv_pdf_set(no, file.filename or f"BLV {no}.pdf", data)
    return RedirectResponse(f"/c/{code}/owine/lmb/blv/{no}?msg=Bon signé enregistré ({len(data) // 1024} Ko).", status_code=303)


@router.post("/c/{code}/owine/lmb/blv/{no}/infos")
async def owine_lmb_blv_infos(request: Request, code: str, no: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    service.blv_meta_set(no, received=(f.get("received") or "").strip() or None, controlled=(f.get("controlled") or "").strip() or None, note=(f.get("note") or "").strip() or None, signed=(f.get("signed") or "").strip() or None)
    return RedirectResponse(f"/c/{code}/owine/lmb/blv/{no}?msg=Informations enregistrées.", status_code=303)


@router.post("/c/{code}/owine/lmb/{name}/brouillon")
def owine_lmb_draft(request: Request, code: str, name: str):
    """Crée (ou recrée) dans Pennylane LMB le brouillon de facture LMB → OWINE de la commande."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    try:
        info = service.create_lmb_draft(name, by=_who(request))
        msg = f"{name} : brouillon créé dans Pennylane La Mémoire de Bourgogne ({info.get('amount')} € TTC). Vérifiez-le puis finalisez-le dans Pennylane : la tâche se fermera à la prochaine synchronisation."
    except Exception as e:
        msg = f"{name} : {e}"
    return RedirectResponse(f"/c/{code}/owine/lmb?msg={msg}#{name}", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/shopify-sync")
def owine_order_shopify_sync(request: Request, code: str, name: str):
    """Relit cette seule commande dans Shopify (client choisi après coup, adresse corrigée, statuts) sans attendre la synchronisation globale."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    if not o:
        return RedirectResponse(f"/c/{code}/owine/commandes", status_code=303)
    try:
        msg = f"{name} : {service.sync_order(o)}"
    except Exception as e:
        msg = f"{name} : synchronisation Shopify impossible — {e}"
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg={msg}", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/shopify-traitee")
def owine_order_shopify_fulfill(request: Request, code: str, name: str):
    """Marque la commande comme traitée dans Shopify (n° Chronopost des cartons), sans e-mail Shopify au client."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    try:
        msg = f"{name} : {service.shopify_fulfill(o)}"
    except Exception as e:
        msg = f"{name} : {e}"
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg={msg}", status_code=303)


# ============================== EXPORT / international (v0.1.195) : formalités sur la commande, page Douane, formulaire public
from app.packs.owine import export as owx


def _exp_redirect(code, name, msg):
    from urllib.parse import quote
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg={quote(str(msg))}#export", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/export/infos")
async def owine_export_infos(request: Request, code: str, name: str):
    """Réglages export de la commande : type de client, n° TVA / EORI / identifiant fiscal, produit Chronopost, incoterm, n° et date de facture, port HT, notes."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    g = lambda k: (f.get(k) or "").strip()
    fields = {k: (g(k) or None) for k in ("customer_type", "vat_number", "eori", "tax_id", "billing_company", "product") if k in f}
    for k in ("incoterm", "invoice_no", "invoice_date", "notes", "content_desc"):
        if k in f:
            fields[k] = g(k) or ("DAP" if k == "incoterm" else "")
    def money(k):
        v = re.sub(r"[^0-9.,]", "", g(k)).replace(",", ".")
        try:
            return float(v) if v else None
        except ValueError:
            return None
    if "shipping_ht" in f:
        fields["shipping_ht"] = money("shipping_ht")
    if "insurance_ht" in f:
        fields["insurance_ht"] = money("insurance_ht") or 0
    if g("company"):
        o.company = g("company")
    owx.set_info(o, by=_who(request), **fields)
    return _exp_redirect(code, name, "Réglages export enregistrés.")


@router.post("/c/{code}/owine/commandes/{name}/export/check/{key}")
async def owine_export_check(request: Request, code: str, name: str, key: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    owx.toggle_check(o, key, (f.get("done") or "") == "1", by=_who(request), note=(f.get("note") or "").strip() or None)
    return _exp_redirect(code, name, "Étape mise à jour.")


@router.post("/c/{code}/owine/commandes/{name}/export/facture")
def owine_export_invoice_make(request: Request, code: str, name: str):
    """Génère et archive la facture commerciale (3 exemplaires par envoi) ; « définitive » quand tous les cartons ont leur n° Chronopost."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); cs = service.cartons(o.id)
    if not cs:
        return _exp_redirect(code, name, "Validez d'abord les cartons.")
    try:
        pdf = docs.commercial_invoice_pdf(o, cs)
    except Exception as e:
        return _exp_redirect(code, name, f"Facture impossible : {e}")
    final = all(c.tracking for c in cs)
    meta = owx.store_invoice(o, pdf, by=_who(request), final=final)
    return _exp_redirect(code, name, f"Facture commerciale {meta['no']} générée (version {meta['version']}, {'définitive' if final else 'PROVISOIRE : numéros de colis manquants'}).")


@router.post("/c/{code}/owine/commandes/{name}/export/demande-infos")
def owine_export_ask_info(request: Request, code: str, name: str):
    """Envoie au client (FR ou EN) le lien du formulaire public « informations douanières » et crée la tâche de suivi."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    if not o.email:
        return _exp_redirect(code, name, "Pas d'e-mail client.")
    url = owx.customs_url(o); lang = "en" if (o.locale or "fr").lower()[:2] != "fr" else "fr"
    cname = owx.X.country_name(o.country, lang); first = (o.customer or "").split(" ")[0]
    if lang == "en":
        subject = f"Your oWine order {o.name}: information needed for shipping to {cname}"
        body = (f"Hello {first},\n\nThank you for your order {o.name}. To ship it to {cname} with Chronopost, customs require a few details that the online checkout does not collect: "
                f"a phone number, and for a company its VAT / EORI number. It takes one minute here:\n\n{url}\n\nYour order is shipped Incoterm DAP: import duties, VAT and clearance fees of your country are payable to the carrier before delivery.\n\n"
                "Thank you, and see you very soon,\nJean-Sébastien CHEUNG-AH-SEUNG · oWine")
    else:
        subject = f"Votre commande oWine {o.name} : informations nécessaires pour l'expédition en {cname}"
        body = (f"Bonjour {first},\n\nMerci pour votre commande {o.name}. Pour l'expédier en {cname} avec Chronopost, la douane exige quelques informations que la commande en ligne ne collecte pas : "
                f"un numéro de téléphone et, pour une société, le n° de TVA / EORI. Cela prend une minute ici :\n\n{url}\n\nVotre commande voyage en incoterm DAP : les droits, la TVA et les frais de dédouanement de votre pays sont réglés au transporteur avant la livraison.\n\n"
                "Merci, et à très bientôt,\nJean-Sébastien CHEUNG-AH-SEUNG · oWine")
    ok, m = gmail_imap.send_mail(CODE, [o.email], subject, body, cc=list(cfg.CLIENT_CC), from_name="Jean-Sébastien CHEUNG-AH-SEUNG · oWine", reply_to=cfg.CONTACT_EMAIL)
    if not ok:
        return _exp_redirect(code, name, f"Envoi impossible : {m}. Lien à transmettre vous-même : {url}")
    owx.mark_form_sent(o, by=_who(request))
    return _exp_redirect(code, name, f"Demande envoyée à {o.email} ({'anglais' if lang == 'en' else 'français'}).")


# ------------------------------ page Douane : réglages exportateur, signature, données douanières des vins, Shopify, matrice pays
@router.get("/c/{code}/owine/douane", response_class=HTMLResponse)
def owine_douane(request: Request, code: str, msg: str = "", tous: int = 0):
    company, redir = _guard(request, code)
    if redir:
        return redir
    rows = owx.customs_rows(active_only=not tous)
    plan = None
    try:
        plan = owx.shopify_customs_plan()
    except Exception as e:
        plan = {"error": str(e)[:200], "hs": [], "weight": [], "skipped": []}
    matrix = []
    for c, r in owx.X.COUNTRY_RULES.items():
        matrix.append({"code": c, "name": owx.X.country_name(c), "zone": owx.zone(c), "customs": bool(r.get("customs")),
                       "b2c": {p: owx.product_status(c, "particulier", p) for p in ("classic", "express")}, "b2b": {p: owx.product_status(c, "societe", p) for p in ("classic", "express")},
                       "notes": (r.get("notes") or []) + ((r.get("b2c") or {}).get("notes") or []) + ((r.get("b2b") or {}).get("notes") or []),
                       "zoning": {p: owx.X.zoning(p, c) for p in ("classic", "express")}})
    matrix.sort(key=lambda m: ({"UE": 0, "EXPORT": 1}.get(m["zone"], 2), m["name"]))
    try:
        ship_plan = owx.shopify_shipping_plan()
    except Exception as e:
        ship_plan = {"error": str(e)[:200], "current": [], "delete": [], "create": []}
    try:
        tr_plan = owx.translation_plan(limit=50)
    except Exception as e:
        tr_plan = {"error": str(e)[:200], "resources": [], "missing_scopes": []}
    setup = owx.setup_items()
    open_keys = {t.key: t for t in service.tasks("open") if t.kind == "customs_setup"}
    done_keys = {t.key: t for t in service.tasks("done") if t.kind == "customs_setup"}
    for it in setup:
        k = f"setup:{it['id']}"; it["task"] = open_keys.get(k) or done_keys.get(k); it["closed"] = it["done"] or (k not in open_keys and k in done_keys)
    scopes = sorted(owx._scopes())
    return templates.TemplateResponse(request, "owine_douane.html", _base(request, company, msg=msg, rows=rows, tous=tous, settings=owx.settings(), has_sig=bool(owx.signature()), plan=plan, matrix=matrix,
                                                                         products=owx.X.PRODUCTS, forbidden=sorted(owx.X.country_name(c) for c in owx.X.FORBIDDEN), missing=sum(1 for r in rows if r["missing"]),
                                                                         unconfirmed=sum(1 for r in rows if r.get("abv_status") != "confirme"), zones=owx.zones(), rates=owx.rate_settings(), grid=owx.rate_grid(),
                                                                         ship_plan=ship_plan, setup=setup, jalons=owx.JALONS, leaks=owx.perimeter_leaks(), log=owx.customs_log()[:60], texts=owx.site_texts(), tr_plan=tr_plan, scopes=scopes, country_name=owx.X.country_name))


@router.post("/c/{code}/owine/douane/reglages")
async def owine_douane_settings(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    owx.save_settings(eori=f.get("eori"), siret=f.get("siret"), capital=f.get("capital"), phone=f.get("phone"), contact=f.get("contact"), sign_place=f.get("sign_place"), rcs=f.get("rcs"),
                      box_dims={k: f.get(f"dims_{k}") for k in ("2031", "2033", "2036")})
    return RedirectResponse(f"/c/{code}/owine/douane?msg=Réglages enregistrés.", status_code=303)


@router.post("/c/{code}/owine/douane/signature")
async def owine_douane_signature(request: Request, code: str, file: UploadFile = File(None)):
    company, redir = _guard(request, code)
    if redir:
        return redir
    form = await request.form()
    data = await file.read() if (file and file.filename) else b""
    if form.get("remove"):
        owx.set_signature(None)
        return RedirectResponse(f"/c/{code}/owine/douane?msg=Signature retirée.", status_code=303)
    if not data:
        return RedirectResponse(f"/c/{code}/owine/douane?msg=Choisissez un fichier image.", status_code=303)
    if len(data) > 1_000_000:
        return RedirectResponse(f"/c/{code}/owine/douane?msg=Image trop lourde (1 Mo maximum).", status_code=303)
    if not (data[:8].startswith(b"\x89PNG") or data[:3] == b"\xff\xd8\xff"):
        return RedirectResponse(f"/c/{code}/owine/douane?msg=La signature doit être une image PNG ou JPEG.", status_code=303)
    owx.set_signature(data, file.filename or "signature.png")
    return RedirectResponse(f"/c/{code}/owine/douane?msg=Signature enregistrée ({len(data) // 1024} Ko) : elle sera apposée sur les factures commerciales.", status_code=303)


@router.get("/c/{code}/owine/douane/signature.png")
def owine_douane_signature_png(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    sig = owx.signature()
    return Response(sig or b"", media_type="image/png" if sig else "text/plain", status_code=200 if sig else 404)


@router.post("/c/{code}/owine/douane/vins")
async def owine_douane_wines(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    n = owx.save_customs_rows(dict(f))
    return RedirectResponse(f"/c/{code}/owine/douane?msg={n} vin(s) mis à jour.", status_code=303)


@router.post("/c/{code}/owine/douane/shopify")
def owine_douane_shopify(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    start_job("owine_shopify_customs", ow_jobs.run_shopify_customs, company_id=company.id, pack="owine", label="OWINE — Shopify : codes SH, origine, poids des sélections", user=current_user(request))
    return RedirectResponse(f"/c/{code}/owine/douane?msg=Écriture Shopify lancée (résultat dans les tâches).", status_code=303)


# ------------------------------ formulaire PUBLIC « informations douanières » (lien envoyé au client, FR / EN)
def _pub_lang(request: Request, o) -> str:
    q = request.query_params.get("lang")
    if q in ("fr", "en"):
        return q
    return "en" if (o and (o.locale or "fr").lower()[:2] != "fr") else "fr"


@router.get("/douane/{token}", response_class=HTMLResponse)
def owine_customs_form(request: Request, token: str, done: int = 0, err: str = ""):
    o = owx.by_token(token)
    lang = _pub_lang(request, o)
    if not o:
        return templates.TemplateResponse(request, "owine_douane_public.html", {"o": None, "lang": lang, "done": 0, "err": "", "cfg": cfg}, status_code=404)
    if o.status not in ("a_traiter", "cartons", "etiquettes") and not done:
        return templates.TemplateResponse(request, "owine_douane_public.html", {"o": o, "lang": lang, "done": 1, "err": "", "cfg": cfg, "country": owx.X.country_name(o.country, lang)})
    light = {"customs": owx.zone(o.country) == "EXPORT", "tax_id_label": owx.X.TAX_ID_B2C.get((o.country or "").upper())}
    return templates.TemplateResponse(request, "owine_douane_public.html", {"o": o, "lang": lang, "done": done, "err": err, "cfg": cfg, "exp": light, "kind": owx.dest_kind(o),
                                                                            "country": owx.X.country_name(o.country, lang), "tax_id_label": light["tax_id_label"], "token": token})


@router.post("/douane/{token}")
async def owine_customs_form_submit(request: Request, token: str):
    o = owx.by_token(token)
    if not o or o.status not in ("a_traiter", "cartons", "etiquettes"):
        return RedirectResponse(f"/douane/{token}", status_code=303)
    f = await request.form(); lang = "en" if (f.get("lang") or "fr") == "en" else "fr"
    if not (f.get("phone") or "").strip():
        return RedirectResponse(f"/douane/{token}?lang={lang}&err=phone", status_code=303)
    if (f.get("kind") == "societe") and not ((f.get("vat") or "").strip() or (f.get("eori") or "").strip()):
        return RedirectResponse(f"/douane/{token}?lang={lang}&err=company", status_code=303)
    if f.get("dap") != "1":
        return RedirectResponse(f"/douane/{token}?lang={lang}&err=dap", status_code=303)
    fwd = request.headers.get("x-forwarded-for")
    owx.apply_customs_form(o, dict(f), ip=(fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")))
    return RedirectResponse(f"/douane/{token}?lang={lang}&done=1", status_code=303)


# ============================== EXPORT v2 : zones ouvertes, mise en place, degrés confirmés, textes du site, traduction, VIES
from fastapi.responses import JSONResponse, PlainTextResponse


@router.post("/c/{code}/owine/douane/zones")
async def owine_douane_zones(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    owx.save_zones(dict(f)); owx.save_rate_settings(dict(f))
    return RedirectResponse(f"/c/{code}/owine/douane?msg=Zones et réglages de la grille enregistrés.#t-zones", status_code=303)


@router.post("/c/{code}/owine/douane/zones/shopify")
def owine_douane_zones_shopify(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    start_job("owine_shipping", ow_jobs.run_shipping_apply, company_id=company.id, pack="owine", label="OWINE — Shopify : grille de port des zones ouvertes", user=current_user(request))
    return RedirectResponse(f"/c/{code}/owine/douane?msg=Écriture de la grille de port lancée (résultat dans les tâches).#t-zones", status_code=303)


@router.post("/c/{code}/owine/douane/vins/{sku}/confirmer")
async def owine_douane_confirm(request: Request, code: str, sku: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    if f.get("undo"):
        owx.unconfirm_wine(sku, by=_who(request)); msg = f"{sku} : confirmation retirée."
    else:
        owx.save_customs_rows(dict(f))                                   # les valeurs tapées sur toutes les lignes sont d'abord enregistrées
        ok = owx.confirm_wine(sku, by=_who(request))
        msg = f"{sku} : données douanières confirmées." if ok else f"{sku} : confirmation impossible — degré ou couleur manquant."
    return RedirectResponse(f"/c/{code}/owine/douane?msg={msg}#t-vins", status_code=303)


@router.post("/c/{code}/owine/douane/vins/confirmer-selection")
async def owine_douane_confirm_many(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form(); n = 0; ko = []
    owx.save_customs_rows(dict(f))
    for sku in f.getlist("sel"):
        if owx.confirm_wine(sku, by=_who(request)):
            n += 1
        else:
            ko.append(sku)
    msg = f"{n} vin(s) confirmé(s)." + (f" Non confirmés (degré ou couleur manquant) : {', '.join(ko)}." if ko else "")
    return RedirectResponse(f"/c/{code}/owine/douane?msg={msg}#t-vins", status_code=303)


@router.post("/c/{code}/owine/douane/vins/estimer")
def owine_douane_estimate(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    n = owx.fill_abv_estimates()
    return RedirectResponse(f"/c/{code}/owine/douane?msg={n} vin(s) : degrés pré-remplis (à confirmer).#t-vins", status_code=303)


@router.post("/c/{code}/owine/douane/setup/{item}")
async def owine_douane_setup(request: Request, code: str, item: str):
    """Mise en place : « fait » ferme la tâche setup:<id>, « rouvrir » la recrée."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    f = await request.form()
    if item == "refresh":
        n = owx.setup_tasks(); return RedirectResponse(f"/c/{code}/owine/douane?msg=Liste de mise en place actualisée ({n} nouvelle(s)).#t-setup", status_code=303)
    if f.get("reopen"):
        from sqlmodel import Session, select
        from app.models import OwTask
        with Session(engine) as s:
            t = s.exec(select(OwTask).where(OwTask.key == f"setup:{item}")).first()
            if t:
                t.status, t.done_at = "open", None; s.add(t); s.commit()
        return RedirectResponse(f"/c/{code}/owine/douane?msg=Étape rouverte.#t-setup", status_code=303)
    service.close_tasks_by_key(f"setup:{item}")
    return RedirectResponse(f"/c/{code}/owine/douane?msg=Étape marquée faite.#t-setup", status_code=303)


@router.get("/c/{code}/owine/douane/snippet.liquid")
def owine_douane_snippet(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return Response(owx.theme_snippet(), media_type="text/plain; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="owine-international.liquid"'})


@router.post("/c/{code}/owine/douane/traduction")
def owine_douane_translate(request: Request, code: str, what: str = "apply"):
    company, redir = _guard(request, code)
    if redir:
        return redir
    if what == "publish":
        try:
            msg = owx.publish_english()
        except Exception as e:
            msg = str(e)
        return RedirectResponse(f"/c/{code}/owine/douane?msg={msg}#t-langue", status_code=303)
    start_job("owine_translate", ow_jobs.run_translation, company_id=company.id, pack="owine", label="OWINE — Shopify : traduction anglaise (Claude → translationsRegister)", user=current_user(request))
    return RedirectResponse(f"/c/{code}/owine/douane?msg=Traduction lancée (plusieurs minutes ; résultat dans les tâches).#t-langue", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/export/demande-degres")
def owine_export_ask_abv(request: Request, code: str, name: str):
    """E-mail à Alix : confirmer les degrés (étiquette) des vins non confirmés de la commande, avant la facture commerciale."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); cs = service.cartons(o.id)
    em = owx.abv_request_email(o, cs)
    if not em["wines"]:
        return _exp_redirect(code, name, "Tous les degrés sont déjà confirmés.")
    ok, m = gmail_imap.send_mail(CODE, em["to"], em["subject"], em["body"] + "\n\n" + _signature(), cc=em["cc"], from_name="Jean-Sébastien CHEUNG-AH-SEUNG · oWine", reply_to=cfg.CONTACT_EMAIL)
    if not ok:
        return _exp_redirect(code, name, f"Envoi impossible : {m}")
    st = owx.get_state(o); st["abv_request_sent_at"] = service.now_local().strftime("%d/%m/%Y %H:%M"); owx.set_state(o, st)
    return _exp_redirect(code, name, f"Demande envoyée à Alix pour {len(em['wines'])} vin(s).")


@router.post("/c/{code}/owine/commandes/{name}/export/degres")
async def owine_export_confirm_abv(request: Request, code: str, name: str):
    """Réponse d'Alix reportée : un degré par vin non confirmé → vins confirmés (source « Alix, commande OWxxxx »)."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form(); n = 0
    for k, v in f.items():
        if k.startswith("abv_") and (v or "").strip():
            sku = k[4:]
            try:
                owx.confirm_wine(sku, by=f"{_who(request)} (réponse Alix, commande {o.name})", abv=float(str(v).replace(",", ".")), source=f"étiquette lue par Alix, commande {o.name}"); n += 1
            except ValueError:
                pass
    return _exp_redirect(code, name, f"{n} degré(s) confirmé(s).")


_VIES_HITS: dict = {}
_VIES_ORIGINS = {"https://owine.co", "https://www.owine.co", "https://2ac587-75.myshopify.com", "https://owineco.myshopify.com"}


@router.get("/api/owine/vies")
def owine_api_vies(request: Request, vat: str = ""):
    """Vérification VIES pour la page panier du site (extrait de thème) : {valid, name}. Format contrôlé, 30 appels par IP et par 10 minutes, résultats gardés côté Vaelan."""
    import time
    origin = request.headers.get("origin") or ""
    headers = {"Cache-Control": "no-store"}
    if origin in _VIES_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin; headers["Vary"] = "Origin"
    v = re.sub(r"[^A-Za-z0-9]", "", vat or "").upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{2,13}", v):
        return JSONResponse({"valid": False, "error": "format", "vat": v}, headers=headers)
    fwd = request.headers.get("x-forwarded-for"); ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))
    now = time.time(); hits = [t for t in _VIES_HITS.get(ip, []) if now - t < 600]
    if len(hits) >= 30:
        return JSONResponse({"valid": None, "error": "trop de vérifications, réessayez plus tard", "vat": v}, status_code=429, headers=headers)
    hits.append(now); _VIES_HITS[ip] = hits
    if len(_VIES_HITS) > 5000:
        _VIES_HITS.clear()
    r = owx.vies_check(v)
    return JSONResponse({"valid": r.get("valid"), "name": r.get("name"), "error": r.get("error"), "vat": r.get("vat")}, headers=headers)


@router.post("/c/{code}/owine/commandes/{name}/export/facture-alix")
def owine_export_invoice_to_alix(request: Request, code: str, name: str):
    """Envoie à Alix la facture commerciale définitive (3 exemplaires) après la saisie des degrés et la réception des étiquettes."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); cs = service.cartons(o.id)
    if owx.unconfirmed_wines(o, cs):
        return _exp_redirect(code, name, "Des degrés restent à confirmer : saisissez la réponse d'Alix d'abord.")
    if not all(c.tracking for c in cs):
        return _exp_redirect(code, name, "Numéros de colis manquants : enregistrez les étiquettes d'abord.")
    pdf = docs.commercial_invoice_pdf(o, cs); meta = owx.store_invoice(o, pdf, by=_who(request), final=True)
    em = docs.final_invoice_email(o, cs)
    ok, m = gmail_imap.send_mail(CODE, em["to"], em["subject"], em["body"] + "\n\n" + _signature(), cc=em["cc"], attachments=[(f"Facture commerciale {meta['no']}.pdf", pdf, "application/pdf")],
                                 from_name="Jean-Sébastien CHEUNG-AH-SEUNG · oWine", reply_to=cfg.CONTACT_EMAIL)
    if not ok:
        return _exp_redirect(code, name, f"Envoi impossible : {m}")
    st = owx.get_state(o); st["final_invoice_sent_at"] = service.now_local().strftime("%d/%m/%Y %H:%M"); st.pop("final_invoice_pending", None); owx.set_state(o, st)
    service.close_tasks_by_key(f"final_invoice:{o.name}")
    return _exp_redirect(code, name, f"Facture définitive {meta['no']} (version {meta['version']}) envoyée à Alix.")


@router.post("/c/{code}/owine/commandes/{name}/export/refuser")
async def owine_export_refuse(request: Request, code: str, name: str):
    """Commande hors périmètre refusée : annulation Vaelan, tâche de remboursement Shopify, e-mail FR/EN au client."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    if o.status not in ("a_traiter", "cartons", "etiquettes"):
        return _exp_redirect(code, name, "Cette commande n'est plus au stade où l'on peut la refuser.")
    em = owx.refuse_order(o, by=_who(request), reason=(f.get("reason") or "").strip()[:200])
    msg = "Commande refusée et annulée dans Vaelan ; tâche « rembourser dans Shopify » créée."
    if o.email and f.get("send_email"):
        ok, m = gmail_imap.send_mail(CODE, [o.email], em["subject"], em["body"], cc=list(cfg.CLIENT_CC), from_name="Jean-Sébastien CHEUNG-AH-SEUNG · oWine", reply_to=cfg.CONTACT_EMAIL)
        msg += f" E-mail au client ({em['lang']}) : {'envoyé' if ok else 'non envoyé — ' + m}."
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg={msg}", status_code=303)


@router.post("/c/{code}/owine/commandes/{name}/export/vies")
def owine_export_vies(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    r = owx.vies_check(o.vat_number or "", force=True)
    return _exp_redirect(code, name, f"VIES : {'valide' if r.get('valid') else ('invalide' if r.get('valid') is False else 'indisponible')}" + (f" — {r.get('name')}" if r.get("name") else "") + (f" ({r.get('error')})" if r.get("error") else ""))


@router.get("/api/owine/rules")
def owine_api_rules(request: Request):
    """Règles d'expédition lues par l'extrait de thème du panier : zones ouvertes, limites, textes FR/EN (public, sans donnée personnelle)."""
    origin = request.headers.get("origin") or ""
    headers = {"Cache-Control": "public, max-age=300"}
    if origin in _VIES_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin; headers["Vary"] = "Origin"
    return JSONResponse(owx.public_rules(), headers=headers)
