"""OWINE — tâches planifiées : synchronisation Shopify (articles, commandes), rappel hebdomadaire des tâches, alertes coûts manquants."""
from datetime import date
from . import service, config


def run_sync(ctx=None) -> str:
    log = ctx.log if ctx else (lambda m: None)
    r1 = service.sync_items(log=log)
    r2 = service.sync_orders(log=log)
    missing = set(r1.get("missing_cost") or [])
    for sku in missing:
        it = service.get_item(sku)
        if it and it.status == "ACTIVE" and it.kind == "wine":
            service.add_task("cost_missing", f"Coût d'achat manquant : {it.title} ({sku})", ref=sku, key=f"cost:{sku}",
                             details="Renseigner le coût dans Shopify (fiche variante) ou dans Vaelan ; il sert à la valeur assurée.")
    # tâches de coût devenues sans objet (coût renseigné, sélection, article retiré) → fermées ; les sélections n'ont pas de coût propre (JS : on regarde les bouteilles)
    for t in service.tasks("open"):
        if t.kind == "cost_missing" and t.ref and (t.ref not in missing or t.ref.upper().startswith("SEL-")):
            service.close_tasks_by_key(f"cost:{t.ref}")
    try:
        n3 = service.pennylane_draft_tasks(log=log)
        n4 = service.close_invoiced_orders(log=log)
        if n4:
            log(f"{n4} commande(s) clôturée(s) (facture validée)")
    except Exception as e:
        log(f"Pennylane : {e}"); n3 = 0
    from . import export
    for fn in (service.pennylane_purchase_tasks, service.lmb_drafts_sync, service.shopify_fulfill_due, export.export_tasks):
        try:
            fn(log=log)
        except Exception as e:
            log(f"{fn.__name__} : {e}")
    return f"articles {r1['items']}, commandes +{r2['created']} / {r2['updated']} mises à jour, {len(r1.get('missing_cost') or [])} coût(s) manquant(s), {n3} brouillon(s) Pennylane"


def run_reprise_livre(ctx=None) -> str:
    """Reprise du 14/09/2026 : livre des mouvements reconstitué (achats Pennylane, BLV, ventes)."""
    from . import reprise
    return reprise.apply_livre(log=ctx.log if ctx else None)


def run_reprise_shopify(ctx=None) -> str:
    """Reprise du 14/09/2026 : corrections Shopify validées (coûts, revalorisation BLV 2, doublon archivé)."""
    from . import reprise
    return reprise.apply_shopify(log=ctx.log if ctx else None)


def weekly_digest(ctx=None) -> str:
    """Le lundi : e-mail récapitulatif des tâches ouvertes et des commandes en cours."""
    from app.core import mailer
    log = ctx.log if ctx else (lambda m: None)
    ts = service.tasks("open")
    pending = [o for o in service.orders() if o.status not in ("cloturee", "annulee")]
    if not ts and not pending:
        return "rien à rappeler"
    lines = [f"Bonjour,\n\nÉtat OWINE au {service.now_local():%d/%m/%Y} :\n"]
    if pending:
        lines.append(f"Commandes en cours ({len(pending)}) :")
        for o in pending:
            lines.append(f"  • {o.name} — {config.ORDER_STATUS.get(o.status, (o.status,))[0]} — {o.customer or ''} ({config.MODES.get(o.mode, o.mode)})")
        lines.append("")
    if ts:
        lines.append(f"Tâches ouvertes ({len(ts)}) :")
        for t in ts:
            due = f" — échéance {t.due_date:%d/%m}" if t.due_date else ""
            late = " (EN RETARD)" if t.due_date and t.due_date < date.today() else ""
            lines.append(f"  • [{config.TASK_KINDS.get(t.kind, t.kind)}] {t.title}{due}{late}")
    lines.append(f"\n{service.admin_url() if hasattr(service, 'admin_url') else 'https://vaelan.com'}/c/OWINE/owine\n\n— Vaelan")
    ok, info = mailer.send(config.DIGEST_TO, f"OWINE — {len(ts)} tâche(s) ouverte(s), {len(pending)} commande(s) en cours", "\n".join(lines))
    log(f"rappel hebdomadaire : {'envoyé' if ok else info}")
    return f"{len(ts)} tâche(s), {len(pending)} commande(s) — e-mail {'envoyé' if ok else 'non envoyé : ' + info}"


def run_shopify_customs(ctx=None) -> str:
    """Écrit dans Shopify les codes SH + origine FR des vins et le poids réel des sélections (page Douane, bouton)."""
    from . import export
    return export.shopify_customs_apply(log=ctx.log if ctx else None)
