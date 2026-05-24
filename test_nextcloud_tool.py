"""
TDD tests for Nextcloud Document Sync tool.
Run inside openwebui container:
  docker exec openwebui python3 -m pytest /tmp/test_nextcloud_tool.py -v
"""
import unittest
import asyncio
from unittest.mock import MagicMock, patch, call
from datetime import date
import xml.etree.ElementTree as ET


# ── Helpers to build fake PROPFIND XML responses ──────────────────────────────

def _make_propfind_xml(entries):
    """
    entries: list of dicts with keys: href, name, is_collection, size
    """
    items = ""
    for e in entries:
        rtype = "<d:collection/>" if e.get("is_collection") else ""
        size = e.get("size", 0)
        items += f"""
  <d:response>
    <d:href>{e['href']}</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>{e['name']}</d:displayname>
        <d:resourcetype>{rtype}</d:resourcetype>
        <d:getcontentlength>{size}</d:getcontentlength>
        <d:getlastmodified>Mon, 01 Jan 2026 00:00:00 GMT</d:getlastmodified>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>"""
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">{items}
</d:multistatus>"""


def _make_tools():
    """Return a Tools instance with test credentials pre-set.

    All public async methods are wrapped to run synchronously so existing
    tests don't need to be rewritten after the async migration.
    """
    from nextcloud_document_sync import Tools
    t = Tools()
    t.valves.NEXTCLOUD_URL = "https://nextcloud.example.com"
    t.valves.NEXTCLOUD_USER = "TestBot"
    t.valves.NEXTCLOUD_APP_PASS = "test-app-pass"
    t.valves.NEXTCLOUD_FOLDER = "/Shared/Docs/"
    t.valves.NEXTCLOUD_SHARE_WITH = ""  # prevent container env var from leaking in

    # Wrap async public methods to be callable synchronously from tests
    for attr_name in [a for a in dir(t) if not a.startswith("_")]:
        cls_attr = getattr(type(t), attr_name, None)
        if cls_attr is not None and asyncio.iscoroutinefunction(cls_attr):
            bound = getattr(t, attr_name)
            setattr(t, attr_name, lambda *a, _m=bound, **kw: asyncio.run(_m(*a, **kw)))

    return t


USER_ROOT = "https://nextcloud.example.com/remote.php/dav/files/TestBot"
FOLDER_URL = f"{USER_ROOT}/Shared/Docs"


# ── Path resolution ────────────────────────────────────────────────────────────

class TestPathResolution(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    def test_relative_path_resolves_under_folder(self):
        url = self.t._resolve_url("file.md")
        self.assertEqual(url, f"{USER_ROOT}/Shared/Docs/file.md")

    def test_empty_path_resolves_to_folder(self):
        url = self.t._resolve_url("")
        self.assertEqual(url, f"{USER_ROOT}/Shared/Docs")

    def test_absolute_path_resolves_to_user_root(self):
        url = self.t._resolve_url("/Other/folder/file.md")
        self.assertEqual(url, f"{USER_ROOT}/Other/folder/file.md")

    def test_absolute_root_slash_resolves_to_user_root(self):
        url = self.t._resolve_url("/")
        self.assertEqual(url, USER_ROOT)

    def test_path_traversal_relative_blocked(self):
        with self.assertRaises(ValueError):
            self.t._resolve_url("../../etc/passwd")

    def test_path_traversal_absolute_blocked(self):
        # /../../other_user would normalize to ../other_user → caught
        with self.assertRaises(ValueError):
            self.t._resolve_url("/../../other_user/file")

    def test_trailing_slash_on_folder_stripped_cleanly(self):
        url = self.t._resolve_url("sub/")
        self.assertEqual(url, f"{USER_ROOT}/Shared/Docs/sub")

    def test_relative_path_equal_to_folder_does_not_double(self):
        """Model passing NEXTCLOUD_FOLDER as a relative path must not nest it."""
        url = self.t._resolve_url("Shared/Docs")
        self.assertEqual(url, f"{USER_ROOT}/Shared/Docs")

    def test_relative_path_equal_to_folder_with_trailing_slash(self):
        url = self.t._resolve_url("Shared/Docs/")
        self.assertEqual(url, f"{USER_ROOT}/Shared/Docs")


# ── sync_document_to_nextcloud ─────────────────────────────────────────────────

class TestSyncDocument(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_explicit_filename_takes_priority_over_topic(self, mock_put, mock_req):
        # MKCOL on folder returns 405 (exists)
        mock_req.return_value = MagicMock(status_code=405)
        mock_put.return_value = MagicMock(status_code=201)

        result = self.t.sync_document_to_nextcloud(
            content="hello", filename="my-doc.md", topic="Something Else"
        )
        put_url = mock_put.call_args[0][0]
        self.assertIn("my-doc.md", put_url)
        self.assertNotIn("Something Else", put_url)
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_topic_match_overwrites_existing_file(self, mock_put, mock_req):
        folder_xml = _make_propfind_xml([
            {"href": f"/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
            {"href": f"/remote.php/dav/files/TestBot/Shared/Docs/Marcus filing.md", "name": "Marcus filing.md", "is_collection": False, "size": 500},
        ])
        # PROPFIND returns existing file; MKCOL returns 405
        mock_req.side_effect = [
            MagicMock(status_code=207, text=folder_xml),  # PROPFIND in _find_existing_topic_file
            MagicMock(status_code=405),                   # MKCOL Shared
            MagicMock(status_code=405),                   # MKCOL Shared/Docs
        ]
        mock_put.return_value = MagicMock(status_code=204)

        result = self.t.sync_document_to_nextcloud(
            content="updated", topic="Marcus filing"
        )
        put_url = mock_put.call_args[0][0]
        self.assertIn("Marcus filing.md", put_url)
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.date")
    def test_topic_no_match_generates_dated_filename(self, mock_date, mock_put, mock_req):
        mock_date.today.return_value = date(2026, 5, 19)
        folder_xml = _make_propfind_xml([
            {"href": f"/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
        ])
        mock_req.side_effect = [
            MagicMock(status_code=207, text=folder_xml),  # PROPFIND
            MagicMock(status_code=405),                   # MKCOL
            MagicMock(status_code=405),
        ]
        mock_put.return_value = MagicMock(status_code=201)

        result = self.t.sync_document_to_nextcloud(content="text", topic="Case Review")
        put_url = mock_put.call_args[0][0]
        self.assertIn("Case Review-2026-05-19.md", put_url)
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.date")
    def test_no_filename_no_topic_generates_document_fallback(self, mock_date, mock_put, mock_req):
        mock_date.today.return_value = date(2026, 5, 19)
        mock_req.return_value = MagicMock(status_code=405)
        mock_put.return_value = MagicMock(status_code=201)

        result = self.t.sync_document_to_nextcloud(content="text")
        put_url = mock_put.call_args[0][0]
        self.assertIn("Document-2026-05-19.md", put_url)
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_topic_case_mismatch_does_not_overwrite(self, mock_put, mock_req):
        """marcus filing.md must NOT match topic='Marcus filing' (case-sensitive)."""
        folder_xml = _make_propfind_xml([
            {"href": f"/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
            {"href": f"/remote.php/dav/files/TestBot/Shared/Docs/marcus filing.md", "name": "marcus filing.md", "is_collection": False},
        ])
        mock_req.side_effect = [
            MagicMock(status_code=207, text=folder_xml),
            MagicMock(status_code=405),
            MagicMock(status_code=405),
        ]
        mock_put.return_value = MagicMock(status_code=201)

        self.t.sync_document_to_nextcloud(content="text", topic="Marcus filing")
        put_url = mock_put.call_args[0][0]
        # Should NOT overwrite the lowercase file
        self.assertNotIn("marcus filing.md", put_url)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_folder_created_if_missing(self, mock_put, mock_req):
        """MKCOL called with 201 (created) should proceed without error.
        When filename is explicit, no PROPFIND is issued — only 2 MKCOL calls."""
        mock_req.side_effect = [
            MagicMock(status_code=201),  # MKCOL Shared
            MagicMock(status_code=201),  # MKCOL Shared/Docs
        ]
        mock_put.return_value = MagicMock(status_code=201)
        result = self.t.sync_document_to_nextcloud(content="text", filename="f.md")
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_mkcol_failure_returns_error(self, mock_put, mock_req):
        mock_req.return_value = MagicMock(status_code=503, text="Service unavailable")
        result = self.t.sync_document_to_nextcloud(content="text", filename="f.md")
        self.assertIn("Error", result)
        mock_put.assert_not_called()

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_auth_failure_returns_error(self, mock_put, mock_req):
        mock_req.return_value = MagicMock(status_code=405)
        mock_put.return_value = MagicMock(status_code=401, text="Unauthorized")
        result = self.t.sync_document_to_nextcloud(content="text", filename="f.md")
        self.assertIn("401", result)
        self.assertNotIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_network_error_returns_descriptive_message(self, mock_put, mock_req):
        import requests as req_lib
        mock_req.return_value = MagicMock(status_code=405)
        mock_put.side_effect = req_lib.ConnectionError("connection refused")
        result = self.t.sync_document_to_nextcloud(content="text", filename="f.md")
        self.assertIn("Network error", result)
        self.assertIn("connection refused", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    def test_empty_content_is_allowed(self, mock_put, mock_req):
        mock_req.return_value = MagicMock(status_code=405)
        mock_put.return_value = MagicMock(status_code=201)
        result = self.t.sync_document_to_nextcloud(content="", filename="empty.md")
        self.assertIn("✓", result)


# ── list_nextcloud_folder ──────────────────────────────────────────────────────

class TestListFolder(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_lists_default_folder(self, mock_req):
        xml = _make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/note.md", "name": "note.md", "is_collection": False, "size": 100},
        ])
        mock_req.return_value = MagicMock(status_code=207, text=xml)
        result = self.t.list_nextcloud_folder()
        self.assertIn("note.md", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_lists_specified_relative_path(self, mock_req):
        xml = _make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/sub", "name": "sub", "is_collection": True},
        ])
        mock_req.return_value = MagicMock(status_code=207, text=xml)
        self.t.list_nextcloud_folder("sub")
        call_url = mock_req.call_args[0][1]
        self.assertIn("/Shared/Docs/sub", call_url)

    @patch("nextcloud_document_sync.requests.request")
    def test_lists_absolute_path(self, mock_req):
        xml = _make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Other", "name": "Other", "is_collection": True},
        ])
        mock_req.return_value = MagicMock(status_code=207, text=xml)
        self.t.list_nextcloud_folder("/Other")
        call_url = mock_req.call_args[0][1]
        self.assertIn("/files/TestBot/Other", call_url)
        self.assertNotIn("/Shared/Docs", call_url)

    @patch("nextcloud_document_sync.requests.request")
    def test_404_returns_not_found_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.list_nextcloud_folder("nonexistent")
        self.assertIn("not found", result.lower())

    @patch("nextcloud_document_sync.requests.request")
    def test_401_returns_auth_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=401, text="Unauthorized")
        result = self.t.list_nextcloud_folder()
        self.assertIn("401", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_empty_folder_reported_clearly(self, mock_req):
        xml = _make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
        ])
        mock_req.return_value = MagicMock(status_code=207, text=xml)
        result = self.t.list_nextcloud_folder()
        self.assertIn("empty", result.lower())

    @patch("nextcloud_document_sync.requests.request")
    def test_network_error_returns_message(self, mock_req):
        import requests as req_lib
        mock_req.side_effect = req_lib.ConnectionError("timeout")
        result = self.t.list_nextcloud_folder()
        self.assertIn("Network error", result)


# ── move_nextcloud_file ────────────────────────────────────────────────────────

class TestMoveFile(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_successful_move(self, mock_req):
        mock_req.return_value = MagicMock(status_code=201)
        result = self.t.move_nextcloud_file("a.md", "archive/a.md")
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_move_uses_destination_header(self, mock_req):
        mock_req.return_value = MagicMock(status_code=201)
        self.t.move_nextcloud_file("a.md", "sub/a.md")
        headers = mock_req.call_args[1]["headers"]
        self.assertIn("Destination", headers)
        self.assertIn("/Shared/Docs/sub/a.md", headers["Destination"])

    @patch("nextcloud_document_sync.requests.request")
    def test_creates_destination_folder_if_missing(self, mock_req):
        # First MOVE returns 409 Conflict (parent missing),
        # then MKCOL creates folder, then MOVE succeeds
        mock_req.side_effect = [
            MagicMock(status_code=409, text="Conflict"),  # first MOVE attempt
            MagicMock(status_code=405),                   # MKCOL Shared
            MagicMock(status_code=201),                   # MKCOL Shared/Docs
            MagicMock(status_code=201),                   # MKCOL Shared/Docs/sub
            MagicMock(status_code=201),                   # retry MOVE
        ]
        result = self.t.move_nextcloud_file("a.md", "sub/a.md")
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_source_not_found_returns_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.move_nextcloud_file("ghost.md", "dest.md")
        self.assertIn("404", result)
        self.assertNotIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_network_error_on_move(self, mock_req):
        import requests as req_lib
        mock_req.side_effect = req_lib.ConnectionError("lost")
        result = self.t.move_nextcloud_file("a.md", "b.md")
        self.assertIn("Network error", result)


# ── copy_nextcloud_file ────────────────────────────────────────────────────────

class TestCopyFile(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_successful_copy(self, mock_req):
        mock_req.return_value = MagicMock(status_code=201)
        result = self.t.copy_nextcloud_file("a.md", "backup/a.md")
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_copy_uses_destination_header(self, mock_req):
        mock_req.return_value = MagicMock(status_code=201)
        self.t.copy_nextcloud_file("a.md", "sub/copy.md")
        headers = mock_req.call_args[1]["headers"]
        self.assertIn("Destination", headers)

    @patch("nextcloud_document_sync.requests.request")
    def test_creates_destination_folder_if_missing(self, mock_req):
        mock_req.side_effect = [
            MagicMock(status_code=409, text="Conflict"),
            MagicMock(status_code=405),
            MagicMock(status_code=201),
            MagicMock(status_code=201),
            MagicMock(status_code=201),
        ]
        result = self.t.copy_nextcloud_file("a.md", "newdir/a.md")
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_source_not_found(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.copy_nextcloud_file("ghost.md", "dest.md")
        self.assertIn("404", result)


# ── create_nextcloud_folder ────────────────────────────────────────────────────

class TestCreateFolder(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_creates_single_folder(self, mock_req):
        mock_req.side_effect = [
            MagicMock(status_code=405),  # MKCOL Shared
            MagicMock(status_code=405),  # MKCOL Shared/Docs
            MagicMock(status_code=201),  # MKCOL Shared/Docs/new
        ]
        result = self.t.create_nextcloud_folder("new")
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_creates_nested_folders_parents_missing(self, mock_req):
        mock_req.side_effect = [
            MagicMock(status_code=405),  # Shared
            MagicMock(status_code=201),  # Shared/Docs
            MagicMock(status_code=201),  # Shared/Docs/a
            MagicMock(status_code=201),  # Shared/Docs/a/b
        ]
        result = self.t.create_nextcloud_folder("a/b")
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_folder_already_exists_returns_already_exists(self, mock_req):
        mock_req.return_value = MagicMock(status_code=405)
        result = self.t.create_nextcloud_folder("existing")
        # 405 at final segment = already exists, should be reported clearly
        self.assertIn("already exists", result.lower())

    @patch("nextcloud_document_sync.requests.request")
    def test_mkcol_server_error_returns_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=500, text="Internal Server Error")
        result = self.t.create_nextcloud_folder("broken")
        self.assertIn("Error", result)


# ── delete_nextcloud_file ──────────────────────────────────────────────────────

class TestDeleteFile(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_unconfirmed_never_sends_delete(self, mock_req):
        """The most critical test: confirmed=False must NEVER DELETE."""
        # Even if mock would return success, DELETE must not be called
        mock_req.return_value = MagicMock(status_code=207, text=_make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/file.md", "name": "file.md", "is_collection": False, "size": 100},
        ]))
        result = self.t.delete_nextcloud_file("file.md", confirmed=False)

        # No DELETE should have been issued
        for c in mock_req.call_args_list:
            self.assertNotEqual(c[0][0], "DELETE")

        self.assertIn("confirmed=True", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_unconfirmed_lists_file_in_warning(self, mock_req):
        mock_req.return_value = MagicMock(status_code=207, text=_make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/report.md", "name": "report.md", "is_collection": False, "size": 2048},
        ]))
        result = self.t.delete_nextcloud_file("report.md", confirmed=False)
        self.assertIn("report.md", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_unconfirmed_folder_shows_tree_of_contents(self, mock_req):
        xml = _make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/proj", "name": "proj", "is_collection": True},
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/proj/a.md", "name": "a.md", "is_collection": False, "size": 500},
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/proj/b.md", "name": "b.md", "is_collection": False, "size": 300},
        ])
        mock_req.return_value = MagicMock(status_code=207, text=xml)
        result = self.t.delete_nextcloud_file("proj", confirmed=False)
        self.assertIn("a.md", result)
        self.assertIn("b.md", result)
        self.assertIn("confirmed=True", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_confirmed_true_sends_delete(self, mock_req):
        """confirmed=True: PROPFIND first (enumerate), then DELETE."""
        mock_req.side_effect = [
            MagicMock(status_code=207, text=_make_propfind_xml([  # PROPFIND
                {"href": "/remote.php/dav/files/TestBot/Shared/Docs/file.md", "name": "file.md", "is_collection": False, "size": 100},
            ])),
            MagicMock(status_code=204),  # DELETE
        ]
        result = self.t.delete_nextcloud_file("file.md", confirmed=True)
        methods = [c[0][0] for c in mock_req.call_args_list]
        self.assertIn("DELETE", methods)
        self.assertIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_confirmed_delete_nonexistent_returns_404_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.delete_nextcloud_file("ghost.md", confirmed=True)
        self.assertIn("404", result)
        self.assertNotIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_network_error_on_confirmed_delete(self, mock_req):
        import requests as req_lib
        mock_req.side_effect = req_lib.ConnectionError("down")
        result = self.t.delete_nextcloud_file("file.md", confirmed=True)
        self.assertIn("Network error", result)


# ── _share_folder ─────────────────────────────────────────────────────────────

class TestShareFolder(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()
        self.t.valves.NEXTCLOUD_SHARE_WITH = "group:bap,user:churnm"

    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.post")
    def test_shares_with_group_and_user(self, mock_post, mock_get):
        # Not yet shared
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"ocs": {"data": []}})
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ocs": {"meta": {"statuscode": 100}}})

        result = self.t._ensure_folder_shared(FOLDER_URL)
        self.assertIsNone(result)  # None = success

        post_calls = [c[1]['data'] for c in mock_post.call_args_list]
        share_types_and_targets = [(d['shareType'], d['shareWith']) for d in post_calls]
        self.assertIn((1, 'bap'), share_types_and_targets)    # group share
        self.assertIn((0, 'churnm'), share_types_and_targets) # user share

    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.post")
    def test_skips_already_shared_targets(self, mock_post, mock_get):
        # bap already shared, churnm not yet
        existing = [{"share_type": 1, "share_with": "bap"}]
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"ocs": {"data": existing}})
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ocs": {"meta": {"statuscode": 100}}})

        self.t._ensure_folder_shared(FOLDER_URL)

        post_calls = [c[1]['data'] for c in mock_post.call_args_list]
        targets = [(d['shareType'], d['shareWith']) for d in post_calls]
        self.assertNotIn((1, 'bap'), targets)    # already shared, skip
        self.assertIn((0, 'churnm'), targets)    # not yet shared, add

    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.post")
    def test_no_duplicate_shares_when_all_already_shared(self, mock_post, mock_get):
        existing = [
            {"share_type": 1, "share_with": "bap"},
            {"share_type": 0, "share_with": "churnm"},
        ]
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"ocs": {"data": existing}})

        self.t._ensure_folder_shared(FOLDER_URL)
        mock_post.assert_not_called()

    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.post")
    def test_share_api_failure_returns_error_string(self, mock_post, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"ocs": {"data": []}})
        mock_post.return_value = MagicMock(status_code=200,
            json=lambda: {"ocs": {"meta": {"statuscode": 403, "message": "Cannot share"}}})

        result = self.t._ensure_folder_shared(FOLDER_URL)
        self.assertIsNotNone(result)
        self.assertIn("Cannot share", result)

    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.post")
    def test_empty_share_with_skips_sharing(self, mock_post, mock_get):
        self.t.valves.NEXTCLOUD_SHARE_WITH = ""
        self.t._ensure_folder_shared(FOLDER_URL)
        mock_post.assert_not_called()
        mock_get.assert_not_called()

    @patch("nextcloud_document_sync.requests.get")
    def test_network_error_on_share_check_returns_error(self, mock_get):
        import requests as req_lib
        mock_get.side_effect = req_lib.ConnectionError("down")
        result = self.t._ensure_folder_shared(FOLDER_URL)
        self.assertIsNotNone(result)
        self.assertIn("Network error", result)

    @patch("nextcloud_document_sync.requests.request")
    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.post")
    def test_sync_calls_share_after_folder_creation(self, mock_post, mock_get, mock_put, mock_req):
        """sync_document_to_nextcloud must call _ensure_folder_shared on NEXTCLOUD_FOLDER."""
        mock_req.return_value = MagicMock(status_code=405)  # MKCOL (folder exists)
        mock_put.return_value = MagicMock(status_code=201)  # PUT file
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"ocs": {"data": []}})
        mock_post.return_value = MagicMock(status_code=200,
            json=lambda: {"ocs": {"meta": {"statuscode": 100}}})

        self.t.sync_document_to_nextcloud(content="hello", filename="test.md")
        mock_post.assert_called()  # sharing API was invoked


# ── Helpers for new tests ─────────────────────────────────────────────────────

def _make_fileid_xml(fileid, path="/remote.php/dav/files/TestBot/Shared/Docs/file.txt"):
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>{path}</d:href>
    <d:propstat>
      <d:prop><oc:fileid>{fileid}</oc:fileid></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


def _make_versions_xml(versions):
    """versions: list of dicts with version_id, size, last_modified"""
    root_entry = """
  <d:response>
    <d:href>/remote.php/dav/versions/TestBot/versions/9999/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>9999</d:displayname>
        <d:resourcetype><d:collection/></d:resourcetype>
        <d:getcontentlength>0</d:getcontentlength>
        <d:getlastmodified>Mon, 01 Jan 2026 00:00:00 GMT</d:getlastmodified>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>"""
    items = ""
    for v in versions:
        items += f"""
  <d:response>
    <d:href>/remote.php/dav/versions/TestBot/versions/9999/{v['version_id']}</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>{v['version_id']}</d:displayname>
        <d:resourcetype/>
        <d:getcontentlength>{v['size']}</d:getcontentlength>
        <d:getlastmodified>{v['last_modified']}</d:getlastmodified>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>"""
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">{root_entry}{items}
</d:multistatus>"""


