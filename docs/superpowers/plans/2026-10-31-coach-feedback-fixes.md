# Coach feedback fixes + weekly rhythm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four problems found in real use (per-side work not alternating, short rests after
heavy sets, hangs starting with no time to get ready, unfamiliar exercise names) and give the
coach a weekly rhythm: a Sunday menu with reasoning, and a gym-day plan for each session.

**Architecture:**
- The builders in `workout_gen.py` gain two things, each pinned to a Garmin reference:
  - a `pair` block (two exercises alternating inside one repeat);
  - a `ready_s` "Get ready" countdown step before timed holds and interval sets.
- Migration 0013 adds:
  - `exercise_catalog.display_name` / `video_url` (generated columns);
  - `protocols.menu_week`;
  - rewritten coach comments.
- Coach behaviour (rest from Lattice, previews with a reasoning table, weekly menu, gym-day
  plans) ships as text: database comments plus Project-instruction and scheduled-task prompts
  in `docs/coach/`.

**Tech Stack:** Python 3.11+ (uv PEP 723 scripts), psycopg 3, Postgres (Neon; Docker for smoke),
pytest, garminconnect (unofficial).

**Spec:** `docs/superpowers/specs/2026-10-31-coach-feedback-fixes-design.md`

## Global Constraints

- **Garmin payloads:**
  - Every new step shape is copied from a hand-built reference or verified by a live upload
    round-trip.
  - `workout_gen.py` stays pure (no I/O, no garminconnect import).
  - Only `garmin_write.py` calls write endpoints (`tests/test_write_isolation.py`).
- **Approvals:**
  - The approval fingerprint and `planning.protocol_hash` are unchanged.
  - Rows the athlete already approved must not go stale because of this work.
  - Already-uploaded protocols keep their old shape. New or edited protocols get the new one.
    No builder version is added to any hash (decided here, as the spec leaves it to the builder).
- **Sync:** `.github/workflows/sync.yml` is not touched. The sync stays `0 6 * * *`, and PR #8
  is closed.
