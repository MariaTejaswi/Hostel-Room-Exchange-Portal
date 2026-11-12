from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = "accounts"

urlpatterns = [
    # Authentication
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    # Dashboards
    path("dashboard/", views.dashboard_router, name="dashboard"),
    path("dashboard/student/", views.student_dashboard_view, name="dashboard_student"),
    path("dashboard/warden/", views.warden_dashboard_view, name="dashboard_warden"),
    path("dashboard/chief-warden/", views.chief_warden_dashboard_view, name="dashboard_chief_warden"),
    path("dashboard/admin/", views.hostel_admin_dashboard_view, name="dashboard_admin"),
    path("dashboard/dean/", views.dean_dashboard_view, name="dashboard_dean"),
    # Profile & Password Management
    path("profile/", views.profile_view, name="profile"),
    path("password-change/", views.password_change_view, name="password_change"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset_form.html",
            email_template_name="accounts/password_reset_email.html",
            subject_template_name="accounts/password_reset_subject.txt",
            success_url="/password-reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "password-reset-confirm/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url="/password-reset/complete/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
]
