"""
Copywriter: calls Gemini API to generate personalised email body + subject line.
Two separate calls per lead. Validates output. Retries once on failure.
Flags lead after two consecutive failures.
Uses the google-genai SDK (replaces deprecated google-generativeai).
"""

import logging
import time
from typing import Optional

from google import genai
from google.genai import types

from config import settings
from db import database

logger = logging.getLogger(__name__)

_client: genai.Client | None = None
_MODEL  = "gemini-2.5-flash"


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not set. Add it to your .env file.")
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client

_SYSTEM_PROMPT = (
    "You are a technical copywriter writing cold outreach emails for a software developer "
    "offering backend engineering services to businesses. Output rules:\n"
    "- Under 100 words strictly\n"
    "- First person, direct tone\n"
    "- Frame the email as offering value to their business, not asking for a job\n"
    "- Specific to the company's domain and likely tech pain points\n"
    "- Forbidden phrases: \"I hope\", \"passionate\", \"excited to\", \"leverage\", "
    "\"synergy\", \"innovative\", \"cutting-edge\", \"I wanted to reach out\", "
    "\"touch base\", \"circle back\", \"just checking in\"\n"
    "- End with one clear low-friction call to action (e.g. a 15-min call)\n"
    "- Return only the email body, nothing else"
)

_BODY_TEMPLATE = (
    "Company: {company}\n"
    "Industry: {niche}\n"
    "What they do: {company_desc}\n\n"
    "My stack: Java (Spring Boot), Python, Docker, GitHub Actions CI/CD\n"
    "My work: Internship Management Portal (Angular + Spring Boot + JWT + MySQL),\n"
    "CampusConnect (Railway deployment, Docker, 194 automated tests)\n\n"
    "Write a cold outreach email offering backend/full-stack development services "
    "to this business. The recipient may be a founder, CEO, or decision-maker. "
    "Show you understand their domain and propose a concrete way to help."
)

_SUBJECT_TEMPLATE = (
    "Write a subject line for this cold email:\n"
    "- Under 8 words\n"
    "- No punctuation at end\n"
    "- Not a question\n"
    "- Reference something specific about their company or industry\n"
    "- Forbidden: \"Quick question\", \"Following up\", \"Opportunity\", \"Hi\", \"Services\"\n\n"
    "Email body: {email_body}\n"
    "Company: {company_name}"
)

_GEN_CONFIG = types.GenerateContentConfig(
    temperature=0.7,
    max_output_tokens=300,
    system_instruction=_SYSTEM_PROMPT,
)


def _validate(text: str) -> bool:
    word_count     = len(text.split())
    has_forbidden  = any(f in text.lower() for f in settings.FORBIDDEN_PHRASES)
    return word_count <= settings.EMAIL_BODY_MAX_WORDS and not has_forbidden


def _call_gemini(prompt: str) -> Optional[str]:
    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=_GEN_CONFIG,
        )
        return response.text.strip() if response.text else None
    except Exception as exc:
        logger.warning("Gemini API error: %s", exc)
        return None


def _generate_body(lead: dict) -> Optional[str]:
    prompt = _BODY_TEMPLATE.format(
        company=lead.get("company", "Unknown"),
        niche=lead.get("niche") or "technology",
        company_desc=lead.get("company_desc") or "a technology company",
    )
    for attempt in range(2):
        text = _call_gemini(prompt)
        if text and _validate(text):
            return text
        if attempt == 0:
            logger.debug("Body validation failed — retrying lead_id=%s", lead.get("id"))
            time.sleep(2)
    return None


def _generate_subject(body: str, company: str) -> Optional[str]:
    prompt = _SUBJECT_TEMPLATE.format(email_body=body, company_name=company)
    for attempt in range(2):
        text = _call_gemini(prompt)
        if text and len(text.split()) <= 10:
            return text.rstrip(".!?,;:")
        if attempt == 0:
            time.sleep(2)
    return None


def run_copywriting() -> dict:
    """
    Generates email body + subject for all 'ready' leads.
    Returns stats: {drafted, flagged, skipped}
    """
    stats = {"drafted": 0, "flagged": 0, "skipped": 0}
    leads = database.get_leads_by_status("ready")

    if not leads:
        logger.info("No ready leads for copywriting.")
        return stats

    logger.info("Copywriting %d leads...", len(leads))

    for lead in leads:
        lead_dict = dict(lead)
        lead_id   = lead_dict["id"]

        body = _generate_body(lead_dict)
        if not body:
            database.update_lead_status(lead_id, "flagged", "email body generation failed x2")
            stats["flagged"] += 1
            continue

        subject = _generate_subject(body, lead_dict.get("company", ""))
        if not subject:
            subject = f"Backend developer available — {lead_dict.get('company', 'your team')}"

        database.insert_email(lead_id, subject, body, len(body.split()))
        database.update_lead_status(lead_id, "drafted")
        stats["drafted"] += 1
        logger.debug("Drafted for lead_id=%d (%s)", lead_id, lead_dict.get("company"))

        time.sleep(1.5)  # free-tier rate limit

    logger.info("Copywriting done: %s", stats)
    return stats
