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
        foot = "oWine SAS · Parc d'activité, 14 E rue Coubertin, 21000 Dijon · www.owine.co · js@owine.co"
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
    if o.mode == "chronopost":
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
        pg.insert_text((M + 30, y + 14), f"Carton {c.ref} · {nb} bouteille{'s' if nb > 1 else ''}", fontname="hebo", fontsize=9.5, color=DARK)
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
            ("Écrivez toute anomalie sur le bon de livraison, avant de signer", "Bouteille cassée ou manquante, carton abîmé ou humide : notez-le en toutes lettres, par exemple « 1 bouteille cassée, carton B »."),
            ("Photographiez et prévenez-nous", "Colis, bouteilles et bon de livraison annoté, envoyés à js@owine.co : nous ouvrons la réclamation et remplaçons ce qui doit l'être."),
        ]
        why = ("Pourquoi c'est essentiel : Chronopost n'accepte une réclamation que si les réserves figurent sur le bon de livraison au moment de la remise. "
               "Un bon signé sans réserve vaut acceptation d'un colis complet et en bon état ; plus aucun recours n'est ensuite possible, ni pour vous, ni pour nous. "
               "Ces règles sont celles du transporteur : en les suivant, vous nous permettez de vous garantir un remplacement en cas de problème.")
        box_h = 34 + len(steps) * 40 + 62
        if y + box_h > H - 50:
            pg = new_page(); y = 60
        pg.draw_rect(fitz.Rect(M, y, W - M, y + box_h), color=WINE, fill=(0.99, 0.975, 0.975), width=0.8)
        pg.insert_text((M + 14, y + 20), "À LA LIVRAISON : TROIS RÉFLEXES QUI VOUS PROTÈGENT", fontname="hebo", fontsize=9.5, color=WINE)
        yy = y + 34
        for i, (title, detail) in enumerate(steps, 1):
            pg.draw_circle((M + 22, yy + 6), 7.5, color=None, fill=DARK)
            pg.insert_text((M + 19.2, yy + 9.2), str(i), fontname="hebo", fontsize=8.5, color=(1, 1, 1))
            pg.insert_text((M + 36, yy + 9), title, fontname="hebo", fontsize=9, color=DARK)
            r = pg.insert_textbox(fitz.Rect(M + 36, yy + 13, W - M - 12, yy + 40), detail, fontname="helv", fontsize=8.2, color=(0.2, 0.2, 0.2), lineheight=1.15)
            assert r >= 0, "texte de l'encart trop long"
            yy += 40
        r = pg.insert_textbox(fitz.Rect(M + 14, yy + 2, W - M - 12, yy + 60), why, fontname="heit", fontsize=8, color=GREY, lineheight=1.15)
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
        body = (f"Bonjour,\n\nJe vous prie de trouver ci-joint le détail et les étiquettes d'envoi pour cette nouvelle commande à préparer ({desc}, {nb} bouteille{'s' if nb > 1 else ''}).\n\n"
                "Rappel : Attention comme toujours à bien respecter le contenu de chaque carton selon l'étiquette référencée.\n\n"
                f"L'enlèvement a été réservé sur le créneau suivant :\n\n{slot}\n\nEn vous remerciant pour votre confirmation une fois que ce sera prêt.\n\nA bientôt,")
        subject = f"Commande OWINE #{o.name}"
    return {"to": [config.ALIX_EMAIL], "cc": config.ALIX_CC, "subject": subject, "body": body}


def email_client(o, cs) -> dict:
    first = (o.customer or "").split(" ")[0]
    if o.mode == "retrait":
        body = (f"Bonjour {first},\n\nJ'ai le plaisir de vous confirmer que votre commande {o.name} sera prête pour être collectée à notre entrepôt "
                f"à partir du {fr_date(o.pickup_date) if o.pickup_date else '(date à confirmer)'}.\n\n"
                "Je vous invite à vous rendre à l'adresse suivante muni de la confirmation ci-jointe et de votre pièce d'identité, aux horaires d'ouverture : "
                "8h-12h / 13h30-17h (sauf le vendredi 16h30) :\n\n"
                f"{config.ALIX_ADDRESS} · {config.ALIX_EMAIL} · 03 73 55 41 35\n\nEn vous souhaitant bonne réception,\n\nTrès cordialement,")
        subject = f"Commande oWine {o.name} prête à être récupérée"
    else:
        deliv = o.delivery_date or (next_business_day(o.pickup_date) if o.pickup_date else None)
        body = (f"Bonjour {first},\n\nJ'ai le plaisir de vous confirmer que l'enlèvement de votre commande par Chronopost est programmé pour le "
                f"{fr_date(o.pickup_date) if o.pickup_date else '(date à confirmer)'}. Vous trouverez ci-jointes les étiquettes d'envoi correspondantes.\n\n"
                f"La livraison est ainsi prévue pour le {fr_date(deliv) if deliv else '(à confirmer)'} ou le lendemain (sous réserve des délais de transport).\n\n"
                "⚠️ AVERTISSEMENT IMPORTANT\n\nAu moment de la livraison, nous vous recommandons vivement d'ouvrir le carton avant de signer afin de vérifier que :\n"
                "- les bouteilles sont intactes ;\n- le nombre de bouteilles correspond bien à votre commande (cf. liste de colisage ci-jointe pour le détail du contenu de chaque carton).\n\n"
                "Conformément à la politique de Chronopost (que nous sommes tenus d'appliquer), toute bouteille cassée ou manquante, ou tout colis visiblement abîmé, "
                "doit impérativement être signalé sur le bon de livraison avant signature.\n\n"
                "👉 Si le bon de livraison est signé sans réserve, Chronopost considère le colis comme complet et en bon état et ne permet plus d'ouvrir de réclamation par la suite. "
                "Dans ce cas, nous ne pourrons malheureusement plus intervenir auprès d'eux.\n\n"
                "En cas de problème (bouteille cassée, manquante, carton humide ou abîmé, etc.), il suffit donc de :\n- le mentionner clairement sur le bon de livraison ;\n- prendre des photos et nous les transmettre.\n\n"
                "Nous restons bien entendu à votre disposition pour toute question et vous souhaitons une excellente réception et une très belle journée.\n\nTrès cordialement,")
        subject = f"Confirmation de planification d'enlèvement de votre commande {o.name}"
    return {"to": [o.email] if o.email else [], "cc": [], "subject": subject, "body": body}


def bundle_zip(o, cs, labels: List[Tuple[str, bytes]] = None) -> bytes:
    """Tout ce qu'il faut joindre : liste de colisage, fichier Alix, étiquettes renommées « XN…FR (A).pdf », textes des e-mails."""
    import zipfile
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"Détail {o.name}.pdf", packing_list_pdf(o, cs))
        z.writestr(f"{o.name}.xlsx", alix_xlsx(o, cs))
        for name, data in (labels or []):
            z.writestr(f"Etiquettes/{name}", data)
        ea, ec = email_alix(o, cs), email_client(o, cs)
        z.writestr("E-mail Alix.txt", f"À : {', '.join(ea['to'])}\nCc : {', '.join(ea['cc'])}\nObjet : {ea['subject']}\n\n{ea['body']}")
        z.writestr("E-mail client.txt", f"À : {', '.join(ec['to'])}\nObjet : {ec['subject']}\n\n{ec['body']}")
    return bio.getvalue()
