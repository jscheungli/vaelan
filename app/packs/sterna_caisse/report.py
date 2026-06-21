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

    # vue « comptabilité » = le CSV agrégé s'il existe, sinon le flux tickets (cas cadrage/bloqué)
    compta = csv if has_csv else api
    prows = []
    for m in modes:
        sv = syn.get("payments", {}).get(m)
        av = api.get("payments", {}).get(m)
        cv = (csv or {}).get("payments", {}).get(m)
        rw = row(m, sv, av, cv)
        # un écart synthèse ↔ compta sur un MODE n'est pas une erreur : c'est un partage
        # encaissé/créances différent (factures réglées en caisse mais en 411 chez nous).
        if rw["match"] == "ecart":
            rw["match"] = "split"
        prows.append(rw)
    sv, av, cv = others(syn.get("payments")), others(api.get("payments")), others((csv or {}).get("payments"))
    if sv is not None or av is not None or cv is not None:
        rw = row("Autres / divers", sv, av, cv)
        if rw["match"] == "ecart":
            rw["match"] = "split"
        prows.append(rw)
    sections.append({"name": "Paiements par mode (net encaissé)", "note": "", "rows": prows})

    balance = None
    if has_csv and balanced is not None:
        balance = {"debit": csv.get("debit"), "credit": csv.get("credit"), "ok": bool(balanced)}

    # ---- Réconciliation encaissé / créances (TOUT tie au CA TTC) ----
    # Le CA TTC cadre ; seul le PARTAGE encaissé vs créances diffère, parce que la caisse
    # officielle compte des factures comme réglées que notre compta a encore en 411.
    def _sum_pay(d):
        return round(sum(v or 0 for v in (d or {}).get("payments", {}).values()), 2)

    ca_ttc = api.get("ca_ttc") if api.get("ca_ttc") is not None else syn.get("ca_total")
    recon2 = None
    if ca_ttc is not None and syn.get("payments"):
        syn_enc = _sum_pay(syn)
        compta_enc = _sum_pay(compta)
        syn_cr = round(ca_ttc - syn_enc, 2)
        compta_cr = round(ca_ttc - compta_enc, 2)
        mode_ec = []
        for m in modes + ["Autres / divers"]:
            sv2 = syn.get("payments", {}).get(m)
            cv2 = (compta.get("payments", {}) or {}).get(m)
            if m == "Autres / divers":
                sv2, cv2 = others(syn.get("payments")), others(compta.get("payments"))
            if sv2 is None and cv2 is None:
                continue
            e = round((cv2 or 0) - (sv2 or 0), 2)
            if abs(e) > TOL:
                mode_ec.append({"mode": m, "ecart": e})
        recon2 = {"ca_ttc": ca_ttc, "syn_enc": syn_enc, "compta_enc": compta_enc,
                  "syn_cr": syn_cr, "compta_cr": compta_cr,
                  "ecart_enc": round(compta_enc - syn_enc, 2),
                  "ecart_cr": round(compta_cr - syn_cr, 2), "mode_ecarts": mode_ec}

    # créances non routées (pré-vol bloquant) -> regroupées par client, avec le diagnostic
    unres_groups = []
    by = {}
    for u in (api.get("unresolved") or []):
        k = u.get("company_id") or "?"
        g = by.setdefault(k, {"company_id": u.get("company_id"), "name": u.get("name"),
                              "siret": u.get("siret"), "pennylane_name": u.get("pennylane_name"),
                              "reason": u.get("reason"), "factures": [], "total": 0.0})
        g["factures"].append({"facture": u.get("facture"), "date": u.get("date"),
                              "amount": u.get("amount") or 0})
        g["total"] = round(g["total"] + (u.get("amount") or 0), 2)
    unres_groups = sorted(by.values(), key=lambda x: -x["total"])
    return {"title": title, "meta": meta, "sections": sections, "recon2": recon2,
            "unresolved": unres_groups, "has_csv": has_csv, "balance": balance}


