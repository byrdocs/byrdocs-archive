"""Production object ownership, publication confirmation and resumable cleanup."""

import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlparse

import boto3
import sync
from botocore.config import Config
from botocore.exceptions import ClientError
from PIL import Image

STATE_KEY = "_bump/state.json"
STATE_BACKUP_KEY = "_bump/state.backup.json"
TYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "webp": "image/webp"}


def now():
    return datetime.now(UTC).isoformat()


def settings(env):
    """Use the same explicit storage configuration as the archive publisher."""
    sync.assert_repository(env["GITHUB_REPOSITORY"])
    names = (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_FILE_BUCKET",
        "R2_DATA_BUCKET",
        "BACKUP_ENDPOINT",
        "BACKUP_ENDPOINT_REGION",
        "BACKUP_ACCESS_KEY_ID",
        "BACKUP_SECRET_ACCESS_KEY",
        "BACKUP_FILE_BUCKET",
    )
    for name in names:
        if not env[name].strip():
            raise ValueError(f"Missing configuration: {name}")
    account = env["R2_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", account):
        raise ValueError("Invalid R2 account ID")
    endpoint = f"https://{account}.r2.cloudflarestorage.com"
    backup = urlparse(env["BACKUP_ENDPOINT"])
    if (
        backup.scheme != "https"
        or not backup.hostname
        or backup.username
        or backup.password
    ):
        raise ValueError("Backup endpoint must be an HTTPS service URL")
    if env["R2_FILE_BUCKET"] == env["R2_DATA_BUCKET"]:
        raise ValueError("File and data buckets must be distinct")
    if env["BACKUP_ENDPOINT"].rstrip("/") == endpoint and env["BACKUP_FILE_BUCKET"] in {
        env["R2_FILE_BUCKET"],
        env["R2_DATA_BUCKET"],
    }:
        raise ValueError("Backup must be separate from the primary buckets")
    return {
        "endpoint": endpoint,
        "access_key": env["R2_ACCESS_KEY_ID"],
        "secret_key": env["R2_SECRET_ACCESS_KEY"],
        "backup_endpoint": env["BACKUP_ENDPOINT"],
        "backup_region": env["BACKUP_ENDPOINT_REGION"],
        "backup_access_key": env["BACKUP_ACCESS_KEY_ID"],
        "backup_secret_key": env["BACKUP_SECRET_ACCESS_KEY"],
        "buckets": {
            "files": env["R2_FILE_BUCKET"],
            "data": env["R2_DATA_BUCKET"],
            "backup": env["BACKUP_FILE_BUCKET"],
        },
    }


