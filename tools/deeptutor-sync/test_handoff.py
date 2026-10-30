"""Regression tests use temporary fixtures, never the learner's live runtime."""
import asyncio
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import deeptutor_sync as sync


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dt-handoff-test-")
        self.root = Path(self.temporary.name)
        self.office = sync.Handoff(self.root / "office")
        self.home = sync.Handoff(self.root / "home")
        self.key = sync.AESGCM.generate_key(bit_length=256)
        for helper in (self.office, self.home):
            helper.data.mkdir(parents=True)
            sync.atomic_json(helper.config_path, {"remote": "https://github.com/example/test.git", "device": helper.app.name})
            (helper.local / "sync.key").write_bytes(sync.base64.urlsafe_b64encode(self.key))
        self.git_remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.git_remote)], check=True, capture_output=True)

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self, helper, title="Python 异步学到一半"):
        os.environ["DEEPTUTOR_HOME"] = str(helper.app / "runtime")
        from deeptutor.runtime.launcher import _reset_runtime_singletons
        _reset_runtime_singletons()
        from deeptutor.services.session.sqlite_store import SQLiteSessionStore
        store = SQLiteSessionStore(helper.data / "user/chat_history.db")

        async def seed():
            session = await store.create_session(title, "unified_handoff_fixture")
            await store.add_message(session["id"], "user", "await 和 Promise 有什么关系？",
                                    attachments=[{"url": "/files/attachments/unified_handoff_fixture/a/example.txt", "name": "example.txt"}])
            await store.add_message(session["id"], "assistant", "先保留这个问题，回家继续验证事件循环。")
        asyncio.run(seed())
        for name, content in {
            "user/workspace/chat/attachments/unified_handoff_fixture/a_example.txt": "附件原文",
            "user/workspace/learning/progress.json": '{"unit":"async-await","state":"in_progress"}',
            "memory/L3/recent.md": "正在学习 await，下一步画调用时序。",
            "user/settings/main.yaml": "MODEL_API_KEY=credential-canary-must-not-sync",
        }.items():
            path = helper.data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return store

    def package(self, target=None):
        stage = self.root / ("stage-" + sync.uuid.uuid4().hex)
        stage.mkdir()
        roots, files = sync.collect(self.office.data, stage)
        manifest = {"format": 1, "deeptutor": sync.VERSION,
                    "data_path": str((target or self.office).data), "created_at": "2026-10-30T00:00:00Z",
                    "roots": roots, "files": files}
        return sync.encrypt_package(manifest, stage, self.key), manifest

    def transport(self, helper):
        return sync.GitTransport(helper.local / "test-cache.git", str(self.git_remote), test_local=True)

    def test_full_conversation_progress_memory_attachment_roundtrip_with_real_store(self):
        self.fixture(self.office)
        payload, manifest = self.package(self.home)
        self.assertNotIn(b"credential-canary", payload)
        stage = self.root / "restore"
        parsed = sync.unpack_package(payload, stage, self.key, self.home.data)
        self.home.replace(stage, parsed, "fixture-commit")
        from deeptutor.services.session.sqlite_store import SQLiteSessionStore
        store = SQLiteSessionStore(self.home.data / "user/chat_history.db")
        result = asyncio.run(store.get_session_with_messages("unified_handoff_fixture"))
        self.assertEqual(result["title"], "Python 异步学到一半")
        self.assertEqual(len(result["messages"]), 2)
        self.assertIn("Promise", result["messages"][0]["content"])
        self.assertTrue(result["messages"][0]["attachments"])
        self.assertEqual((self.home.data / "memory/L3/recent.md").read_text(encoding="utf-8"), "正在学习 await，下一步画调用时序。")
        self.assertEqual(json.loads((self.home.data / "user/workspace/learning/progress.json").read_text())["state"], "in_progress")
        self.assertEqual((self.home.data / "user/workspace/chat/attachments/unified_handoff_fixture/a_example.txt").read_text(encoding="utf-8"), "附件原文")
        self.assertFalse((self.home.data / "user/settings/main.yaml").exists())
        self.assertFalse(any("settings" in n for n in manifest["files"]))

    def test_sqlite_wal_commits_are_included(self):
        path = self.office.data / "user/chat_history.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE sample(value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('committed-in-wal')")
            conn.commit()
            self.assertTrue(Path(str(path) + "-wal").exists())
            payload, _ = self.package()
        stage = self.root / "restored-wal"
        sync.unpack_package(payload, stage, self.key)
        with closing(sqlite3.connect(stage / "user/chat_history.db")) as conn:
            self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "committed-in-wal")

    def test_wrong_key_and_corruption_do_not_restore(self):
        self.fixture(self.office)
        payload, _ = self.package()
        for bad, key in [(payload, os.urandom(32)), (payload[:-1] + bytes([payload[-1] ^ 1]), self.key)]:
            stage = self.root / sync.uuid.uuid4().hex
            with self.assertRaises(sync.HandoffError):
                sync.unpack_package(bad, stage, key)
            self.assertFalse(stage.exists())

    def test_zip_traversal_is_rejected_even_with_valid_encryption(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../outside.txt", "no")
        nonce = os.urandom(12)
        payload = sync.MAGIC + nonce + sync.AESGCM(self.key).encrypt(nonce, archive.getvalue(), sync.MAGIC)
        with self.assertRaises(sync.HandoffError):
            sync.unpack_package(payload, self.root / "unsafe", self.key)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_version_and_absolute_path_mismatch_are_rejected(self):
        self.fixture(self.office)
        payload, _ = self.package()
        with self.assertRaises(sync.HandoffError):
            sync.unpack_package(payload, self.root / "mismatch", self.key, self.home.data)
        with patch.object(sync, "VERSION", "different"):
            with self.assertRaises(sync.HandoffError):
                sync.unpack_package(payload, self.root / "wrong-version", self.key)

    def test_active_turn_blocks_handoff(self):
        self.fixture(self.office)
        with closing(sqlite3.connect(self.office.data / "user/chat_history.db")) as conn:
            conn.execute("INSERT INTO turns(id,session_id,status,created_at,updated_at) VALUES ('t','unified_handoff_fixture','running',0,0)")
            conn.commit()
        with self.assertRaises(sync.HandoffError):
            sync.assert_idle(self.office.data)

    def test_system_workspace_config_is_excluded_but_general_attachment_included(self):
        general = "ws_" + "a" * 32
        system = "ws_" + "b" * 32
        catalog = self.office.data / sync.CATALOG
        catalog.parent.mkdir(parents=True)
        with closing(sqlite3.connect(catalog)) as conn:
            conn.execute("CREATE TABLE workspaces(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
            for identity, kind in [(general, "general"), (system, "system")]:
                path = self.office.data / "user/workspaces" / identity
                conn.execute("INSERT INTO workspaces VALUES (?,?)", (identity, json.dumps({"workspace_id": identity, "owner_id": "local-admin", "path": str(path), "kind": kind})))
                path.mkdir(parents=True)
                (path / "example.txt").write_text(kind)
            conn.commit()
        _, manifest = self.package()
        self.assertIn("user/workspaces/" + general + "/example.txt", manifest["files"])
        self.assertFalse(any(system in n for n in manifest["files"]))

    def test_restore_preserves_settings_and_saves_old_content(self):
        self.fixture(self.office)
        self.fixture(self.home, "家里旧记录")
        payload, _ = self.package(self.home)
        stage = self.root / "restore-backup"
        manifest = sync.unpack_package(payload, stage, self.key, self.home.data)
        credentials = self.home.data / "user/settings/main.yaml"
        before = credentials.read_bytes()
        self.home.replace(stage, manifest, "new-head")
        self.assertEqual(credentials.read_bytes(), before)
        backups = list((self.home.local / "backups").glob("*/data/user/chat_history.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual(conn.execute("SELECT title FROM sessions").fetchone()[0], "家里旧记录")

    def test_partial_restore_failure_rolls_back_original_and_baseline(self):
        self.fixture(self.office)
        self.fixture(self.home, "不能丢的原记录")
        sync.atomic_json(self.home.state_path, {"head": "original", "fingerprint": "original-fingerprint"})
        original = self.home.local_fingerprint()
        payload, _ = self.package(self.home)
        stage = self.root / "failed-restore"
        manifest = sync.unpack_package(payload, stage, self.key, self.home.data)
        with patch.object(sync.shutil, "copytree", side_effect=OSError("simulated disk full")):
            with self.assertRaises(OSError):
                self.home.replace(stage, manifest, "replacement")
        self.assertEqual(self.home.local_fingerprint(), original)
        self.assertEqual(sync.read_json(self.home.state_path)["head"], "original")
        self.assertFalse(self.home.journal_path.exists())

    def test_concurrent_git_push_rejects_stale_writer(self):
        a, b = self.transport(self.office), self.transport(self.home)
        first = a.publish(b"first-encrypted-fixture", None)
        self.assertEqual(b.head(), first)
        second = a.publish(b"second-encrypted-fixture", first)
        with self.assertRaises(sync.HandoffError):
            b.publish(b"conflicting-encrypted-fixture", first)
        self.assertEqual(a.head(), second)
        self.assertEqual(a.read(second), b"second-encrypted-fixture")

    def test_resume_does_not_overwrite_unsynced_local_changes(self):
        self.fixture(self.office)
        self.fixture(self.home, "本机新记录")
        sync.atomic_json(self.home.state_path, {"head": "old", "fingerprint": "different"})
        payload, _ = self.package(self.home)
        transport = self.transport(self.office)
        head = transport.publish(payload, None)
        before = self.home.local_fingerprint()
        with patch.object(self.home, "transport", return_value=transport), patch.object(self.home, "stop"):
            with self.assertRaises(sync.HandoffError):
                self.home.resume(False)
        self.assertEqual(before, self.home.local_fingerprint())
        self.assertNotEqual(sync.read_json(self.home.state_path)["head"], head)

    def test_offline_upload_keeps_encrypted_outbox_without_advancing_state(self):
        self.fixture(self.office)
        with patch.object(self.office, "stop"), patch.object(self.office, "transport", side_effect=sync.HandoffError("offline")):
            with self.assertRaises(sync.HandoffError):
                self.office.finish()
        self.assertEqual(len(list((self.office.local / "outbox").glob("*.bin"))), 1)
        self.assertFalse(self.office.state_path.exists())

    def test_cannot_target_main_branch(self):
        with self.assertRaises(sync.HandoffError):
            sync.GitTransport(self.root / "bad-cache", "https://github.com/example/test.git", "main")

    def test_empty_learning_directories_survive_restore(self):
        (self.office.data / "user/workspace/book/empty-book").mkdir(parents=True)
        payload, _ = self.package(self.home)
        stage = self.root / "empty-directories"
        manifest = sync.unpack_package(payload, stage, self.key, self.home.data)
        self.home.replace(stage, manifest, "empty-head")
        self.assertTrue((self.home.data / "user/workspace/book/empty-book").is_dir())

    def test_restored_conversation_is_returned_by_real_fastapi_route(self):
        self.fixture(self.office)
        payload, _ = self.package(self.home)
        stage = self.root / "api-restore"
        manifest = sync.unpack_package(payload, stage, self.key, self.home.data)
        self.home.replace(stage, manifest, "api-fixture")
        # A fresh process matches a real post-restore restart. Some installed
        # feature modules keep import-time path constants that a test reset
        # cannot change safely inside an already-imported Python interpreter.
        code = '''
from pathlib import Path
import os
from deeptutor.services.setup import init_user_directories
from deeptutor.services.config import ensure_runtime_settings_files, export_runtime_settings_to_env
init_user_directories(Path(os.environ["DEEPTUTOR_HOME"]))
ensure_runtime_settings_files()
export_runtime_settings_to_env(overwrite=True)
from fastapi.testclient import TestClient
from deeptutor.api.main import app
client = TestClient(app)
response = client.get("/api/sessions/unified_handoff_fixture?dt_workspace=")
assert response.status_code == 200, response.status_code
assert response.json()["title"] == "Python 异步学到一半"
assert len(response.json()["messages"]) == 2
print("RESTORED_SESSION_HTTP_OK")
'''
        env = dict(os.environ, DEEPTUTOR_HOME=str(self.home.app / "runtime"))
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-2500:])
        self.assertIn("RESTORED_SESSION_HTTP_OK", result.stdout)

    def test_active_background_writer_blocks_stop(self):
        activity = self.office.data / "user/.runtime/workspace-activity.sqlite3"
        activity.parent.mkdir(parents=True)
        with closing(sqlite3.connect(activity)) as conn:
            conn.execute("BEGIN")
            conn.execute("PRAGMA user_version").fetchone()
            with patch.object(sync.subprocess, "run") as runner:
                with self.assertRaises(sync.HandoffError):
                    self.office.stop()
                runner.assert_not_called()

    def test_unmodified_installation_templates_are_not_unsynced_learning(self):
        from deeptutor.runtime.home import PACKAGE_ROOT
        source = PACKAGE_ROOT / "deeptutor/services/persona/presets/peer/PERSONA.md"
        target = self.home.data / "user/workspace/personas/peer/PERSONA.md"
        target.parent.mkdir(parents=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8", newline="\r\n")
        suggestion = self.home.data / "user/workspace/suggestions/starters.json"
        sync.atomic_json(suggestion, {"suggestions": [], "status": "no-material", "shown": []})
        self.assertFalse(self.home.has_learning_content())
        target.write_text(target.read_text(encoding="utf-8") + "\n我的修改\n", encoding="utf-8")
        self.assertTrue(self.home.has_learning_content())

    def test_database_header_and_starter_cache_do_not_create_false_conflicts(self):
        self.fixture(self.home)
        before = self.home.local_fingerprint()
        with closing(sqlite3.connect(self.home.data / "user/chat_history.db")) as conn:
            conn.execute("PRAGMA user_version=3")
        sync.atomic_json(self.home.data / "user/workspace/suggestions/starters.json", {"generated_at": 123})
        self.assertEqual(before, self.home.local_fingerprint())
        with closing(sqlite3.connect(self.home.data / "user/chat_history.db")) as conn:
            conn.execute("UPDATE messages SET content='真实的新学习内容' WHERE role='user'")
            conn.commit()
        self.assertNotEqual(before, self.home.local_fingerprint())

    def test_first_resume_bootstraps_local_configuration_and_accepts_saved_history(self):
        self.fixture(self.office)
        payload, _ = self.package(self.home)
        transport = self.transport(self.home)
        head = transport.publish(payload, None)
        with patch.object(self.home, "transport", return_value=transport), patch.object(self.home, "stop"):
            self.home.resume(False)
        self.assertEqual(sync.read_json(self.home.state_path)["head"], head)
        self.assertTrue((self.home.data / "user/settings/main.yaml").exists())
        with closing(sqlite3.connect(self.home.data / "user/chat_history.db")) as conn:
            self.assertEqual(conn.execute("SELECT title FROM sessions").fetchone()[0], "Python 异步学到一半")


if __name__ == "__main__":
    unittest.main(verbosity=2)
