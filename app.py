"""
Markdown-backed to-do list with projects.

The file on disk is the source of truth. `##` headers are projects; tasks
belong to whichever header precedes them. Anything before the first header
lands in Inbox. Every request does a read-modify-write, so Obsidian / vim /
Syncthing can touch the same file without this app losing their changes.
"""

import os
import re
import secrets
import threading
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)

TASKPAL_PATH = Path(os.environ.get("TASKPAL_PATH", "data/taskpal.md"))

# Where voice tasks land, and the home for anything written before the
# first `##` header.
INBOX = os.environ.get("TASKPAL_INBOX", "Inbox")

# Completed tasks get moved here rather than deleted. It's a real `##` section
# in the file, just kept out of the sidebar.
ARCHIVE = os.environ.get("TASKPAL_ARCHIVE", "Archive")

# --- transcription -----------------------------------------------------
# Defaults to OpenAI. Point TRANSCRIBE_URL at a local speaches / LocalAI
# instance to move this off the cloud -- the request shape is identical.
TRANSCRIBE_URL = os.environ.get(
    "TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions"
)
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
TRANSCRIBE_KEY = os.environ.get("OPENAI_API_KEY", "")
TRANSCRIBE_HINT = os.environ.get("TRANSCRIBE_HINT", "")

# Optional shared secret. When set, write routes require
# `Authorization: Bearer <token>`. Leave unset behind WireGuard.
TASKPAL_TOKEN = os.environ.get("TASKPAL_TOKEN", "")

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # OpenAI's per-request ceiling

_lock = threading.Lock()

# - [ ] Some task <!--id:a3f2c1-->
TASK_RE = re.compile(
    r"^(?P<indent>\s*)-\s\[(?P<mark>[ xX])\]\s*"
    r"(?P<text>.*?)"
    r"(?:\s*<!--id:(?P<id>[0-9a-f]{6})-->)?\s*$"
)

# ## Project name
HEADER_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")

# Completion date, Obsidian Tasks style: ✅ 2026-08-24
DONE_RE = re.compile(r"\s*\u2705\s*(?P<date>\d{4}-\d{2}-\d{2})")


def new_id() -> str:
    return secrets.token_hex(3)


# --- file io -----------------------------------------------------------

def ensure_file() -> None:
    TASKPAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TASKPAL_PATH.exists():
        TASKPAL_PATH.write_text(f"# To do\n\n## {INBOX}\n\n", encoding="utf-8")


def read_file() -> str:
    ensure_file()
    return TASKPAL_PATH.read_text(encoding="utf-8")


def write_file(content: str) -> None:
    """Atomic-ish write: temp file then rename, so a crash mid-write
    can't leave you with a truncated list."""
    ensure_file()
    tmp = TASKPAL_PATH.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(TASKPAL_PATH)


# --- parsing -----------------------------------------------------------

def parse(content: str):
    """Return (tasks, healed_content).

    Each task carries the project it sits under. Tasks missing an id get one
    assigned, so items added by hand in Obsidian become clickable here.
    """
    tasks, lines, changed = [], content.splitlines(), False
    current = INBOX

    for i, line in enumerate(lines):
        h = HEADER_RE.match(line)
        if h:
            current = h.group("name")
            continue

        m = TASK_RE.match(line)
        if not m or not m.group("text").strip():
            continue

        tid = m.group("id")
        if not tid:
            tid = new_id()
            lines[i] = f"{m.group('indent')}- [{m.group('mark')}] " \
                       f"{m.group('text').strip()} <!--id:{tid}-->"
            changed = True

        body = m.group("text").strip()
        d = DONE_RE.search(body)
        if d:
            body = DONE_RE.sub("", body).strip()

        tasks.append({
            "id": tid,
            "text": body,
            "done": m.group("mark").lower() == "x",
            "done_on": d.group("date") if d else None,
            "project": current,
            "raw": lines[i],
        })

    healed = "\n".join(lines) + "\n" if changed else content
    return tasks, healed


