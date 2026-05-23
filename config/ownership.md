# Ownership

Module → primary owner. Used by Phase 3 (cross-correlate) to detect when a
hot spot crosses an ownership line — e.g., a commit to `services/billing/`
authored by Karen (who owns `ingestion/`) is worth a second look.

| Module / area              | Owner   | Backup  |
|----------------------------|---------|---------|
| `services/billing/`        | john    | matt    |
| `services/auth/`           | matt    | john    |
| `web/dashboard/`           | alicia  | john    |
| `web/admin/`               | alicia  | karen   |
| `services/ingestion/`      | karen   | matt    |
| `services/notifications/`  | karen   | alicia  |
| `infra/terraform/`         | matt    | (none)  |
| `migrations/`              | john    | matt    |

Cross-ownership commits are not inherently a problem — pair work and reviews
happen. But unannounced cross-ownership work that appears in commits without
a standup mention is a YELLOW signal for `phase3_correlate`.