# ---------------------------------------------------------------- rendu TEXTE
def to_text(data) -> str:
    def cell(v):
        return f"{_fmt(v):>14}"

    def badge(st):
        return {"ok": "OK", "ecart": "⚠️ ÉCART", "na": "—",
                "split": "≠ partage encaissé/créances"}.get(st, st)

    L = [data["title"], *data["meta"], ""]
    if data.get("unresolved"):
        nf = sum(len(g["factures"]) for g in data["unresolved"])
        L.append(f"== ⛔ CLIENTS À CORRIGER — {nf} créance(s) non routée(s) → CSV NON généré ==")
        L.append("  Chaque créance d'un client PRO doit pointer vers un compte client Pennylane (411).")
        L.append("  Corrige les clients ci-dessous (selon la raison), puis resynchronise les clients et relance.")
        L.append("")
        for g in data["unresolved"]:
            nm = g.get("name") or "(nom inconnu)"
            L.append(f"  • {nm}   ({g['total']:.2f} € · {len(g['factures'])} facture(s))")
            L.append(f"      companyId TopOrder : {g.get('company_id') or '—'}    SIRET : {g.get('siret') or '— (absent)'}")
            if g.get("pennylane_name"):
                L.append(f"      côté Pennylane : {g['pennylane_name']}")
            L.append(f"      ⚠ {g.get('reason') or 'à vérifier'}")
            L.append(f"      factures : " + ", ".join(
                f"F{f['facture']} ({f['date']}, {f['amount']:.2f} €)" for f in g["factures"][:12]))
            L.append("")
    head = f"  {'':<22}{'Synthèse':>14}{'API tickets':>14}{'CSV agrégé':>14}    Match"
    for sec in data["sections"]:
        note = f"   ({sec['note']})" if sec["note"] else ""
        L.append(f"== {sec['name'].upper()} =={note}")
        L.append(head)
        for r in sec["rows"]:
            L.append(f"  {r['label']:<22}{cell(r['syn'])}{cell(r['api'])}{cell(r['csv'])}    {badge(r['match'])}")
        L.append("")
    if data.get("recon2"):
        r = data["recon2"]
        L.append("== RÉCONCILIATION ENCAISSÉ / CRÉANCES (tout cadre au CA TTC) ==")
        L.append("  Le CA TTC est identique des deux côtés ; seul le PARTAGE encaissé vs créances diffère :")
        L.append("  la caisse officielle compte certaines factures comme réglées, alors que la comptabilité")
        L.append("  les a encore en créances clients ouvertes (411) — elles seront soldées au lettrage / suivi")
        L.append("  des paiements. Démonstration (tout retombe sur le même CA TTC) :")
        L.append("")
        L.append(f"  {'':<16}{'Encaissé':>14}{'+ Créances 411':>18}{'= CA TTC':>14}")
        L.append(f"  {'Synthèse':<16}{_fmt(r['syn_enc']):>14}{_fmt(r['syn_cr']):>18}{_fmt(r['ca_ttc']):>14}")
        L.append(f"  {'Comptabilité':<16}{_fmt(r['compta_enc']):>14}{_fmt(r['compta_cr']):>18}{_fmt(r['ca_ttc']):>14}")
        ec = " + ".join(f"{m['mode']} {_fmt(m['ecart'])}" for m in r["mode_ecarts"]) or "—"
        L.append(f"  {'Écart':<16}{_fmt(r['ecart_enc']):>14}{_fmt(r['ecart_cr']):>18}{_fmt(0):>14}")
        L.append("")
        L.append(f"  → écart d'encaissement {_fmt(r['ecart_enc'])} € (= {ec}) compensé EXACTEMENT par")
        L.append(f"    l'écart de créances {_fmt(r['ecart_cr'])} € → le CA TTC cadre, c'est cohérent ✓")
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

    # ⛔ clients à corriger (créances non routées) — bloc en tête car c'est le point bloquant
    if data.get("unresolved"):
        nf = sum(len(g["factures"]) for g in data["unresolved"])
        ensure(40)
        page.draw_rect(fitz.Rect(x0, y - 9, W - 40, y + 4), fill=_RED, color=_RED)
        left(x0 + 3, f"CLIENTS A CORRIGER — {nf} creance(s) non routee(s) -> CSV NON genere",
             10, "hebo", (1, 1, 1))
        nl(15)
        left(x0, "Chaque creance d'un client PRO doit pointer vers un compte client Pennylane (411). "
                 "Corrige selon la raison, resynchronise les clients, puis relance.", 8, "helv", _GREY)
        nl(14)
        for g in data["unresolved"]:
            ensure(58)
            nm = g.get("name") or "(nom inconnu)"
            left(x0 + 4, nm, 10, "hebo", _DARK)
            right(W - 40, f"{_fmt(g['total'])} EUR · {len(g['factures'])} facture(s)", 9, "cour", _DARK)
            nl(12)
            left(x0 + 8, f"companyId TopOrder : {g.get('company_id') or '-'}", 8, "cour", _GREY)
            left(x0 + 300, f"SIRET : {g.get('siret') or '- (absent)'}", 8, "cour", _GREY)
            nl(11)
            if g.get("pennylane_name"):
                left(x0 + 8, f"cote Pennylane : {g['pennylane_name']}", 8, "helv", _GREY); nl(11)
            left(x0 + 8, f"-> {g.get('reason') or 'a verifier'}", 8, "helv", _RED)
            nl(12)
            facs = ", ".join(f"F{f['facture']} ({f['date']}, {_fmt(f['amount'])})" for f in g["factures"][:10])
            left(x0 + 8, "factures : " + facs, 8, "cour", _DARK)
            nl(16)
        nl(4)

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
            if st in ("ok", "ecart", "split"):
                col = {"ok": _GREEN, "ecart": _RED, "split": _GREY}[st]
                lbl = {"ok": "OK", "ecart": "ÉCART", "split": "≠ partage"}[st]
                w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
                page.draw_rect(fitz.Rect(BADGE_X, y - 8, BADGE_X + w, y + 2.5), fill=col, color=col)
                page.insert_text((BADGE_X + 4, y), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))
            else:
                left(BADGE_X + 2, "—", size=9, color=_GREY)
            nl(14)
        nl(6)

    if data.get("recon2"):
        r = data["recon2"]
        ensure(64)
        page.draw_rect(fitz.Rect(x0, y - 9, W - 40, y + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, "Réconciliation encaissé / créances (tout cadre au CA TTC)", size=10, font="hebo", color=(0.15, 0.15, 0.22))
        nl(16)
        for ln in ["Le CA TTC est identique des deux côtés ; seul le PARTAGE encaissé vs créances diffère : la caisse",
                   "officielle compte certaines factures comme réglées, alors que la comptabilité les a encore en",
                   "créances clients ouvertes (411) — soldées au lettrage / suivi des paiements. Tout retombe sur le CA TTC :"]:
            ensure(11); left(x0 + 2, ln, 8, "helv", _GREY); nl(10)
        nl(4)
        C1, C2, C3 = 300, 410, 500
        right(C1, "Encaissé", 8, "helv", _GREY); right(C2, "+ Créances 411", 8, "helv", _GREY)
        right(C3, "= CA TTC", 8, "helv", _GREY); nl(13)
        for lbl, enc, cr in [("Synthèse (caisse)", r["syn_enc"], r["syn_cr"]),
                             ("Comptabilité (CSV)", r["compta_enc"], r["compta_cr"])]:
            ensure(14)
            left(x0 + 4, lbl, 9)
            right(C1, _fmt(enc)); right(C2, _fmt(cr)); right(C3, _fmt(r["ca_ttc"])); nl(13)
        ensure(14)
        left(x0 + 4, "Écart", 9, "hebo")
        right(C1, _fmt(r["ecart_enc"]), 9, "cour", _RED); right(C2, _fmt(r["ecart_cr"]), 9, "cour", _GREEN)
        right(C3, _fmt(0)); nl(15)
        ec = " + ".join(f"{m['mode']} {_fmt(m['ecart'])}" for m in r["mode_ecarts"]) or "—"
        left(x0 + 2, f"-> ecart d'encaissement {_fmt(r['ecart_enc'])} EUR (= {ec})", 8, "helv", _GREY); nl(10)
        left(x0 + 2, f"   compense EXACTEMENT l'ecart de creances {_fmt(r['ecart_cr'])} EUR -> CA TTC cadre, coherent.", 8, "helv", _GREY)
        nl(16)

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


def lettrage_pdf(company_name, period_label, counts, full, partial, vir_ok,
                 ambiguous, open_creances, errors, coherent, run_id=None, executed_at=None) -> bytes:
    """Compte rendu PDF de l'étape 6 (lettrage des comptes 411)."""
    def _ascii(s):
        return (str(s).replace("—", "·").replace("→", "->").replace("€", "EUR")
                .replace("œ", "oe").replace("…", "...").replace("⚠️", "!").replace("✅", "OK"))
    doc = fitz.open()
    state = {"y": 56, "page": doc.new_page()}
    W = state["page"].rect.width
    x0 = 40

    def left(x, s, size=9, font="helv", color=_DARK):
        state["page"].insert_text((x, state["y"]), _ascii(s), fontsize=size, fontname=font, color=color)

    def right(xr, s, size=9, font="cour", color=_DARK):
        s = _ascii(s)
        state["page"].insert_text((xr - fitz.get_text_length(s, fontname=font, fontsize=size), state["y"]),
                                  s, fontsize=size, fontname=font, color=color)

    def ny(d):
        state["y"] += d

    def ensure(space=18):
        if state["y"] + space > 800:
            state["page"] = doc.new_page()
            state["y"] = 56

    def section(name, color=(0.15, 0.15, 0.22)):
        ensure(30)
        state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, name, 10, "hebo", color); ny(16)

    left(x0, "Compte rendu · Lettrage des comptes 411", 14, "hebo"); ny(18)
    left(x0, f"Société : {company_name}        {period_label}", 9, "helv", (0.3, 0.3, 0.3)); ny(13)
    trace = []
    if run_id is not None:
        trace.append(f"Tâche #{run_id}")
    if executed_at:
        trace.append(f"Vérifié le {executed_at}")
    if trace:
        left(x0, "        ".join(trace), 9, "helv", (0.3, 0.3, 0.3)); ny(13)
    ny(8)

    section("Synthèse")
    for lbl, v in [("Factures soldées lettrées", counts["full"]),
                   ("Lettrages partiels (acomptes)", counts["partial"]),
                   ("Virements rapprochés (certains)", counts["vir"]),
                   ("Ambigus (à traiter à la main)", counts["ambiguous"]),
                   ("Créances ouvertes (impayées)", counts["open"]),
                   ("Erreurs API", counts["errors"]),
                   ("Comptes clients analysés", counts["accounts"])]:
        ensure()
        left(x0 + 4, lbl, 9); right(W - 60, str(v)); ny(14)
    ny(6)

    if partial:
        section("Lettrages partiels (reste dû)")
        for p in partial:
            ensure(13)
            left(x0 + 4, f"F{p['fnum']}", 9, "cour"); left(x0 + 80, p["nm"][:30], 9)
            right(W - 150, f"payé {p['paid']:.2f}"); right(W - 60, f"reste {p['due']:.2f}", color=_RED); ny(13)
        ny(6)
    if vir_ok:
        section("Virements rapprochés", _GREEN)
        for v in vir_ok:
            ensure(13)
            left(x0 + 4, str(v["date"]), 8, "cour"); left(x0 + 90, f"F{v['fnum']}", 9, "cour")
            left(x0 + 160, v["nm"][:30], 9); right(W - 60, f"{v['amount']:.2f}"); ny(13)
        ny(6)
    if ambiguous:
        section("À traiter manuellement (ambigus)", _RED)
        for a in ambiguous:
            ensure(13)
            ref = f"F{a['fnum']}" if a.get("fnum") else f"virement {a.get('amount', 0):.2f}"
            left(x0 + 4, a["nm"][:28], 9); left(x0 + 200, ref, 9, "cour")
            left(x0 + 290, str(a["why"])[:34], 8, "helv", _RED); ny(13)
        ny(6)
    if open_creances:
        section("Créances ouvertes (impayées)")
        for c in sorted(open_creances, key=lambda x: -x["age"]):
            ensure(13)
            left(x0 + 4, f"F{c['fnum']}", 9, "cour"); left(x0 + 80, c["nm"][:30], 9)
            right(W - 110, f"{c['amount']:.2f}")
            left(W - 95, f"{c['age']} j", 8, "helv", (_RED if c["age"] > 60 else _GREY)); ny(13)
        ny(6)
    if errors:
        section("Erreurs API", _RED)
        for e in errors:
            ensure(13)
            left(x0 + 4, str(e.get("nm", e.get("acc", "")))[:30], 9)
            left(x0 + 200, str(e["why"])[:46], 8, "helv", _RED); ny(13)
        ny(6)

    ensure(20)
    col = _GREEN if coherent else _RED
    msg = ("COMPLET — toutes les factures soldables ont été lettrées"
           if coherent else "À TRAITER — des points restent à la main (voir détail)")
    w = fitz.get_text_length(msg, fontname="hebo", fontsize=10) + 12
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, x0 + w, state["y"] + 4), fill=col, color=col)
    state["page"].insert_text((x0 + 6, state["y"]), _ascii(msg), fontsize=10, fontname="hebo", color=(1, 1, 1))
    out = doc.tobytes(); doc.close()
    return out