def project_list(content: str, tasks):
    """Ordered project names with open-task counts.

    Order follows the file, so rearranging headers in Obsidian rearranges the
    sidebar. Empty projects still appear -- a project you just made shouldn't
    vanish because you haven't filled it yet.
    """
    names = []
    for line in content.splitlines():
        h = HEADER_RE.match(line)
        if h and h.group("name") not in names:
            names.append(h.group("name"))

    # Tasks sitting above the first header, or under a header that has since
    # been deleted.
    for t in tasks:
        if t["project"] not in names:
            names.append(t["project"])

    names = [n for n in names if n != ARCHIVE]

    # Inbox is where voice tasks land, so it stays pinned to the top no
    # matter where its header sits in the file.
    if INBOX in names:
        names.remove(INBOX)
    names.insert(0, INBOX)

    counts = {n: 0 for n in names}
    for t in tasks:
        if not t["done"]:
            counts[t["project"]] = counts.get(t["project"], 0) + 1

    return [{"name": n, "open": counts.get(n, 0)} for n in names]


# --- mutation ----------------------------------------------------------

def section_bounds(lines, project: str):
    """(start, end) line indices for a project's body, or None.

    `start` is the line after the header; `end` is one past the last line
    that belongs to the section.
    """
    start = None
    for i, line in enumerate(lines):
        h = HEADER_RE.match(line)
        if h and h.group("name") == project:
            start = i + 1
            break
    if start is None:
        return None

    end = len(lines)
    for j in range(start, len(lines)):
        if HEADER_RE.match(lines[j]):
            end = j
            break

    # Don't count trailing blank lines as part of the section.
    while end > start and not lines[end - 1].strip():
        end -= 1

    return start, end


def split_sections(lines):
    """(preamble, [(name, body_lines), ...]).

    Preamble is everything above the first `##` -- the H1, notes, whatever
    you keep at the top of the file. Reordering must never disturb it.
    """
    preamble, sections = [], []
    for line in lines:
        h = HEADER_RE.match(line)
        if h:
            sections.append([h.group("name"), []])
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return preamble, sections


def join_sections(preamble, sections):
    """Rebuild the file, normalising to one blank line between sections."""
    out = list(preamble)
    while out and not out[-1].strip():
        out.pop()

    for name, body in sections:
        trimmed = list(body)
        while trimmed and not trimmed[-1].strip():
            trimmed.pop()
        if out:
            out.append("")
        out.append(f"## {name}")
        out.extend(trimmed)

    return "\n".join(out) + "\n"


def insert_line(lines, project: str, line: str):
    """Put `line` at the end of a project's section, creating it if needed."""
    bounds = section_bounds(lines, project)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"## {project}")
        lines.append(line)
        return lines

    _, end = bounds
    lines.insert(end, line)
    return lines


def append_task(text: str, project: str = None) -> str:
    """Append one task. The single write path -- typed and voice both land
    here."""
    project = project or INBOX
    tid = new_id()
    entry = f"- [ ] {text} <!--id:{tid}-->"

    with _lock:
        lines = read_file().splitlines()
        insert_line(lines, project, entry)
        write_file("\n".join(lines) + "\n")
    return tid


def authorised() -> bool:
    if not TASKPAL_TOKEN:
        return True
    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.startswith("Bearer ") else ""
    return secrets.compare_digest(supplied, TASKPAL_TOKEN)


def deny():
    return jsonify(error="unauthorised"), 401


# --- views -------------------------------------------------------------

def render(active: str = None, archive: bool = False):
    with _lock:
        content = read_file()
        tasks, healed = parse(content)
        if healed != content:
            write_file(healed)
            content = healed

    projects = project_list(content, tasks)
    known = {p["name"] for p in projects}
    if active is not None and active not in known:
        active = None  # unknown project -> show everything

    if archive:
        shown = [t for t in tasks if t["project"] == ARCHIVE]
    elif active is None:
        shown = [t for t in tasks if t["project"] != ARCHIVE]
    else:
        shown = [t for t in tasks if t["project"] == active]

    return render_template(
        "index.html",
        open_tasks=[t for t in shown if not t["done"]],
        done_tasks=[t for t in shown if t["done"]],
        projects=projects,
        active=active,
        total_open=sum(p["open"] for p in projects),
        inbox=INBOX,
        archive=archive,
        archived_count=sum(1 for t in tasks if t["project"] == ARCHIVE),
        path=TASKPAL_PATH.name,
    )


