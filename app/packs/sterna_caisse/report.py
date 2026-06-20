"""Compte rendu détaillé d'une tâche (cadrage / génération TOSLT), en texte ET PDF.

Compare, source par source, les montants : journal de SYNTHÈSE (PDF officiel),
pull API (tickets), et CSV AGRÉGÉ (relecture du fichier généré). On vérifie le
CA HT total, le CA TTC, la TVA, le HT/TVA par taux, et les paiements par mode.
La synthèse ne ventile pas par taux de TVA (elle ventile par famille de produit),
d'où « n/a » sur ces lignes côté synthèse — on y vérifie API vs CSV.
"""
from collections import defaultdict
import fitz  # pymupdf
from . import config

TOL = 0.05


def _rate_from_label(taux: str) -> str:
    return taux.replace("%", "").replace(",", ".").strip()


def aggregate_rows(rows) -> dict:
    """Relit les lignes du CSV généré et reconstitue les totaux par poste."""
    ca_acc = config.CA_ANONYME
    rev_tva = {v: k for k, v in config.TVA_ACCOUNT.items()}
    ht_by_rate, tva_by_rate, pay = defaultdict(float), defaultdict(float), defaultdict(float)
    deb = cred = 0.0
    pay_lbl = {}
    for est in config.ESTABLISHMENTS.values():
        a = config.caisse_accounts(est["pfx"])
        pay_lbl[a["cb"]] = "CB"
        pay_lbl[a["especes"]] = "Espèce"
        pay_lbl[a["ticket_resto"]] = "Ticket restaurant"
        pay_lbl[a["autres"]] = "Autres"
    for r in rows:
        acc, taux, d, c = r[2], r[5], float(r[7] or 0), float(r[8] or 0)
        deb += d
        cred += c
        if acc == ca_acc:
            ht_by_rate[_rate_from_label(taux)] += c
        elif acc in rev_tva:
            tva_by_rate[rev_tva[acc]] += c
        elif acc in pay_lbl:
            pay[pay_lbl[acc]] += d - c
    return {
        "ht_by_rate": {k: round(v, 2) for k, v in ht_by_rate.items()},
        "tva_by_rate": {k: round(v, 2) for k, v in tva_by_rate.items()},
        "ca_ht": round(sum(ht_by_rate.values()), 2),
        "tva": round(sum(tva_by_rate.values()), 2),
        "ca_ttc": round(sum(ht_by_rate.values()) + sum(tva_by_rate.values()), 2),
        "payments": {k: round(v, 2) for k, v in pay.items()},
        "debit": round(deb, 2), "credit": round(cred, 2),
    }


def _match(*vals):
    present = [v for v in vals if v is not None]
    if len(present) < 2:
        return "na"
    return "ok" if max(present) - min(present) < TOL else "ecart"


def _fmt(v):
    if v is None:
        return ""
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


