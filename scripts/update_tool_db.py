"""
Deploy nextcloud_document_sync.py into a running Open WebUI instance.

Usage:
    # Copy tool source into the container
    docker cp nextcloud_document_sync.py openwebui:/tmp/nextcloud_document_sync.py

    # Copy and run this script
    docker cp scripts/update_tool_db.py openwebui:/tmp/update_tool_db.py
    docker exec openwebui python3 /tmp/update_tool_db.py

The tool must already exist in Open WebUI (created via the UI first).
The script updates its source code and function specs in place.
"""

import sqlite3, json, time

TOOL_ID = "nextcloud_document_sync"
DB_PATH = "/app/backend/data/webui.db"
TOOL_SOURCE_PATH = "/tmp/nextcloud_document_sync.py"

db = sqlite3.connect(DB_PATH)

with open(TOOL_SOURCE_PATH, "r") as f:
    content = f.read()

new_specs = [
    {
        "name": "sync_document_to_nextcloud",
        "description": "Upload document content to Nextcloud via WebDAV PUT.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Full text content of the document."},
                "filename": {"type": "string", "description": "Explicit filename (e.g. 'report.md').", "default": ""},
                "topic": {"type": "string", "description": "Topic for filename matching/generation.", "default": ""},
            },
            "required": ["content"]
        }
    },
    {
        "name": "read_nextcloud_file",
        "description": "Read and return the text content of a file from Nextcloud. Supports .txt, .md, .csv, .json, .pdf, .docx.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, relative to NEXTCLOUD_FOLDER or absolute from user root."},
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_nextcloud_folder",
        "description": "List files and subfolders at a path (one level deep).",
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string", "description": "Folder to list. Empty = default folder.", "default": ""},
            },
            "required": []
        }
    },
    {
        "name": "list_folder_recursive",
        "description": "List all files and subfolders recursively.",
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string", "description": "Folder to list recursively. Empty = default folder.", "default": ""},
            },
            "required": []
        }
    },
    {
        "name": "get_file_info",
        "description": "Return metadata: name, size, last modified, content type, Nextcloud file ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or folder path."},
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_file_versions",
        "description": "List all stored versions of a file in Nextcloud version history.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to list versions for."},
            },
            "required": ["path"]
        }
    },
    {
        "name": "restore_file_version",
        "description": "Restore a file to a previous version. Always call list_file_versions first to get a valid version_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to restore."},
                "version_id": {"type": "string", "description": "Version ID from list_file_versions output."},
            },
            "required": ["path", "version_id"]
        }
    },
    {
        "name": "move_nextcloud_file",
        "description": "Move or rename a file or folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Current location."},
                "destination_path": {"type": "string", "description": "Target location."},
            },
            "required": ["source_path", "destination_path"]
        }
    },
    {
        "name": "copy_nextcloud_file",
        "description": "Copy a file or folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "File or folder to copy."},
                "destination_path": {"type": "string", "description": "Where to place the copy."},
            },
            "required": ["source_path", "destination_path"]
        }
    },
    {
        "name": "create_nextcloud_folder",
        "description": "Create a new folder and any missing parent folders.",
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string", "description": "Path of the folder to create."},
            },
            "required": ["folder_path"]
        }
    },
    {
        "name": "delete_nextcloud_file",
        "description": "Delete a file or folder. confirmed=False (default) is a dry run. Set confirmed=True only after user approves.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or folder path to delete."},
                "confirmed": {"type": "boolean", "description": "Must be true to actually delete.", "default": False},
            },
            "required": ["path"]
        }
    },
    {
        "name": "export_as_pdf",
        "description": "Export a Nextcloud file as a formatted PDF saved back to Nextcloud.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Source file path."},
                "output_filename": {"type": "string", "description": "Output filename (e.g. 'Report.pdf')."},
            },
            "required": ["source_path", "output_filename"]
        }
    },
    {
        "name": "export_as_docx",
        "description": "Export a Nextcloud file as a Word .docx saved back to Nextcloud.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Source file path."},
                "output_filename": {"type": "string", "description": "Output filename (e.g. 'Report.docx')."},
            },
            "required": ["source_path", "output_filename"]
        }
    },
]

now = int(time.time())
db.execute(
    "UPDATE tool SET content=?, specs=?, updated_at=? WHERE id=?",
    (content, json.dumps(new_specs), now, TOOL_ID)
)
db.commit()

row = db.execute("SELECT id, updated_at FROM tool WHERE id=?", (TOOL_ID,)).fetchone()
print(f"Updated tool '{row[0]}' at {row[1]}")
print(f"Specs: {len(new_specs)} functions")
print(f"Content: {len(content)} chars")
db.close()
