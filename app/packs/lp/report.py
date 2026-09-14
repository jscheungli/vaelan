"""LP — PDF du prévisionnel et de l'analyse des écarts (pymupdf). Charte sobre : Gelasio (métrique Georgia,
police libre embarquée), marine #0A2540 pour les chiffres clés, filets fins, logo Anvael en tête (option)."""
import os
from datetime import datetime, timedelta

import fitz

from . import config, engine

_STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "web", "static")
_FONTS = {"ge": os.path.join(_STATIC, "fonts", "Gelasio-Regular.ttf"), "geb": os.path.join(_STATIC, "fonts", "Gelasio-Bold.ttf"),
          "gei": os.path.join(_STATIC, "fonts", "Gelasio-Italic.ttf")}
_FALLBACK = {"ge": "tiro", "geb": "tibo", "gei": "tiit"}
LOGO = os.path.join(_STATIC, "anvael_logo.png")
NAVY = (0.039, 0.145, 0.251)
BLACK = (0.05, 0.05, 0.05)
GREY = (0.42, 0.42, 0.42)
LIGHT = (0.82, 0.82, 0.82)
PALE = (0.955, 0.96, 0.97)
RED = (0.72, 0.16, 0.16)
GREEN = (0.13, 0.45, 0.28)
_HAVE_FONTS = all(os.path.exists(p) for p in _FONTS.values())
_FONT_OBJ = {k: fitz.Font(fontfile=p) for k, p in _FONTS.items()} if _HAVE_FONTS else {}


def k(v, signed=False) -> str:
    if v is None:
        return "—"
    if abs(v) < 500:
        return "0"
    s = f"{abs(v)/1000:,.0f}".replace(",", " ")
    if v < 0:
        return "-" + s
    return ("+" + s) if signed and v > 0 else s


def mio(v) -> str:
    return "—" if v is None else f"{v/1e6:,.2f} M".replace(",", " ").replace(".", ",")


def pct(v, digits=1, signed=False) -> str:
    if v is None:
        return "—"
    s = f"{v*100:.{digits}f}".replace(".", ",") + " %"
    return ("+" + s) if signed and v > 0 else s


def _ascii(s: str) -> str:
    return (str(s).replace("—", "-").replace("–", "-").replace("→", "->").replace("€", "EUR").replace("…", "...")
            .replace("«", '"').replace("»", '"').replace("’", "'"))


