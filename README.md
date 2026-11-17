# openIMIS Backend deduplication reference module

## Legacy deduplication (beneficiaries, benefit payments)

Summary-based duplicate detection over `Beneficiary` and `BenefitConsumption`, reviewed
through `tasks_management` tasks (`CreateDeduplicationReviewTasksService`,
`CreateDeduplicationPaymentReviewTasksService`) and merged by
`merge_duplicate_beneficiaries` / `remove_duplicate_benefit_payments` on task completion.
GraphQL: `beneficiaryDeduplicationSummary`, `benefitDeduplicationSummary`,
`createDeduplicationTasks`, `createDeduplicationPaymentTasks`.

For a user without the masked-data right (core `gql_query_enable_viewing_masked_data_perms`,
900101), a summary column that reads a field a registered mask masks gives the string mask
in `columnValues`: a column of the beneficiary or benefit, of its person (`individual`'s
person mask, with the fields other modules register), or a `json_ext` key masked on either.
A column reading the beneficiary's or benefit's own `json_ext` (a bare key, `json_ext__<key>`
or `json_ext`) is also checked against the person's `json_ext`, which it may copy.
Grouping and counts are computed on the stored values.

The `businessData` of the beneficiary and payment review tasks is masked the same way for a
reader without 900101: each beneficiary or benefit row, its person, and the summary row
(`column_values`) the task was made from.

A summary row is given only when the user may see every beneficiary (`Beneficiary.get_queryset`)
or every benefit's person (`Individual.get_queryset`) of its `ids`; a row with one record out of
the user's locations is left out. `createDeduplicationTasks` and `createDeduplicationPaymentTasks` apply
the same rule to the rows they are given: a row naming a beneficiary, or a benefit whose person, the user
may not see gets no review task (and its benefits are not flagged `duplicated`), and the other rows get theirs.

A beneficiary review task records in `data.current_state` the `status` and `version` of each of
its beneficiaries when it is created.

`merge_duplicate_beneficiaries` (completion of a beneficiary review task) merges what the completing
user recorded when resolving the task, their entry in `additional_resolve_data` of its `json_ext`
(`{"values", "beneficiaryIds"}`): `beneficiaryIds` are among the task's own `data.ids` (the rows the
reviewer leaves out of the merge are kept), and two or more of them are not deleted (a row deleted since
the task was created, by another merge among others, takes no part). It runs between the
enrolment hooks of the beneficiaries' benefit plans (`social_protection.enrolment_hooks.enrolment_write`):
it deletes the other beneficiaries, then writes the merged `status` and `json_ext` of the oldest one
through `BeneficiaryService.update`, sending each only when the merge changes it, so the hooks'
`before_service_write` checks and adjusts that write as for the service's other callers. It writes
nothing when the completing user recorded no merge, names a beneficiary the task does not or fewer
than two undeleted ones, when that user may not see one of the beneficiaries (`Beneficiary.get_queryset`),
when the kept beneficiary no longer has the `status` and `version` its task recorded (a write since
the task was created, such as a suspension, calls for a new review; a task without `current_state`
is not checked), when the merged `json_ext` it writes does not satisfy the plan's
`beneficiary_data_schema` (JSON Schema draft 7, the whole `json_ext` checked; the reason names the
property and the rule, not the value), or when a hook refuses the write. A refused or failed merge rolls
back whole and the task ends `FAILED` instead of `COMPLETED`, with the reason under `completion_error` of
its `json_ext` (as `tasks_management` does for a refused approval).

`remove_duplicate_benefit_payments` (completion of a payment review task) removes nothing when the
completing user may not see the person of one of the benefits it removes (`Individual.get_queryset`);
a refused or failed removal rolls back whole and fails the task the same way.

## Candidate sources

`deduplication.sources` is the seam other modules (e.g. `biometric_verification`) use to
feed duplicate-subject candidates into this module, without a hard dependency in either
direction:

- `Watermark(updated_at, last_id)` — a scan cursor.
- `Candidate(subject_model, subject_a, subject_b, kind, score, evidence)` — one suspected
  duplicate pair; `subject_a < subject_b` as strings, `kind` identifies the detector
  (`"demographic"`, `"identifier"`, `"biometric"`, or any other registered kind).
