"""OWINE — documents d'une commande : liste de colisage (PDF), fichier de préparation Alix (xlsx), fiche de saisie Chronopost,
lecture des étiquettes Chronopost (n° de colis ↔ référence carton), textes des e-mails (Alix, client)."""
import io
import json
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import fitz

from . import config, service

DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def fr_date(d: date) -> str:
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def next_business_day(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def delivery_of(pickup: date) -> date:
    """Règle JS : Chrono Viti livre le lendemain de l'enlèvement avant 13 h (le lundi si l'enlèvement est le samedi)."""
    n = pickup + timedelta(days=1)
    if n.weekday() == 6:
        n += timedelta(days=1)
    return n


def lab(n: int) -> str:
    """« l'étiquette d'envoi » / « les étiquettes d'envoi » selon le nombre de cartons."""
    return "l'étiquette d'envoi" if n <= 1 else "les étiquettes d'envoi"


def missing_vars(o, cs) -> List[str]:
    """Ce qu'il faut renseigner avant de générer les e-mails."""
    miss = []
    if not cs:
        miss.append("cartons validés")
    if not o.pickup_date:
        miss.append("date de retrait" if o.mode == "retrait" else "date d'enlèvement")
    if o.mode == "chronopost":
        if not o.pickup_no:
            miss.append("numéro d'enlèvement Chronopost")
        if not o.pickup_slot:
            miss.append("créneau d'enlèvement")
        if any(not c.tracking for c in cs):
            miss.append("numéro Chronopost de chaque carton (étiquettes)")
        if not (o.address1 and o.zip and o.city):
            miss.append("adresse de livraison")
    if not o.email:
        miss.append("e-mail du client")
    if not o.customer:
        miss.append("nom du client")
    from . import export as _x
    if o.mode == "chronopost" and _x.zone(o.country) != "FR":
        exp = _x.state(o, cs)
        for c in exp["blocking"]:
            miss.append(f"export : {c['label'].lower()}")
        if exp["customs"] and not (exp["invoice"] or {}).get("final"):
            miss.append("facture commerciale (3 exemplaires) à générer après les étiquettes")
    return miss


# ---------------------------------------------------------------- liste de colisage (document client, aussi utilisé par Alix)
import os
LOGO = os.path.join(os.path.dirname(__file__), "assets", "logo.png")
WINE = (0.55, 0.10, 0.10)      # rouge oWine
DARK = (0.29, 0.05, 0.12)      # bordeaux foncé du logo
GREY = (0.45, 0.45, 0.45)
LIGHT = (0.96, 0.94, 0.94)


def packing_list_pdf(o, cs) -> bytes:
    """Liste de colisage : logo oWine, commande, destinataire, un bloc par carton (référence, n° Chronopost, contenu), totaux."""
    doc = fitz.open()
    W, H = 595, 842
    M = 48

    def new_page(first=False):
        pg = doc.new_page(width=W, height=H)
        if first:
            try:
                pg.insert_image(fitz.Rect(M, 34, M + 160, 34 + 56), filename=LOGO, keep_proportion=True)
            except Exception:
                pg.insert_text((M, 60), "oWine", fontname="tibo", fontsize=22, color=WINE)
            pg.insert_text((W - M - fitz.get_text_length("LISTE DE COLISAGE", fontname="hebo", fontsize=15), 56), "LISTE DE COLISAGE", fontname="hebo", fontsize=15, color=DARK)
            sub = f"Commande {o.name} · {service.now_local():%d/%m/%Y}"
            pg.insert_text((W - M - fitz.get_text_length(sub, fontname="helv", fontsize=9.5), 72), sub, fontname="helv", fontsize=9.5, color=GREY)
            pg.draw_line((M, 100), (W - M, 100), color=WINE, width=1.2)
        else:
            pg.insert_text((M, 40), f"Liste de colisage · commande {o.name} (suite)", fontname="helv", fontsize=9, color=GREY)
        motto = f"« {config.MOTTO} »"
        pg.insert_text((W / 2 - fitz.get_text_length(motto, fontname="tiit", fontsize=11) / 2, H - 52), motto, fontname="tiit", fontsize=11, color=WINE)
        pg.draw_line((W / 2 - 28, H - 44), (W / 2 + 28, H - 44), color=WINE, width=0.6)
        foot = f"oWine SAS · Parc d'activité, 14 E rue Coubertin, 21000 Dijon · www.owine.co · {config.CONTACT_EMAIL}"
        pg.insert_text((W / 2 - fitz.get_text_length(foot, fontname="helv", fontsize=8) / 2, H - 30), foot, fontname="helv", fontsize=8, color=GREY)
        return pg

    pg = new_page(first=True)
    y = 122
    pg.insert_text((M, y), "DESTINATAIRE", fontname="hebo", fontsize=8.5, color=WINE)
    addr = [x for x in [o.customer, o.company, o.address1, o.address2, f"{o.zip or ''} {o.city or ''}".strip()] if x]
    if o.mode == "retrait":
        addr = [o.customer or ""] + ["Retrait à l'entrepôt oWine chez Alix Transport, Beaune"]
    yy = y + 14
    for i, line in enumerate(addr):
        pg.insert_text((M, yy), line, fontname="hebo" if i == 0 else "helv", fontsize=10 if i == 0 else 9.5, color=DARK if i == 0 else (0.15, 0.15, 0.15)); yy += 13
    x2 = W / 2 + 10
    pg.insert_text((x2, y), "EXPÉDITION", fontname="hebo", fontsize=8.5, color=WINE)
    total_b = sum(sum(int(l["qty"]) for l in service.carton_lines(c)) for c in cs)
    info = [f"{len(cs)} carton{'s' if len(cs) > 1 else ''} · {total_b} bouteille{'s' if total_b > 1 else ''}"]
    intl = o.mode == "chronopost" and _export.zone(o.country) != "FR"
    if o.mode == "chronopost" and intl:
        exp = _export.state(o, cs)
        info.append(f"Transporteur : Chronopost {exp['product_label'].split(' ·')[0]} — {exp['country_name']}")
        if o.pickup_date:
            info.append(f"Enlèvement le {fr_date(o.pickup_date)}")
        info.append(f"Livraison estimée : {exp['delay'] or 'selon zoning Chronopost'}")
        if exp["customs"]:
            info.append(f"Incoterm {exp['incoterm']} · facture commerciale {_export.invoice_number(o)} (3 ex. sur le colis A)")
    elif o.mode == "chronopost":
        info.append("Transporteur : Chronopost (Chrono Viti)")
        if o.pickup_date:
            info.append(f"Enlèvement le {fr_date(o.pickup_date)}")
        if o.delivery_date:
            info.append(f"Livraison prévue le {fr_date(o.delivery_date)}")
    else:
        info.append("Retrait sur place" + (f" à partir du {fr_date(o.pickup_date)}" if o.pickup_date else ""))
    yy2 = y + 14
    for line in info:
        pg.insert_text((x2, yy2), line, fontname="helv", fontsize=9.5, color=(0.15, 0.15, 0.15)); yy2 += 13
    y = max(yy, yy2) + 16
    for c in cs:
        lines = service.carton_lines(c); nb = sum(int(l["qty"]) for l in lines)
        if y + 36 + 15 * len(lines) > H - 60:
            pg = new_page(); y = 60
        pg.draw_rect(fitz.Rect(M, y, W - M, y + 20), color=None, fill=LIGHT)
        pg.draw_rect(fitz.Rect(M, y, M + 22, y + 20), color=None, fill=DARK)
        pg.insert_text((M + 7, y + 14.5), c.ref, fontname="hebo", fontsize=11, color=(1, 1, 1))
        pos = f" · colis {cs.index(c) + 1}/{len(cs)}" if intl and len(cs) > 1 else ""
        pg.insert_text((M + 30, y + 14), f"Carton {c.ref} · {nb} bouteille{'s' if nb > 1 else ''}{pos}", fontname="hebo", fontsize=9.5, color=DARK)
        if c.tracking:
            t = f"N° Chronopost {c.tracking}"
            pg.insert_text((W - M - 6 - fitz.get_text_length(t, fontname="helv", fontsize=9), y + 14), t, fontname="helv", fontsize=9, color=GREY)
        y += 20
        for l in lines:
            y += 14
            title = l.get("title") or l["sku"]
            fs = 9.5
            while fitz.get_text_length(title, fontname="helv", fontsize=fs) > W - 2 * M - 60 and fs > 7:
                fs -= 0.5
            pg.insert_text((M + 30, y), title, fontname="helv", fontsize=fs, color=(0.1, 0.1, 0.1))
            q = f"{int(l['qty'])} btl"
            pg.insert_text((W - M - 6 - fitz.get_text_length(q, fontname="hebo", fontsize=9.5), y), q, fontname="hebo", fontsize=9.5, color=DARK)
            pg.draw_line((M + 30, y + 4), (W - M, y + 4), color=(0.88, 0.88, 0.88), width=0.4)
        y += 18
    y += 4
    pg.draw_line((M, y), (W - M, y), color=WINE, width=1)
    tot = f"TOTAL : {len(cs)} CARTON{'S' if len(cs) > 1 else ''} · {total_b} BOUTEILLE{'S' if total_b > 1 else ''}"
    pg.insert_text((W - M - fitz.get_text_length(tot, fontname="hebo", fontsize=10.5), y + 17), tot, fontname="hebo", fontsize=10.5, color=DARK)
    y += 34
    if o.mode == "chronopost":
        # encart « à la livraison » : trois réflexes + la règle Chronopost expliquée
        steps = [
            ("Ouvrez chaque carton devant le livreur, avant de signer", "Vérifiez que les bouteilles sont intactes et que le contenu correspond à cette liste, carton par carton."),
            ("Écrivez toute anomalie sur le bon de livraison, avant de signer", "Précisez le carton et la bouteille concernée (nom et millésime), par exemple « carton B : Meursault Narvaux 2022, 1 bouteille cassée ». Idem pour une bouteille manquante ou un carton abîmé ou humide."),
            ("Photographiez et prévenez-nous", f"La bouteille concernée, le carton et le bon de livraison annoté, envoyés à {config.CONTACT_EMAIL} : nous ouvrons la réclamation auprès de Chronopost."),
        ]
        why = ("Pourquoi c'est essentiel : Chronopost n'accepte une réclamation que si les réserves figurent sur le bon de livraison au moment de la remise. "
               "Un bon signé sans réserve vaut acceptation d'un colis complet et en bon état ; plus aucun recours n'est ensuite possible, ni pour vous, ni pour nous. "
               "Ces règles sont celles du transporteur : en les suivant, vous nous permettez de vous garantir un remplacement, ou un remboursement si un remplacement "
               "par la même bouteille ou une autre qui vous conviendrait n'est pas possible.")
        box_h = 34 + len(steps) * 42 + 70
        if y + box_h > H - 50:
            pg = new_page(); y = 60
        pg.draw_rect(fitz.Rect(M, y, W - M, y + box_h), color=WINE, fill=(0.99, 0.975, 0.975), width=0.8)
        pg.insert_text((M + 14, y + 20), "À LA LIVRAISON : TROIS RÉFLEXES QUI VOUS PROTÈGENT", fontname="hebo", fontsize=9.5, color=WINE)
        yy = y + 34
        for i, (title, detail) in enumerate(steps, 1):
            pg.draw_circle((M + 22, yy + 6), 7.5, color=None, fill=DARK)
            pg.insert_text((M + 19.2, yy + 9.2), str(i), fontname="hebo", fontsize=8.5, color=(1, 1, 1))
            pg.insert_text((M + 36, yy + 9), title, fontname="hebo", fontsize=9, color=DARK)
            r = pg.insert_textbox(fitz.Rect(M + 36, yy + 13, W - M - 12, yy + 42), detail, fontname="helv", fontsize=8.2, color=(0.2, 0.2, 0.2), lineheight=1.15)
            assert r >= 0, "texte de l'encart trop long"
            yy += 42
        r = pg.insert_textbox(fitz.Rect(M + 14, yy + 2, W - M - 12, yy + 68), why, fontname="heit", fontsize=8, color=GREY, lineheight=1.15)
        assert r >= 0, "paragraphe de l'encart trop long"
        y += box_h
    return doc.tobytes()


# ---------------------------------------------------------------- fichier Alix (même format que le Sheet OWxxxx)
def alix_xlsx(o, cs) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook(); ws = wb.active; ws.title = o.name
    ws.append(["No. Commande", "No Colis", "Etiquette", "SKU", "Designation", "Millesime", "Qté"])
    for c in ws[1]:
        c.font = Font(bold=True)
    imap = service.item_map()
    boxes = {}
    for c in cs:
        for l in service.carton_lines(c):
            it = imap.get(l["sku"])
            ws.append([f"#{o.name}", c.ref, c.tracking or "", l["sku"], l.get("title") or (it.title if it else ""), it.millesime if it else "", int(l["qty"])])
        if c.box_sku and o.mode != "retrait":
            boxes[c.box_sku] = boxes.get(c.box_sku, 0) + 1
    if boxes:
        for sku, n in boxes.items():
            ws.append([f"#{o.name}", "", "", sku, config.PACKAGING[sku]["title"], "n/a", n])
        ws.append([f"#{o.name}", "", "", config.LABEL_SHEET, config.PACKAGING[config.LABEL_SHEET]["title"], "n/a", sum(boxes.values())])
    for col, w in zip("ABCDEFG", (14, 9, 16, 22, 48, 10, 6)):
        ws.column_dimensions[col].width = w
    bio = io.BytesIO(); wb.save(bio); return bio.getvalue()


# ---------------------------------------------------------------- fiche de saisie Chronopost (à reporter à la main)
def chronopost_sheet(o, cs) -> dict:
    return {"recipient": {"name": o.customer, "company": o.company, "address1": o.address1, "address2": o.address2, "zip": o.zip, "city": o.city, "country": o.country,
                          "phone": o.phone, "email": o.email},
            "parcels": [{"ref": c.ref, "reference": f"{o.name}-{c.ref}", "weight_kg": c.weight_kg, "insured": c.insured_value, "bottles": sum(int(l["qty"]) for l in service.carton_lines(c)), "box": c.box_sku} for c in cs],
            "order_ref": o.name, "count": len(cs), "total_weight": round(sum(c.weight_kg for c in cs), 1), "total_insured": round(sum(c.insured_value for c in cs))}


# ---------------------------------------------------------------- lecture des étiquettes Chronopost
NUM = re.compile(r"\b([A-Z]{2}\d{9}[A-Z]{2})\b")


def parse_labels(files: List[Tuple[str, bytes]], refs: List[str]) -> Dict[str, str]:
    """{ref carton: n° Chronopost} à partir des PDF d'étiquettes. La référence est lue dans le texte de l'étiquette
    (« OW1047-A », « Reference : A ») ou dans le nom du fichier « XN…FR (A).pdf » ; sinon dans l'ordre des pages."""
    found: Dict[str, str] = {}
    pages = []
    for name, data in files:
        try:
            d = fitz.open(stream=data, filetype="pdf")
        except Exception:
            continue
        for p in d:
            t = p.get_text()
            nums = NUM.findall(t)
            if not nums:
                continue
            num = nums[0]
            ref = None
            m = re.search(r"OW\d{4}\s*[-–]\s*([A-Z])\b", t) or re.search(r"\(([A-Z])\)", name)
            if m:
                ref = m.group(1)
            else:
                m2 = re.search(r"[Rr]ef(?:erence|érence)?\s*:?\s*([A-Z])\b", t)
                ref = m2.group(1) if m2 else None
            pages.append((ref, num))
    for ref, num in pages:
        if ref in refs and ref not in found:
            found[ref] = num
    leftover = [num for ref, num in pages if num not in found.values()]
    for r in refs:
        if r not in found and leftover:
            found[r] = leftover.pop(0)
    return found


# ---------------------------------------------------------------- e-mails
def email_alix(o, cs) -> dict:
    nb = sum(sum(int(l["qty"]) for l in service.carton_lines(c)) for c in cs)
    kinds = {}
    for c in cs:
        kinds[c.box_sku] = kinds.get(c.box_sku, 0) + 1
    desc = " et ".join(f"{n} carton{'s' if n > 1 else ''} de {config.PACKAGING[k]['bottles']} bouteille{'s' if config.PACKAGING[k]['bottles'] > 1 else ''}" for k, n in kinds.items() if k in config.PACKAGING)
    if o.mode == "retrait":
        body = (f"Bonjour,\n\nJe vous prie de trouver ci-joint le détail de cette nouvelle commande à préparer pour un retrait sur place par le client ({nb} bouteille{'s' if nb > 1 else ''}, dans nos cartons).\n\n"
                f"Le client se présentera à partir du {fr_date(o.pickup_date) if o.pickup_date else '(date à confirmer)'} muni de la confirmation de commande et d'une pièce d'identité.\n\n"
                "En vous remerciant pour votre confirmation une fois que ce sera prêt.\n\nA bientôt,")
        subject = f"Commande OWINE #{o.name} (retrait client)"
    else:
        slot = f"Enlèvement n° {o.pickup_no or '…'} | {fr_date(o.pickup_date) if o.pickup_date else '(date à confirmer)'} entre {o.pickup_slot or '14:00 et 17:00'} | 21200 BEAUNE"
        rappel = "Rappel : Attention comme toujours à bien respecter le contenu de chaque carton selon l'étiquette référencée.\n\n" if len(cs) > 1 else ""
        intl = ""
        if _export.zone(o.country) != "FR":
            intl = export_alix_instructions(o, cs, _export.state(o, cs)) + "\n\n"
        body = (f"Bonjour,\n\nJe vous prie de trouver ci-joint le détail et {lab(len(cs))} pour cette nouvelle commande à préparer ({desc}, {nb} bouteille{'s' if nb > 1 else ''}).\n\n"
                + intl + rappel + f"L'enlèvement a été réservé sur le créneau suivant :\n\n{slot}\n\nEn vous remerciant pour votre confirmation une fois que ce sera prêt.\n\nA bientôt,")
        subject = f"Commande OWINE #{o.name}" + (f" — INTERNATIONAL {_export.X.country_name(o.country)}" if _export.zone(o.country) != "FR" else "")
    return {"to": [config.ALIX_EMAIL], "cc": config.ALIX_CC, "subject": subject, "body": body}


def email_client(o, cs) -> dict:
    first = (o.customer or "").split(" ")[0]
    if o.mode != "retrait" and _export.zone(o.country) != "FR":
        return export_client_email(o, cs, _export.state(o, cs))
    if o.mode == "retrait":
        body = (f"Bonjour {first},\n\nJ'ai le plaisir de vous confirmer que votre commande {o.name} sera prête pour être collectée à notre entrepôt "
                f"à partir du {fr_date(o.pickup_date) if o.pickup_date else '(date à confirmer)'}.\n\n"
                "Je vous invite à vous rendre à l'adresse suivante muni de la confirmation ci-jointe et de votre pièce d'identité, aux horaires d'ouverture : "
                "8h-12h / 13h30-17h (sauf le vendredi 16h30) :\n\n"
                f"{config.ALIX_ADDRESS} · {config.ALIX_EMAIL} · 03 73 55 41 35\n\nEn vous souhaitant bonne réception,\n\nTrès cordialement,")
        subject = f"Commande oWine {o.name} prête à être récupérée"
    else:
        deliv = delivery_of(o.pickup_date) if o.pickup_date else None
        body = (f"Bonjour {first},\n\nJ'ai le plaisir de vous confirmer que l'enlèvement de votre commande {o.name} par Chronopost est réservé pour le "
                f"{fr_date(o.pickup_date) if o.pickup_date else '(date à confirmer)'}. Vous trouverez ci-joint{'e' if len(cs) <= 1 else 'es'} la liste de colisage et {lab(len(cs))} correspondante{'' if len(cs) <= 1 else 's'}.\n\n"
                f"La livraison est prévue le {fr_date(deliv) if deliv else '(à confirmer)'} avant 13 h, sauf aléa de transport.\n\n"
                "⚠️ AVERTISSEMENT IMPORTANT\n\nAu moment de la livraison, nous vous recommandons vivement d'ouvrir le carton avant de signer afin de vérifier que :\n"
                "- les bouteilles sont intactes ;\n- le nombre de bouteilles correspond bien à votre commande (cf. liste de colisage ci-jointe pour le détail du contenu de chaque carton).\n\n"
                "Conformément à la politique de Chronopost (que nous sommes tenus d'appliquer), toute bouteille cassée ou manquante, ou tout colis visiblement abîmé, "
                "doit impérativement être signalé sur le bon de livraison avant signature.\n\n"
                "👉 Si le bon de livraison est signé sans réserve, Chronopost considère le colis comme complet et en bon état et ne permet plus d'ouvrir de réclamation par la suite. "
                "Dans ce cas, nous ne pourrons malheureusement plus intervenir auprès d'eux.\n\n"
                "En cas de problème (bouteille cassée ou manquante, carton humide ou abîmé), il suffit donc de :\n- le mentionner clairement sur le bon de livraison, en précisant le carton et la bouteille concernée (nom et millésime) ;\n"
                f"- photographier la bouteille, le carton et le bon annoté, et nous les envoyer à {config.CONTACT_EMAIL}.\n\n"
                "Nous pourrons alors vous garantir un remplacement, ou un remboursement si un remplacement par la même bouteille ou une autre qui vous conviendrait n'est pas possible.\n\n"
                "Nous restons bien entendu à votre disposition pour toute question et vous souhaitons une excellente réception et une très belle journée.\n\nTrès cordialement,")
        subject = f"Votre commande {o.name} : enlèvement Chronopost réservé, livraison le {fr_date(deliv) if deliv else 'lendemain'} avant 13 h"
    return {"to": [o.email] if o.email else [], "cc": list(config.CLIENT_CC), "subject": subject, "body": body}


def bundle_zip(o, cs, labels: List[Tuple[str, bytes]] = None) -> bytes:
    """Tout ce qu'il faut joindre : liste de colisage, fichier Alix, étiquettes renommées « XN…FR (A).pdf », textes des e-mails."""
    import zipfile
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"Détail {o.name}.pdf", packing_list_pdf(o, cs))
        z.writestr(f"{o.name}.xlsx", alix_xlsx(o, cs))
        if _export.zone(o.country) == "EXPORT":
            inv = _export.stored_invoice(o)
            z.writestr(inv[0] if inv else f"Facture commerciale {o.name} (provisoire).pdf", inv[1] if inv else commercial_invoice_pdf(o, cs))
        for name, data in (labels or []):
            z.writestr(f"Etiquettes/{name}", data)
        ea, ec = email_alix(o, cs), email_client(o, cs)
        z.writestr("E-mail Alix.txt", f"À : {', '.join(ea['to'])}\nCc : {', '.join(ea['cc'])}\nObjet : {ea['subject']}\n\n{ea['body']}")
        z.writestr("E-mail client.txt", f"À : {', '.join(ec['to'])}\nCc : {', '.join(ec['cc'])}\nObjet : {ec['subject']}\n\n{ec['body']}")
    return bio.getvalue()


