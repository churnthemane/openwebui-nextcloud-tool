# Open WebUI — Nextcloud Document Sync Tool

A production-ready Open WebUI Tool that gives any AI model live read/write access to a Nextcloud folder via WebDAV. Models can create, read, update, copy, move, delete, version, and export files through plain English conversation.

All 13 functions emit live status events (`__event_emitter__`) so you can see exactly what the tool is doing in real time — "Saving report.md to Nextcloud..." → "✅ Saved: report.md" — rather than waiting in silence.

## What it does

13 functions exposed to the model:

| Function | Description |
|---|---|
| `list_nextcloud_folder` | List files in the shared folder (one level) |
| `list_folder_recursive` | Full recursive directory tree |
| `sync_document_to_nextcloud` | Create or overwrite a file |
| `read_nextcloud_file` | Read .txt, .md, .csv, .json, .pdf, .docx |
| `get_file_info` | File metadata: size, modified date, file ID |
| `copy_nextcloud_file` | Copy a file or folder |
| `move_nextcloud_file` | Move or rename a file or folder |
| `create_nextcloud_folder` | Create a folder (and any missing parents) |
| `delete_nextcloud_file` | Delete with a mandatory dry-run confirmation step |
| `list_file_versions` | List Nextcloud version history for a file |
| `restore_file_version` | Restore a file to a previous version |
| `export_as_pdf` | Export to formatted PDF saved in Nextcloud |
| `export_as_docx` | Export to Word .docx saved in Nextcloud |

## Requirements

- Open WebUI running in Docker
- Nextcloud instance with:
  - WebDAV enabled (default)
  - Files Versioning app enabled (for version history)
  - A dedicated bot account with an app password
- Python packages in the Open WebUI container: `fpdf2`, `python-docx`, `pypdf`

## How the bot account and auto-sharing works

The recommended setup is a dedicated Nextcloud bot account (e.g. `ai-assistant`) that the tool authenticates as. When the tool creates the working folder for the first time, it automatically shares that folder with whoever you specify in `NEXTCLOUD_SHARE_WITH` — so your real users immediately have access without any manual sharing steps.

**Example:** Set `NEXTCLOUD_SHARE_WITH=group:everyone` and every user in Nextcloud's built-in `everyone` group will get read/write access to the AI's working folder the moment it's created.

The share is created with read/write permissions (no re-share). If the folder already exists and is already shared with a target, the share is skipped — it won't duplicate.

### Changing who gets auto-shared

Edit the `NEXTCLOUD_SHARE_WITH` valve (or environment variable). The format is a comma-separated list:

```
# Share with an entire group
group:everyone

# Share with a specific user
user:alice

# Share with multiple targets
group:staff,user:alice,user:bob
```

To disable auto-sharing entirely, leave `NEXTCLOUD_SHARE_WITH` blank.

> **Note:** Auto-sharing only fires when the tool creates a new folder via `create_nextcloud_folder`. If your working folder already exists, set the share manually in Nextcloud or delete and recreate the folder with the valve set.

## Installation

### 1a. Automated install via AI agent

If you are using an AI coding assistant (Claude Code, Cursor, etc.) with shell access to your server, you can hand off the entire installation:

> Point your AI at [AI_INSTALL.md](AI_INSTALL.md) and say: *"Follow the instructions in AI_INSTALL.md to install the Nextcloud Document Sync tool on my server."*

