# Team

The full roster — team lead, product manager, and the four engineers. Each
section is parsed by `storage.markdown_loader.load_team()` into
`models.Engineer`. The `role_kind` bullet (`team_lead` / `product_manager` /
`engineer`) decides how the workflow treats each person; it defaults to
`engineer` when omitted. Per-system logins (`jira_account_id`,
`gitlab_username`, `chat_user_id`) capture handles that differ across systems.

---

## Sprint scope

Reserved (non-person) section. Defines which Jira sprints "belong to the team"
so `sprint_select` can discover the current sprint instead of blindly trusting
Jira's active flag. A sprint is in scope when it lives on `board_id` **and** its
name matches `sprint_name_pattern` (a Python regex). The operative sprint is the
single in-scope one in the `active` state; zero or several active matches raises
a human decision on the Workflow tab.

- **board_id:** ENG
- **sprint_name_pattern:** Eng Sprint .*

---

## Kirill

- **id:** kirill
- **display_name:** Kirill
- **role:** team lead
- **role_kind:** team_lead
- **jira_account_id:** kirill
- **gitlab_username:** kirill
- **chat_user_id:** kirill
- **email:** kirill@example.local

The TL running this workflow. Not triaged like an IC; listed so the roster and
identity mapping are complete.

---

## Dana

- **id:** dana
- **display_name:** Dana Park
- **role:** product manager
- **role_kind:** product_manager
- **jira_account_id:** dpark
- **gitlab_username:** dpark
- **chat_user_id:** dana
- **email:** dana@example.local
- **aliases:** dpark

Product manager. Jira/GitLab handle (`dpark`) differs from the `id` (`dana`) —
resolution falls back to the alias.

---

## John

- **id:** john
- **display_name:** John Doe
- **role:** senior backend
- **jira_account_id:** john
- **gitlab_username:** john
- **chat_user_id:** john
- **email:** john@example.local
- **aliases:** jdoe, johnny

Baseline: usually verbose in standups (3-5 lines), commits in the morning
window. Quiet days are an unusual signal — surface them.

---

## Matt

- **id:** matt
- **display_name:** Matt Stone
- **role:** senior backend
- **jira_account_id:** matt
- **gitlab_username:** matt
- **chat_user_id:** matt
- **email:** matt@example.local
- **aliases:** mstone

Baseline: terse standups (1-2 lines), commits cluster late afternoon. Hedging
language ("trying to", "looking into") day-over-day is the signal to watch.

---

## Alicia

- **id:** alicia
- **display_name:** Alicia Park
- **role:** staff frontend
- **jira_account_id:** alicia
- **gitlab_username:** alicia
- **chat_user_id:** alicia
- **email:** alicia@example.local
- **aliases:** apark

Baseline: standups always include a "blocker / risk" section even when none
exists. Absence of that section = signal.

---

## Karen

- **id:** karen
- **display_name:** Karen Liu
- **role:** mid backend
- **jira_account_id:** karen
- **gitlab_username:** karen
- **chat_user_id:** karen
- **email:** karen@example.local
- **aliases:** kliu

Baseline: still ramping; first sprint as lead on the ingestion module. Scope
creep is more likely than for the others — diff size relative to estimate
deserves weight.
