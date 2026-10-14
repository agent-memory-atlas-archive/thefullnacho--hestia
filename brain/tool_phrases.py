"""What the brain is doing right now, as a short line for the app's status row.

Deterministic on purpose: the phrase comes from the tool name and its arguments, never from
the model, so "Turning off the kitchen lights…" is a fact about the call being dispatched,
not a claim about its outcome. The receipts say whether it worked.
"""
from __future__ import annotations

import re

_PLAIN = {
    "weather": "Checking the weather…",
    "status": "Checking on the house systems…",
    "search": "Searching the web…",
    "memory": "Checking what I remember…",
    "skill": "Looking up the details…",
}
_ACTIONS = {
    "shopping": {"add": "Adding to the shopping list…", "remove": "Taking it off the shopping list…",
                 "clear": "Clearing the shopping list…", "": "Reading the shopping list…"},
    "calendar": {"add": "Adding it to the calendar…", "": "Checking the calendar…"},
    "reminder": {"create": "Setting a reminder…", "cancel": "Cancelling the reminder…",
                 "": "Checking your reminders…"},
    "records": {"remember": "Writing that down…", "log": "Logging it…", "birth": "Recording the birth…",
                "weigh": "Recording the weight…", "harvest": "Logging the harvest…",
                "yield": "Logging the harvest…", "relate": "Linking those records…",
                "due": "Checking what maintenance is due…", "": "Looking through the records…"},
    "media": {"add": "Adding it to the library…", "status": "Checking the downloads…",
              "": "Searching the media library…"},
    "recipe": {"save": "Saving the recipe…", "import_url": "Fetching the recipe…",
               "list": "Listing the recipes…", "": "Looking up the recipe…"},
}


def _target(entity_id) -> str:
    """light.light_kitchen_lights -> 'kitchen lights'; a bare name passes through."""
    name = str(entity_id or "").split(".", 1)[-1]
    name = re.sub(r"^light_", "", name).replace("_", " ").strip()
    return name[:40]


def phrase(name: str, args) -> str:
    args = args if isinstance(args, dict) else {}
    action = str(args.get("action") or "")
    if name == "home":
        target = _target(args.get("entity_id"))
        if action in ("turn_on", "turn_off"):
            verb = "Turning on" if action == "turn_on" else "Turning off"
            return f"{verb} the {target}…" if target else f"{verb} the lights…"
        if action == "toggle":
            return f"Switching the {target}…" if target else "Switching the lights…"
        return f"Checking the {target}…" if target else "Checking the house…"
    if name in _PLAIN:
        return _PLAIN[name]
    table = _ACTIONS.get(name)
    if table:
        return table.get(action, table[""])
    return "Working on it…"
