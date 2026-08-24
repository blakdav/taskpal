# taskpal

Markdown-backed to-do list. `taskpal.md` on disk is the source of truth; the web
page is a render of it. Nothing is ever stored as HTML.

## Run locally

    pip install -r requirements.txt
    python app.py            # http://localhost:8080

## Run in Docker

    mkdir -p /opt/docker/taskpal/data
    docker compose up -d      # http://<host>:8092

Or build on the host instead of pulling:

    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

## Routes

| Route            | Method   | What it does                       |
|------------------|----------|------------------------------------|
| `/`              | GET      | The list                           |
| `/task`          | POST     | Add a task. Stored verbatim.       |
| `/toggle/<id>`   | POST     | Flip `- [ ]` / `- [x]`             |
| `/delete/<id>`   | POST     | Remove a line                      |
| `/clear-done`    | POST     | Drop all completed tasks           |
| `/edit`          | GET/POST | Raw file editor, with mtime guard  |
| `/task/voice`    | POST     | Audio in, transcribed task out. Always Inbox. |
| `/all`           | GET      | Every project at once              |
| `/p/<project>`   | GET      | The list, filtered to one project  |
| `/project/<name>/<up\|down>` | POST | Reorder a project |
| `/archive`       | GET      | Completed tasks, with dates        |
| `/archive-done`  | POST     | Move done tasks to Archive         |
| `/move/<id>`     | POST     | Move a task to another project     |
| `/project`       | POST     | Create an empty project            |

`/task` accepts form-encoded or JSON `{"text": "..."}`, so the browser input
and an iOS Shortcut can hit the same endpoint.

## File format

    - [ ] Call the pharmacy <!--id:a3f2c1-->

The HTML comment is a stable id so toggles survive reordering, and it stays
invisible in any markdown viewer. Tasks added by hand in Obsidian or vim get
an id assigned on next page load.

## Keys

- `/` focus the input
- `s` show the raw markdown behind the page
- `t` switch dark / light (dark is the default; choice is remembered)
- `1`-`9` jump between projects (1 is All)

## Projects

`##` headers are projects. Tasks belong to whichever header sits above them;
anything before the first header lands in Inbox.

    # To do

    ## Inbox
    - [ ] call about the thing <!--id:27267e-->

    ## Work
    - [ ] read the paper <!--id:b8a93e-->

Inbox is pinned to the top of the sidebar and is what `/` opens, since it's
where voice tasks land. Everything else follows file order -- reorder with the
arrows in the sidebar, or by moving headers around in Obsidian. All and
Archive sit at the bottom.

## Archive

Completing a task doesn't delete it. The `archive` button moves finished tasks
into a `## Archive` section stamped with the date:

    ## Archive
    - [x] finished thing ✅ 2026-08-24 <!--id:7cf33e-->

That's the Obsidian Tasks date format, so the plugin reads it if you use it.
Archive is hidden from the sidebar until it has something in it, and its tasks
never show up in All. Archiving is scoped to the view you're in. Voice tasks always go to
Inbox -- the Action Button has no UI to pick a project with, so triage happens
afterwards via the arrow control on each row.

## Config

| Env            | Default           | Notes                                 |
|----------------|-------------------|---------------------------------------|
| `TASKPAL_PATH` | `data/taskpal.md` | Set to `/data/taskpal.md` in the image |
| `OPENAI_API_KEY` | *(unset)* | Required for `/task/voice` |
| `TRANSCRIBE_URL` | OpenAI | Point at speaches/LocalAI to go local |
| `TRANSCRIBE_MODEL` | `gpt-4o-transcribe` | |
| `TRANSCRIBE_HINT` | *(empty)* | Comma-separated vocabulary hints |
| `TASKPAL_TOKEN` | *(unset)* | When set, write routes need a bearer token |
| `TASKPAL_INBOX` | `Inbox` | Name of the default project |
| `TASKPAL_ARCHIVE` | `Archive` | Section completed tasks move to |

## Voice

Copy `.env.example` to `.env` and set `OPENAI_API_KEY` first. Then check the
route works before wiring anything to it:

    curl -X POST https://taskpal.example.com/task/voice -F file="@memo.m4a"

    {"ok": true, "text": "call the pharmacy", "project": "Inbox"}

Dictated tasks always land in the inbox. The Action Button has no UI to pick a
project with, and triage after the fact is cheaper than friction at capture.

## iPhone Shortcut

Three actions. Build it in the Shortcuts app, test it there until it's boring,
then bind it to the Action Button.

**1. Record Audio**

In the action's settings: *Start Recording* → **Immediately**, and
*Finish Recording* → **On Tap**.

Immediately means no confirmation screen between pressing the button and
recording — that screen is most of the friction. On Tap is worth the extra tap
over *After Pause*, which cuts you off mid-thought whenever you hesitate.

**2. Get Contents of URL**

| Field        | Value                                        |
|--------------|----------------------------------------------|
| URL          | `https://taskpal.example.com/task/voice`     |
| Method       | `POST`                                       |
| Headers      | *(none)*                                     |
| Request Body | **Form**                                     |

Then add one form field:

| Key    | Type     | Value                    |
|--------|----------|--------------------------|
| `file` | **File** | Recorded Audio           |

Two things go wrong here, and both return a confusing error:

- **The field type defaults to Text.** It must be **File**, or Shortcuts sends
  the filename as a string and the server answers
  `400 No audio uploaded under field 'file'`.
- **Don't set a `Content-Type` header.** Shortcuts generates the multipart
  boundary itself when Request Body is set to Form. Overriding the header
  strips the boundary and the request arrives malformed.

If `TASKPAL_TOKEN` is set, add exactly one header: `Authorization` with the
value `Bearer <your-token>`. Still nothing about content type.

**3. Show Notification**

Body: the `text` value from the response.

This step looks cosmetic and isn't. A silent failure means you believe you
captured something you didn't, and the first time that happens you stop
trusting the tool. The notification is also how you catch a mangled
transcription while you still remember what you said.

### Binding it to the Action Button

Settings → Action Button → scroll the carousel to **Shortcut** → choose yours.

It fires on release of a long press, and with *Start Recording: Immediately*
the mic is live at that moment — begin talking as you let go or you'll clip
the first word. Tap the screen to finish. It works from the Lock Screen
without unlocking, which is the actual use case, so test it locked.

For a Lock Screen control instead: long-press the Lock Screen → Customize →
tap either bottom control → swap the flashlight or camera for the Shortcut.

### Debugging

Get Contents of URL is silent, so add a **Quick Look** action right after it
while you're setting things up. It dumps the raw response on screen. Swap it
for Show Notification once things work.

| Response                                | Cause                                        |
|-----------------------------------------|----------------------------------------------|
| `400 No audio uploaded under field 'file'` | Form field is typed Text, not File        |
| `503 No OPENAI_API_KEY configured`      | `.env` never reached the container           |
| `502 Transcription failed (401)`        | Key rejected — check API credit              |
| `404`                                   | Wrong URL, or the container predates the route |
| `405 Method Not Allowed`                | Hitting `/task` instead of `/task/voice`     |
| Nothing at all                          | DNS or VPN — the phone can't reach the host  |

If the host is on an internal domain, the phone needs to be on your network or
VPN. Consider WireGuard On-Demand so the tunnel is up before you press the
button — otherwise the capture fails exactly when you're least able to notice.

### Typed tasks from a Shortcut

The same trick works without audio. Point Get Contents of URL at `/task` with
Request Body set to **JSON** and one text field `text`. Apple's on-device
**Dictate Text** action can fill it, which skips the upload and the API call
entirely — faster and free, at the cost of some accuracy.
