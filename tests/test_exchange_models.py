from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.exchange.models import (
    ExchangeProposal,
    ExchangeRequest,
    PolicyRule,
    ProposalParticipant,
)
from apps.hostels.services import assign_student_to_bed



def _create_request(student, institution, occupancy, **overrides):
    values = {
        "institution": institution,
        "student": student,
        "current_occupancy": occupancy,
        "reason": "Needs a room closer to academic facilities.",
        "expires_at": timezone.now() + timedelta(days=14),
    }
    values.update(overrides)
    return ExchangeRequest.objects.create(**values)


@pytest.mark.django_db
def test_exchange_request_reuses_active_occupancy_and_prevents_duplicate_open_request(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    occupancy = assign_student_to_bed(
        student=student_user,
        bed=hostel_setup["bed_a"],
        created_by=warden_user,
    )
    request = _create_request(student_user, institution, occupancy)

    assert request.current_occupancy == occupancy
    assert request.status == ExchangeRequest.Status.OPEN

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _create_request(student_user, institution, occupancy)


@pytest.mark.django_db
def test_exchange_proposal_supports_two_to_five_participant_cycle_lengths(institution):
    for cycle_length in (2, 3, 4, 5):
        proposal = ExchangeProposal.objects.create(
            institution=institution,
            cycle_length=cycle_length,
        )
        assert proposal.cycle_length == cycle_length

    with pytest.raises(ValidationError):
        ExchangeProposal(institution=institution, cycle_length=1).full_clean()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ExchangeProposal.objects.create(institution=institution, cycle_length=6)


@pytest.mark.django_db
def test_active_participant_reservation_cannot_overlap_another_proposal(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    occupancy = assign_student_to_bed(
        student=student_user,
        bed=hostel_setup["bed_a"],
        created_by=warden_user,
    )
    request = _create_request(student_user, institution, occupancy)
    first_proposal = ExchangeProposal.objects.create(institution=institution, cycle_length=2)
    second_proposal = ExchangeProposal.objects.create(institution=institution, cycle_length=2)

    ProposalParticipant.objects.create(
        proposal=first_proposal,
        exchange_request=request,
        student=student_user,
        current_occupancy=occupancy,
        proposed_bed=hostel_setup["bed_b"],
        sequence_order=0,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ProposalParticipant.objects.create(
                proposal=second_proposal,
                exchange_request=request,
                student=student_user,
                current_occupancy=occupancy,
                proposed_bed=hostel_setup["bed_b"],
                sequence_order=0,
            )


@pytest.mark.django_db
def test_policy_rule_is_unique_per_institution(institution):
    PolicyRule.objects.create(
        institution=institution,
        key=PolicyRule.Key.MAX_CYCLE_LENGTH,
        value={"value": 5},
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PolicyRule.objects.create(
                institution=institution,
                key=PolicyRule.Key.MAX_CYCLE_LENGTH,
                value={"value": 4},
            )
