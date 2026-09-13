"""CIOP — collecte Pennylane (comptes CIOP, journaux d'achats), pièces, établissement, libellés, cadrage.
Données d'un exercice : Setting `ciop:ex:<AAAA-MM-JJ de clôture>` = {"lines": [...], "collected_at", "totals"}.
Chaque ligne garde les valeurs lues (Pennylane, facture) et les choix de l'utilisateur (label, site, start, include, note)."""
import io
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
from sqlmodel import Session, select

from app.core.db import engine
from app.models import Setting, CiopForm
from app.core.connectors.pennylane import for_company
from . import config


# ----------------------------------------------------------------- config
def _deep(base: dict, upd: dict) -> dict:
    for k, v in (upd or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep(base[k], v)
        else:
            base[k] = v
    return base


def _setting(company_code: str, key: str) -> Optional[str]:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == company_code, Setting.key == key)).first()
        return st.value if st else None


def _save_setting(company_code: str, key: str, value: str) -> None:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == company_code, Setting.key == key)).first()
        if not st:
            st = Setting(company_code=company_code, key=key, value=value)
        else:
            st.value = value
        s.add(st)
        s.commit()


def get_config(company_code: str) -> dict:
    cfg = json.loads(json.dumps({"params": config.PARAMS, **config.COMPANIES.get(company_code, {"identity": {}, "partners": [], "sites": [], "declarant": {}})}))
    raw = _setting(company_code, "ciop:config")
    if raw:
        try:
            _deep(cfg, json.loads(raw))
        except Exception:
            pass
    return cfg


def save_config(company_code: str, values: dict) -> dict:
    cfg = get_config(company_code)
    _deep(cfg, values)
    for k in ("partners", "sites"):
        if k in values:
            cfg[k] = values[k]
    _save_setting(company_code, "ciop:config", json.dumps(cfg, ensure_ascii=False))
    return cfg


# ----------------------------------------------------------------- exercices
def fy_bounds(cfg: dict, fy_end: date) -> Tuple[date, date]:
    return fy_end.replace(year=fy_end.year - 1) + timedelta(days=1), fy_end


def fy_ends(cfg: dict, today: date = None, n: int = 3) -> List[date]:
    """Clôtures proposées : la dernière passée et les précédentes (n)."""
    today = today or date.today()
    p = cfg["params"]
    end = date(today.year, int(p["fy_end_month"]), int(p["fy_end_day"]))
    if end > today:
        end = end.replace(year=end.year - 1)
    return [end.replace(year=end.year - i) for i in range(n)]


def fy_label(fy_end: date) -> str:
    return f"{fy_end.year - 1}-{fy_end.year}"


def get_exercise(company_code: str, fy_end: date) -> dict:
    raw = _setting(company_code, f"ciop:ex:{fy_end.isoformat()}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"lines": [], "collected_at": None, "other_moves": [], "accounts": []}


def save_exercise(company_code: str, fy_end: date, ex: dict) -> None:
    _save_setting(company_code, f"ciop:ex:{fy_end.isoformat()}", json.dumps(ex, ensure_ascii=False, default=str))


# ----------------------------------------------------------------- outils texte
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower())


def detect_site(cfg: dict, text: str, filename: str = "") -> Optional[str]:
    t = norm((text or "") + " " + (filename or ""))
    hits = []
    for site in cfg.get("sites", []):
        for a in site.get("aliases", []):
            i = t.find(norm(a))
            if i >= 0:
                hits.append((i, site["code"]))
    if not hits:
        return None
    # l'alias qui apparaît en premier dans le document (adresse de livraison / établissement en tête)
    return sorted(hits)[0][1]


def propose_label(text: str, fallback: str = "") -> str:
    """Règle dont le mot-clé apparaît le PLUS TÔT dans le texte (la première désignation est l'article principal) ;
    à position égale, l'ordre des règles départage (les règles précises sont avant les génériques)."""
    t = norm(text)
    best = None
    for i, (pat, lab) in enumerate(config.LABEL_RULES):
        m = re.search(pat, t)
        if m and (best is None or (m.start(), i) < best[0]):
            best = ((m.start(), i), lab)
    if best:
        return best[1]
    fb = re.sub(r"^[\s\-–•*_:.]+|[\s\-–•*_:.]+$", "", norm(fallback or "")).upper()
    fb = re.sub(r"\s*\(.*?\)\s*", " ", fb).strip()
    return (fb + " PRODUCTION").strip() if len(fb) > 3 else "MATERIEL DE PRODUCTION"


