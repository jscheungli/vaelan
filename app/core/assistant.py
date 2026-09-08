"""Assistant conversationnel (Claude) — répond aux questions de l'équipe sur les réservations,
avec le contexte fourni par le module appelant. Clé : ANTHROPIC_API_KEY (Render) ou
section "anthropic": {"apiKey": "…"} de credentials.json (local)."""
import os
from typing import List, Optional

MODEL = "claude-opus-5"


def configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def answer(system: str, question: str, history: Optional[List[dict]] = None, max_tokens: int = 1500) -> str:
    """Réponse texte à `question` (historique optionnel = liste de {"role", "content"})."""
    if not configured():
        return "(assistant non configuré : ANTHROPIC_API_KEY absente)"
    import anthropic
    client = anthropic.Anthropic()
    messages = list(history or []) + [{"role": "user", "content": question}]
    try:
        try:
            # Claude Opus 5 : pensée adaptative par défaut, effort bas (réponses courtes d'équipe),
            # repli serveur en cas de refus (recommandation SDK)
            resp = client.beta.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system, messages=messages,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        except TypeError:            # SDK plus ancien (sans `fallbacks`)
            resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system, messages=messages)
    except anthropic.RateLimitError:
        return "L'assistant est momentanément saturé, réessayez dans une minute."
    except anthropic.APIStatusError as e:
        return f"Assistant indisponible (erreur {e.status_code})."
    except anthropic.APIConnectionError:
        return "Assistant injoignable (réseau)."
    if getattr(resp, "stop_reason", "") == "refusal":
        return "Je ne peux pas répondre à cette demande."
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip() or "(réponse vide)"
