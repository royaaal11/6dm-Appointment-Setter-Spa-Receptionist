"""Booking strategy interface.

The local `Appointment` row is always written — it is what the dashboard, the
conflict check and the analytics endpoint read. An adapter sits *beside* that
write and owns two provider-specific concerns:

  1. whether a requested slot is bookable at all (business hours, provider-side
     staff availability);
  2. mirroring the booking into the external calendar the tenant actually runs
     their day from (Dominic's Google Calendar, a spa's Mindbody diary, ...).

Adapters therefore never touch the database. That keeps the transaction
boundary in one place — `app.services.appointment_booking_service` — and means
a provider outage degrades to "booked locally, not mirrored" instead of losing
the appointment.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


class BookingProviderError(RuntimeError):
    """Provider rejected or failed the request. Non-fatal: the local booking
    still stands and the failure is surfaced in logs, not to the caller."""


class ProviderNotConfigured(BookingProviderError):
    """The tenant selected this provider but supplied no usable credentials."""


@dataclass(frozen=True)
class BookingContext:
    """Everything a provider needs to create or move a booking."""

    start: datetime
    end: datetime
    title: str
    customer_phone: str
    customer_name: str | None = None
    customer_email: str | None = None
    service_description: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ExternalBooking:
    """Pointer to the mirrored booking in the provider's system."""

    provider: str
    external_id: str | None = None


@dataclass(frozen=True)
class AvailabilityVerdict:
    available: bool
    # Phrased for the voice agent to paraphrase, e.g. "the spa is closed then".
    reason: str | None = None

    @classmethod
    def ok(cls) -> "AvailabilityVerdict":
        return cls(available=True)

    @classmethod
    def no(cls, reason: str) -> "AvailabilityVerdict":
        return cls(available=False, reason=reason)


class BookingAdapter(ABC):
    """Base strategy. Concrete adapters override only what they support."""

    #: Stored on Appointment.booking_provider.
    provider: str = "internal"
    #: Human label the voice agent uses, e.g. "Dominic's Google Calendar".
    calendar_label: str = "the calendar"
    #: Fallback appointment title when the caller never named a service.
    default_title: str = "Appointment"
    #: Used when the caller gives a start time but no duration.
    default_duration_minutes: int = 30

    async def check_availability(self, ctx: BookingContext) -> AvailabilityVerdict:
        """Provider-side availability. Local double-booking is checked
        separately against the `appointments` table."""
        return AvailabilityVerdict.ok()

    @abstractmethod
    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        """Mirror a new booking. Raise `BookingProviderError` on failure."""

    async def move_booking(
        self, booking: ExternalBooking, start: datetime, end: datetime
    ) -> ExternalBooking:
        """Reschedule a mirrored booking. Defaults to a no-op for providers
        with no write-back."""
        return booking

    async def cancel_booking(self, booking: ExternalBooking) -> None:
        """Cancel a mirrored booking. Defaults to a no-op."""
        return None


class LocalCalendarAdapter(BookingAdapter):
    """The always-available fallback: the booking lives only in our own
    database. Used when a tenant has picked a provider we cannot reach, so a
    misconfigured integration never costs the spa a booking."""

    provider = "internal"
    calendar_label = "the internal calendar"

    def __init__(
        self,
        calendar_label: str | None = None,
        default_title: str | None = None,
    ) -> None:
        if calendar_label:
            self.calendar_label = calendar_label
        if default_title:
            self.default_title = default_title

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        return ExternalBooking(provider=self.provider, external_id=None)