- **Countdown:** default `ready_s` = 5, `0` = off, max 15, step description `"Get ready"`.
- **Pairs:** exactly 2 items per `pair` block (spec: builder's call, decided here: 2 only).
- **Video links:** `video_url = 'https://www.youtube.com/results?search_query=' ||
  replace(lower(exercise), '_', '+') || '+exercise+form'`;
  `display_name = initcap(replace(exercise, '_', ' '))`.
- **Garmin writes on the athlete's account:** ask before every one, and before any Neon
  migration.
- **Smoke tests:** local Docker only, never the production `DATABASE_URL`.
- **Commands:**
  - Unit tests:
    `uv run -q --with pytest --with 'garminconnect>=0.3.6' --with 'psycopg[binary]>=3.3' --with 'python-dotenv>=1.0' pytest tests -q`
  - Smoke tests: start the Docker database with
    `docker run -d --rm --name garmin-smoke -e POSTGRES_PASSWORD=smoke -p 55432:5432 postgres:16`
    if it isn't running, then run
    `SMOKE_TEST_DATABASE_URL=postgresql://postgres:smoke@localhost:55432/postgres uv run scripts/smoke_test.py`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A one-arm hang pair** (two timed `BAR_HOLDS` items with notes L/R, rests omitted, so they
   end on the lap button) must get a countdown before each side. Test in Task 2.
2. **Bad `ready_s` values** must be refused with a message naming `ready_s`, never silently
   accepted. That covers the string `"5"`, the bool `true`, `-1` and `20`. Test in Task 1 and
   Task 3.
3. **A pair naming an exercise Garmin doesn't know** must be refused at seed like a single
   exercise. The catalogue check must look inside `pair` items. Test in Task 2.
4. **Error messages for a bad pair item** must say which block and which item, so the seed note
   is actionable for the coach. Test in Task 2.
5. **`video_url` must be URL-safe** for every catalogue name (digits, odd names). Test in
   Task 4: `^https://www\.youtube\.com/results\?search_query=[a-z0-9+]+$` over the sample
   catalogue.

---

### Task 0: Reflect the athlete's hand-built pair reference (gate for Task 2)

**Athlete action:** the athlete builds a Strength workout in Garmin Connect, saved but not
scheduled, with a single repeat ×3 of:

- Dumbbell Row, 10 reps, 12 kg, note "L"
- Rest 30 s
- Dumbbell Row, 10 reps, 12 kg, note "R"
- Rest 90 s

with the usual lap-button warm-up. They give its name. If it isn't done yet, do Tasks 1, 3, 4
and 5 first.

**Files:**
- Create: `tests/fixtures/strength_pair_reference.json`

- [ ] **Step 1:** Run `uv run scripts/probe_workout_link.py preflight` and find the
  workout id by name. This is a read-only Garmin call.
- [ ] **Step 2:** Run `uv run scripts/probe_workout_link.py reflect <id>` and save the JSON
  as `tests/fixtures/strength_pair_reference.json`.
- [ ] **Step 3:** Inspect it. Expected: one `RepeatGroupDTO` with `numberOfIterations` 3 whose
  `workoutSteps` are `[interval(ROW/DUMBBELL_ROW, reps 10, 12 kg, "L"), rest(time 30),
  interval(…, "R"), rest(time 90)]`. Record in the ledger any field that differs from the
  single-exercise reference (`childStepId`, `skipLastRestStep`, step order).
- [ ] **Step 4: Commit** the fixture with the spec and this plan.
  ```bash
  git add tests/fixtures/strength_pair_reference.json docs/superpowers/specs/2026-10-31-coach-feedback-fixes-design.md docs/superpowers/plans/2026-10-31-coach-feedback-fixes.md
  git commit -m "test: hand-built L/R pair reference workout"
  ```

### Task 1: "Get ready" countdown in strength workouts

**Files:**
- Modify: `workout_gen.py` (strength section: `_validate_strength`, `make_strength_workout`;
  add `_ready_step`)
- Test: `tests/test_workout_gen_strength.py`

**Interfaces:**
- Produces:
  - `make_strength_workout(name, description=None, *, exercises, warmup="lap", ready_s=5) -> dict`;
  - `_validate_ready(ready_s) -> None`, which raises `ValueError` mentioning `"ready_s"`
    (shared with Task 3);
  - `READY_MAX_S = 15`;
  - `_READY_DESCRIPTION = "Get ready"`.

- [ ] **Step 1: Write the failing tests**
  - `test_matches_reference_field_for_field` and `test_timed_rests_match_bear_essentials`
    now pass `ready_s=0`. Deliberate: the references predate the countdown.
  - `test_timed_hold_gets_ready_step_inside_its_repeat`: exercises
    `[{"category":"CARRY","exercise":"BAR_HOLDS","sets":3,"time_s":10,"weight_kg":6}]` →
    group `workoutSteps` has 3 steps:
    - `[0]` is `stepType.stepTypeKey == "rest"`, `endCondition.conditionTypeKey == "time"`,
      `endConditionValue == 5.0`, `description == "Get ready"`, `category is None`,
      `exerciseName is None`;
    - `[1]` is the hold;
    - `[2]` is the rest (lap button).
  - `test_rep_exercise_has_no_ready_step`: a reps exercise keeps exactly `[work, rest]`.
  - `test_ready_s_zero_turns_it_off`: the same timed hold with `ready_s=0` → `[work, rest]`.
  - `test_ready_s_rejects_bad_values`: parametrize `"5"`, `True`, `-1`, `20` → `ValueError`
    matching `"ready_s"`. 15 and 0 are accepted.
  - Extend `test_step_orders_unique_increasing_and_child_ids_per_group` with a timed exercise
    and the default countdown: orders still unique and increasing, and the ready step carries
    its group's `childStepId`.
- [ ] **Step 2: Run them.** Expected: FAIL (unexpected keyword `ready_s` / missing step).
- [ ] **Step 3: Implement.**
  - The ready step is `_strength_step(order, "rest", child=group_no, category=None,
    exercise=None, condition=_TIME, value=ready_s, description=_READY_DESCRIPTION)`.
  - It goes first in a group's steps when the item uses `time_s` and `ready_s > 0`.
  - `ready_s` is validated as a non-bool number, `0 <= ready_s <= 15`.
  - Update the strength section comment: a countdown precedes timed holds; it is unverified
    until the Task 6 round-trip.
- [ ] **Step 4: Run the unit tests.** Expected: all pass.
- [ ] **Step 5: Commit** with the message `feat: 5 s get-ready countdown before timed strength holds`.

### Task 2: Paired (alternating) exercise blocks

Blocked on Task 0's fixture.

**Files:**
- Modify: `workout_gen.py`:
  - `STRENGTH_PAIR_KEYS = frozenset({"sets", "pair"})`;
  - `STRENGTH_ITEM_KEYS = STRENGTH_EXERCISE_KEYS - {"sets"}`;
  - validation refactor: `_validate_item(item, where)`;
  - builder refactor: `_item_steps(order, child, item, ready_s) -> tuple[list[dict], int]`.
- Modify: `protocols.py`. Add `exercise_pairs(params) -> list[tuple[str | None, str | None]]`
  (pure, flattens singles and pair items) and use it in `strength_problems`.
- Test: `tests/test_workout_gen_strength.py`, `tests/test_protocols.py`

**Interfaces:**
- Consumes: Task 1's `_validate_ready` and the ready-step rule.
- Produces:
  - an exercises-list entry is either a single exercise (unchanged) or
    `{"sets": int>=1, "pair": [item, item]}`;
  - `protocols.exercise_pairs(params)`.

- [ ] **Step 1: Write the failing tests**
  - `test_pair_matches_hand_built_reference`: build from
    `[{"sets":3,"pair":[{"category":"ROW","exercise":"DUMBBELL_ROW","reps":10,"weight_kg":12,"rest_s":30,"note":"L"},{…,"rest_s":90,"note":"R"}]}]`
    with `ready_s=0` and the reference's name, then compare with
    `_norm(_strip(fixture))`, field for field. If Task 0 recorded differences, the builder
    copies the fixture.
  - `test_pair_of_timed_holds_gets_ready_before_each_side`: two `CARRY/BAR_HOLDS` items with
    `time_s` 7, notes L/R, no `rest_s` → group steps are
    `[ready, hold L, rest(lap), ready, hold R, rest(lap)]`, all with the same `childStepId`.
  - `test_pair_rejects_bad_shapes`: each case raises `ValueError` matching the given fragment:

    | Input | Message contains |
    |---|---|
    | 1 item | `"exactly 2"` |
    | 3 items | `"exactly 2"` |
    | an item with `sets` | `"block 1, item 2"` and `"sets"` |
    | a block without `sets` | `"block 1"` and `"sets"` |
    | a block with both `pair` and `exercise` | `"unknown key"` |
    | an item with reps and time_s | `"block 1, item 1"` |

  - Existing single-exercise errors keep the wording `exercise N`.
  - `tests/test_protocols.py::test_exercise_pairs_flattens_singles_and_pairs`: a single
    exercise plus a pair gives three tuples in order. Non-dicts are ignored.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - A pair block is one `RepeatGroupDTO` (`numberOfIterations = sets`, `childStepId` =
    the group number) whose steps are `_item_steps` for item A followed by item B.
  - Each item's rest uses that item's `rest_s` (omitted = lap button).
  - `strength_problems` uses `exercise_pairs`.
  - Remove the "two consecutive exercises with note L/R" sentence from the strength comment
    in `workout_gen.py`.
- [ ] **Step 4: Run the unit tests.** Expected: all pass.
- [ ] **Step 5: Smoke.** In `test_seed_validates_strength_protocols`, add a protocol `s-pairbad`
  whose pair contains `ROW/NOT_AN_EXERCISE` on a fourth day. Assert it's refused with
  `"ROW/NOT_AN_EXERCISE"` in the notes, and that `s-good` still uploads. Run the smoke suite.
  Expected: `All smoke tests passed.`
- [ ] **Step 6: Commit** with the message `feat: paired L/R strength blocks (alternate sides inside one repeat)`.

### Task 3: "Get ready" countdown in interval (`kind='other'`) workouts

**Files:**
- Modify: `workout_gen.py` (`make_other_workout`, `_multi_set_steps`, `_executable` gains
  `description: str | None = None`)
- Test: `tests/test_workout_gen.py`

**Interfaces:**
- Consumes: `_validate_ready`, `_READY_DESCRIPTION` (Task 1).
- Produces: `make_other_workout(..., ready_s: float = 5.0)`.

- [ ] **Step 1: Write the failing tests**
  - `test_single_set_matches_finger_field_for_field` passes `ready_s=0`.
  - `test_ready_step_before_each_set`: `work_s=7, rest_s=3, reps=6, sets=2, set_rest_s=180`
    → top-level step types in order:
    `warmup, rest(5, "Get ready"), repeat, rest(180), rest(5, "Get ready"), repeat, cooldown`.
    The ready steps have `childStepId is None`.
  - `test_other_ready_s_rejects_bad_values`: the same four bad values as Task 1.
  - `test_multi_set_step_orders_are_unique_and_increasing` still passes with the default countdown.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The ready step is
  `_executable(order, "rest", _TIME, ready_s, None, description=_READY_DESCRIPTION)` before
  each set's repeat group. The module docstring notes the countdown, and that the
  `rest`-outside-a-repeat step is unverified until Task 6.
- [ ] **Step 4: Run the unit tests.** Expected: all pass.
- [ ] **Step 5: Commit** with the message `feat: get-ready countdown before each interval set`.

### Task 4: Migration 0013 (video links, display names, weekly menu, coach docs)

**Files:**
- Create: `schema/migrations/0013_coach_feedback.sql` (`-- domain: workouts`)
- Modify: `scripts/smoke_test.py`

**Interfaces:**
- Produces:
  - `exercise_catalog.display_name text` and `exercise_catalog.video_url text`, both
    `generated always … stored`;
  - `protocols.menu_week date` (nullable).
  Later tasks (docs, prompts) name these columns.

- [ ] **Step 1: Write the failing smoke test `test_coach_feedback_schema`**
  - After `_setup_coach_loop()`, insert the sample catalogue via `_refresh_exercise_catalog`
    with the fake doc.
  - As `coach_url`:
    - `select display_name, video_url from exercise_catalog where exercise='DUMBBELL_ROW'`
      returns `("Dumbbell Row", "https://www.youtube.com/results?search_query=dumbbell+row+exercise+form")`;
    - every `video_url` matches `^https://www\.youtube\.com/results\?search_query=[a-z0-9+]+$`;
    - an insert into `protocols (slug,name,kind,params,menu_week)` with `menu_week` =
      next Monday succeeds, and an `update protocols set menu_week` succeeds.
  - Run `_refresh_exercise_catalog` again with the cache forced stale
    (`update exercise_catalog set synced_at = now() - interval '8 days'`). The generated
    columns are still filled.
  - `planning.protocol_hash` for that protocol equals the hash computed before `menu_week` was
    set (load the row twice).
  - Extend `test_coach_tables_are_self_describing`. The `protocols` comment contains `"pair"`,
    `"ready_s"`, `"Lattice"` and `"menu_week"`. The `exercise_catalog` comment contains
    `"video_url"`. The view comment contains `"Get ready"`. `protocols.menu_week`,
    `exercise_catalog.video_url` and `exercise_catalog.display_name` have column comments.
  - Register the new test in `main()`.
- [ ] **Step 2: Run the smoke suite.** Expected: FAIL (column does not exist).
- [ ] **Step 3: Write the migration.**
  - Add the columns with `add column if not exists`, using the expressions in Global
    Constraints.
  - Replace the comment on `protocols` (supersedes 0012), keeping everything 0012 says about
    BAR_HOLDS, the %-of-max formula and `kind='other'`, plus:
    - **Pairs:** one-arm or one-leg work and antagonist supersets go in
      `{"sets": N, "pair": [A, B]}`, never two consecutive entries. Each item has its own
      `rest_s`: short after side A, the set rest after side B.
    - **Rest:** set `rest_s` from the Lattice guidance in the Project knowledge for the
      session's intensity. Max strength and max hangs need long, full rests (typically
      3–5 min); volume and accessory work can be shorter.
    - **Countdown:** `ready_s` (default 5, 0 = off, max 15) adds a "Get ready" countdown
      before every timed hold and every interval set.
    - **Menu:** `menu_week` = the Monday of the week a menu protocol is for. It is set by the
      Sunday review, and the gym-day plan picks from `where menu_week = <this Monday>`. It
      isn't part of approvals.
    - **Previews:** list each exercise as `display_name` linked to `exercise_catalog.video_url`
      (BAR_HOLDS: describe the grip and edge from the note instead).
  - Replace the comment on `exercise_catalog` (keep the 0011 text) and add `video_url` /
    `display_name`.
  - Replace the comment on the view `activity_exercise_set_rows` (keep the 0011 text) and add
    that REST sets of ≤15 s are "Get ready" countdowns, not real rests.
  - Add the three column comments.
