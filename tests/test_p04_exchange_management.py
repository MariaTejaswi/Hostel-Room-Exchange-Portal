from datetime import timedelta
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditLog
from apps.exchange.eligibility import (
    ExternalStatusResult,
    UnavailableExternalEligibilityAdapter,
    check_student_exchange_eligibility,
    validate_exchange_request_eligibility,
)
from apps.exchange.models import (
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
)
from apps.exchange.services import (
    create_exchange_request,
    expire_exchange_requests,
    get_institution_exchange_policies,
    update_exchange_request,
    update_institution_exchange_policy,
    withdraw_exchange_request,
)
from apps.exchange.tasks import expire_exchange_requests_task
from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
)
from apps.hostels.services import assign_student_to_bed


class MockStatusAdapter:
    def __init__(self, fee_result: ExternalStatusResult, disciplinary_result: ExternalStatusResult):
        self._fee = fee_result
        self._disciplinary = disciplinary_result

    def check_fee_status(self, student) -> ExternalStatusResult:
        return self._fee

    def check_disciplinary_status(self, student) -> ExternalStatusResult:
        return self._disciplinary


def _configure_policies(
    institution,
    cooling_off_days=0,
    request_validity_days=30,
    require_fee_clearance=False,
    require_no_disciplinary_hold=False,
):
    configs = {
        PolicyRule.Key.COOLING_OFF_DAYS: cooling_off_days,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS: request_validity_days,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE: require_fee_clearance,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: require_no_disciplinary_hold,
    }
    for key, val in configs.items():
        PolicyRule.objects.update_or_create(
            institution=institution,
            key=key,
            defaults={"value": {"value": val}, "is_active": True},
        )


def _request_data(**overrides):
    values = {
        "preferred_hostel": None,
        "preferred_block": None,
        "preferred_floor": None,
        "preferred_bed": None,
        "preferred_room_type": "",
        "preferred_occupancy_category": "",
        "preferred_accessibility_attributes": "",
        "reason": "I require accommodation closer to academic labs.",
    }
    values.update(overrides)
    return values


def _occupy(student_user, hostel_setup, warden_user, **kwargs):
    return assign_student_to_bed(
        student=student_user,
        bed=hostel_setup["bed_a"],
        created_by=warden_user,
        **kwargs,
    )


# =========================================================================
# 1. Policy Configuration & Invariants
# =========================================================================