ALL = "*"  # sentinel in return_to fields meaning "the unfiltered view"


@app.route("/")
def index():
    """Opens on whichever project sits at the top of the file. Reorder the
    sidebar and you've changed what greets you."""
    with _lock:
        content = read_file()
    tasks, _ = parse(content)
    projects = project_list(content, tasks)
    return render(projects[0]["name"] if projects else None)


@app.route("/all")
def all_view():
    return render(None)


@app.route("/p/<path:project>")
def project_view(project):
    return render(unquote(project))


def back_to(project: str = None):
    if project == ALL:
        return redirect(url_for("all_view"))
    if project:
        return redirect(url_for("project_view", project=quote(project)))
    return redirect(url_for("index"))


# --- write routes ------------------------------------------------------

@app.route("/task", methods=["POST"])
def add_task():
    """Typed input. Stored verbatim -- no parsing, no LLM, no surprises."""
    if not authorised():
        return deny()

    payload = request.get_json(silent=True) or {}
    text = (request.form.get("text") or payload.get("text") or "").strip()
    project = (request.form.get("project")
               or payload.get("project") or INBOX).strip()

    if not text:
        return back_to(request.form.get("project"))

    append_task(text, project)

    if request.is_json:
        return jsonify(ok=True, text=text, project=project)
    return back_to(request.form.get("return_to"))


@app.route("/task/voice", methods=["POST"])
def add_task_voice():
    """Audio in, task out. Always lands in the inbox -- the Action Button has
    no UI to pick a project with, and triage is cheaper than dictation
    friction.

    Send the recording as multipart form-data under `file`. Responds with the
    transcript so the caller can show you what actually landed.
    """
    if not authorised():
        return deny()

    if not TRANSCRIBE_KEY:
        return jsonify(error="No OPENAI_API_KEY configured"), 503

    audio = request.files.get("file")
    if not audio or not audio.filename:
        return jsonify(error="No audio uploaded under field 'file'"), 400

    blob = audio.read()
    if not blob:
        return jsonify(error="Empty recording"), 400
    if len(blob) > MAX_AUDIO_BYTES:
        return jsonify(error="Recording over the 25 MB limit"), 413

    data = {"model": TRANSCRIBE_MODEL}
    if TRANSCRIBE_HINT:
        data["prompt"] = TRANSCRIBE_HINT

    try:
        resp = requests.post(
            TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {TRANSCRIBE_KEY}"},
            files={"file": (audio.filename, blob,
                            audio.mimetype or "application/octet-stream")},
            data=data,
            timeout=60,
        )
    except requests.RequestException as exc:
        return jsonify(error=f"Transcription unreachable: {exc}"), 502

    if resp.status_code != 200:
        return jsonify(error=f"Transcription failed ({resp.status_code})",
                       detail=resp.text[:400]), 502

    text = (resp.json().get("text") or "").strip()
    # Speech-to-text ends most utterances with a full stop. A task isn't a
    # sentence, so drop it.
    text = text.rstrip(".").strip()

    if not text:
        return jsonify(error="Nothing recognised in that recording"), 422

    append_task(text, INBOX)
    return jsonify(ok=True, text=text, project=INBOX)


@app.route("/move/<task_id>", methods=["POST"])
def move(task_id):
    """Pull a task out of its section and drop it at the end of another."""
    if not authorised():
        return deny()

    target = (request.form.get("project") or "").strip()
    if not target:
        return back_to(request.form.get("return_to"))

    with _lock:
        lines = read_file().splitlines()
        entry = None
        for i, line in enumerate(lines):
            m = TASK_RE.match(line)
            if m and m.group("id") == task_id:
                entry = lines.pop(i)
                break
        if entry is not None:
            insert_line(lines, target, entry.strip())
            write_file("\n".join(lines) + "\n")

    return back_to(request.form.get("return_to"))


@app.route("/project", methods=["POST"])
def add_project():
    if not authorised():
        return deny()

    name = (request.form.get("name") or "").strip().lstrip("#").strip()
    if not name:
        return back_to(request.form.get("return_to"))

    with _lock:
        lines = read_file().splitlines()
        if section_bounds(lines, name) is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"## {name}")
            lines.append("")
            write_file("\n".join(lines) + "\n")

    return back_to(name)


