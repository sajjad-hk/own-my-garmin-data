import json
import pathlib

import pytest

from tests.test_workout_gen import _strip
from workout_gen import BUILDERS, make_strength_workout

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# What the reference workout (hand-built in Connect, 2026-10-15) prescribes.
REFERENCE_EXERCISES = [
    {"category": "PULL_UP", "exercise": "WEIGHTED_PULL_UP", "sets": 3, "reps": 3, "weight_kg": 10},
    {"category": "PLANK", "exercise": "PLANK", "sets": 3, "time_s": 60},
    {"category": "ROW", "exercise": "DUMBBELL_ROW", "sets": 3, "reps": 10, "weight_kg": 10},
]


def _norm(obj):
    """Garmin's own UI leaves two fields unset on some steps that we always
    send explicitly: skipLastRestStep (null on some repeat groups; we send
    false) and an exercise step's description (null on Bear Essentials, ''
    on newer workouts; we send '' when there's no note)."""
    if isinstance(obj, dict):
        out = {k: _norm(v) for k, v in obj.items()}
        if out.get("type") == "RepeatGroupDTO" and out.get("skipLastRestStep") is None:
            out["skipLastRestStep"] = False
        if out.get("type") == "ExecutableStepDTO" and out.get("exerciseName") and out.get("description") is None:
            out["description"] = ""
        return out
    if isinstance(obj, list):
        return [_norm(v) for v in obj]
    return obj


def test_matches_reference_field_for_field():
    expected = _norm(_strip(json.loads((FIXTURES / "strength_reference.json").read_text())))
    got = make_strength_workout("testing workout for coaching ", None, exercises=REFERENCE_EXERCISES, ready_s=0)
    assert got == expected


def test_timed_rests_match_bear_essentials():
    bear = _norm(_strip(json.loads((FIXTURES / "bear_essentials.json").read_text())))
    groups = bear["workoutSegments"][0]["workoutSteps"]
    exercises = []
    for g in groups:
        ex, rest = g["workoutSteps"]
        exercises.append({"category": ex["category"], "exercise": ex["exerciseName"], "sets": g["numberOfIterations"],
                          "reps": int(ex["endConditionValue"]), "rest_s": rest["endConditionValue"],
                          **({"weight_kg": ex["weightValue"]} if ex["weightValue"] is not None else {})})
    got = make_strength_workout("Bear Essentials", None, exercises=exercises, warmup=None, ready_s=0)
    assert got["workoutSegments"][0]["workoutSteps"] == groups


def test_note_goes_to_step_description_and_lap_rest_is_default():
    w = make_strength_workout("x", None, exercises=[
        {"category": "ROW", "exercise": "DUMBBELL_ROW", "sets": 2, "reps": 8, "note": "L"}])
    group = w["workoutSegments"][0]["workoutSteps"][1]
    ex, rest = group["workoutSteps"]
    assert ex["description"] == "L"
    assert rest["endCondition"]["conditionTypeKey"] == "lap.button" and rest["endConditionValue"] == 0.0


def _flatten(steps):
    for s in steps:
        yield s
        yield from _flatten(s.get("workoutSteps") or [])


def test_step_orders_unique_increasing_and_child_ids_per_group():
    # PLANK is timed, so its groups carry the default countdown step too.
    w = make_strength_workout("x", None, exercises=REFERENCE_EXERCISES * 2)
    steps = w["workoutSegments"][0]["workoutSteps"]
    orders = [s["stepOrder"] for s in _flatten(steps)]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)
    groups = [s for s in steps if s["type"] == "RepeatGroupDTO"]
    assert [g["childStepId"] for g in groups] == list(range(1, 7))
    for g in groups:
        assert {c["childStepId"] for c in g["workoutSteps"]} == {g["childStepId"]}


HANG = {"category": "CARRY", "exercise": "BAR_HOLDS", "sets": 3, "time_s": 10, "weight_kg": 6}


def _group_steps(w, index=1):
    return w["workoutSegments"][0]["workoutSteps"][index]["workoutSteps"]


def test_timed_hold_gets_ready_step_inside_its_repeat():
    ready, hold, rest = _group_steps(make_strength_workout("x", None, exercises=[HANG]))
    assert ready["stepType"]["stepTypeKey"] == "rest"
    assert ready["endCondition"]["conditionTypeKey"] == "time" and ready["endConditionValue"] == 5.0
    assert ready["description"] == "Get ready"
    assert ready["category"] is None and ready["exerciseName"] is None
    assert hold["exerciseName"] == "BAR_HOLDS" and hold["endConditionValue"] == 10.0
    assert rest["stepType"]["stepTypeKey"] == "rest" and rest["endCondition"]["conditionTypeKey"] == "lap.button"


def test_rep_exercise_has_no_ready_step():
    steps = _group_steps(make_strength_workout("x", None, exercises=[
        {"category": "ROW", "exercise": "DUMBBELL_ROW", "sets": 2, "reps": 8}]))
    assert [s["exerciseName"] for s in steps] == ["DUMBBELL_ROW", None]


