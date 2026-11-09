"""Upstream BUMP upload and reference-aware post-publication cleanup."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import publisher
import storage
import sync


def read_metadata(directory):
    return sorted(
        [
            sync.YAML_CODEC.load(path.read_text(encoding="utf-8"))
            for path in directory.glob("*.y*ml")
        ],
        key=lambda item: item["id"],
    )


def require_current_default(repo):
    """Reject a superseded checkout before publication or deletion."""
    checkout = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    current = json.loads(
        sync.gh("api", f"repos/{sync.REPOSITORY}/git/ref/heads/master")
    )["object"]["sha"]
    if checkout != current:
        raise ValueError(
            "Default branch changed during this run; rerun on current merged data"
        )
    return checkout


def pull_requests(state):
    pages = json.loads(
        sync.gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{sync.REPOSITORY}/pulls?state={state}&per_page=100",
        )
    )
    return [pr for page in pages for pr in page]


def managed_pr(pr):
    return (
        pr["head"]["repo"] is not None
        and pr["head"]["repo"]["full_name"] == sync.REPOSITORY
        and pr["head"]["ref"].startswith(sync.BRANCH_PREFIX)
    )


def pr_references(prs):
    """Protect YAML references from all open PRs, including ordinary contributions."""
    refs, opened, closed = set(), set(), {}
    for pr in prs:
        if managed_pr(pr):
            branch = pr["head"]["ref"]
            if pr["state"] == "open":
                opened.add(branch)
            elif branch not in closed or pr["closed_at"] > closed[branch]:
                closed[branch] = pr["closed_at"]
        if pr["state"] != "open":
            continue
        details = json.loads(
            sync.gh("api", f"repos/{sync.REPOSITORY}/pulls/{pr['number']}")
        )
        if details["changed_files"] > 3000:
            raise ValueError("PR file list exceeds GitHub's limit; cleanup blocked")
        pages = json.loads(
            sync.gh(
                "api",
                "--paginate",
                "--slurp",
                f"repos/{sync.REPOSITORY}/pulls/{pr['number']}/files?per_page=100",
            )
        )
        files = [item for page in pages for item in page]
        if (
            len(files) != details["changed_files"]
            or details["head"]["sha"] != pr["head"]["sha"]
        ):
            raise ValueError("PR changed during reference collection; retry cleanup")
        for item in files:
            if item["status"] == "removed" or not re.fullmatch(
                r"metadata/[^/]+\.ya?ml", item["filename"]
            ):
                continue
            repository = details["head"]["repo"]["full_name"]
            blob = json.loads(
                sync.gh("api", f"repos/{repository}/git/blobs/{item['sha']}")
            )
            contents = base64.b64decode(blob["content"]).decode("utf-8")
            # PRs are unfinished input: conservatively protect every mentioned ID,
            # even in invalid YAML/URLs, without running the repository schema gate.
            refs.update(
                digest.lower()
                for digest in re.findall(
                    r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])",
                    item["filename"] + "\n" + contents,
                    re.IGNORECASE,
                )
            )
    return refs, opened, closed


def maintenance(store, repo):
    state = storage.load_state(store)
    if not state["owned"]:
        print("没有受管理的存储对象需要清理")
        return
    revision = require_current_default(repo)
    if not confirm_committed(store, repo, revision):
        return
    require_current_default(repo)
    if not publisher.public_ready(store):
        return
    prs = pull_requests("all")
    refs, opened, closed = pr_references(prs)
    open_heads = {
        pr["number"]: pr["head"]["sha"] for pr in prs if pr["state"] == "open"
    }

    def before_delete():
        # Recheck once per paper (its four objects are adjacent), keeping GitHub
        # requests bounded while long cleanups notice merges and edited/new PRs.
        require_current_default(repo)
        current = {pr["number"]: pr["head"]["sha"] for pr in pull_requests("open")}
        if current != open_heads:
            raise ValueError("Open PR references changed during cleanup; retry")

    require_current_default(repo)
    refs |= storage.metadata_refs(read_metadata(repo / "metadata"))
    count = storage.cleanup(store, refs, opened, closed, before_delete=before_delete)
    print(f"清理完成：{count} 个无引用的受管理对象")


def validate_build(out):
    manifest = sync.read_json(out / "manifest.json")
    ids = {entry["md5"] for entry in manifest["files"]}
    items = read_metadata(out / "metadata")
    if not ids or ids != {item["id"] for item in items}:
        raise ValueError("Build metadata does not match manifest")
    for item in items:
        if (
            hashlib.md5((out / "files" / f"{item['id']}.pdf").read_bytes()).hexdigest()
            != item["id"]
        ):
            raise ValueError("PDF MD5 differs from the manifest")
    return manifest, items


def build(repo, out, version):
    sync.build(
        SimpleNamespace(
            repo=repo,
            output=out,
            version=version,
            source_pdf=None,
            source_metadata=None,
            apply=True,
        )
    )
    manifest, items = validate_build(out)
    # Preserve non-BUMP metadata for an existing file with the same MD5.
    items = [
        sync.YAML_CODEC.load(
            (repo / "metadata" / f"{item['id']}.yml").read_text(encoding="utf-8")
        )
        for item in items
    ]
    return manifest, items


def stage_release(store, manifest, items, files):
    """Keep rebuilds separate until their replacement PR has been published."""
    batches = storage.load_state(store)["batches"]
    base = "release-" + manifest["version"]
    for label, batch in batches.items():
        if batch["manifest"] == manifest and batch["metadata"] == items:
            break
    else:
        label, revision = base, 1
        while label in batches:
            revision += 1
            label = f"{base}-{revision}"
    return storage.stage(
        store, label, manifest, items, files, sync.BRANCH_PREFIX + manifest["version"]
    )


def confirm_committed(store, repo, revision):
    """Confirm the independent publisher's output; missing uploads need manual recovery."""
    manifest_path = repo / sync.MANIFEST
    label = None
    if manifest_path.exists():
        manifest = sync.read_json(manifest_path)
        batches = storage.load_state(store)["batches"]
        label = next(
            (
                key
                for key, batch in batches.items()
                if batch["ready"] and batch["manifest"] == manifest
            ),
            None,
        )
        if label is None:
            raise ValueError(
                "已合并的大邮批次缺失或未上传完成；停止清理，请手动检查同步任务"
            )
    items = read_metadata(repo / "metadata")
    if not items:
        raise ValueError("Refusing an empty archive confirmation")
    if not publisher.confirm(store, label, revision, items):
        return False
    print(f"已确认全库发布：{len(items)} 条元信息")
    return True