It will ask you for credentials, run all the steps, and confirm when done. Skip to [Running tests](#running-tests) to verify afterwards.

---

### 1. Install Python dependencies

Add this to your Open WebUI service in `docker-compose.yml`:

```yaml
entrypoint: ["/bin/sh", "-c", "pip install python-docx fpdf2 pypdf --quiet && bash start.sh"]
```

Then restart: `docker compose restart openwebui`

### 2. Create a dedicated bot account in Nextcloud

Create a new Nextcloud user (e.g. `ai-assistant`) that the tool will authenticate as. Then go to that account's **Settings → Security → Devices & Sessions → Create new app password** and note the generated password.

Using a dedicated account keeps the AI's activity separate from your personal account and makes it easy to revoke access later.

### 3. Register the tool in Open WebUI

1. Go to **Workspace → Tools → +**
2. Name: `Nextcloud Document Sync`, ID: `nextcloud_document_sync`
3. Paste the contents of `nextcloud_document_sync.py` into the editor
4. Save

### 4. Configure the tool valves

In the tool settings, set:

| Valve | Value |
|---|---|
| `NEXTCLOUD_URL` | `https://nextcloud.yourdomain.com` |
| `NEXTCLOUD_USER` | The bot account username |
| `NEXTCLOUD_APP_PASS` | The app password from step 2 |
| `NEXTCLOUD_FOLDER` | Folder the AI works in, e.g. `/Shared/AI-Docs/` |
| `NEXTCLOUD_SHARE_WITH` | Who to auto-share the folder with, e.g. `group:everyone` |

All valves can also be set as environment variables on the Open WebUI container.

### 5. Attach the tool to a model

1. Go to **Workspace → Models → (your model) → Edit**
2. Under **Tools**, enable `Nextcloud Document Sync`
3. Save

### 6. Choose a model that reliably calls tools

**Not all models call tools reliably.** Some large models (including several Gemini variants) will read the tool schema, acknowledge the instruction to use it, and then respond as if they called the tool — without ever issuing an actual function call. You get a confident "Done! I saved your file." and no file on disk.

**Recommended for analysis + file ops in one shot: `mistral-large-latest`**

Mistral Large reliably issues tool calls when instructed and has the reasoning depth for complex tasks — legal drafting, document analysis, multi-step workflows. It will draft the document and save it to Nextcloud in a single response without needing a follow-up prompt.

**Recommended for a dedicated file manager model: `mistral-small-latest`**

If you want a lightweight model whose only job is file operations (no drafting, no analysis), Mistral Small is fast, cheap, and calls tools without fail. Pair it with the system prompt below as its entire identity.

Models confirmed to work reliably:
- `mistral-large-latest` ✅ (best for analysis + tool use combined)
- `mistral-small-latest` ✅ (best for dedicated file management)
- `mistral-small-2603` ✅

Models that may hallucinate tool calls:
- Gemini 2.5 Flash ❌ (ignores tool-calling instructions)
- Gemini 2.5 Pro ⚠️ (fakes saves when document content is already in context via RAG)

> **Tip — dedicated file manager model:** Consider creating one dedicated model (e.g. `TheSeventhScribe`) backed by Mistral Small with only this tool attached and the system prompt below as its entire identity. Your analysis models handle drafting; the file manager handles persistence. File uploads should be disabled on this model — content should come from other models, not user uploads, to avoid RAG chunking corrupting the saved output.

### 7. Add system prompt rules

For reliable tool-calling behavior, prepend these rules to your model's system prompt. If using a dedicated file manager model, these can be the entire system prompt:

```
NEXTCLOUD PRIORITY RULES — READ FIRST:
You have live access to a Nextcloud shared folder via the nextcloud_document_sync tool. These rules override everything else when file operations are involved.

RULE 1: When the user asks you to draft, write, create, or produce any document — complete the ENTIRE document first, then IMMEDIATELY call sync_document_to_nextcloud(content="...", filename="...") in the same response. Never wait for a follow-up prompt to save.

RULE 2: To read an existing Nextcloud file, call read_nextcloud_file(path). Do NOT rely on files uploaded to the chat — always fetch from Nextcloud when a path is specified.

RULE 3: If the user mentions a specific filename, call list_nextcloud_folder() first to check whether it already exists before writing.

RULE 4: To delete anything, always call delete_nextcloud_file(path="...", confirmed=False) first to preview what will be deleted, then ask the user to confirm before calling with confirmed=True.

RULE 5: To restore a file version, you MUST call list_file_versions(path) FIRST and wait for the result. Only then call restore_file_version(path, version_id) using a version_id from that output. Never guess version IDs.
```

## Deploying updates

If you update `nextcloud_document_sync.py` and need to push changes into a running Open WebUI instance without going through the UI:

```bash
docker cp nextcloud_document_sync.py openwebui:/tmp/nextcloud_document_sync.py
docker cp scripts/update_tool_db.py openwebui:/tmp/update_tool_db.py
docker exec openwebui python3 /tmp/update_tool_db.py
```

## Running tests

```bash
pip install fpdf2 python-docx pypdf
python3 -m pytest test_nextcloud_tool.py -v
```

82 tests, no network required — all HTTP calls are mocked. Tests are written against synchronous wrappers so they work without an event loop setup.

## Security notes

- SSL verification is hardcoded `True` — the tool will not connect to a Nextcloud instance with an invalid certificate
- Use a Nextcloud **app password**, not a login password
- The delete function has a mandatory two-step confirmation guard — `confirmed=False` is the default and only shows what would be deleted

## Known server-side issues

If `copy_nextcloud_file`, `move_nextcloud_file`, or `restore_file_version` return Internal Server Error, your Nextcloud instance may have a corrupted session encryption key. Fix:

```bash
docker exec -u 33 nextcloud php occ config:system:set secret --value=$(openssl rand -hex 32)
docker restart nextcloud
```

This regenerates the HMAC key used for session crypto. Existing browser sessions will be invalidated — users will need to log in again.
