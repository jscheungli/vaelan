"""ISFAHAAN — justificatifs Inqom → Pennylane (accrochage des pièces aux écritures).

Principe (AUCUNE interprétation) : dans Inqom, chaque écriture porte le document qui lui
est accroché (FileId). On matche l'écriture Inqom avec l'écriture Pennylane issue du FEC :
  1. par PIÈCE : DocRef Inqom == piece_number Pennylane (normalisés) et unique ;
  2. sinon par (DATE, MONTANT total débit) unique.
Puis, en mode « apply », on télécharge le document (URL signée) et on l'accroche à
l'écriture Pennylane (upload_attachment + attach_to_entry — mécanisme de l'étape 5 FDF).

IDEMPOTENT : une écriture Pennylane qui a déjà une pièce (champ attachment du list
endpoint) est sautée. Mode par défaut = CADRAGE À BLANC (aucune écriture, aucun upload).
Les journaux Inqom sans journal Pennylane de même code sont signalés (non traités).
"""
import csv
import io
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from app.core.connectors import inqom, pennylane
from . import config
from . import report as isf_report

_TZ = timedelta(hours=4)


def _norm_piece(s):
    return re.sub(r"\s+", "", str(s or "")).upper()


def _iamt(e):
    """Montant total débit d'une écriture Inqom (somme des Amount positifs)."""
    return round(sum(float(l.get("Amount") or 0) for l in (e.get("Lines") or [])
                     if float(l.get("Amount") or 0) > 0), 2)


