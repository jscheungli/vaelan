"""LP — PDF du prévisionnel et de l'analyse des écarts (pymupdf), en anglais, libellés du Management Report.
Charte sobre : Gelasio (métrique Georgia, police libre embarquée), marine #0A2540, filets fins, logo Anvael en tête (option)."""
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
_HAVE_FONTS = all(os.path.exists(p) for p in _FONTS.values())
_FONT_OBJ = {k: fitz.Font(fontfile=p) for k, p in _FONTS.items()} if _HAVE_FONTS else {}
ML = engine.mlabel_en


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
    return "—" if v is None else f"{v/1e6:,.2f} M".replace(",", " ")


def pct(v, digits=1, signed=False) -> str:
    if v is None:
        return "—"
    s = f"{v*100:.{digits}f} %"
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
            self.center(self.W / 2, yy, "Delivered by Anvael through the Vaelan platform", 8.5, "gei", GREY)
            yy += 12
        self.hline(yy + 4, width=0.7, color=GREY)
        self.y = yy + 24

    def box(self, title, lines, size=9):
        """Encadré pâle : titre marine + phrases (retour à la ligne), pour la conclusion."""
        wrapped = [(t, self.wrap(t, self.W - 2 * self.M - 24, size)) for t in lines]
        h = 22 + sum(len(w) for _, w in wrapped) * size * 1.32 + 8
        self.ensure(h + 6)
        self.page.draw_rect(fitz.Rect(self.M, self.y - 12, self.W - self.M, self.y - 12 + h), color=None, fill=PALE)
        self.page.draw_rect(fitz.Rect(self.M, self.y - 12, self.M + 3, self.y - 12 + h), color=None, fill=NAVY)
        self.text(self.M + 12, self.y + 2, title.upper(), 9, "geb", NAVY)
        yy = self.y + 18
        for t, w in wrapped:
            for j, line in enumerate(w):
                self.text(self.M + 12 + (0 if j == 0 else 8), yy, ("· " if j == 0 else "") + line, size, "ge", BLACK)
                yy += size * 1.32
        self.y = self.y - 12 + h + 14

    def section(self, title):
        self.ensure(30)
        self.text(self.M, self.y, title.upper(), 9.5, "geb", NAVY)
        self.hline(self.y + 5, width=0.5)
        self.y += 20

    def table(self, cols, rows, widths, aligns=None, size=8.2, header=True, bold_rows=(), grey_rows=(), zebra=False, lh=13):
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
            nl = max(len(str(c).split("\n")) for c in cols)
            self.ensure(lh * (nl + 1))
            for li in range(nl):
                draw_row([(str(c).split("\n") + [""] * nl)[li] if nl - len(str(c).split("\n")) <= li or True else "" for c in cols], "geb", GREY, self.y + li * (lh - 2))
            self.y += (nl - 1) * (lh - 2)
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

    def bar_chart(self, months, values, h=190, threshold=None):
        """Histogramme des niveaux mensuels (k) avec la valeur au-dessus de chaque barre."""
        self.ensure(h + 34)
        x0, x1 = self.M + 34, self.W - self.M - 4
        y0, y1 = self.y + 12, self.y + h
        vmax = max(max(values), threshold or 0, 0)
        vmin = min(min(values), 0)
        span = (vmax - vmin) or 1
        vmax, vmin = vmax + 0.10 * span, vmin - (0.06 * span if vmin < 0 else 0)
        span = vmax - vmin
        sy = lambda v: y1 - (v - vmin) / span * (y1 - y0)
        step = _nice(span / 4)
        gline = (vmin // step) * step
        while gline <= vmax:
            if vmin <= gline <= vmax:
                self.hline(sy(gline), x0, x1, 0.3, LIGHT)
                self.right(x0 - 4, sy(gline) + 2.5, f"{gline/1000:,.0f}".replace(",", " "), 6.5, "ge", GREY)
            gline += step
        n = len(values)
        slot = (x1 - x0) / n
        bw = slot * 0.66
        for i, v in enumerate(values):
            x = x0 + slot * i + (slot - bw) / 2
            r = fitz.Rect(x, min(sy(v), sy(0)), x + bw, max(sy(v), sy(0)))
            low = v < (threshold if threshold is not None else 0) - 0.5
            self.page.draw_rect(r, color=None, fill=RED if low else NAVY)
            lab = k(v)
            fs = 6.2 if n > 18 else 7
            self.center(x + bw / 2, (sy(v) - 3) if v >= 0 else (sy(v) + fs + 2), lab, fs, "geb", RED if low else NAVY)
            self.center(x + bw / 2, y1 + 10, ML(months[i], short_year=True).split(" ")[0], 6.2, "ge", GREY)
            if int(months[i][5:7]) == 1 or i == 0:
                self.center(x + bw / 2, y1 + 19, months[i][:4], 6.2, "geb", GREY)
        self.hline(sy(0), x0, x1, 0.7, GREY)
        if threshold is not None and vmin <= threshold <= vmax:
            self.hline(sy(threshold), x0, x1, 0.6, RED, dashes="[3 3] 0")
            self.right(x0 - 4, sy(threshold) + 2.5, f"{threshold/1000:,.0f} k".replace(",", " "), 6.3, "gei", RED)
            self.right(x0 - 4, sy(threshold) + 9.5, "minimum", 5.8, "gei", RED)
        self.y = y1 + 30


def _nice(x):
    import math
    if x <= 0:
        return 1
    p = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * p:
            return m * p
    return 10 * p


def _now():
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%d/%m/%Y")


def _footer(kind, branding):
    return f"Anvael | {kind} | La Parisienne Shanghai | {_now()}" if branding else f"La Parisienne Shanghai | {kind} | {_now()}"


def _closing(as_of):
    import calendar
    y, m = int(as_of[:4]), int(as_of[5:7])
    return f"{calendar.monthrange(y, m)[1]} {ML(as_of, True)}"


# =============================================================== PRÉVISIONNEL
def _chart_and_conclusion(pdf, cfg, res, title=None):
    g = cfg.get("general") or {}
    if title:
        pdf.section(title)
    else:
        pdf.section("Group cash position — end of month (k RMB)")
    pdf.bar_chart(res["months"], res["group"]["cash"], h=176, threshold=float(g.get("alert_group") or 0) or None)
    ov = res.get("overrides") or {}
    if ov and engine.describe_overrides(ov) and not res.get("scenario"):
        pdf.para("Simulation — changes versus the base case: " + engine.describe_overrides(ov) + ".", 8.5, "gei", NAVY)
    pdf.box("Conclusion — funding need", res.get("conclusion") or [])
    shown = [a for a in res.get("alerts") or [] if a.get("scope") == "loan"]
    if shown:
        pdf.section("Loan maturities")
        for a in shown:
            pdf.para("· " + a["text"], 9, "ge", BLACK, indent=4)
        pdf.y += 4


def _assumptions(pdf, cfg, res, with_projects=True):
    g = cfg.get("general") or {}
    pdf.section("Key assumptions — existing stores")
    rows, projects = [], []
    for code, c in res["calibration"].items():
        e = c["effective"]
        if e.get("opened") and e["opened"] > res["as_of"]:
            projects.append((code, c))
            continue
        name = f"{code} · {e.get('name') or ''}"
        rows.append([name if len(name) <= 24 else name[:23] + "…", e.get("entity") or "", mio(e["runrate_annual"]), pct(e["growth_pct"] / 100, 1, True),
                     pct(e["food_pct"] / 100), f"{k(e['labor'])} k ({pct(c['labor_pct_equiv'], 0)})", f"{k(e['rent'])} k", pct(e["other_pct"] / 100),
                     f"{k(e['da'])} k", pct(c["ebitda_pct_equiv"], 0)])
    pdf.table(["Store", "Entity", "Annual\nincome", "Trend\nper year", "Food\ncost", "Labor cost\nper month", "Rent\nper month", "Other\nopex", "D&A\nper month", "EBITDA\nmargin"],
              rows, [104, 30, 52, 40, 36, 68, 44, 40, 42, 36], size=7.4)
    if res.get("loans"):
        pdf.para("Bank loans: " + " ".join(
            f"{l['label']} ({l['entity']}) {mio(l['principal'])} at {pct((l.get('rate_pct') or 0)/100)}, repaid {ML(engine.month_add(l['maturity'], -int(g.get('repay_lead', 1) or 0)), True)}"
            + (f" and renewed in {ML(engine.month_add(l['maturity'], int(l.get('renew_gap') or 0)), True)} for {mio(l.get('renew_amount') or l['principal'])} (rolled every {l.get('term_months', 12)} months)" if l.get("renew") else ", not renewed") + "."
            for l in res["loans"]), 8, "ge", GREY)
    pdf.para(f"Funding rule: minimum group cash position of {k(float(g.get('alert_group') or 0))} k; when the position falls below it, a shareholder contribution in round amounts of "
             f"{k(float(g.get('contribution_round') or 300000))} k ({int(g.get('partners') or 3)} equal shares) is assumed, repaid as soon as the position allows "
             "and no further need arises within six months (no back-and-forth).", 8, "ge", GREY)
    if not with_projects:
        return
    for code, c in projects:
        e = c["effective"]
        pdf.ensure(230)
        pdf.section(f"Project — {e.get('name') or code} ({config.ENTITIES.get(e.get('entity'), {}).get('latin', e.get('entity')).split(' (')[0]})")
        left = [["Opening", ML(e["opened"], True)], ["Year-1 income", f"{mio(e['runrate_annual'])} ex-VAT, projected monthly profile"
                + (f", ramp-up from {e['ramp_start_pct']:.0f} % over {e['ramp_months']} months" if e.get("ramp_months") else "")],
                ["Food cost", pct(e["food_pct"] / 100)], ["Labor cost", f"{k(e['labor'])} k per month"],
                ["Rent", f"{k(e['rent'])} k per month, paid " + ("monthly" if e.get("rent_period", 1) == 1 else f"every {e['rent_period']} months")],
                ["Other opex", pct(e["other_pct"] / 100) + " of income"], ["D&A", f"{k(e['da'])} k per month"],
                ["Pre-opening", f"{e['preopening_months']} month(s) of rent and payroll before opening" if e.get("preopening_months") else "none"],
                ["Head office", "no additional cost"], ["EBITDA margin (steady state)", pct(c["ebitda_pct_equiv"], 0)]]
        pdf.table(["Operating assumptions", ""], left, [150, 342], aligns=["l", "l"], size=8, header=True)
        evs = [x for x in res.get("events_applied") or [] if x.get("store") == code]
        if evs:
            cats = {"capex": "Capital expenditure", "other": "Deposit / other", "cca": "Shareholder account", "loan": "Bank loan", "interco": "Intercompany"}
            erows = [[ML(x["month"], True), cats.get(x["category"], x["category"]), x["label"].split(": ", 1)[-1][:58], k(x["amount"], True) + " k"] for x in evs]
            erows.append(["", "", "Total cash out before opening", k(sum(x["amount"] for x in evs), True) + " k"])
            pdf.table(["Cash events", "Type", "Description", "Amount"], erows, [90, 110, 226, 66], aligns=["l", "l", "l", "r"], size=8, bold_rows=(len(erows) - 1,))


def _monthly_tables(pdf, res, prefix=""):
    months, H = res["months"], res["horizon"]
    rows_all = engine.pl_rows(res, with_entities=False)
    for start in range(0, H, 12):
        idx = list(range(start, min(start + 12, H)))
        pdf.finish_page()
        pdf.new_page(landscape=True, top=44)
        pdf.text(pdf.M, pdf.y, f"{prefix}Monthly forecast — {ML(months[idx[0]], True)} to {ML(months[idx[-1]], True)} (k RMB)", 10.5, "geb", NAVY)
        pdf.y += 18
        cols = [""] + [ML(months[i], short_year=True) for i in idx] + ["Total"]
        wl = 176
        wc = (pdf.W - 2 * pdf.M - wl - 56) / len(idx)
        widths = [wl] + [wc] * len(idx) + [56]
        trows, bold, grey = [], [], []
        for r in rows_all:
            trows.append([r["label"]] + [k(r["values"][i]) for i in idx] + [k(sum(r["values"][i] for i in idx)) if r["total"] else ""])
            if r["kind"] == "total":
                bold.append(len(trows) - 1)
            elif r["kind"] == "grey":
                grey.append(len(trows) - 1)
        pdf.table(cols, trows, widths, size=7.2, bold_rows=bold, grey_rows=grey, zebra=True, lh=11.6)


def _events_and_method(pdf, cfg, res, include_plan=True):
    g = cfg.get("general") or {}
    pdf.finish_page()
    pdf.new_page(top=50)
    pdf.section("Cash events included in the forecast")
    evs = [x for x in res.get("events_applied") or [] if include_plan or "funding plan" not in (x.get("label") or "")]
    items = [(x["month"], x["entity"], {"capex": "Capital expenditure", "cca": "Shareholder contribution" if x["amount"] > 0 else "Shareholder repayment", "loan": "Bank loan", "interco": "Intercompany", "other": "Deposit / other"}.get(x["category"], x["category"]),
              x["label"], x["amount"]) for x in evs]
    items += [(x["month"], x["entity"], "Bank loan " + x["type"], x["loan"], x["amount"]) for x in res.get("loan_schedule") or []]
    items.sort(key=lambda t: t[0])
    if items:
        pdf.table(["Month", "Entity", "Type", "Description", "Amount"], [[ML(m), e, t, d[:62], k(a, True) + " k"] for m, e, t, d, a in items],
                  [60, 40, 110, 216, 66], aligns=["l", "l", "l", "l", "r"], size=8)
    else:
        pdf.para("No cash event over the horizon.", 9, "ge", GREY)
    pdf.para("Shareholder contributions come from the funding rule (round amounts, equal shares); the shareholder current-account register is a module under development.", 8, "gei", GREY)
    pdf.y += 4
    pdf.section("Methodology (summary)")
    for t in METHOD_SHORT:
        pdf.para(t, 8.3, "ge", BLACK, lh=1.28)
        pdf.y += 2
    pdf.para(f"Calibration: existing stores use their actuals — activity level = seasonally-adjusted average of the last 12 months (excluding extremes), "
             f"food cost and other opex as medians of the last {int(g.get('calib_months') or 6)} months, labor and rent as flat monthly medians; wage drift "
             f"{pct((g.get('wage_inflation_pct') or 0)/100)} per year, fixed-cost drift {pct((g.get('cost_inflation_pct') or 0)/100)} per year. "
             f"Head office G&A {k(res['ho_monthly'])} k per month ({res['ho_src']}), borne by JIANZAN; bank charges and platform commissions "
             f"{pct((g.get('bank_fees_pct') or 0)/100)} of income; operation taxes {pct((g.get('tax_ops_pct') or 0)/100)}; corporate income tax "
             f"{pct((g.get('cit_rate_pct') or 0)/100, 0)} of positive quarterly profit, booked and paid the month after quarter end.", 8.3, "ge", BLACK, lh=1.28)


def _header_lines(res, H):
    return [f"{ML(res['months'][0], True)} – {ML(res['months'][-1], True)} ({H} months) · based on accounts closed at {_closing(res['as_of'])}",
            f"Prepared {_now()}" + (f" · {res['label']}" if res.get("label") else "")]


def forecast_pdf(cfg: dict, res: dict, kind: str = "previsionnel") -> bytes:
    """PDF d'un scénario seul (variante ou simulation)."""
    g = cfg.get("general") or {}
    branding = (g.get("branding") or "anvael") == "anvael"
    pdf = Pdf()
    pdf.footer = _footer("Cash flow forecast" if kind == "previsionnel" else "Cash flow simulation", branding)
    pdf.new_page()
    title = "CASH FLOW FORECAST" if kind == "previsionnel" else "CASH FLOW SIMULATION"
    lines = _header_lines(res, res["horizon"])
    if res.get("scenario"):
        lines.append(f"Scenario: {res['scenario']}")
    res = dict(res, scenario=None)      # titre de section standard pour un scénario seul
    pdf.header(title, "LA PARISIENNE — SHANGHAI (JIANZAN · LEBLANC)", lines, branding)
    _chart_and_conclusion(pdf, cfg, res)
    _assumptions(pdf, cfg, res)
    _monthly_tables(pdf, res)
    _events_and_method(pdf, cfg, res)
    return pdf.bytes()


def forecast_set_pdf(cfg: dict, results: list) -> bytes:
    """PDF combiné : vue d'ensemble des scénarios, hypothèses communes, puis chaque scénario (graphique, conclusion, tableaux)."""
    g = cfg.get("general") or {}
    branding = (g.get("branding") or "anvael") == "anvael"
    base = results[-1]
    pdf = Pdf()
    pdf.footer = _footer("Cash flow forecast", branding)
    pdf.new_page()
    pdf.header("CASH FLOW FORECAST", "LA PARISIENNE — SHANGHAI (JIANZAN · LEBLANC)", _header_lines(base, base["horizon"]), branding)
    pdf.section("Scenarios")
    floor = float(g.get("alert_group") or 0)
    rows = []
    for n, r in enumerate(results, 1):
        kp = r["kpis"]
        contrib = sum(x["amount"] for x in (r.get("funding_plan") or []) if x["amount"] > 0)
        repaid = -sum(x["amount"] for x in (r.get("funding_plan") or []) if x["amount"] < 0)
        low = sum(1 for v in r["group"]["cash"] if v < floor - 0.5)
        first = next((x for x in (r.get("funding_plan") or []) if x["amount"] > 0), None)
        last_rep = next((x for x in reversed(r.get("funding_plan") or []) if x["amount"] < 0), None)
        rows.append([f"{n}", r.get("scenario_short") or r.get("scenario") or "", f"{k(kp['cash_min'])} k · {ML(kp['cash_min_month'], short_year=True)}", str(low) if low else "—",
                     (f"{k(contrib)} k · {ML(first['month'], short_year=True)}" if contrib else "—"),
                     (f"{k(repaid)} k · {ML(last_rep['month'], short_year=True)}" if repaid else ("—" if not contrib else "not repaid")), f"{k(kp['cash_end'])} k"])
    pdf.table(["#", "Scenario", "Low point", "Months\nbelow min.", "Shareholder\ncontribution", "Repaid", "Cash end\nof horizon"], rows, [14, 176, 78, 44, 76, 60, 44],
              aligns=["l", "l", "r", "r", "r", "r", "r"], size=7.6)
    pdf.para(f"Minimum group cash position: {k(floor)} k. Scenarios without contribution show the cash shortfalls in red; scenarios with contributions show the "
             "amounts, timing and repayment required to stay above the minimum.", 8, "ge", GREY)
    pdf.y += 2
    _assumptions(pdf, cfg, base)
    for n, r in enumerate(results, 1):
        pdf.finish_page()
        pdf.new_page(top=50)
        pdf.section(f"Scenario {n} — {r.get('scenario_short') or r.get('scenario') or ''}")
        if r.get("scenario") and r.get("scenario") != r.get("scenario_short"):
            pdf.para(r["scenario"], 8.5, "gei", GREY)
        _chart_and_conclusion(pdf, cfg, r, title="Group cash position — end of month (k RMB)")
        _monthly_tables(pdf, r, prefix=f"Scenario {n} — ")
    _events_and_method(pdf, cfg, base, include_plan=False)
    return pdf.bytes()


METHOD_SHORT = [
    "Income: for each store, a seasonally-adjusted activity level (run-rate) calibrated on the last 12 months excluding extremes, multiplied by the store's "
    "monthly seasonality (its own history, or a school-calendar profile for the kiosks) and by the chosen annual trend. A projected store is described by "
    "its year-1 income, its opening date and its monthly profile.",
    "Costs: Operation Food Cost and other operation expenses as a share of income (median of the last 6 months, editable); Operation Labor Cost and Rent "
    "as flat monthly amounts (median of the last 6 months, editable) with an annual drift; Amortization & Depreciation as a monthly amount. "
    "EBITDA (Store) = income − food − labor − rent − other opex, within 1 % of the Management Report. G&A Expenses = head-office monthly cost. "
    "Profit (After G&A, Before Tax) = EBITDA (After G&A) − D&A − Financial Expenses − Operation Taxes; Corporate Income Tax on positive quarterly profit.",
    "Cash: previous month + Company Profit (After Tax) + D&A (non-cash) ± bank loan repayments and drawdowns ± capital expenditure, deposits and other cash "
    "events ± rent payment timing. Bank loans are 12-month bullet loans: each maturity is shown with the assumed renewal. Working capital is not modelled "
    "line by line; its effect is measured each month in the variance analysis.",
]


# =============================================================== ÉCARTS
def variance_pdf(cfg: dict, v: dict) -> bytes:
    g = cfg.get("general") or {}
    branding = (g.get("branding") or "anvael") == "anvael"
    month = v["month"]
    pdf = Pdf()
    pdf.footer = _footer("Variance analysis", branding)
    pdf.new_page()
    pdf.header(f"VARIANCE ANALYSIS — {ML(month, True).upper()}", "LA PARISIENNE — SHANGHAI (JIANZAN · LEBLANC)",
               [f"Reference: {v.get('reference') or 'saved forecast'}", f"Prepared {_now()}"], branding)
    G = v["group"]

    def d(a, b):
        return k(a - b, True)
    pdf.section("Group P&L and cash — forecast vs actual (k RMB)")
    rows = [["Main Business Income (All Stores)", k(G["revenue_F"]), k(G["revenue_A"]), d(G["revenue_A"], G["revenue_F"])],
            ["EBITDA (All Stores)", k(G["ebitda_stores_F"]), k(G["ebitda_stores_A"]), d(G["ebitda_stores_A"], G["ebitda_stores_F"])],
            ["G&A Expenses", k(-G["ga_F"]), k(-G["ga_A"]), d(G["ga_F"], G["ga_A"])],
            ["EBITDA (After G&A)", k(G["ebitda_F"]), k(G["ebitda_A"]), d(G["ebitda_A"], G["ebitda_F"])],
            ["Amortization & Depreciation", k(-G["da_F"]), k(-G["da_A"]), d(G["da_F"], G["da_A"])],
            ["Financial Expenses", k(-G["fin_F"]), k(-G["fin_A"]), d(G["fin_F"], G["fin_A"])],
            ["Operation Taxes & Surcharges", k(-G["tax_F"]), k(-G["tax_A"]), d(G["tax_F"], G["tax_A"])],
            ["Profit (After G&A, Before Tax)", k(G["pbt_F"]), k(G["pbt_A"]), d(G["pbt_A"], G["pbt_F"])],
            ["Corporate Income Tax", k(-G["cit_F"]), k(-G["cit_A"]), d(G["cit_F"], G["cit_A"])],
            ["Company Profit (After Tax)", k(G["pat_F"]), k(G["pat_A"]), d(G["pat_A"], G["pat_F"])]]
    bold = [1, 3, 7, 9]
    if "cash_A" in G:
        rows.append(["Net cash flow of the month", k(G["cash_F"] - G["cash_prev_F"], True), k(G["cash_A"] - G["cash_prev_A"], True), d(G["cash_A"] - G["cash_prev_A"], G["cash_F"] - G["cash_prev_F"])])
        rows.append(["Cash (Group, End of Month)", k(G["cash_F"]), k(G["cash_A"]), d(G["cash_A"], G["cash_F"])])
        bold += [len(rows) - 1]
    pdf.table(["", "Forecast", "Actual", "Variance"], rows, [252, 80, 80, 80], size=8.2, bold_rows=bold)

    pdf.section("By store (k RMB)")
    rows, bold = [], []
    for r in v["stores"]:
        F, A = r["F"], r["A"]
        if not A:
            if F["revenue"] or F["labor"]:
                rows.append([r["code"], k(F["revenue"]), "—", "—", pct(F["food"] / F["revenue"] if F["revenue"] else None), "—", k(F["labor"]), "—", k(F["ebitda"]), "—", "—"])
            continue
        rows.append([r["code"], k(F["revenue"]), k(A["revenue"]), pct(r["Dpct"]["revenue"], 0, True), pct(r["ratios"]["food_F"]), pct(r["ratios"]["food_A"]),
                     k(F["labor"]), k(A["labor"]), k(F["ebitda"]), k(A["ebitda"]), k(r["D"]["ebitda"], True)])
    pdf.table(["", "Income fcst", "Income act.", "Var.", "Food fcst", "Food act.", "Labor fcst", "Labor act.", "EBITDA fcst", "EBITDA act.", "Var."], rows,
              [58, 46, 46, 34, 40, 40, 44, 44, 48, 48, 44], size=7.2)

    pdf.section("Where the EBITDA (Store) variance comes from (k RMB)")
    rows = [[r["code"], k(r["bridge"]["ca"], True), k(r["bridge"]["food"], True), k(r["bridge"]["other"], True), k(r["bridge"]["labor"], True), k(r["bridge"]["rent"], True), k(r["D"]["ebitda"], True)]
            for r in v["stores"] if r.get("bridge")]
    if rows:
        pdf.table(["", "Income volume", "Food cost ratio", "Other opex ratio", "Labor cost", "Rent", "EBITDA variance"], rows, [58, 74, 76, 78, 72, 60, 74], size=7.6)
        pdf.para("Income volume = income variance valued at the forecast margin; food / other opex ratio = effect of the actual ratio on actual income; labor and rent = amount variance.", 7.3, "gei", GREY)

    pdf.section("Cash by entity — forecast vs actual (k RMB)")
    for e, ent in v["entities"].items():
        if "cash_A" not in ent:
            pdf.para(f"{e}: trial balance of the month not imported.", 9, "ge", GREY)
            continue
        rows = [["Cash at beginning of month", k(ent["cash_prev_F"]), k(ent["cash_prev_A"])],
                ["Operating cash (profit after tax + D&A)", k(ent["op_F"]), k(ent["op_A"])],
                ["Capital expenditure", k(ent["capex_F"]), k(ent["capex_A"])],
                ["Bank loans (repayment / drawdown)", k(ent["loans_F"]), k(ent["d_loans"])],
                ["Shareholder accounts", k(ent["cca_F"]), k(ent["d_cca"])],
                ["Intercompany", k(ent["interco_F"]), k(ent["d_interco"])],
                ["Working capital, deposits and unexplained", k(ent["other_F"]), k(ent["residual"])],
                ["Net cash flow", k(ent["delta_F"], True), k(ent["delta_A"], True)],
                ["Cash at end of month", k(ent["cash_F"]), k(ent["cash_A"])]]
        pdf.text(pdf.M, pdf.y, config.ENTITIES.get(e, {}).get("latin", e), 8.5, "geb", NAVY); pdf.y += 12
        pdf.table(["", "Forecast", "Actual"], rows, [300, 96, 96], size=7.8, bold_rows=(7, 8))
        wc = sorted(ent["wc"].items(), key=lambda kv: -abs(kv[1]))
        pdf.para("Balance-sheet movements of the month (cash effect): " + ", ".join(f"{kk} {k(vv, True)} k" for kk, vv in wc if abs(vv) >= 5000) + f" — working capital total {k(ent['wc_total'], True)} k.", 7.5, "ge", GREY)
        pdf.y += 4

    pdf.section("Diagnosis")
    for dd in v["diagnostic"]:
        col = RED if dd["level"] == "danger" else (NAVY if dd["level"] == "warn" else BLACK)
        pdf.para("· " + dd["text"], 8.8, "ge", col, indent=4)
    pdf.y += 4
    pdf.section("Recalibration proposals")
    if v["proposals"]:
        pdf.table(["Store", "Parameter", "Used", "Recalibrated", "Setting"],
                  [[p["name"][:26], p["label"], _fmtp(p, p["used"]), _fmtp(p, p["new"]), p["setting"]] for p in v["proposals"]], [130, 150, 70, 70, 72], size=7.8)
    else:
        pdf.para("No significant recalibration to propose.", 9, "ge", GREY)
    return pdf.bytes()


def _fmtp(p, val):
    if p["param"] in ("food_pct", "other_pct"):
        return pct(val / 100)
    if p["param"] == "runrate_annual":
        return mio(val)
    return k(val) + " k"