class R2:
    """Access the configured primary R2 and independent S3 backup storage."""

    def __init__(self, config):
        self.config = config
        options = {
            "config": Config(
                retries={"mode": "standard", "max_attempts": 3},
                connect_timeout=15,
                read_timeout=120,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        }
        self.clients = {
            "main": boto3.client(
                "s3",
                endpoint_url=config["endpoint"],
                region_name="auto",
                aws_access_key_id=config["access_key"],
                aws_secret_access_key=config["secret_key"],
                **options,
            ),
            "backup": boto3.client(
                "s3",
                endpoint_url=config["backup_endpoint"],
                region_name=config["backup_region"],
                aws_access_key_id=config["backup_access_key"],
                aws_secret_access_key=config["backup_secret_key"],
                **options,
            ),
        }

    def client(self, role):
        return self.clients["backup" if role == "backup" else "main"]

    def get(self, role, key):
        try:
            response = self.client(role).get_object(
                Bucket=self.config["buckets"][role], Key=key
            )
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        with response["Body"] as body:
            return body.read(), response["ContentType"]

    def put(self, role, key, body, content_type):
        self.client(role).put_object(
            Bucket=self.config["buckets"][role],
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    def delete(self, role, key):
        self.client(role).delete_object(Bucket=self.config["buckets"][role], Key=key)


def load_state(store):
    value = store.get("data", STATE_KEY)
    if value is None:
        if store.get("data", STATE_BACKUP_KEY) is not None:
            raise ValueError(
                f"主账本 {STATE_KEY} 缺失，但恢复副本 {STATE_BACKUP_KEY} 存在；"
                "请暂停大邮任务，核对并手动恢复主账本后重试。不会初始化空账本或自动恢复删除。"
            )
        return {
            "batches": {},
            "owned": [],
            "published": None,
            "published_revision": None,
            "published_run": None,
            "public_verified_at": None,
            "confirming": None,
            "pending_delete": [],
        }
    state = json.loads(value[0])
    # Cloud state is the ownership boundary for deletions. Reject arbitrary keys.
    for field in ("owned", "pending_delete"):
        for obj in state[field]:
            split_object(obj)
    for batch in state["batches"].values():
        if not batch["ids"] or any(
            not re.fullmatch(sync.MD5, digest) for digest in batch["ids"]
        ):
            raise ValueError("Invalid batch IDs in cloud state")
    if state["published"] is not None and state["published"] not in state["batches"]:
        raise ValueError("Publication refers to an absent batch")
    return state


def save_state(store, state):
    body = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
    # The primary remains authoritative if updating the recovery copy fails.
    put_verified(store, "data", STATE_KEY, body, "application/json")
    put_verified(store, "data", STATE_BACKUP_KEY, body, "application/json")


def put_verified(store, role, key, body, content_type):
    store.put(role, key, body, content_type)
    if not matches_object(store.get(role, key), (body, content_type)):
        raise ValueError(f"Read-back mismatch: {role}/{key}")


def media_type(content_type):
    return content_type.split(";", 1)[0].strip().lower()


def matches_object(actual, expected):
    """Compare exact bytes and media types, allowing Content-Type parameters."""
    return (
        actual is not None
        and actual[0] == expected[0]
        and media_type(actual[1]) == media_type(expected[1])
    )


def split_object(obj):
    match = re.fullmatch(rf"(files|backup)/({sync.MD5})\.(pdf|jpg|webp)", obj)
    if not match or (match[1] == "backup" and match[3] != "pdf"):
        raise ValueError(f"Invalid managed object: {obj}")
    return match[1], obj.split("/", 1)[1], match[2]


def object_names(ids):
    return [
        f"{role}/{digest}.{ext}"
        for digest in sorted(ids)
        for role, ext in (
            ("files", "pdf"),
            ("files", "jpg"),
            ("files", "webp"),
            ("backup", "pdf"),
        )
    ]


def verify_cover(key, value):
    """Validate cover format and content type for local uploads and stored objects."""
    if value is None:
        raise ValueError(f"Missing cover: {key}")
    body, content_type = value
    extension = key.rsplit(".", 1)[1]
    with Image.open(io.BytesIO(body)) as image:
        if (
            image.format != {"jpg": "JPEG", "webp": "WEBP"}[extension]
            or media_type(content_type) != TYPES[extension]
        ):
            raise ValueError(f"Invalid cover: {key}")
        image.load()


def stage(store, label, manifest, metadata, files, branch=None):
    """Journal and upload local PDFs, covers and original-file backups before a PR."""
    ids = sorted({entry["md5"] for entry in manifest["files"]})
    if not ids or any(not re.fullmatch(sync.MD5, digest) for digest in ids):
        raise ValueError("Empty or invalid batch")
    if sorted(item["id"] for item in metadata) != ids:
        raise ValueError("Metadata and batch IDs differ")
    for digest in ids:
        for extension in ("jpg", "webp"):
            key = f"{digest}.{extension}"
            verify_cover(key, ((files / key).read_bytes(), TYPES[extension]))
    state = load_state(store)
    if label in state["batches"]:
        batch = state["batches"][label]
        if batch["manifest"] != manifest or batch["metadata"] != metadata:
            raise ValueError("The same batch changed; refusing an implicit replacement")
    else:
        state["batches"][label] = {
            "manifest": manifest,
            "metadata": metadata,
            "ids": ids,
            "branch": branch,
            "ready": False,
            "public_files_verified": False,
        }
    # A retry reuses identical content but starts a fresh orphan-retention window,
    # even if inspection or upload fails before a new PR can be created.
    state["batches"][label]["attempted_at"] = now()
    save_state(store, state)

    def inspect(obj):
        role, key, _ = split_object(obj)
        existing = store.get(role, key)
        if existing is not None:
            body, content_type = existing
            extension = key.rsplit(".", 1)[1]
            if extension == "pdf":
                if body != (files / key).read_bytes():
                    raise ValueError(f"Existing object differs: {obj}")
                if media_type(content_type) not in {
                    TYPES[extension],
                    "application/octet-stream",
                }:
                    raise ValueError(f"Invalid original content type: {obj}")
            else:
                verify_cover(key, existing)
            return None
        return obj

    def upload(obj):
        role, key, _ = split_object(obj)
        put_verified(
            store, role, key, (files / key).read_bytes(), TYPES[key.rsplit(".", 1)[1]]
        )

    # Journal ownership before any object write; the workflow lock excludes publication.
    with ThreadPoolExecutor(max_workers=4) as pool:
        missing = [
            obj for obj in pool.map(inspect, object_names(ids)) if obj is not None
        ]
    for obj in missing:
        if obj not in state["owned"]:
            state["owned"].append(obj)
    if missing:
        state["batches"][label]["ready"] = False
        if any(obj.startswith("files/") and obj.endswith(".pdf") for obj in missing):
            state["batches"][label]["public_files_verified"] = False
        save_state(store, state)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for number, _ in enumerate(pool.map(upload, missing), 1):
                if number % 40 == 0 or number == len(missing):
                    print(
                        f"{label}: verified {number}/{len(missing)} uploaded objects",
                        flush=True,
                    )
    state["batches"][label]["ready"] = True
    save_state(store, state)
    return label


def confirm_publication(store, label, revision, attempt, expected):
    """Record read-back confirmation without publishing or modifying root objects."""
    state = load_state(store)
    if label is not None and not state["batches"][label]["ready"]:
        raise ValueError("Batch is incomplete")
    # Confirmation is resumable even when only one snapshot/state write succeeds.
    state["confirming"] = attempt
    state["public_verified_at"] = None
    save_state(store, state)
    if label is not None:

        def check_cover(key):
            verify_cover(key, store.get("files", key))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    check_cover,
                    (
                        f"{digest}.{ext}"
                        for digest in state["batches"][label]["ids"]
                        for ext in ("jpg", "webp")
                    ),
                )
            )
    for key, (body, content_type) in expected.items():
        if not matches_object(store.get("data", key), (body, content_type)):
            raise ValueError(f"Publication read-back mismatch: {key}")
    if set(expected) != {"metadata.json", "sitemap.xml"}:
        raise ValueError("Publisher must confirm metadata.json and sitemap.xml")
    for key, (body, content_type) in expected.items():
        put_verified(store, "data", "_bump/confirmed/" + key, body, content_type)
    state["published"] = label
    state["published_revision"] = revision
    state["published_run"] = attempt
    state["confirming"] = None
    save_state(store, state)