# ---------------------------------------------------------------- modèle de données
def _compute(kind, establishment, date_from, date_to, syn, api, csv,
             batch_code, n_tickets, balanced, run_id=None, executed_at=None):
    has_csv = csv is not None
    title = ("Compte rendu — Génération TOSLT (cadrage + CSV)"
             if kind == "generate" else "Compte rendu — Cadrage caisse")
    meta = [f"Établissement : {establishment}        Période : {date_from} → {date_to}"]
    bits = []
    if batch_code:
        bits.append(f"Lot : {batch_code}")
    if n_tickets is not None:
        bits.append(f"Tickets : {n_tickets}")
    if bits:
        meta.append("        ".join(bits))
    trace = []
    if run_id is not None:
        trace.append(f"Tâche #{run_id}")
    if executed_at:
        trace.append(f"Exécutée le {executed_at}")
    if trace:
        meta.append("        ".join(trace))

    def row(label, sv, av, cv):
        return {"label": label, "syn": sv, "api": av, "csv": cv,
                "match": _match(sv, av, cv)}

    sections = []

    # CA global
    syn_ttc, syn_ht = syn.get("ca_total"), syn.get("ca_ht")
    syn_tva = round(syn_ttc - syn_ht, 2) if (syn_ttc is not None and syn_ht is not None) else None
    sections.append({"name": "Chiffre d'affaires", "note": "", "rows": [
        row("CA HT total", syn_ht, api.get("ca_ht"), (csv or {}).get("ca_ht")),
        row("TVA total", syn_tva, api.get("tva"), (csv or {}).get("tva")),
        row("CA TTC total", syn_ttc, api.get("ca_ttc"), (csv or {}).get("ca_ttc")),
    ]})

    # HT par taux
    rates = sorted(set(list(api.get("ht_by_rate", {})) + list((csv or {}).get("ht_by_rate", {}))),
                   key=lambda x: float(x))
    sections.append({"name": "HT par taux de TVA", "note": "synthèse : n/a (ventile par famille)",
                     "rows": [row(f"HT {rt}%", None, api.get("ht_by_rate", {}).get(rt),
                                  (csv or {}).get("ht_by_rate", {}).get(rt)) for rt in rates]})

    # TVA par taux (on masque les taux sans TVA)
    rates = sorted(set(list(api.get("tva_by_rate", {})) + list((csv or {}).get("tva_by_rate", {}))),
                   key=lambda x: float(x))
    trows = []
    for rt in rates:
        av = api.get("tva_by_rate", {}).get(rt)
        cv = (csv or {}).get("tva_by_rate", {}).get(rt)
        if abs(av or 0) < TOL and abs(cv or 0) < TOL:
            continue
        trows.append(row(f"TVA {rt}%", None, av, cv))
    sections.append({"name": "TVA par taux", "note": "synthèse : n/a", "rows": trows})

    # paiements
    modes = ["CB", "Espèce", "Ticket restaurant"]

    def others(d):
        s = round(sum(v for k, v in (d or {}).items() if k not in modes), 2)
        return s if abs(s) >= TOL else None

    prows = [row(m, syn.get("payments", {}).get(m), api.get("payments", {}).get(m),
                 (csv or {}).get("payments", {}).get(m)) for m in modes]
    sv, av, cv = others(syn.get("payments")), others(api.get("payments")), others((csv or {}).get("payments"))
    if sv is not None or av is not None or cv is not None:
        prows.append(row("Autres / divers", sv, av, cv))
    sections.append({"name": "Paiements par mode (net encaissé)", "note": "", "rows": prows})

    balance = None
    if has_csv and balanced is not None:
        balance = {"debit": csv.get("debit"), "credit": csv.get("credit"), "ok": bool(balanced)}

    return {"title": title, "meta": meta, "sections": sections,
            "has_csv": has_csv, "balance": balance}


# ---------------------------------------------------------------- rendu TEXTE
def to_text(data) -> str:
    def cell(v):
        return f"{_fmt(v):>14}"

    def badge(st):
        return {"ok": "OK", "ecart": "⚠️ ÉCART", "na": "—"}[st]

    L = [data["title"], *data["meta"], ""]
    head = f"  {'':<22}{'Synthèse':>14}{'API tickets':>14}{'CSV agrégé':>14}    Match"
    for sec in data["sections"]:
        note = f"   ({sec['note']})" if sec["note"] else ""
        L.append(f"== {sec['name'].upper()} =={note}")
        L.append(head)
        for r in sec["rows"]:
            L.append(f"  {r['label']:<22}{cell(r['syn'])}{cell(r['api'])}{cell(r['csv'])}    {badge(r['match'])}")
        L.append("")
    if data["balance"]:
        b = data["balance"]
        L.append(f"== ÉQUILIBRE CSV ==   débit {_fmt(b['debit'])} = crédit {_fmt(b['credit'])}   "
                 f"{'OK' if b['ok'] else '⚠️ DÉSÉQUILIBRE'}")
        L.append("")
    L.append("Tolérance de rapprochement : ± 0,05 €.")
    return "\n".join(L)


# ---------------------------------------------------------------- rendu PDF (couleur)
_GREEN = (0.10, 0.53, 0.33)
_RED = (0.86, 0.21, 0.27)
_GREY = (0.55, 0.55, 0.58)
_DARK = (0.12, 0.12, 0.16)
_BAR = (0.93, 0.93, 0.96)


