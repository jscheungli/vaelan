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


def aggregate_rows(rows, cfg, journal=None) -> dict:
    """Relit les lignes du CSV généré et reconstitue les totaux par poste.

    `journal` : si fourni, on n'agrège QUE ce journal (le CSV unique contient
    TOSLT + TOSLF ; pour comparer le CA au Z, on ne prend que la caisse TOSLT où
    le 70101 porte tout le CA avant reclassement — sinon les reclass d'avoirs sont
    comptés en double)."""
    ca_acc = cfg["ca_anonyme"]
    rev_tva = {v: k for k, v in cfg["tva"].items()}
    ht_by_rate, tva_by_rate, pay = defaultdict(float), defaultdict(float), defaultdict(float)
    deb = cred = 0.0
    pay_lbl = {}
    for a in cfg["est"].values():
        pay_lbl[a["cb"]] = "CB"
        pay_lbl[a["especes"]] = "Espèce"
        pay_lbl[a["ticket_resto"]] = "Ticket restaurant"
        pay_lbl[a["autres"]] = "Autres"
    for r in rows:
        if journal and r[1] != journal:
            continue
        acc, taux, d, c = r[2], r[5], float(r[7] or 0), float(r[8] or 0)
        deb += d
        cred += c
        if acc == ca_acc:                       # CA = crédit (vente) - débit (avoir)
            ht_by_rate[_rate_from_label(taux)] += c - d
        elif acc in rev_tva:                    # TVA = crédit - débit (avoir)
            tva_by_rate[rev_tva[acc]] += c - d
        elif acc in pay_lbl:                    # encaissement = débit - crédit (rendu)
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
             batch_code, n_tickets, balanced, run_id=None, executed_at=None, fac_payments=None,
             fac_detail=None):
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
        trace.append(f"Exécutée le {executed_at} (heure de La Réunion)")
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

    facp = fac_payments or {}
    prows, recon = [], []
    for m in modes:
        sv = syn.get("payments", {}).get(m)
        av = api.get("payments", {}).get(m)
        cv = (csv or {}).get("payments", {}).get(m)
        rw = row(m, sv, av, cv)
        # un écart synthèse↔tickets est RÉCONCILIÉ s'il est couvert par les paiements de
        # factures (api≈csv, et |api-syn| <= factures du mode).
        ecart = round((av or 0) - (sv or 0), 2) if (av is not None and sv is not None) else None
        facv = round(facp.get(m, 0.0), 2)
        if rw["match"] == "ecart" and ecart is not None and abs((av or 0) - (cv or 0)) < TOL \
                and abs(ecart) <= abs(facv) + TOL:
            rw["match"] = "reconciled"
            recon.append({"mode": m, "syn": sv, "api": av, "ecart": ecart, "facture": facv})
        prows.append(rw)
    sv, av, cv = others(syn.get("payments")), others(api.get("payments")), others((csv or {}).get("payments"))
    if sv is not None or av is not None or cv is not None:
        prows.append(row("Autres / divers", sv, av, cv))
    sections.append({"name": "Paiements par mode (net encaissé)", "note": "", "rows": prows})

    balance = None
    if has_csv and balanced is not None:
        balance = {"debit": csv.get("debit"), "credit": csv.get("credit"), "ok": bool(balanced)}

    # détail des paiements de factures, limité aux modes qui ont un écart réconcilié
    recon_modes = {x["mode"] for x in recon}
    fac_detail = [d for d in (fac_detail or []) if d.get("mode") in recon_modes] if recon else []
    fac_detail = sorted(fac_detail, key=lambda d: (d.get("mode", ""), d.get("date", ""), d.get("fnum", 0)))
    return {"title": title, "meta": meta, "sections": sections, "reconciliation": recon,
            "fac_detail": fac_detail, "has_csv": has_csv, "balance": balance}


