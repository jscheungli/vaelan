"""ISFAHAAN — correspondance clients Odoo ↔ Pennylane (diagnostic doublons + matching).

Problème adressé : doublons de clients et mauvais rapprochements entre Odoo et le
Pennylane de la société. Vaelan LIT les deux côtés, calcule une clé de matching
(SIREN, extrait du SIRET Odoo, du n° de TVA FR ou du reg_no Pennylane), détecte :

  - doublon_odoo       : plusieurs fiches Odoo partagent le même SIREN (ou le même nom normalisé)
  - doublon_pennylane  : un client Odoo correspond à PLUSIEURS clients Pennylane
  - conflit_nom        : SIREN identique mais noms sans rapport (probable mauvais matching)
  - sans_cle           : ni SIRET ni TVA côté Odoo -> matching par nom seulement (fragile)
  - absent_pennylane   : client Odoo introuvable côté Pennylane
  - absent_odoo        : client Pennylane sans fiche Odoo (orphelin)
  - ok                 : 1 ↔ 1, cohérent

Vaelan ne corrige RIEN : la fusion des doublons / saisie des SIREN se fait dans
Odoo / Pennylane, puis on resynchronise (mêmes principes que la synchro TopOrder).
"""
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from sqlmodel import Session, select, delete

from app.core.db import engine
from app.core.connectors import odoo, pennylane
from app.models import Company, OdooClientMatch

_TZ = timedelta(hours=4)

# formes juridiques / mots creux ignorés dans le nom normalisé
_LEGAL = {"SARL", "SAS", "SASU", "EURL", "SA", "SCI", "SNC", "SELARL", "SELAS", "GIE",
          "STE", "SOCIETE", "ETS", "ETABLISSEMENT", "ETABLISSEMENTS", "GROUPE", "CIE", "COMPAGNIE"}


