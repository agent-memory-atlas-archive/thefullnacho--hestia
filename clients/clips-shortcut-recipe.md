# "Save to Hestia" — iOS Shortcut recipe

One Shortcut, two modes. iOS runs the Share-Sheet branch when it receives a
video, and the standalone branch when run with no input (home screen icon,
Back Tap, Siri). Zero camera interaction in either mode.

Server: `POST http://alex-ms-7e51.tail1690ab.ts.net:8730/api/clips`
(MagicDNS name — stable across tailnet IP changes. Tailscale must be on.)
Auth: header `X-Ingest-Token` = your Hestia ingest token
(the same one the wildlife/photo Shortcut already uses — never share it).

## Setup (once)

1. Shortcuts app → + → name it **Save to Hestia**.
2. Tap ⓘ (Details) → enable **Show in Share Sheet** → under "Receives",
   select **Media** (and Images, harmless). Set "If there's no input" to
   **Continue** (we branch on it ourselves).
3. Build the actions below in order. Where it says *Magic Variable*, long-press
   to pick the output of the named earlier action.

## Actions

1. **If** → Condition: *Shortcut Input* **has any value**
   - *Inside If (Mode A — Share Sheet with video):*
   1. **Ask for Input** → Question: `Note (optional)`, Input Type: Text,
      Default Answer: (leave empty)
   2. **Ask for Input** → Question: `Tags, comma separated (optional)`,
      Input Type: Text
   3. **Get Current Location**
   4. **Get Details of** *Current Location* → Detail: **Latitude** → rename
      magic variable to `Lat`
   5. **Get Details of** *Current Location* → Detail: **Longitude** → rename
      to `Lon`
   6. **Date** → *Current Date* → **Format Date** → Custom:
      `yyyy-MM-dd'T'HH:mm:ss` → rename to `TakenAt`
   7. **Get Contents of URL** →
      - URL: `http://alex-ms-7e51.tail1690ab.ts.net:8730/api/clips`
      - Method: **POST**
      - Headers: Add new → Key `X-Ingest-Token`, Value: *your token*
      - Request Body: **Form** → Add new fields:
        - `file` = *Shortcut Input* (tap the field → File)
        - `sender` = `alex` (type it literally; on your wife's phone use her name)
        - `note` = *Provided Input* (the answer from step 1)
        - `tags` = *Provided Input* (step 2)
        - `lat` = *Lat*, `lon` = *Lon*, `taken_at` = *TakenAt*
   8. **Show Notification** → `Saved to Hestia ✓`
   - *Otherwise (Mode B — standalone, no input):*
   1. **Get Current Location** → **Get Details** → Latitude → `Lat`
   2. **Get Details of** *Current Location* → Longitude → `Lon`
   3. **Date** → Current Date → **Format Date** → Custom
      `yyyy-MM-dd'T'HH:mm:ss` → `TakenAt`
   4. **Dictate Text** → Language: English (stop on tap — say what you saw,
      e.g. "red fox by the north fence, limping slightly")
   5. **Get Contents of URL** → same URL, POST, same `X-Ingest-Token` header,
      Request Body: **Form** with fields (NO `file` field):
      `sender` = `alex`, `note` = *Dictated Text*, `lat` = *Lat*,
      `lon` = *Lon*, `taken_at` = *TakenAt*
   6. **Show Notification** → `Sighting logged ✓`
2. **End If**

## Notes

- Mode B is the wildlife one: one tap, speak one sentence, done — no camera,
  no viewfinder, eyes stay on the animal.
- Skipping the note/dictation still uploads; the server flags it
  `needs_note=1` and it appears under the **needs note** chip in
  `/clips` for later enrichment from the couch.
- `taken_at` records when it happened; the server also stamps `created_at`
  on upload, so late uploads stay truthful.
- Keep videos short — the server caps uploads at 200 MB.
- Browse everything at `http://alex-ms-7e51.tail1690ab.ts.net:8730/clips` (it opens the app's Library view)
  (Add to Home Screen for the fullscreen app feel).
