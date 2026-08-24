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

from flask import Flask, redirect, render_template, request, url_for

app = Flask(__name__)

TODO_PATH = Path(os.environ.get("TODO_PATH", "data/todo.md"))

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
    TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TODO_PATH.exists():
        TODO_PATH.write_text("# To do\n\n", encoding="utf-8")


def read_file() -> str:
    ensure_file()
    return TODO_PATH.read_text(encoding="utf-8")


def write_file(content: str) -> None:
    """Atomic-ish write: temp file then rename, so a crash mid-write
    can't leave you with a truncated list."""
    ensure_file()
    tmp = TODO_PATH.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(TODO_PATH)


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
        path=TODO_PATH.name,
    )


@app.route("/task", methods=["POST"])
def add_task():
    """Typed input. Stored verbatim -- no parsing, no LLM, no surprises."""
    text = (request.form.get("text") or request.json.get("text", "")).strip()
    if not text:
        return redirect(url_for("index"))

    with _lock:
        content = read_file()
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"- [ ] {text} <!--id:{new_id()}-->\n"
        write_file(content)

    return redirect(url_for("index"))


@app.route("/toggle/<task_id>", methods=["POST"])
def toggle(task_id):
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
            current_mtime = TODO_PATH.stat().st_mtime
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
        mtime=TODO_PATH.stat().st_mtime,
        conflict=None,
    )


if __name__ == "__main__":
    ensure_file()
    app.run(host="0.0.0.0", port=8080, debug=True)
