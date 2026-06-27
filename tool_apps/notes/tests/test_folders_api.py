import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tool_apps.notes import api


class FolderApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="notes-test-", dir=str(Path(__file__).parent))
        self.original_workspace_dir = api.WORKSPACE_DIR
        api.WORKSPACE_DIR = Path(self.temp_dir)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        api.WORKSPACE_DIR = self.original_workspace_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_folder_and_block_delete_when_pages_exist(self):
        folder = asyncio.run(api.create_folder("notes", "New folder"))
        self.assertEqual(folder["name"], "New folder")
        self.assertTrue(folder["id"])

        page_dir = api.get_base_dir("notes") / "test-page"
        page_dir.mkdir(parents=True, exist_ok=True)
        meta = {"id": "test-page", "type": "notes", "title": "Test", "createdAt": "now", "updatedAt": "now", "tags": [], "appVersion": "0.1", "folderId": folder["id"]}
        (page_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with self.assertRaises(Exception):
            asyncio.run(api.delete_folder("notes", folder["id"]))


if __name__ == "__main__":
    unittest.main()
