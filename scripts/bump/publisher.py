"""Confirm an independent official publication and verify public consumers."""

import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import storage
import sync

# The deployed search service refreshes its in-memory index after five minutes.
SEARCH_CACHE_SECONDS = 300
PUBLIC_METADATA = (
    "https://data.byrdocs.org/metadata.json",
    "https://byrdocs.org/data/metadata.json",
)
PUBLIC_SITEMAP = "https://byrdocs.org/sitemap.xml"
SEARCH_URL = "https://search.byrdocs.org/api/search"


def confirm(store, label, revision, items):
    """Accept output only from a successful current-master publication attempt."""
    runs = json.loads(
        sync.gh(
            "api",
            f"repos/{sync.REPOSITORY}/actions/workflows/upload-metadata.yml/runs"
            "?branch=master&per_page=1",
        )
    )["workflow_runs"]
    if not runs:
        raise ValueError("没有发布运行记录；请运行 Upload metadata")
    run = runs[0]
    # Do not fall back to an older success after a failed or in-progress publication.
    if (
        run["head_sha"] != revision
        or run["head_repository"]["full_name"] != sync.REPOSITORY
        or run["event"] not in {"push", "workflow_dispatch"}
    ):
        raise ValueError(
            "当前默认分支的最新发布未成功；请重跑 Upload metadata 后再清理"
        )
    if run["status"] != "completed":
        print("Upload metadata 尚未完成，本次跳过清理；发布成功后会再次触发")
        return False
    if run["conclusion"] != "success":
        raise ValueError("最新 Upload metadata 运行失败或取消；请重跑发布后再清理")
    attempt = f"{run['id']}-{run['run_attempt']}"
    state = storage.load_state(store)
    if (
        state["published"] == label
        and state["published_revision"] == revision
        and state["published_run"] == attempt
        and state["confirming"] is None
    ):
        # Keep confirmed bytes beyond artifact retention, but still check run status.
        storage.verify_publication(store)
        return True
    with tempfile.TemporaryDirectory(prefix="publication-") as directory:
        try:
            sync.gh(
                "run",
                "download",
                str(run["id"]),
                "--repo",
                sync.REPOSITORY,
                "--name",
                f"bump-publication-{attempt}",
                "--dir",
                directory,
            )
            expected = {
                name: ((Path(directory) / name).read_bytes(), kind)
                for name, kind in (
                    ("metadata.json", "application/json"),
                    ("sitemap.xml", "application/xml"),
                )
            }
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            raise ValueError(
                f"无法读取发布产物 bump-publication-{attempt}（仓库 {sync.REPOSITORY}）。"
                "请检查该次 Upload metadata 的 artifact 上传步骤、产物保留期及 Actions 读取权限；"
                "产物缺失或不完整时，在当前 master 手动运行 Upload metadata 后再清理。"
            ) from error
    raw = expected["metadata.json"][0]
    actual = json.loads(raw)
    if {item["id"] for item in actual} != {item["id"] for item in items} or len(
        actual
    ) != len(items):
        raise ValueError("Publisher changed the full archive's entry set")
    if any(
        not isinstance(item["data"].get("filesize"), int)
        or item["data"]["filesize"] <= 0
        for item in actual
    ):
        raise ValueError("Published metadata contains a missing or empty original file")
    sitemap = expected["sitemap.xml"][0]
    locations = [
        node.text
        for node in ET.fromstring(sitemap).iter(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
        )
    ]
    site = os.environ["BYRDOCS_SITE_URL"].rstrip("/")
    if set(locations) != {
        site,
        site + "/about",
        *[f"{site}/?q={item['id']}" for item in items],
    }:
        raise ValueError("Sitemap does not cover the full archive")
    storage.confirm_publication(store, label, revision, attempt, expected)
    return True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the site's download credential to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, payload=None, *, authenticated=False):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "BYRDocs-Bump-Sync", "Content-Type": "application/json"},
    )
    if authenticated:
        request.add_header("X-Byrdocs-Token", os.environ["BYRDOCS_SITE_TOKEN"])
    with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
        return response.read()


def public_ready(store, timestamp=None):
    """Block cleanup until public data, downloads and the search index agree."""
    state = storage.load_state(store)
    if state["confirming"] is not None or state["published_revision"] is None:
        raise ValueError("A confirmed full publication is required before cleanup")
    storage.verify_publication(store)
    expected = json.loads(store.get("data", "_bump/confirmed/metadata.json")[0])
    sitemap = store.get("data", "_bump/confirmed/sitemap.xml")[0]
    for url in PUBLIC_METADATA:
        if json.loads(fetch(url)) != expected:
            raise ValueError(
                "Public metadata has not reached the confirmed publication"
            )
    if fetch(PUBLIC_SITEMAP) != sitemap:
        raise ValueError("Public sitemap has not reached the confirmed publication")

    label = state["published"]
    batch = state["batches"][label] if label is not None else None
    if batch is not None and not batch["public_files_verified"]:

        def verify_file(digest):
            raw = fetch(
                f"https://byrdocs.org/files/{digest}.pdf?f=3", authenticated=True
            )
            if hashlib.md5(raw).hexdigest() != digest:
                raise ValueError(f"Public PDF differs from its MD5: {digest}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(verify_file, batch["ids"]))
        batch["public_files_verified"] = True
        storage.save_state(store, state)

    timestamp = datetime.now(UTC) if timestamp is None else timestamp
    if state["public_verified_at"] is None:
        state["public_verified_at"] = timestamp.isoformat()
        storage.save_state(store, state)
        print("本次公开元信息已确认；搜索缓存等待期结束后再运行清理")
        return False

    elapsed = (
        timestamp - datetime.fromisoformat(state["public_verified_at"])
    ).total_seconds()
    if elapsed < SEARCH_CACHE_SECONDS:
        print(
            f"等待搜索缓存更新，剩余 {int(SEARCH_CACHE_SECONDS - elapsed)} 秒；本次不删除"
        )
        return False
    # Check new and retired IDs together; stale fallback results must not permit deletion.
    managed = sorted(
        {storage.split_object(obj)[2] for obj in state["owned"]}
        | {digest for batch in state["batches"].values() for digest in batch["ids"]}
    )
    published_ids = {item["id"] for item in expected}
    for offset in range(0, len(managed), 100):
        chunk = managed[offset : offset + 100]
        expression = "[?contains(`" + json.dumps(chunk) + "`, id)].id"
        result = json.loads(
            fetch(SEARCH_URL, {"jmespath": expression, "limit": 100, "shorten": False})
        )
        wanted = published_ids & set(chunk)
        if set(result["results"]) != wanted or result["total"] != len(wanted):
            raise ValueError(
                "Search still differs from the confirmed metadata; cleanup blocked"
            )
    return True