def _make_file_info_xml(fileid="42", size=2048, modified="Mon, 19 May 2026 18:00:00 GMT"):
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/TestBot/Shared/Docs/report.md</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>report.md</d:displayname>
        <d:resourcetype/>
        <d:getcontentlength>{size}</d:getcontentlength>
        <d:getlastmodified>{modified}</d:getlastmodified>
        <d:getcontenttype>text/markdown</d:getcontenttype>
        <oc:fileid>{fileid}</oc:fileid>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


# ── read_nextcloud_file ────────────────────────────────────────────────────────

class TestReadFile(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.get")
    def test_text_file_returns_decoded_content(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"Hello, world!",
            headers={"Content-Type": "text/plain"}
        )
        result = self.t.read_nextcloud_file("notes.txt")
        self.assertIn("Hello, world!", result)

    @patch("nextcloud_document_sync.requests.get")
    def test_404_returns_not_found(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"", headers={})
        result = self.t.read_nextcloud_file("missing.txt")
        self.assertIn("not found", result.lower())

    @patch("nextcloud_document_sync.requests.get")
    def test_401_returns_auth_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=401, content=b"", headers={})
        result = self.t.read_nextcloud_file("secret.txt")
        self.assertIn("401", result)

    @patch("nextcloud_document_sync.requests.get")
    def test_network_error_returns_message(self, mock_get):
        import requests as req_lib
        mock_get.side_effect = req_lib.ConnectionError("timeout")
        result = self.t.read_nextcloud_file("file.txt")
        self.assertIn("Network error", result)

    @patch("nextcloud_document_sync.requests.get")
    def test_pdf_extracts_text(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"%PDF-fake",
            headers={"Content-Type": "application/pdf"}
        )
        with patch("nextcloud_document_sync.pypdf.PdfReader") as mock_reader:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = "Extracted PDF text"
            mock_reader.return_value.pages = [mock_page]
            result = self.t.read_nextcloud_file("report.pdf")
        self.assertIn("Extracted PDF text", result)

    @patch("nextcloud_document_sync.requests.get")
    def test_docx_extracts_text(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"PK fake docx",
            headers={"Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        )
        with patch("nextcloud_document_sync.Document") as mock_doc:
            para = MagicMock()
            para.text = "Word content here"
            mock_doc.return_value.paragraphs = [para]
            result = self.t.read_nextcloud_file("report.docx")
        self.assertIn("Word content here", result)

    @patch("nextcloud_document_sync.requests.get")
    def test_unknown_binary_returns_error(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"\x89PNG\r\n",
            headers={"Content-Type": "image/png"}
        )
        result = self.t.read_nextcloud_file("photo.png")
        self.assertIn("binary", result.lower())


# ── get_file_info ──────────────────────────────────────────────────────────────

class TestGetFileInfo(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_returns_metadata(self, mock_req):
        mock_req.return_value = MagicMock(status_code=207, text=_make_file_info_xml("42", 2048))
        result = self.t.get_file_info("report.md")
        self.assertIn("report.md", result)
        self.assertIn("2", result)   # size shows somewhere
        self.assertIn("42", result)  # file_id

    @patch("nextcloud_document_sync.requests.request")
    def test_404_returns_not_found(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.get_file_info("ghost.md")
        self.assertIn("not found", result.lower())

    @patch("nextcloud_document_sync.requests.request")
    def test_401_returns_auth_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=401, text="Unauthorized")
        result = self.t.get_file_info("file.md")
        self.assertIn("401", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_network_error(self, mock_req):
        import requests as req_lib
        mock_req.side_effect = req_lib.ConnectionError("down")
        result = self.t.get_file_info("file.md")
        self.assertIn("Network error", result)


# ── list_folder_recursive ──────────────────────────────────────────────────────

class TestListFolderRecursive(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_shows_nested_files(self, mock_req):
        xml = _make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/sub", "name": "sub", "is_collection": True},
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs/sub/deep.md", "name": "deep.md", "is_collection": False, "size": 100},
        ])
        mock_req.return_value = MagicMock(status_code=207, text=xml)
        result = self.t.list_folder_recursive()
        self.assertIn("deep.md", result)
        self.assertIn("sub", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_empty_folder(self, mock_req):
        xml = _make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
        ])
        mock_req.return_value = MagicMock(status_code=207, text=xml)
        result = self.t.list_folder_recursive()
        self.assertIn("empty", result.lower())

    @patch("nextcloud_document_sync.requests.request")
    def test_uses_infinity_depth(self, mock_req):
        mock_req.return_value = MagicMock(status_code=207, text=_make_propfind_xml([
            {"href": "/remote.php/dav/files/TestBot/Shared/Docs", "name": "Docs", "is_collection": True},
        ]))
        self.t.list_folder_recursive()
        headers = mock_req.call_args[1]["headers"]
        self.assertEqual(headers["Depth"], "infinity")

    @patch("nextcloud_document_sync.requests.request")
    def test_404_returns_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.list_folder_recursive("nonexistent")
        self.assertIn("not found", result.lower())


# ── list_file_versions ─────────────────────────────────────────────────────────

class TestListFileVersions(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.request")
    def test_lists_versions(self, mock_req):
        mock_req.side_effect = [
            MagicMock(status_code=207, text=_make_fileid_xml("9999")),
            MagicMock(status_code=207, text=_make_versions_xml([
                {"version_id": "1748000000", "size": 1234, "last_modified": "Mon, 19 May 2026 18:00:00 GMT"},
                {"version_id": "1747900000", "size": 1100, "last_modified": "Sun, 18 May 2026 10:00:00 GMT"},
            ])),
        ]
        result = self.t.list_file_versions("file.txt")
        self.assertIn("1748000000", result)
        self.assertIn("1747900000", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_no_versions_returns_message(self, mock_req):
        mock_req.side_effect = [
            MagicMock(status_code=207, text=_make_fileid_xml("9999")),
            MagicMock(status_code=207, text=_make_versions_xml([])),
        ]
        result = self.t.list_file_versions("file.txt")
        self.assertIn("no version", result.lower())

    @patch("nextcloud_document_sync.requests.request")
    def test_file_not_found_returns_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.list_file_versions("ghost.txt")
        self.assertIn("404", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_network_error(self, mock_req):
        import requests as req_lib
        mock_req.side_effect = req_lib.ConnectionError("down")
        result = self.t.list_file_versions("file.txt")
        self.assertIn("Network error", result)


# ── restore_file_version ───────────────────────────────────────────────────────

class TestRestoreFileVersion(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.request")
    def test_restores_version(self, mock_req, mock_get, mock_put):
        mock_req.return_value = MagicMock(status_code=207, text=_make_fileid_xml("9999"))
        mock_get.return_value = MagicMock(
            status_code=200, content=b"old content",
            headers={"Content-Type": "text/plain"},
        )
        mock_put.return_value = MagicMock(status_code=204)
        result = self.t.restore_file_version("file.txt", "1748000000")
        self.assertIn("✓", result)
        self.assertIn("1748000000", mock_get.call_args[0][0])

    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.request")
    def test_put_targets_live_file_url(self, mock_req, mock_get, mock_put):
        mock_req.return_value = MagicMock(status_code=207, text=_make_fileid_xml("9999"))
        mock_get.return_value = MagicMock(
            status_code=200, content=b"old content",
            headers={"Content-Type": "text/plain"},
        )
        mock_put.return_value = MagicMock(status_code=204)
        self.t.restore_file_version("file.txt", "1748000000")
        self.assertIn("/Shared/Docs/file.txt", mock_put.call_args[0][0])

    @patch("nextcloud_document_sync.requests.get")
    @patch("nextcloud_document_sync.requests.request")
    def test_version_not_found_returns_404_error(self, mock_req, mock_get):
        mock_req.return_value = MagicMock(status_code=207, text=_make_fileid_xml("9999"))
        mock_get.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.restore_file_version("file.txt", "badid")
        self.assertIn("404", result)
        self.assertNotIn("✓", result)

    @patch("nextcloud_document_sync.requests.request")
    def test_file_not_found_returns_error(self, mock_req):
        mock_req.return_value = MagicMock(status_code=404, text="Not Found")
        result = self.t.restore_file_version("ghost.txt", "123")
        self.assertIn("404", result)


# ── export_as_pdf ──────────────────────────────────────────────────────────────

class TestExportAsPdf(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    def test_exports_text_as_pdf(self, mock_get, mock_put):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"# Report\n\nSome content.",
            headers={"Content-Type": "text/plain"}
        )
        mock_put.return_value = MagicMock(status_code=201)
        result = self.t.export_as_pdf("report.md", "report.pdf")
        self.assertIn("✓", result)
        put_url = mock_put.call_args[0][0]
        self.assertIn("report.pdf", put_url)

    @patch("nextcloud_document_sync.requests.get")
    def test_source_not_found_returns_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"", headers={})
        result = self.t.export_as_pdf("missing.txt", "out.pdf")
        self.assertIn("not found", result.lower())

    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    def test_adds_pdf_extension_if_missing(self, mock_get, mock_put):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"text",
            headers={"Content-Type": "text/plain"}
        )
        mock_put.return_value = MagicMock(status_code=201)
        self.t.export_as_pdf("report.md", "nopdf")
        put_url = mock_put.call_args[0][0]
        self.assertIn("nopdf.pdf", put_url)

    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    def test_put_uses_pdf_content_type(self, mock_get, mock_put):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"text",
            headers={"Content-Type": "text/plain"}
        )
        mock_put.return_value = MagicMock(status_code=201)
        self.t.export_as_pdf("report.md", "report.pdf")
        put_headers = mock_put.call_args[1]["headers"]
        self.assertIn("pdf", put_headers["Content-Type"].lower())


# ── export_as_docx ─────────────────────────────────────────────────────────────

class TestExportAsDocx(unittest.TestCase):

    def setUp(self):
        self.t = _make_tools()

    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    def test_exports_text_as_docx(self, mock_get, mock_put):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"# Report\n\nSome content.",
            headers={"Content-Type": "text/plain"}
        )
        mock_put.return_value = MagicMock(status_code=201)
        result = self.t.export_as_docx("report.md", "report.docx")
        self.assertIn("✓", result)
        put_url = mock_put.call_args[0][0]
        self.assertIn("report.docx", put_url)

    @patch("nextcloud_document_sync.requests.get")
    def test_source_not_found_returns_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"", headers={})
        result = self.t.export_as_docx("missing.txt", "out.docx")
        self.assertIn("not found", result.lower())

    @patch("nextcloud_document_sync.requests.put")
    @patch("nextcloud_document_sync.requests.get")
    def test_adds_docx_extension_if_missing(self, mock_get, mock_put):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"text",
            headers={"Content-Type": "text/plain"}
        )
        mock_put.return_value = MagicMock(status_code=201)
        self.t.export_as_docx("report.md", "nodocx")
        put_url = mock_put.call_args[0][0]
        self.assertIn("nodocx.docx", put_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
