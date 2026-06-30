"""
Copywriter: assembles personalised cold outreach emails from YAML templates.
Fully offline — no external API calls.

Assembly per lead
─────────────────
  1. opening    niche-specific, rotated by lead_id
  2. value_prop with_desc variant when company_desc > 50 chars, else general
  3. cta        rotated by lead_id (offset so it differs from opening rotation)

  body    = opening  +  "\\n\\n"  +  value_prop  +  "\\n\\n"  +  cta
  subject = subjects[niche][lead_id % pool_size]

Validation
──────────
  Word count ≤ EMAIL_BODY_MAX_WORDS and no FORBIDDEN_PHRASES.
  If validation fails: retry with value_props.short.
  If still invalid: flag the lead.
"""

import logging
from pathlib import Path

import yaml

from config import settings
from db import database

logger = logging.getLogger(__name__)

_templates: dict | None = None


def _load_templates() -> dict:
    global _templates
    if _templates is None:
        path = Path(settings.BASE_DIR) / "config" / "email_templates.yaml"
        _templates = yaml.safe_load(path.read_text())
    return _templates


def _pick(pool: list, idx: int) -> str:
    return pool[idx % len(pool)]


def _render(template: str, lead: dict) -> str:
    """Fill placeholders; silently ignore unknown keys or malformed company data."""
    company    = lead.get("company")      or "your company"
    niche      = lead.get("niche")        or "technology"
    desc       = lead.get("company_desc") or ""
    desc_short = (desc[:80].rstrip() + "...") if len(desc) > 80 else desc

    # Sanitise values that could break str.format() if they contain literal braces
    def _safe(s: str) -> str:
        return s.replace("{", "{{").replace("}", "}}")

    safe_company    = _safe(company)
    safe_niche      = _safe(niche)
    safe_desc       = _safe(desc)

    # desc_short has already been built from desc, so sanitise the same way
    safe_desc_short = _safe(desc_short)

    # Normalise YAML folded/literal block scalars: collapse internal newlines
    # that the YAML parser left as-is (folded lines become spaces, not \n)
    template = " ".join(template.split())

    try:
        return template.format(
            company=safe_company,
            niche=safe_niche,
            company_desc=safe_desc,
            company_desc_short=safe_desc_short,
        )
    except (KeyError, IndexError, ValueError):
        # Last-resort: replace manually without format()
        return (
            template
            .replace("{company}", safe_company)
            .replace("{niche}", safe_niche)
            .replace("{company_desc_short}", safe_desc_short)
            .replace("{company_desc}", safe_desc)
        )


def _count_words(text: str) -> int:
    return len(text.split())


def _validate(text: str) -> bool:
    return (
        _count_words(text) <= settings.EMAIL_BODY_MAX_WORDS
        and not any(phrase.lower() in text.lower() for phrase in settings.FORBIDDEN_PHRASES)
    )


def _assemble_body(lead: dict, t: dict) -> tuple[str, bool]:
    """
    Returns (body, is_valid).
    Tries short value_prop on first validation failure before giving up.
    """
    niche = lead.get("niche") or "technology"
    idx   = lead.get("id", 0)
    desc  = lead.get("company_desc") or ""

    # Opening: prefer niche pool, fall back to "technology"
    openings = t["openings"].get(niche) or t["openings"]["technology"]
    opening  = _render(_pick(openings, idx), lead)

    # Value prop pool selection
    vp_pool = (
        t["value_props"]["with_desc"]
        if len(desc) > 50
        else t["value_props"]["general"]
    )
    value_prop = _render(_pick(vp_pool, idx + 1), lead)

    # CTA (offset +2 so the rotation is independent of the opening)
    cta  = _render(_pick(t["ctas"], idx + 2), lead)
    body = f"{opening}\n\n{value_prop}\n\n{cta}"

    if _validate(body):
        return body, True

    # First failure: swap in the shorter value prop variant
    short_vp = _render(_pick(t["value_props"]["short"], idx), lead)
    body = f"{opening}\n\n{short_vp}\n\n{cta}"
    return body, _validate(body)


def _assemble_subject(lead: dict, t: dict) -> str:
    niche    = lead.get("niche") or "technology"
    idx      = lead.get("id", 0)
    subjects = t["subjects"].get(niche) or t["subjects"]["technology"]
    raw      = _render(_pick(subjects, idx + 3), lead)
    return raw.rstrip(".!?,;:")


def run_copywriting(on_draft=None) -> dict:
    """
    Draft emails for all 'ready' leads. Returns {drafted, flagged, skipped}.
    on_draft(company, subject) — optional callback fired per drafted email.
    """
    stats = {"drafted": 0, "flagged": 0, "skipped": 0}
    leads = database.get_leads_by_status("ready")

    if not leads:
        logger.debug("No ready leads for copywriting.")
        return stats

    t = _load_templates()
    logger.info("Copywriting %d leads (template engine)", len(leads))

    for lead in leads:
        ld      = dict(lead)
        lead_id = ld["id"]

        body, valid = _assemble_body(ld, t)
        subject     = _assemble_subject(ld, t)

        if not valid:
            logger.warning(
                "Body failed validation for lead_id=%d (%s) — flagging",
                lead_id, ld.get("company"),
            )
            database.update_lead_status(lead_id, "flagged")
            stats["flagged"] += 1
            continue

        database.insert_email(lead_id, subject, body, _count_words(body))
        database.update_lead_status(lead_id, "drafted")
        stats["drafted"] += 1
        if on_draft:
            on_draft(ld.get("company", ""), subject)
        logger.debug("Drafted lead_id=%d (%s) — %d words", lead_id, ld.get("company"), _count_words(body))

    logger.info("Copywriting done: %s", stats)
    return stats
