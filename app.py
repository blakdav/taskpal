"""
Markdown-backed to-do list.

The file on disk is the source of truth. Every request does a
read-modify-write against it, so Obsidian / vim / Syncthing can touch the
same file without this app losing their changes.
"""

import os
import re
import secrets
import threading
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)

TASKPAL_PATH = Path(os.environ.get("TASKPAL_PATH", "data/taskpal.md"))

# --- transcription -----------------------------------------------------
# Defaults to OpenAI. Point TRANSCRIBE_URL at a local speaches / LocalAI
# instance to move this off the cloud -- the request shape is identical.
TRANSCRIBE_URL = os.environ.get(
    "TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions"
)
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
TRANSCRIBE_KEY = os.environ.get("OPENAI_API_KEY", "")
# Comma-separated words to bias the recogniser toward. Names, jargon, and
# anything it reliably mangles.
TRANSCRIBE_HINT = os.environ.get("TRANSCRIBE_HINT", "")

# Optional shared secret. When set, write routes require
# `Authorization: Bearer <token>`. Leave unset behind WireGuard.
TASKPAL_TOKEN = os.environ.get("TASKPAL_TOKEN", "")

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # OpenAI's per-request ceiling

# Serialises this app's own writes. Does NOT protect against other processes
# editing the file -- that's what the mtime guard on /edit is for.
_lock = threading.Lock()

# - [ ] Some task <!--id:a3f2c1-->
TASK_RE = re.compile(
    r"^(?P<indent>\s*)-\s\[(?P<mark>[ xX])\]\s*"
    r"(?P<text>.*?)"
    r"(?:\s*<!--id:(?P<id>[0-9a-f]{6})-->)?\s*$"
)


def new_id() -> str:
    return secrets.token_hex(3)


def ensure_file() -> None:
    TASKPAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TASKPAL_PATH.exists():
        TASKPAL_PATH.write_text("# To do\n\n", encoding="utf-8")


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


def parse(content: str):
    """Return (tasks, healed_content).

    Any task line missing an id gets one assigned, so tasks you add by hand
    in Obsidian become clickable here.
    """
    tasks, lines, changed = [], content.splitlines(), False

    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if not m or not m.group("text").strip():
            continue

        tid = m.group("id")
        if not tid:
            tid = new_id()
            lines[i] = f"{m.group('indent')}- [{m.group('mark')}] " \
                       f"{m.group('text').strip()} <!--id:{tid}-->"
            changed = True

        tasks.append({
            "id": tid,
            "text": m.group("text").strip(),
            "done": m.group("mark").lower() == "x",
            "raw": lines[i],
        })

    healed = "\n".join(lines) + "\n" if changed else content
    return tasks, healed


@app.route("/")
def index():
    with _lock:
        content = read_file()
        tasks, healed = parse(content)
        if healed != content:
            write_file(healed)

    return render_template(
        "index.html",
        open_tasks=[t for t in tasks if not t["done"]],
        done_tasks=[t for t in tasks if t["done"]],
        path=TASKPAL_PATH.name,
    )


def append_task(text: str) -> str:
    """Append one task line. Returns its id. The single write path -- typed
    input and voice input both land here."""
    tid = new_id()
    with _lock:
        content = read_file()
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"- [ ] {text} <!--id:{tid}-->\n"
        write_file(content)
    return tid


def authorised() -> bool:
    if not TASKPAL_TOKEN:
        return True
    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.startswith("Bearer ") else ""
    return secrets.compare_digest(supplied, TASKPAL_TOKEN)


@app.route("/task", methods=["POST"])
def add_task():
    """Typed input. Stored verbatim -- no parsing, no LLM, no surprises."""
    if not authorised():
        return jsonify(error="unauthorised"), 401

    payload = request.get_json(silent=True) or {}
    text = (request.form.get("text") or payload.get("text") or "").strip()
    if not text:
        return redirect(url_for("index"))

    append_task(text)

    # Browsers get a redirect; API clients get the task back.
    if request.is_json:
        return jsonify(ok=True, text=text)
    return redirect(url_for("index"))


@app.route("/task/voice", methods=["POST"])
def add_task_voice():
    """Audio in, task out.

    Send the recording as multipart form-data under `file`. Responds with the
    transcript so the caller can show you what actually landed -- a silent
    success you can't verify is worse than a visible failure.
    """
    if not authorised():
        return jsonify(error="unauthorised"), 401

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
            files={"file": (audio.filename, blob, audio.mimetype
                            or "application/octet-stream")},
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

    append_task(text)
    return jsonify(ok=True, text=text)


@app.route("/toggle/<task_id>", methods=["POST"])
def toggle(task_id):
    if not authorised():
        return jsonify(error="unauthorised"), 401

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

    return redirect(url_for("index"))


@app.route("/delete/<task_id>", methods=["POST"])
def delete(task_id):
    if not authorised():
        return jsonify(error="unauthorised"), 401

    with _lock:
        lines = read_file().splitlines()
        kept = [
            ln for ln in lines
            if not ((m := TASK_RE.match(ln)) and m.group("id") == task_id)
        ]
        write_file("\n".join(kept) + "\n")

    return redirect(url_for("index"))


@app.route("/clear-done", methods=["POST"])
def clear_done():
    if not authorised():
        return jsonify(error="unauthorised"), 401

    with _lock:
        lines = read_file().splitlines()
        kept = [
            ln for ln in lines
            if not ((m := TASK_RE.match(ln)) and m.group("mark").lower() == "x")
        ]
        write_file("\n".join(kept) + "\n")

    return redirect(url_for("index"))


@app.route("/edit", methods=["GET", "POST"])
def edit():
    """Raw file editor. The escape hatch for anything the UI can't express."""
    ensure_file()

    if request.method == "POST":
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
