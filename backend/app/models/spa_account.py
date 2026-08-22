import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Enum, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class BookingProvider(str, enum.Enum):
    """Calendar/booking system a spa's appointments are mirrored into."""

    GOOGLE_CALENDAR = "google_calendar"
    MINDBODY = "mindbody"
    MANGOMINT = "mangomint"
    SQUARE = "square"
    VAGARO = "vagaro"
    ZENOTI = "zenoti"


class SpaAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single spa tenant.

    Onboarding a new spa is purely a matter of inserting one of these rows: the
    inbound Twilio webhook resolves the tenant from `twilio_phone_number`, the
    receptionist persona comes from `grok_system_prompt` + `services`/`staff`/
    `business_hours`, and bookings route through `booking_provider`. No code
    changes are required per spa.
    """

    __tablename__ = "spa_accounts"

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The dialed number that identifies this tenant on inbound calls.
    twilio_phone_number: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True
    )

    # Tenant-specific receptionist persona. Prepended to the shared voice rules.
    grok_system_prompt: Mapped[str | None] = mapped_column(Text)

    # {"mon": [{"open": "09:00", "close": "18:00"}], ..., "sun": []}
    business_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # [{"name": "Deep tissue massage", "duration_minutes": 60, "price": "120"}]
    services: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # [{"name": "Riley", "role": "Massage therapist"}]
    staff: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )

    booking_provider: Mapped[BookingProvider] = mapped_column(
        Enum(
            BookingProvider,
            name="booking_provider",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=BookingProvider.GOOGLE_CALENDAR,
        server_default=BookingProvider.GOOGLE_CALENDAR.value,
    )
    # Provider credentials / location ids. Shape is provider-specific; see
    # app/services/booking_adapters/providers/.
    booking_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # Twilio <Say> voice override for this tenant.
    twiml_voice: Mapped[str | None] = mapped_column(String(64))

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # --- Relationships ---
    users: Mapped[list["User"]] = relationship(back_populates="tenant")

    def __repr__(self) -> str:
        return f"<SpaAccount id={self.id} name={self.name!r}>"
