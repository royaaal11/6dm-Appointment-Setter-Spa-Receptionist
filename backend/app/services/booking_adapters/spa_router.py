"""Spa Receptionist booking strategy.

`SpaBookingAdapter` is a router, not an integration: it owns the rules that
apply to *every* spa (business hours, service duration defaults) and delegates
the actual write to whichever provider the tenant configured — falling back to
the tenant's Google/internal calendar when that provider is unimplemented or
missing credentials.

That fallback is what makes onboarding config-only. A new `SpaAccount` row with
`booking_provider = "mindbody"` and an empty `booking_config` still answers
calls and books guests today; filling in the credentials later upgrades it in
place with no deploy.
"""
import logging

from app.core.config import settings
from app.models.spa_account import BookingProvider, SpaAccount
from app.services.booking_adapters.base import (
    AvailabilityVerdict,
    BookingAdapter,
    BookingContext,
    BookingProviderError,
    ExternalBooking,
)
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.booking_adapters.providers import VERTICAL_PROVIDERS
from app.services.business_hours import is_open_between

logger = logging.getLogger(__name__)


class SpaBookingAdapter(BookingAdapter):
    def __init__(self, spa: SpaAccount) -> None:
        self.spa = spa
        self.delegate = self._select_delegate(spa)
        self.provider = self.delegate.provider
        self.calendar_label = f"{spa.name}'s service calendar"
        self.default_title = "Spa Service Appointment"
        self.default_duration_minutes = self._default_duration(spa)

    # -- wiring ----------------------------------------------------------- #
    @staticmethod
    def _select_delegate(spa: SpaAccount) -> BookingAdapter:
        fallback = GoogleCalendarAdapter(
            calendar_id=(spa.booking_config or {}).get("google_calendar_id"),
            calendar_label=f"{spa.name}'s service calendar",
            default_title="Spa Service Appointment",
        )

        if spa.booking_provider == BookingProvider.GOOGLE_CALENDAR:
            return fallback

        provider_cls = VERTICAL_PROVIDERS.get(spa.booking_provider)
        if provider_cls is None:
            logger.warning(
                "Spa %s requests unknown provider %r; using the calendar fallback.",
                spa.name,
                spa.booking_provider,
            )
            return fallback

        if not provider_cls.implemented:
            logger.warning(
                "Spa %s is configured for %s, which has no live client yet; "
                "bookings will be held on the calendar fallback.",
                spa.name,
                provider_cls.provider,
            )
            return fallback

        missing = provider_cls.missing_config_keys(spa.booking_config)
        if missing:
            logger.warning(
                "Spa %s is configured for %s but booking_config is missing %s; "
                "using the calendar fallback.",
                spa.name,
                provider_cls.provider,
                missing,
            )
            return fallback

        return provider_cls(spa.name, spa.booking_config)

    @staticmethod
    def _default_duration(spa: SpaAccount) -> int:
        for service in spa.services or []:
            duration = service.get("duration_minutes")
            if isinstance(duration, int) and duration > 0:
                return duration
        return settings.DEFAULT_APPOINTMENT_DURATION_MINUTES

    def duration_for_service(self, service_description: str | None) -> int:
        """Match the caller's words against the tenant's configured services so
        "a deep tissue massage" books 60 minutes rather than the global default."""
        if not service_description:
            return self.default_duration_minutes

        wanted = service_description.casefold()
        for service in self.spa.services or []:
            name = str(service.get("name", "")).casefold()
            duration = service.get("duration_minutes")
            if name and isinstance(duration, int) and duration > 0:
                if name in wanted or wanted in name:
                    return duration
        return self.default_duration_minutes

    # -- BookingAdapter ---------------------------------------------------- #
    async def check_availability(self, ctx: BookingContext) -> AvailabilityVerdict:
        if not is_open_between(
            self.spa.business_hours, self.spa.timezone, ctx.start, ctx.end
        ):
            return AvailabilityVerdict.no(
                f"{self.spa.name} is closed at that time. Offer a slot inside "
                "the spa's opening hours instead."
            )
        return await self.delegate.check_availability(ctx)

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        try:
            return await self.delegate.create_booking(ctx)
        except BookingProviderError:
            # The local Appointment row is already the source of truth; a
            # provider failure must not cost the spa the booking.
            logger.exception(
                "Provider %s failed to mirror a booking for spa %s",
                self.delegate.provider,
                self.spa.name,
            )
            return ExternalBooking(provider=self.delegate.provider, external_id=None)

    async def move_booking(
        self, booking: ExternalBooking, start, end
    ) -> ExternalBooking:
        try:
            return await self.delegate.move_booking(booking, start, end)
        except BookingProviderError:
            logger.exception(
                "Provider %s failed to move a booking for spa %s",
                self.delegate.provider,
                self.spa.name,
            )
            return booking

    async def cancel_booking(self, booking: ExternalBooking) -> None:
        try:
            await self.delegate.cancel_booking(booking)
        except BookingProviderError:
            logger.exception(
                "Provider %s failed to cancel a booking for spa %s",
                self.delegate.provider,
                self.spa.name,
            )
