"""
Redis-backed session memory for active calls.
"""
import json
import logging
import time
import uuid
from typing import Any, Literal

from redis.asyncio import Redis

from app.core.config import settings
from app.core.tenancy import TenantScope

logger = logging.getLogger(__name__)

CALL_STATE_PREFIX = "call:state:"
ACTIVE_CALLS_SET = "call:active"

Role = Literal["system", "user", "assistant"]


class CallSession:
    def __init__(
        self,
        call_sid: str,
        direction: str,
        from_number: str,
        to_number: str,
        history: list[dict[str, str]] | None = None,
        entities: dict[str, Any] | None = None,
        phase: str = "greeting",
        call_objective: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        business_name: str | None = None,
        tenant_prompt: str | None = None,
        voice: str | None = None,
        created_at: float | None = None,
    ) -> None:
        self.call_sid = call_sid
        self.direction = direction
        self.from_number = from_number
        self.to_number = to_number
        self.history = history or []
        self.entities = entities or {}
        self.phase = phase
        self.call_objective = call_objective
        self.user_id = user_id  # 6DM sales workspace owner, for outbound calls
        # Spa tenant resolved from the dialed number on inbound calls. Exactly
        # one of user_id / tenant_id identifies the scope every record this call
        # creates is written into.
        self.tenant_id = tenant_id
        self.business_name = business_name
        # Tenant-specific receptionist persona, rendered once at call setup from
        # SpaAccount (prompt + services + staff + hours) so every turn is
        # answered with the same configuration even if the row changes mid-call.
        self.tenant_prompt = tenant_prompt
        self.voice = voice or settings.DEFAULT_TWIML_VOICE  # Twilio <Say> voice for this call
        self.created_at = created_at or time.time()

    def add_turn(self, role: Role, content: str) -> None:
        self.history.append({"role": role, "content": content})

    @property
    def scope(self) -> TenantScope | None:
        """Where records created during this call belong.

        None means the call could not be attributed to a spa or a workspace —
        an inbound call to an unclaimed number — in which case nothing is
        persisted rather than being written somewhere arbitrary.
        """
        if self.tenant_id:
            return TenantScope.for_tenant(uuid.UUID(self.tenant_id))
        if self.user_id:
            return TenantScope.for_sales_workspace(uuid.UUID(self.user_id))
        return None

    @property
    def customer_phone(self) -> str:
        """The human caller/callee's number, regardless of call direction."""
        return self.from_number if self.direction == "inbound" else self.to_number

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "direction": self.direction,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "history": self.history,
            "entities": self.entities,
            "phase": self.phase,
            "call_objective": self.call_objective,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "business_name": self.business_name,
            "tenant_prompt": self.tenant_prompt,
            "voice": self.voice,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallSession":
        # Tolerate payloads written by an older release still sitting in Redis:
        # unknown keys are dropped rather than blowing up a live call.
        known = cls.__init__.__code__.co_varnames[1 : cls.__init__.__code__.co_argcount]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def transcript_text(self) -> str:
        lines = []
        for turn in self.history:
            if turn["role"] == "system":
                continue
            speaker = "Agent" if turn["role"] == "assistant" else "Caller"
            lines.append(f"{speaker}: {turn['content']}")
        return "\n".join(lines)


class CallStateStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._ttl = settings.CALL_STATE_TTL_SECONDS

    def _key(self, call_sid: str) -> str:
        return f"{CALL_STATE_PREFIX}{call_sid}"

    async def create(self, session: CallSession) -> CallSession:
        await self.save(session)
        await self._redis.sadd(ACTIVE_CALLS_SET, session.call_sid)
        logger.info("Call session created: %s", session.call_sid)
        return session

    async def get(self, call_sid: str) -> CallSession | None:
        raw = await self._redis.get(self._key(call_sid))
        if raw is None:
            return None
        return CallSession.from_dict(json.loads(raw))

    async def save(self, session: CallSession) -> None:
        await self._redis.set(
            self._key(session.call_sid), json.dumps(session.to_dict()), ex=self._ttl
        )

    async def append_turn(self, call_sid: str, role: Role, content: str) -> CallSession | None:
        session = await self.get(call_sid)
        if session is None:
            return None
        session.add_turn(role, content)
        await self.save(session)
        return session

    async def set_entities(self, call_sid: str, entities: dict[str, Any]) -> None:
        session = await self.get(call_sid)
        if session is None:
            return
        session.entities.update(entities)
        await self.save(session)

    async def end(self, call_sid: str) -> CallSession | None:
        session = await self.get(call_sid)
        await self._redis.srem(ACTIVE_CALLS_SET, call_sid)
        if session:
            await self._redis.expire(self._key(call_sid), 300)
        logger.info("Call session ended: %s", call_sid)
        return session

    async def active_call_sids(self) -> set[str]:
        members = await self._redis.smembers(ACTIVE_CALLS_SET)
        return {m.decode() if isinstance(m, bytes) else m for m in members}