# ---------------------------------------------------------------- rendu TEXTE
def to_text(data) -> str:
    def cell(v):
        return f"{_fmt(v):>14}"

    def badge(st):
        return {"ok": "OK", "ecart": "⚠️ ÉCART", "na": "—", "reconciled": "✓ RÉCONCILIÉ"}[st]

    L = [data["title"], *data["meta"], ""]
    head = f"  {'':<22}{'Synthèse':>14}{'API tickets':>14}{'CSV agrégé':>14}    Match"
    for sec in data["sections"]:
        note = f"   ({sec['note']})" if sec["note"] else ""
        L.append(f"== {sec['name'].upper()} =={note}")
        L.append(head)
        for r in sec["rows"]:
            L.append(f"  {r['label']:<22}{cell(r['syn'])}{cell(r['api'])}{cell(r['csv'])}    {badge(r['match'])}")
        L.append("")
    if data.get("reconciliation"):
        L.append("== RÉCONCILIATION DES ÉCARTS DE PAIEMENT ==")
        for x in data["reconciliation"]:
            L.append(f"  {x['mode']} : tickets {_fmt(x['api'])} − synthèse {_fmt(x['syn'])} = écart {_fmt(x['ecart'])} €")
            L.append(f"     → cet écart vient de paiements de factures que la synthèse classe hors « caisse ».")
            L.append(f"     Total {x['mode']} de factures retraité (crédit 411, lettré F<n°>) : {_fmt(x['facture'])} €")
            L.append(f"     Contrôle : écart {_fmt(x['ecart'])} ≤ {_fmt(x['facture'])} → entièrement constitué de "
                     f"paiements de factures → COUVERT ✓")
        if data.get("fac_detail"):
            L.append("")
            L.append("  Détail des paiements de factures encaissés en caisse (bookés au 411) :")
            L.append(f"    {'Date':<12}{'Facture':<10}{'Client':<24}{'Mode':<12}{'Montant':>12}")
            for d in data["fac_detail"]:
                L.append(f"    {d['date']:<12}{('F' + str(d['fnum'])):<10}{(d['nm'] or '')[:22]:<24}"
                         f"{d['mode']:<12}{_fmt(d['amount']):>12}")
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
            if st in ("ok", "ecart", "reconciled"):
                col = _RED if st == "ecart" else _GREEN
                lbl = {"ok": "OK", "ecart": "ÉCART", "reconciled": "RÉCONCILIÉ"}[st]
                w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
                page.draw_rect(fitz.Rect(BADGE_X, y - 8, BADGE_X + w, y + 2.5), fill=col, color=col)
                page.insert_text((BADGE_X + 4, y), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))
            else:
                left(BADGE_X + 2, "—", size=9, color=_GREY)
            nl(14)
        nl(6)

    if data.get("reconciliation"):
        ensure(50)
        page.draw_rect(fitz.Rect(x0, y - 9, W - 40, y + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, "Réconciliation des écarts de paiement", size=10, font="hebo", color=(0.15, 0.15, 0.22))
        nl(16)
        for x in data["reconciliation"]:
            ensure(46)
            left(x0, f"{x['mode']} : tickets {_fmt(x['api'])}  −  synthèse {_fmt(x['syn'])}  =  écart {_fmt(x['ecart'])} EUR",
                 9, "cour", _DARK)
            # badge COUVERT à droite
            w = fitz.get_text_length("COUVERT", fontname="hebo", fontsize=8) + 8
            page.draw_rect(fitz.Rect(BADGE_X, y - 8, BADGE_X + w, y + 2.5), fill=_GREEN, color=_GREEN)
            page.insert_text((BADGE_X + 4, y), "COUVERT", fontsize=8, fontname="hebo", color=(1, 1, 1))
            nl(13)
            left(x0 + 6, "Cet écart vient de paiements de factures que la synthèse classe hors « caisse ».", 8, "helv", _GREY)
            nl(11)
            left(x0 + 6, f"Total {x['mode']} de factures retraité (crédit 411, lettré F<n°>) : {_fmt(x['facture'])} EUR.", 8, "helv", _GREY)
            nl(11)
            left(x0 + 6, f"Contrôle : écart {_fmt(x['ecart'])} <= {_fmt(x['facture'])} -> entièrement constitué de paiements de factures.", 8, "helv", _GREY)
            nl(15)
        if data.get("fac_detail"):
            left(x0, "Détail des paiements de factures encaissés en caisse :", 9, "hebo", (0.2, 0.2, 0.25))
            nl(13)
            left(x0 + 4, "Date", 8, "helv", _GREY)
            left(x0 + 70, "Facture", 8, "helv", _GREY)
            left(x0 + 140, "Client", 8, "helv", _GREY)
            left(x0 + 330, "Mode", 8, "helv", _GREY)
            right(BADGE_X + 40, "Montant", 8, "helv", _GREY)
            nl(12)
            for d in data["fac_detail"]:
                ensure(14)
                left(x0 + 4, d["date"], 8, "cour")
                left(x0 + 70, "F" + str(d["fnum"]), 8, "cour")
                left(x0 + 140, (d["nm"] or "")[:30], 8, "helv")
                left(x0 + 330, d["mode"], 8, "helv")
                right(BADGE_X + 40, _fmt(d["amount"]), 8, "cour")
                nl(12)
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
          batch_code=None, n_tickets=None, balanced=None, run_id=None, executed_at=None,
          fac_payments=None, fac_detail=None) -> str:
    return to_text(_compute(kind, establishment, date_from, date_to, syn, api, csv,
                            batch_code, n_tickets, balanced, run_id, executed_at, fac_payments, fac_detail))


