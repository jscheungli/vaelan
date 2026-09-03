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


# Normalisation des libellés de mode de paiement vers les libellés de la synthèse
# (gère les variantes TopOrder : « Carte »→CB, « Espece »→Espèce, etc.). Tout libellé
# inconnu est conservé tel quel — ainsi un mode non mappé (ex. Chèque) cadre quand même.
_MODE_LABEL = {
    "CB": "CB", "Carte": "CB", "Carte bancaire": "CB", "Carte Bancaire": "CB",
    "Espèce": "Espèce", "Espece": "Espèce", "Espèces": "Espèce", "Especes": "Espèce",
    "Chèque": "Chèque", "Cheque": "Chèque", "Chèques": "Chèque",
    "Ticket restaurant": "Ticket restaurant", "Titre restaurant": "Ticket restaurant",
    "Ticket Restaurant": "Ticket restaurant",
}


def aggregate_rows(rows, cfg, journal=None, pay_journal=None) -> dict:
    """Relit les lignes du CSV généré et reconstitue les totaux par poste.

    `journal` : CA/TVA/équilibre agrégés sur CE journal seulement (le CSV unique
    contient TOSLT + TOSLF + TOSLP ; pour comparer le CA au Z on ne prend que la
    caisse TOSLT, sinon le reclassement TOSLF compterait en double).
    `pay_journal` : les ENCAISSEMENTS sont agrégés sur le journal de ventes (caisse
    anonyme) ET le journal de paiement (règlements de factures), pour totaliser tout
    ce qui a été encaissé (= le Z)."""
    ca_acc = cfg["ca_anonyme"]
    rev_tva = {v: k for k, v in cfg["tva"].items()}
    ht_by_rate, tva_by_rate, pay = defaultdict(float), defaultdict(float), defaultdict(float)
    deb = cred = 0.0
    pay_journals = {j for j in (journal, pay_journal) if j} or None
    pay_accs = set()                    # comptes d'encaissement (caisse) ; KK facture -> aucun
    for a in cfg["est"].values():
        for k in ("cb", "especes", "ticket_resto", "cheque", "autres"):
            if a.get(k):
                pay_accs.add(a[k])
    for r in rows:
        rj, acc, taux, d, c = r[1], r[2], r[5], float(r[7] or 0), float(r[8] or 0)
        if not journal or rj == journal:        # CA/TVA/équilibre : journal de ventes seul
            deb += d
            cred += c
            if acc == ca_acc:                   # CA = crédit (vente) - débit (avoir)
                ht_by_rate[_rate_from_label(taux)] += c - d
            elif acc in rev_tva:                # TVA = crédit - débit (avoir)
                tva_by_rate[rev_tva[acc]] += c - d
        if acc in pay_accs and (not pay_journals or rj in pay_journals):
            # mode = libellé de la ligne (= le paymentType écrit par le moteur), normalisé
            # vers le libellé de la synthèse -> un chèque (compte « autres ») cadre quand même.
            mode = _MODE_LABEL.get(r[3], r[3])
            pay[mode] += d - c                  # encaissement = débit - crédit (rendu)
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
    title = ("Compte rendu — Cadrage + génération du CSV"
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

    def row(label, sv, av, cv, cmp_syn=True):
        # cmp_syn=False : on ne cadre QUE API↔CSV (la synthèse, ventilée par famille, est
        # indicative pour HT/TVA — seul le CA TTC est strictement comparé à la synthèse).
        m = _match(sv, av, cv) if cmp_syn else _match(av, cv)
        return {"label": label, "syn": sv, "api": av, "csv": cv, "match": m}

    sections = []

    # CA global
    syn_ttc, syn_ht = syn.get("ca_total"), syn.get("ca_ht")
    syn_tva = round(syn_ttc - syn_ht, 2) if (syn_ttc is not None and syn_ht is not None) else None
    sections.append({"name": "Chiffre d'affaires",
                     "note": "HT/TVA : synthèse ventilée par famille (arrondi ≠) → cadrage API↔CSV ; le CA TTC fait foi face à la synthèse.",
                     "rows": [
        row("CA HT total", syn_ht, api.get("ca_ht"), (csv or {}).get("ca_ht"), cmp_syn=False),
        row("TVA total", syn_tva, api.get("tva"), (csv or {}).get("tva"), cmp_syn=False),
        row("CA TTC total", syn_ttc, api.get("ca_ttc"), (csv or {}).get("ca_ttc")),
    ]})

    # Par TAUX : la répartition HT/TVA du CSV est arrondie PAR PIÈCE (la TVA de chaque pièce
    # = complément de son TTC exact), alors que la colonne API somme les valeurs brutes. Sur
    # des dizaines de pièces, les arrondis dérivent de quelques centimes en sens OPPOSÉS
    # (HT −x / TVA +x), TTC inchangé. Ce n'est PAS un écart : on ne flague un taux que si
    # son TTC (HT + TVA) diverge réellement entre API et CSV.
    def _rate_match(rt, av, cv):
        """État d'une ligne par taux : « ok » (identique au centime), « arrondi » (la ligne
        diffère de quelques centimes MAIS le TTC du taux est identique — dérive d'arrondi
        par pièce, compensée entre HT et TVA), « ecart » (vraie divergence)."""
        if csv is None or (av is None and cv is None):
            return _match(av, cv)
        if abs((av or 0.0) - (cv or 0.0)) < TOL:
            return "ok"
        ah = api.get("ht_by_rate", {}).get(rt) or 0.0
        ch = (csv.get("ht_by_rate", {}) or {}).get(rt) or 0.0
        at = api.get("tva_by_rate", {}).get(rt) or 0.0
        ct = (csv.get("tva_by_rate", {}) or {}).get(rt) or 0.0
        return "arrondi" if abs((ah + at) - (ch + ct)) < TOL else "ecart"

    _ARRONDI_NOTE = ("≈ arrondi : la répartition HT/TVA du CSV est arrondie PIÈCE PAR PIÈCE "
                     "(la TVA de chaque pièce complète son TTC exact) ; la ligne diffère de "
                     "quelques centimes de la somme brute, mais HT + TVA du taux est IDENTIQUE "
                     "au centime — rien ne manque, ça matche au final.")

    # HT par taux
    rates = sorted(set(list(api.get("ht_by_rate", {})) + list((csv or {}).get("ht_by_rate", {}))),
                   key=lambda x: float(x))
    hrows = [{"label": f"HT {rt}%", "syn": None,
              "api": api.get("ht_by_rate", {}).get(rt),
              "csv": (csv or {}).get("ht_by_rate", {}).get(rt),
              "match": _rate_match(rt, api.get("ht_by_rate", {}).get(rt),
                                   (csv or {}).get("ht_by_rate", {}).get(rt))} for rt in rates]
    sections.append({"name": "HT par taux de TVA", "note": "synthèse : n/a (ventile par famille)",
                     "rows": hrows,
                     "footnote": (_ARRONDI_NOTE if any(r["match"] == "arrondi" for r in hrows) else None)})

    # TVA par taux (on masque les taux sans TVA)
    rates = sorted(set(list(api.get("tva_by_rate", {})) + list((csv or {}).get("tva_by_rate", {}))),
                   key=lambda x: float(x))
    trows = []
    for rt in rates:
        av = api.get("tva_by_rate", {}).get(rt)
        cv = (csv or {}).get("tva_by_rate", {}).get(rt)
        if abs(av or 0) < TOL and abs(cv or 0) < TOL:
            continue
        trows.append({"label": f"TVA {rt}%", "syn": None, "api": av, "csv": cv,
                      "match": _rate_match(rt, av, cv)})
    sections.append({"name": "TVA par taux", "note": "synthèse : n/a", "rows": trows,
                     "footnote": (_ARRONDI_NOTE if any(r["match"] == "arrondi" for r in trows) else None)})

    # paiements
    modes = ["CB", "Espèce", "Chèque", "Ticket restaurant"]

    def others(d):
        s = round(sum(v for k, v in (d or {}).items() if k not in modes), 2)
        return s if abs(s) >= TOL else None

    # vue « tickets » = le CSV agrégé s'il existe, sinon le flux tickets (cas cadrage/bloqué)
    compta = csv if has_csv else api
    facp = fac_payments or {}
    balance = None
    if has_csv and balanced is not None:
        balance = {"debit": csv.get("debit"), "credit": csv.get("credit"), "ok": bool(balanced)}

    # ---- Encaissements par mode — CADRAGE FACTUEL ----
    # La synthèse donne, par mode, un TOTAL DE CAISSE = encaissements − remboursements (rendu).
    # Notre CSV somme la MÊME source (ticketpaymentdata, par date de paiement) -> les deux DOIVENT
    # être identiques. Un écart = vraie anomalie (affiché en ROUGE), pas une « réconciliation ».
    ca_total = syn.get("ca_total")
    syn_det = syn.get("payments_detail") or {}
    paydetail = None
    if syn.get("payments"):
        mode_list = list(dict.fromkeys(list(syn.get("payments", {}).keys()) + modes))
        rows_pd = []
        for m in mode_list:
            st = syn.get("payments", {}).get(m)                 # total net synthèse
            ot = (compta.get("payments", {}) or {}).get(m)      # total nos tickets
            ofac = round(facp.get(m, 0.0), 2)                   # dont factures
            oanon = round((ot or 0) - ofac, 2) if ot is not None else None
            d = syn_det.get(m, {})
            ec = round((ot or 0) - (st or 0), 2)
            rows_pd.append({"mode": m, "syn_total": st,
                            "syn_enc": d.get("encaissements"), "syn_remb": d.get("remboursements"),
                            "our_total": ot, "our_anon": oanon, "our_fac": ofac,
                            "ecart": ec, "match": abs(ec) < TOL})
        syn_modes = round(sum(v or 0 for v in syn.get("payments", {}).values()), 2)
        our_modes = round(sum(v or 0 for v in (compta.get("payments", {}) or {}).values()), 2)
        # bilan qui CADRE sur le CA Total des deux côtés (chaque nombre est vérifiable) :
        #   encaissé au comptoir + part non encaissée = CA Total.
        #   synthèse : part non encaissée = CA − Σmodes (déduite, pas un poste du PDF).
        #   CSV : part non encaissée = solde des comptes clients 411 (sommable dans le CSV).
        paydetail = {
            "rows": rows_pd,
            "syn_modes_total": syn_modes,
            "our_modes_total": our_modes,
            "entree_caisse": syn.get("entree_caisse"),
            "sortie_caisse": syn.get("sortie_caisse"),
            "ca_total": ca_total,
            "syn_noncaisse": round(ca_total - syn_modes, 2) if ca_total is not None else None,
            "our_noncaisse": round(ca_total - our_modes, 2) if ca_total is not None else None,
            "ecart_encaisse": round(our_modes - syn_modes, 2),
            "all_match": all(r["match"] for r in rows_pd),
        }

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
    # cohérence GLOBALE : tous les rapprochements OK (ou n/a) + tous les modes cadrent +
    # CSV équilibré + aucune créance non routée. Sert au VERDICT du résumé de tâche.
    coherent = (all(r["match"] in ("ok", "na", "arrondi") for s in sections for r in s["rows"])
                and (paydetail is None or paydetail.get("all_match", True))
                and (balance is None or balance.get("ok", True))
                and not unres_groups)
    return {"title": title, "meta": meta, "sections": sections, "paydetail": paydetail,
            "unresolved": unres_groups, "has_csv": has_csv, "balance": balance,
            "coherent": coherent}


# ---------------------------------------------------------------- rendu TEXTE
def to_text(data) -> str:
    def cell(v):
        return f"{_fmt(v):>14}"

    def badge(st):
        return {"ok": "OK", "ecart": "⚠️ ÉCART", "na": "—", "arrondi": "≈ arrondi (compensé)",
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
        if sec.get("footnote"):
            L.append(f"  {sec['footnote']}")
        L.append("")
    if data.get("paydetail"):
        p = data["paydetail"]
        ok = p.get("all_match")
        L.append("== ENCAISSEMENTS PAR MODE — SYNTHÈSE vs CSV (doivent être identiques) ==")
        L.append("  Même source des deux côtés (ticketpaymentdata, par date de paiement). Un écart = anomalie.")
        L.append(f"  {'Mode':<16}{'Synthèse':>14}{'Notre CSV':>14}    État")
        for r in p["rows"]:
            L.append(f"  {r['mode']:<16}{cell(r['syn_total'])}{cell(r['our_total'])}    "
                     f"{'OK' if r['match'] else '⚠️ ÉCART ' + _fmt(r['ecart'])}")
        L.append("  " + ("✅ Encaissements cadrés (tous les modes = synthèse)."
                         if ok else "❌ ÉCART D'ENCAISSEMENT — ne cadre pas avec la synthèse (à corriger)."))
        fac_tot = round(sum(r.get("our_fac") or 0 for r in p["rows"]), 2)
        if abs(fac_tot) > TOL:
            L.append(f"  dont RÈGLEMENTS DE FACTURES (journal d'encaissements dédié, à lettrer au 411) : "
                     f"{_fmt(fac_tot)} EUR")
        L.append("")
        L.append("  BILAN — répartition encaissé / créances (cadre sur le CA Total) :")
        L.append(f"    {'':<14}{'Encaissé comptoir':>20}{'+ Créances 411':>18}{'= CA Total':>14}")
        L.append(f"    {'Synthèse':<14}{_fmt(p['syn_modes_total']):>20}{_fmt(p['syn_noncaisse']):>18}{_fmt(p['ca_total']):>14}")
        L.append(f"    {'Notre CSV':<14}{_fmt(p['our_modes_total']):>20}{_fmt(p['our_noncaisse']):>18}{_fmt(p['ca_total']):>14}")
        L.append("    (synthèse : créances = CA − total des modes ; CSV : solde des comptes 411 — sommable)")
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
_BLUE = (0.16, 0.42, 0.75)   # « ≈ arrondi » : ligne décalée de qq centimes mais compensée (TTC exact)
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
    page.draw_rect(fitz.Rect(x0 + 126, y - 7, x0 + 136, y + 1), fill=_BLUE, color=_BLUE)
    left(x0 + 140, "écart d'arrondi compensé (voir note)", size=8, color=_GREY)
    left(x0 + 310, "—  non comparable (source absente)", size=8, color=_GREY)
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
            if st in ("ok", "ecart", "split", "arrondi"):
                col = {"ok": _GREEN, "ecart": _RED, "split": _GREY, "arrondi": _BLUE}[st]
                lbl = {"ok": "OK", "ecart": "ÉCART", "split": "≠ partage", "arrondi": "≈ arrondi *"}[st]
                w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
                page.draw_rect(fitz.Rect(BADGE_X, y - 8, BADGE_X + w, y + 2.5), fill=col, color=col)
                page.insert_text((BADGE_X + 4, y), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))
            else:
                left(BADGE_X + 2, "—", size=9, color=_GREY)
            nl(14)
        if sec.get("footnote"):
            # note « * » sous la section (repliée sur plusieurs lignes), en bleu
            words = _ascii("* " + sec["footnote"]).split()
            line = ""
            for wd in words:
                if len(line) + len(wd) + 1 > 118:
                    ensure(12)
                    left(x0 + 4, line, size=7.5, color=_BLUE); nl(10)
                    line = wd
                else:
                    line = (line + " " + wd).strip()
            if line:
                ensure(12)
                left(x0 + 4, line, size=7.5, color=_BLUE); nl(10)
        nl(6)

    if data.get("paydetail"):
        p = data["paydetail"]
        ok_all = p.get("all_match")
        ensure(64)
        page.draw_rect(fitz.Rect(x0, y - 9, W - 40, y + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, "Encaissements par mode — synthèse vs CSV (doivent être identiques)", size=10, font="hebo", color=(0.15, 0.15, 0.22))
        nl(15)
        left(x0 + 2, "Même source des deux côtés (ticketpaymentdata, par date de paiement). Un écart = anomalie (rouge).",
             8, "helv", _GREY); nl(13)
        right(COL["syn"], "Synthèse", 8, "helv", _GREY); right(COL["api"], "Notre CSV", 8, "helv", _GREY)
        left(BADGE_X, "État", 8, "helv", _GREY); nl(13)
        for r in p["rows"]:
            ensure(15)
            left(x0 + 4, r["mode"], 9)
            right(COL["syn"], _fmt(r["syn_total"])); right(COL["api"], _fmt(r["our_total"]))
            col = _GREEN if r["match"] else _RED
            lbl = "OK" if r["match"] else "ÉCART"
            w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
            page.draw_rect(fitz.Rect(BADGE_X, y - 8, BADGE_X + w, y + 2.5), fill=col, color=col)
            page.insert_text((BADGE_X + 4, y), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))
            if not r["match"]:
                right(BADGE_X - 4, _fmt(r["ecart"]), 8, "cour", _RED)
            nl(14)
        nl(2)
        vcol = _GREEN if ok_all else _RED
        vmsg = ("Encaissements cadrés (tous les modes = synthèse)" if ok_all
                else "ÉCART D'ENCAISSEMENT — ne cadre pas avec la synthèse")
        w = fitz.get_text_length(vmsg, fontname="hebo", fontsize=9) + 12
        page.draw_rect(fitz.Rect(x0, y - 8, x0 + w, y + 3), fill=vcol, color=vcol)
        page.insert_text((x0 + 6, y), _ascii(vmsg), fontsize=9, fontname="hebo", color=(1, 1, 1)); nl(16)
        fac_tot = round(sum(r.get("our_fac") or 0 for r in p["rows"]), 2)
        if abs(fac_tot) > TOL:
            left(x0 + 2, _ascii(f"dont reglements de factures (journal d'encaissements dedie, a lettrer au 411) : "
                                f"{_fmt(fac_tot)} EUR"), 8, "helv", _GREY); nl(14)
        # bilan répartition encaissé / créances (cadre sur le CA Total)
        left(x0 + 3, "Répartition encaissé / créances (cadre sur le CA Total)", 9, "hebo", (0.2, 0.2, 0.25)); nl(13)
        B1, B2, B3 = 360, 460, 520
        right(B1, "Encaissé comptoir", 8, "helv", _GREY); right(B2, "+ Créances 411", 8, "helv", _GREY)
        right(B3, "= CA Total", 8, "helv", _GREY); nl(12)
        for lbl, enc, nc in [("Synthèse", p["syn_modes_total"], p["syn_noncaisse"]),
                             ("Notre CSV", p["our_modes_total"], p["our_noncaisse"])]:
            ensure(13); left(x0 + 4, lbl, 9)
            right(B1, _fmt(enc), 8, "cour"); right(B2, _fmt(nc), 8, "cour"); right(B3, _fmt(p["ca_total"]), 8, "cour"); nl(12)
        left(x0 + 2, "Synthèse : créances = CA − total des modes (déduit). CSV : solde des comptes 411 (sommable).",
             8, "helv", _GREY); nl(14)

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
def compute(kind, establishment, date_from, date_to, syn, api, csv=None, *,
            batch_code=None, n_tickets=None, balanced=None, run_id=None, executed_at=None,
            fac_payments=None, fac_detail=None) -> dict:
    """Données du compte rendu (dont `coherent` = verdict global). À rendre via to_text/to_pdf."""
    return _compute(kind, establishment, date_from, date_to, syn, api, csv,
                    batch_code, n_tickets, balanced, run_id, executed_at, fac_payments, fac_detail)


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


def ledger_pdf(company_name, ledger, generated_at=None) -> bytes:
    """Extrait de compte (grand-livre par exercice) en PDF — salariés (421) et clients (411).
    Reprend la vue affichée : exercice choisi, ou vue CUMULÉE si exercice(s) non clôturé(s)."""
    def _ascii(s):
        return (str(s).replace("—", "·").replace("→", "->").replace("€", "EUR")
                .replace("œ", "oe").replace("…", "...").replace("⚠️", "!").replace("✅", "OK"))
    doc = fitz.open()
    state = {"y": 56, "page": doc.new_page()}
    W = state["page"].rect.width
    x0 = 36

    def left(x, s, size=8, font="helv", color=_DARK):
        state["page"].insert_text((x, state["y"]), _ascii(s), fontsize=size, fontname=font, color=color)

    def right(xr, s, size=8, font="cour", color=_DARK):
        s = _ascii(s)
        state["page"].insert_text((xr - fitz.get_text_length(s, fontname=font, fontsize=size), state["y"]),
                                  s, fontsize=size, fontname=font, color=color)

    def ny(d):
        state["y"] += d

    def header_cols():
        state["page"].draw_rect(fitz.Rect(x0, state["y"] - 8, W - 36, state["y"] + 4), fill=_BAR, color=_BAR)
        left(x0 + 2, "Date", 8, "hebo"); left(x0 + 58, "Jour.", 8, "hebo")
        left(x0 + 92, "Libellé", 8, "hebo"); left(x0 + 268, "Nº pièce", 8, "hebo")
        left(x0 + 372, "Lett.", 8, "hebo")
        right(W - 148, "Débit", 8, "hebo"); right(W - 96, "Crédit", 8, "hebo"); right(W - 40, "Solde", 8, "hebo")
        ny(14)

    def ensure(space=16):
        if state["y"] + space > 800:
            state["page"] = doc.new_page()
            state["y"] = 56
            header_cols()

    left(x0, f"Extrait de compte · {ledger.get('name') or ledger.get('number')}", 13, "hebo"); ny(16)
    left(x0, f"{company_name}        compte {ledger.get('number')}        exercice {ledger.get('exercise_label')}"
             + ("  (VUE CUMULÉE)" if ledger.get("combined") else ""), 9, "helv", (0.3, 0.3, 0.3)); ny(12)
    if generated_at:
        left(x0, f"édité le {generated_at} (heure de La Réunion) · Vaelan", 8, "helv", (0.45, 0.45, 0.45)); ny(12)
    if ledger.get("combined"):
        state["page"].draw_rect(fitz.Rect(x0, state["y"] - 8, W - 36, state["y"] + 16), fill=(1, 0.95, 0.85),
                                color=(1, 0.95, 0.85))
        left(x0 + 3, f"Exercice(s) non clôturé(s) : à-nouveaux absents au 1er juillet "
                     f"{', '.join(str(y) for y in ledger.get('missing_an', []))}.", 8, "hebo", (0.55, 0.35, 0.05)); ny(11)
        left(x0 + 3, f"Cette vue CUMULE les écritures depuis l'exercice {ledger.get('span_from')}-"
                     f"{ledger.get('span_from', 0) + 1} pour un solde juste.", 8, "helv", (0.55, 0.35, 0.05)); ny(15)
    ny(4)
    header_cols()

    for l in ledger.get("lines", []):
        ensure()
        left(x0 + 2, str(l.get("date") or ""), 7, "cour")
        left(x0 + 58, str(l.get("journal") or ""), 7, "cour")
        left(x0 + 92, str(l.get("label") or "")[:36], 8)
        left(x0 + 268, str(l.get("piece") or "")[:22], 6.5, "cour", (0.35, 0.35, 0.35))
        if l.get("letter"):
            left(x0 + 372, l["letter"], 8, "hebo", (0.15, 0.35, 0.65))
        if l.get("debit"):
            right(W - 148, f"{l['debit']:.2f}", 7.5)
        if l.get("credit"):
            right(W - 96, f"{l['credit']:.2f}", 7.5)
        right(W - 40, f"{l.get('balance', 0):.2f}", 7.5, "cour",
              (_RED if l.get("balance", 0) > 0.005 else _DARK))
        ny(12)

    ny(4)
    ensure(20)
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 8, W - 36, state["y"] + 5), fill=_BAR, color=_BAR)
    left(x0 + 2, f"TOTAL {ledger.get('number')} (exercice {ledger.get('exercise_label')})", 8, "hebo")
    right(W - 148, f"{ledger.get('total_debit', 0):.2f}", 8, "hebo")
    right(W - 96, f"{ledger.get('total_credit', 0):.2f}", 8, "hebo")
    right(W - 40, f"{ledger.get('solde', 0):.2f}", 8, "hebo")
    return doc.tobytes()


