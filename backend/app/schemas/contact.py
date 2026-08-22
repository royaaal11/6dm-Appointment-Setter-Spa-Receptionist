import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel


class ContactBase(BaseModel):
    first_name: str | None = Field(None, max_length=120)
    last_name: str | None = Field(None, max_length=120)
    phone_number: str = Field(..., min_length=7, max_length=32)
    email: EmailStr | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        cleaned = v.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not cleaned.startswith("+"):
            raise ValueError("phone_number must be in E.164 format (e.g. +15551234567)")
        return cleaned


class ContactCreate(ContactBase):
    pass


class ContactUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone_number: str | None = None
    email: EmailStr | None = None
    extra_metadata: dict[str, Any] | None = None


class ContactRead(ORMModel, ContactBase):
    id: uuid.UUID
    # Exactly one of these is set: `owner_id` for a 6DM sales lead, `tenant_id`
    # for a spa guest created by the receptionist.
    owner_id: uuid.UUID | None
    tenant_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # Read-only convenience field backed by Contact.full_name; the telephony
    # pipeline and the dashboard both address contacts by display name.
    full_name: str