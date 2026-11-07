"""Long-lived storage contracts: mixed references, crash recovery and isolation."""

import base64
import copy
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pipeline
import publisher
import storage
import sync
from PIL import Image, UnidentifiedImageError
from pypdf import PageObject

ROOT = Path(__file__).resolve().parents[2]


def cover_bytes(extension, size=(2, 2), quality=85):
    with Image.new("RGB", size, "white") as image:
        buffer = io.BytesIO()
        image.save(
            buffer, format={"jpg": "JPEG", "webp": "WEBP"}[extension], quality=quality
        )
        return buffer.getvalue()


def local_covers(directory, digest):
    for extension in ("jpg", "webp"):
        (directory / f"{digest}.{extension}").write_bytes(cover_bytes(extension))


class MemoryStore:
    """In-memory object boundary with explicit interrupted-operation injection."""

    def __init__(self):
        self.objects = {}
        self.fail = None
        self.deleted = []

    def get(self, role, key):
        return self.objects.get((role, key))

    def put(self, role, key, body, content_type):
        if self.fail == ("put", role, key):
            self.fail = None
            raise OSError("injected upload failure")
        self.objects[role, key] = (body, content_type)

    def delete(self, role, key):
        if self.fail == ("delete", role, key):
            self.fail = None
            raise OSError("injected delete failure")
        self.deleted.append((role, key))
        self.objects.pop((role, key), None)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore()

    def stage(self, label, ids, branch=None):
        directory = self.root / label
        directory.mkdir(exist_ok=True)
        items = []
        for digest in ids:
            (directory / f"{digest}.pdf").write_bytes(f"{digest}.pdf".encode())
            local_covers(directory, digest)
            items.append(
                {"id": digest, "url": f"https://byrdocs.org/files/{digest}.pdf"}
            )
        manifest = {"files": [{"md5": digest} for digest in ids]}
        storage.stage(self.store, label, manifest, items, directory, branch)

    def publisher(self, items):
        expected = {
            "metadata.json": (json.dumps(items).encode(), "application/json"),
            "sitemap.xml": (b"<urlset />", "application/xml"),
        }
        for key, (body, kind) in expected.items():
            self.store.put("data", key, body, kind)
        return expected

    def test_uploaded_covers_are_owned_and_missing_output_blocks_cleanup(self):
        # Long-lived storage contract: covers upload before PR and remain a cleanup gate.
        digest = "a" * 32
        self.stage("A", [digest], "A")
        key = digest + ".webp"
        self.assertIsNotNone(self.store.get("files", key))
        self.assertIn("files/" + key, storage.load_state(self.store)["owned"])

        for bad in (None, (b"broken", "image/webp")):

            def incomplete(items, bad=bad):
                roots = self.publisher(items)
                if bad is None:
                    self.store.objects.pop(("files", key))
                else:
                    self.store.objects["files", key] = bad
                return roots

            with (
                self.subTest(bad=bad),
                self.assertRaises((ValueError, UnidentifiedImageError)),
            ):
                self.publish(self.store, "A", incomplete)
            self.assertIsNone(storage.load_state(self.store)["published"])
            with self.assertRaisesRegex(ValueError, "incomplete"):
                self.cleanup(self.store, set(), set(), {"A"})
            self.store.put("files", key, cover_bytes("webp"), "image/webp")

        self.publish(self.store, "A", self.publisher)
        self.assertIsNone(storage.load_state(self.store)["confirming"])
        self.assertIsNotNone(self.store.get("files", key))

    def test_replace_preserves_ordinary_shared_open_and_unknown_objects(self):
        old, shared, new, ordinary, waiting = [letter * 32 for letter in "abcde"]
        self.stage("ordinary", [ordinary])
        self.stage("A", [old, shared], "A")
        self.publish(self.store, "A", self.publisher)
        self.stage("pending", [waiting], "pending")
        self.stage("B", [new], "B")
        self.publish(self.store, "B", self.publisher)
        unknown = ("files", "f" * 32 + ".pdf")
        self.store.objects[unknown] = (b"not ours", "application/pdf")
        self.cleanup(self.store, {shared, ordinary}, {"pending"}, {"A"})
        for obj in storage.object_names([old]):
            role, key, _ = storage.split_object(obj)
            self.assertIsNone(self.store.get(role, key))
        for digest in (shared, new, ordinary, waiting):
            for obj in storage.object_names([digest]):
                role, key, _ = storage.split_object(obj)
                self.assertIsNotNone(self.store.get(role, key))
        self.assertIn(unknown, self.store.objects)

    def test_upload_crash_journal_allows_retry_without_adopting_existing_objects(self):
        digest = "a" * 32
        existing = ("files", digest + ".jpg")
        self.store.objects[existing] = (cover_bytes("jpg", (3, 3)), "image/jpeg")
        self.store.fail = ("put", "backup", digest + ".pdf")
        with self.assertRaises(OSError):
            self.stage("A", [digest])
        self.assertFalse(storage.load_state(self.store)["batches"]["A"]["ready"])
        self.stage("A", [digest])
        state = storage.load_state(self.store)
        self.assertTrue(state["batches"]["A"]["ready"])
        self.assertNotIn("files/" + digest + ".jpg", state["owned"])
        self.store.objects["files", digest + ".pdf"] = (b"corrupt", "application/pdf")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.stage("A", [digest])

    def test_reuse_valid_covers_and_original_backup_without_adopting_or_overwriting(
        self,
    ):
        # Long-lived importer regression: encoding and backup MIME do not identify a PDF.
        digest = "a" * 32
        existing = {
            ("files", digest + ".jpg"): (
                cover_bytes("jpg", (8, 8), 60),
                "Image/JPEG; charset=binary",
            ),
            ("files", digest + ".webp"): (
                cover_bytes("webp", (8, 8), 60),
                "image/webp; charset=binary",
            ),
            ("backup", digest + ".pdf"): (
                (digest + ".pdf").encode(),
                "application/octet-stream; charset=binary",
            ),
        }
        self.store.objects.update(existing)
        self.stage("A", [digest])
        for key, value in existing.items():
            self.assertEqual(self.store.objects[key], value)
        self.assertEqual(
            storage.load_state(self.store)["owned"], ["files/" + digest + ".pdf"]
        )
        for bad, kind in (
            (b"corrupt image", "image/jpeg"),
            (cover_bytes("webp"), "image/jpeg"),
            (cover_bytes("jpg"), "image/png; charset=binary"),
        ):
            with self.subTest(body=bad[:10]):
                self.store.objects["files", digest + ".jpg"] = (bad, kind)
                with self.assertRaises((ValueError, UnidentifiedImageError)):
                    self.stage("A", [digest])

    def test_same_version_rebuild_replaces_only_after_publication_and_is_repeatable(
        self,
    ):
        # Long-lived pipeline contract: an explicit retry may change a release's PDFs.
        branch = sync.BRANCH_PREFIX + "1.5.0"

        def upload(digest):
            (self.root / f"{digest}.pdf").write_bytes(digest.encode())
            local_covers(self.root, digest)
            manifest = {"version": "1.5.0", "files": [{"md5": digest}]}
            items = [{"id": digest, "url": f"https://byrdocs.org/files/{digest}.pdf"}]
            return pipeline.stage_release(self.store, manifest, items, self.root)

        old, new = "a" * 32, "b" * 32
        first = upload(old)
        self.publish(self.store, first, self.publisher)
        second = upload(new)
        self.assertNotEqual(first, second)
        self.assertEqual(storage.load_state(self.store)["published"], first)
        self.cleanup(self.store, {old}, {branch}, {branch})
        self.assertIsNotNone(self.store.get("files", old + ".pdf"))
        self.publish(self.store, second, self.publisher)
        self.cleanup(self.store, {new}, set(), {branch})
        self.assertIsNone(self.store.get("files", old + ".pdf"))
        self.assertIsNotNone(self.store.get("files", new + ".pdf"))
        self.assertEqual(upload(new), second)

    def test_partial_publication_blocks_cleanup_and_retry_confirms_both_roots(self):
        self.stage("A", ["a" * 32], "A")
        self.publish(self.store, "A", self.publisher)
        self.stage("B", ["b" * 32], "B")
        self.store.fail = ("put", "data", "sitemap.xml")
        with self.assertRaises(OSError):
            self.publish(self.store, "B", self.publisher)
        self.assertEqual(storage.load_state(self.store)["published"], "A")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.cleanup(self.store, set(), set(), {"A"})
        self.publish(self.store, "B", self.publisher)
        self.cleanup(self.store, set(), set(), {"A"})
        self.assertIsNone(self.store.get("files", "a" * 32 + ".pdf"))

    def test_delete_failure_retries_remaining_and_rechecks_new_references(self):
        a, b = "a" * 32, "b" * 32
        self.stage("A", [a], "A")
        self.stage("B", [b], "B")
        self.publish(self.store, "B", self.publisher)
        self.store.fail = ("delete", "files", a + ".jpg")
        with self.assertRaises(OSError):
            self.cleanup(self.store, set(), set(), {"A"})
        state = storage.load_state(self.store)
        self.assertTrue(state["pending_delete"])
        self.assertNotIn("backup/" + a + ".pdf", state["pending_delete"])
        deleted = list(self.store.deleted)
        self.cleanup(self.store, {a}, set(), {"A"})
        self.assertEqual(deleted, self.store.deleted)
        self.cleanup(self.store, set(), set(), {"A"})
        self.assertIsNone(self.store.get("files", a + ".pdf"))
        self.assertEqual(self.store.deleted.count(("backup", a + ".pdf")), 1)

    def test_cleanup_bounds_ledger_writes_and_recovers_a_lost_checkpoint(self):
        # Long-lived cleanup contract: bounded ledger I/O, safe retry after final save loss.
        a, b = "a" * 32, "b" * 32
        self.stage("old", [a, b], "old")
        with patch.object(self.store, "put", wraps=self.store.put) as put:
            self.cleanup(self.store, set(), set(), {"old"})
        self.assertEqual(
            sum(call.args[1] == storage.STATE_KEY for call in put.call_args_list), 2
        )
        self.assertEqual(
            sum(
                call.args[1] == storage.STATE_BACKUP_KEY for call in put.call_args_list
            ),
            2,
        )
        self.stage("retry", [a, b], "retry")
        original_put = self.store.put

        def fail_checkpoint(role, key, body, kind):
            if key == storage.STATE_KEY and not json.loads(body)["pending_delete"]:
                raise OSError("checkpoint lost")
            original_put(role, key, body, kind)

        with (
            patch.object(self.store, "put", side_effect=fail_checkpoint),
            self.assertRaisesRegex(OSError, "checkpoint lost"),
        ):
            self.cleanup(self.store, set(), set(), {"retry"})
        self.assertEqual(len(storage.load_state(self.store)["pending_delete"]), 8)
        # A new reference and re-upload must beat the stale deletion journal.
        self.store.put("files", a + ".pdf", b"reuploaded", "application/pdf")
        already_deleted = self.store.deleted.count(("files", a + ".pdf"))
        self.cleanup(self.store, {a}, set(), {"retry"})
        self.assertEqual(self.store.get("files", a + ".pdf")[0], b"reuploaded")
        self.assertEqual(
            self.store.deleted.count(("files", a + ".pdf")), already_deleted
        )
        self.assertFalse(storage.load_state(self.store)["pending_delete"])

    def test_lost_ledger_preserves_objects_and_does_not_infer_ownership(self):
        # Long-lived ownership boundary: published metadata cannot recreate deletion rights.
        self.stage("A", ["a" * 32])
        self.publish(self.store, "A", self.publisher)
        self.store.objects.pop(("data", storage.STATE_KEY))
        self.store.objects.pop(("data", storage.STATE_BACKUP_KEY))
        snapshot = copy.deepcopy(self.store.objects)
        pipeline.maintenance(self.store, self.root)
        self.assertEqual(self.store.objects, snapshot)
        self.assertFalse(self.store.deleted)
        self.stage("A", ["a" * 32])
        self.assertEqual(storage.load_state(self.store)["owned"], [])

    def test_missing_primary_preserves_backup_until_explicit_restore(self):
        # Long-lived ownership recovery contract: a missing primary must not reset its copy.
        self.stage("A", ["a" * 32])
        self.publish(self.store, "A", self.publisher)
        state = storage.load_state(self.store)
        backup = self.store.get("data", storage.STATE_BACKUP_KEY)
        self.assertEqual(backup, self.store.get("data", storage.STATE_KEY))
        self.store.objects.pop(("data", storage.STATE_KEY))
        snapshot = copy.deepcopy(self.store.objects)
        with self.assertRaisesRegex(ValueError, "主账本.*缺失.*恢复副本"):
            pipeline.maintenance(self.store, self.root)
        self.assertEqual(self.store.objects, snapshot)
        self.assertFalse(self.store.deleted)
        # Model an explicit operator restore; normal reference checks still own deletion.
        self.store.put("data", storage.STATE_KEY, *backup)
        self.assertEqual(storage.load_state(self.store), state)

    def test_backup_failure_stops_upload_and_retry_refreshes_both_ledgers(self):
        # Long-lived upload boundary: originals are exposed only after both journals save.
        self.store.fail = ("put", "data", storage.STATE_BACKUP_KEY)
        with self.assertRaises(OSError):
            self.stage("A", ["a" * 32])
        self.assertFalse(storage.load_state(self.store)["batches"]["A"]["ready"])
        self.assertFalse(
            any(role in {"files", "backup"} for role, _ in self.store.objects)
        )
        self.stage("A", ["a" * 32])
        self.assertTrue(storage.load_state(self.store)["batches"]["A"]["ready"])
        self.assertEqual(
            self.store.get("data", storage.STATE_KEY),
            self.store.get("data", storage.STATE_BACKUP_KEY),
        )

    def test_publication_allows_mime_parameters_but_rejects_wrong_type_or_bytes(self):
        # Long-lived storage boundary: media parameters do not change publication bytes.
        expected = self.publisher([])
        for key, (body, kind) in expected.items():
            self.store.put("data", key, body, kind + "; charset=utf-8")
        storage.confirm_publication(self.store, None, "revision", "1-1", expected)
        storage.verify_publication(self.store)
        for body, kind in ((b"[]", "text/plain"), (b"wrong", "application/json")):
            with self.subTest(body=body, kind=kind):
                self.store.put("data", "metadata.json", body, kind)
                with self.assertRaisesRegex(ValueError, "changed"):
                    storage.verify_publication(self.store)

    def test_confirmed_objects_survive_final_state_write_failure(self):
        self.stage("A", ["a" * 32], "A")
        self.publish(self.store, "A", self.publisher)
        self.stage("B", ["b" * 32], "B")
        original_put = self.store.put

        def fail_final_state(role, key, body, kind):
            if key == storage.STATE_KEY:
                state = json.loads(body)
                if state["published"] == "B" and state["confirming"] is None:
                    raise OSError("final state write interrupted")
            original_put(role, key, body, kind)

        with (
            patch.object(self.store, "put", side_effect=fail_final_state),
            self.assertRaises(OSError),
        ):
            self.publish(self.store, "B", self.publisher)
        self.assertEqual(
            storage.load_state(self.store)["confirming"],
            "B",
        )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.cleanup(self.store, set(), set(), {"A"})
        self.publish(self.store, "B", self.publisher)
        self.cleanup(self.store, set(), set(), {"A"})
        self.assertIsNone(self.store.get("files", "a" * 32 + ".pdf"))

    def test_orphans_expire_and_closed_pr_uploads_are_reclaimed(self):
        self.stage("orphan", ["a" * 32], "no-pr")
        self.stage("closed", ["b" * 32], "closed")
        self.cleanup(self.store, set(), set(), {"closed"})
        self.assertIsNone(self.store.get("files", "b" * 32 + ".pdf"))
        self.assertIsNotNone(self.store.get("files", "a" * 32 + ".pdf"))
        self.cleanup(
            self.store,
            set(),
            set(),
            set(),
            datetime.now(UTC) + timedelta(days=2),
        )
        self.assertFalse(storage.load_state(self.store)["owned"])

    def test_modified_publication_or_invalid_cleanup_key_fails_closed(self):
        self.stage("A", ["a" * 32])
        self.publish(self.store, "A", self.publisher)
        self.store.objects["data", "metadata.json"] = (b"[]", "application/json")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.cleanup(self.store, set(), set(), set())
        state = storage.load_state(self.store)
        state["owned"].append("data/metadata.json")
        self.store.put(
            "data", storage.STATE_KEY, json.dumps(state).encode(), "application/json"
        )
        with self.assertRaisesRegex(ValueError, "Invalid managed"):
            storage.load_state(self.store)

    def test_cleanup_accepts_legacy_time_and_prefers_retry_time(self):
        # Long-lived storage compatibility: preserve old ownership and retention rules.
        timestamp = datetime(2026, 11, 8, tzinfo=UTC)
        for legacy_only in (True, False):
            with self.subTest(legacy_only=legacy_only):
                self.store = MemoryStore()
                with patch.object(storage, "now", return_value=timestamp.isoformat()):
                    self.stage("A", ["a" * 32], "A")
                state = storage.load_state(self.store)
                batch = state["batches"]["A"]
                batch["created_at"] = (
                    timestamp if legacy_only else timestamp - timedelta(days=2)
                ).isoformat()
                if legacy_only:
                    del batch["attempted_at"]
                storage.save_state(self.store, state)
                closed = {"A": (timestamp - timedelta(days=1)).isoformat()}
                storage.cleanup(
                    self.store,
                    set(),
                    set(),
                    closed,
                    timestamp + timedelta(minutes=1),
                    before_delete=lambda: None,
                )
                self.assertFalse(self.store.deleted)
                self.assertEqual(
                    storage.load_state(self.store)["owned"], state["owned"]
                )
                storage.cleanup(
                    self.store,
                    set(),
                    set(),
                    closed,
                    timestamp + timedelta(hours=25),
                    before_delete=lambda: None,
                )
                self.assertEqual(len(self.store.deleted), 4)

    def test_retry_renews_grace_period_even_when_reusing_a_batch_or_upload_fails(self):
        # Long-lived pipeline regression: old PR closure cannot expire a fresh retry.
        digest = "a" * 32
        (self.root / f"{digest}.pdf").write_bytes(b"paper")
        local_covers(self.root, digest)
        manifest = {"version": "1.5.0", "files": [{"md5": digest}]}
        items = [{"id": digest, "url": f"https://byrdocs.org/files/{digest}.pdf"}]
        timestamp = datetime(2026, 11, 7, tzinfo=UTC)
        closed = {"automation/bump-1.5.0": (timestamp - timedelta(days=1)).isoformat()}
        for reuse, fail_upload in ((False, False), (True, False), (True, True)):
            with self.subTest(reuse=reuse, fail_upload=fail_upload):
                store = MemoryStore()
                if reuse:
                    with patch.object(
                        storage,
                        "now",
                        return_value=(timestamp - timedelta(days=2)).isoformat(),
                    ):
                        original = pipeline.stage_release(
                            store, manifest, items, self.root
                        )
                if fail_upload:
                    store.objects.pop(("backup", f"{digest}.pdf"))
                    store.fail = ("put", "backup", f"{digest}.pdf")
                with patch.object(storage, "now", return_value=timestamp.isoformat()):
                    if fail_upload:
                        with self.assertRaises(OSError):
                            pipeline.stage_release(store, manifest, items, self.root)
                    else:
                        retried = pipeline.stage_release(
                            store, manifest, items, self.root
                        )
                        if reuse:
                            self.assertEqual(retried, original)
                storage.cleanup(
                    store,
                    set(),
                    set(),
                    closed,
                    timestamp + timedelta(minutes=1),
                    before_delete=lambda: None,
                )
                self.assertFalse(store.deleted)
                self.assertIsNotNone(store.get("files", f"{digest}.pdf"))
                storage.cleanup(
                    store,
                    set(),
                    set(),
                    closed,
                    timestamp + timedelta(hours=24, minutes=1),
                    before_delete=lambda: None,
                )
                self.assertEqual(len(store.deleted), 4)

    def test_reference_change_guard_leaves_deletions_pending(self):
        self.stage("old", ["a" * 32], "closed")

        def changed():
            raise ValueError("References changed")

        with self.assertRaisesRegex(ValueError, "References changed"):
            storage.cleanup(
                self.store,
                set(),
                set(),
                {"closed": datetime.now(UTC).isoformat()},
                before_delete=changed,
            )
        self.assertFalse(self.store.deleted)
        self.assertEqual(len(storage.load_state(self.store)["pending_delete"]), 4)

    def cleanup(self, store, references, opened, closed, timestamp=None):
        return storage.cleanup(
            store,
            references,
            opened,
            {branch: datetime.now(UTC).isoformat() for branch in closed},
            timestamp,
            before_delete=lambda: None,
        )

    def publish(self, store, label, callback):
        expected = callback(storage.load_state(store)["batches"][label]["metadata"])
        return storage.confirm_publication(
            store,
            label,
            label,
            label,
            expected,
        )

    def test_ordinary_change_republishes_same_bump_batch(self):
        self.stage("A", ["a" * 32])
        items = storage.load_state(self.store)["batches"]["A"]["metadata"]
        storage.confirm_publication(
            self.store, "A", "commit-1", "1-1", self.publisher(items)
        )
        ordinary = {
            "id": "f" * 32,
            "url": "https://byrdocs.org/files/" + "f" * 32 + ".pdf",
        }
        storage.confirm_publication(
            self.store, "A", "commit-2", "2-1", self.publisher(items + [ordinary])
        )
        self.assertEqual(len(json.loads(self.store.get("data", "metadata.json")[0])), 2)
        self.assertEqual(
            storage.load_state(self.store)["published_revision"], "commit-2"
        )

    def test_full_publication_without_bump_and_newer_revision_recovers_partial(self):
        self.store.fail = ("put", "data", "sitemap.xml")
        with self.assertRaises(OSError):
            self.publisher([])
        ordinary = {
            "id": "f" * 32,
            "url": "https://byrdocs.org/files/" + "f" * 32 + ".pdf",
        }
        storage.confirm_publication(
            self.store, None, "commit-2", "2-1", self.publisher([ordinary])
        )
        state = storage.load_state(self.store)
        self.assertIsNone(state["confirming"])
        self.assertIsNone(state["published"])
        self.assertEqual(state["published_revision"], "commit-2")
        self.assertIsNone(state["public_verified_at"])

    def test_production_repository_and_separate_backup_configuration(self):
        env = production_env()
        config = storage.settings(env)
        self.assertEqual(config["backup_region"], "us-east-1")
        self.assertEqual(config["buckets"]["backup"], "archive-backup")
        with patch.object(storage.boto3, "client") as client:
            storage.R2(config)
        calls = client.call_args_list
        self.assertEqual(calls[0].kwargs["aws_access_key_id"], "primary")
        self.assertEqual(calls[1].kwargs["aws_access_key_id"], "backup")
        self.assertEqual(calls[1].kwargs["endpoint_url"], "https://backup.example")
        for key, value in [
            ("GITHUB_REPOSITORY", "Shxiao111/byrdocs-archive"),
            ("R2_ACCOUNT_ID", "invalid"),
            ("BACKUP_ENDPOINT", "http://backup.example"),
            ("R2_SECRET_ACCESS_KEY", ""),
            ("R2_DATA_BUCKET", "archive-files"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                storage.settings({**env, key: value})
        del env["BACKUP_SECRET_ACCESS_KEY"]
        with self.assertRaises(KeyError):
            storage.settings(env)


class PublisherBoundaryTests(unittest.TestCase):
    def test_superseded_default_branch_stops_publication_and_cleanup(self):
        with (
            patch.object(pipeline.subprocess, "check_output", return_value="old\n"),
            patch.object(sync, "gh", return_value='{"object":{"sha":"new"}}'),
            self.assertRaisesRegex(ValueError, "Default branch changed"),
        ):
            pipeline.require_current_default(ROOT)

    def test_workflows_use_only_upstream_secrets_and_one_publication_lock(self):
        workflow = sync.YAML_CODEC.load(
            (ROOT / ".github/workflows/bump-sync.yml").read_text(encoding="utf-8")
        )
        job = workflow["jobs"]["sync"]
        self.assertEqual(job["concurrency"]["queue"], "max")
        self.assertIn("github.repository == 'byrdocs/byrdocs-archive'", job["if"])
        self.assertIn("github.ref == 'refs/heads/master'", job["if"])
        self.assertNotIn("pull_request", job["if"])
        self.assertNotIn("secrets", str(workflow["jobs"]["test"]))
        publication = sync.YAML_CODEC.load(
            (ROOT / ".github/workflows/upload-metadata.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            job["concurrency"], publication["jobs"]["update"]["concurrency"]
        )
        self.assertEqual(
            job["env"]["BACKUP_ACCESS_KEY_ID"], "${{ secrets.BACKUP_ACCESS_KEY_ID }}"
        )
        cleanup = sync.YAML_CODEC.load(
            (ROOT / ".github/workflows/bump-cleanup.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(job["concurrency"], cleanup["jobs"]["cleanup"]["concurrency"])
        self.assertEqual(
            cleanup["on"]["workflow_run"]["workflows"], ["BUMP sync", "Upload metadata"]
        )
        self.assertTrue(
            any(
                step.get("env", {}).get("GH_TOKEN") == "${{ secrets.BUMP_PAT }}"
                for step in job["steps"]
            )
        )
        self.assertNotIn("BUMP_APP_PRIVATE_KEY", str(workflow))

    def test_ordinary_open_pr_protects_bump_file_references(self):
        digest = "d" * 32
        pr = {
            "number": 7,
            "state": "open",
            "head": {
                "ref": "ordinary-fix",
                "sha": "head",
                "repo": {"full_name": "contributor/archive"},
            },
        }
        details = {**pr, "changed_files": 1}
        item = {"id": digest, "url": f"https://byrdocs.org/files/{digest}.pdf"}
        responses = [
            json.dumps(details),
            json.dumps(
                [
                    [
                        {
                            "filename": f"metadata/{digest}.yml",
                            "status": "modified",
                            "sha": "blob",
                        }
                    ]
                ]
            ),
            json.dumps(
                {"content": base64.b64encode(sync.yaml_text(item).encode()).decode()}
            ),
        ]
        with patch.object(sync, "gh", side_effect=responses):
            refs, opened, closed = pipeline.pr_references([pr])
        self.assertEqual(refs, {digest})
        self.assertFalse(opened or closed)

        # Unfinished YAML and URLs still protect every potentially referenced file.
        other = "e" * 32
        unfinished = f"id: {digest}\nurl: https://example/{other.upper()}.pdf\ndata: ["
        responses[1] = json.dumps(
            [
                [
                    {
                        "filename": "metadata/unfinished.yml",
                        "status": "added",
                        "sha": "blob",
                    }
                ]
            ]
        )
        responses[2] = json.dumps(
            {"content": base64.b64encode(unfinished.encode()).decode()}
        )
        with patch.object(sync, "gh", side_effect=responses):
            refs, _, _ = pipeline.pr_references([pr])
        self.assertEqual(refs, {digest, other})

    def setUp(self):
        self.run = {
            "id": 42,
            "run_attempt": 1,
            "head_sha": "revision",
            "head_repository": {"full_name": sync.REPOSITORY},
            "event": "push",
            "status": "completed",
            "conclusion": "success",
        }
        self.outputs = {}
        self.downloads = 0

    def github(self, *args):
        if args[0] == "api":
            self.assertEqual(
                args[1],
                f"repos/{sync.REPOSITORY}/actions/workflows/upload-metadata.yml/runs"
                "?branch=master&per_page=1",
            )
            return json.dumps({"workflow_runs": [self.run]})
        self.assertEqual(
            args[:7],
            (
                "run",
                "download",
                "42",
                "--repo",
                sync.REPOSITORY,
                "--name",
                f"bump-publication-42-{self.run['run_attempt']}",
            ),
        )
        self.assertEqual(args[7], "--dir")
        self.downloads += 1
        for name, (body, _) in self.outputs.items():
            (Path(args[8]) / name).write_bytes(body)
        return ""

    def published_output(self, store, items):
        # The official publisher has already written these bytes before BUMP runs.
        actual = [
            {**item, "data": {**item.get("data", {}), "filesize": 100}}
            for item in items
        ]
        urls = [
            "https://byrdocs.org",
            "https://byrdocs.org/about",
            *[f"https://byrdocs.org/?q={item['id']}" for item in items],
        ]
        sitemap = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{url}</loc></url>" for url in urls)
            + "</urlset>"
        )
        self.outputs = {
            "metadata.json": (json.dumps(actual).encode(), "application/json"),
            "sitemap.xml": (sitemap.encode(), "application/xml"),
        }
        for name, value in self.outputs.items():
            store.put("data", name, *value)

    def test_confirmation_uses_successful_attempt_and_reuses_confirmed_output(self):
        # Long-lived boundary: evidence is bound to a run attempt and current revision.
        store = MemoryStore()
        items = [{"id": "a" * 32}, {"id": "b" * 32}]
        self.published_output(store, items)
        with (
            patch.object(sync, "gh", side_effect=self.github),
            patch.dict(os.environ, {"BYRDOCS_SITE_URL": "https://byrdocs.org"}),
        ):
            publisher.confirm(store, None, "revision", items)
            self.assertEqual(storage.load_state(store)["published_run"], "42-1")
            state = storage.load_state(store)
            state["public_verified_at"] = "already-verified"
            storage.save_state(store, state)
            snapshot = copy.deepcopy(store.objects)
            # Confirmation remains usable after artifact expiry; current status is rechecked.
            self.outputs = {}
            publisher.confirm(store, None, "revision", items)
            self.assertEqual(store.objects, snapshot)
            self.assertEqual(self.downloads, 1)
            self.run.update(run_attempt=2, conclusion="failure")
            with self.assertRaisesRegex(ValueError, "Upload metadata"):
                publisher.confirm(store, None, "revision", items)
            self.assertEqual(store.objects, snapshot)
            # A successful rerun of the same commit needs its own output confirmation.
            self.run["conclusion"] = "success"
            self.published_output(store, items)
            publisher.confirm(store, None, "revision", items)
            state = storage.load_state(store)
            self.assertEqual(state["published_run"], "42-2")
            self.assertIsNone(state["public_verified_at"])
            self.assertEqual(self.downloads, 2)

    def test_untrusted_or_unsuccessful_run_cannot_confirm_publication(self):
        for change in (
            {"head_sha": "old"},
            {"head_repository": {"full_name": "someone/fork"}},
            {"event": "pull_request"},
            {"conclusion": "failure"},
            {"conclusion": "cancelled"},
        ):
            with self.subTest(change=change):
                store = MemoryStore()
                original = self.run.copy()
                self.run.update(change)
                with (
                    patch.object(sync, "gh", side_effect=self.github),
                    self.assertRaisesRegex(ValueError, "Upload metadata"),
                ):
                    publisher.confirm(store, None, "revision", [])
                self.run = original
                self.assertEqual(store.objects, {})
        self.assertEqual(self.downloads, 0)

    def test_pending_publication_skips_maintenance_without_touching_storage(self):
        # Long-lived scheduling contract: queued/in-progress publication is not a failure.
        for status in ("queued", "in_progress", "waiting"):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = MemoryStore()
                state = storage.load_state(store)
                state["owned"] = storage.object_names(["a" * 32])
                storage.save_state(store, state)
                self.run.update(status=status, conclusion=None)
                snapshot = copy.deepcopy(store.objects)
                with (
                    patch.object(
                        pipeline, "require_current_default", return_value="revision"
                    ),
                    patch.object(
                        pipeline, "read_metadata", return_value=[{"id": "a" * 32}]
                    ),
                    patch.object(sync, "gh", side_effect=self.github),
                    patch.object(
                        publisher,
                        "public_ready",
                        side_effect=AssertionError("verified too early"),
                    ),
                ):
                    pipeline.maintenance(store, Path(directory))
                self.assertEqual(store.objects, snapshot)
                self.assertFalse(store.deleted)
        self.assertEqual(self.downloads, 0)

    def test_artifact_download_error_identifies_attempt_and_recovery(self):
        with (
            patch.object(
                sync,
                "gh",
                side_effect=[
                    json.dumps({"workflow_runs": [self.run]}),
                    subprocess.CalledProcessError(1, ["gh", "run", "download"]),
                ],
            ),
            self.assertRaisesRegex(
                ValueError, "bump-publication-42-1.*Upload metadata"
            ),
        ):
            publisher.confirm(MemoryStore(), None, "revision", [])

    def test_missing_or_mismatched_evidence_never_modifies_published_roots(self):
        # Long-lived isolation: only internal confirmation records may be written here.
        items = [{"id": "a" * 32}]
        for fault in (
            "missing-artifact",
            "missing-sitemap",
            "wrong-ids",
            "root-mismatch",
        ):
            with self.subTest(fault=fault):
                store = MemoryStore()
                self.published_output(store, items)
                roots = copy.deepcopy(store.objects)
                if fault == "missing-artifact":
                    self.outputs = {}
                elif fault == "missing-sitemap":
                    del self.outputs["sitemap.xml"]
                elif fault == "wrong-ids":
                    self.outputs["metadata.json"] = (b"[]", "application/json")
                else:
                    store.objects["data", "sitemap.xml"] = (
                        b"partial",
                        "application/xml",
                    )
                    roots = copy.deepcopy(store.objects)
                with (
                    patch.object(sync, "gh", side_effect=self.github),
                    patch.dict(os.environ, {"BYRDOCS_SITE_URL": "https://byrdocs.org"}),
                    self.assertRaises((FileNotFoundError, ValueError)),
                ):
                    publisher.confirm(store, None, "revision", items)
                for key, value in roots.items():
                    self.assertEqual(store.objects[key], value)
                self.assertIsNone(storage.load_state(store)["published_revision"])
                self.assertFalse(store.deleted)

    def test_first_import_uploads_before_pr_and_merge_publishes_ordinary_entries(self):
        # Long-lived production bootstrap contract: no pre-merged test manifest needed.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, work = root / "repo", root / "work"
            (repo / "schema").mkdir(parents=True)
            (repo / "metadata").mkdir()
            work.mkdir()
            (repo / "schema/test.yaml").write_bytes(
                (ROOT / "schema/test.yaml").read_bytes()
            )
            ordinary = {
                "id": "e" * 32,
                "url": "https://byrdocs.org/files/" + "e" * 32 + ".pdf",
                "type": "doc",
                "data": {"title": "ordinary"},
            }
            (repo / "metadata" / ("e" * 32 + ".yml")).write_text(
                sync.yaml_text(ordinary), encoding="utf-8"
            )
            source = {
                "config": {"release-type": "release", "version-number": "1.5.0"},
                "problemsets": [
                    {
                        "title": "2024–2025 高等数学期末考试",
                        "tags": ["分析上期末"],
                        "page-physical": 2,
                        "length-in-page": 1,
                    }
                ],
            }

            def download(url, path):
                if url.endswith(".pdf"):
                    with sync.PdfWriter() as writer:
                        writer.add_blank_page(width=200, height=200)
                        writer.add_blank_page(width=100, height=100)
                        writer.write(path)
                else:
                    sync.write_json(path, source)

            store = MemoryStore()
            output = root / "github-output"
            original_put = store.put

            def fail_cover_upload(role, key, body, kind):
                if role == "files" and key.endswith(".webp"):
                    raise OSError("cover upload failed")
                original_put(role, key, body, kind)

            with (
                patch.object(
                    pipeline, "require_current_default", return_value="revision-1"
                ),
                patch.object(pipeline, "pull_requests", return_value=[]),
                patch.object(sync, "download", side_effect=download),
                patch.object(
                    PageObject,
                    "extract_text",
                    lambda page: (
                        "版权声明与授权许可 CC BY-NC-SA 4.0"
                        if page.mediabox.width == 200
                        else ""
                    ),
                ),
                patch.object(
                    pipeline,
                    "maintenance",
                    side_effect=ValueError("cleanup unavailable"),
                ),
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}),
            ):
                with (
                    patch.object(store, "put", side_effect=fail_cover_upload),
                    self.assertRaisesRegex(OSError, "cover upload"),
                ):
                    pipeline.sync_release(store, repo, root / "failed", "1.5.0", False)
                self.assertFalse(output.exists())
                self.assertFalse(
                    next(iter(storage.load_state(store)["batches"].values()))["ready"]
                )
                pipeline.sync_release(store, repo, work, "1.5.0", True)
            self.assertEqual(output.read_text(encoding="utf-8"), "create_pr=true\n")
            self.assertEqual(len(storage.load_state(store)["owned"]), 4)
            for obj in storage.load_state(store)["owned"]:
                role, key, _ = storage.split_object(obj)
                self.assertEqual(
                    store.get(role, key)[0],
                    (work / "candidate/files" / key).read_bytes(),
                )
            self.assertIsNone(store.get("data", "metadata.json"))
            self.assertEqual(
                {path.suffix for path in (work / "candidate/files").iterdir()},
                {".pdf", ".jpg", ".webp"},
            )
            items = pipeline.read_metadata(repo / "metadata")
            self.assertEqual(len(items), 2)
            self.assertIn(ordinary, items)
            self.run["head_sha"] = "revision-2"
            self.published_output(store, items)
            with (
                patch.object(sync, "gh", side_effect=self.github),
                patch.dict(os.environ, {"BYRDOCS_SITE_URL": "https://byrdocs.org"}),
            ):
                pipeline.confirm_committed(store, repo, "revision-2")
            self.assertEqual(len(json.loads(store.get("data", "metadata.json")[0])), 2)
            self.assertEqual(
                storage.load_state(store)["published_revision"], "revision-2"
            )

    def test_official_workflow_is_independent_of_bump_confirmation(self):
        # Long-lived workflow boundary: BUMP processing stays outside normal publication.
        workflow = sync.YAML_CODEC.load(
            (ROOT / ".github/workflows/upload-metadata.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["update"]["steps"]
        commands = [step["run"] for step in steps if "run" in step]
        self.assertEqual(commands[-2:], ["./check-format", "./upload-metadata"])
        self.assertNotIn("scripts/bump", str(steps))
        artifact = steps[-1]
        self.assertEqual(artifact["uses"], "actions/upload-artifact@v4")
        self.assertTrue(artifact["continue-on-error"])
        self.assertIn("github.run_attempt", artifact["with"]["name"])
        self.assertNotIn("if", artifact)

    def test_missing_batch_stops_cleanup_without_downloading_or_rebuilding(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            sync.write_json(repo / sync.MANIFEST, {"version": "1.5.0", "files": []})
            store = MemoryStore()
            with (
                patch.object(sync, "download", side_effect=AssertionError("download")),
                patch.object(pipeline, "build", side_effect=AssertionError("rebuild")),
                self.assertRaisesRegex(ValueError, "批次缺失"),
            ):
                pipeline.confirm_committed(store, repo, "revision")
            self.assertFalse(store.objects)

    def test_cleanup_does_not_modify_storage_after_failed_publication(self):
        store = MemoryStore()
        state = storage.load_state(store)
        state["owned"] = storage.object_names(["a" * 32])
        storage.save_state(store, state)
        self.run["conclusion"] = "failure"
        snapshot = copy.deepcopy(store.objects)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(pipeline, "require_current_default", return_value="revision"),
            patch.object(pipeline, "read_metadata", return_value=[{"id": "a" * 32}]),
            patch.object(sync, "gh", side_effect=self.github),
            self.assertRaisesRegex(ValueError, "Upload metadata"),
        ):
            pipeline.maintenance(store, Path(directory))
        self.assertEqual(store.objects, snapshot)
        self.assertFalse(store.deleted)


class PublicConsumerTests(unittest.TestCase):
    """Long-lived cleanup gates at public data, authenticated download and search boundaries."""

    def setUp(self):
        self.store = MemoryStore()
        self.raw = b"original paper bytes"
        self.digest = hashlib.md5(self.raw).hexdigest()
        self.store.put("files", self.digest + ".pdf", self.raw, "application/pdf")
        for extension in ("jpg", "webp"):
            self.store.put(
                "files",
                f"{self.digest}.{extension}",
                cover_bytes(extension),
                storage.TYPES[extension],
            )
        self.old = "f" * 32
        self.items = [
            {"id": self.digest, "url": f"https://byrdocs.org/files/{self.digest}.pdf"}
        ]
        state = storage.load_state(self.store)
        state["batches"]["release"] = {
            "ids": [self.digest],
            "ready": True,
            "public_files_verified": False,
        }
        state.update(
            published="release",
            published_revision="revision",
            owned=storage.object_names([self.digest, self.old]),
        )
        storage.save_state(self.store, state)
        for prefix in ("", "_bump/confirmed/"):
            self.store.put(
                "data",
                prefix + "metadata.json",
                json.dumps(self.items).encode(),
                "application/json",
            )
            self.store.put(
                "data", prefix + "sitemap.xml", b"sitemap", "application/xml"
            )
        self.time = datetime.now(UTC)
        self.stale = False
        self.corrupt = False

    def fetch(self, url, payload=None, *, authenticated=False):
        if url in publisher.PUBLIC_METADATA:
            return json.dumps(self.items).encode()
        if url == publisher.PUBLIC_SITEMAP:
            return b"sitemap"
        if url == publisher.SEARCH_URL:
            self.assertIn(self.old, payload["jmespath"])
            ids = [self.old] if self.stale else [self.digest]
            return json.dumps({"total": len(ids), "results": ids}).encode()
        self.assertTrue(authenticated)
        return b"wrong file" if self.corrupt else self.raw

    def test_public_data_downloads_cache_wait_and_search_all_gate_deletion(self):
        with patch.object(publisher, "fetch", side_effect=self.fetch):
            self.assertFalse(publisher.public_ready(self.store, self.time))
            self.assertFalse(
                publisher.public_ready(self.store, self.time + timedelta(seconds=299))
            )
            self.stale = True
            with self.assertRaisesRegex(ValueError, "Search"):
                publisher.public_ready(self.store, self.time + timedelta(seconds=301))
            self.stale = False
            self.assertTrue(
                publisher.public_ready(self.store, self.time + timedelta(seconds=301))
            )

    def test_corrupt_download_and_stale_public_metadata_do_not_start_cache_wait(self):
        self.corrupt = True
        with (
            patch.object(publisher, "fetch", side_effect=self.fetch),
            self.assertRaisesRegex(ValueError, "MD5"),
        ):
            publisher.public_ready(self.store, self.time)
        self.assertIsNone(storage.load_state(self.store)["public_verified_at"])

        with (
            patch.object(publisher, "fetch", return_value=b"[]"),
            self.assertRaisesRegex(ValueError, "Public metadata"),
        ):
            publisher.public_ready(self.store, self.time)
        self.assertIsNone(storage.load_state(self.store)["public_verified_at"])

    def test_ordinary_publication_rechecks_metadata_without_redownloading_verified_batch(
        self,
    ):
        # Long-lived publisher regression: file verification belongs to the immutable batch.
        with patch.object(publisher, "fetch", side_effect=self.fetch):
            self.assertFalse(publisher.public_ready(self.store, self.time))

        def publish_roots():
            roots = {
                "metadata.json": (json.dumps(self.items).encode(), "application/json"),
                "sitemap.xml": (b"sitemap", "application/xml"),
            }
            for key, value in roots.items():
                self.store.put("data", key, *value)
            return roots

        self.items[0]["data"] = {"college": ["计算机学院"]}
        storage.confirm_publication(
            self.store, "release", "ordinary-edit", "2-1", publish_roots()
        )
        with patch.object(publisher, "fetch", side_effect=self.fetch) as fetch:
            self.assertFalse(
                publisher.public_ready(self.store, self.time + timedelta(seconds=600))
            )
            self.assertFalse(
                any("/files/" in call.args[0] for call in fetch.call_args_list)
            )
            self.assertTrue(
                publisher.public_ready(self.store, self.time + timedelta(seconds=901))
            )

        state = storage.load_state(self.store)
        state["batches"]["new-release"] = {
            "ids": [self.digest],
            "ready": True,
            "public_files_verified": False,
        }
        storage.save_state(self.store, state)
        storage.confirm_publication(
            self.store, "new-release", "new-release-commit", "3-1", publish_roots()
        )
        with patch.object(publisher, "fetch", side_effect=self.fetch) as fetch:
            self.assertFalse(
                publisher.public_ready(self.store, self.time + timedelta(seconds=1200))
            )
            self.assertEqual(
                sum("/files/" in call.args[0] for call in fetch.call_args_list), 1
            )

    def test_authenticated_download_rejects_redirect_without_forwarding_token(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("X-Byrdocs-Token")))
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.dict(os.environ, {"BYRDOCS_SITE_TOKEN": "test-token"}),
                self.assertRaises(urllib.error.HTTPError),
            ):
                publisher.fetch(
                    f"http://127.0.0.1:{server.server_port}/download",
                    authenticated=True,
                )
            self.assertEqual(seen, [("/download", "test-token")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_rate_limited_download_retries_then_succeeds(self):
        attempts = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                attempts.append(self.path)
                if len(attempts) < 3:
                    self.send_response(429)
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    return
                body = b"payload"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(publisher.time, "sleep") as sleep:
                body = publisher.fetch(f"http://127.0.0.1:{server.server_port}/r")
            self.assertEqual(body, b"payload")
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_count, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def production_env():
    return {
        "GITHUB_REPOSITORY": sync.REPOSITORY,
        "R2_ACCOUNT_ID": "a" * 32,
        "R2_ACCESS_KEY_ID": "primary",
        "R2_SECRET_ACCESS_KEY": "primary-secret",
        "R2_FILE_BUCKET": "archive-files",
        "R2_DATA_BUCKET": "archive-data",
        "BACKUP_ENDPOINT": "https://backup.example",
        "BACKUP_ENDPOINT_REGION": "us-east-1",
        "BACKUP_ACCESS_KEY_ID": "backup",
        "BACKUP_SECRET_ACCESS_KEY": "backup-secret",
        "BACKUP_FILE_BUCKET": "archive-backup",
    }


if __name__ == "__main__":
    unittest.main()
