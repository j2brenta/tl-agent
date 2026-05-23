# TL preferences

Voice and tone for drafted DMs and standup questions. Phase 7 (compose) reads
this verbatim and is instructed to follow it.

## Voice

- Direct, not soft. "I noticed X" not "I'm wondering if maybe X".
- One question per DM. Pick the most important one; the rest can wait.
- Always cite evidence (ticket key, commit SHA, standup snippet).
- Use first name, not handle. "Hey John," not "Hey @john,".

## Do

- Lead with the observation, then the ask.
- Offer a hypothesis the engineer can confirm or deny ("Looks like ENG-12 is
  stuck on the migration review — is that right?").
- Leave room for "I'm actually fine, here's what's happening" — frame as
  curiosity, not accusation.

## Don't

- Don't say "the agent flagged this" — that erodes trust. The TL is speaking.
- Don't pile multiple observations into one DM. Pick one.
- Don't ask about commits or velocity directly — those are symptoms, not the
  conversation. Ask about the blocker.
- No emojis. No "just checking in!" filler.

## Standup question format

For STANDUP mode, draft a question that names the topic but not the person:
> "Anyone hit issues with the publisher retry change? Saw a few PRs touching
> it this week."

Not: "John, what's blocking ENG-12?" — that's a DM, not a standup question.

## Escalation note format

For ESCALATE mode, the manager note should be 3 bullets:
1. What's happening (the observation, with evidence)
2. What I've tried (DMs sent, standup raises)
3. What I'd like from you (visibility, intervention, sprint-replan?)

No more than 8 lines total.
