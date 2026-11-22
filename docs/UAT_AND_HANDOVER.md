# UAT and Deployment Handover

Last verified: 21 November 2026

## Automated UAT evidence

The release suite verifies student, assigned-Warden, Chief-Warden, Hostel Administrator,
Dean, and System Administrator dashboards; two- through five-person cycles; proposal
acceptance, decline, expiry, approval, rejection, execution, notification badges, reporting,
audit history, authorization, institution isolation, and PostgreSQL concurrency. See
`RELEASE_READINESS.md` for exact results.

Shared accessibility protections include a keyboard skip link, a focusable main landmark,
accessible names on icon-only global controls, visible focus rings, and live-region toast
announcements. Automated template tests protect the shared dashboard baseline.

## Target-environment acceptance checklist

The deployment owner must record the environment-specific evidence below without placing
credentials or secrets in this repository.

- [ ] Production/staging URL and release identifier recorded externally.
- [ ] Student completes one controlled exchange request.
- [ ] Same-hostel proposal appears only for the assigned Warden.
- [ ] Cross-hostel proposal appears only for the Chief Warden.
- [ ] Participant response and authority decision notifications appear with correct badges.
- [ ] Approved exchange updates occupancy and creates transfer/audit records.
- [ ] Keyboard-only navigation reaches skip link, sidebar, notification menu, forms, and actions.
- [ ] Screen-reader spot check announces headings, form errors, notification count, and toasts.
- [ ] Mobile and desktop layouts are visually approved, including coffee-brown Warden styling.
- [ ] Report totals, date filters, exports, and institution boundaries match controlled data.

## Deployment sequence

1. Confirm backups and rollback owner.
2. Supply environment variables through the deployment platform; never copy `.env` into an artifact.
3. Apply migrations with the release image.
4. Collect static assets and start web, worker, and scheduled-task processes.
5. Run Django deployment checks and health checks.
6. Complete the controlled acceptance checklist above.

## Rollback

Stop new workers, restore the previous application image, and follow the migration-specific
rollback plan reviewed for the release. Restore database backups only under the designated
database owner's authority. Preserve audit and transfer records for incident review.
