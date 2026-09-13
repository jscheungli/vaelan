"""OWINE — tâches planifiées : synchronisation Shopify (articles, commandes), rappel hebdomadaire des tâches, alertes coûts manquants."""
from datetime import date
from . import service, config


def run_sync(ctx=None) -> str:
    log = ctx.log if ctx else (lambda m: None)
    r1 = service.sync_items(log=log)
    r2 = service.sync_orders(log=log)
    for sku in r1.get("missing_cost") or []:
        it = service.get_item(sku)
        if it and it.status == "ACTIVE":
            service.add_task("cost_missing", f"Coût d'achat manquant : {it.title} ({sku})", ref=sku, key=f"cost:{sku}",
                             details="Renseigner le coût dans Shopify (fiche variante) ou dans Vaelan ; il sert à la valeur assurée.")
    try:
        n3 = service.pennylane_draft_tasks(log=log)
    except Exception as e:
        log(f"Pennylane : {e}"); n3 = 0
    return f"articles {r1['items']}, commandes +{r2['created']} / {r2['updated']} mises à jour, {len(r1.get('missing_cost') or [])} coût(s) manquant(s), {n3} brouillon(s) Pennylane"


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
