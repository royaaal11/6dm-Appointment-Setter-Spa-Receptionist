from app.services.booking_adapters.providers.base import VerticalProviderAdapter


class SquareAdapter(VerticalProviderAdapter):
    """Square Bookings API (`POST /v2/bookings`)."""

    provider = "square"
    required_config_keys = ("access_token", "location_id")
    api_docs = "https://developer.squareup.com/reference/square/bookings-api"
    implemented = False
