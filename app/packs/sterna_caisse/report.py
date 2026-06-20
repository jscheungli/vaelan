"""Compte rendu détaillé téléchargeable d'une tâche (cadrage / génération TOSLT).

Compare, source par source, les montants : journal de SYNTHÈSE (PDF officiel),
pull API (tickets), et CSV AGRÉGÉ (relecture du fichier généré). On vérifie le
CA HT total, le CA TTC, la TVA, le HT/TVA par taux, et les paiements par mode.
La synthèse ne ventile pas par taux de TVA (elle ventile par famille de produit),
d'où « n/a » sur ces lignes côté synthèse — on y vérifie API vs CSV.
"""
from collections import defaultdict
from . import config

TOL = 0.05


def _rate_from_label(taux: str) -> str:
    """« 2,1% » -> « 2.1 » ; « 0% » -> « 0 »."""
    return taux.replace("%", "").replace(",", ".").strip()


def aggregate_rows(rows) -> dict:
    """Relit les lignes du CSV généré et reconstitue les totaux par poste."""
    ca_acc = config.CA_ANONYME
    rev_tva = {v: k for k, v in config.TVA_ACCOUNT.items()}   # 44571005 -> 2.1
    ht_by_rate, tva_by_rate, pay = defaultdict(float), defaultdict(float), defaultdict(float)
    deb = cred = 0.0
    # comptes d'encaissement -> libellé de mode (pour comparer à la synthèse)
    pay_lbl = {}
    for pfx_est in config.ESTABLISHMENTS.values():
        a = config.caisse_accounts(pfx_est["pfx"])
        pay_lbl[a["cb"]] = "CB"
        pay_lbl[a["especes"]] = "Espèce"
        pay_lbl[a["ticket_resto"]] = "Ticket restaurant"
        pay_lbl[a["autres"]] = "Autres"
    for r in rows:
        acc, taux, d, c = r[2], r[5], float(r[7] or 0), float(r[8] or 0)
        deb += d
        cred += c
        if acc == ca_acc:                          # CA HT (70101) par taux
            ht_by_rate[_rate_from_label(taux)] += c
        elif acc in rev_tva:                       # TVA collectée par taux
            tva_by_rate[rev_tva[acc]] += c
        elif acc in pay_lbl:                       # encaissement net par mode
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


def _eur(v):
    return "" if v is None else f"{v:>14,.2f}".replace(",", " ")


def _flag(*vals):
    vals = [v for v in vals if v is not None]
    return "OK" if len(vals) >= 2 and max(vals) - min(vals) < TOL else "⚠️ ÉCART"


def _row3(label, syn, api, csv):
    f = _flag(syn, api, csv)
    return f"  {label:<22}{_eur(syn)}{_eur(api)}{_eur(csv)}    {f}"


def build(kind, establishment, date_from, date_to, syn, api, csv=None, *,
          batch_code=None, n_tickets=None, balanced=None) -> str:
    """Construit le compte rendu texte. `api` et `csv` sont des dicts agrégés
    (ca_ht, ca_ttc, tva, ht_by_rate, tva_by_rate, payments). `csv` peut être None
    (cadrage seul)."""
    L = []
    title = "GÉNÉRATION TOSLT (cadrage + CSV)" if kind == "generate" else "CADRAGE CAISSE"
    L.append(f"COMPTE RENDU — {title}")
    L.append(f"Établissement : {establishment}    Période : {date_from} → {date_to}")
    meta = []
    if batch_code:
        meta.append(f"Lot : {batch_code}")
    if n_tickets is not None:
        meta.append(f"Tickets : {n_tickets}")
    if meta:
        L.append("    ".join(meta))
    L.append("")
    head = f"  {'':<22}{'Synthèse':>14}{'API tickets':>14}{'CSV agrégé':>14}    Match"
    if csv is None:
        head = f"  {'':<22}{'Synthèse':>14}{'API tickets':>14}    Match"

    def line(label, sv, av, cv):
        if csv is None:
            return f"  {label:<22}{_eur(sv)}{_eur(av)}    {_flag(sv, av)}"
        return _row3(label, sv, av, cv)

    # ----- chiffre d'affaires global -----
    L.append("== CHIFFRE D'AFFAIRES ==")
    L.append(head)
    syn_ttc = syn.get("ca_total")
    syn_ht = syn.get("ca_ht")
    syn_tva = round(syn_ttc - syn_ht, 2) if (syn_ttc is not None and syn_ht is not None) else None
    L.append(line("CA HT total", syn_ht, api.get("ca_ht"), (csv or {}).get("ca_ht")))
    L.append(line("TVA total", syn_tva, api.get("tva"), (csv or {}).get("tva")))
    L.append(line("CA TTC total", syn_ttc, api.get("ca_ttc"), (csv or {}).get("ca_ttc")))
    L.append("")

    # ----- HT par taux (synthèse n/a : elle ventile par famille) -----
    L.append("== HT PAR TAUX DE TVA ==   (synthèse : n/a, ventile par famille)")
    L.append(head)
    rates = sorted(set(list(api.get("ht_by_rate", {})) + list((csv or {}).get("ht_by_rate", {}))),
                   key=lambda x: float(x))
    for rt in rates:
        L.append(line(f"HT {rt}%", None, api.get("ht_by_rate", {}).get(rt),
                      (csv or {}).get("ht_by_rate", {}).get(rt)))
    L.append("")

    L.append("== TVA PAR TAUX ==   (synthèse : n/a)")
    L.append(head)
    rates = sorted(set(list(api.get("tva_by_rate", {})) + list((csv or {}).get("tva_by_rate", {}))),
                   key=lambda x: float(x))
    for rt in rates:
        av = api.get("tva_by_rate", {}).get(rt)
        cv = (csv or {}).get("tva_by_rate", {}).get(rt)
        if abs(av or 0) < TOL and abs(cv or 0) < TOL:
            continue                                   # taux sans TVA (ex. 0%)
        L.append(line(f"TVA {rt}%", None, av, cv))
    L.append("")

    # ----- paiements par mode (CB/Espèce/TR comparés ; le reste regroupé en « divers ») -----
    L.append("== PAIEMENTS PAR MODE (net encaissé) ==")
    L.append(head)
    modes = ["CB", "Espèce", "Ticket restaurant"]

    def _others(d):
        s = round(sum(v for k, v in (d or {}).items() if k not in modes), 2)
        return s if abs(s) >= TOL else None

    for m in modes:
        L.append(line(m, syn.get("payments", {}).get(m),
                      api.get("payments", {}).get(m), (csv or {}).get("payments", {}).get(m)))
    sv, av, cv = _others(syn.get("payments")), _others(api.get("payments")), _others((csv or {}).get("payments"))
    if sv is not None or av is not None or cv is not None:
        L.append(line("Autres / divers", sv, av, cv))
    L.append("")

    if csv is not None and balanced is not None:
        bal = "OK" if balanced else f"⚠️ DÉSÉQUILIBRE"
        L.append(f"== ÉQUILIBRE CSV ==   débit {_eur(csv.get('debit')).strip()} = "
                 f"crédit {_eur(csv.get('credit')).strip()}   {bal}")
        L.append("")

    L.append("Tolérance de rapprochement : ± 0,05 €.")
    return "\n".join(L)
