# taskpal

Markdown-backed to-do list. `todo.md` on disk is the source of truth; the web
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

## Config

| Env         | Default        | Notes                                |
|-------------|----------------|--------------------------------------|
| `TODO_PATH` | `data/todo.md` | Set to `/data/todo.md` in the image  |