# ---------------------------------------------------------------- e-mails HTML (style de la liste de colisage, logo inline cid:logo)
import html as _html

CSS_WINE, CSS_DARK, CSS_GREY, CSS_LIGHT = "#8c1a1a", "#4a0d1f", "#6b6b6b", "#f7efef"
TRACK_URL = "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT={n}"


def _esc(x) -> str:
    return _html.escape(str(x or ""))


def _shell(title: str, inner: str) -> str:
    """Gabarit : bandeau logo + titre, corps, devise, pied de page."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<style>:root{{color-scheme:light;}} p,td,span,div{{color:#222222;}}</style></head>
<body style="margin:0;padding:0;background:#f4f1f1;font-family:Helvetica,Arial,sans-serif;color:#222222;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1f1;padding:24px 12px;"><tr><td align="center">
<table role="presentation" width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;">
<tr><td style="padding:22px 30px 14px 30px;border-bottom:2px solid {CSS_WINE};">
  <table role="presentation" width="100%"><tr><td><img src="cid:logo" alt="oWine" style="height:46px;width:auto;display:block;"></td>
  <td align="right" style="font-size:15px;font-weight:bold;color:{CSS_DARK};">{_esc(title)}</td></tr></table></td></tr>
<tr><td style="padding:22px 30px 10px 30px;font-size:14.5px;line-height:1.55;color:#222222;background:#ffffff;">{inner}</td></tr>
<tr><td align="center" style="padding:8px 30px 4px 30px;font-family:Georgia,'Times New Roman',serif;font-style:italic;font-size:15px;color:{CSS_WINE};">« {_esc(config.MOTTO)} »</td></tr>
<tr><td align="center" style="padding:0 30px 4px 30px;"><div style="width:56px;border-top:1px solid {CSS_WINE};"></div></td></tr>
<tr><td align="center" style="padding:10px 30px 22px 30px;font-size:11.5px;color:{CSS_GREY};">oWine SAS · Parc d'activité, 14 E rue Coubertin, 21000 Dijon · <a href="https://www.owine.co" style="color:{CSS_GREY};">www.owine.co</a> · <a href="mailto:{config.CONTACT_EMAIL}" style="color:{CSS_GREY};">{config.CONTACT_EMAIL}</a></td></tr>
</table></td></tr></table></body></html>"""