- `CandidateSource` — ABC with `scan(since)` and `watermark()`, and `covered(subject_model, subject_ids,
  state)`, which returns the ids among `subject_ids`, as passed, that the source's last scan covered,
  `state` being its `ScanState` row. The default is the rule of the subject sources: a subject is covered
  when its `(date_updated, id)` is at or before the cursor; an empty cursor covers nobody. A source whose
  cursor is not a subject's (`biometric`) overrides it.
- `register(source)` / `sources()` — a process-wide registry, in registration order.
- `order_pair(a, b)` — sorts two subject ids into the canonical `(a, b)` order.
- `register_subject_filter(key, subject_filter)` — registers
  `subject_filter(subject_model, subject_ids)`, which returns the ids among `subject_ids` that
  scans leave out; `None` removes the key. `run_scan` and `scan_subject` record no candidate
  with a subject a registered filter leaves out, whatever source found it; the filters are
  asked once per batch of 500 candidates. A scan's cursor still moves past the subjects left
  out.
- `rescan(subject_model, subject_ids)` — moves every source cursor that is past the creation of
  the earliest of `subject_ids` back before it, so each source's next scan revisits them, for
  example once a filter no longer leaves them out. A source's cursor must order rows written no
  earlier than their subject's `date_created`. A scan locks its source's `ScanState` row for its
  transaction, and `rescan` waits for it.

Two built-in sources are registered automatically (`DeduplicationConfig.ready()`):

- `DemographicSource` (`deduplication/sources/demographic.py`) groups subjects sharing the
  same values on `demographic_columns` (module config, default `first_name`, `last_name`,
  `dob`; a name not on the subject model is read as a `json_ext` key).
- `IdentifierSource` (`deduplication/sources/identifier.py`) matches subjects on
  `identifier_keys` `json_ext` keys, normalised (trimmed, case-folded). `identifier_match`
  selects the mode: `"each"` (default) matches a pair when any single key is equal;
  `"all"` matches only when every configured key is equal and non-empty for both
  subjects, and evidence then carries every key's value. Any other value raises
  `ImproperlyConfigured` when the source scans.

Both scan the configured subject model (`subject_model`, default `individual.Individual`,
resolved with `django.apps.apps.get_model`).

## Persisted candidates

`DuplicateCandidate` (table `deduplication_candidate`) stores one row per
`(subject_model, subject_a, subject_b, kind)`, with `source`, `score`, `evidence`, a
`status` (`OPEN` / `CONFIRMED` / `DISMISSED`), an optional `task` FK, and review metadata
(`reviewed_by`, `reviewed_at`, `decision_note`). `ScanState` keeps one watermark row per
source `kind`.

`DuplicateCandidate.evidence` is stored as the sources give it. Read through GraphQL by a
user without the masked-data right (900101), each value of its `columns` that reads a field
the mask of the subject model masks (`individual`'s person or group mask, with the fields
other modules register) is masked by that mask; every value is masked when `subject_model`
names no model. Other evidence keys are given as stored.

A review task (source `deduplication_candidate`) holds a copy of its candidate's evidence in its
`data`. The module registers a masker for that source with `individual`'s task mask
(`TaskMask.register_source`), so the task's `data` and `businessData` give the evidence masked
the same way to a user without the masked-data right, on the task queries, the task history and
through `duplicateCandidates { task }`. The beneficiary and payment review tasks
(`CreateDeduplicationReviewTasksService`, `CreateDeduplicationPaymentReviewTasksService`) store the
summary row they were made from in `data.column_values`; maskers registered for those sources mask
each value whose column reads a masked field, by the rule of the summaries, on the task, its node
and its history.

For a user without the masked-data right, an `orderBy` term of `duplicateCandidates` whose path
traverses a JSON field (the evidence, or one reached through a relation such as the review task's
`data`) or names no field is left out (logged at info), and the primary key orders last.

Services (`deduplication/services.py`):

- `record_candidate(candidate, source=...)` — get-or-creates the row; an `OPEN` row keeps
  the higher score and merges evidence; a `DISMISSED` row is never reopened.
- `run_scan(kinds=None, actor=...)` — runs registered sources (optionally filtered by
  kind), records candidates, advances each source's `ScanState`. Also available as
  `manage.py scan_duplicates [--kind KIND] [--user USERNAME]`.
- `scan_subject(subject_model, subject_id)` — on-demand scan across all sources for one
  subject.