class Pdf:
    def __init__(self):
        self.doc = fitz.open()
        self.page = None
        self.W = self.H = 0
        self.y = 0
        self.n = 0
        self.footer = ""
        self._fonts_on_page = set()

    # ---- pages
    def new_page(self, landscape=False, top=56):
        self.page = self.doc.new_page(width=792 if landscape else 612, height=612 if landscape else 792)
        self.W, self.H = self.page.rect.width, self.page.rect.height
        self.M = 40 if landscape else 60
        self.y = top
        self.n += 1
        self._fonts_on_page = set()
        return self.page

    def _font(self, key):
        if not _HAVE_FONTS:
            return _FALLBACK[key]
        if key not in self._fonts_on_page:
            self.page.insert_font(fontname=key, fontfile=_FONTS[key])
            self._fonts_on_page.add(key)
        return key

    def width(self, s, size, font="ge"):
        s = s if _HAVE_FONTS else _ascii(s)
        if _HAVE_FONTS:
            return _FONT_OBJ[font].text_length(s, fontsize=size)
        return fitz.get_text_length(s, fontname=_FALLBACK[font], fontsize=size)

    def text(self, x, y, s, size=9.5, font="ge", color=BLACK):
        s = s if _HAVE_FONTS else _ascii(s)
        self.page.insert_text((x, y), s, fontsize=size, fontname=self._font(font), color=color)

    def right(self, xr, y, s, size=9.5, font="ge", color=BLACK):
        self.text(xr - self.width(s, size, font), y, s, size, font, color)

    def center(self, xc, y, s, size=9.5, font="ge", color=BLACK):
        self.text(xc - self.width(s, size, font) / 2, y, s, size, font, color)

    def hline(self, y, x1=None, x2=None, width=0.6, color=LIGHT, dashes=None):
        self.page.draw_line((x1 if x1 is not None else self.M, y), (x2 if x2 is not None else self.W - self.M, y), color=color, width=width, dashes=dashes)

    def wrap(self, s, w, size, font="ge"):
        lines, cur = [], ""
        for wd in str(s).split(" "):
            t = (cur + " " + wd).strip()
            if not cur or self.width(t, size, font) <= w:
                cur = t
            else:
                lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        return lines

    def para(self, s, size=9.5, font="ge", color=BLACK, lh=1.3, x=None, w=None, indent=0):
        """Paragraphe avec retour à la ligne (césure mot à mot) ; avance y."""
        x = self.M if x is None else x
        w = (self.W - self.M - x) if w is None else w
        for line in self.wrap(s, w - indent, size, font):
            self.ensure(size * lh + 2)
            self.text(x + indent, self.y, line, size, font, color)
            self.y += size * lh
        self.y += 2

    def ensure(self, space):
        if self.y + space > self.H - 52:
            self.finish_page()
            self.new_page(landscape=self.W > self.H)

    def finish_page(self):
        if not self.page:
            return
        self.hline(self.H - 40, width=0.5)
        self.center(self.W / 2, self.H - 27, self.footer, 8, "ge", GREY)
        self.right(self.W - self.M, self.H - 27, f"{self.n}", 8, "ge", GREY)

    def bytes(self):
        self.finish_page()
        try:
            self.doc.subset_fonts()
        except Exception:
            pass
        return self.doc.tobytes(garbage=3, deflate=True)

    # ---- blocs
    def header(self, title, subtitle, lines, branding=True):
        y0 = 40
        if branding and os.path.exists(LOGO):
            self.page.insert_image(fitz.Rect(self.W / 2 - 62, y0 - 6, self.W / 2 + 62, y0 + 44), filename=LOGO, keep_proportion=True)
            y0 += 62
        self.center(self.W / 2, y0, title, 14, "geb")
        self.center(self.W / 2, y0 + 17, subtitle, 11, "geb")
        yy = y0 + 32
        for ln in lines:
            self.center(self.W / 2, yy, ln, 9.5, "ge", GREY)
            yy += 13
        if branding:
            self.center(self.W / 2, yy, "Livré par Anvael via l'outil Vaelan", 8.5, "gei", GREY)
            yy += 12
        self.hline(yy + 4, width=0.7, color=GREY)
        self.y = yy + 24

    def section(self, title):
        self.ensure(30)
        self.text(self.M, self.y, title.upper(), 9.5, "geb", NAVY)
        self.hline(self.y + 5, width=0.5)
        self.y += 20

    def kpis(self, items, h=52):
        """items = [(label, value, sub)] — boîtes marine sur fond pâle."""
        n = len(items)
        w = (self.W - 2 * self.M - (n - 1) * 8) / n
        for i, (lab, val, sub) in enumerate(items):
            x = self.M + i * (w + 8)
            self.page.draw_rect(fitz.Rect(x, self.y, x + w, self.y + h), color=None, fill=PALE)
            self.text(x + 8, self.y + 14, lab, 7.5, "ge", GREY)
            self.text(x + 8, self.y + 33, val, 13, "geb", NAVY)
            if sub:
                self.text(x + 8, self.y + 45, sub, 7.5, "ge", GREY)
        self.y += h + 16

    def table(self, cols, rows, widths, aligns=None, size=8.2, header=True, bold_rows=(), grey_rows=(), zebra=False, lh=13):
        """cols = en-têtes ; rows = listes de chaînes ; widths en pt ; aligns 'l'/'r'."""
        aligns = aligns or ["l"] + ["r"] * (len(cols) - 1)
        xs = [self.M]
        for w in widths[:-1]:
            xs.append(xs[-1] + w)

        def draw_row(vals, font, color, y):
            for j, v in enumerate(vals):
                if aligns[j] == "r":
                    self.right(xs[j] + widths[j] - 3, y, str(v), size, font, color)
                else:
                    self.text(xs[j] + 2, y, str(v), size, font, color)
        if header:
            self.ensure(lh * 2)
            draw_row(cols, "geb", GREY, self.y)
            self.hline(self.y + 4, width=0.5, color=GREY)
            self.y += lh
        for i, r in enumerate(rows):
            self.ensure(lh)
            if zebra and i % 2 == 1:
                self.page.draw_rect(fitz.Rect(self.M, self.y - lh + 4, self.W - self.M, self.y + 4), color=None, fill=PALE)
            bold = i in bold_rows
            if bold:
                self.hline(self.y - lh + 4, width=0.4)
            draw_row(r, "geb" if bold else "ge", NAVY if bold else (GREY if i in grey_rows else BLACK), self.y)
            self.y += lh
        self.y += 6

    def line_chart(self, months, series, h=150, threshold=None, unit="k"):
        """series = [(label, values, color, width, dashes)]."""
        self.ensure(h + 30)
        x0, x1 = self.M + 42, self.W - self.M - 8
        y0, y1 = self.y + 6, self.y + h
        allv = [v for _, vals, *_ in series for v in vals] + ([threshold] if threshold is not None else []) + [0]
        vmin, vmax = min(allv), max(allv)
        span = (vmax - vmin) or 1
        vmin, vmax = vmin - 0.06 * span, vmax + 0.08 * span
        span = vmax - vmin
        sy = lambda v: y1 - (v - vmin) / span * (y1 - y0)
        n = len(months)
        sx = lambda i: x0 + (x1 - x0) * (i / max(n - 1, 1))
        # grille
        step = _nice((vmax - vmin) / 4)
        g = (vmin // step) * step
        while g <= vmax:
            if vmin <= g <= vmax:
                self.hline(sy(g), x0, x1, 0.3, LIGHT)
                self.right(x0 - 5, sy(g) + 3, f"{g/1000:,.0f}".replace(",", " "), 7, "ge", GREY)
            g += step
        if vmin < 0 < vmax:
            self.hline(sy(0), x0, x1, 0.8, GREY)
        if threshold is not None and vmin <= threshold <= vmax:
            self.hline(sy(threshold), x0, x1, 0.7, RED, dashes="[3 3] 0")
            self.text(x0 + 3, sy(threshold) - 3, f"seuil {threshold/1000:,.0f} k".replace(",", " "), 6.5, "gei", RED)
        for i, m in enumerate(months):
            if i % 3 == 0 or i == n - 1:
                self.center(sx(i), y1 + 12, engine.mlabel(m).replace(" 20", " "), 6.8, "ge", GREY)
                self.page.draw_line((sx(i), y1), (sx(i), y1 + 3), color=GREY, width=0.4)
        for lab, vals, color, w, dashes in series:
            pts = [(sx(i), sy(v)) for i, v in enumerate(vals)]
            for a, b in zip(pts, pts[1:]):
                self.page.draw_line(a, b, color=color, width=w, dashes=dashes)
        # légende
        lx = x0
        ly = y1 + 26
        for lab, vals, color, w, dashes in series:
            self.page.draw_line((lx, ly - 3), (lx + 14, ly - 3), color=color, width=w, dashes=dashes)
            self.text(lx + 18, ly, lab, 7.5, "ge", GREY)
            lx += 24 + self.width(lab, 7.5)
        self.y = y1 + 40

    def bar_chart(self, months, values, h=90, color=NAVY, label=""):
        self.ensure(h + 30)
        x0, x1 = self.M + 42, self.W - self.M - 8
        y0, y1 = self.y + 6, self.y + h
        vmax, vmin = max(max(values), 0), min(min(values), 0)
        span = (vmax - vmin) or 1
        sy = lambda v: y1 - (v - vmin) / span * (y1 - y0)
        n = len(values)
        bw = (x1 - x0) / n * 0.62
        for i, v in enumerate(values):
            x = x0 + (x1 - x0) * (i + 0.19) / n
            r = fitz.Rect(x, min(sy(v), sy(0)), x + bw, max(sy(v), sy(0)))
            self.page.draw_rect(r, color=None, fill=color if v >= 0 else RED)
            if i % 3 == 0 or i == n - 1:
                self.center(x + bw / 2, y1 + 12, engine.mlabel(months[i]).replace(" 20", " "), 6.8, "ge", GREY)
        self.hline(sy(0), x0, x1, 0.6, GREY)
        for v in (vmax, vmin):
            if v:
                self.right(x0 - 5, sy(v) + 3, f"{v/1000:,.0f}".replace(",", " "), 7, "ge", GREY)
        if label:
            self.text(x0, ly := y1 + 26, label, 7.5, "ge", GREY)
        self.y = y1 + 36


def _nice(x):
    import math
    if x <= 0:
        return 1
    p = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * p:
            return m * p
    return 10 * p


def _now_fr():
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%d/%m/%Y")


# =============================================================== PRÉVISIONNEL
def forecast_pdf(cfg: dict, res: dict, kind: str = "previsionnel") -> bytes:
    g = cfg.get("general") or {}
    branding = (g.get("branding") or "anvael") == "anvael"
    months, H = res["months"], res["horizon"]
    grp, ents, kp = res["group"], res["entities"], res["kpis"]
    pdf = Pdf()
    pdf.footer = f"Anvael | Prévisionnel de trésorerie | La Parisienne Shanghai | {_now_fr()}" if branding else f"La Parisienne Shanghai | Prévisionnel de trésorerie | {_now_fr()}"
    pdf.new_page()
    title = "PRÉVISIONNEL DE TRÉSORERIE" if kind == "previsionnel" else "SIMULATION DE TRÉSORERIE"
    lines = [f"Horizon {engine.mlabel(months[0], True)} – {engine.mlabel(months[-1], True)} ({H} mois) · comptes arrêtés au {engine.mlabel(res['as_of'], True)}",
             f"Établi le {_now_fr()}" + (f" · {res['label']}" if res.get("label") else "")]
    pdf.header(title, "LA PARISIENNE — SHANGHAI (JIANZAN · LEBLANC)", lines, branding)

    pdf.kpis([("Trésorerie au " + engine.mlabel(res["as_of"]), k(kp["cash_open"]) + " k", " + ".join(f"{e} {k(ents[e]['opening'])} k" for e in ents)),
              ("Point bas", k(kp["cash_min"]) + " k", engine.mlabel(kp["cash_min_month"], True)),
              ("Trésorerie fin d'horizon", k(kp["cash_end"]) + " k", engine.mlabel(months[-1], True)),
              ("EBITDA 12 mois (après siège)", k(kp["ebitda_12m"]) + " k", f"CA {mio(kp['revenue_12m'])} · flux d'exploitation {k(kp['op_cash_12m'])} k")])

    pdf.section("Trésorerie mensuelle (k RMB)")
    series = [("Groupe", grp["cash"], NAVY, 1.5, None)]
    for e, col, dash in (("JZ", GREY, None), ("LBL", GREY, "[3 2] 0")):
        if e in ents:
            series.append((f"{e}", ents[e]["cash"], col, 0.8, dash))
    pdf.line_chart(months, series, h=150, threshold=float(g.get("alert_group") or 0) or None)

    pdf.section("Alertes et échéances")
    if res.get("alerts"):
        for a in res["alerts"]:
            col = RED if a["level"] == "danger" else (NAVY if a["level"] == "warn" else BLACK)
            pdf.para("· " + a["text"], 9, "ge", col, indent=4)
    else:
        pdf.para("Aucune alerte sur l'horizon.", 9, "ge", GREY)
    pdf.y += 4

    pdf.section("Hypothèses clés")
    rows = []
    for code, c in res["calibration"].items():
        e = c["effective"]
        name = f"{code} · {e.get('name') or ''}"
        rows.append([name if len(name) <= 26 else name[:25] + "…", e.get("entity") or "", mio(e["runrate_annual"]), pct(e["growth_pct"] / 100, 1, True),
                     pct(e["food_pct"] / 100), f"{k(e['labor'])} k ({pct(c['labor_pct_equiv'], 0)})", f"{k(e['rent'])} k", pct(e["other_pct"] / 100), pct(c["ebitda_pct_equiv"], 0)])
    pdf.table(["Magasin", "Entité", "CA annuel", "Tendance", "Food", "Masse sal. / mois", "Loyer / mois", "Autres", "EBITDA %"], rows,
              [110, 32, 52, 42, 38, 74, 50, 40, 44], size=7.6)
    pdf.para(f"Siège (G&A) : {k(res['ho_monthly'])} k/mois ({res['ho_src']}), porté par {g.get('ho_entity', 'JZ')} · frais bancaires et commissions {pct((g.get('bank_fees_pct') or 0)/100)} du CA · "
             f"taxes d'exploitation {pct((g.get('tax_ops_pct') or 0)/100)} · IS {pct((g.get('cit_rate_pct') or 0)/100, 0)} du résultat trimestriel positif · "
             f"dérive salaires {pct((g.get('wage_inflation_pct') or 0)/100)}/an, coûts fixes {pct((g.get('cost_inflation_pct') or 0)/100)}/an · "
             f"friction P&L→trésorerie : " + ", ".join(f"{e} {k(float((g.get('friction') or {}).get(e) or 0), True)} k/mois" for e in ents) + ".", 8, "ge", GREY)
    if res.get("loans"):
        pdf.para("Prêts : " + " ; ".join(f"{l['label']} ({l['entity']}) {mio(l['principal'])} à {pct((l.get('rate_pct') or 0)/100)}, échéance {engine.mlabel(l['maturity'])}, "
                                          + ("renouvelé chaque année" + (f" avec {l.get('renew_gap')} mois de décalage" if l.get("renew_gap") else "") if l.get("renew") else "non renouvelé")
                                          for l in res["loans"]) + ".", 8, "ge", GREY)
    ov = res.get("overrides") or {}
    if ov and engine.describe_overrides(ov):
        pdf.para("Simulation — variantes par rapport au modèle de base : " + engine.describe_overrides(ov) + ".", 8, "gei", NAVY)

    # ---- tableaux mensuels (paysage, 12 mois par page)
    stores = list(res["stores"])
    for start in range(0, H, 12):
        idx = list(range(start, min(start + 12, H)))
        pdf.finish_page()
        pdf.new_page(landscape=True, top=44)
        pdf.text(pdf.M, pdf.y, f"Prévisionnel mensuel — {engine.mlabel(months[idx[0]])} à {engine.mlabel(months[idx[-1]])} (k RMB)", 10.5, "geb", NAVY)
        pdf.y += 18
        cols = [""] + [engine.mlabel(months[i]).replace(" 20", " ") for i in idx] + ["Total"]
        wl = 128
        wc = (pdf.W - 2 * pdf.M - wl - 56) / len(idx)
        widths = [wl] + [wc] * len(idx) + [56]

        def line(label, vals, total=True):
            return [label] + [k(vals[i]) for i in idx] + [k(sum(vals[i] for i in idx)) if total else ""]
        rows, bold, grey = [], [], []
        for c in stores:
            rows.append(line(f"CA {c}", res["stores"][c]["revenue"]))
        rows.append(line("Chiffre d'affaires", grp["revenue"])); bold.append(len(rows) - 1)
        for c in stores:
            rows.append(line(f"EBITDA {c}", res["stores"][c]["ebitda"]))
        rows.append(line("EBITDA magasins", grp["ebitda_stores"])); bold.append(len(rows) - 1)
        rows.append(line("Siège (G&A)", [-v for v in grp["ga"]]))
        rows.append(line("Intérêts et frais bancaires", [-(grp["interest"][i] + grp["fees"][i]) for i in range(H)]))
        rows.append(line("Impôts et taxes", [-(grp["tax_ops"][i] + grp["cit"][i]) for i in range(H)]))
        if any(grp["rent_adj"]) or any(grp["friction"]):
            rows.append(line("Décalage loyers / friction", [grp["rent_adj"][i] + grp["friction"][i] for i in range(H)]))
        rows.append(line("Flux d'exploitation", grp["op_cash"])); bold.append(len(rows) - 1)
        rows.append(line("Investissements", grp["capex"]))
        rows.append(line("Prêts (échéances / tirages)", grp["loans"]))
        rows.append(line("Comptes courants d'associés", grp["cca"]))
        rows.append(line("Intercos et autres", [grp["interco"][i] + grp["other"][i] for i in range(H)]))
        rows.append(line("Variation de trésorerie", grp["delta"])); bold.append(len(rows) - 1)
        for e in ents:
            rows.append(line(f"Trésorerie {e}", ents[e]["cash"], total=False)); grey.append(len(rows) - 1)
        rows.append(line("Trésorerie groupe", grp["cash"], total=False)); bold.append(len(rows) - 1)
        pdf.table(cols, rows, widths, size=7.4, bold_rows=bold, grey_rows=grey, zebra=True, lh=12)

    # ---- événements, prêts, CCA, méthode
    pdf.finish_page()
    pdf.new_page(top=50)
    pdf.section("Événements pris en compte")
    evs = res.get("events_applied") or []
    if evs:
        pdf.table(["Mois", "Entité", "Nature", "Libellé", "Montant"],
                  [[engine.mlabel(e["month"]), e["entity"], config.EVENT_CATEGORIES.get(e["category"], e["category"]).split(" (")[0], e["label"][:60], k(e["amount"], True) + " k"] for e in evs],
                  [60, 40, 110, 216, 66], size=8)
    else:
        pdf.para("Aucun événement ponctuel (investissement, apport, remboursement) saisi sur l'horizon.", 9, "ge", GREY)
    pdf.y += 4
    pdf.section("Échéancier des prêts")
    if res.get("loan_schedule"):
        pdf.table(["Mois", "Prêt", "Mouvement", "Montant"], [[engine.mlabel(x["month"]), x["loan"], x["type"], k(x["amount"], True) + " k"] for x in res["loan_schedule"]],
                  [70, 220, 120, 82], size=8)
    else:
        pdf.para("Aucun mouvement de prêt sur l'horizon.", 9, "ge", GREY)
    pdf.y += 4
    cca = cfg.get("cca") or {}
    if cca.get("positions"):
        pdf.section("Comptes courants d'associés (registre — à confirmer)")
        pdf.table(["Associé", "Entité", "Montant", "Source"], [[p.get("shareholder", ""), p.get("entity", ""), k(float(p.get("amount") or 0)) + (" k USD" if p.get("entity") == "GHHL" else " k"), str(p.get("source", ""))[:70]]
                                                               for p in cca["positions"]], [110, 50, 70, 262], aligns=["l", "l", "r", "l"], size=7.8)
    pdf.section("Méthode (résumé)")
    for t in METHOD_SHORT:
        pdf.para(t, 8.3, "ge", BLACK, lh=1.28)
        pdf.y += 2
    return pdf.bytes()


METHOD_SHORT = [
    "Chiffre d'affaires : pour chaque magasin, un niveau d'activité désaisonnalisé (run-rate) calibré sur les 12 derniers mois hors extrêmes, "
    "multiplié par un coefficient saisonnier mensuel (historique du magasin, ou profil scolaire pour les kiosques) et par une tendance annuelle choisie. "
    "Un magasin projeté est décrit par un CA annuel de croisière, une montée en charge et une date d'ouverture.",
    "Coûts : food cost et autres charges d'exploitation en % du CA (médiane des 6 derniers mois, modifiable) ; masse salariale et loyer en montants mensuels "
    "lissés (médiane des 6 derniers mois, modifiable) avec une dérive annuelle. EBITDA magasin = CA − food − masse salariale − loyer − autres charges "
    "(écart < 1 % avec l'EBITDA (Store) du management report). Siège = G&A mensuel porté par JIANZAN.",
    "Trésorerie : trésorerie du mois précédent + EBITDA magasins − siège − intérêts et frais bancaires − taxes − IS trimestriel ± échéances et "
    "renouvellements des prêts ± événements saisis (investissements, apports ou remboursements d'associés, transferts entre entités) ± décalage de "
    "paiement des loyers ± friction structurelle. Les amortissements sont exclus (non décaissés) ; le besoin en fonds de roulement n'est pas modélisé "
    "ligne à ligne : ses effets apparaissent dans l'analyse mensuelle des écarts, qui recalibre le modèle.",
    "Chaque mois : import du Board Management Report, comparaison prévu / réalisé (CA, ratios, masse salariale, siège, trésorerie et pont de trésorerie), "
    "diagnostic des écarts et propositions de recalibrage — les paramètres laissés en « auto » se réajustent seuls, les valeurs saisies restent sous contrôle.",
]


# =============================================================== ÉCARTS
def variance_pdf(cfg: dict, v: dict) -> bytes:
    g = cfg.get("general") or {}
    branding = (g.get("branding") or "anvael") == "anvael"
    month = v["month"]
    pdf = Pdf()
    pdf.footer = f"Anvael | Analyse des écarts | La Parisienne Shanghai | {_now_fr()}" if branding else f"La Parisienne Shanghai | Analyse des écarts | {_now_fr()}"
    pdf.new_page()
    pdf.header(f"ANALYSE DES ÉCARTS — {engine.mlabel(month, True).upper()}", "LA PARISIENNE — SHANGHAI (JIANZAN · LEBLANC)",
               [f"Référence : {v.get('reference') or 'prévisionnel enregistré'}", f"Établi le {_now_fr()}"], branding)
    G = v["group"]
    items = [("Chiffre d'affaires", f"{k(G['revenue_A'])} k", f"prévu {k(G['revenue_F'])} k ({pct(engine._pct(G['revenue_A'], G['revenue_F']), 1, True)})"),
             ("EBITDA après siège", f"{k(G['ebitda_A'])} k", f"prévu {k(G['ebitda_F'])} k ({k(G['ebitda_A'] - G['ebitda_F'], True)} k)")]
    if "cash_A" in G:
        items.append(("Trésorerie fin de mois", f"{k(G['cash_A'])} k", f"prévue {k(G['cash_F'])} k ({k(G['cash_A'] - G['cash_F'], True)} k)"))
        items.append(("Variation du mois", f"{k(G['cash_A'] - G['cash_prev_A'], True)} k", f"prévue {k(G['cash_F'] - G['cash_prev_F'], True)} k"))
    pdf.kpis(items)

    pdf.section("Par magasin (k RMB)")
    rows, bold = [], []
    for r in v["stores"]:
        F, A = r["F"], r["A"]
        if not A:
            rows.append([r["code"], k(F["revenue"]), "—", "—", pct(F["food"] / F["revenue"] if F["revenue"] else None), "—", k(F["labor"]), "—", k(F["ebitda"]), "—", "—"])
            continue
        rows.append([r["code"], k(F["revenue"]), k(A["revenue"]), pct(r["Dpct"]["revenue"], 0, True), pct(r["ratios"]["food_F"]), pct(r["ratios"]["food_A"]),
                     k(F["labor"]), k(A["labor"]), k(F["ebitda"]), k(A["ebitda"]), k(r["D"]["ebitda"], True)])
    rows.append(["Magasins", k(G["ebitda_stores_F"] and G["revenue_F"]), k(G["revenue_A"]), pct(engine._pct(G["revenue_A"], G["revenue_F"]), 0, True), "", "", "", "",
                 k(G["ebitda_stores_F"]), k(G["ebitda_stores_A"]), k(G["ebitda_stores_A"] - G["ebitda_stores_F"], True)]); bold.append(len(rows) - 1)
    rows.append(["Siège (G&A)", "", "", "", "", "", "", "", k(-G["ga_F"]), k(-G["ga_A"]), k(G["ga_F"] - G["ga_A"], True)])
    rows.append(["EBITDA après siège", "", "", "", "", "", "", "", k(G["ebitda_F"]), k(G["ebitda_A"]), k(G["ebitda_A"] - G["ebitda_F"], True)]); bold.append(len(rows) - 1)
    pdf.table(["", "CA prévu", "CA réel", "Écart", "Food p.", "Food r.", "Sal. p.", "Sal. r.", "EBITDA p.", "EBITDA r.", "Écart"], rows,
              [72, 42, 42, 36, 40, 40, 42, 42, 46, 46, 44], size=7.4, bold_rows=bold)
    pdf.para("p. = prévu, r. = réel ; masse salariale et EBITDA en k RMB.", 7.2, "gei", GREY)

    pdf.section("D'où vient l'écart d'EBITDA magasin (k RMB)")
    rows = []
    for r in v["stores"]:
        if r.get("bridge"):
            b = r["bridge"]
            rows.append([r["code"], k(b["ca"], True), k(b["food"], True), k(b["other"], True), k(b["labor"], True), k(b["rent"], True), k(r["D"]["ebitda"], True)])
    if rows:
        pdf.table(["", "Volume de CA", "Ratio food", "Ratio autres", "Masse salariale", "Loyer", "Écart EBITDA"], rows, [78, 70, 70, 70, 76, 60, 68], size=7.8)
        pdf.para("Volume de CA = écart de CA valorisé à la marge prévue ; ratio food / autres = effet du taux réel sur le CA réel ; masse salariale et loyer = écart de montant.", 7.5, "gei", GREY)

    pdf.section("Trésorerie : prévu contre réel, par entité (k RMB)")
    for e, ent in v["entities"].items():
        if "cash_A" not in ent:
            pdf.para(f"{e} : balance du mois non importée.", 9, "ge", GREY)
            continue
        rows = [["Trésorerie début de mois", k(ent["cash_prev_F"]), k(ent["cash_prev_A"])],
                ["EBITDA magasins − siège − financier", k(ent["delta_F"] - ent["loans_F"] - ent["cca_F"] - ent["interco_F"] - ent["capex_F"] - ent["other_F"]), k(ent["ebitda_A"] - ent["ga_A"] - ent["fin_A"])],
                ["Investissements", k(ent["capex_F"]), k(ent["capex_A"])],
                ["Prêts", k(ent["loans_F"]), k(ent["d_loans"])],
                ["Comptes courants d'associés", k(ent["cca_F"]), k(ent["d_cca"])],
                ["Intercos", k(ent["interco_F"]), k(ent["d_interco"])],
                ["Autres / BFR / non expliqué", k(ent["other_F"]), k(ent["residual"])],
                ["Variation du mois", k(ent["delta_F"], True), k(ent["delta_A"], True)],
                ["Trésorerie fin de mois", k(ent["cash_F"]), k(ent["cash_A"])]]
        pdf.text(pdf.M, pdf.y, config.ENTITIES.get(e, {}).get("latin", e), 8.5, "geb", NAVY); pdf.y += 12
        pdf.table(["", "Prévu", "Réel"], rows, [300, 96, 96], size=7.8, bold_rows=(7, 8))
        wc = sorted(ent["wc"].items(), key=lambda kv: -abs(kv[1]))
        pdf.para("Mouvements de bilan du mois (effet trésorerie) : " + ", ".join(f"{kk} {k(vv, True)} k" for kk, vv in wc if abs(vv) >= 5000) + f" — total BFR {k(ent['wc_total'], True)} k.", 7.5, "ge", GREY)
        pdf.y += 4

    pdf.section("Diagnostic")
    for d in v["diagnostic"]:
        col = RED if d["level"] == "danger" else (NAVY if d["level"] == "warn" else BLACK)
        pdf.para("· " + d["text"], 8.8, "ge", col, indent=4)
    pdf.y += 4
    pdf.section("Propositions de recalibrage")
    if v["proposals"]:
        pdf.table(["Magasin", "Paramètre", "Utilisé", "Recalibré", "Réglage"],
                  [[p["name"][:26], p["label"], _fmtp(p, p["used"]), _fmtp(p, p["new"]), p["setting"]] for p in v["proposals"]], [130, 150, 70, 70, 72], size=7.8)
        pdf.para("Un paramètre en « auto » se recalibre seul au prochain prévisionnel ; un paramètre « saisi » reste tel quel tant qu'on ne l'applique pas ou ne le repasse pas en auto.", 7.5, "gei", GREY)
    else:
        pdf.para("Aucun recalibrage significatif à proposer.", 9, "ge", GREY)
    return pdf.bytes()


def _fmtp(p, val):
    if p["param"] in ("food_pct", "other_pct"):
        return pct(val / 100)
    if p["param"] == "runrate_annual":
        return mio(val)
    return k(val) + " k"