def _cartons_table(o, cs, with_tracking: bool = True) -> str:
    rows = ""
    for c in cs:
        lines = service.carton_lines(c); nb = sum(int(l["qty"]) for l in lines)
        track = ""
        if with_tracking and c.tracking:
            track = f'<a href="{TRACK_URL.format(n=c.tracking)}" style="color:{CSS_WINE};text-decoration:none;font-size:12.5px;">{_esc(c.tracking)}</a>'
        content = "<br>".join(f"{int(l['qty'])} × {_esc(l.get('title') or l['sku'])}" for l in lines)
        rows += (f'<tr><td style="padding:8px 10px;border-top:1px solid #e9e2e2;vertical-align:top;"><span style="display:inline-block;background:{CSS_DARK};color:#fff;font-weight:bold;border-radius:4px;padding:2px 8px;">{_esc(c.ref)}</span></td>'
                 f'<td style="padding:8px 10px;border-top:1px solid #e9e2e2;font-size:13.5px;vertical-align:top;">{content}</td>'
                 f'<td style="padding:8px 10px;border-top:1px solid #e9e2e2;text-align:right;white-space:nowrap;vertical-align:top;font-size:13px;">{nb} btl{("<br>" + track) if track else ""}</td></tr>')
    total = sum(sum(int(l["qty"]) for l in service.carton_lines(c)) for c in cs)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e9e2e2;border-radius:6px;font-size:14px;">'
            f'<tr style="background:{CSS_LIGHT};"><td style="padding:8px 10px;font-weight:bold;color:{CSS_DARK};">Carton</td><td style="padding:8px 10px;font-weight:bold;color:{CSS_DARK};">Contenu</td>'
            f'<td style="padding:8px 10px;font-weight:bold;color:{CSS_DARK};text-align:right;">Bouteilles{" · n° de suivi" if with_tracking else ""}</td></tr>{rows}'
            f'<tr style="background:{CSS_LIGHT};"><td colspan="3" style="padding:8px 10px;text-align:right;font-weight:bold;color:{CSS_DARK};">TOTAL : {len(cs)} carton{"s" if len(cs) > 1 else ""} · {total} bouteille{"s" if total > 1 else ""}</td></tr></table>')


