"""
Twilio voice webhooks and outbound call trigger.

Pipeline (100% Twilio + Grok, no third-party STT/TTS):
  1. Caller speaks -> Twilio <Gather input="speech"> transcribes it (Twilio ASR)
     and POSTs SpeechResult to /voice/respond.
  2. We run Grok structured extraction -> auto-booking engine -> Grok reply.
  3. Reply is spoken back via Twilio <Say> (Amazon Polly voice), then we
     <Gather> again for the caller's next turn.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException
from twilio.twiml.voice_response import Gather, VoiceResponse

from app.api.deps import get_call_state_store, get_db, get_super_admin, verify_twilio_signature
from app.core.config import settings
from app.core.tenancy import TenantScope, scope_filter
from app.models import CallDirection, CallLog, CallStatus, Contact, SpaAccount, User
from app.schemas import OutboundCallRequest, OutboundCallResponse
from app.services.appointment_booking_service import BookingOutcome, attempt_booking
from app.services.call_state import CallSession, CallStateStore
from app.services.grok_service import build_spa_prompt_context, grok_service
from app.services.twilio_service import twilio_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telephony", tags=["telephony"])

TWIML_MEDIA_TYPE = "application/xml"
INTENT_CONFIDENCE_THRESHOLD = 0.55
ACTIONABLE_INTENTS = {"schedule", "reschedule", "cancel"}
GOODBYE_MARKERS = ("goodbye", "have a great day", "bye now")

_TWILIO_STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "answered": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.CANCELLED,
}


def _twiml(vr: VoiceResponse) -> Response:
    return Response(content=str(vr), media_type=TWIML_MEDIA_TYPE)


def _gather_twiml(prompt: str, voice: str) -> VoiceResponse:
    """Speak `prompt` via Twilio TTS, then gather the caller's next utterance
    via Twilio's built-in speech recognition."""
    vr = VoiceResponse()
    
    # Use full absolute URL to avoid route-resolution issues across proxies
    action_url = (
    f"{settings.PUBLIC_BASE_URL}"
    f"{settings.API_V1_PREFIX}/telephony/voice/respond"
)

    gather = Gather(
        input="speech",
        action=action_url,
        method="POST",
        speech_timeout="auto",
        language=settings.DEFAULT_TWIML_LANGUAGE,
        action_on_empty_result=True,
    )
    gather.say(prompt, voice=voice)
    vr.append(gather)

    # Fallback if gather times out or no speech is heard
    vr.say("Are you still there?", voice=voice)
    vr.redirect(action_url, method="POST")
    return vr


def _resolve_voice(spa: SpaAccount | None, owner: User | None) -> str:
    """Per-tenant Twilio <Say> voice, with the workspace setting as fallback."""
    if spa and spa.twiml_voice:
        return spa.twiml_voice
    if owner and isinstance(owner.workspace_settings, dict):
        return owner.workspace_settings.get("twiml_voice", settings.DEFAULT_TWIML_VOICE)
    return settings.DEFAULT_TWIML_VOICE