- `resolve(candidate, decision="same"|"different", keep=None, actor=..., note="")` —
  `different` dismisses the candidate; `same` merges the subjects (see below) and confirms
  it. Only an `OPEN` candidate is resolved: the row is re-read under a lock, and a
  `CONFIRMED` or `DISMISSED` candidate is returned unchanged. A `same` decision raises
  `ValueError` and leaves the candidate `OPEN` when `keep` is not one of the pair's two
  subjects (`deduplication.resolve.keep_not_in_pair`) or when either subject is already
  soft-deleted (`deduplication.resolve.subject_deleted`).
- `create_review_tasks(candidate_ids, actor)` — one `tasks_management.Task` per `OPEN`
  candidate that has no `RECEIVED`/`ACCEPTED` task (`source="deduplication_candidate"`),
  FK'd back onto the candidate; other ids, and the candidates `actor` may not see
  (`DuplicateCandidate.get_queryset`), are skipped. Completing such a task
  (existing `task_service.complete_task` flow) resolves the candidate from
  `task.json_ext["additional_resolve_data"]` (`{"decision", "keep", "note"}`); when the
  completing user may not see the candidate, the completion is refused
  (`deduplication.resolve.candidate_out_of_scope`).

## Merge policy

`merge_subjects(kept, retired, actor, policy=None)` merges `retired` into `kept`:

- an empty field on `kept` is filled from `retired`; a differing non-empty value is kept
  and journaled into `kept.json_ext["merge_conflicts"]`
  (`{"field", "kept", "retired", "retired_id", "at", "actor"}`) — never overwritten.
  `json_ext` keys are merged the same way.
- `MERGE_POLICY` (module config `merge_policy`, default `"delete"`) then decides how
  `retired` is retired: `"delete"` soft-deletes it; `"retire"` first sets
  `retired.json_ext["retired_into"] = kept.id`, then soft-deletes it.
- After the transaction commits, the `deduplication.subject_merged` service signal fires
  with `subject_model`, `kept_id`, `retired_id`, `actor`, `policy` (read off
  `kwargs["result"]` by an `AFTER` receiver, the same pattern as this module's other
  service signals).

## Module configuration

Loaded the same way as the legacy rights, via `ModuleConfiguration` onto
`DeduplicationConfig`:

| key | default |
| --- | --- |
| `subject_model` | `"individual.Individual"` |
| `demographic_columns` | `["first_name", "last_name", "dob"]` |
| `identifier_keys` | `[]` |
| `identifier_match` | `"each"` |
| `merge_policy` | `"delete"` |
| `gql_create_deduplication_review_perms` | `["172001"]` |
| `gql_create_deduplication_payment_review_perms` | `["172002"]` |
| `gql_resolve_duplicate_perms` | `["172003"]` |
| `gql_run_scan_perms` | `["172004"]` |
| `gql_query_duplicates_perms` | `["172005"]` |

## GraphQL

- `duplicateCandidates(status, kind, subjectId, first, offset, orderBy)` — paginated
  connection over `DuplicateCandidate` (`gql_query_duplicates_perms`). With `ROW_SECURITY`, a
  user who is not an IMIS admin gets only the pairs whose two subjects are both rows the subject
  model's `get_queryset(None, user)` gives the user (`DuplicateCandidate.get_queryset`):
  for `individual.Individual`, the person's location or its household's; for `individual.Group`,
  the group's location; a soft-deleted subject included. A pair with one subject out of the
  user's locations is left out. The same scope applies to the `node` lookup of a candidate, to
  `duplicateCandidates` of a task, and to `duplicateCandidateResolveCheck`.
- `runDuplicateScan(kinds)` — mutation, runs `run_scan` (`gql_run_scan_perms`).
- `resolveDuplicateCandidate(id, decision, keep, note)` — mutation, runs `resolve`
  (`gql_resolve_duplicate_perms`); fails with `deduplication.mutation.candidate_not_open`
  when the candidate is no longer `OPEN`, and as for a missing candidate when the user may
  not see it (`DuplicateCandidate.get_queryset`).
- `createDuplicateReviewTasks(ids)` — mutation, runs `create_review_tasks`
  (`gql_create_deduplication_review_perms`, same right as the legacy
  `createDeduplicationTasks`).