def _delivery_box() -> str:
    steps = [("Ouvrez chaque carton devant le livreur, avant de signer", "et vérifiez que les bouteilles sont intactes et que le contenu correspond à la liste de colisage jointe."),
             ("Écrivez toute anomalie sur le bon de livraison, avant de signer", "en précisant le carton et la bouteille concernée (nom et millésime), par exemple « carton B : Meursault Narvaux 2022, 1 bouteille cassée ». Idem pour une bouteille manquante ou un carton abîmé ou humide."),
             ("Photographiez et prévenez-nous", f"la bouteille concernée, le carton et le bon annoté, à <a href=\"mailto:{config.CONTACT_EMAIL}\" style=\"color:{CSS_WINE};\">{config.CONTACT_EMAIL}</a> : nous ouvrons la réclamation auprès de Chronopost.")]
    items = "".join(f'<tr><td style="padding:6px 8px 6px 0;vertical-align:top;"><span style="display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border-radius:11px;background:{CSS_DARK};color:#fff;font-weight:bold;font-size:12px;">{i}</span></td>'
                    f'<td style="padding:6px 0;vertical-align:top;font-size:13.5px;"><strong style="color:{CSS_DARK};">{t}</strong><br><span style="color:#333;">{d}</span></td></tr>' for i, (t, d) in enumerate(steps, 1))
    return (f'<div style="border:1px solid {CSS_WINE};border-radius:8px;background:#fdf8f8;padding:14px 16px;margin:18px 0;">'
            f'<div style="font-weight:bold;color:{CSS_WINE};font-size:13px;letter-spacing:.3px;margin-bottom:6px;">À LA LIVRAISON : TROIS RÉFLEXES QUI VOUS PROTÈGENT</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0">{items}</table>'
            f'<div style="font-size:12.5px;color:{CSS_GREY};font-style:italic;margin-top:8px;line-height:1.45;">Pourquoi c\'est essentiel : Chronopost n\'accepte une réclamation que si les réserves figurent sur le bon de livraison au moment de la remise. '
            f'Un bon signé sans réserve vaut acceptation d\'un colis complet et en bon état ; plus aucun recours n\'est ensuite possible, ni pour vous, ni pour nous. '
            f'Ces règles sont celles du transporteur : en les suivant, vous nous permettez de vous garantir un remplacement, ou un remboursement si un remplacement par la même bouteille ou une autre qui vous conviendrait n\'est pas possible.</div></div>')


def email_client_html(o, cs, extra: str = "") -> str:
    if o.mode != "retrait" and _export.zone(o.country) != "FR":
        return export_client_email_html(o, cs, _export.state(o, cs), extra)
    first = _esc((o.customer or "").split(" ")[0])
    extra_html = f"<p>{_esc(extra).replace(chr(10), '<br>')}</p>" if extra else ""
    if o.mode == "retrait":
        inner = (f"<p>Bonjour {first},</p><p>J'ai le plaisir de vous confirmer que votre commande <strong>{_esc(o.name)}</strong> sera prête pour être collectée à notre entrepôt "
                 f"à partir du <strong>{_esc(fr_date(o.pickup_date)) if o.pickup_date else '(date à confirmer)'}</strong>.</p>"
                 f"<p>Merci de vous présenter muni de la confirmation ci-jointe et de votre pièce d'identité, aux horaires d'ouverture : 8h-12h / 13h30-17h (sauf le vendredi 16h30).</p>"
                 f"<p style=\"background:{CSS_LIGHT};padding:10px 14px;border-radius:6px;\"><strong>{_esc(config.ALIX_ADDRESS)}</strong><br>{_esc(config.ALIX_EMAIL)} · 03 73 55 41 35</p>"
                 + extra_html + _cartons_table(o, cs, with_tracking=False) + "<p>En vous souhaitant bonne réception,</p><p>Très cordialement,<br><strong>Jean-Sébastien CHEUNG-AH-SEUNG</strong><br>oWine</p>")
        return _shell(f"Commande {o.name} prête", inner)
    deliv = delivery_of(o.pickup_date) if o.pickup_date else None
    inner = (f"<p>Bonjour {first},</p>"
             f"<p>J'ai le plaisir de vous confirmer que l'enlèvement de votre commande <strong>{_esc(o.name)}</strong> par Chronopost est réservé pour le <strong>{_esc(fr_date(o.pickup_date)) if o.pickup_date else '(date à confirmer)'}</strong>. "
             f"La livraison est prévue le <strong>{_esc(fr_date(deliv)) if deliv else '(à confirmer)'} avant 13 h</strong>, sauf aléa de transport.</p>"
             + extra_html
             + _cartons_table(o, cs, with_tracking=True)
             + f"<p style=\"font-size:13px;color:#555;\">Vous trouverez ci-joint{'e' if len(cs) <= 1 else 's'} la liste de colisage détaillée et {lab(len(cs))} ({'numéro de suivi cliquable ci-dessus' if len(cs) <= 1 else 'un numéro de suivi par carton, cliquable ci-dessus'}).</p>"
             + _delivery_box()
             + "<p>Nous restons bien entendu à votre disposition pour toute question et vous souhaitons une excellente réception.</p>"
             + "<p>Très cordialement,<br><strong>Jean-Sébastien CHEUNG-AH-SEUNG</strong><br>oWine</p>")
    return _shell(f"Commande {o.name} · enlèvement réservé", inner)


