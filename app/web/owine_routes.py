"""OWINE : tableau de bord, commandes (cartons → Chronopost → Alix / client → réception), stock, emballages, tâches, dépôt-vente LMB."""
import json
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
from app.packs.owine import config as cfg, service, docs, jobs as ow_jobs
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
    return templates.TemplateResponse(request, "owine_home.html", _base(request, company, orders=orders, tasks=tasks, pack=pack, runs=runs, msg=msg, today=date.today(),
                                                                       cost_missing=service.cost_alerts()))


@router.post("/c/{code}/owine/sync")
def owine_sync(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    start_job("owine_sync", ow_jobs.run_sync, company_id=company.id, pack="owine", label="OWINE — synchronisation Shopify et tâches Pennylane", user=current_user(request))
    return RedirectResponse(f"/c/{code}/owine?msg=Synchronisation lancée (quelques secondes).", status_code=303)


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
    proposal = service.propose_cartons(lines, st) if not cs else None
    sheet = docs.chronopost_sheet(o, cs) if cs else None
    initial = [{"ref": p["ref"], "box_sku": p["box_sku"], "lines": p["lines"]} for p in proposal] if proposal else \
              [{"ref": c.ref, "box_sku": c.box_sku or "2036", "lines": service.carton_lines(c)} for c in cs]
    editor = {"lines": [{"sku": l["sku"], "title": l["title"], "qty": int(l["qty"]), "cost": float(l.get("cost") or 0), "price": float(l.get("price") or 0)} for l in lines],
              "cartons": initial, "boxes": {k: v for k, v in cfg.PACKAGING.items() if v["bottles"]}, "bottle_kg": cfg.BOTTLE_KG}
    return _base(request, company, o=o, lines=lines, cartons=cs, carton_lines=service.carton_lines, proposal=proposal, sheet=sheet, msg=msg, editor=json.dumps(editor, ensure_ascii=False),
                 missing=docs.missing_vars(o, cs) if cs else [],
                 email_alix=docs.email_alix(o, cs) if cs else None, email_client=docs.email_client(o, cs) if cs else None,
                 gmail_ok=gmail_imap.configured(CODE), tasks=[t for t in service.tasks("open") if t.ref == o.name], moves=service.moves(ref=o.name))


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
        plan = service.propose_cartons(lines, st)
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
    """PDF d'étiquettes stockés en base (Setting) pour les documents et brouillons."""
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
            "client": {"to": [ed.get("to_client") or (ec["to"][0] if ec["to"] else "")], "cc": [], "subject": ed.get("subject_client") or ec["subject"],
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
    return templates.TemplateResponse(request, "owine_emails.html", _base(request, company, o=o, em=em, ed=ed, msg=msg, gmail_ok=gmail_imap.configured(CODE), labels=[n for n, _ in labels],
                                                                         att_alix=att_alix, att_client=att_client, sent=o.status in ("envoye", "attente_reception", "cloturee")))


@router.post("/c/{code}/owine/commandes/{name}/emails")
async def owine_order_emails_save(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); f = await request.form()
    ed = _email_edits(o)
    for k in ("subject_alix", "subject_client", "extra_alix", "extra_client", "cc_alix", "to_client"):
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
    ok1, m1 = gmail_imap.send_mail(CODE, em["alix"]["to"], em["alix"]["subject"], em["alix"]["text"], cc=em["alix"]["cc"], attachments=att_alix, html=em["alix"]["html"], inline=logo,
                                   from_name="Jean-Sébastien CHEUNG-AH-SEUNG · oWine", reply_to=cfg.CONTACT_EMAIL)
    ok2, m2 = gmail_imap.send_mail(CODE, em["client"]["to"], em["client"]["subject"], em["client"]["text"], attachments=att_cli, html=em["client"]["html"], inline=logo,
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


@router.post("/c/{code}/owine/commandes/{name}/brouillons")
def owine_order_drafts(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name); cs = service.cartons(o.id)
    pl = docs.packing_list_pdf(o, cs); xl = docs.alix_xlsx(o, cs); labels = _labels(o)
    ea, ec = docs.email_alix(o, cs), docs.email_client(o, cs)
    logo = [("logo", open(docs.LOGO, "rb").read(), "image/png")]
    att_alix = [(f"Détail {o.name}.pdf", pl, "application/pdf"), (f"{o.name}.xlsx", xl, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")] + [(n, d, "application/pdf") for n, d in labels]
    ok1, m1 = gmail_imap.create_draft(CODE, ea["to"], ea["subject"], ea["body"] + "\n\n" + _signature(), cc=ea["cc"], attachments=att_alix, html=docs.email_alix_html(o, cs), inline=logo)
    att_cli = [(f"Détail {o.name}.pdf", pl, "application/pdf")] + [(n, d, "application/pdf") for n, d in labels]
    ok2, m2 = gmail_imap.create_draft(CODE, ec["to"], ec["subject"], ec["body"] + "\n\n" + _signature(), attachments=att_cli, html=docs.email_client_html(o, cs), inline=logo)
    return RedirectResponse(f"/c/{code}/owine/commandes/{name}?msg=Brouillon Alix : {m1} · brouillon client : {m2}", status_code=303)


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


@router.post("/c/{code}/owine/commandes/{name}/cloturer")
def owine_order_close(request: Request, code: str, name: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    o = service.get_order(name)
    service.close_order(o, by=_who(request))
    return RedirectResponse(f"/c/{code}/owine/commandes?", status_code=303)


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
def owine_lmb(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    imap = service.item_map()
    dep = defaultdict(lambda: {"in": 0.0, "sold": 0.0, "price": None, "title": ""})
    sales = defaultdict(lambda: defaultdict(float))                       # commande → sku → qté (propriétaire LMB)
    blvs = defaultdict(lambda: {"date": None, "qty": 0, "ht": 0.0})
    for m in service.moves(owner="LMB", limit=5000):
        if m.kind == "deposit_in":
            dep[m.sku]["in"] += m.qty; dep[m.sku]["price"] = m.unit_cost
            blvs[m.ref]["date"] = m.date; blvs[m.ref]["qty"] += m.qty; blvs[m.ref]["ht"] += m.qty * (m.unit_cost or 0)
        elif m.kind in ("sale", "pickup") and m.location == "ALIX":
            dep[m.sku]["sold"] += -m.qty
            sales[m.ref][m.sku] += -m.qty
    for sku, d in dep.items():
        it = imap.get(sku); d["title"] = it.title if it else sku
        d["price"] = d["price"] or (it.lmb_price if it else None)
    open_inv = {t.ref for t in service.tasks("open") if t.kind == "lmb_invoice"}
    to_invoice = []
    for ref, skus in sorted(sales.items()):
        tot = sum(q * (dep[s]["price"] or 0) for s, q in skus.items())
        to_invoice.append({"ref": ref, "lines": [(s, q, dep[s]["title"], dep[s]["price"]) for s, q in skus.items()], "ht": tot, "open": ref in open_inv})
    return templates.TemplateResponse(request, "owine_lmb.html", _base(request, company, dep=sorted(dep.items(), key=lambda kv: kv[1]["title"]), blvs=sorted(blvs.items()),
                                                                      to_invoice=to_invoice, total_to_invoice=sum(x["ht"] for x in to_invoice if x["open"])))
