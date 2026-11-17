from django import forms
from django.contrib.auth import authenticate, password_validation
from django.core.exceptions import ValidationError
from .models import User


class LoginForm(forms.Form):
    """User login form supporting email-based credentials with remember-me checkbox."""

    email = forms.EmailField(
        label="Institutional Email",
        widget=forms.EmailInput(
            attrs={
                "class": "form-input",
                "placeholder": "name@university.edu",
                "autocomplete": "email",
                "required": True,
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-input",
                "placeholder": "Enter your password",
                "autocomplete": "current-password",
                "required": True,
            }
        ),
    )
    remember_me = forms.BooleanField(
        required=False,
        initial=False,
        label="Remember this device",
        widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        email = cleaned_data.get("email")
        password = cleaned_data.get("password")

        if email and password:
            self.user = authenticate(username=email.strip().lower(), password=password)
            if self.user is None:
                raise forms.ValidationError(
                    "Invalid email or password. Please verify your credentials and try again."
                )
            if not self.user.is_active:
                raise forms.ValidationError(
                    "This account is currently inactive. Please contact your hostel administrator."
                )
        return cleaned_data


class UserProfileForm(forms.ModelForm):
    """Form allowing users to update only authorized self-service profile attributes."""

    class Meta:
        model = User
        fields = ["full_name", "phone_number", "avatar"]
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "form-input", "placeholder": "Full legal name"}),
            "phone_number": forms.TextInput(attrs={"class": "form-input", "placeholder": "+1 (555) 000-0000"}),
            "avatar": forms.FileInput(attrs={"class": "form-file-input"}),
        }

    def clean_full_name(self):
        name = self.cleaned_data.get("full_name")
        if not name or len(name.strip()) < 2:
            raise forms.ValidationError("Please provide a valid full name.")
        return name.strip()


