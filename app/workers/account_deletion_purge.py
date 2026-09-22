"""Background worker for purging account-deletion requests past their grace period.

Runs on the same cron cadence as ``conversation_auto_resolver``'s
``auto_abandon_inactive_conversations`` (every 5 minutes) — see the
``cron_jobs`` list in ``app/workers/document_processor.py``. Queries
``DeletionRequest`` rows that are due (``status == GRACE`` and
``scheduled_purge_at <= now``) and dispatches each to
``AccountPurgeService`` — ``purge_user`` for ``self_user`` scope,
``purge_tenant`` for ``tenant`` scope — then marks the request ``PURGED``.

The purge itself is irreversible (see ``app/services/account_purge_service.py``);
this worker's only job is finding due requests and dispatching them. A single
request failing does not abort the batch: it is logged (by request id only —
no PII) and the worker moves on to the next one, so one bad row can't wedge
every other pending purge behind it. Each item's purge attempt runs inside
its own ``SAVEPOINT`` (``Session.begin_nested()``), rolled back on failure —
this scopes a failed attempt's rollback to exactly that item's own partial
writes, rather than the whole batch's shared session, so one item's failure
can never undo an earlier item's already-committed purge.
"""

from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.deletion_request import DeletionRequest, DeletionScope
from app.db.models.user import DeletionStatus
from app.db.session import SessionLocal
from app.services.account_purge_service import AccountPurgeService


async def purge_due_deletion_requests(ctx: dict) -> dict[str, Any]:
    """
    Purge accounts/tenants whose deletion grace period has ended.

    Runs every 5 minutes.
    Purges a ``DeletionRequest`` if:
    - Status is GRACE
    - scheduled_purge_at <= now

    Args:
        ctx: ARQ context

    Returns:
        dict with success status and purged/failed counts
    """
    db: Session = SessionLocal()

    try:
        now = datetime.now(UTC)

        due_requests = (
            db.execute(
                select(DeletionRequest)
                .where(
                    DeletionRequest.status == DeletionStatus.GRACE,
                    DeletionRequest.scheduled_purge_at <= now,
                )
                .order_by(DeletionRequest.scheduled_purge_at)
            )
            .scalars()
            .all()
        )

        purge_service = AccountPurgeService(db)
        purged_count = 0
        failed_count = 0

        for request in due_requests:
            # The GRACE filter above already excludes already-purged rows,
            # but re-check defensively: purging an earlier request in this
            # same batch (e.g. a tenant purge that anonymizes a member who
            # is also the requester of another open request) can flip this
            # row's status as a side effect before we get to it.
            if request.status == DeletionStatus.PURGED:
                continue

            # Scope this item's attempt to its own SAVEPOINT so a failure
            # only unwinds this request's partial writes, never an earlier
            # request's already-committed purge in the same batch.
            savepoint = db.begin_nested()
            try:
                if request.scope == DeletionScope.SELF_USER:
                    purge_service.purge_user(request.user_id)
                else:
                    purge_service.purge_tenant(request.tenant_id)

                # AccountPurgeService already flips this request's status to
                # PURGED as a side effect of purging its user (see
                # _anonymize_user) and commits it -- this is a defensive,
                # explicit mark in case that ever stops being true, and is a
                # no-op DB round-trip in the normal case.
                if request.status != DeletionStatus.PURGED:
                    request.status = DeletionStatus.PURGED
                    db.commit()
                purged_count += 1
                logger.info(
                    "Purged deletion request {} (scope={})", request.id, request.scope.value
                )
            except Exception as e:
                savepoint.rollback()
                failed_count += 1
                logger.error(
                    "Failed to purge deletion request {}: {}", request.id, str(e), exc_info=True
                )
                # Continue with other requests even if this one fails

        logger.success(
            f"Account deletion purge task completed: "
            f"{purged_count} purged, {failed_count} failed"
        )

        return {
            "success": True,
            "purged_count": purged_count,
            "failed_count": failed_count,
            "cutoff_time": now.isoformat(),
        }

    except Exception as e:
        logger.error("Error in purge_due_deletion_requests: {}", str(e), exc_info=True)
        db.rollback()
        return {"success": False, "error": str(e)}

    finally:
        db.close()
