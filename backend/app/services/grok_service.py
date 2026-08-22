"""
Grok (xAI) pipeline service — the conversational brain behind the voice agent.

Twilio handles all audio I/O (speech-to-text via <Gather>, text-to-speech via
<Say>). This service is purely text-in / text-out:
  1. Generate the next spoken reply given call history and agent persona.
  2. Extract structured appointment intent from the transcript so far.
  3. Post-call summarization for CallLog persistence.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.call_state import CallSession

logger = logging.getLogger(__name__)


def _tts_text(value: str) -> str:
    """Keep model output readable when Twilio sends it through <Say>."""
    value = re.sub(r"[`*_#>]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------
class AppointmentIntent(BaseModel):
    intent: str = Field(
        ..., description="One of: schedule, reschedule, cancel, inquiry, other"
    )
    caller_name: str | None = None
    caller_email: str | None = None
    requested_start_iso: str | None = Field(
        None, description="ISO8601 datetime the caller requested, if any"
    )
    requested_end_iso: str | None = None
    service_description: str | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class CallAnalysis(BaseModel):
    summary: str
    sentiment: str = Field(..., description="positive | neutral | negative")
    action_items: list[str] = Field(default_factory=list)
    appointment: AppointmentIntent | None = None


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
VOICE_CALL_BASE_RULES = """
RULES:
- You are on a live phone call. Twilio will speak your reply aloud via text-to-speech.
- Keep responses SHORT (1-2 sentences), natural, and conversational. 
- Never use markdown, bullet points, lists, emojis, or special characters.
- Confirm names, phone numbers, dates, and times by repeating them back.
- If you don't understand, politely ask the caller to repeat.
- When the conversation is complete, thank the person and say goodbye.
"""

SALES_AGENT_PROMPT = f"""
You are a top-tier B2B sales agent for 6DM.
Current date/time: {{now_iso}}.

Your goal is to talk to the business owner or manager, qualify their business, 
handle objections smoothly, and book a sales presentation on Dominic's calendar.

{VOICE_CALL_BASE_RULES}
{{objective_block}}
"""

SPA_RECEPTIONIST_PROMPT = f"""
You are a friendly, professional AI voice receptionist for {{business_name}}.
Current date/time: {{now_iso}}.

Your goal is to answer customer questions, check service availability,
and book spa service appointments directly onto the Spa's calendar.