class StudentCreateForm(forms.Form):
    """Form for System Administrators to create an individual student account."""

    full_name = forms.CharField(
        max_length=150,
        label="Full Legal Name",
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. John Doe", "required": True}),
    )
    registration_number = forms.CharField(
        max_length=50,
        label="Roll / Registration Number",
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. 2024CS001", "required": True}),
    )
    email = forms.EmailField(
        label="Institutional Email Address",
        widget=forms.EmailInput(attrs={"class": "form-input", "placeholder": "e.g. john.doe@university.edu", "required": True}),
    )
    department = forms.CharField(
        max_length=100,
        required=False,
        label="Department / Branch",
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. Computer Science & Engineering"}),
    )
    program = forms.CharField(
        max_length=100,
        required=False,
        label="Degree / Academic Program",
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. B.Tech, M.Sc, Ph.D"}),
    )
    academic_year = forms.ChoiceField(
        choices=[("", "Select Academic Year")] + list(User.AcademicYear.choices),
        required=False,
        label="Year of Study / Cohort",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    phone_number = forms.CharField(
        max_length=20,
        required=False,
        label="Contact Phone Number",
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. +91 9876543210"}),
    )
    claimed_residential_category = forms.ChoiceField(
        choices=User.ClaimedResidentialCategory.choices,
        initial=User.ClaimedResidentialCategory.UNSPECIFIED,
        label="Initial Claimed Residential Category",
        help_text="Unverified student claim. Official residential eligibility will remain unverified until verified by Hostel Administration.",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, institution=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.institution = institution

    def clean_full_name(self):
        name = self.cleaned_data.get("full_name", "").strip()
        if len(name) < 2:
            raise forms.ValidationError("Full name must be at least 2 characters long.")
        return name

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not self.institution:
            raise forms.ValidationError("Institution context is required.")
        if User.objects.filter(institution=self.institution, email=email).exists():
            raise forms.ValidationError(f"A user with email '{email}' already exists in this institution.")
        return email

    def clean_registration_number(self):
        reg_no = self.cleaned_data.get("registration_number", "").strip().upper()
        if not self.institution:
            raise forms.ValidationError("Institution context is required.")
        if User.objects.filter(institution=self.institution, registration_number=reg_no).exists():
            raise forms.ValidationError(f"A student with registration number '{reg_no}' already exists in this institution.")
        return reg_no


class StudentEditForm(forms.Form):
    """Form for System Administrators to edit an existing student account."""

    full_name = forms.CharField(
        max_length=150,
        label="Full Legal Name",
        widget=forms.TextInput(attrs={"class": "form-input", "required": True}),
    )
    registration_number = forms.CharField(
        max_length=50,
        label="Roll / Registration Number",
        widget=forms.TextInput(attrs={"class": "form-input", "required": True}),
    )
    email = forms.EmailField(
        label="Institutional Email Address",
        widget=forms.EmailInput(attrs={"class": "form-input", "required": True}),
    )
    department = forms.CharField(
        max_length=100,
        required=False,
        label="Department / Branch",
        widget=forms.TextInput(attrs={"class": "form-input"}),
    )
    program = forms.CharField(
        max_length=100,
        required=False,
        label="Degree / Academic Program",
        widget=forms.TextInput(attrs={"class": "form-input"}),
    )
    academic_year = forms.ChoiceField(
        choices=[("", "Select Academic Year")] + list(User.AcademicYear.choices),
        required=False,
        label="Year of Study / Cohort",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    phone_number = forms.CharField(
        max_length=20,
        required=False,
        label="Contact Phone Number",
        widget=forms.TextInput(attrs={"class": "form-input"}),
    )
    claimed_residential_category = forms.ChoiceField(
        choices=User.ClaimedResidentialCategory.choices,
        label="Initial Claimed Residential Category",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    change_reason = forms.CharField(
        max_length=500,
        required=False,
        label="Reason for Administrative Correction",
        help_text="Mandatory if modifying identity fields (full name, registration number, or email).",
        widget=forms.Textarea(attrs={"class": "form-textarea", "rows": 2, "placeholder": "Explain reason for identity updates (min 5 characters)..."}),
    )

    def __init__(self, *args, instance=None, institution=None, **kwargs):
        self.instance = instance
        self.institution = institution or (instance.institution if instance else None)
        if instance and "initial" not in kwargs:
            kwargs["initial"] = {
                "full_name": instance.full_name,
                "registration_number": instance.registration_number,
                "email": instance.email,
                "department": instance.department,
                "program": instance.program,
                "academic_year": instance.academic_year,
                "phone_number": instance.phone_number,
                "claimed_residential_category": instance.claimed_residential_category,
            }
        super().__init__(*args, **kwargs)

    def clean_full_name(self):
        name = self.cleaned_data.get("full_name", "").strip()
        if len(name) < 2:
            raise forms.ValidationError("Full name must be at least 2 characters long.")
        return name

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if self.institution and self.instance:
            qs = User.objects.filter(institution=self.institution, email=email).exclude(id=self.instance.id)
            if qs.exists():
                raise forms.ValidationError(f"A user with email '{email}' already exists in this institution.")
        return email

    def clean_registration_number(self):
        reg_no = self.cleaned_data.get("registration_number", "").strip().upper()
        if self.institution and self.instance:
            qs = User.objects.filter(institution=self.institution, registration_number=reg_no).exclude(id=self.instance.id)
            if qs.exists():
                raise forms.ValidationError(f"A student with registration number '{reg_no}' already exists in this institution.")
        return reg_no

    def clean(self):
        cleaned_data = super().clean()
        if not self.instance:
            return cleaned_data

        name = cleaned_data.get("full_name")
        reg_no = cleaned_data.get("registration_number")
        email = cleaned_data.get("email")
        reason = cleaned_data.get("change_reason", "").strip()

        # Check if identity fields were changed
        identity_changed = (
            (name and name != self.instance.full_name)
            or (reg_no and reg_no != self.instance.registration_number)
            or (email and email != self.instance.email)
        )

        if identity_changed:
            if not reason or len(reason) < 5:
                self.add_error(
                    "change_reason",
                    "A mandatory administrative explanation (at least 5 characters) is required when updating identity fields (name, registration number, or email).",
                )
        return cleaned_data


class StudentStatusToggleForm(forms.Form):
    """Form to confirm activation or deactivation of a student account."""

    reason = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "form-textarea",
                "rows": 3,
                "placeholder": "Mandatory explanation for account deactivation (min 5 characters)...",
            }
        ),
        required=False,
        label="Administrative Reason",
    )

    def __init__(self, *args, is_deactivation=False, **kwargs):
        self.is_deactivation = is_deactivation
        super().__init__(*args, **kwargs)
        if is_deactivation:
            self.fields["reason"].required = True

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if self.is_deactivation and len(reason) < 5:
            raise forms.ValidationError("A reason of at least 5 characters is required for account deactivation.")
        return reason


class EligibilityVerificationForm(forms.Form):
    """Form for Hostel Administrators to verify or update student residential eligibility."""

    new_eligibility = forms.ChoiceField(
        choices=User.ResidentialEligibility.choices,
        label="Authoritative Residential Eligibility",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "form-textarea",
                "rows": 3,
                "placeholder": "Enter mandatory verification / justification notes (min 5 characters)...",
                "required": True,
            }
        ),
        min_length=5,
        label="Verification Notes & Justification",
        help_text="Mandatory explanation recorded in the permanent audit trail.",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if len(reason) < 5:
            raise forms.ValidationError("Verification reason must be at least 5 characters long.")
        return reason


class StudentCSVUploadForm(forms.Form):
    """Form for uploading student roster CSV."""

    csv_file = forms.FileField(
        label="Select CSV Roster File",
        widget=forms.FileInput(attrs={"class": "form-file-input", "accept": ".csv,text/csv"}),
        help_text="Upload a UTF-8 encoded .csv file (maximum 500 rows, 2MB limit).",
    )

    def clean_csv_file(self):
        file = self.cleaned_data.get("csv_file")
        if not file:
            raise forms.ValidationError("Please select a file to upload.")

        name = file.name.lower()
        if not name.endswith(".csv"):
            raise forms.ValidationError("Only .csv files are supported. Please upload a valid CSV file.")

        if file.size > 2 * 1024 * 1024:
            raise forms.ValidationError("File size exceeds the 2MB limit.")

        return file


class PasswordSetupForm(forms.Form):
    """First-time password setup form for newly provisioned student accounts."""

    new_password1 = forms.CharField(
        label="New Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-input",
                "placeholder": "Enter your new secure password",
                "autocomplete": "new-password",
                "required": True,
            }
        ),
    )
    new_password2 = forms.CharField(
        label="Confirm New Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-input",
                "placeholder": "Confirm your new secure password",
                "autocomplete": "new-password",
                "required": True,
            }
        ),
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get("new_password1")
        p2 = cleaned_data.get("new_password2")

        if p1 and p2:
            if p1 != p2:
                self.add_error("new_password2", "The two password fields didn't match.")
            else:
                try:
                    password_validation.validate_password(p1, self.user)
                except ValidationError as e:
                    self.add_error("new_password1", e)
        return cleaned_data
