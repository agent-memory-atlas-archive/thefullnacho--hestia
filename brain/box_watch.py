"""Box watch — the whelping box's air temperature against this week's band, every two minutes.

Deterministic sibling of puppy_watch. The kennel box (`deploy/esphome/kennel-box.yaml`) puts
the Govee H5075 in the whelping box into Home Assistant; this reads it next to the litter in
`records` and pushes when the box has been out of range long enough to matter:

  - Too hot       — above the week's high. The heat lamp is the usual cause, and the dam lies
                    in there too.
  - Too cold      — below the week's low. The lamp or the pad has probably failed, and
                    chilling is the #1 neonatal killer.
  - No reading    — the sensor went quiet (dead coin cell, board unplugged), so the box is
                    unwatched. Said out loud, the same way puppy_watch reports an unweighed pup.
  - Back in range — once, after any of the above was pushed.

The week comes from the litter's whelp date in records, read on every run, so the band steps
down on its own and the watch arms as soon as the first pup is logged. Nothing is set by hand
and nothing is copied into HA to go stale. Before a whelp and after WATCH_DAYS it is silent.

Bands follow the house practice in `brain/skills/whelping/references/knowledge.md`: about 90F
at pup height in week one (from the last litter), stepping down to about 75F by week four.
Only week one is confirmed here; the later bands are general guidance until a litter says
otherwise.

No LLM anywhere in this file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import httpx

import config  # puts brain/ on sys.path + owns paths

config.load_secrets()
import records_store  # noqa: E402  (after config puts brain/ on the path)

HA_URL = os.environ.get("HA_URL", "http://hl-relay:8124").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
NOTIFY = os.environ.get("BOX_NOTIFY", os.environ.get("PUPPY_NOTIFY", "mobile_app_alexs_iphone"))
ENTITY = os.environ.get("BOX_ENTITY", "sensor.kennel_box_box_temperature")
WATCH_DAYS = int(os.environ.get("BOX_WATCH_DAYS", "35"))
SUSTAIN_MIN = int(os.environ.get("BOX_SUSTAIN_MIN", "5"))   # a door left open is not an alert
REPEAT_MIN = int(os.environ.get("BOX_REPEAT_MIN", "30"))    # while it stays wrong, say so again
SILENT_MIN = int(os.environ.get("BOX_SILENT_MIN", "10"))    # matches the board's own blanking
STATE_PATH = config.BOX_STATE

# (through litter day, low F, high F)
BANDS = (
    (7, 82.0, 93.0),    # week one: ~90 at pup height, confirmed from the last litter
    (21, 75.0, 88.0),   # weeks two and three: ~80
    (35, 68.0, 83.0),   # weeks four and five: ~75
)


def band(age_days: int) -> tuple[float, float] | None:
    """This litter day's (low, high) in F, or None once the litter is past the last band."""
    for through, low, high in BANDS:
        if age_days <= through:
            return low, high
    return None


def box_temp_f(state: dict | None, now: dt.datetime) -> float | None:
    """The box temperature in F from an HA state object, or None when it can't be trusted.

    None covers the entity missing, `unknown`/`unavailable` (the board blanks its own value
    after ten silent minutes), and a value HA hasn't heard repeated within SILENT_MIN."""
    if not state or state.get("state") in (None, "unknown", "unavailable", ""):
        return None
    try:
        value = float(state["state"])
    except (TypeError, ValueError):
        return None
    heard = state.get("last_reported") or state.get("last_updated")
    if heard:
        age = now - dt.datetime.fromisoformat(heard)
        if age > dt.timedelta(minutes=SILENT_MIN):
            return None
    if state.get("attributes", {}).get("unit_of_measurement") == "°C":
        value = value * 9 / 5 + 32
    return value


def classify(temp_f: float | None, low: float, high: float) -> str:
    if temp_f is None:
        return "silent"
    if temp_f > high:
        return "hot"
    if temp_f < low:
        return "cold"
    return "ok"


def _message(status: str, temp_f: float | None, low: float, high: float, age_days: int,
             minutes: int) -> str:
    day = f"day {age_days}"
    if status == "hot":
        return (f"Whelping box is {temp_f:.1f}°F, above {high:.0f} for {minutes} min ({day}). "
                f"Raise or switch off the heat lamp, and check Lily isn't panting.")
    if status == "cold":
        return (f"Whelping box is {temp_f:.1f}°F, below {low:.0f} for {minutes} min ({day}). "
                f"Check the heat lamp and the pad are both on. Pups piled up means cold.")
    if status == "silent":
        return (f"No reading from the whelping box for over {SILENT_MIN} min ({day}). Check "
                f"the Govee battery and that the kennel box is powered. The box is unwatched "
                f"until it's back.")
    return f"Whelping box back in range: {temp_f:.1f}°F ({day}, band {low:.0f}-{high:.0f})."


def step(prev: dict, status: str, temp_f: float | None, low: float, high: float,
         age_days: int, now: dt.datetime) -> tuple[str | None, dict]:
    """One run of the alert state machine: (message or None, new state).

    An out-of-range status has to hold for SUSTAIN_MIN before the first push, then repeats
    every REPEAT_MIN while it lasts. "No reading" already waited SILENT_MIN, so it pushes on
    sight. Recovery is announced only when the problem was announced."""
    stamp = now.isoformat()
    if status != prev.get("status"):
        was_pushed = bool(prev.get("pushed_at")) and prev.get("status") != "ok"
        state = {"status": status, "since": stamp, "pushed_at": None}
        if status == "ok":
            msg = _message("ok", temp_f, low, high, age_days, 0) if was_pushed else None
            return msg, state
    else:
        state = dict(prev)
        if status == "ok":
            return None, state

    since = dt.datetime.fromisoformat(state["since"])
    held = int((now - since).total_seconds() // 60)
    if status != "silent" and held < SUSTAIN_MIN:
        return None, state
    pushed = state.get("pushed_at")
    if pushed and now - dt.datetime.fromisoformat(pushed) < dt.timedelta(minutes=REPEAT_MIN):
        return None, state
    state["pushed_at"] = stamp
    return _message(status, temp_f, low, high, age_days, held), state


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def fetch_state() -> dict | None:
    r = httpx.get(f"{HA_URL}/api/states/{ENTITY}",
                  headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def run(now: dt.datetime | None = None, fetch=fetch_state, persist: bool = True) -> str | None:
    """The message to push this run, or None. Silent with no litter in the watch window."""
    now = now or dt.datetime.now().astimezone()
    litter = records_store.active_litter(WATCH_DAYS, now=now)
    limits = band(litter["age_days"]) if litter else None
    if not limits:
        if persist and STATE_PATH.exists():
            STATE_PATH.unlink()  # a finished litter's last status must not greet the next one
        return None
    low, high = limits
    temp_f = box_temp_f(fetch(), now)
    status = classify(temp_f, low, high)
    msg, state = step(_load_state(), status, temp_f, low, high, litter["age_days"], now)
    if persist:
        _save_state(state)
    return msg


def push(message: str) -> None:
    httpx.post(f"{HA_URL}/api/services/notify/{NOTIFY}",
               headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
               json={"title": "🌡️ Whelping box", "message": message}, timeout=15).raise_for_status()


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    try:
        msg = run(persist=not dry_run)
    except Exception as e:  # noqa: BLE001 — a watcher that crashes is a watcher nobody notices
        print(f"box-watch: failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not msg:
        return 0
    if dry_run:
        print("box-watch (dry-run) would push:\n" + msg)
        return 0
    push(msg)
    print("box-watch: pushed: " + msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