@app.route("/project/<path:project>/<direction>", methods=["POST"])
def reorder_project(project, direction):
    """Swap a whole `## section` with its neighbour -- header, tasks, and any
    notes underneath move together."""
    if not authorised():
        return deny()

    project = unquote(project)
    step = -1 if direction == "up" else 1 if direction == "down" else 0
    if not step:
        return back_to(project)

    with _lock:
        preamble, sections = split_sections(read_file().splitlines())
        idx = next((i for i, (n, _) in enumerate(sections) if n == project), None)
        target = None if idx is None else idx + step
        if idx is not None and target is not None and 0 <= target < len(sections):
            sections[idx], sections[target] = sections[target], sections[idx]
            write_file(join_sections(preamble, sections))

    return back_to(project)


@app.route("/toggle/<task_id>", methods=["POST"])
def toggle(task_id):
    if not authorised():
        return deny()

    with _lock:
        lines = read_file().splitlines()
        for i, line in enumerate(lines):
            m = TASK_RE.match(line)
            if m and m.group("id") == task_id:
                mark = " " if m.group("mark").lower() == "x" else "x"
                lines[i] = f"{m.group('indent')}- [{mark}] " \
                           f"{m.group('text').strip()} <!--id:{task_id}-->"
                break
        write_file("\n".join(lines) + "\n")

    return back_to(request.form.get("return_to"))


@app.route("/delete/<task_id>", methods=["POST"])
def delete(task_id):
    if not authorised():
        return deny()

    with _lock:
        lines = read_file().splitlines()
        kept = [ln for ln in lines
                if not ((m := TASK_RE.match(ln)) and m.group("id") == task_id)]
        write_file("\n".join(kept) + "\n")

    return back_to(request.form.get("return_to"))


@app.route("/archive-done", methods=["POST"])
def archive_done():
    """Move completed tasks into the Archive section, stamped with today's
    date. Scoped to the current view, so archiving Work leaves finished items
    under Home alone.

    Nothing is deleted -- the point is that a completed task is a record, and
    the file is where records belong.
    """
    if not authorised():
        return deny()

    scope = (request.form.get("return_to") or "").strip()
    if scope == ALL:
        scope = ""

    stamp = date.today().isoformat()

    with _lock:
        lines = read_file().splitlines()
        kept, moved, current = [], [], INBOX

        for line in lines:
            h = HEADER_RE.match(line)
            if h:
                current = h.group("name")
                kept.append(line)
                continue

            m = TASK_RE.match(line)
            done = m and m.group("mark").lower() == "x"
            in_scope = (not scope) or current == scope

            if done and in_scope and current != ARCHIVE:
                text = m.group("text").strip()
                # Don't double-stamp something archived by hand already.
                if not DONE_RE.search(text):
                    text = f"{text} \u2705 {stamp}"
                tid = m.group("id") or new_id()
                moved.append(f"- [x] {text} <!--id:{tid}-->")
                continue

            kept.append(line)

        for entry in moved:
            insert_line(kept, ARCHIVE, entry)

        if moved:
            write_file("\n".join(kept) + "\n")

    return back_to(request.form.get("return_to"))


@app.route("/archive")
def archive_view():
    return render(None, archive=True)


@app.route("/edit", methods=["GET", "POST"])
def edit():
    """Raw file editor. The escape hatch for anything the UI can't express."""
    ensure_file()

    if request.method == "POST":
        if not authorised():
            return deny()
        seen_mtime = float(request.form.get("mtime", 0))
        with _lock:
            current_mtime = TASKPAL_PATH.stat().st_mtime
            # Someone else wrote to the file since this page was served.
            # Refuse rather than clobber their work.
            if abs(current_mtime - seen_mtime) > 0.001:
                return render_template(
                    "edit.html",
                    content=request.form.get("content", ""),
                    mtime=current_mtime,
                    conflict=read_file(),
                ), 409
            write_file(request.form.get("content", ""))
        return redirect(url_for("index"))

    return render_template(
        "edit.html",
        content=read_file(),
        mtime=TASKPAL_PATH.stat().st_mtime,
        conflict=None,
    )


if __name__ == "__main__":
    ensure_file()
    app.run(host="0.0.0.0", port=8080, debug=True)
