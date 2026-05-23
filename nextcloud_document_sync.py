"""
Nextcloud Document Sync — Open WebUI Tool
WebDAV-based file management for a Nextcloud shared folder.

Conditions under which this works:
- Nextcloud WebDAV endpoint is reachable from the Open WebUI container
- NEXTCLOUD_APP_PASS is a valid Nextcloud app password (not the login password)
- NEXTCLOUD_USER has read/write access to NEXTCLOUD_FOLDER
- Nextcloud version >= 20 (standard WebDAV PROPFIND/MKCOL/MOVE/COPY/DELETE)
- SSL certificate is valid (verify=True is hardcoded)
"""

import io
import os
import posixpath
import xml.etree.ElementTree as ET
from datetime import date
from typing import Optional

import pypdf
import requests
from docx import Document
from fpdf import FPDF
from pydantic import BaseModel, Field


class Tools:
    """
    WebDAV file management for a Nextcloud shared folder.

    Relative paths are resolved under NEXTCLOUD_FOLDER.
    Absolute paths (beginning with /) are relative to the bot user's file space root.
    Path traversal outside the bot user's space is blocked.
    """

    class Valves(BaseModel):
        NEXTCLOUD_URL: str = Field(
            default_factory=lambda: os.environ.get("NEXTCLOUD_URL", ""),
            description="Nextcloud base URL, e.g. https://nextcloud.yourdomain.com",
        )
        NEXTCLOUD_USER: str = Field(
            default_factory=lambda: os.environ.get("NEXTCLOUD_USER", ""),
            description="Nextcloud bot username",
        )
        NEXTCLOUD_APP_PASS: str = Field(
            default_factory=lambda: os.environ.get("NEXTCLOUD_APP_PASS", ""),
            description="Nextcloud app password for the bot user",
        )
        NEXTCLOUD_FOLDER: str = Field(
            default_factory=lambda: os.environ.get(
                "NEXTCLOUD_FOLDER", "/Shared/Live-Documents/"
            ),
            description="Default target folder (relative to bot user root)",
        )
        NEXTCLOUD_SHARE_WITH: str = Field(
            default_factory=lambda: os.environ.get("NEXTCLOUD_SHARE_WITH", ""),
            description=(
                "Comma-separated list of groups/users to share NEXTCLOUD_FOLDER with. "
                "Prefix groups with 'group:' and users with 'user:'. "
                "Example: group:staff,user:alice"
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _auth(self):
        return (self.valves.NEXTCLOUD_USER, self.valves.NEXTCLOUD_APP_PASS)

    def _user_root(self) -> str:
        """Full WebDAV root URL for the bot user (no trailing slash)."""
        return (
            f"{self.valves.NEXTCLOUD_URL.rstrip('/')}"
            f"/remote.php/dav/files/{self.valves.NEXTCLOUD_USER}"
        )

    def _resolve_url(self, path: str) -> str:
        """
        Convert path to a full WebDAV URL scoped to the bot user.

        - Absolute paths (start with /) → relative to user root.
        - Relative paths → relative to NEXTCLOUD_FOLDER.
        - Raises ValueError if the normalised path would escape user space.
        """
        if path.startswith("/"):
            rel = path.lstrip("/")
        else:
            folder = self.valves.NEXTCLOUD_FOLDER.strip("/")
            # Avoid doubling: if the relative path IS the folder name, treat as the folder itself
            if path and posixpath.normpath(path) == posixpath.normpath(folder):
                rel = folder
            else:
                rel = f"{folder}/{path}" if path else folder

        if not rel:
            return self._user_root()

        normalized = posixpath.normpath(rel)

        if path.startswith("/"):
            # Absolute: block traversal above user root
            if normalized.startswith(".."):
                raise ValueError(
                    f"Path {path!r} would escape the bot user's file space."
                )
        else:
            # Relative: must stay within NEXTCLOUD_FOLDER
            folder_normalized = posixpath.normpath(
                self.valves.NEXTCLOUD_FOLDER.strip("/")
            )
            if not (
                normalized == folder_normalized
                or normalized.startswith(folder_normalized + "/")
            ):
                raise ValueError(
                    f"Path {path!r} would escape the default folder "
                    f"({self.valves.NEXTCLOUD_FOLDER})."
                )

        return f"{self._user_root()}/{normalized}"

    def _ensure_folder(self, folder_url: str) -> Optional[str]:
        """
        Create folder_url and all missing parent folders via MKCOL.
        Returns None on success, error string on failure.
        201 = created (OK), 405 = already exists (OK), anything else = error.
        """
        user_root = self._user_root()
        after_root = folder_url[len(user_root):].strip("/")
        components = [p for p in after_root.split("/") if p]

        current = user_root
        for component in components:
            current = f"{current}/{component}"
            try:
                resp = requests.request(
                    "MKCOL", current, auth=self._auth(), verify=True, timeout=15
                )
            except requests.RequestException as exc:
                return f"Network error creating folder {current!r}: {exc}"
            if resp.status_code not in (201, 405):
                return (
                    f"Failed to create folder {current!r}: "
                    f"HTTP {resp.status_code} — {resp.text[:200]}"
                )
        return None

    def _propfind(self, url: str, depth: str = "1") -> requests.Response:
        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop>"
            "<d:displayname/>"
            "<d:resourcetype/>"
            "<d:getcontentlength/>"
            "<d:getlastmodified/>"
            "</d:prop>"
            "</d:propfind>"
        )
        return requests.request(
            "PROPFIND",
            url,
            data=body,
            headers={"Depth": depth, "Content-Type": "application/xml"},
            auth=self._auth(),
            verify=True,
            timeout=30,
        )

    def _parse_propfind(self, xml_text: str) -> list:
        """Parse PROPFIND XML into a list of entry dicts."""
        ns = {"d": "DAV:"}
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            return []

        entries = []
        for response in root.findall("d:response", ns):
            href_el = response.find("d:href", ns)
            href = (href_el.text or "").rstrip("/") if href_el is not None else ""

            propstat = response.find("d:propstat", ns)
            if propstat is None:
                continue
            prop = propstat.find("d:prop", ns)
            if prop is None:
                continue

            name_el = prop.find("d:displayname", ns)
            name = (
                name_el.text
                if name_el is not None and name_el.text
                else href.split("/")[-1]
            )

            rtype = prop.find("d:resourcetype", ns)
            is_collection = (
                rtype is not None and rtype.find("d:collection", ns) is not None
            )

            size_el = prop.find("d:getcontentlength", ns)
            try:
                size = int(size_el.text) if size_el is not None and size_el.text else 0
            except (ValueError, TypeError):
                size = 0

            lm_el = prop.find("d:getlastmodified", ns)
            last_modified = lm_el.text if lm_el is not None and lm_el.text else ""

            entries.append(
                {
                    "href": href,
                    "name": name,
                    "is_collection": is_collection,
                    "size": size,
                    "last_modified": last_modified,
                }
            )
        return entries

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _build_tree(self, entries: list, root_href: str, indent: int = 0) -> str:
        """Format PROPFIND entries as an indented directory tree, skipping the root entry."""
        root_clean = root_href.rstrip("/")
        children = [e for e in entries if e["href"] != root_clean]
        if not children:
            return "(empty)"
        lines = []
        pad = "  " * indent
        for e in sorted(children, key=lambda x: (not x["is_collection"], x["name"])):
            icon = "📁" if e["is_collection"] else "📄"
            size_str = "" if e["is_collection"] else f" ({self._format_size(e['size'])})"
            lines.append(f"{pad}{icon} {e['name']}{size_str}")
        return "\n".join(lines)

    def _ensure_folder_shared(self, folder_url: str) -> Optional[str]:
        """
        Share folder_url with every target in NEXTCLOUD_SHARE_WITH that doesn't
        already have a share. Skips targets that are already shared.
        Returns None on success (or if NEXTCLOUD_SHARE_WITH is empty),
        error string on first failure.

        share_type 0 = individual user, 1 = group.
        permissions 15 = READ+UPDATE+CREATE+DELETE (read-write, no re-share).
        """
        raw = self.valves.NEXTCLOUD_SHARE_WITH.strip()
        if not raw:
            return None

        # Parse targets: [("group", "bap"), ("user", "churnm"), ...]
        targets = []
        for entry in raw.split(","):
            entry = entry.strip()
            if entry.startswith("group:"):
                targets.append((1, entry[len("group:"):]))
            elif entry.startswith("user:"):
                targets.append((0, entry[len("user:"):]))
            # silently skip malformed entries

        if not targets:
            return None

        ocs_base = (
            f"{self.valves.NEXTCLOUD_URL.rstrip('/')}"
            "/ocs/v2.php/apps/files_sharing/api/v1/shares"
        )
        # Extract the path portion for the OCS API (path relative to user root)
        user_root = self._user_root()
        dav_path = folder_url[len(user_root):]  # e.g. /Shared/Live-Documents
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}

        # Fetch existing shares to avoid duplicates
        try:
            resp = requests.get(
                ocs_base,
                params={"path": dav_path, "reshares": "true"},
                auth=self._auth(),
                headers=headers,
                verify=True,
                timeout=10,
            )
        except requests.RequestException as exc:
            return f"Network error checking existing shares: {exc}"

        try:
            existing = resp.json().get("ocs", {}).get("data", []) or []
        except Exception:
            existing = []

        already_shared = {
            (s.get("share_type"), s.get("share_with"))
            for s in existing
        }

        for share_type, share_with in targets:
            if (share_type, share_with) in already_shared:
                continue
            try:
                post_resp = requests.post(
                    ocs_base,
                    data={
                        "path": dav_path,
                        "shareType": share_type,
                        "shareWith": share_with,
                        "permissions": 15,
                    },
                    auth=self._auth(),
                    headers=headers,
                    verify=True,
                    timeout=10,
                )
            except requests.RequestException as exc:
                return f"Network error sharing with {share_with!r}: {exc}"

            try:
                meta = post_resp.json().get("ocs", {}).get("meta", {})
                status_code = meta.get("statuscode", 0)
            except Exception:
                status_code = 0

            if status_code not in (100, 200):
                msg = meta.get("message", post_resp.text[:200])
                return f"Error sharing with {share_with!r}: {msg}"

        return None

    def _find_existing_topic_file(self, folder_url: str, topic: str) -> Optional[str]:
        """
        Return '{topic}.md' if it exists in folder_url (exact, case-sensitive).
        Returns None if not found or on any error.
        """
        try:
            resp = self._propfind(folder_url, depth="1")
        except requests.RequestException:
            return None
        if resp.status_code != 207:
            return None

        target = f"{topic}.md"
        for entry in self._parse_propfind(resp.text):
            if entry["name"] == target and not entry["is_collection"]:
                return target
        return None

    def _webdav_transfer(
        self,
        method: str,
        source_path: str,
        destination_path: str,
        operation_name: str,
    ) -> str:
        """
        Shared logic for MOVE and COPY.
        On 409 Conflict (missing parent), creates the destination folder and retries once.
        """
        try:
            src_url = self._resolve_url(source_path)
            dst_url = self._resolve_url(destination_path)
        except ValueError as exc:
            return f"Error: {exc}"

        headers = {
            "Destination": dst_url,
            "Overwrite": "T",
        }
        try:
            resp = requests.request(
                method,
                src_url,
                headers=headers,
                auth=self._auth(),
                verify=True,
                timeout=30,
            )
        except requests.RequestException as exc:
            return f"Network error during {operation_name}: {exc}"

        if resp.status_code in (201, 204):
            return f"✓ {operation_name} succeeded: {src_url} → {dst_url}"

        if resp.status_code == 409:
            # Parent folder of destination is missing — create it then retry
            dst_folder = dst_url.rsplit("/", 1)[0]
            err = self._ensure_folder(dst_folder)
            if err:
                return f"Error creating destination folder: {err}"
            try:
                resp = requests.request(
                    method,
                    src_url,
                    headers=headers,
                    auth=self._auth(),
                    verify=True,
                    timeout=30,
                )
            except requests.RequestException as exc:
                return f"Network error during {operation_name} retry: {exc}"
            if resp.status_code in (201, 204):
                return f"✓ {operation_name} succeeded: {src_url} → {dst_url}"

        return (
            f"Error during {operation_name}: "
            f"HTTP {resp.status_code} — {resp.text[:200]}"
        )

    # ── Public tool methods ───────────────────────────────────────────────────

    def sync_document_to_nextcloud(
        self,
        content: str,
        filename: str = "",
        topic: str = "",
    ) -> str:
        """
        Upload document content to Nextcloud via WebDAV PUT.

        Filename priority:
          1. `filename` if provided.
          2. If `topic` exactly matches an existing .md file in the folder (case-sensitive), overwrite it.
          3. Otherwise generate `{topic}-YYYY-MM-DD.md`, or `Document-YYYY-MM-DD.md` if no topic.

        Call this after every document update. Returns the full WebDAV URL on success,
        or a descriptive error string on failure.

        Args:
            content: Full text content of the document.
            filename: Explicit filename (e.g. 'report.md'). Takes highest priority.
            topic: Document topic used for filename matching and auto-generation.
        """
        try:
            folder_url = self._resolve_url("")
        except ValueError as exc:
            return f"Error: {exc}"

        if filename:
            target_filename = filename
        elif topic:
            existing = self._find_existing_topic_file(folder_url, topic)
            if existing:
                target_filename = existing
            else:
                today = date.today().strftime("%Y-%m-%d")
                target_filename = f"{topic}-{today}.md"
        else:
            today = date.today().strftime("%Y-%m-%d")
            target_filename = f"Document-{today}.md"

        err = self._ensure_folder(folder_url)
        if err:
            return f"Error creating target folder: {err}"

        err = self._ensure_folder_shared(folder_url)
        if err:
            return f"Error sharing target folder: {err}"

        file_url = f"{folder_url.rstrip('/')}/{target_filename}"
        try:
            resp = requests.put(
                file_url,
                data=content.encode("utf-8"),
                auth=self._auth(),
                headers={"Content-Type": "text/markdown; charset=utf-8"},
                verify=True,
                timeout=30,
            )
        except requests.RequestException as exc:
            return f"Network error uploading to Nextcloud: {exc}"

        if resp.status_code in (200, 201, 204):
            return f"✓ Synced to Nextcloud: {file_url}"

        return (
            f"Error uploading {target_filename!r}: "
            f"HTTP {resp.status_code} — {resp.text[:200]}"
        )

    def list_nextcloud_folder(self, folder_path: str = "") -> str:
        """
        List files and subfolders at the given path via WebDAV PROPFIND.

        Uses NEXTCLOUD_FOLDER if folder_path is empty.
        Relative paths are resolved under NEXTCLOUD_FOLDER.
        Absolute paths (/) are relative to the bot user's root.

        Returns a readable directory listing the model can report to the user,
        or a descriptive error string.

        Args:
            folder_path: Path to list. Empty string = default NEXTCLOUD_FOLDER.
        """
        try:
            url = self._resolve_url(folder_path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp = self._propfind(url, depth="1")
        except requests.RequestException as exc:
            return f"Network error listing folder: {exc}"

        if resp.status_code == 404:
            return f"Folder not found: {url}"
        if resp.status_code == 401:
            return f"Authentication failed (HTTP 401). Check NEXTCLOUD_USER and NEXTCLOUD_APP_PASS."
        if resp.status_code != 207:
            return f"Error listing folder: HTTP {resp.status_code} — {resp.text[:200]}"

        entries = self._parse_propfind(resp.text)
        # The root entry href (without user root prefix, just /dav/files/User/... path)
        root_href = url.split("/remote.php/dav/files/")[-1]
        root_href_full = f"/remote.php/dav/files/{root_href.lstrip('/')}"

        # Filter out the root entry itself
        children = [
            e for e in entries
            if e["href"].rstrip("/") != root_href_full.rstrip("/")
            and e["href"].rstrip("/") != ("/" + root_href.strip("/"))
        ]

        if not children:
            return f"📁 {url}\n(empty)"

        lines = [f"📁 {url}"]
        for e in sorted(children, key=lambda x: (not x["is_collection"], x["name"])):
            icon = "📁" if e["is_collection"] else "📄"
            size_str = "" if e["is_collection"] else f" ({self._format_size(e['size'])})"
            lines.append(f"  {icon} {e['name']}{size_str}")
        return "\n".join(lines)

    def move_nextcloud_file(self, source_path: str, destination_path: str) -> str:
        """
        Move or rename a file or folder via WebDAV MOVE.

        Creates the destination parent folder automatically if it doesn't exist.
        Relative paths are under NEXTCLOUD_FOLDER; absolute paths (/) from user root.

        Returns success message with source → destination, or a descriptive error.

        Args:
            source_path: Current location of the file or folder.
            destination_path: Target location (new path or new name).
        """
        return self._webdav_transfer("MOVE", source_path, destination_path, "Move")

    def copy_nextcloud_file(self, source_path: str, destination_path: str) -> str:
        """
        Copy a file or folder via WebDAV COPY.

        Creates the destination parent folder automatically if it doesn't exist.
        Relative paths are under NEXTCLOUD_FOLDER; absolute paths (/) from user root.

        Returns success message, or a descriptive error.

        Args:
            source_path: File or folder to copy.
            destination_path: Where to place the copy.
        """
        return self._webdav_transfer("COPY", source_path, destination_path, "Copy")

    def create_nextcloud_folder(self, folder_path: str) -> str:
        """
        Create a new folder (and any missing parent folders) via WebDAV MKCOL.

        Relative paths are created under NEXTCLOUD_FOLDER.
        Absolute paths (/) are relative to the bot user's root.

        Returns success message, or a descriptive error.

        Args:
            folder_path: Path of the folder to create.
        """
        try:
            url = self._resolve_url(folder_path)
        except ValueError as exc:
            return f"Error: {exc}"

        user_root = self._user_root()
        after_root = url[len(user_root):].strip("/")
        components = [p for p in after_root.split("/") if p]

        if not components:
            return "Error: cannot create the user root itself."

        current = user_root
        last_status = None
        for i, component in enumerate(components):
            current = f"{current}/{component}"
            is_final = i == len(components) - 1
            try:
                resp = requests.request(
                    "MKCOL", current, auth=self._auth(), verify=True, timeout=15
                )
            except requests.RequestException as exc:
                return f"Network error creating folder {current!r}: {exc}"

            last_status = resp.status_code
            if resp.status_code == 201:
                continue
            elif resp.status_code == 405:
                if is_final:
                    return f"Folder already exists: {current}"
                continue
            else:
                return (
                    f"Error creating folder {current!r}: "
                    f"HTTP {resp.status_code} — {resp.text[:200]}"
                )

        return f"✓ Folder created: {url}"

    def delete_nextcloud_file(self, path: str, confirmed: bool = False) -> str:
        """
        Delete a file or folder via WebDAV DELETE.

        SAFETY: If confirmed=False (the default), this function will NEVER delete anything.
        Instead it returns a full directory tree of what would be deleted and instructs
        the user to call again with confirmed=True.

        For folders, uses PROPFIND (Depth: infinity) to enumerate all contents before
        showing the confirmation warning. The full tree with file sizes is displayed.

        Args:
            path: File or folder path to delete (relative or absolute).
            confirmed: Must be explicitly True to perform the deletion. Default False.
        """
        try:
            url = self._resolve_url(path)
        except ValueError as exc:
            return f"Error: {exc}"

        # Always enumerate first so the warning (or final delete) has full context
        try:
            probe = self._propfind(url, depth="infinity")
        except requests.RequestException as exc:
            if confirmed:
                # On network error, don't delete blind
                return f"Network error checking path before delete: {exc}"
            return f"Network error checking path: {exc}"

        if probe.status_code == 404:
            return f"Path not found (HTTP 404): {url}"
        if probe.status_code == 401:
            return "Authentication failed (HTTP 401). Check credentials."
        if probe.status_code != 207:
            return f"Error inspecting path: HTTP {probe.status_code} — {probe.text[:200]}"

        entries = self._parse_propfind(probe.text)

        # Build tree for display (strip the leading /remote.php/... prefix for matching)
        root_href_suffix = url.split("/remote.php/dav/files/")[-1]
        root_href_full = f"/remote.php/dav/files/{root_href_suffix.lstrip('/')}"
        children = [
            e for e in entries
            if e["href"].rstrip("/") != root_href_full.rstrip("/")
        ]

        if not confirmed:
            lines = [f"⚠️  The following will be permanently deleted:\n"]
            # Show root item itself
            root_entry = next(
                (e for e in entries if e["href"].rstrip("/") == root_href_full.rstrip("/")),
                None,
            )
            if root_entry:
                icon = "📁" if root_entry["is_collection"] else "📄"
                size_str = "" if root_entry["is_collection"] else f" ({self._format_size(root_entry['size'])})"
                lines.append(f"  {icon} {root_entry['name']}{size_str}")
            for e in sorted(children, key=lambda x: x["href"]):
                depth = e["href"].count("/") - root_href_full.count("/")
                pad = "    " * max(depth, 1)
                icon = "📁" if e["is_collection"] else "📄"
                size_str = "" if e["is_collection"] else f" ({self._format_size(e['size'])})"
                lines.append(f"{pad}{icon} {e['name']}{size_str}")
            lines.append(
                f"\nAre you sure? Call again with confirmed=True to proceed."
            )
            return "\n".join(lines)

        # confirmed=True — perform the delete
        try:
            resp = requests.request(
                "DELETE", url, auth=self._auth(), verify=True, timeout=30
            )
        except requests.RequestException as exc:
            return f"Network error during delete: {exc}"

        if resp.status_code in (200, 204):
            file_count = sum(1 for e in children if not e["is_collection"])
            folder_count = sum(1 for e in children if e["is_collection"])
            summary = f"{file_count} file(s), {folder_count} subfolder(s)" if children else "1 item"
            return f"✓ Deleted: {url} ({summary})"

        if resp.status_code == 404:
            return f"Error: path not found during delete (HTTP 404): {url}"

        return f"Error deleting {url!r}: HTTP {resp.status_code} — {resp.text[:200]}"

    def _get_file_id(self, path: str) -> str:
        """
        Return the Nextcloud oc:fileid for a file — required by the versions API.
        Raises ValueError on HTTP error or missing property.
        """
        url = self._resolve_url(path)
        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:prop><oc:fileid/></d:prop>"
            "</d:propfind>"
        )
        resp = requests.request(
            "PROPFIND", url,
            data=body,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            auth=self._auth(),
            verify=True,
            timeout=15,
        )
        if resp.status_code != 207:
            raise ValueError(f"HTTP {resp.status_code} getting file ID for {path!r}")
        ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            raise ValueError(f"XML parse error: {exc}")
        for response in root.findall("d:response", ns):
            for propstat in response.findall("d:propstat", ns):
                prop = propstat.find("d:prop", ns)
                if prop is not None:
                    el = prop.find("oc:fileid", ns)
                    if el is not None and el.text:
                        return el.text.strip()
        raise ValueError(f"oc:fileid not found in PROPFIND response for {path!r}")

    def _text_to_pdf_bytes(self, text: str) -> bytes:
        """Render plain text / markdown as a PDF using fpdf2."""
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)  # must be set before any text op
        w = pdf.epw  # effective page width — avoids multi_cell width=0 bug in fpdf2 2.8.x
        for line in text.split("\n"):
            s = line.rstrip().encode("latin-1", errors="replace").decode("latin-1")
            if s.startswith("### "):
                pdf.set_font("Helvetica", "B", 12)
                pdf.multi_cell(w, 8, s[4:])
                pdf.set_font("Helvetica", size=10)
            elif s.startswith("## "):
                pdf.set_font("Helvetica", "B", 14)
                pdf.multi_cell(w, 9, s[3:])
                pdf.set_font("Helvetica", size=10)
            elif s.startswith("# "):
                pdf.set_font("Helvetica", "B", 16)
                pdf.multi_cell(w, 10, s[2:])
                pdf.set_font("Helvetica", size=10)
            elif s == "":
                pdf.ln(4)
            else:
                pdf.multi_cell(w, 6, s)
        return bytes(pdf.output())

    def _text_to_docx_bytes(self, text: str) -> bytes:
        """Render plain text / markdown as a Word .docx using python-docx."""
        doc = Document()
        for line in text.split("\n"):
            s = line.rstrip()
            if s.startswith("# "):
                doc.add_heading(s[2:], level=1)
            elif s.startswith("## "):
                doc.add_heading(s[3:], level=2)
            elif s.startswith("### "):
                doc.add_heading(s[4:], level=3)
            elif s.startswith("- ") or s.startswith("* "):
                doc.add_paragraph(s[2:], style="List Bullet")
            elif s == "":
                doc.add_paragraph("")
            else:
                doc.add_paragraph(s)
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def read_nextcloud_file(self, path: str) -> str:
        """
        Read and return the text content of a file from Nextcloud.

        Supported formats:
        - Plain text (.txt, .md, .csv, .json, etc.): returned as-is.
        - PDF (.pdf): text extracted from all pages via pypdf.
        - Word (.docx): text extracted from all paragraphs via python-docx.
        - Other binary: returns an error message.

        Under what conditions does this work:
        - File must exist and be readable by NEXTCLOUD_USER.
        - For PDF/DOCX, only the text layer is extracted; images/charts are skipped.
        - Max practical size: depends on model context window.

        Args:
            path: File path, relative to NEXTCLOUD_FOLDER or absolute from user root.
        """
        try:
            url = self._resolve_url(path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp = requests.get(url, auth=self._auth(), verify=True, timeout=30)
        except requests.RequestException as exc:
            return f"Network error reading {path!r}: {exc}"

        if resp.status_code == 404:
            return f"File not found: {url}"
        if resp.status_code == 401:
            return "Authentication failed (HTTP 401). Check credentials."
        if resp.status_code != 200:
            return f"Error reading {path!r}: HTTP {resp.status_code}"

        ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
        ct = resp.headers.get("Content-Type", "").lower()

        if ext == "pdf" or "pdf" in ct:
            try:
                reader = pypdf.PdfReader(io.BytesIO(resp.content))
                pages_text = [page.extract_text() or "" for page in reader.pages]
                text = "\n\n".join(t for t in pages_text if t)
                return f"[PDF — {len(reader.pages)} page(s)]\n\n{text}"
            except Exception as exc:
                return f"Error reading PDF {path!r}: {exc}"

        if ext == "docx" or "wordprocessingml" in ct:
            try:
                doc = Document(io.BytesIO(resp.content))
                text = "\n".join(p.text for p in doc.paragraphs)
                return f"[Word document]\n\n{text}"
            except Exception as exc:
                return f"Error reading Word document {path!r}: {exc}"

        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            return (
                f"Error: {path!r} appears to be binary and cannot be read as text. "
                "Supported formats: .txt, .md, .csv, .json, .pdf, .docx"
            )

    def get_file_info(self, path: str) -> str:
        """
        Return metadata for a file or folder: name, size, last modified,
        content type, and Nextcloud internal file ID.

        Under what conditions does this work:
        - Requires the oc:fileid property (available on Nextcloud >= 8, always present).
        - File must exist and be readable by NEXTCLOUD_USER.

        Args:
            path: File or folder path (relative or absolute).
        """
        try:
            url = self._resolve_url(path)
        except ValueError as exc:
            return f"Error: {exc}"

        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:prop>"
            "<d:displayname/><d:resourcetype/><d:getcontentlength/>"
            "<d:getlastmodified/><d:getcontenttype/><oc:fileid/>"
            "</d:prop>"
            "</d:propfind>"
        )
        try:
            resp = requests.request(
                "PROPFIND", url,
                data=body,
                headers={"Depth": "0", "Content-Type": "application/xml"},
                auth=self._auth(),
                verify=True,
                timeout=15,
            )
        except requests.RequestException as exc:
            return f"Network error: {exc}"

        if resp.status_code == 404:
            return f"Not found: {url}"
        if resp.status_code == 401:
            return "Authentication failed (HTTP 401)."
        if resp.status_code != 207:
            return f"Error: HTTP {resp.status_code} — {resp.text[:200]}"

        ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            return f"Error parsing response: {exc}"

        info: dict = {}
        for response in root.findall("d:response", ns):
            for propstat in response.findall("d:propstat", ns):
                prop = propstat.find("d:prop", ns)
                if prop is None:
                    continue
                for tag, key in [
                    ("d:displayname", "name"),
                    ("d:getcontentlength", "size"),
                    ("d:getlastmodified", "modified"),
                    ("d:getcontenttype", "type"),
                    ("oc:fileid", "file_id"),
                ]:
                    el = prop.find(tag, ns)
                    if el is not None and el.text:
                        info[key] = el.text.strip()
                rtype = prop.find("d:resourcetype", ns)
                if rtype is not None:
                    info["is_folder"] = rtype.find("d:collection", ns) is not None

        if not info:
            return f"No metadata returned for {path!r}"

        lines = [f"**{info.get('name', path)}**"]
        lines.append(f"- Type: {'Folder' if info.get('is_folder') else info.get('type', 'file')}")
        if "size" in info:
            lines.append(f"- Size: {self._format_size(int(info['size']))}")
        if "modified" in info:
            lines.append(f"- Last modified: {info['modified']}")
        if "file_id" in info:
            lines.append(f"- Nextcloud file ID: {info['file_id']}")
        lines.append(f"- URL: {url}")
        return "\n".join(lines)

    def list_folder_recursive(self, folder_path: str = "") -> str:
        """
        List all files and subfolders recursively using PROPFIND Depth:infinity.

        Under what conditions does this work:
        - Nextcloud must allow Depth:infinity PROPFIND (it does by default).
        - Very large trees may be slow or time out (30s limit).

        Args:
            folder_path: Folder to list. Empty string = NEXTCLOUD_FOLDER.
        """
        try:
            url = self._resolve_url(folder_path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp = self._propfind(url, depth="infinity")
        except requests.RequestException as exc:
            return f"Network error: {exc}"

        if resp.status_code == 404:
            return f"Folder not found: {url}"
        if resp.status_code == 401:
            return "Authentication failed (HTTP 401)."
        if resp.status_code != 207:
            return f"Error: HTTP {resp.status_code} — {resp.text[:200]}"

        entries = self._parse_propfind(resp.text)
        root_href_suffix = url.split("/remote.php/dav/files/")[-1]
        root_href_full = f"/remote.php/dav/files/{root_href_suffix.lstrip('/')}"
        tree = self._build_tree(entries, root_href_full)
        return f"📁 {url} (recursive)\n{tree}"

    def list_file_versions(self, path: str) -> str:
        """
        List all stored versions of a file in Nextcloud version history.

        Under what conditions does this work:
        - Requires the Nextcloud 'files_versions' app (enabled by default).
        - NEXTCLOUD_USER must own or have access to the file.
        - Only files have versions; folders do not.

        Args:
            path: File path (relative to NEXTCLOUD_FOLDER or absolute).
        """
        try:
            file_id = self._get_file_id(path)
        except ValueError as exc:
            return f"Error: {exc}"
        except requests.RequestException as exc:
            return f"Network error getting file ID: {exc}"

        versions_url = (
            f"{self.valves.NEXTCLOUD_URL.rstrip('/')}"
            f"/remote.php/dav/versions/{self.valves.NEXTCLOUD_USER}/versions/{file_id}"
        )
        try:
            resp = requests.request(
                "PROPFIND", versions_url,
                headers={"Depth": "1"},
                auth=self._auth(),
                verify=True,
                timeout=15,
            )
        except requests.RequestException as exc:
            return f"Network error listing versions: {exc}"

        if resp.status_code == 404:
            return f"No versions found (or file not found): {path!r}"
        if resp.status_code != 207:
            return f"Error listing versions: HTTP {resp.status_code}"

        entries = self._parse_propfind(resp.text)
        versions = [e for e in entries if not e["is_collection"]]

        if not versions:
            return f"No versions stored for {path!r} (only the current version exists)"

        lines = [f"Versions of {path!r} ({len(versions)} stored):"]
        for v in sorted(versions, key=lambda x: x["href"], reverse=True):
            version_id = v["href"].rstrip("/").split("/")[-1]
            size_str = self._format_size(v["size"]) if v["size"] else "unknown size"
            modified = v["last_modified"] or "unknown date"
            lines.append(f"  • version_id={version_id!r}  {modified}  ({size_str})")
        lines.append(
            f"\nTo restore: call restore_file_version(path={path!r}, version_id='<version_id>')"
        )
        return "\n".join(lines)

    def restore_file_version(self, path: str, version_id: str) -> str:
        """
        Restore a file to a specific previous version.

        The restored content becomes the new current version; existing versions
        are preserved (Nextcloud saves the current state as a new version first).

        Under what conditions does this work:
        - version_id must be from list_file_versions output.
        - NEXTCLOUD_USER must own the file.
        - Requires files_versions app.

        Args:
            path: File path to restore (relative or absolute).
            version_id: Version ID string from list_file_versions.
        """
        try:
            file_id = self._get_file_id(path)
        except ValueError as exc:
            return f"Error: {exc}"
        except requests.RequestException as exc:
            return f"Network error getting file ID: {exc}"

        base = self.valves.NEXTCLOUD_URL.rstrip("/")
        version_url = (
            f"{base}/remote.php/dav/versions/{self.valves.NEXTCLOUD_USER}"
            f"/versions/{file_id}/{version_id}"
        )

        try:
            get_resp = requests.get(
                version_url, auth=self._auth(), verify=True, timeout=30
            )
        except requests.RequestException as exc:
            return f"Network error fetching version content: {exc}"

        if get_resp.status_code == 404:
            return f"Version not found: version_id={version_id!r} for {path!r} (HTTP 404)"
        if get_resp.status_code != 200:
            return f"Error fetching version: HTTP {get_resp.status_code}"

        try:
            live_url = self._resolve_url(path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            put_resp = requests.put(
                live_url,
                data=get_resp.content,
                headers={"Content-Type": get_resp.headers.get("Content-Type", "application/octet-stream")},
                auth=self._auth(),
                verify=True,
                timeout=30,
            )
        except requests.RequestException as exc:
            return f"Network error writing restored version: {exc}"

        if put_resp.status_code in (200, 201, 204):
            return f"✓ Restored version {version_id!r} of {path!r}"
        return f"Error writing restored version: HTTP {put_resp.status_code} — {put_resp.text[:200]}"

    def export_as_pdf(self, source_path: str, output_filename: str) -> str:
        """
        Read a source file from Nextcloud and export it as a PDF, saved back to Nextcloud.

        Markdown headings (#, ##, ###) and paragraphs are rendered with appropriate
        font sizes. Note: built-in fonts support Latin-1 only; non-Latin characters
        are replaced with '?'.

        Under what conditions does this work:
        - Source must be a readable text file (.txt, .md) or extractable PDF/DOCX.
        - Output is saved in NEXTCLOUD_FOLDER unless output_filename is absolute.

        Args:
            source_path: Path to the source file (e.g. 'report.md').
            output_filename: Output filename (e.g. 'ClientReport.pdf'). .pdf added if missing.
        """
        try:
            src_url = self._resolve_url(source_path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp = requests.get(src_url, auth=self._auth(), verify=True, timeout=30)
        except requests.RequestException as exc:
            return f"Network error reading {source_path!r}: {exc}"

        if resp.status_code == 404:
            return f"Source file not found: {src_url}"
        if resp.status_code != 200:
            return f"Error reading source: HTTP {resp.status_code}"

        ext = source_path.lower().rsplit(".", 1)[-1] if "." in source_path else ""
        ct = resp.headers.get("Content-Type", "").lower()

        if ext == "pdf" or "pdf" in ct:
            try:
                reader = pypdf.PdfReader(io.BytesIO(resp.content))
                text = "\n\n".join(p.extract_text() or "" for p in reader.pages)
            except Exception as exc:
                return f"Error reading source PDF: {exc}"
        elif ext == "docx" or "wordprocessingml" in ct:
            try:
                doc = Document(io.BytesIO(resp.content))
                text = "\n".join(p.text for p in doc.paragraphs)
            except Exception as exc:
                return f"Error reading source Word doc: {exc}"
        else:
            try:
                text = resp.content.decode("utf-8")
            except UnicodeDecodeError:
                return f"Error: source file {source_path!r} appears to be binary."

        try:
            pdf_bytes = self._text_to_pdf_bytes(text)
        except Exception as exc:
            return f"Error generating PDF: {exc}"

        if not output_filename.lower().endswith(".pdf"):
            output_filename = output_filename + ".pdf"

        try:
            dest_url = self._resolve_url(output_filename)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp2 = requests.put(
                dest_url,
                data=pdf_bytes,
                auth=self._auth(),
                headers={"Content-Type": "application/pdf"},
                verify=True,
                timeout=60,
            )
        except requests.RequestException as exc:
            return f"Network error uploading PDF: {exc}"

        if resp2.status_code in (200, 201, 204):
            return f"✓ Exported as PDF: {dest_url} ({self._format_size(len(pdf_bytes))})"
        return f"Error uploading PDF: HTTP {resp2.status_code} — {resp2.text[:200]}"

    def export_as_docx(self, source_path: str, output_filename: str) -> str:
        """
        Read a source file from Nextcloud and export it as a Word .docx document,
        saved back to Nextcloud.

        Markdown headings and bullet points are converted to Word styles.

        Under what conditions does this work:
        - Source must be a readable text file or extractable PDF/DOCX.
        - Output is saved in NEXTCLOUD_FOLDER unless output_filename is absolute.

        Args:
            source_path: Path to the source file (e.g. 'report.md').
            output_filename: Output filename (e.g. 'ClientReport.docx'). .docx added if missing.
        """
        try:
            src_url = self._resolve_url(source_path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp = requests.get(src_url, auth=self._auth(), verify=True, timeout=30)
        except requests.RequestException as exc:
            return f"Network error reading {source_path!r}: {exc}"

        if resp.status_code == 404:
            return f"Source file not found: {src_url}"
        if resp.status_code != 200:
            return f"Error reading source: HTTP {resp.status_code}"

        ext = source_path.lower().rsplit(".", 1)[-1] if "." in source_path else ""
        ct = resp.headers.get("Content-Type", "").lower()

        if ext == "pdf" or "pdf" in ct:
            try:
                reader = pypdf.PdfReader(io.BytesIO(resp.content))
                text = "\n\n".join(p.extract_text() or "" for p in reader.pages)
            except Exception as exc:
                return f"Error reading source PDF: {exc}"
        elif ext == "docx" or "wordprocessingml" in ct:
            try:
                doc = Document(io.BytesIO(resp.content))
                text = "\n".join(p.text for p in doc.paragraphs)
            except Exception as exc:
                return f"Error reading source Word doc: {exc}"
        else:
            try:
                text = resp.content.decode("utf-8")
            except UnicodeDecodeError:
                return f"Error: source file {source_path!r} appears to be binary."

        try:
            docx_bytes = self._text_to_docx_bytes(text)
        except Exception as exc:
            return f"Error generating Word document: {exc}"

        if not output_filename.lower().endswith(".docx"):
            output_filename = output_filename + ".docx"

        try:
            dest_url = self._resolve_url(output_filename)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp2 = requests.put(
                dest_url,
                data=docx_bytes,
                auth=self._auth(),
                headers={"Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
                verify=True,
                timeout=60,
            )
        except requests.RequestException as exc:
            return f"Network error uploading Word document: {exc}"

        if resp2.status_code in (200, 201, 204):
            return f"✓ Exported as Word document: {dest_url} ({self._format_size(len(docx_bytes))})"
        return f"Error uploading Word document: HTTP {resp2.status_code} — {resp2.text[:200]}"
