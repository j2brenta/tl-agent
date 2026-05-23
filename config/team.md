# Team

The four engineers under the TL. Each section is parsed by
`storage.markdown_loader.load_team()` into `models.Engineer`.

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
