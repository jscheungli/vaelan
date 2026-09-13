"""CIOP — tâches : relecture Pennylane et production du dossier (ZIP en artefact)."""
from datetime import date
from . import service, build


def run_collect(ctx, company_code: str, fy_end: date) -> str:
    ctx.progress(0, 2, "lecture Pennylane")
    ex = service.collect(company_code, fy_end, log=ctx.log)
    t = ex["totals"]
    ctx.progress(2, 2, "terminé")
    return f"{t['n_lignes']} ligne(s) retenue(s), {t['tableau']:.2f} € HT ; {len(t['sans_site'])} établissement(s) à préciser, {len(t['sans_piece'])} pièce(s) manquante(s)"


def run_build(ctx, company_code: str, fy_end: date) -> str:
    ctx.progress(0, 2, "factures et documents")
    data, t = build.build_zip(company_code, fy_end, log=ctx.log)
    cfg = service.get_config(company_code)
    name = cfg["identity"].get("name") or company_code
    ctx.add_artifact("zip", f"CIOP {name} {service.fy_label(fy_end)}.zip", data, "application/zip")
    ctx.progress(2, 2, "terminé")
    return f"Dossier généré : {t['n_lignes']} investissement(s), {t['tableau']:.2f} € HT, réduction {t['reduction']:.2f} € — cadrage {'OK' if t['ok'] else 'à vérifier'}"