def achats_pdf(title_scope, period_label, counts, pushed, already, errors, missing,
               coherent, recip=None, run_id=None, executed_at=None) -> bytes:
    """Compte rendu PDF de l'étape 7 KK (factures d'achat KOOKABURA -> STERNA)."""
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

    left(x0, "Compte rendu · Factures d'achat KOOKABURA -> STERNA", 14, "hebo"); ny(18)
    left(x0, f"{title_scope}        {period_label}", 9, "helv", (0.3, 0.3, 0.3)); ny(13)
    trace = []
    if run_id is not None:
        trace.append(f"Tâche #{run_id}")
    if executed_at:
        trace.append(f"Vérifié le {executed_at}")
    if trace:
        left(x0, "        ".join(trace), 9, "helv", (0.3, 0.3, 0.3)); ny(13)
    ny(8)

    section("Synthèse")
    for lbl, v in [("Factures KK de la période", f"{counts['total']}  ({counts['ttc']:.2f} EUR TTC)"),
                   ("Poussées (nouvelles)", counts["pushed"]),
                   ("Déjà présentes (idempotence)", counts["already"]),
                   ("En erreur", counts["errors"]),
                   ("Manquantes côté STERNA", counts["missing"])]:
        ensure()
        left(x0 + 4, lbl, 9); right(W - 60, str(v)); ny(14)
    ny(6)

    def _facrows(name, rows, color=_DARK):
        if not rows:
            return
        section(name, color)
        for f in rows:
            ensure(13)
            left(x0 + 4, f"F{f['fnum']:07d}", 9, "cour"); left(x0 + 80, str(f.get("date") or ""), 8, "cour")
            left(x0 + 160, str(f.get("client") or "")[:30], 9)
            if f.get("why"):
                left(x0 + 330, str(f["why"])[:30], 8, "helv", _RED)
            right(W - 60, f"{f['ttc']:.2f}"); ny(13)
        ny(6)

    if recip:
        section("Réciprocité client / fournisseur")
        for lbl, v in [("Factures TopOrder (période)", recip.get("topo")),
                       ("Achats KOOKABURA côté STERNA", recip.get("sterna")),
                       ("Compte client STERNA côté KK (TOKKT)", recip.get("kk411"))]:
            ensure()
            left(x0 + 4, lbl, 9)
            right(W - 60, (f"{v:.2f}" if isinstance(v, float) else "?"),
                  color=(_DARK if isinstance(v, float) else _RED))
            ny(14)
        if recip.get("note"):
            ensure()
            left(x0 + 4, recip["note"], 8, "helv", _RED); ny(13)
        ensure()
        ok = recip.get("ok")
        left(x0 + 4, ("RÉCIPROQUE · les trois montants sont identiques" if ok
                      else "ÉCART · les montants ne se recoupent pas"), 9, "hebo",
             (_GREEN if ok else _RED))
        ny(16)

    _facrows("Poussées vers STERNA (factures à saisir)", pushed, _GREEN)
    _facrows("Déjà présentes", already)
    _facrows("Erreurs", errors, _RED)
    _facrows("Manquantes côté STERNA", missing, _RED)

    ensure(30)
    col = _GREEN if coherent else _RED
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 6), fill=col, color=col)
    left(x0 + 4, ("COMPLET · toutes les factures KK de la période sont côté STERNA (à saisir)."
                  if coherent else "INCOMPLET · voir erreurs / manquantes."), 10, "hebo", (1, 1, 1))
    return doc.tobytes()