def test_ready_s_zero_turns_it_off():
    steps = _group_steps(make_strength_workout("x", None, exercises=[HANG], ready_s=0))
    assert [s["exerciseName"] for s in steps] == ["BAR_HOLDS", None]


@pytest.mark.parametrize("bad", ["5", True, -1, 20])
def test_ready_s_rejects_bad_values(bad):
    with pytest.raises(ValueError, match="ready_s"):
        make_strength_workout("x", None, exercises=[HANG], ready_s=bad)


@pytest.mark.parametrize("ok", [0, 15])
def test_ready_s_accepts_bounds(ok):
    make_strength_workout("x", None, exercises=[HANG], ready_s=ok)


def _pair_reference():
    """The athlete's hand-built L/R reference, minus what they typed rather than
    the shape: notes on the rest steps, weightValue 0.0 ("no weight" in the
    UI — we send None, as the single-exercise reference does), and a stray
    kilometer unit Connect's editor left on one of the two timed rests (the
    other, and Bear Essentials' timed rests, have None)."""
    w = _norm(_strip(json.loads((FIXTURES / "strength_pair_reference.json").read_text())))
    for s in _flatten(w["workoutSegments"][0]["workoutSteps"]):
        if s.get("stepType", {}).get("stepTypeKey") == "rest":
            s["description"] = None
            s["preferredEndConditionUnit"] = None
        if s.get("weightValue") == 0.0:
            s["weightValue"] = None
    return w


ROW_PAIR = {"sets": 3, "pair": [
    {"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10, "rest_s": 30},
    {"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10, "rest_s": 90},
]}


def test_pair_matches_hand_built_reference():
    got = make_strength_workout("Strength Workout lr test ", None, exercises=[ROW_PAIR], ready_s=0)
    assert got == _pair_reference()


def test_pair_carries_weight_and_notes_per_item():
    pair = {"sets": 3, "pair": [{**ROW_PAIR["pair"][0], "weight_kg": 12, "note": "L"},
                                {**ROW_PAIR["pair"][1], "weight_kg": 12, "note": "R"}]}
    a, _, b, _ = _group_steps(make_strength_workout("x", None, exercises=[pair]))
    assert (a["description"], a["weightValue"], b["description"], b["weightValue"]) == ("L", 12.0, "R", 12.0)


def test_pair_of_timed_holds_gets_ready_before_each_side():
    hang = {"category": "CARRY", "exercise": "BAR_HOLDS", "time_s": 7}
    w = make_strength_workout("x", None, exercises=[
        HANG, {"sets": 3, "pair": [{**hang, "note": "L"}, {**hang, "note": "R"}]}])
    steps = _group_steps(w, index=2)
    kinds = [("ready" if s.get("description") == "Get ready" else s["exerciseName"] or "rest") for s in steps]
    assert kinds == ["ready", "BAR_HOLDS", "rest", "ready", "BAR_HOLDS", "rest"]
    assert {s["childStepId"] for s in steps} == {2}
    assert all(s["endCondition"]["conditionTypeKey"] == "lap.button" for s in (steps[2], steps[5]))
    orders = [s["stepOrder"] for s in _flatten(w["workoutSegments"][0]["workoutSteps"])]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)


_ITEM = {"category": "ROW", "exercise": "DUMBBELL_ROW", "reps": 10}


@pytest.mark.parametrize("block, fragment", [
    ({"sets": 3, "pair": [_ITEM]}, "exactly 2"),
    ({"sets": 3, "pair": [_ITEM, _ITEM, _ITEM]}, "exactly 2"),
    ({"sets": 3, "pair": [_ITEM, {**_ITEM, "sets": 3}]}, r"block 1, item 2.*sets"),
    ({"pair": [_ITEM, _ITEM]}, r"block 1.*sets"),
    ({"sets": 3, "pair": [_ITEM, _ITEM], "exercise": "DUMBBELL_ROW"}, "unknown key"),
    ({"sets": 3, "pair": [{**_ITEM, "time_s": 30}, _ITEM]}, "block 1, item 1"),
])
def test_pair_rejects_bad_shapes(block, fragment):
    with pytest.raises(ValueError, match=fragment):
        make_strength_workout("x", None, exercises=[block])


@pytest.mark.parametrize("bad, fragment", [
    ({"exercises": []}, "at least one exercise"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3, "reps": 5, "weight": 10}]}, "weight"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3}]}, "exactly one of reps / time_s"),
    ({"exercises": [{"category": "PLANK", "exercise": "PLANK", "sets": 3, "reps": 5, "time_s": 30}]}, "exactly one of reps / time_s"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 0, "reps": 5}]}, "sets"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3, "reps": 5, "weight_kg": -2}]}, "weight_kg"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "", "sets": 3, "reps": 5}]}, "exercise"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3, "reps": 5, "note": "x" * 51}]}, "note"),
    ({"exercises": REFERENCE_EXERCISES, "warmup": "5min"}, "warmup"),
])
def test_strength_rejects_bad_shapes(bad, fragment):
    with pytest.raises(ValueError, match=fragment):
        make_strength_workout("x", None, **bad)


def test_kind_seam():
    assert BUILDERS["strength"] is make_strength_workout
