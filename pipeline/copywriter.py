"""
Copywriter: calls Gemini API to generate personalised email body + subject line.
Two separate calls per lead. Validates output. Retries once on failure.
Flags lead after two consecutive failures.
"""

import logging
import time
from typing import Optional, Tuple

import google.generativeai as genai

from config import settings
from db import database

logger = logging.getLogger(__name__)

genai.configure(api_key=settings.GEMINI_API_KEY)
_MODEL = genai.GenerativeModel("gemini-1.5-flash")

_SYSTEM_PROMPT = """You are a technical copywriter writing cold outreach emails for a software
developer seeking freelance or job opportunities. Output rules:
- Under 100 words strictly
- First person, direct tone
- Specific to the company's domain — no generic claims
- Forbidden phrases: "I hope", "passionate", "excited to", "leverage",
  "synergy", "innovative", "cutting-edge", "I wanted to reach out",
  "touch base", "circle back"
- End with one clear low-friction call to action
- Return only the email body, nothing else"""

_BODY_TEMPLATE = """Company: {company}
Industry: {niche}
What they do: {company_desc}

My stack: Java (Spring Boot), Python, Docker, GitHub Actions CI/CD
My work: Internship Management Portal (Angular + Spring Boot + JWT + MySQL),
CampusConnect (Railway deployment, Docker, 194 automated tests)

Write a cold outreach email for a freelance backend/full-stack opportunity."""

_SUBJECT_TEMPLATE = """Write a subject line for this cold email:
- Under 8 words
- No punctuation at end
- Not a question
- Reference something specific about their company or industry
- Forbidden: "Quick question", "Following up", "Opportunity", "Hi"

Email body: {email_body}
Company: {company_name}"""


def _validate(text: str) -> bool:
    word_count = len(text.split())
    has_forbidden = any(f in text.lower() for f in settings.FORBIDDEN_PHRASES)
    return word_count <= settings.EMAIL_BODY_MAX_WORDS and not has_forbidden


def _call_gemini(prompt: str) -> Optional[str]:
    try:
        response = _MODEL.generate_content(
            f"{_SYSTEM_PROMPT}\n\n{prompt}",
            generation_config={"temperature": 0.7, "max_output_tokens": 300},
        )
        return response.text.strip()
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
            logger.debug("Body validation failed, retrying for lead_id=%s", lead.get("id"))
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
        lead_id = lead_dict["id"]

        body = _generate_body(lead_dict)
        if not body:
            database.update_lead_status(lead_id, "flagged", "email body generation failed x2")
            stats["flagged"] += 1
            continue

        subject = _generate_subject(body, lead_dict.get("company", ""))
        if not subject:
            subject = f"Backend developer available — {lead_dict.get('company', 'your team')}"

        word_count = len(body.split())
        database.insert_email(lead_id, subject, body, word_count)
        database.update_lead_status(lead_id, "drafted")
        stats["drafted"] += 1
        logger.debug("Drafted email for lead_id=%d (%s)", lead_id, lead_dict.get("company"))

        # Respect Gemini free-tier rate limits
        time.sleep(1.5)

    logger.info("Copywriting done: %s", stats)
    return stats