def lettrage_pdf(company_name, period_label, counts, full, partial, vir_ok,
                 ambiguous, open_creances, errors, coherent, unmatched_vir=None,
                 overpaid=None, run_id=None, executed_at=None) -> bytes:
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
                   ("Avoirs/trop-perçus partiels (dû au client)", counts.get("overpaid", 0)),
                   ("Virements rapprochés (certains)", counts["vir"]),
                   ("Ambigus (à traiter à la main)", counts["ambiguous"]),
                   ("Virements non rapprochés (info)", counts.get("unmatched", 0)),
                   ("Créances ouvertes (impayées)", counts["open"]),
                   ("Erreurs API", counts["errors"]),
                   ("Comptes clients analysés", counts["accounts"])]:
        ensure()
        left(x0 + 4, lbl, 9); right(W - 60, str(v)); ny(14)
    ny(6)

    if unmatched_vir:
        section("Virements non rapprochés (informatif - pas une erreur)")
        left(x0 + 4, "aucune créance TopOrder du même montant : ancienne facture Kimayo réglée", 8, "helv", _GREY); ny(11)
        left(x0 + 4, "après la bascule, acompte, ou facture pas encore générée - laissés ouverts.", 8, "helv", _GREY); ny(15)
        for v in sorted(unmatched_vir, key=lambda x: (x.get("date") or "")):
            ensure(13)
            left(x0 + 4, str(v.get("date") or ""), 8, "cour"); left(x0 + 90, v["nm"][:30], 9)
            right(W - 60, f"{v['amount']:.2f}"); ny(13)
        ny(6)

    if overpaid:
        section("Avoirs / trop-perçus partiellement lettrés (informatif)")
        left(x0 + 4, "Règlements > créance (avoir remboursé en partie ou trop-perçu) : lettrage partiel posé,", 8, "helv", _GREY); ny(11)
        left(x0 + 4, "le solde reste ouvert au crédit du client (remboursement du reste ou imputation à venir).", 8, "helv", _GREY); ny(14)
        for o in overpaid:
            ensure(13)
            left(x0 + 4, o.get("fref") or f"F{o['fnum']}", 9, "cour")
            left(x0 + 90, str(o.get("nm") or "")[:30], 9)
            left(x0 + 300, f"reçu {o['received']:.2f}", 9, "cour")
            right(W - 60, f"dû au client {o['back']:.2f}", 9, "cour", (0.55, 0.35, 0.05))
            ny(13)
        ny(6)
    if partial:
        section("Lettrages partiels (reste dû)")
        for p in partial:
            ensure(13)
            left(x0 + 4, p.get("fref") or f"F{p['fnum']}", 9, "cour"); left(x0 + 80, p["nm"][:30], 9)
            right(W - 150, f"payé {p['paid']:.2f}"); right(W - 60, f"reste {p['due']:.2f}", color=_RED); ny(13)
        ny(6)
    if vir_ok:
        section("Virements rapprochés", _GREEN)
        for v in vir_ok:
            ensure(13)
            left(x0 + 4, str(v["date"]), 8, "cour"); left(x0 + 90, v.get("fref") or f"F{v['fnum']}", 9, "cour")
            left(x0 + 160, v["nm"][:30], 9); right(W - 60, f"{v['amount']:.2f}"); ny(13)
        ny(6)
    if ambiguous:
        section("À traiter manuellement (ambigus)", _RED)
        for a in ambiguous:
            ensure(13)
            ref = a.get("fref") or (f"F{a['fnum']}" if a.get("fnum") else f"virement {a.get('amount', 0):.2f}")
            left(x0 + 4, a["nm"][:28], 9); left(x0 + 200, ref, 9, "cour")
            left(x0 + 290, str(a["why"])[:34], 8, "helv", _RED); ny(13)
        ny(6)
    if open_creances:
        section("Créances ouvertes (impayées)")
        for c in sorted(open_creances, key=lambda x: -x["age"]):
            ensure(13)
            left(x0 + 4, c.get("fref") or f"F{c['fnum']}", 9, "cour"); left(x0 + 80, c["nm"][:30], 9)
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