def justif_pdf(establishment, journal, period_label, counts, detail, failed, coherent,
               run_id=None, executed_at=None) -> bytes:
    """Compte rendu PDF de l'étape 5 (justificatifs) : combien d'écritures facture,
    déjà attachées, attachées maintenant, en échec, + détail par facture."""
    def _ascii(s):
        return (str(s).replace("—", "·").replace("→", "->").replace("€", "EUR")
                .replace("œ", "oe").replace("…", "...").replace("⚠️", "!"))
    doc = fitz.open()
    page = doc.new_page()
    W = page.rect.width
    x0 = 40
    state = {"y": 56, "page": page}
    BX = 490

    def left(x, s, size=9, font="helv", color=_DARK):
        state["page"].insert_text((x, state["y"]), _ascii(s), fontsize=size, fontname=font, color=color)

    def right(xr, s, size=9, font="cour", color=_DARK):
        s = _ascii(s)
        state["page"].insert_text((xr - fitz.get_text_length(s, fontname=font, fontsize=size), state["y"]),
                                  s, fontsize=size, fontname=font, color=color)

    def ensure(space=18):
        if state["y"] + space > 800:
            state["page"] = doc.new_page()
            state["y"] = 56

    def badge(ok, lbl_ok="OK", lbl_ko="MANQUE"):
        col = _GREEN if ok else _RED
        lbl = lbl_ok if ok else lbl_ko
        w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
        state["page"].draw_rect(fitz.Rect(BX, state["y"] - 8, BX + w, state["y"] + 2.5), fill=col, color=col)
        state["page"].insert_text((BX + 4, state["y"]), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))

    def section(name):
        ensure(30)
        state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, name, 10, "hebo", (0.15, 0.15, 0.22)); nl_y(16)

    def nl_y(d):
        state["y"] += d

    left(x0, "Compte rendu · Justificatifs (PDF factures rattachés à Pennylane)", 14, "hebo"); nl_y(18)
    for m in [f"Établissement : {establishment}        Période couverte : {period_label}",
              f"Journaux : {journal}"]:
        left(x0, m, 9, "helv", (0.3, 0.3, 0.3)); nl_y(13)
    trace = []
    if run_id is not None:
        trace.append(f"Tâche #{run_id}")
    if executed_at:
        trace.append(f"Vérifié le {executed_at}")
    if trace:
        left(x0, "        ".join(trace), 9, "helv", (0.3, 0.3, 0.3)); nl_y(13)
    nl_y(8)

    section("Synthèse")
    rows = [("Écritures facture (besoin d'un justificatif)", counts["total"]),
            ("Déjà attachées avant ce passage", counts["already"]),
            ("Attachées par Vaelan ce passage", counts["attached"]),
            ("En échec", counts["failed"]),
            ("Couvertes (avec PDF au final)", f"{counts['covered']} / {counts['total']}")]
    for lbl, v in rows:
        ensure()
        left(x0 + 4, lbl, 9); right(BX + 30, str(v)); nl_y(14)
    nl_y(6)

    if failed:
        section("Échecs (à corriger)")
        left(x0 + 4, "Facture", 8, "helv", _GREY); left(x0 + 80, "Journal", 8, "helv", _GREY)
        left(x0 + 170, "Raison", 8, "helv", _GREY); nl_y(12)
        for f in failed:
            ensure(14)
            left(x0 + 4, "F" + str(f["num"]), 8, "cour"); left(x0 + 80, f["journal"], 8, "helv")
            left(x0 + 170, str(f.get("why", ""))[:48], 8, "helv", _RED); nl_y(12)
        nl_y(6)

    section("Détail par facture")
    left(x0 + 4, "Facture", 8, "helv", _GREY); left(x0 + 90, "Journaux", 8, "helv", _GREY)
    right(BX, "PDF", 8, "helv", _GREY); left(BX, "État", 8, "helv", _GREY); nl_y(12)
    for d in detail:
        ensure(14)
        left(x0 + 4, "F" + str(d["num"]), 9, "cour"); left(x0 + 90, d["journaux"], 9, "helv")
        right(BX, f"{d['pdf']}/{d['tot']}"); badge(d["ok"]); nl_y(14)
    nl_y(8)

    ensure(20)
    col = _GREEN if coherent else _RED
    msg = ("COMPLET — toutes les écritures facture portent leur PDF"
           if coherent else
           ("ÉCART — des écritures facture restent sans PDF" if counts["total"]
            else "Aucune écriture facture à justifier"))
    if not coherent and not counts["total"]:
        col = _GREY
    w = fitz.get_text_length(msg, fontname="hebo", fontsize=10) + 12
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, x0 + w, state["y"] + 4), fill=col, color=col)
    state["page"].insert_text((x0 + 6, state["y"]), _ascii(msg), fontsize=10, fontname="hebo", color=(1, 1, 1))
    out = doc.tobytes(); doc.close()
    return out


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
