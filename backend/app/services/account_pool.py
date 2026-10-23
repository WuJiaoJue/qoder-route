import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.core.database import async_session
from app.models.account import Account
from app.models.pool_counter import PoolCounter, CREDITS_SPENT_KEY
from app.services import settings_service
from app.core.config import settings
from app.services import quota_service, logbus

logger = logging.getLogger("qoderroute.pool")

# When an account is down to its last credits, only one in-flight request may
# ride it. Extra concurrent starts spill to the next account (if any) so a
# burst does not all die on the same almost-exhausted PAT.
_NEAR_EMPTY_CREDITS = 15.0


def _utcnow() -> datetime:
    """Naive UTC datetime — matches what SQLite stores/returns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _below_reserve(acc: Account, free_model: bool) -> bool:
    """Would spending on this account eat into its reserved credits?

    The reserve is per-account (``Account.reserve_credits``, 0 = off) because
    plans differ — a 3,000-credit plan and a free-tier row cannot share one
    number.

    Applies only to PAID models on PAID accounts:
      * a free model (Efficient/Lite) costs nothing, so the reserve is
        irrelevant — and gating it would remove the last fallback;
      * a free-tier row reports ``quota_remaining = 0/0``, which says nothing
        about its real (resource-pack) balance, so a numeric reserve must not
        silently exclude it.
    """
    if free_model or not acc.is_paid:
        return False
    reserve = acc.reserve_credits
    if not isinstance(reserve, (int, float)) or reserve <= 0:
        return False
    remaining = acc.quota_remaining
    if not isinstance(remaining, (int, float)):
        return False
    return float(remaining) <= float(reserve)


def _row_is_routable(
    acc: Account,
    now: Optional[datetime] = None,
    *,
    free_model: bool = False,
) -> bool:
    """Fresh check: still active, available, and not parked/exhausted.

    ``free_model`` bypasses the credit gate for models free inside a paid plan
    (e.g. Efficient / Lite): a paid account with 0 remaining credits may still
    serve them. Free-tier accounts (``is_paid == False``) are exempt from the
    local credit gate entirely — the quota API cannot see personal resource
    packs, so upstream errors are the real feedback loop.
    """
    if now is None:
        now = _utcnow()
    # Operator switch wins over everything else: a disabled account is never
    # routable, regardless of quota, cooldown or free-model exemptions.
    if not acc.enabled:
        return False
    # ``is_available`` is informational (parked when exhausted). A paid
    # account serving a free model may be parked yet still routable.
    if not acc.is_active:
        return False
    if not acc.is_available and not (free_model and acc.is_paid):
        return False
    # Exhausted parked accounts stay parked for paid models; a free model on a
    # paid account is allowed through (upstream charges 0 credits). Free-tier
    # parked accounts stay parked (upstream quota error → park → excluded).
    if acc.is_quota_exceeded and not (free_model and acc.is_paid):
        return False
    if acc.cooldown_until is not None and acc.cooldown_until > now:
        return False
    if acc.consecutive_failures >= settings.max_consecutive_failures:
        return False
    # Free-tier accounts: the quota API cannot see personal resource packs,
    # so the local credit gate does not apply to them; upstream errors are
    # the real feedback loop (quota error → park → excluded until refresh).
    if (
        acc.quota_remaining is not None
        and acc.quota_remaining <= 0
        and not ((free_model and acc.is_paid) or not acc.is_paid)
    ):
        return False
    # Operator reserve: stop spending a paid account before it is drained.
    if _below_reserve(acc, free_model):
        return False
    return True


class AccountPool:
    def __init__(self):
        self._last_refresh: datetime = datetime.min
        self._refresh_lock = asyncio.Lock()
        self._in_flight_lock = asyncio.Lock()
        self._in_flight: dict[int, int] = {}

    async def begin_request(self, account_id: int, *, free_model: bool = False) -> bool:
        """Claim a specific account for one in-flight request.

        Routing should use ``get_next_account``, which picks and leases
        atomically. This keeps a DB routability check for callers that
        already know the id.
        """
        async with async_session() as session:
            acc = await session.get(Account, account_id)
            if not acc or not _row_is_routable(acc, free_model=free_model):
                return False
        async with self._in_flight_lock:
            self._in_flight[account_id] = self._in_flight.get(account_id, 0) + 1
        return True

    async def end_request(self, account_id: int) -> None:
        async with self._in_flight_lock:
            n = self._in_flight.get(account_id, 0) - 1
            if n <= 0:
                self._in_flight.pop(account_id, None)
            else:
                self._in_flight[account_id] = n

    async def _refresh(self):
        """Recalculate is_available flags based on cooldown state.

        A served cooldown wipes the failure counter — the account gets a
        fresh chance instead of being parked forever at failures >= max.
        """
        async with async_session() as session:
            stmt = (
                select(Account)
                .where(Account.is_active == True)
                .order_by(Account.priority.desc(), Account.id.asc())
            )
            result = await session.execute(stmt)
            accounts = list(result.scalars().all())

            now = _utcnow()
            changed = False
            for acc in accounts:
                in_cooldown = acc.cooldown_until is not None and acc.cooldown_until > now
                if acc.cooldown_until is not None and not in_cooldown:
                    acc.cooldown_until = None
                    if acc.consecutive_failures:
                        acc.consecutive_failures = 0
                    changed = True
                should_be = acc.enabled and not in_cooldown and not acc.is_quota_exceeded
                if acc.is_available != should_be:
                    acc.is_available = should_be
                    changed = True

            if changed:
                await session.commit()

            self._last_refresh = _utcnow()

    def _order_candidates(self, accounts: list[Account]) -> list[Account]:
        """Order routable candidates by the configured routing strategy.

        Ordering happens here rather than in the SQL ``ORDER BY`` because the
        `spread` strategy ranks on live in-flight counts, which exist only in
        this process's memory. The SQL order (`priority DESC, id ASC`) is kept
        anyway: it is the deterministic tie-break for every strategy, so the
        query stays the single source of truth for the *filter*.

        `priority` is honoured first under every strategy — it is an explicit
        operator override, so a higher-priority account must win regardless of
        load. Strategies only reorder *within* equal priority.
        """
        strategy = str(settings_service.get("routing_strategy") or "fill_first")

        if strategy == "spread":
            # Fewest in-flight first, then the most remaining credit (so a
            # burst lands on the healthiest spare), then the SQL tie-break.
            return sorted(
                accounts,
                key=lambda a: (
                    -a.priority,
                    self._in_flight.get(a.id, 0),
                    -(a.quota_remaining if isinstance(a.quota_remaining, (int, float)) else 0.0),
                    a.id,
                ),
            )

        if strategy == "least_used":
            # Fewest lifetime requests first, so wear is distributed evenly.
            return sorted(
                accounts,
                key=lambda a: (-a.priority, a.total_requests or 0, a.id),
            )

        # fill_first (default): keep the SQL order `priority DESC, id ASC`.
        return accounts

    async def get_next_account(
        self,
        session: AsyncSession,
        exclude_ids: Optional[set[int]] = None,
        *,
        free_model: bool = False,
    ) -> Optional[Account]:
        """Pick and lease the next account for a request.

        Default strategy is fill-first (the first available account WITH quota
        wins); `routing_strategy` can switch this to `spread` or `least_used`,
        which reorder candidates within equal priority — see
        `_order_candidates`.

        Near exhaustion, only one in-flight request may stay on the dying
        account; additional concurrent starts spill to the next PAT so a
        burst does not all fail together.

        ``free_model`` (a model free inside a paid plan, e.g. Efficient/Lite)
        widens the credit gate: paid accounts with 0 credits remain routable
        for it. Exhausted free-tier accounts are still excluded.

        The returned account is already leased (``_in_flight`` incremented).
        The caller must ``end_request`` or hand the lease to the SSE generator.
        """
        await self._refresh_if_stale()

        now = _utcnow()
        if free_model:
            # Paid accounts with 0 remaining credits are routable for free
            # models (Efficient/Lite) even though ``is_available`` is False
            # (parked); the fine-grained gate lives in _row_is_routable.
            # Free-tier accounts are also routable when available and not
            # quota-exceeded (parked free accounts stay excluded).
            stmt = (
                select(Account)
                .where(Account.is_active == True, Account.enabled == True)
                .where(
                    (Account.is_paid == True)
                    | (
                        (Account.is_paid == False)
                        & (Account.is_available == True)
                        & (Account.is_quota_exceeded == False)
                    )
                )
                .where(
                    (Account.cooldown_until == None)
                    | (Account.cooldown_until < now)
                )
                .where(Account.consecutive_failures < settings.max_consecutive_failures)
                .order_by(Account.priority.desc(), Account.id.asc())
            )
        else:
            stmt = (
                select(Account)
                .where(Account.is_active == True, Account.enabled == True)
                .where(Account.is_available == True)
                .where(Account.is_quota_exceeded == False)
                .where(
                    (Account.quota_remaining == None)
                    | (Account.quota_remaining > 0)
                    # Free-tier rows carry a 0/0 quota that hides personal
                    # resource-pack credits; let upstream verdicts gate them.
                    | (Account.is_paid == False)
                )
                .where(
                    (Account.cooldown_until == None)
                    | (Account.cooldown_until < now)
                )
                .where(Account.consecutive_failures < settings.max_consecutive_failures)
                .order_by(Account.priority.desc(), Account.id.asc())
            )
            # Per-account reserve (paid models only). Applied in SQL as well as
            # in `_row_is_routable` so a reserved account cannot win ordering
            # and then be skipped. Always applied — the comparison against each
            # row's own `reserve_credits` is a no-op when that value is 0.
            # Free-tier rows are exempt: their 0/0 quota says nothing about
            # real resource-pack credits.
            stmt = stmt.where(
                (Account.is_paid == False)
                | (Account.reserve_credits <= 0)
                | (Account.quota_remaining == None)
                | (Account.quota_remaining > Account.reserve_credits)
            )
        result = await session.execute(stmt)
        available = list(result.scalars().all())

        if exclude_ids:
            available = [a for a in available if a.id not in exclude_ids]

        if not available:
            return None

        available = self._order_candidates(available)
        # Pick + lease under the same lock so two concurrent starts cannot
        # both stick to a near-empty PAT while a healthy spare sits idle.
        # Caller must ``end_request`` (or transfer the lease to the SSE).
        async with self._in_flight_lock:
            for idx, acc in enumerate(available):
                remaining = acc.quota_remaining
                # Free-model routing: a paid account with 0 credits is solidly
                # usable, so skip the near-empty spill heuristic entirely.
                if not free_model:
                    near_empty = (
                        isinstance(remaining, (int, float))
                        and remaining <= _NEAR_EMPTY_CREDITS
                    )
                    has_fallback = idx + 1 < len(available)
                    if near_empty and self._in_flight.get(acc.id, 0) > 0 and has_fallback:
                        continue
                fresh = await session.get(Account, acc.id)
                if not fresh or not _row_is_routable(fresh, free_model=free_model):
                    continue
                self._in_flight[fresh.id] = self._in_flight.get(fresh.id, 0) + 1
                return fresh
            return None

    async def refresh_quota(self, account_id: int) -> Optional[dict]:
        """Fetch live plan+quota from Qoder and persist on the account row."""
        async with async_session() as session:
            acc = await session.get(Account, account_id)
            if not acc:
                return None

            data = await quota_service.fetch_plan_quota(acc.pat_token)
            if not data:
                return None

            # Only a real /quota/usage answer may park or un-park. When just the
            # plan/userinfo calls succeeded there is no quota verdict, so update
            # the metadata and leave availability exactly as it was.
            quota_fetched = bool(data.get("quota_fetched", True))

            was_parked = bool(acc.is_quota_exceeded)
            was_cooling = acc.cooldown_until is not None
            was_unavailable = not bool(acc.is_available)

            mapping = {
                "plan_tier": "plan_tier",
                "plan_name": "plan_name",
                "is_paid": "is_paid",
                "end_date": "plan_end_date",
                "email": "email",
                "quota_total": "quota_total",
                "quota_used": "quota_used",
                "quota_remaining": "quota_remaining",
                "quota_percentage": "quota_percentage",
                "is_quota_exceeded": "is_quota_exceeded",
                "quota_unit": "quota_unit",
                "expires_at": "quota_expires_at",
                "quota_fetched_at": "quota_fetched_at",
            }
            for src, dst in mapping.items():
                if src in data and data[src] is not None:
                    setattr(acc, dst, data[src])

            if quota_fetched and data.get("is_quota_exceeded"):
                await self._park_exhausted(session, acc, "quota refresh shows exhausted")
                return data

            if not quota_fetched:
                await session.commit()
                return data

            # PAT proved healthy — drop the sticky error so the UI recovers.
            acc.last_error_message = None
            # Un-park: quota came back, so the account rejoins routing right
            # away instead of waiting for the next pool refresh sweep.
            # `enabled` still wins: refreshing quota must never resurrect an
            # account the operator took out of rotation.
            acc.is_available = bool(acc.enabled)
            acc.cooldown_until = None
            acc.consecutive_failures = 0

            await session.commit()
            if was_parked or was_cooling or was_unavailable:
                reason = (
                    "quota restored"
                    if was_parked
                    else "cooldown cleared"
                    if was_cooling
                    else "rejoined routing"
                )
                logbus.push(
                    "info", "pool",
                    f"account restored: {acc.name} (id {acc.id})",
                    action="restored",
                    account_id=acc.id,
                    account_name=acc.name,
                    reason=reason,
                )
            return data

    def _auto_delete_allowed_for(self, acc: Account) -> bool:
        """Return whether automatic deletion of exhausted accounts is enabled."""
        return bool(settings_service.get("accounts_auto_delete_exhausted"))

    async def delete_exhausted_accounts(self) -> int:
        """Bulk-delete every exhausted account (used when the setting turns on)."""
        async with async_session() as session:
            stmt = select(Account).where(Account.is_quota_exceeded == True)
            result = await session.execute(stmt)
            doomed = [a for a in result.scalars().all() if self._auto_delete_allowed_for(a)]
            for acc in doomed:
                logger.warning("Auto-deleting exhausted account %s (%d)", acc.name, acc.id)
                logbus.push(
                    "warn", "pool",
                    f"account auto-deleted (exhausted): {acc.name} (id {acc.id})",
                    action="auto_deleted",
                    account_id=acc.id,
                    account_name=acc.name,
                    reason="bulk sweep (auto-delete exhausted)",
                )
                await session.delete(acc)
            await session.commit()
        if doomed:
            await self._refresh()
        return len(doomed)

    async def _park_exhausted(self, session: AsyncSession, acc: Account, reason: str):
        """Quota exhausted — keep the account in the pool but out of rotation.

        It stays visible ("quiet") and automatically rejoins routing if a
        later quota refresh shows credits again (mapping clears the flag).
        When auto-delete is enabled the account is removed instead of parked.
        """
        if self._auto_delete_allowed_for(acc):
            logger.warning(
                "Account %s (%d) exhausted (%s); auto-deleting",
                acc.name, acc.id, reason,
            )
            logbus.push(
                "warn", "pool",
                f"account auto-deleted (exhausted): {acc.name} (id {acc.id})",
                action="auto_deleted",
                account_id=acc.id,
                account_name=acc.name,
                reason=reason,
            )
            await session.delete(acc)
            await session.commit()
            return
        logger.warning(
            "Account %s (%d) exhausted (%s); parking",
            acc.name, acc.id, reason,
        )
        logbus.push(
            "warn", "pool",
            f"account parked (exhausted): {acc.name} (id {acc.id})",
            action="parked",
            account_id=acc.id,
            account_name=acc.name,
            reason=reason,
        )
        acc.is_quota_exceeded = True
        acc.is_available = False
        acc.quota_remaining = 0.0
        await session.commit()

    async def mark_quota_exceeded(self, account_id: int):
        """Quota-exceeded signal from the chat/anthropic routers — park (or auto-delete)."""
        async with async_session() as session:
            acc = await session.get(Account, account_id)
            if not acc:
                return
            await self._park_exhausted(session, acc, "api reported quota exceeded")

    async def refresh_all_quotas(self) -> int:
        """Refresh plan/quota for every active, non-exhausted account.

        Exhausted accounts are left alone — the backend never polls Qoder for
        them. They only rejoin when the user manually refreshes the account
        (refresh_quota) and it comes back with credits.
        """
        async with async_session() as session:
            stmt = select(Account).where(
                Account.is_active == True,
                Account.is_quota_exceeded == False,
            )
            result = await session.execute(stmt)
            ids = [a.id for a in result.scalars().all()]

        ok = 0
        for account_id in ids:
            data = await self.refresh_quota(account_id)
            if data:
                ok += 1
            await asyncio.sleep(0.3)  # be polite to the API
        return ok

    async def mark_success(
        self,
        account_id: int,
        tokens_used: int = 0,
        credits_used: float = 0.0,
    ) -> None:
        """Persist counters and credit usage for a successful call."""
        try:
            async with async_session() as session:
                acc = await session.get(Account, account_id)
                if not acc:
                    return

                acc.last_used_at = _utcnow()
                acc.consecutive_failures = 0
                acc.last_error_message = None
                acc.is_available = acc.enabled and not acc.is_quota_exceeded
                acc.cooldown_until = None

                # Counters via SQL expressions — concurrent completions on the
                # same account must not lose updates (fill-first routing makes
                # parallel calls on one account common).
                await session.execute(
                    update(Account)
                    .where(Account.id == account_id)
                    .values(
                        total_requests=func.coalesce(Account.total_requests, 0) + 1,
                        total_tokens=func.coalesce(Account.total_tokens, 0) + tokens_used,
                    )
                    .execution_options(synchronize_session=False)
                )
                session.expire(acc, ["total_requests", "total_tokens"])

                # Lifetime pool counter — survives account purges. Atomic
                # upsert for the same reason as the per-account counters:
                # read-modify-write loses credits under parallel completions,
                # and a plain INSERT races into IntegrityError.
                if credits_used:
                    await session.execute(
                        sqlite_insert(PoolCounter)
                        .values(key=CREDITS_SPENT_KEY, value=float(credits_used))
                        .on_conflict_do_update(
                            index_elements=[PoolCounter.key],
                            set_={"value": func.coalesce(PoolCounter.value, 0.0) + float(credits_used)},
                        )
                        .execution_options(synchronize_session=False)
                    )

                # Drain local quota estimate atomically — the ORM
                # read-modify-write used to lose decrements when fill-first
                # routing ran two completions on the same account. Expire the
                # columns afterwards so a later flush cannot overwrite the
                # SQL result with the stale in-memory values.
                drained = False
                if credits_used:
                    remaining_expr = Account.quota_remaining - float(credits_used)
                    result = await session.execute(
                        update(Account)
                        .where(
                            Account.id == account_id,
                            Account.quota_remaining.is_not(None),
                        )
                        .values(
                            quota_remaining=case(
                                (remaining_expr < 0, 0.0),
                                else_=remaining_expr,
                            ),
                            quota_used=func.coalesce(Account.quota_used, 0.0)
                            + float(credits_used),
                        )
                        .returning(Account.quota_remaining, Account.is_quota_exceeded, Account.is_paid)
                        .execution_options(synchronize_session=False)
                    )
                    row = result.first()
                    # A completion that lands after a parallel one already
                    # parked (or auto-deleted) the account must not park it a
                    # second time: that duplicates the pool event and, with
                    # auto-delete on, issues a DELETE for a row that is gone.
                    # Free-tier accounts carry a 0/0 quota (personal resource
                    # packs are invisible to the quota API); local drain must
                    # not park them — upstream errors are the real feedback.
                    drained = (
                        row is not None
                        and row[0] is not None
                        and row[0] <= 0
                        and not bool(row[1])
                        and bool(row[2])
                    )
                    session.expire(acc, ["quota_remaining", "quota_used"])
                    if drained:
                        parked = await session.get(Account, account_id)
                        if parked:
                            await self._park_exhausted(
                                session, parked, "quota drained locally"
                            )
                            return

                await session.commit()
        except StaleDataError:
            # The account row was deleted (manual delete / auto-delete sweep)
            # while the request was in flight. The upstream completion itself
            # succeeded — nothing left to book.
            logger.warning(
                "mark_success: account %s deleted concurrently; skipping bookkeeping",
                account_id,
            )
            return

    async def mark_failure(self, account_id: int, error_message: str = ""):
        try:
            async with async_session() as session:
                # Atomic failure increment — concurrent errors on the same
                # fill-first account must not lose updates.
                result = await session.execute(
                    update(Account)
                    .where(Account.id == account_id)
                    .values(
                        consecutive_failures=func.coalesce(Account.consecutive_failures, 0) + 1,
                        last_error_at=_utcnow(),
                        last_error_message=(error_message or "")[:512],
                    )
                    .returning(
                        Account.consecutive_failures,
                        Account.is_quota_exceeded,
                        Account.name,
                        Account.enabled,
                    )
                    .execution_options(synchronize_session=False)
                )
                row = result.first()
                if not row:
                    return

                new_failures = int(row[0] or 0)
                is_exceeded = bool(row[1])
                name = row[2]
                is_enabled = bool(row[3])
                cooldown_until = None
                is_available = True
                if new_failures >= settings.max_consecutive_failures:
                    backoff = 2 ** (new_failures - settings.max_consecutive_failures + 1)
                    cooldown_until = _utcnow() + timedelta(
                        seconds=settings.account_cooldown_seconds * backoff
                    )
                    is_available = False
                    logger.warning(
                        "Account %s cooldown until %s (%d failures)",
                        name, cooldown_until, new_failures,
                    )

                await session.execute(
                    update(Account)
                    .where(Account.id == account_id)
                    .values(
                        cooldown_until=cooldown_until,
                        # Exhausted accounts stay parked — failures don't revive
                        # them; neither does a failure revive a disabled account.
                        is_available=is_available and not is_exceeded and is_enabled,
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if cooldown_until is not None and not is_exceeded:
                    secs = int((cooldown_until - _utcnow()).total_seconds())
                    logbus.push(
                        "warn", "pool",
                        f"account cooldown: {name} (id {account_id}) · {new_failures} failures",
                        action="cooldown",
                        account_id=account_id,
                        account_name=name,
                        reason=f"{new_failures} consecutive failures · ~{max(secs, 0)}s",
                    )
        except StaleDataError:
            logger.warning(
                "mark_failure: account %s deleted concurrently; skipping",
                account_id,
            )

    async def list_accounts(self, session: AsyncSession) -> list[Account]:
        stmt = select(Account).order_by(Account.priority.desc(), Account.id.asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_account_by_id(self, session: AsyncSession, account_id: int) -> Optional[Account]:
        return await session.get(Account, account_id)

    async def update_account(
        self,
        session: AsyncSession,
        account_id: int,
        *,
        name: Optional[str] = None,
        priority: Optional[int] = None,
        default_model: Optional[str] = None,
        enabled: Optional[bool] = None,
        reserve_credits: Optional[int] = None,
    ) -> Optional[Account]:
        """Partial update of the operator-controlled fields.

        Disabling is authoritative but never destroys other state: quota,
        cooldown and failure counters are left untouched so a later re-enable
        restores the account exactly as it was. Re-enabling deliberately does
        NOT clear ``is_quota_exceeded`` — an exhausted account stays parked
        until quota actually returns.

        Returns the updated row, or ``None`` when the id is unknown.
        """
        acc = await session.get(Account, account_id)
        if not acc:
            return None

        if name is not None:
            acc.name = name
        if priority is not None:
            acc.priority = priority
        if default_model is not None:
            acc.default_model = default_model
        if reserve_credits is not None:
            acc.reserve_credits = max(0, int(reserve_credits))
        if enabled is not None:
            acc.enabled = enabled
            # Keep the derived flag consistent immediately rather than waiting
            # for the next `_refresh` sweep, so the panel and routing agree.
            acc.is_available = bool(enabled) and not bool(acc.is_quota_exceeded)
            if enabled and acc.cooldown_until is not None and acc.cooldown_until <= _utcnow():
                # A served cooldown no longer applies; mirror `_refresh`.
                acc.cooldown_until = None
                acc.consecutive_failures = 0

        await session.commit()
        await session.refresh(acc)

        action = "updated"
        if enabled is not None:
            action = "enabled" if enabled else "disabled"
        logbus.push(
            "info", "pool",
            f"account {action}: {acc.name} (id {acc.id})",
            action=action,
            account_id=acc.id,
            account_name=acc.name,
        )
        return acc

    async def add_account(
        self, session: AsyncSession, name: str, pat_token: str,
        priority: int = 0, model_level: str = "auto", default_model: str = "",
    ) -> Account:
        existing = await session.execute(
            select(Account.id).where(Account.pat_token == pat_token)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError("This PAT is already in the pool")

        account = Account(
            name=name,
            pat_token=pat_token,
            pat_short=pat_token[:12] + "..." if len(pat_token) > 15 else pat_token,
            priority=priority,
            model_level=model_level,
            default_model=default_model,
            # Generate unique machine_id for anti-fraud (random UUID per account)
            machine_id=str(uuid.uuid4()),
        )
        session.add(account)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ValueError("This PAT is already in the pool") from None
        await session.refresh(account)
        await self._refresh()
        logbus.push(
            "info", "pool",
            f"account added: {name} (id {account.id})",
            action="added",
            account_id=account.id,
            account_name=name,
        )
        return account

    async def remove_account(self, session: AsyncSession, account_id: int) -> bool:
        acc = await session.get(Account, account_id)
        if not acc:
            return False
        name = acc.name
        await session.delete(acc)
        await session.commit()
        await self._refresh()
        logbus.push(
            "warn", "pool",
            f"account removed: {name} (id {account_id})",
            action="removed",
            account_id=account_id,
            account_name=name,
            reason="manual delete",
        )
        return True

    async def get_stats(self, session: AsyncSession) -> dict:
        stmt = select(
            func.count(Account.id).label("total"),
            func.sum(Account.total_requests).label("total_req"),
            func.sum(Account.total_tokens).label("total_tok"),
        )
        result = await session.execute(stmt)
        row = result.one()

        active_stmt = select(func.count(Account.id)).where(Account.is_active == True)
        active_count = (await session.execute(active_stmt)).scalar() or 0

        avail_stmt = select(func.count(Account.id)).where(
            Account.is_active == True, Account.is_available == True
        )
        available_now = (await session.execute(avail_stmt)).scalar() or 0

        now = _utcnow()
        cd_stmt = select(func.count(Account.id)).where(
            Account.is_active == True,
            Account.cooldown_until != None,
            Account.cooldown_until > now,
        )
        in_cooldown = (await session.execute(cd_stmt)).scalar() or 0

        spent_stmt = select(PoolCounter.value).where(PoolCounter.key == CREDITS_SPENT_KEY)
        credits_spent = (await session.execute(spent_stmt)).scalar() or 0.0

        disabled_stmt = select(func.count(Account.id)).where(Account.enabled == False)
        disabled_count = (await session.execute(disabled_stmt)).scalar() or 0

        return {
            "total_accounts": row.total or 0,
            "active_accounts": active_count,
            "available_now": available_now,
            "accounts_in_cooldown": in_cooldown,
            "accounts_disabled": disabled_count,
            "total_requests": row.total_req or 0,
            "total_tokens": row.total_tok or 0,
            "credits_spent": credits_spent,
        }

    def _refresh_interval_seconds(self) -> float:
        # ``is_available`` is only recomputed by _refresh(); pacing it by the
        # 5-minute quota poll made a nominal 60s cooldown last up to 300s.
        # Recompute at cooldown granularity instead (one cheap SELECT).
        return float(
            max(1, min(settings.qoder_poll_interval, settings.account_cooldown_seconds))
        )

    async def _refresh_if_stale(self):
        interval = self._refresh_interval_seconds()
        now = _utcnow()
        if (now - self._last_refresh).total_seconds() <= interval:
            return
        async with self._refresh_lock:
            now = _utcnow()
            if (now - self._last_refresh).total_seconds() > interval:
                await self._refresh()


pool = AccountPool()
