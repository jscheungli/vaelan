"""VDS — routes du check-in voyageurs : formulaire PUBLIC (/checkin/<token>) et back-office (/c/VDS/checkin)."""
import json
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app.core.db import engine
from app.core.jobs import start_job
from app.core import mailer
from app.core.connectors import lodgify
from app.core.security import current_user
from app.models import VdsReservation, VdsResponse, VdsFile, Run
from app.packs.vds import config as vcfg, service, jobs as vjobs
from app.packs.vds.texts import t, T
from app.web.routes import templates, _ctx, _company_or_redirect, _watch_fragment

router = APIRouter()
CODE = vcfg.COMPANY_CODE
STATUS_LABEL = {"pending": ("À inviter", "secondary"), "sent": ("Invité", "info"), "reminded": ("Relancé", "warning"),
                "completed": ("Complété", "success"), "refused": ("Règles refusées", "danger"), "cancelled": ("Annulée", "dark")}


def _lang(request: Request, res: Optional[VdsReservation] = None) -> str:
    q = request.query_params.get("lang")
    if q in ("fr", "en"):
        return q
    if res and res.lang in ("fr", "en"):
        return res.lang                     # langue de la réservation (Lodgify / admin), bascule explicite via ?lang=
    al = (request.headers.get("accept-language") or "").lower()
    return "en" if al.startswith("en") else "fr"


def _public_ctx(request: Request, res: Optional[VdsReservation], lang: str, **extra):
    ch = vcfg.CHANNELS.get(res.channel if res else "lodgify", vcfg.CHANNELS["lodgify"])
    base = {"lang": lang, "T": T.get(lang, T["fr"]), "t": lambda k, **kw: t(lang, k, **kw), "res": res, "ch": ch,
            "villa": vcfg.VILLA, "rules": vcfg.RULES, "marketing": vcfg.MARKETING, "fmt_date": service.fmt_date,
            "other_lang": "en" if lang == "fr" else "fr"}
    base.update(extra)
    return base


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or ""


def _more_texts(channel: str, lang: str) -> dict:
    """Encarts « en savoir plus » (question + réponse) par règle, selon le canal."""
    out = {}
    platform = vcfg.CHANNELS[channel]["platform"]
    for r in vcfg.RULES:
        q = r.get(f"more_q_{lang}") or r.get("more_q_fr")
        if not q:
            continue
        import html as _h
        site = vcfg.VILLA["site"]
        link = f'<a href="{site}" target="_blank" rel="noopener">{_h.escape(site.replace("https://", ""))}</a>'
        if platform and r.get(f"more_platform_{lang}"):
            txt = _h.escape(r[f"more_platform_{lang}"]).replace("{platform}", _h.escape(vcfg.CHANNELS[channel]["label"])).replace("{site_link}", link)
        else:
            txt = _h.escape(r.get(f"more_{lang}") or r.get("more_fr")).replace("{site_link}", link)
        out[r["key"]] = {"q": q, "txt": txt}      # txt = HTML sûr (échappé + lien site)
    return out


# ============================== PUBLIC ==============================
@router.get("/checkin/{token}", response_class=HTMLResponse)
def public_form(request: Request, token: str):
    res = service.by_token(token)
    lang = _lang(request, res)
    if not res or res.status == "cancelled":
        return templates.TemplateResponse(request, "vds_message.html",
                                          _public_ctx(request, None, lang, title=t(lang, "invalid_title"), body=t(lang, "invalid_body")), status_code=404)
    if res.status == "completed":
        resp = service.latest_response(res.id)
        if request.query_params.get("done"):
            email = (resp.email if resp else None) or res.guest_email
            return templates.TemplateResponse(request, "vds_message.html",
                                              _public_ctx(request, res, lang, title=t(lang, "done_title"),
                                                          body=t(lang, "done_body", email=(f" ({email})" if email else "")), ok=True))
        return templates.TemplateResponse(request, "vds_message.html",
                                          _public_ctx(request, res, lang, title=t(lang, "already_title"),
                                                      body=t(lang, "already_body", date=service.fmt_dt(resp.submitted_at if resp else None, lang))))
    if res.status == "refused":
        return templates.TemplateResponse(request, "vds_message.html",
                                          _public_ctx(request, res, lang, title=t(lang, "refused_done_title"), body=t(lang, "refused_done_body")))
    if res.lang != lang and request.query_params.get("lang") in ("fr", "en"):
        service.update_reservation(res.id, lang=lang)      # choix explicite du voyageur (bascule de langue)
    listing = t(lang, f"listing_{res.channel}")
    intro = t(lang, "rules_intro_platform", listing=listing) if vcfg.CHANNELS[res.channel]["platform"] else t(lang, "rules_intro_direct")
    max_birth = (date.today() - timedelta(days=18 * 365 + 5)).isoformat()
    return templates.TemplateResponse(request, "vds_form.html",
                                      _public_ctx(request, res, lang, rules_intro=intro, max_birth=max_birth,
                                                  max_mb=service.params().get("max_upload_mb", 12),
                                                  rule_txt={r["key"]: (r.get(lang) or r["fr"]) for r in vcfg.RULES},
                                                  more=_more_texts(res.channel, lang)))


