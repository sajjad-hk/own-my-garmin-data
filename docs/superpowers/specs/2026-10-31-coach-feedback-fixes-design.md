# Coach feedback fixes + weekly rhythm — design

Status: draft for review (2026-10-31)
Builds on: the coach loop (`2026-09-30-workout-coach-loop`), the strength generator
(`2026-10-15-strength-generator-design.md`), migration 0012 (hangboard = `CARRY/BAR_HOLDS`).

## Why

Two weeks of real use turned up five problems. Each one makes the athlete do by hand
what the loop was meant to do for them.

1. **Per-side work doesn't alternate.** "Strength: pull/push basic" had Dumbbell Row as
   3 sets left-arm in one block, then 3 sets right-arm in the next. A real session swaps
   arms every set. Cause: the strength builder only knows "one exercise × N sets", and
   the 0012 docs tell the coach to write per-side work as two consecutive entries.
2. **Rest after heavy work is too short.** For example, 2 min between weighted pull-up
   sets. The coach has the Lattice course files but doesn't apply them when it sets `rest_s`.
3. **Hangs start with no time to get on the board.** Warm-up ends on the lap button, and
   the first timed hang starts at that same press. The athlete would have to press the
   button while hanging, which is physically impossible. The same happens at the start of
   every later set, after a timed rest or a rest ended with the button.
4. **Unfamiliar exercises need a YouTube search.** The athlete previews the plan with the
   coach before approving. Names like `ROW/RENEGADE_ROW` mean a manual search, which
   defeats quick planning.
5. **The coach doesn't know the weekly rhythm.** The athlete climbs Tue and Thu evenings
   (~19:00, gym) and Sun mornings (~08:00). They want a weekly menu and a just-in-time
   plan for each gym day, not a schedule fixed days ahead.

## Goals