{VOICE_CALL_BASE_RULES}
{{tenant_block}}
{{objective_block}}
"""


def build_spa_prompt_context(spa: Any) -> str:
    """Render a tenant's receptionist persona from its SpaAccount row.

    Returns only the tenant-specific block; the shared voice rules are added by
    `_build_messages`. Called once at call setup and cached on the CallSession,
    so onboarding a spa is a database insert: its custom prompt, service menu,
    staff and opening hours reach the model with no code change.

    The result is interpolated *into* the template rather than formatted, so a
    stray brace in a tenant's own prompt text cannot break rendering.
    """
    from app.services.business_hours import describe_business_hours

    sections: list[str] = []

    if spa.grok_system_prompt:
        sections.append(spa.grok_system_prompt.strip())

    services = spa.services or []
    if services:
        lines = []
        for service in services:
            parts = [str(service.get("name", "")).strip()]
            if service.get("duration_minutes"):
                parts.append(f"{service['duration_minutes']} minutes")
            if service.get("price"):
                parts.append(str(service["price"]))
            lines.append("- " + " · ".join(p for p in parts if p))
        sections.append("SERVICE MENU:\n" + "\n".join(lines))

    staff = spa.staff or []
    if staff:
        lines = [
            "- "
            + str(member.get("name", "")).strip()
            + (f" ({member['role']})" if member.get("role") else "")
            for member in staff
        ]
        sections.append("TEAM:\n" + "\n".join(lines))

    sections.append(describe_business_hours(spa.business_hours, spa.timezone))
    sections.append(
        "Never book outside the opening hours above. If a caller asks for a "
        "closed time, say so and offer the nearest open slot."
    )

    return "\n\n".join(section for section in sections if section)

EXTRACTION_SYSTEM_PROMPT = """\
You extract structured scheduling data from phone call transcripts.
Current date/time: {now_iso}. Resolve relative dates ("tomorrow at 2") to ISO8601.
Respond ONLY with valid JSON matching the provided schema. No prose.
"""

SUMMARY_SYSTEM_PROMPT = """\
You are a call-analysis engine. Given a phone transcript, produce JSON with:
summary (2-3 sentences), sentiment (positive|neutral|negative),
action_items (list of strings), and appointment (object or null) with fields:
intent, caller_name, caller_email, requested_start_iso, requested_end_iso,
service_description, confidence. Respond ONLY with valid JSON.
"""


# ---------------------------------------------------------------------------
# Service Implementation
# ---------------------------------------------------------------------------
class GrokService:
    """Async client wrapper around the xAI Grok chat completions API."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.XAI_BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=settings.GROK_TIMEOUT_SECONDS,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": settings.GROK_MODEL,
            "messages": messages,
            "temperature": temperature if temperature is not None else settings.GROK_TEMPERATURE,
            "max_tokens": max_tokens or settings.GROK_MAX_TOKENS,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _build_messages(self, session: CallSession) -> list[dict[str, str]]:
        objective_block = (
            f"\nCALL OBJECTIVE: {session.call_objective}"
            if session.call_objective
            else ""
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        # Dynamic System Prompt Selection: Outbound = B2B Sales, Inbound = Spa
        # Receptionist for whichever tenant owns the dialed number. The tenant
        # block is rendered at call setup by `build_spa_prompt_context` and
        # carried on the session.
        if session.direction == "outbound":
            system = SALES_AGENT_PROMPT.format(
                now_iso=now_iso,
                objective_block=objective_block,
            )
        else:
            system = SPA_RECEPTIONIST_PROMPT.format(
                business_name=session.business_name or settings.APP_NAME,
                now_iso=now_iso,
                tenant_block=session.tenant_prompt or "",
                objective_block=objective_block,
            )

        return [{"role": "system", "content": system}, *session.history]

    async def generate_voice_response(self, session: CallSession) -> str:
        """Full conversational reply, spoken by Twilio's <Say>."""
        try:
            reply = await self._chat(self._build_messages(session))
            return _tts_text(reply)
        except httpx.HTTPError:
            logger.exception("Grok API failure for call %s", session.call_sid)
            return "I'm sorry, I'm having a little trouble right now. Could you say that again?"

    async def extract_appointment_intent(self, session: CallSession) -> AppointmentIntent | None:
        if not session.history:
            return None
        messages = [
            {
                "role": "system",
                "content": EXTRACTION_SYSTEM_PROMPT.format(
                    now_iso=datetime.now(timezone.utc).isoformat()
                ),
            },
            {
                "role": "user",
                "content": (
                    "JSON schema:\n"
                    f"{json.dumps(AppointmentIntent.model_json_schema())}\n\n"
                    f"Transcript:\n{session.transcript_text}"
                ),
            },
        ]
        try:
            raw = await self._chat(messages, json_mode=True, temperature=0.0)
            return AppointmentIntent.model_validate_json(raw)
        except Exception:
            logger.exception("Intent extraction failed for call %s", session.call_sid)
            return None

    async def analyze_call(self, session: CallSession) -> CallAnalysis | None:
        if not session.history:
            return None
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Transcript:\n{session.transcript_text}"},
        ]
        try:
            raw = await self._chat(messages, json_mode=True, temperature=0.0, max_tokens=1024)
            return CallAnalysis.model_validate_json(raw)
        except Exception:
            logger.exception("Call analysis failed for call %s", session.call_sid)
            return None


grok_service = GrokService()