@router.post("/checkin/{token}", response_class=HTMLResponse)
async def public_submit(request: Request, token: str):
    res = service.by_token(token)
    lang = _lang(request, res)
    if not res or res.status in ("cancelled", "completed"):
        return RedirectResponse(f"/checkin/{token}?lang={lang}", status_code=303)
    form = await request.form()
    fields = {k: v for k, v in form.multi_items() if not hasattr(v, "filename")}
    lang = "en" if fields.get("lang") == "en" else "fr"
    # règles : toutes doivent être « yes » (sinon la voie « refus » est dédiée)
    missing = [r["key"] for r in vcfg.RULES if fields.get(f"rule_{r['key']}") != "yes"]
    if missing:
        return RedirectResponse(f"/checkin/{token}?lang={lang}&err=rules", status_code=303)
    sig = fields.get("signature") or ""
    if not sig.startswith("data:image/png;base64,"):
        return RedirectResponse(f"/checkin/{token}?lang={lang}&err=sign", status_code=303)
    import base64
    try:
        sig_png = base64.b64decode(sig.split(",", 1)[1])
    except Exception:
        return RedirectResponse(f"/checkin/{token}?lang={lang}&err=sign", status_code=303)
    max_bytes = int(service.params().get("max_upload_mb", 12)) * 1024 * 1024
    uploads = []
    for k, v in form.multi_items():
        if hasattr(v, "filename") and v.filename:
            blob = await v.read()
            if not blob or len(blob) > max_bytes:
                continue
            uploads.append((v.filename, blob, v.content_type or "application/octet-stream"))
        if len(uploads) >= 3:
            break
    if vcfg.CHANNELS[res.channel]["id_required"] and not uploads:
        return RedirectResponse(f"/checkin/{token}?lang={lang}&err=id", status_code=303)
    service.save_response(res, fields, uploads, _ip(request), request.headers.get("user-agent", ""), sig_png)
    return RedirectResponse(f"/checkin/{token}?lang={lang}&done=1", status_code=303)


@router.post("/checkin/{token}/refus", response_class=HTMLResponse)
async def public_refuse(request: Request, token: str):
    res = service.by_token(token)
    if not res or res.status in ("cancelled", "completed"):
        return RedirectResponse(f"/checkin/{token}", status_code=303)
    form = await request.form()
    lang = "en" if form.get("lang") == "en" else "fr"
    refused = [r["key"] for r in vcfg.RULES if form.get(f"rule_{r['key']}") == "no"]
    service.save_refusal(res, refused, lang, _ip(request))
    return RedirectResponse(f"/checkin/{token}?lang={lang}", status_code=303)


@router.get("/checkin/nouveau/{channel}", response_class=HTMLResponse)
def public_new_form(request: Request, channel: str):
    """Point d'entrée générique par canal (lien / QR sans jeton) : le voyageur identifie sa réservation,
    puis est redirigé vers son formulaire personnel."""
    if channel not in vcfg.CHANNELS:
        return RedirectResponse("/", status_code=303)
    lang = _lang(request)
    return templates.TemplateResponse(request, "vds_new.html", _public_ctx(request, None, lang, channel=channel,
                                                                              ch=vcfg.CHANNELS[channel], min_arrival=date.today().isoformat()))


