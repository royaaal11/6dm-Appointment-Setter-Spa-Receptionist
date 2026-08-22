"""Spa tenant provisioning and per-spa receptionist configuration.

Onboarding is one POST here: name, Twilio number, Grok prompt, services, staff,
hours and booking provider. The inbound webhook resolves everything else from
that row at call time, so no deploy is involved.

Who can do what:
  * super_admin — create, list, read and edit any spa; hard-delete is not
    offered (deactivate instead, so call history survives).
  * spa_admin   — read and edit their own spa's receptionist settings.
  * spa_staff   — read their own spa only.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_super_admin
from app.models import SpaAccount, User, UserRole
from app.schemas import (
    Page,
    SpaAccountCreate,
    SpaAccountRead,
    SpaAccountSummary,
    SpaAccountUpdate,
)
from app.services.booking_adapters.providers import VERTICAL_PROVIDERS

router = APIRouter(prefix="/spa-accounts", tags=["spa-accounts"])

# Fields a spa_admin may not change on their own tenant. Moving your own
# inbound number would let you hijack another spa's routing.
TENANT_LOCKED_FIELDS = {"twilio_phone_number", "is_active"}


def _to_read(spa: SpaAccount) -> SpaAccountRead:
    """`booking_config` holds provider secrets, so the read schema reports only
    whether the configured provider is actually usable."""
    provider_cls = VERTICAL_PROVIDERS.get(spa.booking_provider)
    configured = (
        provider_cls.is_usable(spa.booking_config)
        if provider_cls is not None
        # google_calendar needs no per-tenant secret to fall back cleanly.
        else True
    )
    return SpaAccountRead.model_validate(spa).model_copy(
        update={"booking_provider_configured": configured}
    )


async def _assert_number_free(
    db: AsyncSession, number: str | None, exclude_id: uuid.UUID | None = None
) -> None:
    if not number:
        return
    query = select(SpaAccount.id).where(SpaAccount.twilio_phone_number == number)
    if exclude_id:
        query = query.where(SpaAccount.id != exclude_id)
    if (await db.execute(query)).scalar_one_or_none():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{number} is already routed to another spa account.",
        )


async def _load_visible_spa(
    db: AsyncSession, spa_id: uuid.UUID, user: User
) -> SpaAccount:
    if user.role != UserRole.SUPER_ADMIN and spa_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    spa = await db.get(SpaAccount, spa_id)
    if spa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    return spa


@router.post(
    "",
    response_model=SpaAccountRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_super_admin)],
)
async def create_spa_account(
    payload: SpaAccountCreate,
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    await _assert_number_free(db, payload.twilio_phone_number)

    # Python mode, not JSON: nested models still flatten to plain dicts for the
    # JSONB columns, but `booking_provider` stays an enum member, which is what
    # the SQLAlchemy Enum column expects.
    spa = SpaAccount(**payload.model_dump())
    db.add(spa)
    await db.commit()
    await db.refresh(spa)
    return _to_read(spa)


@router.get(
    "",
    response_model=Page[SpaAccountSummary],
    dependencies=[Depends(get_super_admin)],
)
async def list_spa_accounts(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
) -> Page[SpaAccountSummary]:
    """Backs the super admin's tenant switcher."""
    query = select(SpaAccount)
    if not include_inactive:
        query = query.where(SpaAccount.is_active.is_(True))

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.order_by(SpaAccount.name.asc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return Page(
        items=[SpaAccountSummary.model_validate(r) for r in rows],
        total=total, page=page, size=size,
    )


@router.get("/me", response_model=SpaAccountRead)
async def get_my_spa_account(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    """The signed-in spa user's own tenant — what the spa dashboard loads."""
    if current_user.tenant_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This account is not attached to a spa. Super admins should pick "
            "one from the tenant switcher instead.",
        )
    spa = await db.get(SpaAccount, current_user.tenant_id)
    if spa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    return _to_read(spa)


@router.get("/{spa_id}", response_model=SpaAccountRead)
async def get_spa_account(
    spa_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    return _to_read(await _load_visible_spa(db, spa_id, current_user))


@router.patch("/{spa_id}", response_model=SpaAccountRead)
async def update_spa_account(
    spa_id: uuid.UUID,
    payload: SpaAccountUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    if current_user.role == UserRole.SPA_STAFF:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "spa_staff accounts have read-only access to receptionist settings.",
        )

    spa = await _load_visible_spa(db, spa_id, current_user)
    updates = payload.model_dump(exclude_unset=True)

    if current_user.role != UserRole.SUPER_ADMIN:
        locked = TENANT_LOCKED_FIELDS & updates.keys()
        if locked:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Only 6DM can change: {', '.join(sorted(locked))}.",
            )

    if "twilio_phone_number" in updates:
        await _assert_number_free(db, updates["twilio_phone_number"], exclude_id=spa.id)

    for field, value in updates.items():
        setattr(spa, field, value)
    await db.commit()
    await db.refresh(spa)
    return _to_read(spa)
