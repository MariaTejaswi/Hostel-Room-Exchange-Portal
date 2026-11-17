from django import forms

from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
)

from .models import ExchangeRequest


class ExchangeRequestForm(forms.ModelForm):
    class Meta:
        model = ExchangeRequest
        fields = [
            "preferred_bed",
            "preferred_hostel",
            "preferred_block",
            "preferred_floor",
            "preferred_room_type",
            "preferred_occupancy_category",
            "preferred_accessibility_attributes",
            "reason",
        ]
        widgets = {
            "preferred_bed": forms.HiddenInput(),
            "preferred_hostel": forms.Select(attrs={"class": "form-select"}),
            "preferred_block": forms.Select(attrs={"class": "form-select"}),
            "preferred_floor": forms.Select(attrs={"class": "form-select"}),
            "preferred_room_type": forms.Select(attrs={"class": "form-select"}),
            "preferred_occupancy_category": forms.TextInput(
                attrs={"class": "form-input", "placeholder": "e.g. Standard, AC"}
            ),
            "preferred_accessibility_attributes": forms.TextInput(
                attrs={
                    "class": "form-input",
                    "placeholder": "e.g. Near elevator, wheelchair accessible",
                }
            ),
            "reason": forms.Textarea(
                attrs={
                    "class": "form-textarea",
                    "rows": 4,
                    "placeholder": "Explain why you are requesting an exchange.",
                }
            ),
        }

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.student = student
        if student and student.institution_id:
            self.instance.institution = student.institution
        self.fields["preferred_hostel"].required = False
        self.fields["preferred_block"].required = False
        self.fields["preferred_floor"].required = False
        self.fields["preferred_room_type"].required = False
        self.fields["preferred_bed"].required = False

        institution = getattr(student, "institution", None)
        eligible_categories = [
            Hostel.ResidentCategory.MALE,
            Hostel.ResidentCategory.FEMALE,
        ]
        if student and student.residential_eligibility in eligible_categories:
            eligible_categories = [student.residential_eligibility]

        self.fields["preferred_bed"].queryset = Bed.objects.filter(
            is_active=True,
            operational_status=OperationalStatus.IN_SERVICE,
            room__is_active=True,
            room__operational_status=OperationalStatus.IN_SERVICE,
            room__floor__is_active=True,
            room__floor__block__is_active=True,
            room__floor__block__hostel__institution=institution,
            room__floor__block__hostel__status=Hostel.Status.ACTIVE,
            room__floor__block__hostel__resident_category__in=eligible_categories,
        ).select_related(
            "room__floor__block__hostel",
        ).order_by(
            "room__floor__block__hostel__name",
            "room__floor__block__name",
            "room__floor__floor_number",
            "room__room_number",
            "bed_label",
        )
        self.fields["preferred_hostel"].queryset = Hostel.objects.filter(
            institution=institution,
            status=Hostel.Status.ACTIVE,
            resident_category__in=eligible_categories,
        ).order_by("name")
        self.fields["preferred_block"].queryset = Block.objects.filter(
            hostel__institution=institution,
            hostel__status=Hostel.Status.ACTIVE,
            hostel__resident_category__in=eligible_categories,
            is_active=True,
        ).select_related("hostel").order_by("hostel__name", "name")
        self.fields["preferred_floor"].queryset = Floor.objects.filter(
            block__hostel__institution=institution,
            block__hostel__status=Hostel.Status.ACTIVE,
            block__hostel__resident_category__in=eligible_categories,
            block__is_active=True,
            is_active=True,
        ).select_related("block__hostel").order_by(
            "block__hostel__name",
            "block__name",
            "floor_number",
        )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if len(reason) < 10:
            raise forms.ValidationError("Please provide at least 10 characters for the exchange reason.")
        return reason

    def clean(self):
        cleaned_data = super().clean()
        preferred_bed = cleaned_data.get("preferred_bed")
        hostel = cleaned_data.get("preferred_hostel")
        block = cleaned_data.get("preferred_block")
        floor = cleaned_data.get("preferred_floor")

        if preferred_bed:
            # Bed is authoritative: resolve destination relationships from database
            bed_hostel = preferred_bed.room.floor.block.hostel
            bed_block = preferred_bed.room.floor.block
            bed_floor = preferred_bed.room.floor

            cleaned_data["preferred_hostel"] = bed_hostel
            cleaned_data["preferred_block"] = bed_block
            cleaned_data["preferred_floor"] = bed_floor

            if not cleaned_data.get("preferred_room_type"):
                cleaned_data["preferred_room_type"] = preferred_bed.room.room_type
            if not cleaned_data.get("preferred_occupancy_category"):
                cleaned_data["preferred_occupancy_category"] = preferred_bed.room.occupancy_category or "Standard"
        else:
            # Preferences mode validation
            if hostel and block and block.hostel_id != hostel.id:
                self.add_error("preferred_block", "Select a block from the preferred hostel.")
            if block and floor and floor.block_id != block.id:
                self.add_error("preferred_floor", "Select a floor from the preferred block.")

        return cleaned_data


class ExchangePolicySettingsForm(forms.Form):
    """Configuration form for Hostel Administrators to update institution exchange policies."""

    cooling_off_days = forms.IntegerField(
        min_value=0,
        label="Cooling-off Period (Days)",
        help_text="Days a student must reside in their assigned room before becoming eligible for an exchange. Set to 0 to disable.",
        widget=forms.NumberInput(attrs={"class": "form-input", "min": "0"}),
    )
    request_validity_days = forms.IntegerField(
        min_value=1,
        label="Request Validity Window (Days)",
        help_text="Number of days before an open exchange request expires automatically.",
        widget=forms.NumberInput(attrs={"class": "form-input", "min": "1"}),
    )
    require_fee_clearance = forms.BooleanField(
        required=False,
        label="Require Fee Clearance",
        help_text="Mandates verified fee clearance from institutional accounts before submitting an exchange request.",
        widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
    )
    require_no_disciplinary_hold = forms.BooleanField(
        required=False,
        label="Require No Disciplinary Hold",
        help_text="Blocks exchange requests for students with active disciplinary holds.",
        widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
    )

