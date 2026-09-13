"""CIOP — production du dossier : tableau « investissements acquis » (PDF A4 paysage au format JS + Excel),
CERFA 2083-SD pré-rempli (identité, exercice, associés, déclarant ; page II remplacée par le tableau),
feuille de cadrage, factures renommées, le tout dans un ZIP."""
import io
import os
import zipfile
from datetime import date, datetime
from typing import List, Tuple

import fitz

from . import service

ASSET = os.path.join(os.path.dirname(__file__), "assets", "2083-sd_2026.pdf")
FONT, FONTB, FONTI = "helv", "hebo", "heit"


def eur(x: float) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return s + " €"


def _lines_ok(ex: dict) -> List[dict]:
    return [l for l in ex["lines"] if l.get("include", True)]


# ----------------------------------------------------------------- tableau (format JS)
COLS = [("Code\ninvest.", 26, "l"), ("Nature de l'investissement", 232, "l"), ("Fournisseur", 62, "l"), ("Date Facture", 44, "l"), ("No. Facture", 60, "l"),
        ("Article du\nCGI", 42, "l"), ("Lieu d'exploitation", 150, "l"), ("Date de début\nd'exploitation", 48, "r"), ("Montant de\nl'invest. (HT)", 50, "r"),
        ("Base de\nl'avantage\nfiscal", 48, "r"), ("Taux de la\nréduction\nd'impôt", 36, "r"), ("Montant de\nla réduction\nd'impôt", 50, "r")]


def table_rows(cfg: dict, ex: dict) -> List[dict]:
    p = cfg["params"]
    out = []
    for l in _lines_ok(ex):
        site = service.site_of(cfg, l.get("site") or "")
        out.append({"code": p["code_invest"], "label": l["label"], "supplier": l["supplier"], "date": service._fr(l["date"]), "number": l["invoice_number"] or l["piece"],
                    "article": p["article"], "place": site.get("address") or site.get("label") or "", "start": l.get("start") or service._fr(l["date"]),
                    "amount": l["amount"], "base": l["amount"], "rate": f"{int(p['rate_pct'])}%", "reduction": service.reduction(cfg, l["amount"])})
    return out


def table_pdf(cfg: dict, ex: dict) -> bytes:
    rows = table_rows(cfg, ex)
    doc = fitz.open()
    W, H = 842, 595
    x0, y_top = 40, 60
    total_w = sum(c[1] for c in COLS)
    scale = (W - 2 * x0) / total_w
    widths = [c[1] * scale for c in COLS]

    def new_page():
        pg = doc.new_page(width=W, height=H)
        pg.insert_text((x0, 50), "II - INVESTISSEMENTS ACQUIS", fontname=FONTB, fontsize=11)
        # en-tête
        y = y_top
        hh = 24
        x = x0
        for (title, _, al), w in zip(COLS, widths):
            pg.draw_rect(fitz.Rect(x, y, x + w, y + hh), color=(0, 0, 0), width=0.4)
            lines = title.split("\n")
            for i, t in enumerate(lines):
                tw = fitz.get_text_length(t, fontname=FONTB, fontsize=5.6)
                tx = x + 2 if al == "l" else x + w - 2 - tw
                pg.insert_text((tx, y + hh - 2 - (len(lines) - 1 - i) * 6.2), t, fontname=FONTB, fontsize=5.6)
            x += w
        return pg, y + hh

    pg, y = new_page()
    rh = 9.2
    for r in rows:
        if y + rh > H - 60:
            pg, y = new_page()
        vals = [r["code"], r["label"], r["supplier"], r["date"], r["number"], r["article"], r["place"], r["start"], eur(r["amount"]), eur(r["base"]), r["rate"], eur(r["reduction"])]
        x = x0
        for v, (_, _, al), w in zip(vals, COLS, widths):
            pg.draw_rect(fitz.Rect(x, y, x + w, y + rh), color=(0, 0, 0), width=0.3)
            fs = 6.0
            while fitz.get_text_length(v, fontname=FONT, fontsize=fs) > w - 4 and fs > 4.2:
                fs -= 0.2
            tw = fitz.get_text_length(v, fontname=FONT, fontsize=fs)
            tx = x + 2 if al == "l" else x + w - 2 - tw
            pg.insert_text((tx, y + rh - 2.4), v, fontname=FONT, fontsize=fs)
            x += w
        y += rh
    # totaux (non arrondis, mention « arrondi à l'euro » dessous, comme le modèle)
    t_amt = round(sum(r["amount"] for r in rows), 2); t_red = round(sum(r["reduction"] for r in rows), 2)
    xs = [x0 + sum(widths[:i]) for i in range(len(widths))]
    y += 8
    for i, v in ((8, eur(t_amt)), (9, eur(t_amt)), (11, eur(t_red))):
        tw = fitz.get_text_length(v, fontname=FONTB, fontsize=6.2)
        pg.insert_text((xs[i] + widths[i] - 2 - tw, y), v, fontname=FONTB, fontsize=6.2)
        n = "(arrondi à l'euro)"
        tw = fitz.get_text_length(n, fontname=FONTI, fontsize=5.4)
        pg.insert_text((xs[i] + widths[i] - 2 - tw, y + 7), n, fontname=FONTI, fontsize=5.4)
    return doc.tobytes()


