from django import forms
from django.utils import timezone
from .models import Hostel, Block, Floor, Room, Bed, StudentOccupancy, OperationalStatus
from .selectors import get_eligible_unallocated_students, get_vacant_beds
from apps.accounts.models import User


class HostelForm(forms.ModelForm):
    """Form for creating and editing hostels."""

    class Meta:
        model = Hostel
        fields = ["name", "code", "resident_category", "address", "description", "status"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. North Residence"}),
            "code": forms.TextInput(attrs={"class": "form-input uppercase", "placeholder": "e.g. NR-01"}),
            "resident_category": forms.Select(attrs={"class": "form-select"}),
            "address": forms.TextInput(attrs={"class": "form-input", "placeholder": "Campus location / address"}),
            "description": forms.Textarea(
                attrs={"class": "form-textarea", "rows": 3, "placeholder": "Hostel description and facilities"}
            ),
            "status": forms.Select(attrs={"class": "form-select"}),
        }

    def clean_code(self):
        code = self.cleaned_data.get("code")
        return code.strip().upper() if code else ""


class RoomForm(forms.ModelForm):
    """Form for creating a new room."""

    create_default_beds = forms.BooleanField(
        required=False,
        initial=True,
        label="Automatically generate bed records matching room capacity",
        widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
    )

    class Meta:
        model = Room
        fields = [
            "floor",
            "room_number",
            "room_type",
            "occupancy_category",
            "accessibility_attributes",
            "operational_status",
            "description",
        ]
        widgets = {
            "floor": forms.Select(attrs={"class": "form-select"}),
            "room_number": forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. 101, 204"}),
            "room_type": forms.Select(attrs={"class": "form-select"}),
            "occupancy_category": forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. Standard, AC"}),
            "accessibility_attributes": forms.TextInput(
                attrs={"class": "form-input", "placeholder": "e.g. Ground Floor, Wheelchair ramp"}
            ),
            "operational_status": forms.Select(attrs={"class": "form-select"}),
            "description": forms.Textarea(
                attrs={"class": "form-textarea", "rows": 2, "placeholder": "Room notes"}
            ),
        }

    def __init__(self, *args, institution=None, **kwargs):
        super().__init__(*args, **kwargs)
        if institution:
            self.fields["floor"].queryset = Floor.objects.filter(
                block__hostel__institution=institution
            ).select_related("block__hostel")


class BedStatusForm(forms.ModelForm):
    """Form to toggle or change bed operational status."""

    class Meta:
        model = Bed
        fields = ["operational_status"]
        widgets = {
            "operational_status": forms.Select(attrs={"class": "form-select"}),
        }


class OccupancyAssignForm(forms.Form):
    """Form to allocate an eligible unassigned student to a vacant bed."""

    student = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Student",
        widget=forms.Select(attrs={"class": "form-select"}),
        empty_label="Select an eligible student...",
    )
    bed = forms.ModelChoiceField(
        queryset=Bed.objects.none(),
        label="Available Bed",
        widget=forms.Select(attrs={"class": "form-select"}),
        empty_label="Select a vacant bed...",
    )
    start_date = forms.DateField(
        label="Allocation Start Date",
        initial=timezone.now().date,
        widget=forms.DateInput(attrs={"class": "form-input", "type": "date"}),
    )
    source = forms.ChoiceField(
        choices=StudentOccupancy.AllocationSource.choices,
        initial=StudentOccupancy.AllocationSource.ADMINISTRATIVE,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, institution=None, **kwargs):
        super().__init__(*args, **kwargs)
        if institution:
            self.fields["student"].queryset = get_eligible_unallocated_students(institution)
            self.fields["bed"].queryset = get_vacant_beds(institution)


class OccupancyReleaseForm(forms.Form):
    """Form to record the release of a student from their bed allocation."""

    end_date = forms.DateField(
        label="Release Date",
        initial=timezone.now().date,
        widget=forms.DateInput(attrs={"class": "form-input", "type": "date"}),
    )
    release_reason = forms.CharField(
        label="Reason for Release",
        min_length=5,
        widget=forms.Textarea(
            attrs={
                "class": "form-textarea",
                "rows": 3,
                "placeholder": "Provide an official reason (e.g. Semester completion, Academic withdrawal, Disciplinary, Voluntary checkout)",
            }
        ),
    )
