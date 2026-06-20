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

    payments = {}
    for mode in ("CB", "Espèce", "Ticket restaurant", "Titre restaurant", "Chèque"):
        v = value_after(mode)
        if v is not None:
            payments[mode] = v

    return {
        "establishment": establishment,
        "period": period,
        "ca_total": value_after("CA Total"),
        "total_paiements": value_after("TOTAL"),  # total net encaissé (modes de paiement)
        "payments": payments,
        "nb_clients": value_after("Nombre de"),
    }
