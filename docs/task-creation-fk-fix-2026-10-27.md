# Interview task creation: PostgreSQL foreign-key fix

## Verified failure

Production `POST /api/v1/interview-tasks` failed at the initial flush with
`applications_candidate_id_fkey`. A new Candidate and Application were added
together without ORM relationships defining their flush dependency; Application
was inserted first. SQLite tests did not enable foreign-key enforcement, masking
the bug. An isolated FK-enabled SQLite reproduction failed the same way.

## Change

Flush the new candidate and any pending job before adding the Application.
Do not commit early: candidate, job, application, rounds and assignments remain
one transaction. No schema, scoring, AI provider or production configuration change.

## Regression coverage and results

`backend/tests/test_task_creation_integrity.py` verifies:

- New candidate and new job: create task, load workbench, acknowledge notice and start.
- New candidate and existing job: same flow.
- Failure assigning a later round: all task records roll back.
- Failure generating a later round's plan: all task records roll back.

The tests explicitly enable SQLite foreign keys. They can also run against a
caller-provided disposable PostgreSQL database via `COPILOT_TASK_TEST_DATABASE_URL`;
the database name must begin with `interview_copilot_test_`. No real candidate
data or external model calls are used.

Verified on 2026-10-27 in an isolated Linux source directory with the installed
server dependencies (not against the production business database):

- SQLite FK regression: 4 passed.
- Disposable PostgreSQL regression: 4 passed; the created test database was removed.
- Full backend suite: 227 passed, 2 existing dependency deprecation warnings.
- `git diff --check`: passed.

Local Windows tests could not run: sandbox loopback preflight was blocked, and
the approved run then encountered Windows application-control blocking the
Pydantic native dependency. No security policy was disabled; Linux validation
above is the evidence for this change. Passing tests does not by itself confirm
deployment or a real-user browser submission.
