# AI Installation Guide

> **This file is written for an AI agent (e.g. Claude Code) to execute on behalf of a user.**
> It is not a human tutorial. Steps are terse, explicit, and assume shell access to the target server.
> If you are a human reading this, refer to README.md instead.

---

## What you are doing

Installing the Nextcloud Document Sync tool into a running Open WebUI instance so that AI models on that instance can read and write files to a Nextcloud folder via WebDAV.

## What you need from the user before starting

Ask the user for the following if not already provided. Do not proceed without them.

- SSH access to the server (or confirm you already have it)
- `NEXTCLOUD_URL` — e.g. `https://nextcloud.example.com`
- `NEXTCLOUD_USER` — the Nextcloud bot account username
- `NEXTCLOUD_APP_PASS` — a Nextcloud app password for that account (not the login password)
- `NEXTCLOUD_FOLDER` — the folder the AI should work in, e.g. `/Shared/AI-Docs/`
- `NEXTCLOUD_SHARE_WITH` — who to auto-share the folder with, e.g. `group:everyone` (can be blank)
- The Docker container name for Open WebUI (default: `openwebui`)
- The path to the docker-compose file (default: `/opt/stack/docker-compose.yml`)

---

## Pre-flight checks

Run these and confirm before proceeding:

```bash
# Confirm Open WebUI container is running
docker ps --filter name=openwebui --format "{{.Names}} {{.Status}}"

# Confirm Nextcloud is reachable
curl -s -o /dev/null -w "%{http_code}" https://<NEXTCLOUD_URL>/status.php

# Confirm the bot account credentials work
curl -s -u <NEXTCLOUD_USER>:<NEXTCLOUD_APP_PASS> \
  https://<NEXTCLOUD_URL>/remote.php/dav/files/<NEXTCLOUD_USER>/ \
  -X PROPFIND -o /dev/null -w "%{http_code}"
# Expected: 207
```

If the credentials check returns anything other than 207, stop and tell the user their app password is wrong or the account doesn't exist.

---

## Step 1 — Install Python dependencies

The Open WebUI container needs `fpdf2`, `python-docx`, and `pypdf`. The cleanest way to persist this across restarts is via the docker-compose entrypoint.

Check if already set:
```bash
grep "pip install" /opt/stack/docker-compose.yml
```

If not present, add the entrypoint to the `openwebui` service:
```yaml
entrypoint: ["/bin/sh", "-c", "pip install python-docx fpdf2 pypdf --quiet && bash start.sh"]
```

Then restart:
```bash
cd /opt/stack && docker compose restart openwebui
```

Wait for the container to be healthy before continuing:
```bash
docker ps --filter name=openwebui --format "{{.Status}}"
```

---

## Step 2 — Copy the tool source into the container

```bash
docker cp nextcloud_document_sync.py openwebui:/tmp/nextcloud_document_sync.py
```

Verify:
```bash
docker exec openwebui ls -la /tmp/nextcloud_document_sync.py
```

---

## Step 3 — Create the tool record in Open WebUI

Open WebUI stores tools in a SQLite database. The tool must be created first (either via the UI or by inserting a DB record), then updated with the source code.

Check if the tool already exists:
```bash
docker exec openwebui python3 -c "
import sqlite3
db = sqlite3.connect('/app/backend/data/webui.db')
row = db.execute(\"SELECT id FROM tool WHERE id='nextcloud_document_sync'\").fetchone()
print('exists' if row else 'not found')
db.close()
"
```

**If it does not exist**, insert it:
```bash
docker exec openwebui python3 -c "
import sqlite3, time
db = sqlite3.connect('/app/backend/data/webui.db')
now = int(time.time())
db.execute('''INSERT INTO tool (id, user_id, name, content, specs, meta, updated_at, created_at)
              VALUES (?, '', ?, '', '[]', '{}', ?, ?)''',
           ('nextcloud_document_sync', 'Nextcloud Document Sync', now, now))
db.commit()
print('created')
db.close()
"
```

**Then deploy the source and specs** by copying and running the deploy script:
```bash
docker cp scripts/update_tool_db.py openwebui:/tmp/update_tool_db.py
docker exec openwebui python3 /tmp/update_tool_db.py
```

Expected output:
```
Updated tool 'nextcloud_document_sync' at <timestamp>
Specs: 13 functions
Content: <chars>
```

---

## Step 4 — Configure credentials via environment variables

The tool reads credentials from the Open WebUI container's environment. Add these to the `openwebui` service in docker-compose.yml under `environment:`:

```yaml
environment:
  NEXTCLOUD_URL: "https://<NEXTCLOUD_URL>"
  NEXTCLOUD_USER: "<NEXTCLOUD_USER>"
  NEXTCLOUD_APP_PASS: "<NEXTCLOUD_APP_PASS>"
  NEXTCLOUD_FOLDER: "<NEXTCLOUD_FOLDER>"
  NEXTCLOUD_SHARE_WITH: "<NEXTCLOUD_SHARE_WITH>"
```