def sync_release(store, repo, work, requested, retry):
    require_current_default(repo)
    manifest_path = repo / sync.MANIFEST
    committed = sync.read_json(manifest_path) if manifest_path.exists() else None
    prs = [pr for pr in pull_requests("all") if managed_pr(pr)]
    if requested:
        version = requested
    else:
        request = urllib.request.Request(
            sync.PUB + "/rss", headers={"User-Agent": "Mozilla/5.0 BYRDocs-Bump-Sync"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            version = sync.release_from_feed(response.read())
    if not re.fullmatch(sync.VERSION, version):
        raise ValueError("Invalid formal version")
    should_build, reason = sync.should_build(version, committed, prs, retry)
    print(reason)
    if not should_build:
        return
    out = work / "candidate"
    manifest, items = build(repo, out, version)
    require_current_default(repo)
    stage_release(store, manifest, items, out / "files")
    require_current_default(repo)
    with (out / "pr-body.md").open("a", encoding="utf-8") as stream:
        stream.write(
            "\n## 存储结果\n\nPDF、本地生成的 JPG/WebP 封面及原文件备份"
            "已上传正式存储并读回校验，已有对象已核对并复用。"
            "合并后发布当前仓库的完整元信息并检查封面完整性；"
            "公开文件与搜索更新确认后才清理无引用的受管理旧对象。\n"
        )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("create_pr=true\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sync", "cleanup"))
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    sync.assert_origin(args.repo)
    store = storage.connect()
    if args.action == "cleanup":
        maintenance(store, args.repo)
    else:
        sync_release(store, args.repo, args.output, args.version, args.retry)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
