"""Parseur du journal de synthèse TopOrder (PDF).

Extrait les totaux qui servent de VERROU de cadrage : CA Total, modes de
paiement, période, établissement. L'import n'est autorisé que si le CA calculé
depuis les tickets == CA Total de la synthèse.
"""
import re
import fitz  # pymupdf


def _num(s: str) -> float:
    s = s.replace(" ", "").replace("\xa0", "").replace(" ", "")
    s = s.replace("€", "").replace(",", ".").strip()
    return float(s)


def parse(pdf_bytes: bytes) -> dict:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    lines = [l.strip() for p in doc for l in p.get_text().splitlines() if l.strip()]
    text = "\n".join(lines)

    m = re.search(r"Du (\d{2})/(\d{2})/(\d{4}) au (\d{2})/(\d{2})/(\d{4})", text)
    period = None
    if m:
        period = {"date_from": f"{m[3]}-{m[2]}-{m[1]}", "date_to": f"{m[6]}-{m[5]}-{m[4]}"}

    me = re.search(r"Journal de synth[èe]se\s*[—–-]\s*(.+)", text)
    establishment = me.group(1).strip() if me else None

    def value_after(label: str):
        """1ʳᵉ ligne contenant un nombre après une ligne == label."""
        for i, l in enumerate(lines):
            if l == label:
                for j in range(i + 1, min(i + 4, len(lines))):
                    if re.search(r"\d", lines[j]):
                        try:
                            return _num(lines[j])
                        except ValueError:
                            pass
        return None

    pay_detail = _paytable(lines)
    payments = {m: d["total"] for m, d in pay_detail.items()}
    if not payments:                               # repli : ancienne méthode
        for mode in ("CB", "Espèce", "Ticket restaurant", "Titre restaurant", "Chèque"):
            v = value_after(mode)
            if v is not None:
                payments[mode] = v

    families, ca_ht = _families(lines)

    return {
        "establishment": establishment,
        "period": period,
        "ca_total": value_after("CA Total"),      # CA TTC
        "ca_ht": ca_ht,                            # somme du CA HT des familles
        "families": families,                      # [{name, ht, ttc}]
        "total_paiements": value_after("TOTAL"),  # total net encaissé (modes de paiement)
        "payments": payments,                      # {mode: total net = encaiss. − remb.}
        "payments_detail": pay_detail,             # {mode: {total, encaissements, remboursements}}
        "entree_caisse": value_after("Entrée de caisse"),
        "sortie_caisse": value_after("Sortie de caisse"),
        "nb_clients": value_after("Nombre de"),
    }


_MODES = ("CB", "Espèce", "Especes", "Espèces", "Ticket restaurant", "Titre restaurant",
          "Chèque", "Cheque", "Carte cadeau", "Avoir", "Crédit", "Compte client", "Virement")


def _paytable(lines):
    """Table « Mode paiement » : par mode, [Total€, Qté(entier), Encaiss€, Remb€].
    Renvoie {mode: {total, encaissements, remboursements}} (ligne « TOTAL » exclue)."""
    try:
        i = lines.index("Mode paiement")
    except ValueError:
        return {}
    euro = re.compile(r"^-?[\d\s.,\xa0]+\s*€$")
    out, name, vals = {}, None, []

    def flush():
        if name and vals:
            out[name] = {"total": vals[0],
                         "encaissements": vals[1] if len(vals) >= 2 else None,
                         "remboursements": vals[2] if len(vals) >= 3 else None}

    for l in lines[i + 1:]:
        if l in ("Total", "Qté", "Encaissements", "Remboursements"):
            continue
        if l in ("TOTAL", "Informations", "Familles"):
            break
        if euro.match(l):
            try:
                vals.append(_num(l))
            except ValueError:
                pass
        elif re.fullmatch(r"\d+", l):              # Qté (entier sans €) -> ignorée
            continue
        elif not re.search(r"\d", l):              # ligne texte = nom de mode
            flush()
            name, vals = (l if l in _MODES else None), []
    flush()
    return out


def _families(lines):
    """Table « Familles » : chaque famille = nom, CA HT (€), CA TTC (€), %.
    Renvoie ([{name, ht, ttc}], total_ht) ; (None, None) si table absente."""
    try:
        i = lines.index("Familles")
    except ValueError:
        return None, None
    euro = re.compile(r"^([\d\s.,\xa0]+)\s*€$")
    fams, pending, name = [], [], None
    for l in lines[i + 1:]:
        if l in ("Famille", "CA HT", "CA TTC", "%"):
            continue
        if l.endswith("%"):           # fin d'une ligne famille (nom, HT, TTC, %)
            if name and len(pending) >= 2:
                fams.append({"name": name, "ht": pending[0], "ttc": pending[1]})
            name, pending = None, []
            continue
        m = euro.match(l)
        if m:
            try:
                pending.append(_num(m.group(1)))
            except ValueError:
                pass
        elif not re.search(r"\d", l):   # ligne texte = nom de famille
            name = l
    if not fams:
        return None, None
    return fams, round(sum(f["ht"] for f in fams), 2)
