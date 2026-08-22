"""Booking strategy selection.

The invariant worth protecting: which calendar a booking lands on is decided by
the call direction and the tenant row, never by anything a caller says.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import BookingProvider
from app.services.booking_adapters import (
    BookingContext,
    GoogleCalendarAdapter,
    LocalCalendarAdapter,
    OutboundSalesAdapter,
    SpaBookingAdapter,
    get_booking_adapter,
)
from app.services.booking_adapters.providers import MindbodyAdapter
from tests.conftest import make_spa

# A Wednesday, 14:00-15:00 UTC.
START = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def _ctx(start: datetime = START, end: datetime = END) -> BookingContext:
    return BookingContext(
        start=start, end=end, title="Deep tissue massage", customer_phone="+15550001"
    )


def test_outbound_sales_always_books_dominics_calendar():
    adapter = get_booking_adapter(is_outbound_sales=True, spa=None)

    assert isinstance(adapter, OutboundSalesAdapter)
    assert adapter.calendar_label == "Dominic's Google Calendar"
    assert adapter.default_title == "6DM Sales Presentation"


def test_a_spa_can_never_be_routed_to_the_sales_calendar():
    """Even if a spa row somehow named the sales adapter, the factory keys off
    the call direction, so an inbound conversation cannot reach it."""
    spa = make_spa(booking_provider=BookingProvider.GOOGLE_CALENDAR)

    adapter = get_booking_adapter(is_outbound_sales=False, spa=spa)

    assert isinstance(adapter, SpaBookingAdapter)
    assert not isinstance(adapter, OutboundSalesAdapter)
    assert adapter.calendar_label == "Solace Spa's service calendar"


def test_unclaimed_inbound_number_falls_back_to_a_local_calendar():
    adapter = get_booking_adapter(is_outbound_sales=False, spa=None)
    assert isinstance(adapter, LocalCalendarAdapter)


@pytest.mark.parametrize(
    "provider",
    [
        BookingProvider.MINDBODY,
        BookingProvider.MANGOMINT,
        BookingProvider.SQUARE,
        BookingProvider.VAGARO,
        BookingProvider.ZENOTI,
    ],
)
def test_unimplemented_provider_degrades_to_the_calendar_fallback(provider):
    """Requirement 4b: selecting a vertical platform must never break a spa's
    ability to take bookings today."""
    spa = make_spa(booking_provider=provider)

    adapter = SpaBookingAdapter(spa)

    assert isinstance(adapter.delegate, GoogleCalendarAdapter)


def test_partially_configured_provider_also_falls_back(monkeypatch):
    """A live provider with half its credentials is worse than the fallback:
    it would fail mid-call."""
    monkeypatch.setattr(MindbodyAdapter, "implemented", True)
    spa = make_spa(
        booking_provider=BookingProvider.MINDBODY,
        booking_config={"site_id": "123", "api_key": "k"},  # missing source_*
    )

    assert isinstance(SpaBookingAdapter(spa).delegate, GoogleCalendarAdapter)


def test_fully_configured_provider_is_used(monkeypatch):
    monkeypatch.setattr(MindbodyAdapter, "implemented", True)
    spa = make_spa(
        booking_provider=BookingProvider.MINDBODY,
        booking_config={
            "site_id": "123",
            "api_key": "k",
            "source_name": "n",
            "source_password": "p",
        },
    )

    assert isinstance(SpaBookingAdapter(spa).delegate, MindbodyAdapter)


@pytest.mark.asyncio
async def test_spa_refuses_bookings_outside_business_hours():
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "12:00"}]},
        timezone="UTC",
    )

    verdict = await SpaBookingAdapter(spa).check_availability(_ctx())

    assert not verdict.available
    assert "closed" in verdict.reason


@pytest.mark.asyncio
async def test_spa_accepts_bookings_inside_business_hours():
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "18:00"}]},
        timezone="UTC",
    )

    assert (await SpaBookingAdapter(spa).check_availability(_ctx())).available


@pytest.mark.asyncio
async def test_business_hours_are_read_in_the_tenants_timezone():
    """14:00 UTC is 07:00 in Los Angeles — before a 09:00 opening."""
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "18:00"}]},
        timezone="America/Los_Angeles",
    )

    assert not (await SpaBookingAdapter(spa).check_availability(_ctx())).available


def test_service_duration_comes_from_the_tenants_menu():
    spa = make_spa(
        services=[
            {"name": "Signature facial", "duration_minutes": 75},
            {"name": "Deep tissue massage", "duration_minutes": 60},
        ]
    )
    adapter = SpaBookingAdapter(spa)

    assert adapter.duration_for_service("a deep tissue massage please") == 60
    assert adapter.duration_for_service("Signature facial") == 75
    # Unrecognised service falls back to the spa's first configured duration.
    assert adapter.duration_for_service("hot stone") == 75
    assert adapter.duration_for_service(None) == 75


@pytest.mark.asyncio
async def test_a_provider_failure_does_not_lose_the_booking(monkeypatch):
    from app.services.booking_adapters.base import BookingProviderError

    spa = make_spa(booking_provider=BookingProvider.GOOGLE_CALENDAR)
    adapter = SpaBookingAdapter(spa)

    async def _boom(_ctx):
        raise BookingProviderError("provider down")

    monkeypatch.setattr(adapter.delegate, "create_booking", _boom)

    result = await adapter.create_booking(_ctx())

    assert result.external_id is None  # not mirrored, but not raised either