_DATE = r"(\d{2}/\d{2}/\d{4})"


def detect_dates(text: str) -> dict:
    """Dates explicites de la facture : installation / mise en service (retenues), livraison (information)."""
    t = text or ""
    out = {}
    m = re.search(r"(?i)(installation|mise en service|mise en production|mise en route)[^\n\d]{0,40}" + _DATE, t)
    if m:
        out["install"] = m.group(2)
    m = re.search(r"(?i)(livraison|livré le|livre le)[^\n\d]{0,30}" + _DATE, t)
    if m:
        out["delivery"] = m.group(2)
    return out


_KEYWORDS = "|".join(pat for pat, _ in config.LABEL_RULES) + "|inox|machine|equipement|materiel|appareil|meuble|ensemble|kit|pack"
_NOISE = r"iban|bic|rib|siret|siren|tva|tel|email|site web|penalit|decret|escompte|reglement|echeance|code client|commercial|net a payer|acompte|solde|banque|propriete|jouissance|risques|www\.|@|capital|rcs|naf|ape|facture|adresse|total|montant|designation|quantite|date|livraison|transform|bon de|serie|garantie|support|douan|transitaire|conditions|generalites|prix|catalogue|virement|cheque|modifications|stock|retard|assemble|verifie|eco-participation|main d'oeuvre|mise en place"
_ADDR = r"\b\d{5}\b(?![.,]\d)|\b(rue|chemin|route|avenue|ruelle|zone|bp|cedex|impasse|allee|lotissement)\b|france\b"


def descriptions(text: str, exclude: List[str] = None, limit: int = 5) -> List[str]:
    """Désignations probables de la facture : lignes citant un matériel (mots-clés des règles de libellé),
    sinon lignes en majuscules ; jamais les adresses, coordonnées, mentions légales, noms des parties."""
    ex = [norm(x) for x in (exclude or []) if x and len(x) > 2]
    scored = []
    for i, ln in enumerate((text or "").split("\n")):
        s = ln.strip(); n = norm(s)
        if len(s) < 6 or re.fullmatch(r"[\d\s,.\-€%:/x*]+", s) or re.search(_NOISE, n) or re.search(_ADDR, n) or any(x in n for x in ex):
            continue
        score = 3 if re.search(_KEYWORDS, n) else (1 if s.isupper() and re.search(r"[a-z]", n) else 0)
        if score:
            scored.append((-score, i, s[:80]))
    return [s for _, _, s in sorted(scored)[:limit]]


# ----------------------------------------------------------------- Pennylane
def _accounts(c, keyword: str) -> List[dict]:
    accs, cur = [], None
    while True:
        r = c.get("/ledger_accounts", limit=100, **({"cursor": cur} if cur else {}))
        accs += r.get("items") or []
        if not r.get("has_more"):
            break
        cur = r.get("next_cursor")
    kw = keyword.lower()
    return [a for a in accs if kw in (a.get("label") or "").lower() and str(a.get("number", "")).startswith("2") and not str(a.get("number", "")).startswith("28")]


def _lines(c, account_id, d0: str, d1: str) -> List[dict]:
    flt = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": account_id},
                      {"field": "date", "operator": "gteq", "value": d0}, {"field": "date", "operator": "lteq", "value": d1}])
    out, cur = [], None
    while True:
        d = c.get("/ledger_entry_lines", filter=flt, limit=100, **({"cursor": cur} if cur else {}))
        out += d.get("items") or []
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    return out


def fresh_attachment(company_code: str, entry_id) -> Tuple[Optional[str], str]:
    """(URL, nom de fichier) de la pièce d'une écriture, redemandés à Pennylane : les URL sont signées et
    expirent en ~30 minutes, on ne réutilise jamais une URL mémorisée."""
    c = for_company(company_code)
    if not c:
        return None, ""
    try:
        att = (c.get(f"/ledger_entries/{entry_id}") or {}).get("attachment") or {}
        return att.get("url") or None, att.get("filename") or ""
    except Exception:
        return None, ""