def verify_pdf(establishment, journal, period_label, n_entries, used, sections,
               coherent, run_id=None, executed_at=None) -> bytes:
    """Compte rendu PDF de vérification Pennylane : MÊMES contrôles qu'à la génération
    (CA HT/TVA/TTC, TVA par taux, HT par taux, encaissements par mode, détail par compte),
    chaque ligne comparant Attendu (CSV généré) vs Pennylane (lu via l'API) → OK / ÉCART."""
    def _ascii(s):
        return (str(s).replace("—", "·").replace("→", "->").replace("€", "EUR")
                .replace("œ", "oe").replace("…", "..."))
    doc = fitz.open()
    W = doc.new_page().rect.width
    x0 = 40
    state = {"y": 56, "page": doc[0]}
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

    def badge(ok):
        col = _GREEN if ok else _RED
        lbl = "OK" if ok else "ÉCART"
        w = fitz.get_text_length(lbl, fontname="hebo", fontsize=8) + 8
        state["page"].draw_rect(fitz.Rect(BX, state["y"] - 8, BX + w, state["y"] + 2.5), fill=col, color=col)
        state["page"].insert_text((BX + 4, state["y"]), lbl, fontsize=8, fontname="hebo", color=(1, 1, 1))

    left(x0, "Compte rendu · Vérification Pennylane (après import)", 15, "hebo"); nl(18)
    for m in [f"Établissement : {establishment}        Période couverte : {period_label}",
              f"Journaux : {journal}        Écritures Pennylane lues : {n_entries}        Lots : {', '.join(used) or '-'}",
              "Attendu = CSV généré et importé · Pennylane = relu via l'API. Tout doit être identique."]:
        left(x0, m, 9, "helv", (0.3, 0.3, 0.3)); nl(13)
    trace = []
    if run_id is not None:
        trace.append(f"Tâche #{run_id}")
    if executed_at:
        trace.append(f"Vérifié le {executed_at}")
    if trace:
        left(x0, "        ".join(trace), 9, "helv", (0.3, 0.3, 0.3)); nl(13)
    nl(8)

    for sec in sections:
        ensure(36)
        state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, sec["name"], 10, "hebo", (0.15, 0.15, 0.22)); nl(15)
        right(CA, "Attendu (CSV)", 8, "helv", _GREY); right(CP, "Pennylane", 8, "helv", _GREY)
        left(BX, "Contrôle", 8, "helv", _GREY); nl(13)
        for lbl, ev, av, ok in sec["rows"]:
            ensure()
            left(x0 + 4, str(lbl), 9)
            right(CA, _fmt(ev)); right(CP, _fmt(av)); badge(ok); nl(14)
        nl(6)

    ensure(20)
    col = _GREEN if coherent else _RED
    msg = ("COHÉRENT — Pennylane correspond aux lots générés (tous les contrôles OK)"
           if coherent else "ÉCART — Pennylane diffère des lots générés")
    w = fitz.get_text_length(msg, fontname="hebo", fontsize=10) + 12
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, x0 + w, state["y"] + 4), fill=col, color=col)
    state["page"].insert_text((x0 + 6, state["y"]), _ascii(msg), fontsize=10, fontname="hebo", color=(1, 1, 1))
    out = doc.tobytes(); doc.close()
    return out