- Paired (alternating) exercise blocks, so the watch runs L, R, L, R…
- Rest set from training intensity, using the Lattice guidance the coach already has.
- A short countdown before every timed hold, so the athlete can get on the board.
- A how-to video link for every exercise in the coach's previews.
- A Sunday preview that explains itself: a table of every exercise with why it was chosen
  (the Lattice principle behind it and the athlete's weakness it targets), so the athlete
  can check the coach's reasoning and learn from it.
- A weekly rhythm:
  - Sunday evening: review the week and prepare a menu; nothing scheduled.
  - Each gym day, before the session: pick from the menu what today's recovery
    supports, plan it for today, and have the athlete approve.

## Non-goals

- No change to the approval gate, the roles/grants model, the enactor, or the publish
  step. The athlete still approves and publishes from their computer
  (`uv run plan_admin.py approve`).
- No mobile/phone approval.
- No pyramids, drop sets or per-set varying load.
- No run or ride generators.
- No change to the Garmin official API work (spike Part B is separate).

## Anti-goals (failure even if it "works")

- A scheduled task that puts anything on the watch without the athlete's approval.
- Invented video URLs that are dead or show the wrong exercise.
- A builder change that silently voids approvals of rows the athlete already approved.
- Hard-coded rest minimums that override the coach's judgement. The athlete wants Lattice
  guidance applied, not a rule engine.

## Constraints

- Garmin payloads must be copied from a verified reference, as the strength generator was
  (field-for-field fixture tests). Any new step shape needs a hand-built reference workout
  or a live upload round-trip before it's trusted.
- The coach is a claude.ai Project. Scheduled tasks are Cowork tasks inside it: they run in
  the cloud and push to the Claude app. The coach reaches the database only through
  postgres-mcp (`run_sql`, `run_write_sql` as role `coach`). Everything it must know lives in
  table/column comments (a new comment-only migration) or the Project instructions/prompts.
- **The sync schedule is unchanged** (athlete's decision): `0 6 * * *` UTC on main, once a
  day. GitHub's queue delay usually lands it late morning Warsaw, before the 16:00 check.
  - PR #8 (`17 8 * * *`) is not merged and gets closed.
  - This work does not touch `sync.yml`.
- Athlete timezone: Europe/Warsaw (CET since 2026-10-25, UTC+1).

## Design

### 1. Paired blocks (alternating exercises in one repeat)

**Params.** A `kind='strength'` exercises list gets a second entry form, alongside the
existing single-exercise entry:

```json
{"sets": 3, "pair": [
  {"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10, "weight_kg": 12, "rest_s": 30, "note": "L"},
  {"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10, "weight_kg": 12, "rest_s": 90, "note": "R"}
]}
```

- `pair` takes 2 items (per-side work; also antagonist supersets like pull-up / push-up).
- Each item follows today's single-exercise rules, minus `sets`:
  - exactly one of `reps` / `time_s`;
  - optional `weight_kg`, `rest_s` (seconds, or omitted = rest until the lap button) and
    `note`.
- `sets` belongs to the block.
- *Builder's call:* whether to allow more than 2 items (circuits). The athlete asked only
  for L/R.

**Garmin shape (to verify).** One `RepeatGroupDTO` (`numberOfIterations = sets`) whose
`workoutSteps` are `[work A, rest A, work B, rest B]`. Step orders stay unique and
increasing across nesting, as today.

- Not yet verified. **The athlete builds one reference workout by hand in Connect:** a
  repeat ×3 of Dumbbell Row (note L), rest, Dumbbell Row (note R), rest.
- We reflect it into `tests/fixtures/` and pin the builder to it field-for-field, exactly as
  `strength_reference.json` was. This is the first implementation step and blocks the rest
  of part 1.

**Coach rule** (comments + instructions): one-arm / one-leg exercises always go in a `pair`
with notes "L"/"R", never as two separate entries. Replace the 0012 sentence that says the
opposite.

### 2. Rest from intensity (docs only)

- No code change and no enforced minimum.
- The new comment migration adds to the `protocols` comment: set `rest_s` from the Lattice
  guidance for the session's intensity. Max strength and max hangs need long, full rests
  (typically 3–5 min); volume and accessory work can be shorter.
- Inside a `pair`, the rest after side A can be short. The rest after side B is the set rest.
- The same line goes in the suggested Project-instructions text.
- The athlete sees every `rest_s` in the preview and the approve checklist.

### 3. "Get ready" countdown before timed holds

**Behaviour:** every timed hold gets a 5 s countdown step immediately before it, inside its
repeat. This covers:

- the first hang after the warm-up lap press;
- every hang after a lap-ended rest;
- every hang after a timed rest (harmless there, and consistent).

It applies to:

- **`kind='strength'` steps with `time_s`** (hangboard `BAR_HOLDS`, planks, L-sits). The
  repeat becomes `[get ready 5 s, hold, rest]`, or for a pair `[ready, A, rest A, ready, B, rest B]`
  where A/B are timed.
- **Rep-based steps** get no countdown: the athlete starts when ready and the watch counts reps.
- **`kind='other'`** (plain interval timer): one 5 s step before each set's repeat group,
  not inside it (the 3 s "off" between repeaters is already the gap). The rest between sets
  is never skipped.

**Step type:** a timed `rest` step with description "Get ready". *Builder's call* to confirm
the exact type for each sport via the upload round-trip. In the strength reference, rest
steps are `stepType rest`. In `kind='other'`, `rest` outside a repeat is currently marked
UNVERIFIED.

**Param:** optional `ready_s` (default 5, 0 turns it off, max 15) at protocol level for both
kinds. *My call:* default on, because the athlete needs it on every hang workout.

**Per-set results:** the countdown is recorded as an extra short REST set in
`activity_exercise_set_rows`. Mention this in the view/table comment so the coach ignores
≤15 s REST sets.

**Existing protocols:**
- Seed re-uploads a protocol only when `protocol_hash(kind, name, description, params)`
  changes, so already-uploaded protocols keep the old shape.
- Requirement: workouts uploaded from now on get the countdown, and **approvals the athlete
  already gave must not go stale** because the builder changed.
- *Builder's call* how to get there: a builder version in the ledger key but not the approval
  fingerprint, or simply let new protocols pick it up. The weekly menu (part 5) creates fresh
  protocols every week anyway.

### 4. Video links and readable names

- Add two generated columns to `exercise_catalog`:
  - `display_name`, e.g. `DUMBBELL_ROW` → "Dumbbell Row";
  - `video_url`, a YouTube **search** URL built from the exercise name, e.g.
    `https://www.youtube.com/results?search_query=dumbbell+row+exercise+form`.
- These come from the catalogue, never invented by the coach, so the link always works and
  shows several demos.
- **Coach rule:** every preview lists each exercise as display name + sets × reps/time @ load,
  rest, note, and its `video_url`.
- **Hangboard exception:** a `BAR_HOLDS` link is useless, so the coach describes the grip and
  edge from the note instead (it may build a search link for the grip, e.g. "half crimp
  hangboard").
- *Builder's call:* generated column vs a view. It must be readable by the coach role and
  survive the weekly catalogue refresh (which upserts rows).

### 5. Weekly rhythm

**Fixed weekly schedule (athlete's words):** climbing Tue and Thu evenings (gym ~19:00) and
Sun mornings (~08:00). This goes in the Project instructions and the comment docs.

**Menu storage:**
- Add `protocols.menu_week date` (nullable). It is the Monday of the week a menu protocol is
  meant for, and is set by the weekly review.
- It is **not** part of `protocol_hash`, so it doesn't affect uploads or approvals.
- The coach already has insert/update on `protocols`. *Builder's call:* confirm the grant
  covers the new column on upgrade.
- Menu protocols are just protocols, and nothing reaches Garmin until a plan row is written
  and approved.

**Scheduled tasks** (Cowork, in the coach Project). This repo ships their prompt text as a
doc; the athlete pastes it in.

| When (Warsaw) | Task | Writes |
|---|---|---|
| Sun ~19:00 | **Weekly review + menu.** Review the past week (activities, `activity_exercise_set_rows` vs plan, recovery trend). Name 2–4 focus points (Lattice-based). Write next week's menu as protocols (`menu_week` = next Monday): typically one hangboard and one or two strength sessions, plus lighter alternatives. Show the **menu preview** (below). Schedule nothing. | `protocols` only |
| Tue, Thu 16:00 | **Gym-day plan** (replaces the 16:00 brief on these days). Check today's recovery (sleep, HRV, body battery, RHR, recent load). Pick the menu session that fits, or advise climbing only / a lighter option. Insert one `training_plan` row for **today** (`source='generated'`). Give the row id and the approve command. Skip if today already has a plan row. | `training_plan` (1 row) |
| Sat 20:00 | **Gym-day plan for Sunday**, same as above for **tomorrow**, using Saturday's data (the athlete accepts that the night's sleep isn't known yet). | `training_plan` (1 row) |
| Other days 16:00 | Existing read-only recovery brief, unchanged. | nothing |

- These are the first scheduled tasks that write. They write only `protocols` /
  `training_plan`, and the approval gate still stands between them and the watch.
- **Unattended writes are expected to work but are unproven.**
  - Cowork's task dialog says scheduled tasks run "without pausing for approval".
  - The Project's postgres-mcp connector exposes `run_write_sql`.
  - So the first live step is one manual "Run now" of the gym-day prompt that writes a
    single row, which the athlete then skips or approves.
  - **Fallback** if writes don't run unattended: the task shows the exact INSERT plus a
    preview in the push, and the athlete says "go" in chat.
- The existing brief's "never use run_write_sql" stays for the read-only days. That rule
  lives in **the brief task's own prompt**, not the Project instructions, so the new write
  tasks aren't blocked by it.
- *My call:* the new prompts are self-contained. Their recovery check reuses the
  brief's wording (last night's sleep, HRV, resting HR, body battery vs recent average;
  flag anything off), so the read-only and gym-day days agree.
- *Builder's call:* one prompt that branches on weekday vs separate tasks. It depends on
  what Cowork's schedule options allow.

**Menu preview format (Sunday).** One table per menu session, one row per exercise:

| Exercise (video) | Prescription | Why this exercise | Evidence |
|---|---|---|---|
| [Dumbbell Row](video_url) | 3 × 10 @ 12 kg, L/R paired, rest 30 s / 90 s | Lattice: antagonist and scapular work to balance pulling volume (name the Lattice file and section) | Your rows stalled at 10 kg for 3 weeks (`activity_exercise_set_rows`) |

- **Why** cites the Lattice knowledge file (name and section) for the principle. If the
  choice is the coach's own judgement, it says so instead of inventing a citation.
- **Evidence** ties the exercise to the athlete's data, such as stalled loads, missed sets,
  recovery trend, or the focus points named above. Where there is no data yet, it says
  "no data yet" rather than guessing.
- Above the tables: the 2–4 focus points with one line of reasoning each.
- Gym-day plans reuse the same table for the one chosen session (short version: exercise,
  prescription, video, plus one line on why this session today, given recovery).

**Sunday's own session:** the 06:00 UTC sync runs before the 08:00 Sunday session is
uploaded, unless GitHub's queue delays it past then. The weekly review therefore may not see
Sunday's session. The prompt tells the coach to check whether it's there and, if missing,
say so and review the rest of the week. The athlete can trigger the sync manually from the
Actions tab first if they want it included.

**Known gap, kept small:** the coach can't read `plan_approvals`, so it can't tell "approved
but not published" from "not approved".
- The gym-day task only needs to know whether a row for that date exists (it can read
  `training_plan.status` and `skip`).
- *My call:* the planned-vs-done / plan-status view stays a separate later item.

## Data flow

```
Sun 19:00  coach: review week → INSERT protocols (menu_week = next Mon) → preview (+ video links)
Tue/Thu 16:00, Sat 20:00
           coach: recovery check → pick menu protocol → INSERT training_plan(today|tomorrow) → push
athlete:   reads push/report → uv run plan_admin.py approve → publish → watch
watch:     ready 5 s → hold → rest … L, R, L, R …
sync:      daily 06:00 UTC (unchanged) → activities, activity_exercise_sets → next review
```

## Testing

- **Unit:**
  - pair builder pinned field-for-field to the hand-built reference fixture;
  - validator errors for pairs;
  - countdown placement for strength (timed vs reps, single vs pair) and `kind='other'` (per set);
  - `ready_s` bounds and 0 = off;
  - existing fixtures (`strength_reference`, `bear_essentials`, `finger_workout`) still pass
    with `ready_s: 0`, or are updated deliberately.
- **Smoke (Docker Postgres):**
  - the new migration (columns, comments);
  - the coach role can write `menu_week` and read `video_url` / `display_name`;
  - the catalogue refresh keeps the generated columns;
  - `protocol_hash` ignores `menu_week`.
- **Live (ask first for each Garmin write):**
  - upload one pair workout and one hangboard workout with the countdown;
  - read them back and compare to what was sent;
  - the athlete checks them on the watch;
  - after a session, the set rows show the alternation and the short REST sets.

## Deliverables outside the code

- Suggested Project-instructions additions:
  - weekly rhythm;
  - pairs for per-side work;
  - Lattice rest;
  - video links in previews;
  - the menu → gym-day flow.
- Three scheduled-task prompts (weekly review, Tue/Thu gym day, Sat-for-Sun gym day).
- README coach-loop section updated.

## Left to the builder

- Pair size limit beyond 2.
- The exact countdown step type for each sport (verified by round-trip).
- How existing uploads pick up the countdown without voiding approvals.
- Generated column vs view for links.
- One branching prompt vs separate tasks.
- Migration numbering (next is 0013).