@router.post("/checkin/nouveau/{channel}")
async def public_new_submit(request: Request, channel: str):
    if channel not in vcfg.CHANNELS:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    lang = "en" if form.get("lang") == "en" else "fr"
    if form.get("website"):                     # pot de miel anti-robots
        return RedirectResponse("/", status_code=303)
    ref = (form.get("booking_ref") or "").strip().upper().replace("#", "")
    with Session(engine) as s:
        existing = s.exec(select(VdsReservation).where(VdsReservation.booking_ref == ref,
                                                       VdsReservation.status.in_(["pending", "sent", "reminded"]))).first() if ref else None
    if existing:
        return RedirectResponse(f"/checkin/{existing.token}?lang={lang}", status_code=303)
    res = service.create_reservation(channel, booking_ref=ref, guest_name=form.get("guest_name"), guest_email=form.get("guest_email"),
                                     arrival=form.get("arrival") or None, lang=lang, source="link")
    return RedirectResponse(f"/checkin/{res.token}?lang={lang}", status_code=303)


# ============================== BACK-OFFICE ==============================
def _guard(request: Request, code: str):
    return _company_or_redirect(request, code, feature="checkin")


@router.get("/c/{code}/checkin", response_class=HTMLResponse)
def admin_list(request: Request, code: str, view: str = "avenir"):
    company, redir = _guard(request, code)
    if redir:
        return redir
    today = service.now_local().date()
    with Session(engine) as s:
        q = select(VdsReservation)
        if view == "avenir":
            q = q.where(VdsReservation.status != "cancelled", (VdsReservation.arrival >= today - timedelta(days=1)) | (VdsReservation.arrival == None))  # noqa: E711
        elif view == "incomplets":
            q = q.where(VdsReservation.status.in_(["pending", "sent", "reminded", "refused"]), VdsReservation.arrival >= today - timedelta(days=1))
        elif view == "passes":
            q = q.where(VdsReservation.arrival < today)
        rs = list(s.exec(q).all())
        runs = s.exec(select(Run).where(Run.company_id == company.id, Run.kind.in_(["vds_sync", "vds_reminders", "vds_daily"]))
                      .order_by(Run.id.desc()).limit(6)).all()
    rs.sort(key=lambda r: (r.arrival or date.max, r.id), reverse=(view == "passes"))
    with Session(engine) as s:
        answered = {(x[0] if isinstance(x, (tuple, list)) else x) for x in s.exec(select(VdsResponse.reservation_id)).all()}
    counts = {"a_inviter": sum(1 for r in rs if r.status == "pending"), "incomplets": sum(1 for r in rs if r.status in ("sent", "reminded")),
              "completes": sum(1 for r in rs if r.status == "completed"), "refus": sum(1 for r in rs if r.status == "refused")}
    p = service.params()
    warns = []
    if p.get("beta"):
        warns.append(f"MODE BÊTA : tous les emails voyageurs (invitations, relances, confirmations, état des risques) sont redirigés vers {p.get('test_email')} — rien ne part aux clients. À désactiver dans la configuration après validation.")
    if not mailer.configured():
        warns.append("Envoi d'emails non configuré (variables SMTP_HOST / SMTP_FROM / SMTP_USER / SMTP_PASSWORD sur Render) : les invitations ne partent pas, copiez les liens.")
    if not p.get("alert_emails"):
        warns.append("Aucun destinataire d'alertes internes : renseignez-le dans la configuration.")
    if not lodgify.for_company(CODE):
        warns.append("Clé Lodgify absente (LODGIFY_VDS_APIKEY) : la synchro automatique des réservations est inactive.")
    return templates.TemplateResponse(request, "vds_checkin.html",
                                      _ctx(request, company=company, rs=rs, view=view, counts=counts, runs=runs, warns=warns,
                                           labels=STATUS_LABEL, channels=vcfg.CHANNELS, today=today, base=service.base_url(),
                                           smtp=mailer.configured(), fmt_dt=service.fmt_dt, answered=answered))