def email_alix_html(o, cs, extra: str = "") -> str:
    nb = sum(sum(int(l["qty"]) for l in service.carton_lines(c)) for c in cs)
    extra_html = f"<p>{_esc(extra).replace(chr(10), '<br>')}</p>" if extra else ""
    if o.mode == "retrait":
        inner = (f"<p>Bonjour,</p><p>Je vous prie de trouver ci-joint le détail de cette nouvelle commande à préparer pour un <strong>retrait sur place par le client</strong> ({nb} bouteille{'s' if nb > 1 else ''}, dans nos cartons).</p>"
                 f"<p>Le client se présentera à partir du <strong>{_esc(fr_date(o.pickup_date)) if o.pickup_date else '(date à confirmer)'}</strong> muni de la confirmation de commande et d'une pièce d'identité.</p>"
                 + extra_html + _cartons_table(o, cs, with_tracking=False) + "<p>En vous remerciant pour votre confirmation une fois que ce sera prêt.</p><p>A bientôt,<br><strong>Jean-Sébastien CHEUNG-AH-SEUNG</strong><br>oWine</p>")
        return _shell(f"Commande OWINE #{o.name} — retrait client", inner)
    slot = f"Enlèvement n° {_esc(o.pickup_no or '…')} · {_esc(fr_date(o.pickup_date)) if o.pickup_date else '(date à confirmer)'} entre {_esc(o.pickup_slot or '14:00 et 17:00')} · 21200 BEAUNE"
    rappel = f"<p style=\"background:#fff3cd;border-radius:6px;padding:10px 14px;color:#222;\"><strong>Rappel :</strong> attention comme toujours à bien respecter le contenu de chaque carton selon l'étiquette référencée.</p>" if len(cs) > 1 else ""
    intl = ""
    if _export.zone(o.country) != "FR":
        txt = export_alix_instructions(o, cs, _export.state(o, cs)).split("\n")
        intl = (f"<div style=\"border:2px solid {CSS_WINE};border-radius:8px;padding:10px 14px;margin:12px 0;background:#fdf8f8;color:#222;\"><div style=\"font-weight:bold;color:{CSS_WINE};\">{_esc(txt[0])}</div>"
                + "".join(f"<p style=\"margin:6px 0 0 0;\">{_esc(t)}</p>" for t in txt[1:]) + "</div>")
    inner = (f"<p>Bonjour,</p><p>Je vous prie de trouver ci-joint le détail et {lab(len(cs))} pour cette nouvelle commande à préparer ({len(cs)} carton{'s' if len(cs) > 1 else ''}, {nb} bouteille{'s' if nb > 1 else ''}).</p>"
             + intl + rappel
             + _cartons_table(o, cs, with_tracking=True)
             + f"<p style=\"background:{CSS_LIGHT};border-radius:6px;padding:10px 14px;margin-top:14px;color:#222;\"><strong>L'enlèvement a été réservé sur le créneau suivant :</strong><br>{slot}</p>"
             + extra_html + "<p>En vous remerciant pour votre confirmation une fois que ce sera prêt.</p><p>A bientôt,<br><strong>Jean-Sébastien CHEUNG-AH-SEUNG</strong><br>oWine</p>")
    return _shell(f"Commande OWINE #{o.name}" + (f" — {_export.X.country_name(o.country)}" if _export.zone(o.country) != "FR" else ""), inner)


# ================================================================== EXPORT : facture commerciale (modèle Chrono Viti « Facture commerciale Viti to B »)
from . import export as _export

def _clean(txt) -> str:
    """Les polices de base (Helvetica) n'ont ni tiret cadratin ni points de suspension : on les remplace pour éviter les « ? »."""
    return str(txt).replace("\u2014", "-").replace("\u2013", "-").replace("\u2026", "...").replace("\u2019", "'").replace("\u00a0", " ")


def _t(pg, x, y, txt, font="helv", size=8.5, color=(0.1, 0.1, 0.1)):
    pg.insert_text((x, y), _clean(txt), fontname=font, fontsize=size, color=color)


def _tr(pg, xr, y, txt, font="helv", size=8.5, color=(0.1, 0.1, 0.1)):
    t = _clean(txt)
    pg.insert_text((xr - fitz.get_text_length(t, fontname=font, fontsize=size), y), t, fontname=font, fontsize=size, color=color)


def _fit(txt, width, font="helv", size=8.5, min_size=6.0):
    while fitz.get_text_length(_clean(txt), fontname=font, fontsize=size) > width and size > min_size:
        size -= 0.25
    return size


def _wrap(txt, width, font="helv", size=7.2):
    """Retour à la ligne par mots (les zones de texte de fitz refusent un texte trop long au lieu de le couper)."""
    out, cur = [], ""
    for w in _clean(txt).split(" "):
        cand = (cur + " " + w).strip()
        if fitz.get_text_length(cand, fontname=font, fontsize=size) <= width or not cur:
            cur = cand
        else:
            out.append(cur); cur = w
    if cur:
        out.append(cur)
    return out


def _para(pg, rect, txt, font="helv", size=7.2, color=(0.1, 0.1, 0.1), lh=1.25):
    """Paragraphe(s) dans un rectangle, par retour à la ligne manuel ; renvoie l'ordonnée finale."""
    y = rect.y0 + size
    for para in _clean(txt).split("\n"):
        for line in _wrap(para, rect.width, font, size):
            pg.insert_text((rect.x0, y), line, fontname=font, fontsize=size, color=color); y += size * lh
    return y


def _money(v) -> str:
    return f"{float(v or 0):,.2f}".replace(",", " ").replace(".", ",")


