"""Jobs du pack Sterna — Caisse.

run_cadrage : pull tickets -> calcul CA -> parse journal de synthèse ->
comparaison (CA Total + modes de paiement + période). C'est le VERROU :
tant que ça ne cadre pas, on ne génère pas l'import.
"""
from . import engine, synthese


def run_cadrage(ctx, establishment, date_from, date_to, synthese_bytes):
    ctx.log(f"Établissement : {establishment} | période {date_from} → {date_to}")
    ctx.progress(0, None, step="calcul du CA depuis les tickets…")

    ca = engine.compute_ca(
        establishment, date_from, date_to,
        on_progress=lambda n, step: ctx.progress(n, None, step),
    )
    ctx.log(f"CA tickets : {ca['ca_ttc']:.2f} € TTC ({ca['n_tickets']} tickets) "
            f"| HT {ca['ca_ht']:.2f} | TVA {ca['tva']:.2f} | créances {ca['creances_total']:.2f}")

    ctx.progress(ca["n_tickets"], ca["n_tickets"], step="lecture du journal de synthèse…")
    syn = synthese.parse(synthese_bytes)
    if syn.get("ca_total") is None:
        ctx.log("⚠️ impossible de lire le CA Total dans la synthèse")
        return "Synthèse illisible (CA Total introuvable)"
    ctx.log(f"Synthèse : CA Total {syn['ca_total']:.2f} € | période {syn.get('period')}")

    issues = []
    p = syn.get("period")
    if p and (p["date_from"] != date_from or p["date_to"] != date_to):
        issues.append(f"période synthèse {p['date_from']}→{p['date_to']} ≠ demandée")

    diff = round((ca["ca_ttc"] or 0) - syn["ca_total"], 2)
    ctx.log(f"Cadrage CA : tickets {ca['ca_ttc']:.2f} vs synthèse {syn['ca_total']:.2f} → écart {diff:+.2f}")
    if abs(diff) >= 0.05:
        issues.append(f"écart CA {diff:+.2f} €")

    for mode, synv in (syn.get("payments") or {}).items():
        ourv = ca["payments"].get(mode, 0.0)
        d = round(ourv - synv, 2)
        flag = "" if abs(d) < 0.05 else "  ⚠️"
        ctx.log(f"  {mode} : tickets {ourv:.2f} vs synthèse {synv:.2f} → {d:+.2f}{flag}")
        if abs(d) >= 0.05:
            issues.append(f"écart {mode} {d:+.2f} €")

    if not issues:
        ctx.log("✅ CADRAGE PARFAIT — l'import pourra être généré.")
        return f"Cadré ✓ — CA {ca['ca_ttc']:.2f} € = synthèse ({ca['n_tickets']} tickets)"
    ctx.log("❌ ÉCARTS détectés : " + " ; ".join(issues))
    return "Écart : " + " ; ".join(issues[:3])
