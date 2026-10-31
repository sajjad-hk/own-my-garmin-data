-- domain: workouts
-- Coach feedback round (2026-10-31):
--  * exercise_catalog gets display_name + video_url (generated, so the weekly
--    catalogue upsert never has to fill them) for readable previews with a
--    how-to link that always works (a YouTube search, never an invented URL);
--  * protocols.menu_week tags the Sunday review's menu for the gym-day plans
--    (not part of protocol_hash, so it never touches approvals);
--  * comments: paired L/R blocks, rest from the Lattice guidance, the "Get
--    ready" countdown, the weekly menu and the preview format.

alter table exercise_catalog
    add column if not exists display_name text
        generated always as (initcap(replace(exercise, '_', ' '))) stored;
alter table exercise_catalog
    add column if not exists video_url text
        generated always as ('https://www.youtube.com/results?search_query='
                             || replace(lower(exercise), '_', '+') || '+exercise+form') stored;

alter table protocols add column if not exists menu_week date;

comment on table protocols is
'Workout recipes the coach may author for source=''generated'' plan rows. Editing a protocol voids approvals of plan rows that use it (use a new slug when the session changes). Two kinds:
(1) kind=''strength'' — guided Garmin Strength workout. params: {"warmup": "lap" (default) or null, "ready_s": 5 (optional), "exercises": [{"category": "PULL_UP", "exercise": "WEIGHTED_PULL_UP", "sets": 4, "reps": 3, "weight_kg": 10, "rest_s": 240, "note": "full lockout"}, {"category": "PLANK", "exercise": "PLANK", "sets": 3, "time_s": 45, "rest_s": 60}]}. Each exercise: category + exercise from exercise_catalog (required), sets >= 1, exactly one of reps / time_s, optional weight_kg (> 0, added load), rest_s in seconds (omit = rest until lap button), note <= 50 chars shown on the watch. Unknown keys or exercises are refused at upload.
PAIRS — per-side (one-arm / one-leg) work and antagonist supersets alternate inside one block: {"sets": 3, "pair": [{"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10, "weight_kg": 12, "rest_s": 30, "note": "L"}, {"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10, "weight_kg": 12, "rest_s": 90, "note": "R"}]} — the watch runs L, R, L, R… Exactly 2 items; items take the exercise keys except sets (sets belongs to the block). Each item has its own rest_s: short after side A, the real set rest after side B. NEVER write per-side work as two separate entries (that gives all left sets, then all right sets).
REST — set rest_s from the Lattice guidance in the Project knowledge for the session''s intensity: max strength (heavy weighted pull-ups, low reps), max hangs and limit work need long, full rests (typically 3–5 min, rest_s 180–300); volume, endurance and accessory work can be shorter. Never shortchange rest after heavy sets.
COUNTDOWN — every timed hold (time_s) gets a "Get ready" countdown step of ready_s seconds (default 5, 0 = off, max 15) right before it, so the athlete can get on the board after pressing lap. Rep-based exercises get none.
HANGBOARD SESSIONS USE THIS KIND so the hung load is recorded per set: one entry per grip/edge with {"category": "CARRY", "exercise": "BAR_HOLDS", "sets": 3, "time_s": 10, "rest_s": 180, "weight_kg": 6, "note": "half crimp 4F · 20mm"} — time_s = hang length, sets = number of hangs, rest_s = rest between hangs, weight_kg = added load, note = the real grip and edge (one-arm hangs: a pair with notes L/R). Name the protocol so it is obviously hangboarding (e.g. "Max hangs 10s — half crimp"). For %-of-max prescriptions: load = % × (bodyweight + max added load) − bodyweight, bodyweight from the latest daily_metrics.weigh_in; if that is negative (assisted), omit weight_kg and write the assistance in the note (e.g. "half crimp 4F · −6 kg pulley"). Afterwards activity_exercise_set_rows shows each hang as exercise BAR_HOLDS with its actual weight_kg — in this project BAR_HOLDS means a hangboard hang.
(2) kind=''other'' — plain interval timer on Garmin''s ''Other'' sport, for UNWEIGHTED intervals only (records no per-set load): warm-up (ends on lap button), "Get ready" countdown (ready_s, default 5), reps × (work_s on / rest_s off), cool-down (lap button). params (seconds): {"work_s": 7, "rest_s": 3, "reps": 6}, optional "warmup_s"/"cooldown_s" (default 300), "ready_s"; keep "sets" at 1 (multi-set not verified yet).
WEEKLY MENU — the Sunday review writes next week''s sessions as protocols with menu_week = that week''s Monday and schedules nothing; a gym-day plan picks one with where menu_week = date_trunc(''week'', <session date>)::date and inserts a single training_plan row for that date.
PREVIEWS — show each exercise as exercise_catalog.display_name linked to exercise_catalog.video_url, with sets × reps/time @ load, rest and note, plus why it was chosen (the Lattice file and section, or "coach''s judgement") and the evidence from the athlete''s data (or "no data yet"). For BAR_HOLDS describe the grip and edge from the note instead of linking BAR_HOLDS.
name and description are what the athlete sees on the watch. For runs, rides etc. there is no generator: schedule an existing library workout (training_plan.source=''library'') instead.';

comment on column protocols.menu_week is
'Weekly menu tag: the Monday of the week this protocol is offered for (set by the Sunday review). Gym-day plans pick from where menu_week = date_trunc(''week'', <session date>)::date. Null = not part of a menu. Not part of approvals — changing it never voids one.';

comment on table exercise_catalog is
'READ-ONLY reference: Garmin''s exercise catalogue, refreshed weekly by the sync. Every exercise in a kind=''strength'' protocol must be a (category, exercise) pair from this table — the seed refuses any other pair, so look names up instead of guessing (e.g. where exercise ilike ''%PULL_UP%''). primary_muscles/secondary_muscles help pick antagonists; counterpart is the weighted/unweighted twin (WEIGHTED_PULL_UP <-> PULL_UP). In previews, show display_name and link video_url (a YouTube search for the exercise''s form — always valid; never invent a video URL).';

comment on column exercise_catalog.display_name is 'Readable name for previews, e.g. DUMBBELL_ROW -> Dumbbell Row. Generated.';
comment on column exercise_catalog.video_url is 'YouTube search link for how to do the exercise; put it next to every exercise in a preview. Generated — always valid.';

comment on view activity_exercise_set_rows is
'READ-ONLY: one row per set of a strength activity. set_type ACTIVE = work set, REST = rest between sets; a REST set of 15 s or less right before a timed hold is the "Get ready" countdown, not a real rest. weight_kg is already converted from Garmin''s grams (one decimal, as in the Garmin app). category/exercise = what the watch recorded: for a guided (generated) workout the planned exercise, for freestyle a guess — check probability (low = unreliable, UNKNOWN = not recognised). To compare planned vs done: join activities on activity_id, then training_plan on (activities.raw->>''workoutId'')::bigint = resolved_workout_id and (raw->>''startTimeLocal'')::date = enacted_date.';