def commercial_invoice_pdf(o, cs, copies: int = 3) -> bytes:
    """Facture commerciale bilingue pour la douane : expéditeur (EORI, TVA), destinataire (téléphone, e-mail, EORI/TVA ou identifiant fiscal), numéros de
    colis et positions dans le groupage, poids brut / net, description complète de chaque vin (couleur, millésime, contenance, degré, origine, code SH),
    prix unitaires et totaux HT en EUR, port et assurance à part, incoterm, usage final, exonération de TVA, déclaration d'origine sur facture datée,
    localisée et signée. Un bloc par envoi (Suisse particuliers : un envoi par carton), chaque envoi en `copies` exemplaires (3 pour Chronopost)."""
    d = _export.invoice_data(o, cs)
    sig = _export.signature()
    provisional = any(not p["tracking"] for sh in d["shipments"] for p in sh["parcels"])
    doc = fitz.open()
    W, H, M = 595, 842, 40
    INK, GREY, LINE = (0.1, 0.1, 0.1), (0.42, 0.42, 0.42), (0.75, 0.75, 0.75)
    snd, rcp = d["sender"], d["recipient"]

    def header(pg, sh, copy_no):
        try:
            pg.insert_image(fitz.Rect(M, 28, M + 118, 28 + 41), filename=LOGO, keep_proportion=True)
        except Exception:
            _t(pg, M, 55, "oWine", "tibo", 20, WINE)
        _tr(pg, W - M, 44, "FACTURE COMMERCIALE", "hebo", 14, DARK)
        _tr(pg, W - M, 58, "COMMERCIAL INVOICE", "helv", 9.5, GREY)
        _tr(pg, W - M, 74, f"N° / No. {sh['no']}", "hebo", 9.5, INK)
        _tr(pg, W - M, 86, f"Date : {d['date']:%d/%m/%Y}   ·   Exemplaire / Copy {copy_no}/{copies}", "helv", 8.5, INK)
        pg.draw_line((M, 96), (W - M, 96), color=WINE, width=1.2)
        if provisional:
            pg.draw_rect(fitz.Rect(M, 100, W - M, 116), color=None, fill=(1, 0.93, 0.93))
            _t(pg, M + 6, 111.5, "PROVISOIRE — numéros de colis Chronopost à compléter / DRAFT — parcel numbers missing", "hebo", 8.5, (0.7, 0.1, 0.1))

    def party(pg, x, y, w, title, rows):
        pg.draw_rect(fitz.Rect(x, y, x + w, y + 16), color=None, fill=LIGHT)
        _t(pg, x + 6, y + 11.5, title, "hebo", 8.5, DARK)
        yy = y + 16
        for lab, val in rows:
            if val in (None, ""):
                continue
            yy += 10.5
            _t(pg, x + 6, yy, lab, "helv", 6.8, GREY)
            size = _fit(val, w - 12 - 88, "helv", 8.5)
            _t(pg, x + 94, yy, val, "helv" if lab else "hebo", size, INK)
        pg.draw_rect(fitz.Rect(x, y, x + w, yy + 6), color=LINE, width=0.5)
        return yy + 6

    def footer(pg, page_no, pages):
        legal = f"{snd['company']}" + (f" — SAS au capital de {snd['capital']}" if snd.get("capital") else "") + f" — {snd['rcs']}" + (f" — SIRET {snd['siret']}" if snd.get("siret") else "") + f" — TVA {snd['vat']}"
        legal2 = f"{snd['address1']}, {snd['address2']}, {snd['zip']} {snd['city']}, France — {snd['email']} — {snd['phone']}" + (f" — EORI {snd['eori']}" if snd.get("eori") else "")
        pg.draw_line((M, H - 44), (W - M, H - 44), color=LINE, width=0.5)
        _t(pg, W / 2 - fitz.get_text_length(legal, fontname="helv", fontsize=7) / 2, H - 33, legal, "helv", 7, GREY)
        _t(pg, W / 2 - fitz.get_text_length(legal2, fontname="helv", fontsize=7) / 2, H - 23, legal2, "helv", 7, GREY)
        _tr(pg, W - M, H - 23, f"{page_no}/{pages}", "helv", 7, GREY)

    for sh in d["shipments"]:
        for copy_no in range(1, copies + 1):
            pages_of_copy = []                                   # indices : doc.new_page() invalide les objets Page déjà créés
            pg = doc.new_page(width=W, height=H); pages_of_copy.append(doc.page_count - 1)
            header(pg, sh, copy_no)
            y = 124 if provisional else 108
            colw = (W - 2 * M - 10) / 2
            y1 = party(pg, M, y, colw, "EXPÉDITEUR / SENDER (exporter)", [
                ("Société / Company", snd["company"]), ("Contact", snd["contact"]), ("Adresse / Address", f"{snd['address1']}, {snd['address2']}"), ("CP Ville / City", f"{snd['zip']} {snd['city']}"),
                ("Pays / Country", snd["country"]), ("Téléphone / Phone", snd["phone"]), ("E-mail", snd["email"]), ("N° EORI", snd["eori"] or "(à renseigner / to be filled)"), ("N° TVA / VAT No.", snd["vat"])])
            rrows = [("Nom / Name", rcp["name"]), ("Société / Company", rcp["company"]), ("Adresse / Address", rcp["address1"]), ("", rcp["address2"]), ("CP Ville / City", f"{rcp['zip']} {rcp['city']}"),
                     ("Pays / Country", rcp["country"]), ("Téléphone / Phone", rcp["phone"] or "(obligatoire / required)"), ("E-mail", rcp["email"] or "(obligatoire / required)")]
            if d["kind"] == "societe":
                rrows += [("N° EORI", rcp["eori"]), ("N° TVA / VAT No.", rcp["vat"])]
            elif rcp["tax_id"]:
                rrows += [(rcp["tax_id_label"] or "Identifiant / Tax ID", rcp["tax_id"])]
            y2 = party(pg, M + colw + 10, y, colw, "DESTINATAIRE / CONSIGNEE" + (" (professionnel / business)" if d["kind"] == "societe" else " (particulier / private individual)"), rrows)
            y = max(y1, y2) + 10
            # ---- expédition
            pg.draw_rect(fitz.Rect(M, y, W - M, y + 16), color=None, fill=LIGHT)
            _t(pg, M + 6, y + 11.5, "EXPÉDITION / SHIPMENT", "hebo", 8.5, DARK)
            yy = y + 28
            info = [f"Transporteur / Carrier : Chronopost {d['product']['label']} ({d['product']['mode']})", f"Incoterm ICC 2020 : {d['incoterm']} {d['incoterm_place']}",
                    f"Commande / Order : {d['order']}" + (f" du {d['order_date']:%d/%m/%Y}" if d["order_date"] else "") + f"   ·   Nombre de colis / Number of parcels : {len(sh['parcels'])}",
                    f"Poids brut / Gross weight : {sh['gross_kg']} kg   ·   Poids net / Net weight : {sh['net_kg']} kg   ·   {sh['bottles']} bouteilles / bottles",
                    f"Nature / Content : {d['content_desc']}"]
            for line in info:
                _t(pg, M + 6, yy, line, "helv", 8, INK); yy += 10
            yy += 2
            _t(pg, M + 6, yy, "Numéros de colis / Parcel numbers (lettres de transport) :", "helv", 7, GREY); yy += 10
            for p in sh["parcels"]:
                _t(pg, M + 14, yy, f"Colis {p['ref']} ({p['pos']}) : {p['tracking'] or '__________________'}   -   {p['bottles']} btl · {p['kg']} kg", "hebo" if p["tracking"] else "helv", 8, INK); yy += 10
            pg.draw_rect(fitz.Rect(M, y, W - M, yy + 2), color=LINE, width=0.5)
            y = yy + 12
            # ---- tableau des marchandises
            cols = [("Désignation des marchandises\nDescription of goods", 185), ("Origine\nOrigin", 30), ("Code SH\nHS code", 46), ("% vol", 26), ("Cont.\nContent", 28),
                    ("Couleur\nColour", 36), ("Mill.\nVintage", 28), ("Qté\nQty", 24), ("PU HT EUR\nUnit price", 52), ("Total HT EUR\nTotal", 60)]
            xs = [M]
            for _, w in cols:
                xs.append(xs[-1] + w)

            def table_head(pg, y):
                pg.draw_rect(fitz.Rect(M, y, W - M, y + 22), color=None, fill=DARK)
                for i, (lab, w) in enumerate(cols):
                    parts = lab.split("\n")
                    for j, part in enumerate(parts):
                        size = _fit(part, w - 4, "hebo", 6.8)
                        if i == 0:
                            _t(pg, xs[i] + 3, y + 9 + j * 8.5, part, "hebo", size, (1, 1, 1))
                        else:
                            _tr(pg, xs[i + 1] - 3, y + 9 + j * 8.5, part, "hebo", size, (1, 1, 1))
                return y + 22

            y = table_head(pg, y)
            for ln in sh["lines"]:
                t_lines = _wrap(ln["desc_lines"][0], cols[0][1] - 6, "hebo", 7.4)
                d_lines = _wrap(ln["desc_lines"][1], cols[0][1] - 6, "helv", 6.8)
                rh = 7 + 8.5 * (len(t_lines) + len(d_lines))
                if y + rh > H - 210:
                    pg = doc.new_page(width=W, height=H); pages_of_copy.append(doc.page_count - 1); header(pg, sh, copy_no)
                    y = table_head(pg, 124 if provisional else 108)
                yy = y + 10.5
                for line in t_lines:
                    _t(pg, xs[0] + 3, yy, line, "hebo", 7.4, INK); yy += 8.5
                for line in d_lines:
                    _t(pg, xs[0] + 3, yy, line, "helv", 6.8, INK); yy += 8.5
                col = {"rouge": "Rouge / Red", "blanc": "Blanc / White", "rosé": "Rosé", "rose": "Rosé"}.get(ln.get("colour") or "", "-")
                vals = [ln.get("origin") or "FR", ln.get("hs") or "", f"{ln['abv']:g} %" if ln.get("abv") else "-", f"{ln.get('volume_cl') or 75} cl", col, str(ln.get("vintage") or "-"), str(ln["qty"]), _money(ln["unit_ht"]), _money(ln["total_ht"])]
                for i, v in enumerate(vals, 1):
                    _tr(pg, xs[i + 1] - 3, y + 10.5, v, "hebo" if i >= 8 else "helv", _fit(v, cols[i][1] - 6, "helv", 7.6), INK)
                pg.draw_line((M, y + rh), (W - M, y + rh), color=LINE, width=0.4)
                y += rh
            for x in xs:
                pg.draw_line((x, y), (x, y), color=LINE, width=0.4)
            y += 6
            # ---- totaux (droite) et mentions (gauche)
            tx = xs[5]
            rows = [("Marchandises HT / Goods excl. taxes", sh["goods_ht"], False), ("Port et manutention HT / Shipping & handling", sh["shipping_ht"], False),
                    ("Assurance / Insurance", sh["insurance_ht"], False), ("TOTAL HT EUR / Total excl. taxes", sh["total_ht"], True)]
            ty = y + 4
            for lab, val, bold in rows:
                if bold:
                    pg.draw_rect(fitz.Rect(tx, ty - 10, W - M, ty + 4), color=None, fill=LIGHT)
                _t(pg, tx + 4, ty, lab, "hebo" if bold else "helv", _fit(lab, (W - M) - tx - 84, "helv", 7.6), INK)
                _tr(pg, W - M - 4, ty, _money(val) + " EUR", "hebo" if bold else "helv", 8.2, INK); ty += 13
            box = fitz.Rect(M, y, tx - 10, ty + 40)
            mentions = ["Devise / Currency : EUR - prix hors taxes / prices excl. taxes", d["final_use"], d["vat_mention"],
                        f"Incoterm ICC 2020 : {d['incoterm']} - droits et taxes à l'import à la charge du destinataire / import duties and taxes payable by the consignee" if d["incoterm"] == "DAP" else f"Incoterm ICC 2020 : {d['incoterm']}"]
            if sh["eur1"]:
                mentions.append("Valeur > 6 000 EUR : certificat d'origine EUR.1 joint / EUR.1 movement certificate attached")
            if d.get("notes"):
                mentions.append(d["notes"])
            ym = _para(pg, box, "\n".join(mentions), "helv", 6.9, INK, 1.3)
            y = max(ty, ym) + 8
            # ---- déclaration d'origine, signature
            oh = 84
            if y + oh > H - 50:
                pg = doc.new_page(width=W, height=H); pages_of_copy.append(doc.page_count - 1); header(pg, sh, copy_no); y = 124 if provisional else 108
            pg.draw_rect(fitz.Rect(M, y, W - M, y + oh), color=LINE, width=0.5)
            _t(pg, M + 6, y + 11, "DÉCLARATION D'ORIGINE / ORIGIN DECLARATION", "hebo", 8, DARK)
            _para(pg, fitz.Rect(M + 6, y + 14, W - M - 150, y + oh - 2), d["origin_fr"] + "\n" + d["origin_en"], "heit", 7, INK, 1.2)
            sx = W - M - 140
            _t(pg, sx, y + 26, f"Lieu / Place : {snd['sign_place']}", "helv", 7.5, INK)
            _t(pg, sx, y + 37, f"Date : {d['date']:%d/%m/%Y}", "helv", 7.5, INK)
            _t(pg, sx, y + 48, f"Signature — {snd['contact']}", "helv", 7.5, INK)
            if sig:
                try:
                    pg.insert_image(fitz.Rect(sx, y + 50, sx + 120, y + oh - 3), stream=sig, keep_proportion=True)
                except Exception:
                    pass
            for i, idx in enumerate(pages_of_copy, 1):
                footer(doc[idx], i, len(pages_of_copy))
    return doc.tobytes()