def table_xlsx(cfg: dict, ex: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    rows = table_rows(cfg, ex)
    wb = Workbook(); ws = wb.active; ws.title = "Investissements acquis"
    ws["A1"] = "II - INVESTISSEMENTS ACQUIS"; ws["A1"].font = Font(bold=True, size=12)
    heads = [c[0].replace("\n", " ") for c in COLS]
    ws.append([]); ws.append(heads)
    thin = Side(style="thin", color="999999")
    for c in ws[3]:
        c.font = Font(bold=True, size=8); c.alignment = Alignment(wrap_text=True, vertical="bottom"); c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
    for r in rows:
        ws.append([r["code"], r["label"], r["supplier"], r["date"], r["number"], r["article"], r["place"], r["start"], r["amount"], r["base"], float(cfg["params"]["rate_pct"]) / 100, r["reduction"]])
    n = ws.max_row
    for row in ws.iter_rows(min_row=4, max_row=n):
        for c in row:
            c.font = Font(size=8); c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
        row[8].number_format = row[9].number_format = row[11].number_format = '#,##0.00 €'; row[10].number_format = "0%"
    ws.append([]); ws.append(["", "", "", "", "", "", "", "TOTAL", f"=SUM(I4:I{n})", f"=SUM(J4:J{n})", "", f"=SUM(L4:L{n})"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True, size=8)
    ws[f"I{ws.max_row}"].number_format = ws[f"J{ws.max_row}"].number_format = ws[f"L{ws.max_row}"].number_format = '#,##0.00 €'
    ws.append(["", "", "", "", "", "", "", "", "(arrondi à l'euro)", "(arrondi à l'euro)", "", "(arrondi à l'euro)"])
    for col, w in zip("ABCDEFGHIJKL", (7, 52, 16, 12, 16, 13, 40, 13, 14, 14, 9, 14)):
        ws.column_dimensions[col].width = w
    ws.page_setup.orientation = "landscape"; ws.page_setup.fitToWidth = 1
    bio = io.BytesIO(); wb.save(bio); return bio.getvalue()


# ----------------------------------------------------------------- CERFA pré-rempli
def _fit(page, rect: fitz.Rect, text: str, size: float = 9, font: str = FONT, align: str = "l", valign: str = "m"):
    """Texte dans une cellule (réduit la taille si trop long, gère les retours à la ligne)."""
    lines = (text or "").split("\n")
    fs = size
    while fs > 5 and any(fitz.get_text_length(t, fontname=font, fontsize=fs) > rect.width - 6 for t in lines):
        fs -= 0.5
    lh = fs * 1.2
    total_h = lh * len(lines)
    y = rect.y0 + (rect.height - total_h) / 2 + fs if valign == "m" else rect.y0 + fs + 3
    for t in lines:
        tw = fitz.get_text_length(t, fontname=font, fontsize=fs)
        x = rect.x0 + 3 if align == "l" else rect.x0 + rect.width - 3 - tw
        page.insert_text((x, y), t, fontname=font, fontsize=fs)
        y += lh


def _digits(page, xs: List[float], y0: float, y1: float, s: str):
    for x0, x1, ch in zip(xs[:-1], xs[1:], s):
        _fit(page, fitz.Rect(x0, y0, x1, y1), ch, size=9, align="l")
        # centré : recalcul simple
    return


def cerfa_pdf(cfg: dict, ex: dict, table_page: bytes, fy_start: date, fy_end: date) -> bytes:
    """Pages du formulaire (1 à 5) : page 1 et page 5 complétées, page 2 remplacée par le tableau."""
    src = fitz.open(ASSET)
    out = fitz.open()
    out.insert_pdf(src, from_page=0, to_page=0)
    p1 = out[0]
    # exercice : cases (x des séparateurs relevés sur le formulaire 2026)
    du = [311.9, 326.2, 340.1, 354.4, 368.3, 382.6, 397.0, 410.8, 425.2]
    au = [453.4, 467.7, 481.6, 495.9, 510.3, 524.7, 538.5, 552.8, 566.7]
    for xs, d in ((du, fy_start), (au, fy_end)):
        for (a, b), ch in zip(zip(xs[:-1], xs[1:]), d.strftime("%d%m%Y")):
            tw = fitz.get_text_length(ch, fontname=FONT, fontsize=9)
            p1.insert_text(((a + b) / 2 - tw / 2, 409.5), ch, fontname=FONT, fontsize=9)
    idt = cfg["identity"]
    _fit(p1, fitz.Rect(200.6, 477.2, 567.0, 525.6), idt.get("name", ""), size=10)
    _fit(p1, fitz.Rect(200.6, 525.6, 395.6, 570.8), idt.get("address", ""), size=9)
    _fit(p1, fitz.Rect(480.4, 525.6, 567.0, 570.8), idt.get("siren", ""), size=9)
    _fit(p1, fitz.Rect(200.6, 570.8, 395.6, 616.0), idt.get("legal_form", ""), size=9)
    _fit(p1, fitz.Rect(480.4, 570.8, 567.0, 616.0), idt.get("ape", ""), size=9)
    ys = [713.1, 730.1, 747.2, 764.2, 781.3]
    for (y0, y1), pr in zip(zip(ys[:-1], ys[1:]), cfg.get("partners", [])[:4]):
        _fit(p1, fitz.Rect(28.4, y0, 211.7, y1), pr.get("name", ""), size=8)
        _fit(p1, fitz.Rect(211.7, y0, 417.8, y1), pr.get("address", ""), size=7)
        _fit(p1, fitz.Rect(417.8, y0, 502.6, y1), pr.get("siren", ""), size=8)
        _fit(p1, fitz.Rect(502.6, y0, 567.0, y1), str(pr.get("share", "")), size=8)
    # page II = tableau
    out.insert_pdf(fitz.open(stream=table_page, filetype="pdf"))
    # pages 3 à 5 du formulaire
    out.insert_pdf(src, from_page=2, to_page=4)
    p5 = out[len(out) - 1]
    dc = cfg.get("declarant", {})
    _fit(p5, fitz.Rect(146.3, 565.6, 448.6, 581.5), dc.get("name", ""), size=8)
    _fit(p5, fitz.Rect(146.3, 581.5, 448.6, 597.4), dc.get("quality", ""), size=7)
    _fit(p5, fitz.Rect(522.0, 565.6, 848.0, 597.4), dc.get("address", ""), size=8)
    _fit(p5, fitz.Rect(189.6, 597.4, 449.0, 613.4), dc.get("place", ""), size=8)
    if dc.get("date"):
        _fit(p5, fitz.Rect(522.4, 597.4, 639.1, 613.4), dc["date"], size=8)
    if dc.get("signature_note"):
        _fit(p5, fitz.Rect(724.7, 597.4, 849.2, 613.4), dc["signature_note"], size=5.5)
    return out.tobytes()


# ----------------------------------------------------------------- cadrage
def cadrage_xlsx(cfg: dict, ex: dict, files: List[Tuple[str, dict]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    t = service.totals(cfg, ex)
    wb = Workbook(); ws = wb.active; ws.title = "Cadrage"
    ws.append([f"Cadrage CIOP {cfg['identity'].get('name', '')} — exercice du {service._fr(ex['fy_start'])} au {service._fr(ex['fy_end'])}"]); ws["A1"].font = Font(bold=True, size=12)
    ws.append([f"Généré par Vaelan le {datetime.now():%d/%m/%Y %H:%M} — comptes : " + ", ".join(sorted({a['number'] for a in ex.get('accounts', [])})) + f" — journaux d'achats : {', '.join(cfg['params']['purchase_journals'])}"])
    ws.append([])
    ws.append(["1. Comptes CIOP, journaux d'achats, exercice (Pennylane)", t["comptes_achats"]])
    ws.append(["   dont lignes écartées du tableau (voir onglet Écartées)", -t["exclues"]])
    ws.append(["2. Tableau « Investissements acquis »", t["tableau"]])
    ws.append(["3. Factures archivées (dossier Factures)", t["factures"]])
    ws.append(["Réduction d'impôt (35 %)", t["reduction"]])
    ws.append(["Cadré", "OUI" if t["ok"] else "NON"])
    for r in range(4, 10):
        ws.cell(row=r, column=1).font = Font(bold=(r in (4, 6, 7, 9))); ws.cell(row=r, column=2).number_format = '#,##0.00 €'
    ws.append([])
    ws.append(["Autres mouvements sur les comptes CIOP dans l'exercice (hors journaux d'achats, hors à-nouveaux) : information", t["autres_mouvements"]])
    ws.cell(row=ws.max_row, column=2).number_format = '#,##0.00 €'
    ws.column_dimensions["A"].width = 95; ws.column_dimensions["B"].width = 18
    w2 = wb.create_sheet("Lignes")
    w2.append(["Date", "Compte", "Journal", "Fournisseur", "N° facture", "Pièce Pennylane", "Montant HT", "Établissement", "Libellé", "Fichier archivé", "Écriture (id)"])
    for l in _lines_ok(ex):
        fn = next((f for f, ll in files if ll["key"] == l["key"]), "")
        w2.append([service._fr(l["date"]), l["account"], l["journal"], l["supplier"], l["invoice_number"], l["piece"], l["amount"], service.site_of(cfg, l.get("site") or "").get("label", ""), l["label"], fn, l["entry_id"]])
    for c in w2[1]:
        c.font = Font(bold=True)
    w3 = wb.create_sheet("Écartées")
    w3.append(["Date", "Compte", "Fournisseur", "N° facture", "Montant HT", "Motif"])
    for l in ex["lines"]:
        if not l.get("include", True):
            w3.append([service._fr(l["date"]), l["account"], l["supplier"], l["invoice_number"], l["amount"], l.get("note", "")])
    w4 = wb.create_sheet("Autres mouvements")
    w4.append(["Date", "Journal", "Compte", "Montant", "Libellé", "Écriture (id)"])
    for o in ex.get("other_moves", []):
        if o["journal"] != "AN":
            w4.append([service._fr(o["date"]), o["journal"], o["account"], o["amount"], o["label"], o["entry_id"]])
    for w in (w2, w3, w4):
        for col in "ABCDEFGHIJK":
            w.column_dimensions[col].width = 18
    bio = io.BytesIO(); wb.save(bio); return bio.getvalue()


# ----------------------------------------------------------------- dossier complet
def build_zip(company_code: str, fy_end: date, log=None) -> Tuple[bytes, dict]:
    log = log or (lambda m: None)
    cfg = service.get_config(company_code)
    ex = service.get_exercise(company_code, fy_end)
    if not ex.get("lines"):
        raise RuntimeError("Aucune ligne collectée : relire Pennylane d'abord.")
    fy_start = date.fromisoformat(ex["fy_start"]); fy_end = date.fromisoformat(ex["fy_end"])
    name = cfg["identity"].get("name") or company_code
    stamp = datetime.now().strftime("%Y%m%d")
    lab = service.fy_label(fy_end)
    files = []
    zbio = io.BytesIO()
    with zipfile.ZipFile(zbio, "w", zipfile.ZIP_DEFLATED) as z:
        for l in _lines_ok(ex):
            data = service.download(l["attachment_url"]) if l.get("attachment_url") else None
            if not data:
                log(f"pièce manquante : {l['supplier']} {l['invoice_number']}")
                l["has_file"] = False
                continue
            pdf, _ = service.to_pdf(data, l.get("attachment_name") or "")
            fn = service.file_name(l)
            z.writestr(f"Factures/{fn}", pdf)
            files.append((fn, l))
            l["has_file"] = True
        ex["totals"] = service.totals(cfg, ex)
        tpdf = table_pdf(cfg, ex)
        z.writestr(f"{stamp} 01 {name} Investissements acquis CIOP {lab}.pdf", tpdf)
        z.writestr(f"{stamp} 01 {name} Investissements acquis CIOP {lab}.xlsx", table_xlsx(cfg, ex))
        z.writestr(f"{stamp} 02 2083-SD {name} EX CLOS {fy_end:%d-%m-%Y} (pre-rempli).pdf", cerfa_pdf(cfg, ex, tpdf, fy_start, fy_end))
        z.writestr(f"{stamp} 03 Cadrage CIOP {name} {lab}.xlsx", cadrage_xlsx(cfg, ex, files))
    ex["built_at"] = datetime.utcnow().isoformat(timespec="seconds")
    service.save_exercise(company_code, fy_end, ex)
    log(f"{len(files)} facture(s) archivée(s), tableau {service.totals(cfg, ex)['tableau']:.2f} €, cadrage {'OK' if ex['totals']['ok'] else 'À VÉRIFIER'}")
    return zbio.getvalue(), ex["totals"]