def to_pdf(data) -> bytes:
    doc = fitz.open()
    page = doc.new_page()  # A4
    W = page.rect.width
    x0, y = 40, 56
    COL = {"syn": 320, "api": 400, "csv": 480}
    BADGE_X = 500

    def nl(dy=0):
        nonlocal y
        y += dy

    def ensure(space=20):
        nonlocal page, y
        if y + space > 800:
            page = doc.new_page()
            y = 56

    def _ascii(s):
        # les polices base-14 de pymupdf ne couvrent pas —/→/€ : on remplace
        return (str(s).replace("—", "·").replace("→", "->").replace("€", "EUR")
                .replace("œ", "oe").replace("…", "..."))

    def left(x, s, size=9, font="helv", color=_DARK):
        page.insert_text((x, y), _ascii(s), fontsize=size, fontname=font, color=color)

    def right(xr, s, size=9, font="cour", color=_DARK):
        s = _ascii(s)
        w = fitz.get_text_length(s, fontname=font, fontsize=size)
        page.insert_text((xr - w, y), s, fontsize=size, fontname=font, color=color)

    # titre
    left(x0, data["title"], size=15, font="hebo")
    nl(18)
    for m in data["meta"]:
        left(x0, m, size=9, color=(0.3, 0.3, 0.3))
        nl(13)
    nl(2)
    # légende couleur
    page.draw_rect(fitz.Rect(x0, y - 7, x0 + 10, y + 1), fill=_GREEN, color=_GREEN)
    left(x0 + 14, "rapproché", size=8, color=_GREY)
    page.draw_rect(fitz.Rect(x0 + 78, y - 7, x0 + 88, y + 1), fill=_RED, color=_RED)
    left(x0 + 92, "écart", size=8, color=_GREY)
    left(x0 + 130, "—  non comparable (source absente)", size=8, color=_GREY)
    nl(16)

    def col_headers():
        right(COL["syn"], "Synthèse", 8, "helv", _GREY)
        right(COL["api"], "API tickets", 8, "helv", _GREY)
        right(COL["csv"], "CSV agrégé", 8, "helv", _GREY)
        left(BADGE_X, "Match", 8, "helv", _GREY)

    for sec in data["sections"]:
        ensure(40)
        page.draw_rect(fitz.Rect(x0, y - 9, W - 40, y + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, sec["name"], size=10, font="hebo", color=(0.15, 0.15, 0.22))
        if sec["note"]:
            left(x0 + 230, sec["note"], size=8, color=_GREY)
        nl(16)
        col_headers()
        nl(13)
        for r in sec["rows"]:
            ensure(16)
            left(x0 + 4, r["label"], size=9)
            right(COL["syn"], _fmt(r["syn"]))
            right(COL["api"], _fmt(r["api"]))
            right(COL["csv"], _fmt(r["csv"]))
            st = r["match"]
            if st in ("ok", "ecart"):
                col = _GREEN if st == "ok" else _RED
                lbl = "OK" if st == "ok" else "ÉCART"
                w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
                page.draw_rect(fitz.Rect(BADGE_X, y - 8, BADGE_X + w, y + 2.5), fill=col, color=col)
                page.insert_text((BADGE_X + 4, y), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))
            else:
                left(BADGE_X + 2, "—", size=9, color=_GREY)
            nl(14)
        nl(6)

    if data["balance"]:
        ensure(24)
        b = data["balance"]
        col = _GREEN if b["ok"] else _RED
        left(x0 + 3, "Équilibre du CSV", size=10, font="hebo", color=(0.15, 0.15, 0.22))
        left(x0 + 130, f"débit {_fmt(b['debit'])}  =  crédit {_fmt(b['credit'])}", size=9, font="cour")
        lbl = "OK" if b["ok"] else "DÉSÉQUILIBRE"
        w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
        page.draw_rect(fitz.Rect(BADGE_X, y - 8, BADGE_X + w, y + 2.5), fill=col, color=col)
        page.insert_text((BADGE_X + 4, y), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))
        nl(18)

    nl(6)
    left(x0, "Tolérance de rapprochement : ± 0,05 €.", size=8, color=_GREY)
    out = doc.tobytes()
    doc.close()
    return out


# ---------------------------------------------------------------- API publique
def build(kind, establishment, date_from, date_to, syn, api, csv=None, *,
          batch_code=None, n_tickets=None, balanced=None, run_id=None, executed_at=None) -> str:
    return to_text(_compute(kind, establishment, date_from, date_to, syn, api, csv,
                            batch_code, n_tickets, balanced, run_id, executed_at))


def build_pdf(kind, establishment, date_from, date_to, syn, api, csv=None, *,
              batch_code=None, n_tickets=None, balanced=None, run_id=None, executed_at=None) -> bytes:
    return to_pdf(_compute(kind, establishment, date_from, date_to, syn, api, csv,
                           batch_code, n_tickets, balanced, run_id, executed_at))