# ================================================================== EXPORT : e-mails (client FR / EN, Alix), fiche de saisie, pièces
def _lang(o) -> str:
    return "en" if (o.locale or "fr").lower()[:2] not in ("fr",) else "fr"


def export_client_email(o, cs, exp: dict) -> dict:
    """E-mail client pour une commande internationale : enlèvement, délai indicatif (fiche pays / zoning), DAP (droits et taxes payés au transporteur),
    suivi, réserves à la livraison. Français ou anglais selon la langue du client sur Shopify."""
    lang = _lang(o); first = (o.customer or "").split(" ")[0]; n = len(cs)
    cname = _export.X.country_name(o.country, lang)
    delay = _export.delay_text(o.country, exp["kind"], exp["product"], lang)
    customs = exp["customs"]
    if lang == "en":
        body = (f"Hello {first},\n\nYour order {o.name} has been booked for collection by Chronopost ({exp['product_label'].split(' ·')[0].replace('Chrono ', 'Chrono ')}) on "
                f"{o.pickup_date:%A %d %B %Y}" if o.pickup_date else f"Hello {first},\n\nYour order {o.name} has been booked for collection by Chronopost")
        body += f". Estimated delivery to {cname}: {delay or 'a few working days'}, barring transport or customs delays.\n\n"
        body += f"Please find attached the packing list ({n} parcel{'s' if n > 1 else ''}) and the shipping label{'s' if n > 1 else ''}; each parcel can be tracked on chronopost.fr with its number.\n\n"
        if customs:
            body += ("IMPORTANT — CUSTOMS (Incoterm DAP)\nYour order is shipped duty unpaid: the import duties, VAT and customs clearance fees of your country are not included in your order and are payable by you to Chronopost "
                     "(or its local partner) before delivery. Chronopost will contact you by e-mail or SMS to settle them. A copy of the commercial invoice used for customs is attached; three originals travel with the first parcel.\n\n")
        body += ("ON DELIVERY: three reflexes that protect you\n1. Open each parcel in front of the driver, before signing, and check that the bottles are intact and match the packing list.\n"
                 "2. Write any damage or missing bottle on the delivery note before signing (parcel and bottle concerned). A note signed without reservation means the carrier considers the parcel complete and in good condition, and no claim is possible afterwards.\n"
                 f"3. Photograph the bottle, the parcel and the annotated note and send them to {config.CONTACT_EMAIL}: we open the claim with Chronopost and guarantee a replacement, or a refund if a replacement is not possible.\n\n"
                 "We remain at your disposal and wish you a wonderful tasting.\n\nKind regards,")
        subject = f"Your oWine order {o.name}: collection booked, delivery to {cname} {delay}".strip()
    else:
        body = (f"Bonjour {first},\n\nJ'ai le plaisir de vous confirmer que l'enlèvement de votre commande {o.name} par Chronopost ({exp['product_label'].split(' ·')[0]}) est réservé pour le "
                f"{fr_date(o.pickup_date) if o.pickup_date else '(date à confirmer)'}. Livraison estimée en {cname} : {delay or 'quelques jours ouvrés'}, sauf aléa de transport ou de douane.\n\n"
                f"Vous trouverez ci-joint{'e' if n <= 1 else 's'} la liste de colisage ({n} colis) et {lab(n)} ; chaque colis se suit sur chronopost.fr avec son numéro.\n\n")
        if customs:
            body += ("IMPORTANT — DOUANE (incoterm DAP)\nVotre commande voyage droits non acquittés : les droits de douane, la TVA et les frais de dédouanement de votre pays ne sont pas compris dans votre commande et vous seront demandés par Chronopost "
                     "(ou son partenaire local) avant la livraison, par e-mail ou SMS. La copie de la facture commerciale servant au dédouanement est jointe ; trois originaux voyagent avec le premier colis.\n\n")
        body += ("À LA LIVRAISON : trois réflexes qui vous protègent\n1. Ouvrez chaque carton devant le livreur, avant de signer, et vérifiez que les bouteilles sont intactes et conformes à la liste de colisage.\n"
                 "2. Écrivez toute anomalie sur le bon de livraison avant de signer (carton et bouteille concernés). Un bon signé sans réserve vaut acceptation d'un colis complet et en bon état : plus aucun recours n'est possible ensuite.\n"
                 f"3. Photographiez la bouteille, le carton et le bon annoté et envoyez-les à {config.CONTACT_EMAIL} : nous ouvrons la réclamation auprès de Chronopost et vous garantissons un remplacement, ou un remboursement si un remplacement n'est pas possible.\n\n"
                 "Nous restons à votre disposition et vous souhaitons une excellente dégustation.\n\nTrès cordialement,")
        subject = f"Votre commande {o.name} : enlèvement Chronopost réservé, livraison en {cname} {delay}".strip()
    return {"to": [o.email] if o.email else [], "cc": list(config.CLIENT_CC), "subject": subject, "body": body, "lang": lang}