def run_inqom_justificatifs(ctx, company_code, date_from="2026-01-01", date_to=None,
                            apply=False):
    ent_id = config.INQOM_ENTERPRISES.get(company_code)
    if not ent_id:
        raise RuntimeError(f"société {company_code} absente de INQOM_ENTERPRISES")
    iq = inqom.for_key(config.INQOM_KEY)
    if not iq:
        raise RuntimeError("clés Inqom absentes (INQOM_ISFAHAAN_*)")
    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError(f"clé Pennylane {company_code} absente")
    date_to = date_to or (datetime.utcnow() + _TZ).date().isoformat()
    mode = "ACCROCHAGE" if apply else "CADRAGE À BLANC"
    label = f"{date_from} → {date_to}"
    ctx.log(f"Justificatifs Inqom → Pennylane · {company_code} (entreprise Inqom {ent_id}) · {label} · {mode}")

    # ---- journaux des deux côtés, mappés par CODE ----
    ijournals = iq.journals(ent_id)
    jmap_pl = pl.journals_map()                       # id -> code
    pl_by_code = {}
    for jid, code in jmap_pl.items():
        pl_by_code.setdefault(code, jid)
    mapped, unmapped = [], []
    for j in ijournals:
        code = j.get("Name")
        if code in pl_by_code:
            mapped.append((j["Id"], code, pl_by_code[code]))
        else:
            unmapped.append(j)

    # ---- écritures Inqom avec document, par journal mappé ----
    ctx.progress(0, None, step="lecture des écritures Inqom…")
    docs = []                                          # écritures Inqom avec FileId, période
    unmapped_docs = 0
    for j in unmapped:
        n = sum(1 for e in iq.search_entries(ent_id, [j["Id"]], with_lines=False)
                if e.get("FileId") and date_from <= str(e.get("Date") or "")[:10] <= date_to)
        unmapped_docs += n
        if n:
            ctx.log(f"⚠️ journal Inqom « {j.get('Name')} » sans équivalent Pennylane : {n} document(s) non traités")
    for ijid, code, pljid in mapped:
        for e in iq.search_entries(ent_id, [ijid]):
            d = str(e.get("Date") or "")[:10]
            if e.get("FileId") and date_from <= d <= date_to:
                docs.append({"code": code, "pl_jid": pljid, "date": d,
                             "piece": _norm_piece(e.get("DocRef")), "docref": e.get("DocRef"),
                             "amount": _iamt(e), "file_id": e["FileId"], "inqom_id": e["Id"]})
    ctx.log(f"Inqom : {len(docs)} écriture(s) avec document sur la période (journaux mappés)")

    # ---- écritures Pennylane par journal utile ----
    ctx.progress(1, None, step="lecture des écritures Pennylane…")
    pl_entries = {}                                    # id -> entry (métadonnées du list endpoint)
    by_piece = defaultdict(list)
    by_date = defaultdict(list)
    for pljid in sorted({d["pl_jid"] for d in docs}):
        for e in pl.ledger_entries(pljid, date_from, date_to):
            pl_entries[e["id"]] = e
            p = _norm_piece(e.get("piece_number"))
            if p:
                by_piece[p].append(e)
            by_date[(pljid, e.get("date"))].append(e)
    ctx.log(f"Pennylane : {len(pl_entries)} écriture(s) sur la période (journaux concernés)")

    # ---- matching ----
    ctx.progress(2, None, step="matching écriture ↔ écriture…")
    amt_cache = {}

    def _plamt(e):
        if e["id"] not in amt_cache:
            amt_cache[e["id"]] = round(sum(float(l.get("debit") or 0)
                                           for l in pl.entry_lines(e["id"])), 2)
            time.sleep(0.05)
        return amt_cache[e["id"]]

    to_attach, already, unmatched, conflicts = [], [], [], []
    claimed = set()
    for d in sorted(docs, key=lambda x: (x["code"], x["date"])):
        target, how = None, None
        cands = by_piece.get(d["piece"], []) if d["piece"] else []
        cands = [c for c in cands if (c.get("journal_id") or c.get("journal")) is not None]
        if len(cands) == 1:
            target, how = cands[0], "pièce"
        elif len(cands) > 1:
            same_day = [c for c in cands if c.get("date") == d["date"]]
            if len(same_day) == 1:
                target, how = same_day[0], "pièce+date"
        if target is None and d["amount"] > 0:
            day = [c for c in by_date.get((d["pl_jid"], d["date"]), []) if c["id"] not in claimed]
            hits = [c for c in day if abs(_plamt(c) - d["amount"]) < 0.01]
            if len(hits) == 1:
                target, how = hits[0], "date+montant"
        if target is None:
            unmatched.append(d)
            continue
        if target["id"] in claimed:
            conflicts.append({**d, "why": f"écriture PL {target['id']} déjà revendiquée par un autre document"})
            continue
        claimed.add(target["id"])
        if target.get("attachment"):
            already.append({**d, "pl_id": target["id"]})
        else:
            to_attach.append({**d, "pl_id": target["id"], "how": how,
                              "pl_piece": target.get("piece_number"), "pl_label": target.get("label")})

    ctx.log(f"matching : {len(to_attach)} à accrocher · {len(already)} déjà pourvues · "
            f"{len(unmatched)} non matchées · {len(conflicts)} conflits · {unmapped_docs} hors journaux mappés")

    # ---- CSV d'audit : builder réutilisable (flush INCRÉMENTAL pendant l'accrochage,
    # pour qu'une interruption — redéploiement, crash — laisse toujours la trace exacte) ----
    csv_name = f"{(datetime.utcnow() + _TZ).strftime('%Y%m%d %H%M')} audit_justificatifs_{company_code} T{ctx.run_id}.csv"
    attached, errors = [], []

    def _audit_csv(pending):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["statut", "journal", "date_ecriture", "piece_inqom", "montant", "matching",
                    "id_ecriture_pennylane", "piece_pennylane", "fichier", "id_fichier_inqom",
                    "id_ecriture_inqom", "motif"])
        def _row(st, d, **kw):
            w.writerow([st, d.get("code"), d.get("date"), d.get("docref"),
                        f"{d.get('amount', 0):.2f}", kw.get("how") or d.get("how") or "",
                        d.get("pl_id") or "", d.get("pl_piece") or "", kw.get("name") or "",
                        d.get("file_id"), d.get("inqom_id"), kw.get("why") or ""])
        for d in attached:
            _row("accrochee", d, name=d.get("name"))
        for d in pending:
            _row("a_accrocher", d)
        for d in already:
            _row("deja_pourvue", d)
        for d in unmatched:
            _row("non_matchee", d, why="aucune écriture Pennylane trouvée (pièce, puis date+montant)")
        for c in conflicts:
            _row("conflit", c, why=c.get("why"))
        for e in errors:
            _row("erreur", e, why=e.get("why"))
        return buf.getvalue().encode("utf-8-sig")

    def _flush(pending):
        try:
            ctx.add_artifact("csv", csv_name, _audit_csv(pending), "text/csv")
        except Exception:
            pass

    # ---- accrochage (mode apply) ----
    if apply and to_attach:
        _flush(to_attach)                     # état initial persisté avant le premier document
        for i, d in enumerate(to_attach):
            ctx.progress(i, len(to_attach), step=f"accrochage {i + 1}/{len(to_attach)} ({d['code']} {d['date']})…")
            try:
                name, data = iq.download_file(ent_id, d["file_id"])
                if not data:
                    errors.append({**d, "why": "téléchargement Inqom vide"})
                    continue
                fid = pl.upload_attachment(name or f"inqom_{d['file_id']}.pdf", data)
                if not fid:
                    errors.append({**d, "why": "upload Pennylane sans id"})
                    continue
                if pl.attach_to_entry(d["pl_id"], fid):
                    attached.append({**d, "name": name})
                else:
                    errors.append({**d, "why": "attach_to_entry a échoué"})
            except Exception as e:
                errors.append({**d, "why": str(e)[:70]})
            if (i + 1) % 25 == 0 or (errors and errors[-1].get("inqom_id") == d.get("inqom_id")):
                _flush(to_attach[i + 1:])     # trace incrémentale (tous les 25 docs + chaque erreur)
            time.sleep(0.15)

    # ---- compte rendu ----
    now = datetime.utcnow() + _TZ
    stamp = now.strftime("%d/%m/%Y %H:%M")
    per_j = defaultdict(lambda: defaultdict(int))
    for d in docs:
        per_j[d["code"]]["docs"] += 1
    for d in to_attach:
        per_j[d["code"]]["a_accrocher"] += 1
    for d in already:
        per_j[d["code"]]["deja"] += 1
    for d in unmatched:
        per_j[d["code"]]["non_matche"] += 1
    counts = {"docs": len(docs), "to_attach": len(to_attach), "already": len(already),
              "unmatched": len(unmatched), "conflicts": len(conflicts),
              "unmapped_docs": unmapped_docs, "attached": len(attached), "errors": len(errors)}
    ok = not errors and not conflicts

    L = [f"JUSTIFICATIFS INQOM → PENNYLANE — {company_code} — {label} — {mode}",
         f"exécuté le {stamp} · tâche #{ctx.run_id} · entreprise Inqom {ent_id}", "",
         "== SYNTHÈSE ==",
         f"  Documents Inqom (journaux mappés)  : {len(docs)}",
         f"  À accrocher                        : {len(to_attach)}",
         f"  Déjà pourvues côté Pennylane       : {len(already)}",
         f"  Non matchées                       : {len(unmatched)}",
         f"  Conflits                           : {len(conflicts)}",
         f"  Hors journaux mappés               : {unmapped_docs}", ""]
    if apply:
        L += [f"  ACCROCHÉES                         : {len(attached)}",
              f"  Erreurs                            : {len(errors)}", ""]
    L += ["== PAR JOURNAL =="]
    for code in sorted(per_j):
        c = per_j[code]
        L.append(f"  {code:<6} docs {c['docs']:>5} · à accrocher {c['a_accrocher']:>5} · "
                 f"déjà {c['deja']:>5} · non matché {c['non_matche']:>4}")
    L.append("")
    if unmatched:
        L += ["== NON MATCHÉES (aucune écriture Pennylane trouvée) =="]
        for d in unmatched[:80]:
            L.append(f"  {d['code']:<5} {d['date']}  DocRef {str(d['docref'])[:28]:<30} {d['amount']:>10.2f}")
        if len(unmatched) > 80:
            L.append(f"  … et {len(unmatched) - 80} autres")
        L.append("")
    if conflicts:
        L += ["== CONFLITS =="] + [f"  {c['code']} {c['date']} {c['docref']} — {c['why']}" for c in conflicts] + [""]
    if apply and errors:
        L += ["== ERREURS =="] + [f"  {e['code']} {e['date']} {str(e['docref'])[:24]} — {e['why']}" for e in errors[:60]] + [""]
    L.append("✅ " + ("Accrochage terminé." if apply else "Cadrage à blanc terminé — rien n'a été modifié.")
             if ok else "⚠️ Voir conflits/erreurs ci-dessus.")

    # ---- CSV d'audit final (mode à blanc : les « à accrocher » ; mode apply : tout est traité) ----
    _flush(to_attach if not apply else [])
    L.append("")
    L.append("Traçabilité : CSV d'audit joint à la tâche (une ligne par document, statut + ids Inqom/Pennylane).")
    ctx.set_report("\n".join(L))

    ctx.add_artifact("report", f"{now.strftime('%Y%m%d %H%M')} justificatifs_inqom_{company_code} T{ctx.run_id}.pdf",
                     isf_report.justificatifs_pdf(company_code, label, mode, counts, dict(per_j),
                                                  unmatched, errors, ok,
                                                  run_id=ctx.run_id, executed_at=stamp),
                     "application/pdf")

    head = (f"{len(attached)} accrochée(s), {len(errors)} erreur(s)" if apply
            else f"{len(to_attach)} à accrocher, {len(already)} déjà pourvues, {len(unmatched)} non matchées")
    return f"{'✅' if ok else '⚠️'} Justificatifs Inqom {company_code} ({mode.lower()}) — {head} — {stamp}"