def fetch_piece(company_code: str, entry_id, filename_hint: str = "") -> Optional[Tuple[bytes, str]]:
    """Pièce d'une écriture en PDF (photo convertie), via une URL fraîche. None si absente ou inaccessible."""
    url, fn = fresh_attachment(company_code, entry_id)
    data = download(url) if url else None
    if not data:
        return None
    pdf, _ = to_pdf(data, fn or filename_hint or "piece.pdf")
    return pdf, fn


def download(url: str) -> Optional[bytes]:
    """Pièce jointe Pennylane (URL signée, accessible sans jeton, valable ~30 min)."""
    try:
        r = httpx.get(url, follow_redirects=True, timeout=60)
        if r.status_code == 200 and len(r.content) > 300:
            return r.content
    except Exception:
        pass
    return None


def to_pdf(data: bytes, filename: str) -> Tuple[bytes, str]:
    """Photo JPG/PNG -> PDF A4 (la facture doit être archivée en PDF) ; PDF inchangé."""
    import fitz
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf")
    if ext == "pdf" or data[:4] == b"%PDF":
        return data, "pdf"
    img = fitz.open(stream=data, filetype=ext)
    pdf = fitz.open()
    for pg in img:
        rect = pg.rect
        page = pdf.new_page(width=595, height=842)
        # ajuste l'image dans la page en gardant les proportions
        scale = min(555 / rect.width, 802 / rect.height)
        w, h = rect.width * scale, rect.height * scale
        page.insert_image(fitz.Rect(20, 20, 20 + w, 20 + h), stream=data)
    return pdf.tobytes(), "pdf"


def read_scan(pdf: bytes) -> Optional[dict]:
    """Facture scannée ou photographiée (aucun texte) : l'assistant (Claude) lit l'image et renvoie
    {designations: [...], lieu: adresse/établissement de livraison ou du client, dates: {install, delivery}}.
    Rien si l'assistant n'est pas configuré ou en cas d'erreur (le libellé reste à saisir)."""
    try:
        from app.core import assistant
        if not assistant.configured():
            return None
        import anthropic, base64
        client = anthropic.Anthropic()
        prompt = ("Voici une facture d'un fournisseur d'équipement (boulangerie-pâtisserie). Réponds UNIQUEMENT par un JSON : "
                  '{"designations": [désignations des articles facturés, texte exact, sans les accessoires mineurs], '
                  '"lieu": "adresse ou nom de l\'établissement client / site de livraison tel qu\'écrit", '
                  '"dates": {"install": "JJ/MM/AAAA si une date d\'installation ou de mise en service est écrite, sinon null", '
                  '"delivery": "JJ/MM/AAAA si une date de livraison est écrite, sinon null"}}')
        content = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode()}},
                   {"type": "text", "text": prompt}]
        resp = client.messages.create(model=assistant.MODEL, max_tokens=600, messages=[{"role": "user", "content": content}])
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def pdf_text(data: bytes) -> str:
    try:
        import fitz
        d = fitz.open(stream=data, filetype="pdf")
        return "\n".join(p.get_text() for p in d)
    except Exception:
        return ""


def safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "-", s or "").strip()
    return re.sub(r"\s+", " ", s)[:60]


def file_name(line: dict) -> str:
    """« AAAAMMJJ - FOURNISSEUR NUMERO.pdf » (convention du dossier CIOP)."""
    d = line["date"].replace("-", "")
    return safe_name(f"{d} - {line.get('supplier') or 'FOURNISSEUR'} {line.get('invoice_number') or line.get('piece') or ''}").rstrip() + ".pdf"