def verify_publication(store):
    for key in ("metadata.json", "sitemap.xml"):
        confirmed = store.get("data", "_bump/confirmed/" + key)
        if confirmed is None or not matches_object(store.get("data", key), confirmed):
            raise ValueError(f"Published object changed or is missing: {key}")


def metadata_refs(items):
    refs = set()
    for item in items:
        if not re.fullmatch(sync.MD5, item["id"]):
            raise ValueError("Invalid metadata ID")
        refs.add(item["id"])
        match = re.fullmatch(
            rf"https://byrdocs\.org/files/({sync.MD5})\.(pdf|zip)", item["url"]
        )
        if not match:
            raise ValueError("Invalid metadata URL")
        refs.add(match[1])
    return refs


def cleanup(
    store, references, open_branches, closed_branches, timestamp=None, *, before_delete
):
    """Delete only journaled, unreferenced objects; keep interrupted work durable."""
    state = load_state(store)
    if state["confirming"] is not None:
        raise ValueError("Publication confirmation incomplete; cleanup prohibited")
    if state["published_revision"] is not None:
        verify_publication(store)
    timestamp = datetime.now(UTC) if timestamp is None else timestamp
    protected = set(references)
    published = store.get("data", "metadata.json")
    if published is not None:
        protected |= metadata_refs(json.loads(published[0]))
    keep = set()
    for label, batch in state["batches"].items():
        # Older ledgers predate per-attempt timestamps. Only an absent new field
        # uses the legacy creation time; malformed new values must still fail.
        attempted = datetime.fromisoformat(
            batch["attempted_at"] if "attempted_at" in batch else batch["created_at"]
        )
        age = (timestamp - attempted).total_seconds()
        closed_at = closed_branches.get(batch["branch"])
        closed = (
            closed_at is not None and datetime.fromisoformat(closed_at) >= attempted
        )
        if (
            label == state["published"]
            or batch["branch"] in open_branches
            or (not closed and age < 86400)
        ):
            protected.update(batch["ids"])
            keep.add(label)
    candidates = {
        obj for obj in state["owned"] if split_object(obj)[2] not in protected
    }
    # Recompute after every restart, so new references cancel prior delete candidates.
    state["pending_delete"] = sorted(
        candidates, key=lambda obj: (split_object(obj)[2], obj)
    )
    save_state(store, state)
    checked = set()
    try:
        for obj in list(state["pending_delete"]):
            role, key, digest = split_object(obj)
            if digest not in checked:
                before_delete()
                checked.add(digest)
            store.delete(role, key)
            if store.get(role, key) is not None:
                raise ValueError(f"Deletion not confirmed: {obj}")
            state["owned"].remove(obj)
            state["pending_delete"].remove(obj)
        state["batches"] = {
            label: batch for label, batch in state["batches"].items() if label in keep
        }
    finally:
        # Save progress once on completion or failure. A hard stop may replay
        # idempotent deletes, but references are recomputed before any retry.
        save_state(store, state)
    return len(candidates)


def connect():
    return R2(settings(os.environ))