async def _resolve_inbound_target(
    db: AsyncSession, to_number: str
) -> tuple[SpaAccount | None, User | None]:
    """Dispatch an inbound call to the tenant that owns the dialed number.

    Spa accounts are checked first: an inbound B2C call is the Spa Receptionist
    product, and the `To` number is the only thing that identifies which spa is
    answering. Falling through to a workspace user keeps 6DM's own inbound
    number working.

    This lookup is the whole of per-spa routing — onboarding a spa means
    inserting a `SpaAccount` row with its number, nothing more.
    """
    spa = (
        await db.execute(
            select(SpaAccount).where(
                SpaAccount.twilio_phone_number == to_number,
                SpaAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if spa:
        return spa, None

    owner = (
        await db.execute(select(User).where(User.twilio_phone_number == to_number))
    ).scalar_one_or_none()
    if owner:
        return None, owner

    # Single-tenant dev fallback: if exactly one user exists, treat them as owner
    users = (await db.execute(select(User).limit(2))).scalars().all()
    return None, (users[0] if len(users) == 1 else None)


# --------------------------------------------------------------------------- #
# Inbound call entrypoint
# --------------------------------------------------------------------------- #
@router.post("/voice/inbound", dependencies=[Depends(verify_twilio_signature)])
async def voice_inbound(
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    logger.info("Inbound call %s from %s to %s", CallSid, From, To)

    spa, owner = await _resolve_inbound_target(db, To)
    voice = _resolve_voice(spa, owner)

    if spa:
        logger.info("Call %s routed to spa tenant %s (%s)", CallSid, spa.id, spa.name)
    elif owner is None:
        logger.warning("Inbound call %s to unclaimed number %s", CallSid, To)

    # Everything this call creates lands in exactly one scope.
    scope: TenantScope | None = None
    if spa:
        scope = TenantScope.for_tenant(spa.id)
    elif owner:
        scope = TenantScope.for_sales_workspace(owner.id)

    contact = None
    if scope:
        contact = (
            await db.execute(
                select(Contact).where(
                    scope_filter(scope, Contact), Contact.phone_number == From
                )
            )
        ).scalar_one_or_none()

    call_log = CallLog(
        twilio_call_sid=CallSid,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        from_number=From,
        to_number=To,
        contact_id=contact.id if contact else None,
        user_id=owner.id if owner else None,
        tenant_id=spa.id if spa else None,
        started_at=datetime.now(timezone.utc),
    )
    db.add(call_log)
    await db.commit()

    session = CallSession(
        call_sid=CallSid,
        direction="inbound",
        from_number=From,
        to_number=To,
        user_id=str(owner.id) if owner else None,
        tenant_id=str(spa.id) if spa else None,
        business_name=spa.name if spa else settings.APP_NAME,
        # Rendered once here from the tenant's row (prompt, service menu, staff,
        # opening hours) and reused for every turn of this call.
        tenant_prompt=build_spa_prompt_context(spa) if spa else None,
        voice=voice,
    )
    if contact:
        session.entities["known_contact"] = {"id": str(contact.id), "name": contact.full_name}

    business = spa.name if spa else None
    if contact and contact.first_name:
        greeting = f"Hello {contact.first_name}, thanks for calling back. How can I help you today?"
    elif business:
        greeting = f"Thank you for calling {business}. How can I help you today?"
    else:
        greeting = "Hello! Thank you for calling. How can I help you today?"

    session.add_turn("assistant", greeting)
    await state.create(session)

    return _twiml(_gather_twiml(greeting, voice))


# --------------------------------------------------------------------------- #
# Conversation turn: Twilio posts caller speech (its own ASR), Grok extracts
# intent, the booking engine runs, then Grok's reply is spoken via <Say>.
# --------------------------------------------------------------------------- #
@router.post("/voice/respond", dependencies=[Depends(verify_twilio_signature)])
async def voice_respond(
    CallSid: str = Form(...),
    SpeechResult: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    session = await state.get(CallSid)

    if session is None:
        vr = VoiceResponse()
        vr.say("I'm sorry, something went wrong. Please call back. Goodbye.")
        vr.hangup()
        return _twiml(vr)

    # Infinite loop guard for silent / unclear responses
    if not SpeechResult:
        retry_count = session.entities.get("retry_count", 0) + 1
        session.entities["retry_count"] = retry_count
        await state.save(session)

        if retry_count >= 2:
            vr = VoiceResponse()
            vr.say("I'm having trouble hearing you. Please try calling back later. Goodbye.", voice=session.voice)
            vr.hangup()
            return _twiml(vr)

        return _twiml(_gather_twiml("I didn't catch that. Could you say it again?", session.voice))

    # Reset retry count upon successfully receiving speech
    session.entities["retry_count"] = 0
    session.add_turn("user", SpeechResult)

    # Structured extraction + auto-booking BEFORE the agent replies
    intent = await grok_service.extract_appointment_intent(session)
    if (
        intent
        and intent.confidence >= INTENT_CONFIDENCE_THRESHOLD
        and intent.intent in ACTIONABLE_INTENTS
    ):
        result = await attempt_booking(db, session, intent)
        session.entities["last_booking_outcome"] = result.outcome.value
        if result.outcome != BookingOutcome.SKIPPED:
            session.add_turn("system", result.to_system_message())
        if result.appointment:
            session.entities["active_appointment_id"] = str(result.appointment.id)

    reply = await grok_service.generate_voice_response(session)
    session.add_turn("assistant", reply)
    await state.save(session)

    if any(phrase in reply.lower() for phrase in GOODBYE_MARKERS):
        vr = VoiceResponse()
        vr.say(reply, voice=session.voice)
        vr.hangup()
        return _twiml(vr)

    return _twiml(_gather_twiml(reply, session.voice))


# --------------------------------------------------------------------------- #
# Outbound calls — 6DM Sales Agent only.
#
# This is the dial trigger for the outbound B2B product, so it is fenced to
# super_admin: a spa_admin or spa_staff token gets 403 here, and their bookings
# can never reach Dominic's calendar.
# --------------------------------------------------------------------------- #
@router.post("/voice/outbound", response_model=OutboundCallResponse)
async def voice_outbound(
    payload: OutboundCallRequest,
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> OutboundCallResponse:
    configured_from_number = current_user.twilio_phone_number or settings.TWILIO_PHONE_NUMBER
    from_number = payload.from_number or configured_from_number
    voice = _resolve_voice(None, current_user)

    if not configured_from_number:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No caller ID configured. Set TWILIO_PHONE_NUMBER or the user's twilio_phone_number.",
        )
    if payload.from_number and payload.from_number != configured_from_number:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="from_number must match the configured workspace Twilio number.",
        )

    sales_scope = TenantScope.for_sales_workspace(current_user.id)
    if payload.contact_id:
        # Only dial leads from the sales workspace — never a spa's guest list.
        lead = (
            await db.execute(
                select(Contact).where(
                    scope_filter(sales_scope, Contact),
                    Contact.id == payload.contact_id,
                )
            )
        ).scalar_one_or_none()
        if lead is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Lead not found in the sales workspace"
            )

    try:
        call_sid = await twilio_service.create_outbound_call(payload.to_number, from_number)
    except TwilioException as exc:
        # Upstream provider rejected the call (bad number, unverified trial number,
        # bad credentials...). Surface it as a 502 rather than an opaque 500.
        logger.error("Twilio rejected outbound call to %s: %s", payload.to_number, exc)
        # TwilioRestException.__str__ embeds ANSI colour codes meant for a terminal;
        # `msg` is the bare provider message.
        reason = getattr(exc, "msg", None) or str(exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"Telephony provider rejected the call: {reason}",
        ) from exc

    call_log = CallLog(
        twilio_call_sid=call_sid,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        from_number=from_number,
        to_number=payload.to_number,
        contact_id=payload.contact_id,
        user_id=current_user.id,
        tenant_id=None,  # outbound sales is the platform workspace, not a spa
    )
    db.add(call_log)
    await db.commit()
    await db.refresh(call_log)

    session = CallSession(
        call_sid=call_sid,
        direction="outbound",
        from_number=from_number,
        to_number=payload.to_number,
        call_objective=payload.call_objective,
        user_id=str(current_user.id),
        voice=voice,
    )
    await state.create(session)

    return OutboundCallResponse(call_sid=call_sid, call_log_id=call_log.id, status="queued")


@router.post("/voice/outbound/answer", dependencies=[Depends(verify_twilio_signature)])
async def voice_outbound_answer(
    CallSid: str = Form(...),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    """Twilio fetches TwiML here when the callee answers the outbound call."""
    session = await state.get(CallSid)
    voice = session.voice if session else settings.DEFAULT_TWIML_VOICE

    if session and session.call_objective:
        session.add_turn(
            "user",
            "[SYSTEM: The callee just answered the phone. Deliver your opening line.]",
        )
        opening = await grok_service.generate_voice_response(session)
        session.history.pop()  # remove synthetic instruction turn
    else:
        opening = "Hello! This is the AI assistant calling. Is now a good time to talk?"

    if session:
        session.add_turn("assistant", opening)
        await state.save(session)

    return _twiml(_gather_twiml(opening, voice))


# --------------------------------------------------------------------------- #
# Lifecycle callbacks
# --------------------------------------------------------------------------- #
@router.post("/voice/status", dependencies=[Depends(verify_twilio_signature)])
async def voice_status(
    CallSid: str = Form(...),
    CallStatus_: str = Form(..., alias="CallStatus"),
    CallDuration: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    call_log = (
        await db.execute(select(CallLog).where(CallLog.twilio_call_sid == CallSid))
    ).scalar_one_or_none()

    if call_log:
        call_log.status = _TWILIO_STATUS_MAP.get(CallStatus_, call_log.status)
        if CallStatus_ in ("in-progress", "answered") and not call_log.started_at:
            call_log.started_at = datetime.now(timezone.utc)
        if CallStatus_ in ("completed", "failed", "busy", "no-answer", "canceled"):
            call_log.ended_at = datetime.now(timezone.utc)
            call_log.duration_seconds = CallDuration

            session = await state.end(CallSid)
            if session:
                call_log.transcript = session.transcript_text
                analysis = await grok_service.analyze_call(session)
                if analysis:
                    call_log.ai_summary = analysis.summary
                    call_log.ai_analysis = analysis.model_dump()
        await db.commit()

    return Response(status_code=204)


@router.post("/voice/recording", dependencies=[Depends(verify_twilio_signature)])
async def voice_recording(
    CallSid: str = Form(...),
    RecordingUrl: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    call_log = (
        await db.execute(select(CallLog).where(CallLog.twilio_call_sid == CallSid))
    ).scalar_one_or_none()
    if call_log:
        call_log.recording_url = f"{RecordingUrl}.mp3"
        await db.commit()
    return Response(status_code=204)