@pytest.mark.django_db
def test_missing_policy_configuration_blocks_request_and_reports_clear_reason(
    student_user,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    # Ensure no policies are configured
    PolicyRule.objects.filter(institution=student_user.institution).delete()

    report = check_student_exchange_eligibility(student=student_user)
    assert not report.is_eligible
    policy_checks = [c for c in report.checks if c.key == "policy_configuration"]
    assert len(policy_checks) == 1
    assert not policy_checks[0].passed
    assert "Exchange policies" in policy_checks[0].reason
    assert "have not been configured by the Hostel Administrator" in policy_checks[0].reason

    with pytest.raises(ValidationError, match="Exchange policies .* have not been configured"):
        create_exchange_request(student=student_user, data=_request_data())


@pytest.mark.django_db
def test_partial_policy_configuration_identifies_specific_missing_keys(
    student_user,
    hostel_setup,
    warden_user,
    institution,
):
    _occupy(student_user, hostel_setup, warden_user)
    PolicyRule.objects.filter(institution=institution).delete()

    # Configure only cooling off and validity, omitting fee and disciplinary
    PolicyRule.objects.create(
        institution=institution,
        key=PolicyRule.Key.COOLING_OFF_DAYS,
        value={"value": 14},
    )
    PolicyRule.objects.create(
        institution=institution,
        key=PolicyRule.Key.REQUEST_VALIDITY_DAYS,
        value={"value": 30},
    )

    report = check_student_exchange_eligibility(student=student_user)
    assert not report.is_eligible
    policy_check = [c for c in report.checks if c.key == "policy_configuration"][0]
    assert not policy_check.passed
    assert "Require fee clearance" in policy_check.reason
    assert "Require no disciplinary hold" in policy_check.reason
    assert "Cooling-off period" not in policy_check.reason


@pytest.mark.django_db
def test_explicitly_configured_policies_allow_request_and_compute_expiry(
    student_user,
    hostel_setup,
    warden_user,
    institution,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(
        institution,
        cooling_off_days=0,
        request_validity_days=45,
        require_fee_clearance=False,
        require_no_disciplinary_hold=False,
    )

    now = timezone.now()
    req = create_exchange_request(student=student_user, data=_request_data(), now=now)
    assert req.status == ExchangeRequest.Status.OPEN
    expected_expiry = now + timedelta(days=45)
    assert abs((req.expires_at - expected_expiry).total_seconds()) < 5


@pytest.mark.django_db
def test_policy_validation_rejects_negative_or_invalid_values(
    admin_user,
    institution,
):
    with pytest.raises(ValidationError, match="must be a non-negative whole number"):
        update_institution_exchange_policy(
            acting_user=admin_user,
            institution=institution,
            key=PolicyRule.Key.COOLING_OFF_DAYS,
            value=-5,
        )

    with pytest.raises(ValidationError, match="Request validity must be at least 1 day"):
        update_institution_exchange_policy(
            acting_user=admin_user,
            institution=institution,
            key=PolicyRule.Key.REQUEST_VALIDITY_DAYS,
            value=0,
        )

    for invalid_limit in (1, 6, "invalid"):
        with pytest.raises(ValidationError, match="Maximum chain length"):
            update_institution_exchange_policy(
                acting_user=admin_user,
                institution=institution,
                key=PolicyRule.Key.MAX_CYCLE_LENGTH,
                value=invalid_limit,
            )


@pytest.mark.django_db
@pytest.mark.parametrize("limit", [2, 3, 4, 5])
def test_maximum_chain_length_policy_accepts_supported_limits(admin_user, institution, limit):
    rule = update_institution_exchange_policy(
        acting_user=admin_user,
        institution=institution,
        key=PolicyRule.Key.MAX_CYCLE_LENGTH,
        value=limit,
    )
    assert rule.value == {"value": limit}
    assert get_institution_exchange_policies(institution)[PolicyRule.Key.MAX_CYCLE_LENGTH] == limit


@pytest.mark.django_db
def test_maximum_chain_length_default_and_institution_isolation(institution, other_institution):
    assert get_institution_exchange_policies(institution)[PolicyRule.Key.MAX_CYCLE_LENGTH] == 5
    assert get_institution_exchange_policies(other_institution)[PolicyRule.Key.MAX_CYCLE_LENGTH] == 5

    PolicyRule.objects.create(
        institution=institution,
        key=PolicyRule.Key.MAX_CYCLE_LENGTH,
        value={"value": 2},
    )

    assert get_institution_exchange_policies(institution)[PolicyRule.Key.MAX_CYCLE_LENGTH] == 2
    assert get_institution_exchange_policies(other_institution)[PolicyRule.Key.MAX_CYCLE_LENGTH] == 5


# =========================================================================
# 2. Fee & Disciplinary Status Verification
# =========================================================================

@pytest.mark.django_db
def test_fee_clearance_verification_statuses_cleared_blocked_unknown_unavailable(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution, require_fee_clearance=True)

    # 1. Unavailable (fails closed)
    with pytest.raises(ValidationError, match="Fee status could not be verified: Fee status integration is not configured"):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
            status_adapter=UnavailableExternalEligibilityAdapter(),
        )

    # 2. Unknown status (fails closed with actionable reason)
    unknown_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=False, reason="Finance ERP sync pending.", status="unknown"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
    )
    with pytest.raises(ValidationError, match="Fee status could not be verified: Finance ERP sync pending."):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
            status_adapter=unknown_adapter,
        )

    # 3. Blocked status (reports actionable hold reason)
    blocked_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=False, reason="Outstanding hostel mess dues.", status="blocked"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
    )
    with pytest.raises(ValidationError, match="Fee eligibility check failed: Outstanding hostel mess dues."):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
            status_adapter=blocked_adapter,
        )

    # 4. Cleared status (passes)
    cleared_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=True, reason="All dues cleared.", status="cleared"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
    )
    req = create_exchange_request(
        student=student_user,
        data=_request_data(),
        status_adapter=cleared_adapter,
    )
    assert req.status == ExchangeRequest.Status.OPEN


