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
    path("dashboard/sysadmin/", views.sysadmin_dashboard_view, name="dashboard_sysadmin"),
    path("dashboard/dean/", views.dean_dashboard_view, name="dashboard_dean"),
    path("dashboard/api/stats/", views.dashboard_stats_api_view, name="dashboard_stats_api"),
    # Profile & Password Management
    path("profile/", views.profile_view, name="profile"),
    path("password-change/", views.password_change_view, name="password_change"),
    # First-time Student Password Setup
    path("password-setup/<str:uidb64>/<str:token>/", views.password_setup_view, name="password_setup"),
    # Standard Password Reset
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
    # System Administrator: Student Account Management
    path("students/", views.student_list_view, name="student_list"),
    path("students/add/", views.student_create_view, name="student_create"),
    path("students/import/", views.student_csv_import_view, name="student_import"),
    path("students/import/template/", views.student_csv_template_download_view, name="student_csv_template"),
    path("students/<uuid:pk>/", views.student_detail_view, name="student_detail"),
    path("students/<uuid:pk>/edit/", views.student_edit_view, name="student_edit"),
    path("students/<uuid:pk>/toggle-status/", views.student_toggle_status_view, name="student_toggle_status"),
    path("students/<uuid:pk>/password-reset/", views.student_password_reset_initiate_view, name="student_password_reset"),
    # Hostel Administrator: Residential Eligibility Verification
    path("eligibility-verification/", views.eligibility_verification_list_view, name="eligibility_verification_list"),
    path("eligibility-verification/<uuid:pk>/", views.eligibility_verify_student_view, name="eligibility_verify_student"),
]