def export_client_email_html(o, cs, exp: dict, extra: str = "") -> str:
    lang = _lang(o); first = _esc((o.customer or "").split(" ")[0]); n = len(cs)
    cname = _esc(_export.X.country_name(o.country, lang)); delay = _esc(_export.delay_text(o.country, exp["kind"], exp["product"], lang))
    extra_html = f"<p>{_esc(extra).replace(chr(10), '<br>')}</p>" if extra else ""
    prod = _esc(exp["product_label"].split(" ·")[0])
    if lang == "en":
        pick = f"{o.pickup_date:%A %d %B %Y}" if o.pickup_date else "(date to be confirmed)"
        inner = (f"<p>Hello {first},</p><p>Your order <strong>{_esc(o.name)}</strong> has been booked for collection by Chronopost ({prod}) on <strong>{pick}</strong>. "
                 f"Estimated delivery to {cname}: <strong>{delay or 'a few working days'}</strong>, barring transport or customs delays.</p>" + extra_html + _cartons_table(o, cs, with_tracking=True))
        if exp["customs"]:
            inner += (f'<div style="border:1px solid {CSS_WINE};border-radius:8px;background:#fdf8f8;padding:12px 16px;margin:16px 0;"><div style="font-weight:bold;color:{CSS_WINE};font-size:13px;margin-bottom:4px;">IMPORTANT — CUSTOMS (Incoterm DAP)</div>'
                      '<div style="font-size:13.5px;color:#222;">Your order is shipped duty unpaid: the import duties, VAT and customs clearance fees of your country are <strong>not included</strong> in your order and are payable by you to Chronopost (or its local partner) before delivery. '
                      'Chronopost will contact you by e-mail or SMS to settle them. A copy of the commercial invoice used for customs is attached; three originals travel with the first parcel.</div></div>')
        inner += (f'<p style="font-size:13px;color:#555;">Attached: the packing list and the shipping label{"s" if n > 1 else ""} (one tracking number per parcel, clickable above).</p>'
                  f'<div style="border:1px solid {CSS_WINE};border-radius:8px;background:#fdf8f8;padding:14px 16px;margin:18px 0;"><div style="font-weight:bold;color:{CSS_WINE};font-size:13px;margin-bottom:6px;">ON DELIVERY: THREE REFLEXES THAT PROTECT YOU</div>'
                  '<ol style="font-size:13.5px;margin:0;padding-left:18px;"><li><strong>Open each parcel in front of the driver, before signing</strong>, and check that the bottles are intact and match the packing list.</li>'
                  '<li><strong>Write any damage or missing bottle on the delivery note before signing</strong> (parcel and bottle concerned). A note signed without reservation means the carrier considers the parcel complete and in good condition: no claim is possible afterwards.</li>'
                  f'<li><strong>Photograph and tell us</strong>: the bottle, the parcel and the annotated note, to <a href="mailto:{config.CONTACT_EMAIL}" style="color:{CSS_WINE};">{config.CONTACT_EMAIL}</a>. We open the claim with Chronopost and guarantee a replacement, or a refund if a replacement is not possible.</li></ol></div>'
                  "<p>We remain at your disposal and wish you a wonderful tasting.</p><p>Kind regards,<br><strong>Jean-Sébastien CHEUNG-AH-SEUNG</strong><br>oWine</p>")
        return _shell(f"Order {o.name} · collection booked", inner)
    pick = _esc(fr_date(o.pickup_date)) if o.pickup_date else "(date à confirmer)"
    inner = (f"<p>Bonjour {first},</p><p>J'ai le plaisir de vous confirmer que l'enlèvement de votre commande <strong>{_esc(o.name)}</strong> par Chronopost ({prod}) est réservé pour le <strong>{pick}</strong>. "
             f"Livraison estimée en {cname} : <strong>{delay or 'quelques jours ouvrés'}</strong>, sauf aléa de transport ou de douane.</p>" + extra_html + _cartons_table(o, cs, with_tracking=True))
    if exp["customs"]:
        inner += (f'<div style="border:1px solid {CSS_WINE};border-radius:8px;background:#fdf8f8;padding:12px 16px;margin:16px 0;"><div style="font-weight:bold;color:{CSS_WINE};font-size:13px;margin-bottom:4px;">IMPORTANT — DOUANE (incoterm DAP)</div>'
                  '<div style="font-size:13.5px;color:#222;">Votre commande voyage droits non acquittés : les droits de douane, la TVA et les frais de dédouanement de votre pays <strong>ne sont pas compris</strong> dans votre commande et vous seront demandés par Chronopost (ou son partenaire local) avant la livraison, par e-mail ou SMS. '
                  'La copie de la facture commerciale servant au dédouanement est jointe ; trois originaux voyagent avec le premier colis.</div></div>')
    inner += (f'<p style="font-size:13px;color:#555;">Ci-joint{"e" if n <= 1 else "s"} : la liste de colisage et {lab(n)} (un numéro de suivi par colis, cliquable ci-dessus).</p>' + _delivery_box()
              + "<p>Nous restons à votre disposition et vous souhaitons une excellente dégustation.</p><p>Très cordialement,<br><strong>Jean-Sébastien CHEUNG-AH-SEUNG</strong><br>oWine</p>")
    return _shell(f"Commande {o.name} · enlèvement réservé", inner)


def export_alix_instructions(o, cs, exp: dict) -> str:
    """Consignes de préparation propres à l'international (texte, pour l'e-mail Alix)."""
    cname = _export.X.country_name(o.country); n = len(cs); ships = exp["shipments"]
    lines = [f"ENVOI INTERNATIONAL — {cname} ({exp['zone_label']}) — produit Chronopost : {exp['product_label'].split(' ·')[0]}."]
    if exp["customs"]:
        if len(ships) > 1:
            lines.append(f"Chaque carton est un ENVOI SÉPARÉ (limite {cname} : 6 bouteilles / 10 kg par envoi) : imprimer la facture commerciale de CHAQUE carton en 3 exemplaires (PDF joint, un bloc par carton) "
                         "et glisser les 3 exemplaires dans une pochette Chronopost (réf. 2010) collée sur le carton concerné, à côté de l'étiquette.")
        else:
            lines.append("Imprimer la facture commerciale en 3 EXEMPLAIRES (PDF joint, 3 pages identiques) et glisser les 3 exemplaires dans une pochette Chronopost (réf. 2010) collée sur le colis A "
                         + (f"(1/{n}), à côté de l'étiquette. Rien sur les autres colis. " if n > 1 else ", à côté de l'étiquette. "))
            if n > 1:
                lines.append(f"Coller sur chaque colis le sticker multi-pièces Chronopost avec sa position dans le groupage (1/{n}, 2/{n}…) : colis " + ", ".join(f"{c.ref} = {i + 1}/{n}" for i, c in enumerate(cs)) + ".")
        lines.append("Ne pas utiliser de caisse en bois ; étiquette collée à plat sur le dessus, jamais sur une arête (code-barres).")
    else:
        lines.append("Pas de document douanier (Union européenne) : lettre de transport seule sur chaque colis" + (f" ; sticker multi-pièces 1/{n}, 2/{n}… à côté de l'étiquette." if n > 1 else "."))
    return "\n".join(lines)


def export_sheet(o, cs, exp: dict) -> dict:
    """Compléments de la fiche de saisie chronopost.fr pour l'international : produit, valeur en douane, description, dimensions, incoterm, EORI."""
    s = exp["settings"]
    return {"product": exp["product_label"].split(" ·")[0], "product_code": _export.X.PRODUCTS[exp["product"]]["code"], "incoterm": exp["incoterm"], "eori": s.get("eori") or "(à renseigner dans Réglages douane)",
            "customs_value": exp["totals"]["goods_ht"], "content": "Vin de Bourgogne AOP en bouteilles (75 cl) / Burgundy PDO wine", "dims": {k: (s.get("box_dims") or {}).get(k) or "à mesurer (Réglages douane)" for k in ("2031", "2033", "2036")},
            "shipments": [{"suffix": sh["suffix"] or "—", "cartons": [c.ref for c in sh["cartons"]], "bottles": sum(int(l["qty"]) for c in sh["cartons"] for l in service.carton_lines(c)), "kg": round(sum(c.weight_kg for c in sh["cartons"]), 1),
                           "value": round(sum(l["total_ht"] for l in exp["totals"]["lines"]) * (sum(int(l["qty"]) for c in sh["cartons"] for l in service.carton_lines(c)) / max(exp["totals"]["bottles"], 1)), 2)} for sh in exp["shipments"]],
            "customs": exp["customs"], "invoice_desc": exp["invoice_desc"]}
