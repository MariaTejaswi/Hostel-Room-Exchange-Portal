from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm, PasswordChangeForm
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