@pytest.mark.django_db
def test_disciplinary_hold_verification_statuses_cleared_blocked_unknown_unavailable(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution, require_no_disciplinary_hold=True)

    # 1. Unavailable
    with pytest.raises(ValidationError, match="Disciplinary status could not be verified"):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
            status_adapter=UnavailableExternalEligibilityAdapter(),
        )

    # 2. Unknown status
    unknown_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=False, reason="Proctorial board inquiry in progress.", status="unknown"),
    )
    with pytest.raises(ValidationError, match="Disciplinary status could not be verified: Proctorial board inquiry in progress."):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
            status_adapter=unknown_adapter,
        )

    # 3. Blocked status
    blocked_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=False, reason="Student on active disciplinary probation.", status="blocked"),
    )
    with pytest.raises(ValidationError, match="Disciplinary eligibility check failed: Student on active disciplinary probation."):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
            status_adapter=blocked_adapter,
        )

    # 4. Cleared status
    cleared_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=True, reason="Clear conduct record.", status="cleared"),
    )
    req = create_exchange_request(
        student=student_user,
        data=_request_data(),
        status_adapter=cleared_adapter,
    )
    assert req.status == ExchangeRequest.Status.OPEN


@pytest.mark.django_db
def test_cooling_off_period_policy_fails_with_clear_date_and_passes_after(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    today = timezone.localdate()
    _occupy(student_user, hostel_setup, warden_user, start_date=today)
    _configure_policies(institution, cooling_off_days=14)

    eligible_on = today + timedelta(days=14)
    expected_msg = f"Your accommodation is under a 14-day cooling-off period until {eligible_on:%Y-%m-%d}."

    report = check_student_exchange_eligibility(student=student_user, today=today)
    assert not report.is_eligible
    assert expected_msg in report.reasons

    with pytest.raises(ValidationError, match=r"cooling-off period until"):
        create_exchange_request(student=student_user, data=_request_data())

    # After 14 days, check passes
    future_date = today + timedelta(days=15)
    report_future = check_student_exchange_eligibility(student=student_user, today=future_date)
    cooling_checks = [c for c in report_future.checks if c.key == "cooling_off"]
    assert cooling_checks[0].passed


@pytest.mark.django_db
def test_check_student_exchange_eligibility_returns_structured_breakdown(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution)
    report = check_student_exchange_eligibility(student=student_user)

    assert report.is_eligible
    assert len(report.reasons) == 0
    keys = {c.key for c in report.checks}
    assert "policy_configuration" in keys
    assert "student_role" in keys
    assert "active_occupancy" in keys
    assert "operational_status" in keys
    assert "residential_eligibility" in keys
    assert "cooling_off" in keys
    assert "fee_clearance" in keys
    assert "disciplinary_hold" in keys


# =========================================================================
# 3. Policy Permissions & Institution Scoping
# =========================================================================

@pytest.mark.django_db
def test_hostel_admin_can_view_and_update_policies_with_audit(
    client,
    admin_user,
    institution,
):
    client.force_login(admin_user)
    url = reverse("exchange:policy_settings")

    get_res = client.get(url)
    assert get_res.status_code == 200
    assert b"Exchange Policy Settings" in get_res.content

    post_data = {
        "cooling_off_days": 10,
        "request_validity_days": 45,
        "maximum_chain_length": 4,
        "require_fee_clearance": True,
        "require_no_disciplinary_hold": True,
    }
    post_res = client.post(url, post_data)
    assert post_res.status_code == 302

    policies = get_institution_exchange_policies(institution)
    assert policies[PolicyRule.Key.COOLING_OFF_DAYS] == 10
    assert policies[PolicyRule.Key.REQUEST_VALIDITY_DAYS] == 45
    assert policies[PolicyRule.Key.MAX_CYCLE_LENGTH] == 4
    assert policies[PolicyRule.Key.REQUIRE_FEE_CLEARANCE] is True
    assert policies[PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD] is True

    # AuditLog verified with actor, institution, before/after values
    audits = AuditLog.objects.filter(institution=institution, target_type="PolicyRule")
    assert audits.count() >= 5
    for audit in audits:
        assert audit.acting_user == admin_user
        assert audit.institution == institution
        assert "previous=" in audit.change_summary
        assert "new=" in audit.change_summary


@pytest.mark.django_db
def test_unauthorized_roles_denied_policy_settings_view_and_service(
    client,
    student_user,
    warden_user,
    chief_warden_user,
    dean_user,
    sysadmin_user,
    institution,
):
    url = reverse("exchange:policy_settings")
    unauthorized_users = [
        student_user,
        warden_user,
        chief_warden_user,
        dean_user,
        sysadmin_user,
    ]

    for user in unauthorized_users:
        client.force_login(user)
        # GET request denied
        get_res = client.get(url)
        assert get_res.status_code == 403, f"{user.email} was not denied GET access"

        # POST request denied
        post_res = client.post(url, {"cooling_off_days": 5, "request_validity_days": 30})
        assert post_res.status_code == 403, f"{user.email} was not denied POST access"

        # Service-level entry point denied
        with pytest.raises(PermissionDenied, match="Only Hostel Administrators"):
            update_institution_exchange_policy(
                acting_user=user,
                institution=institution,
                key=PolicyRule.Key.COOLING_OFF_DAYS,
                value=5,
            )


@pytest.mark.django_db
def test_cross_institution_policy_update_denied(
    admin_user,
    other_institution,
):
    with pytest.raises(PermissionDenied, match="You cannot configure policies for another institution"):
        update_institution_exchange_policy(
            acting_user=admin_user,
            institution=other_institution,
            key=PolicyRule.Key.COOLING_OFF_DAYS,
            value=10,
        )


@pytest.mark.django_db
def test_cross_institution_preference_isolation(
    student_user,
    institution,
    other_institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution)

    # Bed in another institution
    other_hostel = Hostel.objects.create(
        institution=other_institution,
        name="Foreign Hostel",
        code="FH-01",
        resident_category=Hostel.ResidentCategory.MALE,
    )
    b = Block.objects.create(hostel=other_hostel, name="Block X", code="BX")
    f = Floor.objects.create(block=b, floor_number=1, floor_label="1F")
    r = Room.objects.create(floor=f, room_number="999", operational_status=OperationalStatus.IN_SERVICE)
    foreign_bed = Bed.objects.create(room=r, bed_label="Bed Z", operational_status=OperationalStatus.IN_SERVICE)

    with pytest.raises(ValidationError, match="must belong to your institution"):
        create_exchange_request(
            student=student_user,
            data=_request_data(preferred_bed=foreign_bed),
        )


# =========================================================================
# 4. Request Management, Isolation & Proposal Locking
# =========================================================================

@pytest.mark.django_db
def test_eligible_student_creates_exchange_request_with_derived_occupancy(
    student_user,
    other_student_user,
    institution,
    hostel_setup,
    warden_user,
):
    occupancy = _occupy(student_user, hostel_setup, warden_user)
    assign_student_to_bed(
        student=other_student_user,
        bed=hostel_setup["bed_b"],
        created_by=warden_user,
    )
    _configure_policies(institution)
    initial_bed_id = occupancy.bed_id

    req = create_exchange_request(
        student=student_user,
        data=_request_data(preferred_bed=hostel_setup["bed_b"]),
    )

    assert req.status == ExchangeRequest.Status.MATCHED
    assert req.current_occupancy_id == occupancy.id
    assert req.student_id == student_user.id
    assert req.institution_id == student_user.institution_id
    assert req.expires_at is not None

    # Student occupancy is completely unchanged
    occupancy.refresh_from_db()
    assert occupancy.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occupancy.bed_id == initial_bed_id


@pytest.mark.django_db
def test_student_without_active_occupancy_is_rejected(student_user, institution):
    _configure_policies(institution)
    report = check_student_exchange_eligibility(student=student_user)
    assert not report.is_eligible
    assert any("active room allocation" in r for r in report.reasons)

    with pytest.raises(ValidationError, match="active room allocation"):
        create_exchange_request(student=student_user, data=_request_data())


@pytest.mark.django_db
def test_unverified_residential_eligibility_is_rejected(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution)
    student_user.residential_eligibility = "unverified"
    student_user.save(update_fields=["residential_eligibility"])

    report = check_student_exchange_eligibility(student=student_user)
    assert not report.is_eligible
    assert any("residential eligibility is not verified" in r for r in report.reasons)

    with pytest.raises(ValidationError, match="residential eligibility is not verified"):
        create_exchange_request(student=student_user, data=_request_data())


@pytest.mark.django_db
def test_student_cannot_access_or_modify_other_student_request(
    client,
    student_user,
    other_student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution)
    req = create_exchange_request(student=student_user, data=_request_data())

    # Other student cannot view detail via UI (404 scoped)
    client.force_login(other_student_user)
    detail_res = client.get(reverse("exchange:request_detail", kwargs={"pk": req.pk}))
    assert detail_res.status_code == 404

    # Other student cannot edit via service
    with pytest.raises(ValidationError, match="You can only modify your own"):
        update_exchange_request(
            exchange_request=req,
            student=other_student_user,
            data=_request_data(reason="Attempting to edit someone else's request."),
        )

    # Other student cannot withdraw via service
    with pytest.raises(ValidationError, match="You can only withdraw your own"):
        withdraw_exchange_request(exchange_request=req, student=other_student_user)


@pytest.mark.django_db
def test_request_linked_to_active_proposal_cannot_be_modified_or_withdrawn(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    occupancy = _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution)
    req = create_exchange_request(student=student_user, data=_request_data())

    proposal = ExchangeProposal.objects.create(
        institution=student_user.institution,
        status="pending_acceptance",
        cycle_length=2,
    )
    ProposalParticipant.objects.create(
        proposal=proposal,
        exchange_request=req,
        student=student_user,
        current_occupancy=occupancy,
        proposed_bed=hostel_setup["bed_b"],
        sequence_order=1,
        is_active_reservation=True,
    )

    with pytest.raises(ValidationError, match="Requests linked to active proposals cannot be modified"):
        update_exchange_request(
            exchange_request=req,
            student=student_user,
            data=_request_data(reason="Updated reason during active proposal."),
        )

    with pytest.raises(ValidationError, match="Requests linked to active proposals cannot be withdrawn"):
        withdraw_exchange_request(exchange_request=req, student=student_user)


# =========================================================================
# 5. Expiry Enforcement, Idempotency & Occupancy Invariance
# =========================================================================

@pytest.mark.django_db
def test_proactive_expiry_on_edit_and_withdraw_persists_even_with_validation_error(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution)
    req = create_exchange_request(student=student_user, data=_request_data())

    # Set created_at 35 days ago and expiry 5 days ago (satisfying expires_at >= created_at constraint)
    created_time = timezone.now() - timedelta(days=35)
    past_time = timezone.now() - timedelta(days=5)
    ExchangeRequest.objects.filter(pk=req.pk).update(
        created_at=created_time,
        expires_at=past_time,
    )
    req.refresh_from_db()

    # 1. Attempting to update triggers proactive expiry and persists EXPIRED status
    with pytest.raises(ValidationError, match="This exchange request has expired and cannot be modified"):
        update_exchange_request(
            exchange_request=req,
            student=student_user,
            data=_request_data(reason="Modifying an expired request."),
        )

    req.refresh_from_db()
    assert req.status == ExchangeRequest.Status.EXPIRED
    assert AuditLog.objects.filter(target_type="ExchangeRequest", action="EXCHANGE_REQUEST_EXPIRED").count() == 1
    assert Notification.objects.filter(
        recipient=student_user,
        notification_type=Notification.NotificationType.REQUEST_EXPIRED,
    ).count() == 1

    # 2. Attempting to withdraw also rejects and does not duplicate audit logs or notifications
    with pytest.raises(ValidationError, match="Only open exchange requests can be withdrawn"):
        withdraw_exchange_request(exchange_request=req, student=student_user)

    assert AuditLog.objects.filter(target_type="ExchangeRequest", action="EXCHANGE_REQUEST_EXPIRED").count() == 1
    assert Notification.objects.filter(
        recipient=student_user,
        notification_type=Notification.NotificationType.REQUEST_EXPIRED,
    ).count() == 1


@pytest.mark.django_db
def test_batch_expiry_service_and_celery_task_idempotent(
    student_user,
    other_student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    assign_student_to_bed(
        student=other_student_user,
        bed=hostel_setup["bed_b"],
        created_by=warden_user,
    )
    _configure_policies(institution)

    req_expired = create_exchange_request(
        student=student_user,
        data=_request_data(reason="First exchange request."),
    )
    req_valid = create_exchange_request(
        student=other_student_user,
        data=_request_data(reason="Second exchange request."),
    )

    now = timezone.now()
    ExchangeRequest.objects.filter(pk=req_expired.pk).update(
        created_at=now - timedelta(days=35),
        expires_at=now - timedelta(days=5),
    )
    ExchangeRequest.objects.filter(pk=req_valid.pk).update(expires_at=now + timedelta(days=10))

    # First run expires req_expired
    count, expired_ids = expire_exchange_requests(now=now)
    assert count == 1
    assert str(req_expired.id) in expired_ids

    req_expired.refresh_from_db()
    req_valid.refresh_from_db()
    assert req_expired.status == ExchangeRequest.Status.EXPIRED
    assert req_valid.status == ExchangeRequest.Status.OPEN

    # Idempotent second run: expires nothing, no duplicate audit logs or notifications
    count_rerun, rerun_ids = expire_exchange_requests(now=now)
    assert count_rerun == 0
    assert len(rerun_ids) == 0

    assert AuditLog.objects.filter(target_id=str(req_expired.id), action="EXCHANGE_REQUEST_EXPIRED").count() == 1
    assert Notification.objects.filter(
        recipient=student_user,
        notification_type=Notification.NotificationType.REQUEST_EXPIRED,
    ).count() == 1

    # Celery task execution succeeds
    task_result = expire_exchange_requests_task()
    assert task_result["status"] == "success"


@pytest.mark.django_db
def test_student_occupancy_remains_unchanged_across_lifecycle(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    occupancy = _occupy(student_user, hostel_setup, warden_user)
    _configure_policies(institution)
    initial_bed_id = occupancy.bed_id

    # 1. Create
    req = create_exchange_request(student=student_user, data=_request_data())
    occupancy.refresh_from_db()
    assert occupancy.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occupancy.bed_id == initial_bed_id

    # 2. Update
    update_exchange_request(
        exchange_request=req,
        student=student_user,
        data=_request_data(reason="Updated reason for better accommodation."),
    )
    occupancy.refresh_from_db()
    assert occupancy.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occupancy.bed_id == initial_bed_id

    # 3. Withdraw
    withdraw_exchange_request(exchange_request=req, student=student_user)
    occupancy.refresh_from_db()
    assert occupancy.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occupancy.bed_id == initial_bed_id
