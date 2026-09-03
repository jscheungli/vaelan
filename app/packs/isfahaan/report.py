"""Comptes rendus PDF du pack ISFAHAAN."""
import fitz

_DARK = (0.12, 0.16, 0.15)
_GREY = (0.45, 0.45, 0.45)
_GREEN = (0.10, 0.53, 0.33)
_RED = (0.86, 0.21, 0.27)
_BAR = (0.93, 0.94, 0.93)


def _ascii(s):
    return (str(s).replace("—", "·").replace("→", "->").replace("€", "EUR")
            .replace("…", "...").replace("⚠️", "!").replace("✅", "OK"))


def justificatifs_pdf(company_code, period_label, mode, counts, per_journal,
                      unmatched, errors, ok, run_id=None, executed_at=None) -> bytes:
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

    def ensure(space=16):
        if state["y"] + space > 800:
            state["page"] = doc.new_page()
            state["y"] = 56

    def section(name, color=(0.15, 0.15, 0.22)):
        ensure(30)
        state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 4), fill=_BAR, color=_BAR)
        left(x0 + 3, name, 10, "hebo", color)
        ny(16)

    left(x0, "Compte rendu · Justificatifs Inqom -> Pennylane", 14, "hebo"); ny(18)
    left(x0, f"Société : {company_code}        {period_label}        {mode}", 9, "helv", (0.3, 0.3, 0.3)); ny(13)
    trace = []
    if run_id is not None:
        trace.append(f"Tâche #{run_id}")
    if executed_at:
        trace.append(f"Exécuté le {executed_at}")
    if trace:
        left(x0, "        ".join(trace), 9, "helv", (0.3, 0.3, 0.3)); ny(13)
    ny(8)

    section("Synthèse")
    rows = [("Documents Inqom (journaux mappés)", counts["docs"]),
            ("À accrocher", counts["to_attach"]),
            ("Déjà pourvues côté Pennylane", counts["already"]),
            ("Non matchées", counts["unmatched"]),
            ("Conflits", counts["conflicts"]),
            ("Hors journaux mappés", counts["unmapped_docs"])]
    if mode == "ACCROCHAGE":
        rows += [("ACCROCHÉES", counts["attached"]), ("Erreurs", counts["errors"])]
    for lbl, v in rows:
        ensure()
        left(x0 + 4, lbl, 9)
        right(W - 60, str(v))
        ny(14)
    ny(6)

    section("Par journal")
    for code in sorted(per_journal):
        c = per_journal[code]
        ensure()
        left(x0 + 4, code, 9, "cour")
        left(x0 + 80, f"docs {c.get('docs', 0)}", 9)
        left(x0 + 180, f"à accrocher {c.get('a_accrocher', 0)}", 9)
        left(x0 + 310, f"déjà {c.get('deja', 0)}", 9)
        right(W - 60, f"non matché {c.get('non_matche', 0)}", 9)
        ny(14)
    ny(6)

    if unmatched:
        section("Non matchées (aucune écriture Pennylane trouvée)", _RED)
        for d in unmatched[:100]:
            ensure(13)
            left(x0 + 4, f"{d['code']}", 9, "cour")
            left(x0 + 55, d["date"], 8, "cour")
            left(x0 + 130, str(d.get("docref") or "")[:34], 9)
            right(W - 60, f"{d.get('amount', 0):.2f}", 9)
            ny(13)
        if len(unmatched) > 100:
            ensure()
            left(x0 + 4, f"... et {len(unmatched) - 100} autres", 9, "helv", _GREY)
            ny(13)
        ny(6)

    if errors:
        section("Erreurs", _RED)
        for e in errors[:60]:
            ensure(13)
            left(x0 + 4, f"{e['code']} {e['date']} {str(e.get('docref') or '')[:22]}", 8, "cour")
            left(x0 + 260, str(e.get("why") or "")[:48], 8, "helv", _RED)
            ny(13)
        ny(6)

    ensure(30)
    col = _GREEN if ok else _RED
    state["page"].draw_rect(fitz.Rect(x0, state["y"] - 9, W - 40, state["y"] + 6), fill=col, color=col)
    msg = ("Accrochage terminé." if mode == "ACCROCHAGE" else
           "Cadrage à blanc terminé · rien n'a été modifié.") if ok else "Voir conflits / erreurs."
    left(x0 + 4, msg, 10, "hebo", (1, 1, 1))
    return doc.tobytes()
