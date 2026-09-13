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


# ---------------------------------------------------------------- liste de colisage (format « Détail OWxxxx »)
def packing_list_pdf(o, cs) -> bytes:
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    y = 60
    pg.insert_text((50, y), f"Liste de colisage de la commande {o.name}", fontname="hebo", fontsize=14); y += 18
    pg.insert_text((50, y), f"MAJ {service.now_local():%d/%m/%Y}", fontname="helv", fontsize=9); y += 10
    if o.customer:
        pg.insert_text((50, y + 10), f"Destinataire : {o.customer}{' · ' + o.city if o.city else ''}", fontname="helv", fontsize=9); y += 12
    y += 16
    cols = [("Ref.", 50, 40), ("Etiquette", 90, 110), ("Désignation", 200, 250), ("Qté", 450, 40), ("Btl / CRT", 495, 60)]
    for t, x, w in cols:
        pg.insert_text((x + 2, y), t, fontname="hebo", fontsize=9)
    pg.draw_line((50, y + 4), (555, y + 4), width=0.6); y += 16
    total_b = 0
    for c in cs:
        lines = service.carton_lines(c); nb = sum(int(l["qty"]) for l in lines); total_b += nb
        first = True
        for l in lines:
            if y > 790:
                pg = doc.new_page(width=595, height=842); y = 60
            if first:
                pg.insert_text((52, y), c.ref, fontname="hebo", fontsize=9)
                pg.insert_text((92, y), c.tracking or "—", fontname="helv", fontsize=8)
                pg.insert_text((497, y), str(nb), fontname="hebo", fontsize=9)
            title = l.get("title") or l["sku"]
            fs = 8.5
            while fitz.get_text_length(title, fontname="helv", fontsize=fs) > 245 and fs > 6: fs -= 0.5
            pg.insert_text((202, y), title, fontname="helv", fontsize=fs)
            pg.insert_text((452, y), str(int(l["qty"])), fontname="helv", fontsize=9)
            y += 13; first = False
        pg.draw_line((50, y - 4), (555, y - 4), width=0.3, color=(0.6, 0.6, 0.6)); y += 4
    pg.insert_text((452, y + 4), str(total_b), fontname="hebo", fontsize=9); pg.insert_text((497, y + 4), str(total_b), fontname="hebo", fontsize=9)
    y += 22
    pg.insert_text((50, y), f"TOTAL : {len(cs)} CARTON{'S' if len(cs) > 1 else ''} | {total_b} BOUTEILLE{'S' if total_b > 1 else ''}", fontname="hebo", fontsize=10)
    if o.mode == "chronopost":
        y += 24
        pg.insert_text((50, y), "Attention : respecter le contenu de chaque carton selon l'étiquette référencée.", fontname="heit", fontsize=8.5)
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