def build_pdf(kind, establishment, date_from, date_to, syn, api, csv=None, *,
              batch_code=None, n_tickets=None, balanced=None, run_id=None, executed_at=None,
              fac_payments=None, fac_detail=None) -> bytes:
    return to_pdf(_compute(kind, establishment, date_from, date_to, syn, api, csv,
                           batch_code, n_tickets, balanced, run_id, executed_at, fac_payments, fac_detail))


def verify_pdf(establishment, journal, period_label, n_entries, used, summary, pay_rows,
               recon, cli_ok, fac_detail, accounts, coherent, run_id=None, executed_at=None) -> bytes:
    """Compte rendu PDF de vérification Pennylane DÉTAILLÉ : contrôles (CA/TVA/TTC/411),
    paiements par mode, réconciliation des paiements de factures, détail par compte,
    chacun comparant Attendu (CSV généré) vs Pennylane (lu via l'API)."""
    def _ascii(s):
        return (str(s).replace("—", "·").replace("→", "->").replace("€", "EUR")
                .replace("œ", "oe").replace("…", "..."))
    doc = fitz.open()
    page = doc.new_page()
    W = page.rect.width
    x0 = 40
    state = {"y": 56, "page": page}
    CA, CP, BX = 360, 460, 490

    def left(x, s, size=9, font="helv", color=_DARK):
        state["page"].insert_text((x, state["y"]), _ascii(s), fontsize=size, fontname=font, color=color)

    def right(xr, s, size=9, font="cour", color=_DARK):
        s = _ascii(s)
        state["page"].insert_text((xr - fitz.get_text_length(s, fontname=font, fontsize=size), state["y"]),
                                  s, fontsize=size, fontname=font, color=color)

    def nl(d=0):
        state["y"] += d

    def ensure(space=18):
        if state["y"] + space > 800:
            state["page"] = doc.new_page()
            state["y"] = 56

    def badge(ok, lbl_ok="OK", lbl_ko="ÉCART"):
        col = _GREEN if ok else _RED
        lbl = lbl_ok if ok else lbl_ko
        w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
        state["page"].draw_rect(fitz.Rect(BX, state["y"] - 8, BX + w, state["y"] + 2.5), fill=col, color=col)
        state["page"].insert_text((BX + 4, state["y"]), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))

    def section(name):
        ensure(36)
        state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, name, 10, "hebo", (0.15, 0.15, 0.22)); nl(15)
        right(CA, "Attendu (CSV)", 8, "helv", _GREY); right(CP, "Pennylane", 8, "helv", _GREY)
        left(BX, "Contrôle", 8, "helv", _GREY); nl(13)

    def line(lbl, ev, av, ok):
        ensure()
        left(x0 + 4, lbl, 9)
        right(CA, _fmt(ev)); right(CP, _fmt(av)); badge(ok); nl(14)

    left(x0, "Compte rendu · Vérification Pennylane (après import)", 15, "hebo"); nl(18)
    for m in [f"Établissement : {establishment}        Période couverte : {period_label}",
              f"Journaux : {journal}        Écritures Pennylane lues : {n_entries}        Lots : {', '.join(used) or '-'}"]:
        left(x0, m, 9, "helv", (0.3, 0.3, 0.3)); nl(13)
    trace = []
    if run_id is not None:
        trace.append(f"Tâche #{run_id}")
    if executed_at:
        trace.append(f"Vérifié le {executed_at}")
    if trace:
        left(x0, "        ".join(trace), 9, "helv", (0.3, 0.3, 0.3)); nl(13)
    nl(8)

    section("Contrôles (chiffre d'affaires & comptes clients)")
    for lbl, ev, av, ok in summary:
        line(lbl, ev, av, ok)
    nl(6)

    section("Paiements par mode (net encaissé)")
    for m, ev, av, ok in pay_rows:
        line(m, ev, av, ok)
    nl(6)

    ensure(40)
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 4), fill=_BAR, color=_BAR)
    left(x0 + 3, "Réconciliation des paiements de factures", 10, "hebo", (0.15, 0.15, 0.22)); nl(15)
    left(x0, "Paiements de factures encaissés en caisse (bookés au crédit du 411) — ils expliquent l'écart de", 8, "helv", _GREY); nl(11)
    left(x0, "mode de paiement vs la synthèse. On vérifie qu'ils sont bien présents dans Pennylane.", 8, "helv", _GREY); nl(15)
    for m, v in recon:
        ensure()
        left(x0 + 4, f"{m} sur factures", 9)
        right(CA, _fmt(v)); left(BX - 70, "bookés au 411 (lettrés F<n°>)", 8, "helv", _GREY); nl(14)
    ensure()
    left(x0 + 4, "Comptes clients 411 conformes CSV ↔ Pennylane", 9)
    badge(cli_ok, "CONFIRMÉ", "ÉCART"); nl(16)
    if fac_detail:
        left(x0, "Détail des paiements de factures :", 9, "hebo", (0.2, 0.2, 0.25)); nl(13)
        left(x0 + 4, "Date", 8, "helv", _GREY); left(x0 + 70, "Facture", 8, "helv", _GREY)
        left(x0 + 140, "Client", 8, "helv", _GREY); left(x0 + 330, "Mode", 8, "helv", _GREY)
        right(BX + 50, "Montant", 8, "helv", _GREY); nl(12)
        for x in fac_detail:
            ensure(14)
            left(x0 + 4, x["date"], 8, "cour"); left(x0 + 70, "F" + str(x["fnum"]), 8, "cour")
            left(x0 + 140, (x.get("nm") or "")[:30], 8, "helv"); left(x0 + 330, x["mode"], 8, "helv")
            right(BX + 50, _fmt(x["amount"]), 8, "cour"); nl(12)
        nl(6)

    section("Détail par compte")
    for num, ev, av, ok in accounts:
        line(str(num), ev, av, ok)
    nl(8)

    ensure(20)
    col = _GREEN if coherent else _RED
    msg = ("COHÉRENT — Pennylane correspond aux lots générés (tous les contrôles OK)"
           if coherent else "ÉCART — Pennylane diffère des lots générés")
    w = fitz.get_text_length(msg, fontname="hebo", fontsize=10) + 12
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, x0 + w, state["y"] + 4), fill=col, color=col)
    state["page"].insert_text((x0 + 6, state["y"]), _ascii(msg), fontsize=10, fontname="hebo", color=(1, 1, 1))
    out = doc.tobytes(); doc.close()
    return out
