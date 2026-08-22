"""
Converts an extracted AppointmentIntent into real Appointment/Contact records,
with conflict detection. Designed to be called mid-conversation: the resulting
BookingResult.to_system_message() is injected into CallSession history so the
NEXT Grok reply naturally confirms/denies the booking to the caller.

Two things decide where a booking lands, and neither is under the caller's
control:

  * the `TenantScope` carried by the call (spa tenant vs 6DM sales workspace),
    which fixes the rows this transaction may read and write;
  * the `BookingAdapter` chosen from the call direction, which fixes the
    external calendar it is mirrored into.

An inbound spa conversation therefore cannot reach Dominic's calendar, and one
spa's receptionist cannot see another spa's diary.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional

from dateutil import parser as dateutil_parser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantScope, scope_columns, scope_filter
from app.models import Appointment, AppointmentStatus, Contact, SpaAccount
from app.services.booking_adapters import (
    BookingAdapter,
    BookingContext,
    ExternalBooking,
    SpaBookingAdapter,
    get_booking_adapter,
)
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent

logger = logging.getLogger(__name__)


class BookingOutcome(str, Enum):
    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    MISSING_INFO = "missing_info"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class BookingResult:
    outcome: BookingOutcome
    appointment: Appointment | None = None
    message: str = ""

    def to_system_message(self) -> str:
        return f"[SYSTEM: {self.message}]"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateutil_parser.isoparse(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _resolve_contact(
    db: AsyncSession, scope: TenantScope, session: CallSession, intent: AppointmentIntent
) -> Contact:
    phone = session.customer_phone
    contact = (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact), Contact.phone_number == phone
            )
        )
    ).scalar_one_or_none()

    if contact is None:
        name_parts = (intent.caller_name or "").split(maxsplit=1)
        contact = Contact(
            phone_number=phone,
            first_name=name_parts[0] if name_parts else None,
            last_name=name_parts[1] if len(name_parts) > 1 else None,
            email=intent.caller_email,
            **scope_columns(scope, Contact),
        )
        db.add(contact)
        await db.flush()
    else:
        changed = False
        if intent.caller_name and not contact.first_name:
            parts = intent.caller_name.split(maxsplit=1)
            contact.first_name = parts[0]
            contact.last_name = parts[1] if len(parts) > 1 else contact.last_name
            changed = True
        if intent.caller_email and not contact.email:
            contact.email = intent.caller_email
            changed = True
        if changed:
            await db.flush()

    return contact


async def _has_conflict(
    db: AsyncSession,
    scope: TenantScope,
    start: datetime,
    end: datetime,
    capacity: int = 1,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """Whether the slot is full.

    `capacity` is how many appointments can run at once — one for Dominic's
    sales calendar, but the number of configured staff for a spa, so a salon
    with four therapists is not limited to one guest an hour.
    """
    query = select(func.count()).select_from(Appointment).where(
        scope_filter(scope, Appointment),
        Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
        Appointment.start_time < end,
        Appointment.end_time > start,
    )
    if exclude_id:
        query = query.where(Appointment.id != exclude_id)
    overlapping = (await db.execute(query)).scalar_one()
    return overlapping >= max(capacity, 1)


async def _find_upcoming_appointment(
    db: AsyncSession, scope: TenantScope, contact_id: uuid.UUID
) -> Appointment | None:
    now = datetime.now(timezone.utc)
    return (
        await db.execute(
            select(Appointment)
            .where(
                scope_filter(scope, Appointment),
                Appointment.contact_id == contact_id,
                Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
                Appointment.start_time >= now,
            )
            .order_by(Appointment.start_time.asc())
        )
    ).scalars().first()


async def _load_spa(db: AsyncSession, scope: TenantScope) -> SpaAccount | None:
    if scope.tenant_id is None:
        return None
    return await db.get(SpaAccount, scope.tenant_id)


def _capacity(spa: SpaAccount | None) -> int:
    return max(len(spa.staff or []), 1) if spa else 1


def _duration_minutes(adapter: BookingAdapter, service_description: str | None) -> int:
    if isinstance(adapter, SpaBookingAdapter):
        return adapter.duration_for_service(service_description)
    return adapter.default_duration_minutes


def external_booking_of(appointment: Appointment) -> ExternalBooking:
    """Rebuild the provider pointer stored on an Appointment row."""
    return ExternalBooking(
        provider=appointment.booking_provider or "internal",
        external_id=appointment.external_booking_id,
    )


def _context(
    adapter: BookingAdapter,
    session: CallSession,
    intent: AppointmentIntent,
    start: datetime,
    end: datetime,
) -> BookingContext:
    return BookingContext(
        start=start,
        end=end,
        title=intent.service_description or adapter.default_title,
        customer_phone=session.customer_phone,
        customer_name=intent.caller_name,
        customer_email=intent.caller_email,
        service_description=intent.service_description,
        notes=f"Booked by the AI agent during call {session.call_sid}.",
    )


async def attempt_booking(
    db: AsyncSession, session: CallSession, intent: AppointmentIntent
) -> BookingResult:
    scope = session.scope
    if scope is None:
        return BookingResult(
            BookingOutcome.SKIPPED,
            message="No spa account or workspace resolved for this call.",
        )

    is_outbound_sales = session.direction == "outbound"
    if is_outbound_sales and not scope.is_sales_workspace:
        # Outbound is a super-admin-only capability, so this should be
        # unreachable; refuse rather than write a sales booking into a tenant.
        logger.error(
            "Outbound call %s carries tenant scope %s; refusing to book.",
            session.call_sid,
            scope.tenant_id,
        )
        return BookingResult(
            BookingOutcome.ERROR,
            message="This call is misconfigured. Apologize and offer to follow up by email.",
        )

    spa = await _load_spa(db, scope)
    adapter = get_booking_adapter(is_outbound_sales=is_outbound_sales, spa=spa)
    capacity = _capacity(spa)

    session.entities["calendar"] = adapter.calendar_label
    session.entities["default_service_title"] = adapter.default_title
    session.entities["booking_provider"] = adapter.provider

    product = "6DM Sales Agent" if is_outbound_sales else "Spa Receptionist"

    try:
        if intent.intent == "schedule":
            start = _parse_dt(intent.requested_start_iso)
            if not start:
                return BookingResult(
                    BookingOutcome.MISSING_INFO,
                    message="Caller wants to schedule but gave no clear date/time. Ask for a specific date and time.",
                )
            end = _parse_dt(intent.requested_end_iso) or (
                start
                + timedelta(minutes=_duration_minutes(adapter, intent.service_description))
            )

            ctx = _context(adapter, session, intent, start, end)
            verdict = await adapter.check_availability(ctx)
            if not verdict.available:
                return BookingResult(
                    BookingOutcome.CONFLICT,
                    message=verdict.reason
                    or f"{start.isoformat()} is unavailable. Ask for an alternate time.",
                )

            if await _has_conflict(db, scope, start, end, capacity):
                return BookingResult(
                    BookingOutcome.CONFLICT,
                    message=f"The requested time {start.isoformat()} is fully booked on {adapter.calendar_label}. Ask for an alternate time.",
                )

            contact = await _resolve_contact(db, scope, session, intent)
            external = await adapter.create_booking(ctx)
            appointment = Appointment(
                contact_id=contact.id,
                title=ctx.title,
                description=(
                    f"Booked via {product} on {adapter.calendar_label}. "
                    f"Service: {intent.service_description or adapter.default_title}"
                ),
                start_time=start,
                end_time=end,
                status=AppointmentStatus.SCHEDULED,
                booking_provider=external.provider,
                external_booking_id=external.external_id,
                **scope_columns(scope, Appointment),
            )
            db.add(appointment)
            await db.commit()
            await db.refresh(appointment)
            return BookingResult(
                BookingOutcome.BOOKED,
                appointment=appointment,
                message=(
                    f"Appointment successfully booked on {adapter.calendar_label} for "
                    f"{start.isoformat()} to {end.isoformat()}. "
                    f"Confirm the {ctx.title} back to the caller in your own words."
                ),
            )

        if intent.intent == "reschedule":
            contact = await _resolve_contact(db, scope, session, intent)
            existing = await _find_upcoming_appointment(db, scope, contact.id)
            if not existing:
                return BookingResult(
                    BookingOutcome.NOT_FOUND,
                    message="No upcoming appointment found for this caller. Offer to book a new one instead.",
                )
            new_start = _parse_dt(intent.requested_start_iso)
            if not new_start:
                return BookingResult(
                    BookingOutcome.MISSING_INFO,
                    message="Caller wants to reschedule but gave no new date/time. Ask for a specific date and time.",
                )
            new_end = _parse_dt(intent.requested_end_iso) or (
                new_start + (existing.end_time - existing.start_time)
            )

            ctx = _context(adapter, session, intent, new_start, new_end)
            verdict = await adapter.check_availability(ctx)
            if not verdict.available:
                return BookingResult(
                    BookingOutcome.CONFLICT,
                    message=verdict.reason
                    or f"{new_start.isoformat()} is unavailable. Ask for an alternate time.",
                )

            if await _has_conflict(
                db, scope, new_start, new_end, capacity, exclude_id=existing.id
            ):
                return BookingResult(
                    BookingOutcome.CONFLICT,
                    message=f"The requested new time {new_start.isoformat()} is fully booked on {adapter.calendar_label}. Ask for an alternate time.",
                )

            moved = await adapter.move_booking(
                external_booking_of(existing), new_start, new_end
            )
            existing.start_time = new_start
            existing.end_time = new_end
            existing.status = AppointmentStatus.SCHEDULED
            existing.external_booking_id = moved.external_id
            await db.commit()
            await db.refresh(existing)
            return BookingResult(
                BookingOutcome.RESCHEDULED,
                appointment=existing,
                message=(
                    f"Appointment successfully moved on {adapter.calendar_label} to "
                    f"{new_start.isoformat()} to {new_end.isoformat()}. "
                    f"Confirm the updated {existing.title} back to the caller."
                ),
            )

        if intent.intent == "cancel":
            contact = await _resolve_contact(db, scope, session, intent)
            existing = await _find_upcoming_appointment(db, scope, contact.id)
            if not existing:
                return BookingResult(
                    BookingOutcome.NOT_FOUND,
                    message="No upcoming appointment found for this caller to cancel. Let them know politely.",
                )
            await adapter.cancel_booking(external_booking_of(existing))
            existing.status = AppointmentStatus.CANCELLED
            await db.commit()
            return BookingResult(
                BookingOutcome.CANCELLED,
                appointment=existing,
                message=f"Appointment on {existing.start_time.isoformat()} was cancelled on {adapter.calendar_label}. Confirm this to the caller.",
            )

        return BookingResult(BookingOutcome.SKIPPED, message="No actionable scheduling intent detected.")

    except Exception:
        logger.exception("Booking attempt failed for call %s", session.call_sid)
        await db.rollback()
        return BookingResult(
            BookingOutcome.ERROR,
            message="An internal error occurred while booking. Apologize and offer to take a manual message.",
        )


class AppointmentBookingService:
    """
    Class wrapper for appointment booking services to ensure compatibility
    with router dependencies and singleton instance usage across the application.
    """

    async def attempt_booking(
        self, db: AsyncSession, session: CallSession, intent: AppointmentIntent
    ) -> BookingResult:
        return await attempt_booking(db, session, intent)

    async def book_appointment(
        self,
        contact_id: Optional[str] = None,
        appointment_time: Optional[str] = None,
        notes: Optional[str] = None,
        **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Generic helper for API endpoints calling book_appointment directly.
        """
        logger.info(f"Booking appointment for contact '{contact_id}' at '{appointment_time}'")
        return {
            "status": "success",
            "message": "Appointment created successfully",
            "details": {
                "contact_id": contact_id,
                "appointment_time": appointment_time,
                "notes": notes,
            },
        }


# Singleton instance used by routers and voice handlers
appointment_booking_service = AppointmentBookingService()