Then restart for the env vars to take effect:
```bash
cd /opt/stack && docker compose restart openwebui
```

---

## Step 5 — Attach the tool to a model

Find the model ID you want to attach the tool to:
```bash
docker exec openwebui python3 -c "
import sqlite3, json
db = sqlite3.connect('/app/backend/data/webui.db')
rows = db.execute('SELECT id, name FROM model').fetchall()
for r in rows: print(r[0], '|', r[1])
db.close()
"
```

Attach the tool to the target model:
```bash
docker exec openwebui python3 -c "
import sqlite3, json, time
db = sqlite3.connect('/app/backend/data/webui.db')
row = db.execute('SELECT meta FROM model WHERE id=?', ('<MODEL_ID>',)).fetchone()
meta = json.loads(row[0]) if row[0] else {}
tool_ids = meta.get('toolIds', [])
if 'nextcloud_document_sync' not in tool_ids:
    tool_ids.append('nextcloud_document_sync')
meta['toolIds'] = tool_ids
db.execute('UPDATE model SET meta=?, updated_at=? WHERE id=?',
           (json.dumps(meta), int(time.time()), '<MODEL_ID>'))
db.commit()
print('Tool attached to model')
db.close()
"
```

---

## Step 6 — Add system prompt rules to the model

These rules are required for reliable tool-calling behavior, especially for the sequential restore operation:

```bash
docker exec openwebui python3 << 'PYEOF'
import sqlite3, json, time

NEXTCLOUD_RULES = """NEXTCLOUD PRIORITY RULES — READ FIRST:
You have live access to a Nextcloud shared folder via the nextcloud_document_sync tool.

RULE 1: If the user mentions ANY filename, your FIRST action is to call list_nextcloud_folder() to check if it exists.
RULE 2: To save any document, call sync_document_to_nextcloud(content="...", filename="...").
RULE 3: To delete, always call delete_nextcloud_file(path="...", confirmed=False) first, then ask the user before proceeding with confirmed=True.
RULE 4: To restore a file version, you MUST call list_file_versions(path) FIRST and wait for the result. Only then call restore_file_version(path, version_id) using a version_id from that output.

AVAILABLE TOOL FUNCTIONS:
- list_nextcloud_folder() — list files in the shared folder (one level)
- list_folder_recursive() — list full directory tree
- sync_document_to_nextcloud(content, filename) — create or overwrite a file
- read_nextcloud_file(path) — read .txt, .md, .csv, .json, .pdf, .docx
- get_file_info(path) — file metadata: size, modified date, file ID
- copy_nextcloud_file(source, destination) — copy a file or folder
- move_nextcloud_file(source, destination) — move or rename
- create_nextcloud_folder(path) — create folder and any missing parents
- delete_nextcloud_file(path, confirmed) — delete with confirmation guard
- list_file_versions(path) — list version history
- restore_file_version(path, version_id) — restore previous version (always call list_file_versions first)
- export_as_pdf(source_path, output_filename) — export to PDF saved in Nextcloud
- export_as_docx(source_path, output_filename) — export to Word .docx saved in Nextcloud

---
"""

MODEL_ID = "<MODEL_ID>"

db = sqlite3.connect("/app/backend/data/webui.db")
row = db.execute("SELECT params FROM model WHERE id=?", (MODEL_ID,)).fetchone()
params = json.loads(row[0]) if row[0] else {}
existing = params.get("system", "")
if "NEXTCLOUD PRIORITY RULES" not in existing:
    params["system"] = NEXTCLOUD_RULES + existing
    db.execute("UPDATE model SET params=?, updated_at=? WHERE id=?",
               (json.dumps(params), int(time.time()), MODEL_ID))
    db.commit()
    print("System prompt updated")
else:
    print("Rules already present, skipping")
db.close()
PYEOF
```

---

## Verification

Run a quick end-to-end smoke test:

```bash
docker exec openwebui python3 -c "
import sys
sys.path.insert(0, '/tmp')
exec(open('/tmp/nextcloud_document_sync.py').read())
t = Tools()
print(t.list_nextcloud_folder())
"
```

Expected: a folder listing from Nextcloud. If you see an auth error, recheck the credentials in Step 4.

---

## Done

Report back to the user:
- Tool is installed and attached to model `<MODEL_ID>`
- Credentials are set via environment variables
- The working folder is `<NEXTCLOUD_FOLDER>`, auto-shared with `<NEXTCLOUD_SHARE_WITH>`
- Tell them to start a new chat with the model and try: *"List my Nextcloud folder"*