def collect(company_code: str, fy_end: date, log=None) -> dict:
    """Relit Pennylane pour l'exercice : lignes des comptes CIOP sur les journaux d'achats (retenues),
    autres mouvements (information, cadrage), pièces téléchargées, propositions (libellé, établissement, date).
    Les choix déjà faits par l'utilisateur (label, site, start, include, note) sont conservés ligne par ligne."""
    log = log or (lambda m: None)
    cfg = get_config(company_code)
    p = cfg["params"]
    c = for_company(company_code)
    if not c:
        raise RuntimeError(f"Pas de jeton Pennylane pour {company_code}")
    d0, d1 = fy_bounds(cfg, fy_end)
    prev = {str(l["entry_id"]) + ":" + str(l["line_id"]): l for l in get_exercise(company_code, fy_end).get("lines", [])}
    jm = c.journals_map()
    accs = _accounts(c, p["account_keyword"])
    log(f"{len(accs)} compte(s) CIOP (classe 2 hors amortissements) : " + ", ".join(sorted({a['number'] for a in accs})))
    lines, other = [], []
    seen_entries = {}
    for a in accs:
        for l in _lines(c, a["id"], d0.isoformat(), d1.isoformat()):
            jc = jm.get((l.get("journal") or {}).get("id")) or "?"
            amt = round(float(l.get("debit") or 0) - float(l.get("credit") or 0), 2)
            eid = (l.get("ledger_entry") or {}).get("id")
            if jc not in p["purchase_journals"]:
                other.append({"date": l["date"], "journal": jc, "account": a["number"], "amount": amt, "label": l.get("label") or "", "entry_id": eid})
                continue
            e = seen_entries.get(eid) or c.get(f"/ledger_entries/{eid}")
            seen_entries[eid] = e
            m = re.match(r"(?:Facture|Avoir|Note de crédit) (.+?) - ", e.get("label") or "")
            sup401 = next((x for x in e.get("ledger_entry_lines") or [] if str((x.get("ledger_account") or {}).get("number", "")).startswith("401")), {})
            supplier = (m.group(1) if m else "") or str((sup401.get("ledger_account") or {}).get("label") or "").split(" - ")[0]
            att = e.get("attachment") or {}
            key = f"{eid}:{l['id']}"
            old = prev.get(key, {})
            line = {"key": key, "entry_id": eid, "line_id": l["id"], "date": l["date"], "account": a["number"], "account_label": a["label"], "journal": jc,
                    "amount": amt, "supplier": supplier.strip().upper(), "invoice_number": e.get("invoice_number") or "", "piece": e.get("piece_number") or "",
                    "entry_label": e.get("label") or "", "line_label": l.get("label") or "",
                    "attachment_name": att.get("filename") or "", "attachment_url": att.get("url") or "",
                    "entry_lines": [{"acc": (x.get("ledger_account") or {}).get("number"), "d": x.get("debit"), "c": x.get("credit")} for x in e.get("ledger_entry_lines") or []]}
            # pièce : texte + indices
            text, has_file = "", False
            if line["attachment_url"]:
                data = download(line["attachment_url"])
                if data:
                    has_file = True
                    pdf, _ = to_pdf(data, line["attachment_name"])
                    text = pdf_text(pdf)
            line["has_file"] = has_file
            line["text_len"] = len(text)
            line["descriptions"] = descriptions(text, exclude=[line["supplier"], cfg["identity"].get("name", ""), line["invoice_number"]])
            line["dates"] = detect_dates(text)
            line["site_detected"] = detect_site(cfg, text, line["attachment_name"])
            line["warning"] = next((msg for pat, msg in config.INELIGIBLE_HINTS if re.search(pat, norm(text) + " " + norm(line["line_label"]))), "")
            line["vision"] = None
            if has_file and len(text.strip()) < 20:
                v = read_scan(pdf)                       # scan / photo : lecture par l'assistant (si configuré)
                if v:
                    line["vision"] = v
                    line["descriptions"] = [str(x)[:80] for x in v.get("designations") or []][:5]
                    line["dates"] = {k: v["dates"][k] for k in ("install", "delivery") if v.get("dates") and v["dates"].get(k)}
                    line["site_detected"] = detect_site(cfg, " ".join(line["descriptions"]) + " " + str(v.get("lieu") or ""), line["attachment_name"])
                    log(f"scan lu par l'assistant : {line['supplier']} {line['invoice_number']} → {line['descriptions'][:2]} / {v.get('lieu')}")
            line["label_proposed"] = propose_label(line["line_label"] + " " + " ".join(line["descriptions"]) + " " + line["attachment_name"], fallback=(line["line_label"] or (line["descriptions"] or [""])[0]))
            # choix utilisateur conservés : seulement ce qui a été MODIFIÉ à la main (différent de la proposition d'alors),
            # sinon la nouvelle proposition s'applique
            edited = lambda k, prop_k: old.get(k) and old.get(k) != old.get(prop_k)
            line["label"] = old["label"] if edited("label", "label_proposed") else line["label_proposed"]
            if not line["site_detected"] and len(cfg.get("sites", [])) == 1:
                line["site_detected"] = cfg["sites"][0]["code"]           # un seul établissement : pas d'ambiguïté
            line["site"] = old["site"] if edited("site", "site_detected") else (line["site_detected"] or "")
            start_default = line["dates"].get("install") or _fr(line["date"])
            line["start"] = old["start"] if edited("start", "start_default") else start_default
            line["start_default"] = start_default
            # retenue / note : valeur par défaut (indice d'inéligibilité, avoir) sauf si l'utilisateur l'a changée à la main
            line["include_default"] = not line["warning"]
            line["note_default"] = line["warning"] or ""
            line["_old"] = {"include": old.get("include"), "include_default": old.get("include_default"), "note": old.get("note"), "note_default": old.get("note_default")} if old else None
            lines.append(line)
            log(f"{line['date']} {line['supplier']} {line['invoice_number']} {amt:.2f} € → {line['label']} / {line['site'] or 'établissement ?'}{'' if has_file else ' (PAS DE PIÈCE)'}")
    lines.sort(key=lambda x: (x["date"], x["piece"]))
    # avoir qui annule une facture du même fournisseur (montants opposés) : les deux sortent du tableau par défaut, avec la note
    used = set()
    for l in lines:
        if l["amount"] < 0:
            inv = next((x for x in lines if x["amount"] == -l["amount"] and x["supplier"] == l["supplier"] and x["amount"] > 0 and x["key"] not in used), None)
            if inv:
                used.add(inv["key"])
                l["include_default"] = inv["include_default"] = False
                l["note_default"] = f"avoir annulant la facture {inv['invoice_number']}"
                inv["note_default"] = f"annulée par l'avoir {l['invoice_number']}"
                if not l.get("_old") or l["_old"]["include"] == l["_old"]["include_default"]:
                    l["label"] = l["label_proposed"] = inv["label_proposed"]
    for l in lines:
        old = l.pop("_old", None)
        edited_inc = bool(old) and old.get("include_default") is not None and old.get("include") != old.get("include_default")
        edited_note = bool(old) and old.get("note_default") is not None and (old.get("note") or "") != (old.get("note_default") or "")
        l["include"] = old["include"] if edited_inc else l["include_default"]
        l["note"] = (old.get("note") or "") if edited_note else l["note_default"]
    ex = {"lines": lines, "other_moves": sorted(other, key=lambda x: x["date"]), "accounts": [{"number": a["number"], "label": a["label"], "id": a["id"]} for a in accs],
          "collected_at": datetime.utcnow().isoformat(timespec="seconds"), "fy_start": d0.isoformat(), "fy_end": d1.isoformat()}
    ex["totals"] = totals(cfg, ex)
    save_exercise(company_code, fy_end, ex)
    return ex


