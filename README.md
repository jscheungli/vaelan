# Vaelan

Plateforme de **contrôles comptables sur Pennylane**, multi-sociétés.

Le socle fournit l'authentification (multi-utilisateurs, droits par société),
les connecteurs Pennylane/TopOrder, le journal des runs et la coquille de
dashboard. Le métier vit dans des **packs de contrôle** `(société, domaine)`
enregistrés dans le registre — ex. `sterna.caisse`, `kookabura.caisse`,
demain `*.paie`. Chaque pack déclare ses tuiles de dashboard, ses contrôles
(anomalies) et ses actions (import, lettrage, remédiation…).

## Stack
FastAPI · SQLModel (Postgres en prod, SQLite en local) · Jinja2 + Bootstrap 5 + HTMX · déployé sur Render.

## Dev local
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # ajuster ADMIN_PASSWORD, clés API…
uvicorn main:app --reload
```
→ http://localhost:8000 (login = ADMIN_EMAIL / ADMIN_PASSWORD du `.env`).

## Structure
```
app/core/      config, db, auth, registre des packs, connecteurs
app/packs/     packs de contrôle (1 par société×domaine)
app/web/       routes + templates (Bootstrap+HTMX)
app/models.py  socle DB (companies, users, runs, daily_states, imports)
```

## Phasage
- **0** — socle qui tourne (auth, dashboard shell, registre, déploiement). ← *ici*
- **1** — import gated par synthèse + cadrage date par date + génération CSV.
- **2** — dashboard d'anomalies cliquables (justificatifs, pro non facturées, B2C non soldées, non-lettrage).
- **3** — journaux paiements/banque + lettrage assisté.
- **4** — alertes email (Postmark).

Secrets : variables d'environnement Render uniquement, jamais dans le dépôt.
