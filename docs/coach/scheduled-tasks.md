# Scheduled tasks for the coach Project

Create these in the "Climbing & Alpinism Coach" Project as scheduled tasks (Cowork). They run
in the cloud and send a push to the Claude app. They need migration 0013 and the Project
instruction additions in [project-instructions-additions.md](project-instructions-additions.md).

| Task | Schedule (Warsaw) | Writes |
|---|---|---|
| Weekly review + menu | Sunday 19:00 | `protocols` only (the menu) |
| Gym-day plan | Tuesday and Thursday 16:00 | one `training_plan` row for today |
| Sunday plan | Saturday 20:00 | one `training_plan` row for tomorrow |
| Recovery brief (existing) | the other days 16:00 | nothing (keep its "never use run_write_sql" line) |

Nothing reaches the watch until you run `uv run plan_admin.py approve` and publish.

**First run:** use **Run now** once on the gym-day task. If a scheduled run can't write to
the database, the prompts fall back to showing you the exact SQL and waiting for "go" in chat.

## 1. Weekly review + menu (Sunday 19:00)

```
Weekly review and next week's menu. Use my Garmin data through the database tools.

1. Check whether this morning's session is in activities (startTimeLocal today). If it isn't, say "Today's session hasn't synced yet. Run the garmin-sync workflow in GitHub Actions if you want it included" and review the rest of the week.
2. Review Monday to today:
   - sessions done vs training_plan;
   - activity_exercise_set_rows for the loads and reps I actually did vs what was prescribed;
   - the recovery trend: sleep, HRV, resting HR, body battery.
3. Name 2–4 focus points for next week, each with one line of reasoning from the Lattice knowledge files and my data.
4. Write next week's menu as protocols rows with run_write_sql. Set menu_week = current_date + 1 (tomorrow, next week's Monday; not date_trunc('week', current_date), which on a Sunday gives last Monday), use new slugs, and follow the protocols table description (pairs for per-side work, rest from Lattice). Usually that's one hangboard session, one or two strength sessions, and a lighter alternative for low-recovery days. Do NOT insert any training_plan rows.
5. Show the preview: the focus points, then one table per session with the columns Exercise (video) | Prescription | Why this exercise | Evidence, as in my Project instructions.
If run_write_sql isn't available or fails, show the exact INSERT statements instead and say "reply go to save the menu".
```

## 2. Gym-day plan (Tuesday and Thursday 16:00)

```
Gym-day plan for TODAY (I leave for the gym around 19:00). Use my Garmin data through the database tools.

1. Check training_plan for rows with scheduled_date = current_date.
   - If one has skip = false, show it with its status and stop. Tell me to run `uv run plan_admin.py approve` if it isn't scheduled yet.
   - If only skipped rows exist, I cancelled that session. Don't pick the same protocol again (the database refuses a second row for the same protocol on the same day). Choose a different menu session or recommend just climbing.
2. Check today's recovery: last night's sleep, HRV, resting HR and body battery vs my recent average, plus training load over the last few days. Flag anything off.
3. From this week's menu (protocols where menu_week = date_trunc('week', current_date)::date), pick the session my recovery supports today. Choose the lighter alternative or "just climb" if recovery is poor. If there's no menu this week, say so and suggest one session from the Lattice guidance instead (written as a new protocol).
4. Insert ONE training_plan row with run_write_sql: scheduled_date = current_date, source = 'generated', protocol_slug = the chosen protocol, note = one line of intent.
5. Show the short preview table (Exercise (video) | Prescription | Why | Evidence), one line on why this session fits today's recovery, the row id, and: "run `uv run plan_admin.py approve`, tick the row, and answer yes to publish".
If run_write_sql isn't available or fails, show the exact INSERT instead and say "reply go to save it".
```

## 3. Sunday plan (Saturday 20:00)

```
Gym-day plan for TOMORROW, Sunday (I climb around 08:00). Same as the gym-day plan, with these changes:
- Use the date current_date + 1 everywhere: check for existing rows on that date (the same rules for skipped rows apply), and insert scheduled_date = current_date + 1.
- The menu week is date_trunc('week', current_date + 1)::date.
- Base the recovery check on today's data. Say that tonight's sleep isn't known yet, and give a fallback: what to drop or lighten if I wake up tired.
- End with the row id and: "run `uv run plan_admin.py approve` tonight, tick the row, and answer yes to publish".
If run_write_sql isn't available or fails, show the exact INSERT instead and say "reply go to save it".
```