def _norm_name(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    toks = [t for t in re.split(r"[^A-Z0-9]+", s) if t and t not in _LEGAL]
    return " ".join(toks)


def _siren(siret=None, vat=None, reg_no=None):
    """SIREN (9 chiffres) depuis un SIRET, un n° de TVA FR ou un reg_no Pennylane."""
    d = re.sub(r"\D", "", str(siret or ""))
    if len(d) >= 9:
        return d[:9]
    v = re.sub(r"\s", "", str(vat or "")).upper()
    m = re.match(r"^FR[0-9A-Z]{2}(\d{9})$", v)
    if m:
        return m.group(1)
    d = re.sub(r"\D", "", str(reg_no or ""))
    if len(d) in (9, 14):
        return d[:9]
    return None


def _pull_odoo_partners(oc, ctx):
    """Fiches clients Odoo de tête (pas les contacts enfants), actives."""
    fields = ["name", "vat", "ref", "email", "is_company", "customer_rank"]
    # sources d'identifiant : company_registry (SIREN/SIRET standard Odoo) et/ou siret (l10n_fr)
    for opt in ("company_registry", "siret"):
        if oc.has_field("res.partner", opt):
            fields.append(opt)
    has_siret = "siret" in fields or "company_registry" in fields
    if not has_siret:
        ctx.log("ni « company_registry » ni « siret » sur res.partner — clé = n° de TVA seulement")
    return oc.search_read("res.partner",
                          [("parent_id", "=", False), ("customer_rank", ">", 0)],
                          fields), has_siret


def _pull_pennylane_customers(pl):
    out, cur = [], None
    while True:
        params = {"limit": 100}
        if cur:
            params["cursor"] = cur
        d = pl.get("/customers", **params)
        out += (d.get("items") or [])
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    return out


def run_odoo_clients_sync(ctx, company_code):
    """Synchronise la table de correspondance clients Odoo ↔ Pennylane."""
    with Session(engine) as s:
        company = s.exec(select(Company).where(Company.code == company_code)).first()
        if not company:
            raise RuntimeError(f"société {company_code} introuvable")
    oc = odoo.for_company(company_code)
    if not oc:
        raise RuntimeError(f"clés Odoo absentes (ODOO_{odoo._env_key(company_code)}_URL/DB/LOGIN/APIKEY)")
    pl = pennylane.for_company(company_code)
    if not pl:
        raise RuntimeError(f"clé Pennylane absente (PENNYLANE_{odoo._env_key(company_code)}_TOKEN)")

    ctx.progress(0, 4, step="lecture Odoo (res.partner)…")
    partners, has_siret = _pull_odoo_partners(oc, ctx)
    ctx.log(f"Odoo : {len(partners)} fiche(s) client")
    ctx.progress(1, 4, step="lecture Pennylane (customers)…")
    customers = _pull_pennylane_customers(pl)
    ctx.log(f"Pennylane : {len(customers)} client(s)")

    # ---- clés ----
    for p in partners:
        p["_siren"] = _siren(siret=p.get("siret") or p.get("company_registry"), vat=p.get("vat"))
        p["_nname"] = _norm_name(p.get("name"))
    for c in customers:
        c["_siren"] = _siren(reg_no=c.get("reg_no"))
        c["_nname"] = _norm_name(c.get("name"))

    pl_by_siren = defaultdict(list)
    pl_by_name = defaultdict(list)
    for c in customers:
        if c["_siren"]:
            pl_by_siren[c["_siren"]].append(c)
        if c["_nname"]:
            pl_by_name[c["_nname"]].append(c)
    od_by_siren = defaultdict(list)
    od_by_name = defaultdict(list)
    for p in partners:
        if p["_siren"]:
            od_by_siren[p["_siren"]].append(p)
        if p["_nname"]:
            od_by_name[p["_nname"]].append(p)

    ctx.progress(2, 4, step="matching + détection des doublons…")
    now = datetime.utcnow()
    rows, counts = [], defaultdict(int)
    matched_pl_ids = set()
    for p in sorted(partners, key=lambda x: (x.get("name") or "").upper()):
        sr, nn = p["_siren"], p["_nname"]
        row = OdooClientMatch(company_id=company.id, odoo_id=p["id"], odoo_name=p.get("name"),
                              odoo_ref=(p.get("ref") or None), odoo_vat=(p.get("vat") or None),
                              odoo_siret=(p.get("siret") or p.get("company_registry") or None) if has_siret else None,
                              siren=sr, last_synced=now)
        # candidats Pennylane : par SIREN d'abord, sinon par nom normalisé
        cands = pl_by_siren.get(sr, []) if sr else []
        via = "siren"
        if not cands and nn:
            cands = pl_by_name.get(nn, [])
            via = "nom"
        if cands:
            c = cands[0]
            row.pennylane_customer_id = c.get("id")
            row.pennylane_name = c.get("name")
            row.pennylane_reg_no = c.get("reg_no")
            for x in cands:
                matched_pl_ids.add(x.get("id"))
        # statut (ordre de gravité)
        if sr and len(od_by_siren.get(sr, [])) > 1:
            row.status, row.dup_group = "doublon_odoo", f"SIREN:{sr}"
            row.note = f"{len(od_by_siren[sr])} fiches Odoo partagent ce SIREN — fusionner dans Odoo"
        elif nn and len(od_by_name.get(nn, [])) > 1 and not sr:
            row.status, row.dup_group = "doublon_odoo", f"NOM:{nn[:40]}"
            row.note = f"{len(od_by_name[nn])} fiches Odoo au même nom (sans SIREN) — fusionner/renseigner le SIREN"
        elif len(cands) > 1:
            row.status, row.dup_group = "doublon_pennylane", f"SIREN:{sr}" if sr else f"NOM:{nn[:40]}"
            row.note = f"{len(cands)} clients Pennylane pour cette clé — fusionner côté Pennylane"
        elif not cands:
            if not sr:
                row.status = "sans_cle"
                row.note = "ni SIRET ni TVA côté Odoo, et aucun client Pennylane au même nom"
            else:
                row.status = "absent_pennylane"
                row.note = "aucun client Pennylane avec ce SIREN (reg_no)"
        else:
            c = cands[0]
            if via == "siren" and nn and c["_nname"] and not (set(nn.split()) & set(c["_nname"].split())):
                row.status = "conflit_nom"
                row.note = f"même SIREN mais noms sans rapport : « {p.get('name')} » vs « {c.get('name')} »"
            elif via == "nom":
                row.status, row.note = "ok", "rapproché par NOM (pas de SIREN des deux côtés — fragile)"
            elif not sr:
                row.status, row.note = "ok", "rapproché par nom"
            else:
                row.status = "ok"
        counts[row.status] += 1
        rows.append(row)

    # orphelins Pennylane (aucune fiche Odoo ne pointe dessus)
    for c in sorted(customers, key=lambda x: (x.get("name") or "").upper()):
        if c.get("id") in matched_pl_ids:
            continue
        rows.append(OdooClientMatch(company_id=company.id, odoo_id=None,
                                    pennylane_customer_id=c.get("id"), pennylane_name=c.get("name"),
                                    pennylane_reg_no=c.get("reg_no"), siren=c["_siren"],
                                    status="absent_odoo", last_synced=now,
                                    note="client Pennylane sans fiche Odoo correspondante"))
        counts["absent_odoo"] += 1

    ctx.progress(3, 4, step="écriture de la table de correspondance…")
    with Session(engine) as s:
        s.exec(delete(OdooClientMatch).where(OdooClientMatch.company_id == company.id))
        for r in rows:
            s.add(r)
        s.commit()

    # ---- compte rendu ----
    stamp = (datetime.utcnow() + _TZ).strftime("%d/%m/%Y %H:%M")
    dups = defaultdict(list)
    for r in rows:
        if r.dup_group:
            dups[r.dup_group].append(r)
    L = [f"CLIENTS ODOO ↔ PENNYLANE — {company.name}",
         f"synchronisé le {stamp} · tâche #{ctx.run_id}", "",
         f"  Fiches clients Odoo   : {len(partners)}",
         f"  Clients Pennylane     : {len(customers)}", "",
         "== STATUTS =="]
    for st in ("ok", "conflit_nom", "doublon_odoo", "doublon_pennylane", "sans_cle",
               "absent_pennylane", "absent_odoo"):
        if counts.get(st):
            L.append(f"  {st:<18}: {counts[st]}")
    if dups:
        L += ["", "== GROUPES DE DOUBLONS =="]
        for g, rs in sorted(dups.items()):
            names = ", ".join((r.odoo_name or r.pennylane_name or "?") for r in rs[:6])
            L.append(f"  {g}  ->  {names}")
    ctx.set_report("\n".join(L))

    pb = sum(counts[k] for k in counts if k not in ("ok", "absent_odoo"))
    return (f"{'✅' if pb == 0 else '⚠️'} Clients Odoo↔Pennylane — {counts.get('ok', 0)} ok, "
            f"{pb} à corriger, {counts.get('absent_odoo', 0)} orphelin(s) Pennylane — {stamp}")