- [ ] **Step 4: Run the smoke suite.** Expected: `All smoke tests passed.` If `initcap` is
  rejected as not immutable in a generated column, use a view instead, and note it in the
  ledger (spec: builder's call).
- [ ] **Step 5: Commit** with the message `feat: 0013 video links, display names, weekly menu column, coach docs`.

### Task 5: Coach text (Project instructions, scheduled-task prompts, README)

**Files:**
- Create: `docs/coach/project-instructions-additions.md`
- Create: `docs/coach/scheduled-tasks.md`
- Modify: `README.md` (coach-loop section)

**Interfaces:**
- Consumes: the column names from Task 4; the `pair` / `ready_s` params from Tasks 1–3.

- [ ] **Step 1: Write `project-instructions-additions.md`** for the athlete to paste under the
  existing Project instructions. Sections:
  - **Weekly rhythm:**
    - climbing on Tue and Thu evenings (gym ~19:00) and Sun mornings (~08:00);
    - the Sunday review prepares the menu, nothing scheduled;
    - gym days are planned the same day (Tue/Thu) or the evening before (Sun).
  - **Per-side work** always uses `pair`.
  - **Rest from Lattice**, as in the 0013 comment.
  - **Previews:** the reasoning-table format from the spec, with columns
    `Exercise (video) | Prescription | Why this exercise | Evidence`:
    - "Why" names the Lattice knowledge file and section, or says "coach's judgement";
    - "Evidence" cites the athlete's data or says "no data yet";
    - the focus points go above the table.
  - **Approve/publish:** reminder unchanged from 0012.
- [ ] **Step 2: Write `scheduled-tasks.md`** with three ready-to-paste prompts, each stating
  its schedule.
  - **Sunday 19:00, weekly review + menu.** Writes `protocols` only, with
    `menu_week` = next Monday.
    - It first checks whether today's session is in `activities`. If it's missing, it says so
      and suggests running the sync from GitHub Actions.
    - It shows 2–4 focus points, then the reasoning table per session, and schedules nothing.
  - **Tuesday and Thursday 16:00, gym-day plan.**
    - Recovery check, reusing the brief's wording: last night's sleep, HRV, resting HR and
      body battery against the recent average, flagging anything off.
    - Idempotency: `select … from training_plan where scheduled_date = current_date and not
      skip`. If a row exists, report it and stop.
    - Otherwise it picks a protocol `where menu_week = date_trunc('week', current_date)`, or
      recommends climbing only.
    - It inserts one `training_plan` row (`source='generated'`, today).
    - It shows the short table and one line on why this session fits today's recovery.
    - It gives the row id and `uv run plan_admin.py approve`.
  - **Saturday 20:00:** the same, for `current_date + 1`. The Monday of tomorrow's week is
    `date_trunc('week', current_date + 1)`. It adds a note that last night's sleep isn't
    known yet.
  - **Read-only brief on the other days:** its prompt stays as it is ("never use
    run_write_sql").
  - **Fallback** if a scheduled run can't write: show the exact INSERT and wait for "go" in chat.
- [ ] **Step 3: README.** In the coach-loop section, add a short paragraph on pairs, the
  countdown, video links and the weekly rhythm, linking both docs. Remove any line that says
  per-side work is two consecutive entries.
- [ ] **Step 4: Check.** Run `grep -rn "two consecutive" README.md docs/coach workout_gen.py
  schema/migrations/0013_coach_feedback.sql`. Expected: no output.
- [ ] **Step 5: Commit** with the message `docs: weekly rhythm prompts, pairs, rest and preview guidance for the coach`.

### Task 6: Live acceptance (gated: ask before each step)

- [ ] **Step 1:** Run the full unit and smoke suites. Expected: all pass.
- [ ] **Step 2 (ask):** Have the athlete run `uv run install.py --upgrade` to apply 0013 on
  Neon. Then check with `run_sql`:
  `select display_name, video_url from exercise_catalog where exercise='DUMBBELL_ROW'`
  and `select menu_week from protocols limit 1`.
- [ ] **Step 3 (ask):** The coach (or I, through the coach role) writes two test protocols and
  plan rows for a test date:
  - a pair `ROW/DUMBBELL_ROW` L/R;
  - a hangboard `CARRY/BAR_HOLDS` 3×10 s.
  The athlete approves and publishes. Read each uploaded workout back
  (`probe_workout_link.py reflect <id>`) and compare with what was sent. This verifies the
  pair shape and both countdown step types. Record the result in the ledger.
- [ ] **Step 4 (athlete):** Check both on the watch: rows alternate L, R, L, R, and the 5 s
  countdown runs before each hang. Then skip the rows (the enactor unschedules them).
- [ ] **Step 5 (athlete):** Paste the Project additions. Create the three scheduled tasks. Use
  **Run now** once on the gym-day prompt: it writes one row (or uses the fallback), and the
  athlete skips or approves it.
- [ ] **Step 6:** Open the PR for `feat/coach-feedback-fixes`.
