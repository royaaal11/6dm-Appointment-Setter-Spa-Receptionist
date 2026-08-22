"""Google Calendar adapter.

This is the default target for both products: Dominic's calendar for 6DM sales
presentations, and the fallback service calendar for a spa that has not wired
up a vertical booking platform.

INTEGRATION SEAM — write-back is not live. Talking to Google needs
`google-api-python-client` plus a service-account credential, neither of which
this deployment has yet; see `_push_event`. Until then the adapter is honest
about it: bookings are persisted locally, `write_back_supported` is False, and
`Appointment.external_booking_id` stays NULL rather than holding a fake id.
"""
import logging

from app.core.config import settings
from app.services.booking_adapters.base import (
    BookingContext,
    ExternalBooking,
    LocalCalendarAdapter,
)

logger = logging.getLogger(__name__)


class GoogleCalendarAdapter(LocalCalendarAdapter):
    provider = "google_calendar"
    calendar_label = "the Google Calendar"

    #: Flip to True together with a real `_push_event` implementation.
    write_back_supported = False

    def __init__(
        self,
        calendar_id: str | None = None,
        calendar_label: str | None = None,
        default_title: str | None = None,
    ) -> None:
        super().__init__(calendar_label=calendar_label, default_title=default_title)
        self.calendar_id = calendar_id or settings.GOOGLE_CALENDAR_ID

    @property
    def is_configured(self) -> bool:
        return bool(self.calendar_id) and self.write_back_supported

    async def _push_event(self, ctx: BookingContext) -> str | None:
        """Create the Google Calendar event and return its id.

        Replace with a real `events().insert()` call once the service-account
        credential is provisioned. Returning None keeps the booking local-only.
        """
        return None

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        if not self.is_configured:
            logger.info(
                "Google Calendar write-back unavailable (calendar_id=%r, "
                "supported=%s); booking %s held locally only.",
                self.calendar_id,
                self.write_back_supported,
                ctx.start.isoformat(),
            )
            return ExternalBooking(provider=self.provider, external_id=None)

        return ExternalBooking(
            provider=self.provider, external_id=await self._push_event(ctx)
        )