def _fr(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%d/%m/%Y")
    except Exception:
        return iso


def site_of(cfg: dict, code: str) -> dict:
    return next((s for s in cfg.get("sites", []) if s["code"] == code), {"code": code, "label": code, "address": ""})


def reduction(cfg: dict, amount: float) -> float:
    return round(amount * float(cfg["params"]["rate_pct"]) / 100, 2)


def totals(cfg: dict, ex: dict) -> dict:
    inc = [l for l in ex["lines"] if l.get("include", True)]
    exc = [l for l in ex["lines"] if not l.get("include", True)]
    t_acc = round(sum(l["amount"] for l in ex["lines"]), 2)
    t_tab = round(sum(l["amount"] for l in inc), 2)
    t_fact = round(sum(l["amount"] for l in inc if l.get("has_file")), 2)
    t_red = round(sum(reduction(cfg, l["amount"]) for l in inc), 2)
    other_non_an = [o for o in ex.get("other_moves", []) if o["journal"] != "AN"]
    return {"comptes_achats": t_acc, "tableau": t_tab, "factures": t_fact, "reduction": t_red,
            "exclues": round(sum(l["amount"] for l in exc), 2), "n_exclues": len(exc), "n_lignes": len(inc),
            "sans_piece": [l["key"] for l in inc if not l.get("has_file")], "sans_site": [l["key"] for l in inc if not l.get("site")],
            "autres_mouvements": round(sum(o["amount"] for o in other_non_an), 2), "n_autres": len(other_non_an),
            "ok": abs(t_acc - (t_tab + sum(l["amount"] for l in exc))) < 0.005 and abs(t_tab - t_fact) < 0.005 and not [l for l in inc if not l.get("site")]}


def update_lines(company_code: str, fy_end: date, form: dict) -> dict:
    """Enregistre les choix par ligne (label, site, start, include, note)."""
    cfg = get_config(company_code)
    ex = get_exercise(company_code, fy_end)
    for l in ex["lines"]:
        k = l["key"]
        if f"label_{k}" in form:
            l["label"] = (form.get(f"label_{k}") or "").strip().upper()
            l["site"] = form.get(f"site_{k}") or ""
            l["start"] = (form.get(f"start_{k}") or "").strip() or _fr(l["date"])
            l["include"] = bool(form.get(f"include_{k}"))
            l["note"] = (form.get(f"note_{k}") or "").strip()
    ex["totals"] = totals(cfg, ex)
    save_exercise(company_code, fy_end, ex)
    return ex


# ----------------------------------------------------------------- CERFA vierges par millésime
def form_millesime(fy_end: date) -> int:
    """Millésime attendu = année de clôture (exercice clos le 30/06/2026 → CERFA 2083-SD (2026))."""
    return fy_end.year


def parse_form(pdf: bytes) -> dict:
    """Lit le millésime et la version dans le PDF (« N° 2083-SD (2026) », « N° 13445*18 ») et vérifie le calage."""
    import fitz
    from . import build
    d = fitz.open(stream=pdf, filetype="pdf")
    t = d[0].get_text() if d.page_count else ""
    m = re.search(r"2083-SD\s*\((\d{4})\)", t)
    v = re.search(r"N°\s*(13445\*\d+)", t)
    out = {"millesime": int(m.group(1)) if m else None, "version": v.group(1) if v else None, "is_2083": "2083-SD" in t}
    try:
        g = build.geometry(d)
        out.update({"ok": True, "pages": g["form_pages"], "table_page": g["table_page"] + 1, "declarant_page": g["declarant_page"] + 1})
    except Exception as e:
        out.update({"ok": False, "error": str(e)[:200]})
    return out


def forms() -> List[CiopForm]:
    with Session(engine) as s:
        rows = s.exec(select(CiopForm).order_by(CiopForm.millesime.desc())).all()
        for r in rows:
            s.expunge(r)
        return rows


def form_for(fy_end: date) -> Optional[CiopForm]:
    """Le CERFA vierge du millésime attendu, ou None (→ il faut le déposer)."""
    ensure_builtin()
    with Session(engine) as s:
        r = s.exec(select(CiopForm).where(CiopForm.millesime == form_millesime(fy_end)).order_by(CiopForm.id.desc())).first()
        if r:
            s.expunge(r)
        return r


def save_form(pdf: bytes, filename: str, by: str = None) -> Tuple[Optional[CiopForm], dict]:
    """Enregistre un CERFA vierge déposé ; refusé si ce n'est pas un 2083-SD ou si le calage échoue."""
    info = parse_form(pdf)
    if not info.get("is_2083") or not info.get("millesime"):
        return None, {**info, "refused": "Ce PDF n'est pas un formulaire 2083-SD (millésime introuvable en page 1)."}
    if not info.get("ok"):
        return None, {**info, "refused": "Mise en page non reconnue : " + str(info.get("error"))}
    with Session(engine) as s:
        for old in s.exec(select(CiopForm).where(CiopForm.millesime == info["millesime"])).all():
            s.delete(old)
        f = CiopForm(millesime=info["millesime"], version=info.get("version"), filename=filename, data=pdf, pages=info.get("pages") or 0,
                     check=json.dumps(info, ensure_ascii=False), by_user=by)
        s.add(f)
        s.commit()
        s.refresh(f)
        s.expunge(f)
        return f, info


def ensure_builtin() -> None:
    """Le CERFA 2026 livré avec le module est enregistré une fois (millésime 2026)."""
    import os
    from . import build
    with Session(engine) as s:
        if s.exec(select(CiopForm).where(CiopForm.millesime == 2026)).first():
            return
    if os.path.exists(build.ASSET):
        save_form(open(build.ASSET, "rb").read(), "2083-sd_2026 (modèle intégré).pdf", by="Vaelan")
