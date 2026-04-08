"""Platform-admin email campaign service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.email_notification import EmailNotification
from app.db.models.platform_admin import PlatformAdmin
from app.db.models.platform_email_campaign import (
    PlatformEmailCampaign,
    PlatformEmailCampaignRecipient,
)
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import User, UserRole
from app.schemas.platform_email_campaign import (
    PlatformEmailCampaignCreateRequest,
    PlatformEmailCampaignDetailResponse,
    PlatformEmailCampaignSummaryResponse,
    PlatformEmailCampaignTestSendRequest,
    PlatformEmailRecipientResponse,
)
from app.services.email_service import email_service


@dataclass
class ResolvedRecipient:
    user_id: UUID | None
    tenant_id: UUID | None
    recipient_email: str
    recipient_name: str | None
    tenant_name: str | None


class PlatformEmailCampaignService:
    """Create, queue, and send platform email campaigns."""

    def __init__(self, db: Session):
        self.db = db

    def _validate_request(self, request: PlatformEmailCampaignCreateRequest) -> None:
        if bool(request.cta_label) != bool(request.cta_url):
            raise ValueError("CTA label and CTA URL must be provided together.")

        if request.audience_type == "selected_users" and not request.selected_user_ids:
            raise ValueError("Select at least one recipient for selected-user campaigns.")

        if request.scheduled_at and request.scheduled_at <= datetime.now(UTC):
            raise ValueError("Scheduled send time must be in the future.")

    def _resolve_selected_users(self, user_ids: list[UUID]) -> list[ResolvedRecipient]:
        if not user_ids:
            return []

        users = (
            self.db.query(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .filter(User.id.in_(user_ids), User.is_active.is_(True))
            .all()
        )

        recipients: list[ResolvedRecipient] = []
        for user, tenant in users:
            recipients.append(
                ResolvedRecipient(
                    user_id=user.id,
                    tenant_id=tenant.id if tenant else None,
                    recipient_email=user.email,
                    recipient_name=user.full_name,
                    tenant_name=tenant.display_name if tenant else None,
                )
            )
        return recipients

    def _resolve_all_tenant_admins(self) -> list[ResolvedRecipient]:
        tenant_admins = self._query_tenant_admins()

        recipients: list[ResolvedRecipient] = []
        for user, tenant in tenant_admins:
            recipients.append(
                ResolvedRecipient(
                    user_id=user.id,
                    tenant_id=tenant.id,
                    recipient_email=user.email,
                    recipient_name=user.full_name,
                    tenant_name=tenant.display_name,
                )
            )
        return recipients

    def _query_tenant_admins(self):
        return (
            self.db.query(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .filter(
                User.role == UserRole.TENANT_ADMIN,
                User.is_active.is_(True),
                Tenant.status == TenantStatus.ACTIVE,
            )
            .order_by(Tenant.display_name.asc(), User.email.asc())
            .all()
        )

    def _resolve_tenant_admins_by_status(
        self, *, subscription_statuses: set[str] | None = None, exclude_trial: bool = False
    ) -> list[ResolvedRecipient]:
        query = (
            self.db.query(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .filter(
                User.role == UserRole.TENANT_ADMIN,
                User.is_active.is_(True),
                Tenant.status == TenantStatus.ACTIVE,
            )
        )

        if subscription_statuses is not None:
            query = query.filter(Tenant.subscription_status.in_(subscription_statuses))

        if exclude_trial:
            query = query.filter(Tenant.subscription_tier != "trial")

        tenant_admins = query.order_by(Tenant.display_name.asc(), User.email.asc()).all()

        recipients: list[ResolvedRecipient] = []
        for user, tenant in tenant_admins:
            recipients.append(
                ResolvedRecipient(
                    user_id=user.id,
                    tenant_id=tenant.id,
                    recipient_email=user.email,
                    recipient_name=user.full_name,
                    tenant_name=tenant.display_name,
                )
            )
        return recipients

    def _dedupe_recipients(self, recipients: list[ResolvedRecipient]) -> list[ResolvedRecipient]:
        deduped: dict[str, ResolvedRecipient] = {}
        for recipient in recipients:
            deduped[recipient.recipient_email.lower()] = recipient
        return list(deduped.values())

    def _resolve_recipients(
        self, request: PlatformEmailCampaignCreateRequest
    ) -> tuple[list[ResolvedRecipient], dict[str, Any]]:
        if request.audience_type == "all_tenant_admins":
            recipients = self._resolve_all_tenant_admins()
            audience_summary: dict[str, Any] = {
                "mode": "all_tenant_admins",
                "description": "All active tenant admins",
            }
        elif request.audience_type == "active_paid_tenant_admins":
            recipients = self._resolve_tenant_admins_by_status(
                subscription_statuses={"active"},
                exclude_trial=True,
            )
            audience_summary = {
                "mode": "active_paid_tenant_admins",
                "description": "Tenant admins on active paid plans",
            }
        elif request.audience_type == "trial_tenant_admins":
            recipients = self._resolve_tenant_admins_by_status(
                subscription_statuses={"trial", "trialing"}
            )
            audience_summary = {
                "mode": "trial_tenant_admins",
                "description": "Tenant admins currently on trial",
            }
        elif request.audience_type == "trial_expired_tenant_admins":
            recipients = self._resolve_tenant_admins_by_status(
                subscription_statuses={"trial_expired"}
            )
            audience_summary = {
                "mode": "trial_expired_tenant_admins",
                "description": "Tenant admins whose trial has expired",
            }
        else:
            recipients = self._resolve_selected_users(request.selected_user_ids)
            audience_summary = {
                "mode": "selected_users",
                "selected_user_ids": [str(user_id) for user_id in request.selected_user_ids],
            }

        recipients = self._dedupe_recipients(recipients)
        if not recipients:
            raise ValueError("No active recipients matched your selection.")

        audience_summary["resolved_count"] = len(recipients)
        return recipients, audience_summary

    def _get_admin_name(self, admin_id: UUID) -> str:
        admin = self.db.query(PlatformAdmin).filter(PlatformAdmin.id == admin_id).first()
        if not admin:
            return "Unknown Admin"
        return admin.full_name or admin.email

    def _to_summary_response(
        self, campaign: PlatformEmailCampaign
    ) -> PlatformEmailCampaignSummaryResponse:
        return PlatformEmailCampaignSummaryResponse(
            id=campaign.id,
            created_by_admin_id=campaign.created_by_admin_id,
            created_by_admin_name=self._get_admin_name(campaign.created_by_admin_id),
            title=campaign.title,
            audience_type=campaign.audience_type,
            template_key=campaign.template_key,
            status=campaign.status,
            subject=campaign.subject,
            heading=campaign.heading,
            total_recipients=campaign.total_recipients,
            success_count=campaign.success_count,
            failure_count=campaign.failure_count,
            queued_at=campaign.queued_at,
            scheduled_at=campaign.scheduled_at,
            started_at=campaign.started_at,
            completed_at=campaign.completed_at,
            sent_at=campaign.sent_at,
            error_message=campaign.error_message,
            created_at=campaign.created_at,
        )

    def _to_detail_response(
        self,
        campaign: PlatformEmailCampaign,
        recipients: list[PlatformEmailCampaignRecipient],
    ) -> PlatformEmailCampaignDetailResponse:
        return PlatformEmailCampaignDetailResponse(
            **self._to_summary_response(campaign).model_dump(),
            preheader=campaign.preheader,
            body=campaign.body,
            cta_label=campaign.cta_label,
            cta_url=campaign.cta_url,
            audience_summary=campaign.audience_summary,
            recipients=[PlatformEmailRecipientResponse.model_validate(item) for item in recipients],
        )

    def create_campaign(
        self,
        request: PlatformEmailCampaignCreateRequest,
        *,
        admin_id: UUID,
    ) -> PlatformEmailCampaignSummaryResponse:
        self._validate_request(request)
        recipients, audience_summary = self._resolve_recipients(request)

        now = datetime.now(UTC)
        campaign = PlatformEmailCampaign(
            created_by_admin_id=admin_id,
            title=request.title.strip(),
            audience_type=request.audience_type,
            template_key=request.template_key,
            status="scheduled" if request.scheduled_at else "queued",
            subject=request.subject.strip(),
            preheader=request.preheader.strip() if request.preheader else None,
            heading=request.heading.strip(),
            body=request.body.strip(),
            cta_label=request.cta_label,
            cta_url=request.cta_url,
            audience_summary=audience_summary,
            total_recipients=len(recipients),
            queued_at=now,
            scheduled_at=request.scheduled_at,
        )
        self.db.add(campaign)
        self.db.flush()

        for recipient in recipients:
            self.db.add(
                PlatformEmailCampaignRecipient(
                    campaign_id=campaign.id,
                    tenant_id=recipient.tenant_id,
                    user_id=recipient.user_id,
                    recipient_email=recipient.recipient_email,
                    recipient_name=recipient.recipient_name,
                    tenant_name=recipient.tenant_name,
                    status="pending",
                )
            )

        self.db.commit()
        self.db.refresh(campaign)
        return self._to_summary_response(campaign)

    def list_campaigns(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PlatformEmailCampaignSummaryResponse]:
        campaigns = (
            self.db.query(PlatformEmailCampaign)
            .order_by(PlatformEmailCampaign.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [self._to_summary_response(campaign) for campaign in campaigns]

    def get_campaign_detail(self, campaign_id: UUID) -> PlatformEmailCampaignDetailResponse:
        campaign = (
            self.db.query(PlatformEmailCampaign)
            .filter(PlatformEmailCampaign.id == campaign_id)
            .first()
        )
        if not campaign:
            raise ValueError("Campaign not found")

        recipients = (
            self.db.query(PlatformEmailCampaignRecipient)
            .filter(PlatformEmailCampaignRecipient.campaign_id == campaign_id)
            .order_by(
                PlatformEmailCampaignRecipient.sent_at.desc().nullslast(),
                PlatformEmailCampaignRecipient.created_at.asc(),
            )
            .all()
        )
        return self._to_detail_response(campaign, recipients)

    def send_campaign(self, campaign_id: UUID) -> None:
        campaign = (
            self.db.query(PlatformEmailCampaign)
            .filter(PlatformEmailCampaign.id == campaign_id)
            .first()
        )
        if not campaign:
            raise ValueError("Campaign not found")

        if campaign.status in {"sent", "partial_failed", "failed"}:
            return

        if campaign.scheduled_at and campaign.scheduled_at > datetime.now(UTC):
            return

        now = datetime.now(UTC)
        campaign.status = "sending"
        campaign.started_at = now
        campaign.error_message = None
        self.db.commit()

        recipients = (
            self.db.query(PlatformEmailCampaignRecipient)
            .filter(
                PlatformEmailCampaignRecipient.campaign_id == campaign_id,
                PlatformEmailCampaignRecipient.status == "pending",
            )
            .order_by(PlatformEmailCampaignRecipient.created_at.asc())
            .all()
        )

        success_count = 0
        failure_count = 0
        body_html = campaign.body

        for recipient in recipients:
            success = email_service.send_platform_broadcast_email(
                recipient_email=recipient.recipient_email,
                recipient_name=recipient.recipient_name or recipient.recipient_email,
                template_key=campaign.template_key,
                subject=campaign.subject,
                preheader=campaign.preheader,
                heading=campaign.heading,
                body_html=body_html,
                cta_label=campaign.cta_label,
                cta_url=campaign.cta_url,
            )

            if success:
                recipient.status = "sent"
                recipient.sent_at = datetime.now(UTC)
                recipient.error_message = None
                success_count += 1
                if recipient.tenant_id:
                    self.db.add(
                        EmailNotification(
                            tenant_id=recipient.tenant_id,
                            notification_type="platform_campaign",
                            recipient_email=recipient.recipient_email,
                            subject=campaign.subject,
                            reference_id=campaign.id,
                            reference_type="platform_email_campaign",
                            status="sent",
                            sent_at=recipient.sent_at,
                            template_data={
                                "campaign_id": str(campaign.id),
                                "campaign_title": campaign.title,
                                "template_key": campaign.template_key,
                            },
                        )
                    )
            else:
                recipient.status = "failed"
                recipient.error_message = "Failed to send email"
                failure_count += 1

            self.db.commit()

        campaign.success_count = success_count
        campaign.failure_count = failure_count
        campaign.completed_at = datetime.now(UTC)
        campaign.sent_at = campaign.completed_at

        if success_count and not failure_count:
            campaign.status = "sent"
        elif success_count and failure_count:
            campaign.status = "partial_failed"
            campaign.error_message = "Some emails could not be delivered."
        else:
            campaign.status = "failed"
            campaign.error_message = "All campaign emails failed to send."

        self.db.commit()

    def send_due_campaigns(self) -> int:
        now = datetime.now(UTC)
        campaigns = (
            self.db.query(PlatformEmailCampaign)
            .filter(
                PlatformEmailCampaign.status.in_(["scheduled", "queued"]),
                PlatformEmailCampaign.scheduled_at.isnot(None),
                PlatformEmailCampaign.scheduled_at <= now,
            )
            .order_by(PlatformEmailCampaign.scheduled_at.asc())
            .all()
        )

        sent_count = 0
        for campaign in campaigns:
            self.send_campaign(campaign.id)
            sent_count += 1
        return sent_count

    def send_test_email(
        self,
        request: PlatformEmailCampaignTestSendRequest,
        *,
        recipient_email: str,
        recipient_name: str,
    ) -> bool:
        if bool(request.cta_label) != bool(request.cta_url):
            raise ValueError("CTA label and CTA URL must be provided together.")

        return email_service.send_platform_broadcast_email(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            template_key=request.template_key,
            subject=request.subject.strip(),
            preheader=request.preheader.strip() if request.preheader else None,
            heading=request.heading.strip(),
            body_html=request.body.strip(),
            cta_label=request.cta_label,
            cta_url=request.cta_url,
        )
