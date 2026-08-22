"""Twilio REST client wrapper for outbound calls, executed off the event loop."""

import asyncio
import logging
from functools import partial

from twilio.rest import Client

from app.core.config import settings

logger = logging.getLogger(__name__)


class TwilioService:
    def __init__(self) -> None:
        self._client = Client(
            settings.TWILIO_ACCOUNT_SID,
            settings.TWILIO_AUTH_TOKEN,
        )

    async def create_outbound_call(self, to_number: str, from_number: str | None = None) -> str:
        """
        Place an outbound call.

        `from_number` lets a multi-tenant caller override the workspace default
        (User.twilio_phone_number); it falls back to the global configured number.
        """
        base = f"{settings.PUBLIC_BASE_URL}{settings.API_V1_PREFIX}/telephony"

        kwargs: dict[str, object] = {
            "to": to_number,
            "from_": from_number or settings.TWILIO_PHONE_NUMBER,
            "url": f"{base}/voice/outbound/answer",
            "method": "POST",
            "status_callback": f"{base}/voice/status",
            "status_callback_method": "POST",
            "status_callback_event": [
                "initiated",
                "ringing",
                "answered",
                "completed",
            ],
        }

        # Trial accounts reject these outright, failing the whole call request.
        if settings.TWILIO_ENABLE_RECORDING:
            kwargs |= {
                "record": True,
                "recording_status_callback": f"{base}/voice/recording",
                "recording_status_callback_method": "POST",
            }

        loop = asyncio.get_running_loop()

        call = await loop.run_in_executor(
            None,
            partial(self._client.calls.create, **kwargs),
        )

        logger.info(
            "Outbound call created: %s -> %s",
            call.sid,
            to_number,
        )

        return call.sid


# IMPORTANT: telephony.py imports this exact name
twilio_service = TwilioService()