@router.post("/c/{code}/checkin/new")
def admin_new(request: Request, code: str, channel: str = Form(...), booking_ref: str = Form(""), guest_name: str = Form(""),
              guest_email: str = Form(""), guest_phone: str = Form(""), arrival: str = Form(""), departure: str = Form(""),
              guests: str = Form(""), lang: str = Form("fr"), invite: str = Form("")):
    company, redir = _guard(request, code)
    if redir:
        return redir
    res = service.create_reservation(channel, booking_ref=booking_ref, guest_name=guest_name, guest_email=guest_email,
                                     guest_phone=guest_phone, arrival=arrival or None, departure=departure or None,
                                     guests=int(guests) if guests.isdigit() else None, lang=lang, source="manual")
    if invite and res.guest_email:
        service.send_invitation(res)
    return RedirectResponse(f"/c/{code}/checkin/{res.id}", status_code=303)


@router.get("/c/{code}/checkin/config", response_class=HTMLResponse)
def admin_config(request: Request, code: str, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    return templates.TemplateResponse(request, "vds_config.html",
                                      _ctx(request, company=company, p=service.params(), msgs=service.recent_messages(20),
                                           smtp=mailer.configured(), smtp_from=mailer.sender(), lodgify_ok=bool(lodgify.for_company(CODE)),
                                           msg=msg, base=service.base_url(), channels=vcfg.CHANNELS))


@router.post("/c/{code}/checkin/config")
async def admin_config_save(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    form = await request.form()
    before = service.params()
    vals = {"alert_emails": (form.get("alert_emails") or "").strip(), "reply_to": (form.get("reply_to") or "").strip(),
            "from_email": (form.get("from_email") or "").strip(),
            "auto_invite": bool(form.get("auto_invite")), "beta": bool(form.get("beta")),
            "test_email": (form.get("test_email") or "").strip()}
    if vals["beta"] and not vals["test_email"]:
        vals["test_email"] = before.get("test_email") or "jscheungli@gmail.com"
    vals["base_url"] = (form.get("base_url") or "").strip().rstrip("/")
    for k in ("reminder_days", "max_reminders", "purge_id_days", "max_upload_mb"):
        v = (form.get(k) or "").strip()
        if v.isdigit():
            vals[k] = int(v)
    for k in ("pre_arrival_days", "alert_days"):
        v = [int(x) for x in (form.get(k) or "").replace(";", ",").split(",") if x.strip().isdigit()]
        vals[k] = v
    service.save_params(vals)
    msg = "Réglages enregistrés."
    if before.get("beta") and not vals["beta"]:
        # sortie de bêta : les invitations/relances envoyées à l'adresse de test n'ont jamais atteint les
        # voyageurs -> on remet ces réservations « à inviter » pour que le vrai cycle reparte
        n = 0
        with Session(engine) as s:
            for r in s.exec(select(VdsReservation).where(VdsReservation.status.in_(["sent", "reminded"]))).all():
                r.status, r.invited_at, r.reminded_at, r.reminder_count, r.alerted_at = "pending", None, None, 0, None
                s.add(r)
                n += 1
            s.commit()
        msg += f" Sortie du mode bêta : {n} réservation(s) remise(s) « à inviter »."
    return RedirectResponse(f"/c/{code}/checkin/config?msg={msg}", status_code=303)


@router.post("/c/{code}/checkin/config/test-mail")
def admin_test_mail(request: Request, code: str):
    """Email de test vers l'adresse de test (bêta) ou les alertes : valide la configuration SMTP."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    p = service.params()
    to = (p.get("test_email") or "").strip() or service.alert_emails()[:1]
    to = [to] if isinstance(to, str) else to
    if not to:
        return RedirectResponse(f"/c/{code}/checkin/config?msg=Aucune adresse de test ni d'alerte renseignée.", status_code=303)
    subject = "Email de test — formulaire d'arrivée La Villa des Sables du Lagon"
    body = (f"Bonjour,\n\nCeci est un email de test envoyé par Vaelan le {service.now_local():%d/%m/%Y à %H:%M} (heure de La Réunion).\n\n"
            f"Expéditeur : {mailer.branded_from(service.brand())}\nRéponse vers : {p.get('reply_to') or '— (non renseigné)'}\n\n"
            f"Si vous le recevez, la configuration SMTP est opérationnelle.")
    ok, info = mailer.send_branded(to, subject, body, service.brand(), lang="fr")
    service.log_message(None, "test", ", ".join(to), subject, body, ok, info, sender=mailer.branded_from(service.brand()))
    return RedirectResponse(f"/c/{code}/checkin/config?msg={'✅ Email de test envoyé à ' + ', '.join(to) if ok else '❌ Échec : ' + info}", status_code=303)


@router.post("/c/{code}/checkin/import")
async def admin_import(request: Request, code: str, channel: str = Form(...), file: UploadFile = File(...)):
    company, redir = _guard(request, code)
    if redir:
        return redir
    blob = await file.read()
    created, skipped = service.import_surveysparrow(blob, channel)
    return RedirectResponse(f"/c/{code}/checkin/config?msg=Import {vcfg.CHANNELS[channel]['label']} : {created} réponses importées, {skipped} déjà présentes.", status_code=303)


@router.post("/c/{code}/checkin/sync")
def admin_sync(request: Request, code: str, invite: str = Form("")):
    company, redir = _guard(request, code)
    if redir:
        return redir
    run_id = start_job("vds_sync", lambda ctx: vjobs.run_lodgify_sync(ctx, invite=bool(invite)), company_id=company.id,
                       pack="vds", label="Synchro Lodgify" + (" + invitations" if invite else " (sans invitation)"), user=current_user(request))
    if request.headers.get("HX-Request"):
        return _watch_fragment(run_id)
    return RedirectResponse(f"/c/{code}/checkin", status_code=303)


@router.post("/c/{code}/checkin/reminders")
def admin_reminders(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    run_id = start_job("vds_reminders", vjobs.run_reminders, company_id=company.id, pack="vds",
                       label="Relances / alertes check-in", user=current_user(request))
    if request.headers.get("HX-Request"):
        return _watch_fragment(run_id)
    return RedirectResponse(f"/c/{code}/checkin", status_code=303)


@router.get("/c/{code}/checkin/export.xlsx")
def admin_export(request: Request, code: str):
    company, redir = _guard(request, code)
    if redir:
        return redir
    data = service.excel_export()
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{datetime.utcnow():%Y%m%d} checkin VDS.xlsx"'})


@router.get("/c/{code}/checkin/file/{fid}")
def admin_file(request: Request, code: str, fid: int):
    company, redir = _guard(request, code)
    if redir:
        return redir
    with Session(engine) as s:
        f = s.get(VdsFile, fid)
    if not f:
        return RedirectResponse(f"/c/{code}/checkin", status_code=303)
    disp = "inline" if f.content_type in ("image/jpeg", "image/png", "application/pdf") else "attachment"
    return Response(content=f.data, media_type=f.content_type, headers={"Content-Disposition": f'{disp}; filename="{f.name}"'})


@router.get("/c/{code}/checkin/message/{mid}", response_class=HTMLResponse)
def admin_message(request: Request, code: str, mid: int):
    """Détail d'un envoi : destinataires, expéditeur, sujet, corps texte et aperçu HTML tel qu'envoyé."""
    from app.models import VdsMessage
    company, redir = _guard(request, code)
    if redir:
        return redir
    with Session(engine) as s:
        m = s.get(VdsMessage, mid)
        res = s.get(VdsReservation, m.reservation_id) if (m and m.reservation_id) else None
    if not m:
        return RedirectResponse(f"/c/{code}/checkin", status_code=303)
    lang = (res.lang if res and res.lang in ("fr", "en") else "fr")
    html = mailer.branded_html(m.body or "", service.brand(), lang) if m.body else ""
    return templates.TemplateResponse(request, "vds_message_view.html",
                                      _ctx(request, company=company, m=m, res=res, html=html, fmt_dt=service.fmt_dt))


@router.get("/c/{code}/checkin/{rid}/attestation.pdf")
def admin_attestation(request: Request, code: str, rid: int):
    """Attestation signée (PDF) générée à la demande depuis la dernière réponse — y compris pour les réponses importées."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    with Session(engine) as s:
        res = s.get(VdsReservation, rid)
    resp = service.latest_response(rid) if res else None
    if not res or not resp:
        return RedirectResponse(f"/c/{code}/checkin/{rid}?msg=Pas de réponse : attestation impossible.", status_code=303)
    sig = next((f for f in service.files_for(rid, "signature") if f.response_id == resp.id), None)
    pdf = service.build_recap_pdf(res, resp, sig.data if sig else None)
    name = f"Attestation {res.booking_ref or res.id} - {resp.full_name or res.guest_name or ''}.pdf".replace('"', "")
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.post("/c/{code}/checkin/{rid}/manual")
def admin_manual(request: Request, code: str, rid: int, channel: str = Form("sms"), text: str = Form("")):
    """Journalise une relance / invitation envoyée à la main (SMS, WhatsApp, messagerie…) avec son texte et son auteur."""
    company, redir = _guard(request, code)
    if redir:
        return redir
    with Session(engine) as s:
        res = s.get(VdsReservation, rid)
    if not res or not text.strip():
        return RedirectResponse(f"/c/{code}/checkin/{rid}?msg=Texte vide : rien d'enregistré.", status_code=303)
    u = current_user(request)
    who = (u.name or u.email) if u else "?"
    service.log_manual(res, channel if channel in ("sms", "whatsapp", "airbnb", "booking", "abritel", "email", "telephone") else "autre", text.strip(), who)
    return RedirectResponse(f"/c/{code}/checkin/{rid}?msg=Relance {channel} enregistrée (par {who}).", status_code=303)


@router.get("/c/{code}/checkin/{rid}", response_class=HTMLResponse)
def admin_detail(request: Request, code: str, rid: int, msg: str = ""):
    company, redir = _guard(request, code)
    if redir:
        return redir
    with Session(engine) as s:
        res = s.get(VdsReservation, rid)
        if not res:
            return RedirectResponse(f"/c/{code}/checkin", status_code=303)
        resps = list(s.exec(select(VdsResponse).where(VdsResponse.reservation_id == rid).order_by(VdsResponse.id.desc())).all())
    resp = resps[0] if resps else None
    data = json.loads(resp.data or "{}") if resp else {}
    files = service.files_for(rid)
    msgs = service.messages_for(rid)
    lang = res.lang if res.lang in ("fr", "en") else "fr"
    invite_text = t(lang, "mail_invite_body", **service._mail_vars(res)).replace("Bonjour ,", "Bonjour,").replace("Hello ,", "Hello,")
    return templates.TemplateResponse(request, "vds_reservation.html",
                                      _ctx(request, company=company, res=res, resp=resp, data=data, files=files, msgs=msgs,
                                           labels=STATUS_LABEL, channels=vcfg.CHANNELS, rules=vcfg.RULES, marketing=dict((m[0], m[1]) for m in vcfg.MARKETING),
                                           url=service.public_url(res), invite_text=invite_text, msg=msg, smtp=mailer.configured(),
                                           fmt_dt=service.fmt_dt, fmt_date=service.fmt_date, suggested=service.suggested_texts(res),
                                           rule_rows=service.rule_rows(data, "fr") if resp else []))


@router.post("/c/{code}/checkin/{rid}/action")
def admin_action(request: Request, code: str, rid: int, action: str = Form(...)):
    company, redir = _guard(request, code)
    if redir:
        return redir
    with Session(engine) as s:
        res = s.get(VdsReservation, rid)
    if not res:
        return RedirectResponse(f"/c/{code}/checkin", status_code=303)
    msg = ""
    if action == "invite":
        ok, info = service.send_invitation(res)
        msg = f"Invitation : {info}"
    elif action == "remind":
        ok, info = service.send_invitation(res, reminder=True)
        msg = f"Relance : {info}"
    elif action == "cancel":
        service.update_reservation(rid, status="cancelled")
        msg = "Réservation annulée (lien désactivé)."
    elif action == "reopen":
        service.update_reservation(rid, status="pending")
        msg = "Réservation réouverte (formulaire à nouveau accessible)."
    elif action == "mark_sent":
        service.update_reservation(rid, status="sent", invited_at=datetime.utcnow())
        msg = "Marquée « invitée » (lien envoyé manuellement)."
    elif action == "delete_ids":
        with Session(engine) as s:
            for f in s.exec(select(VdsFile).where(VdsFile.reservation_id == rid, VdsFile.kind == "id_document")).all():
                s.delete(f)
            s.commit()
        msg = "Pièces d'identité supprimées."
    return RedirectResponse(f"/c/{code}/checkin/{rid}?msg={msg}", status_code=303)
