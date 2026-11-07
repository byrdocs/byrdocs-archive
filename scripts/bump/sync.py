"""Build BUMP single-paper artifacts and upstream metadata PRs."""

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import covers
import jsonschema
from pypdf import PdfReader, PdfWriter
from ruamel.yaml import YAML

REPOSITORY = "byrdocs/byrdocs-archive"
PUB = "https://pub.fa-555.com"
MANIFEST = Path("sources/bump.json")
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
MD5 = r"[0-9a-f]{32}"
BRANCH_PREFIX = "automation/bump-"
COURSES = (
    "工科数学分析",
    "数学分析",
    "高等数学",
    "高等代数",
    "线性代数",
    "复变函数",
    "数学物理方法",
    "工程数学",
    "概率论与数理统计",
    "概率论与随机过程",
    "组合数学",
    "运筹学",
    "最优化理论与算法",
    "高级数理逻辑",
    "矩阵分析与应用",
)
COURSE_TAGS = {
    "分析上期中",
    "分析上期末",
    "分析下期中",
    "分析下期末",
    "线代期末",
    "高代上期中",
    "高代下期末",
    "复变期末",
    "数物期末",
    "工数期末",
    "概随期末",
    "概统期末",
    "数数期末",
    "运筹学期末",
    "研概随期末",
    "最优化期末",
    "数理逻辑期末",
    "矩阵论期末",
}
KNOWN_TAGS = COURSE_TAGS | {"研究生课", "无准确标题"}
# BYR Docs Wiki src/utils/examFrontmatter.ts SCHOOLS, with aliases normalized below.
COLLEGES = (
    "人工智能学院",
    "人文学院",
    "信息与通信工程学院",
    "卓越工程师学院",
    "国际学院",
    "数字媒体与设计艺术学院",
    "数学科学学院",
    "智能工程与自动化学院",
    "未来学院",
    "物理科学与技术学院",
    "玛丽女王海南学院",
    "理学院",
    "电子工程学院",
    "经济管理学院",
    "网络空间安全学院",
    "计算机学院（国家示范性软件学院）",
    "集成电路学院",
    "马克思主义学院",
)
COLLEGE_ALIASES = {
    "元班": "未来学院",
    "自动化学院": "智能工程与自动化学院",
    "计算机学院": "计算机学院（国家示范性软件学院）",
}
# Consume full names first: e.g. 智能工程与自动化学院 contains 自动化学院.
COLLEGE_PATTERN = re.compile(
    "|".join(
        re.escape(name)
        for name in sorted((*COLLEGES, *COLLEGE_ALIASES), key=len, reverse=True)
    )
)
YAML_CODEC = YAML(typ="safe")
YAML_CODEC.allow_unicode = True
YAML_CODEC.default_flow_style = False


class SkipPaper(ValueError):
    """A source entry cannot be imported without human interpretation."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def download(url, path):
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 BYRDocs-Bump-Sync"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        path.write_bytes(response.read())


def release_from_feed(xml):
    versions = []
    for item in ET.fromstring(xml).findall("./channel/item"):
        guid = item.findtext("guid", "")
        title = item.findtext("title", "")
        description = item.findtext("description", "")
        if re.search(r"preview|快照|预览", title, re.IGNORECASE):
            continue
        match = re.fullmatch(rf"{re.escape(PUB)}/rss#bump-({VERSION})", guid)
        if not match and "大邮数学集" in title and "正式版" in title:
            match = re.search(
                rf"(?:正式版|Release)\s*({VERSION})",
                title + " " + description,
                re.IGNORECASE,
            )
        if match:
            versions.append(match[1])
    if not versions:
        raise ValueError("RSS 中没有可识别的正式版；请检查上游格式")
    return max(versions, key=lambda value: tuple(map(int, value.split("."))))


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True, encoding="utf-8").strip()


def assert_repository(repository):
    if repository != REPOSITORY:
        raise ValueError(f"同步任务只允许操作 {REPOSITORY}，收到 {repository}")


def assert_origin(repo):
    remote = subprocess.check_output(
        ["git", "-C", str(repo), "remote", "get-url", "origin"], text=True
    ).strip()
    if remote not in {
        f"https://github.com/{REPOSITORY}",
        f"https://github.com/{REPOSITORY}.git",
        f"git@github.com:{REPOSITORY}.git",
    }:
        raise ValueError("origin 必须指向正式 Archive 仓库")


def should_build(version, manifest, prs, retry=False):
    if any(
        p["state"] == "open" and p["head"]["ref"].startswith(BRANCH_PREFIX) for p in prs
    ):
        return False, "已有待审同步 PR"
    if manifest and tuple(map(int, manifest["version"].split("."))) > tuple(
        map(int, version.split("."))
    ):
        return False, "拒绝降级至旧版本"
    if manifest and manifest["version"] == version and not retry:
        return False, "版本已合并"
    if not retry and any(p["head"]["ref"] == BRANCH_PREFIX + version for p in prs):
        return False, "该版本已有已关闭或已合并的 PR；可手动 retry"
    return True, "开始处理正式版 " + version


def parse_time(title):
    text = title.replace("–", "-").replace("—", "-")
    result = {}
    semester = re.search(r"第\s*([一二12])\s*学期", text)
    if semester:
        result["semester"] = "First" if semester[1] in {"一", "1"} else "Second"
    elif "秋" in text:
        result["semester"] = "First"
    elif "春" in text:
        result["semester"] = "Second"
    years = re.search(r"(?<!\d)((?:19|20)\d{2})\s*[-/]\s*((?:19|20)\d{2})(?!\d)", text)
    if years:
        start, end = map(int, years.groups())
    else:
        short = re.search(r"(?<!\d)(\d{2})\s*-\s*(\d{2})\s*学年", text)
        date = re.search(
            r"((?:19|20)\d{2})\s*(?:年\s*|[./])([0-9]{1,2})(?:\s*月|\b)", text
        )
        year = re.search(r"(?:19|20)\d{2}", text)
        if short:
            start, end = (2000 + int(s) for s in short.groups())
        elif date:
            year_number, month = map(int, date.groups())
            if not 1 <= month <= 12:
                raise SkipPaper("月份无效")
            start = year_number - 1 if month <= 7 else year_number
            end = start + 1
            result["semester"] = "First" if month >= 8 or month == 1 else "Second"
        elif year:
            number = int(year[0])
            if "春" in text:
                start, end = number - 1, number
            elif "秋" in text:
                start, end = number, number + 1
            else:
                start = end = number
        else:
            raise SkipPaper("无法解析必填年份")
    if end not in {start, start + 1}:
        raise SkipPaper("学年起止不符合现有格式")
    return {"start": str(start), "end": str(end), **result}


def parse_entry(entry):
    title, tags = entry["title"], set(entry["tags"])
    if tags & {"高考", "附加"}:
        raise SkipPaper("排除：高考、竞赛或外校材料")
    if tags - KNOWN_TAGS:
        raise SkipPaper("未知标签：" + "、".join(sorted(tags - KNOWN_TAGS)))
    if re.search(r"作业|模拟|练习", title):
        raise SkipPaper("收录性质待定：标题不是明确的实际试卷")
    normalized = (
        re.sub(r"\s+", "", title)
        .replace("高数", "高等数学")
        .replace("数分", "数学分析")
    )
    course = next((name for name in COURSES if name in normalized), None)
    if not course:
        raise SkipPaper("无法解析必填课程名称")
    if not tags & COURSE_TAGS and tags:
        raise SkipPaper("缺少可识别的北邮课程分类")
    suffix = normalized.split(course, 1)[1]
    if course == "高等数学" and suffix[:1] in {"A", "B"}:
        course += suffix[0]
        suffix = suffix[1:]
    volume = re.match(r"[》）)]*[(（]?([上下])", suffix)
    if volume:
        course += "（" + volume[1] + "）"
    time = parse_time(title)
    stages = {
        stage
        for stage in ("期中", "期末")
        if stage in title or any(stage in tag for tag in tags)
    }
    if len(stages) > 1:
        raise SkipPaper("标题与标签的考试阶段冲突")
    if stages:
        time["stage"] = stages.pop()
    elif not re.search(r"考试|试卷|试题", title):
        raise SkipPaper("无法确定资料是否为试卷")
    course_data = {"name": course}
    if "研究生课" in tags:
        course_data["type"] = "研究生"
    data = {"course": course_data, "time": time, "filetype": "pdf", "content": ["原题"]}
    colleges = {
        COLLEGE_ALIASES.get(name, name)
        for name in COLLEGE_PATTERN.findall(title.replace("(", "（").replace(")", "）"))
    }
    if colleges:
        data["college"] = sorted(colleges)
    return data


def validate_source(source, reader, version):
    if (
        source["config"]["release-type"] != "release"
        or source["config"]["version-number"] != version
    ):
        raise ValueError("metadata 不是请求版本的正式版")
    entries = source["problemsets"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("源条目必须是非空列表")
    occupied = set()
    for entry in entries:
        if not isinstance(entry["title"], str) or not entry["title"].strip():
            raise ValueError("源条目标题为空")
        if not isinstance(entry["tags"], list) or not all(
            isinstance(t, str) for t in entry["tags"]
        ):
            raise ValueError("源条目 tags 格式错误")
        start, length = entry["page-physical"], entry["length-in-page"]
        if (
            type(start) is not int
            or type(length) is not int
            or start < 1
            or length < 1
            or start + length - 1 > len(reader.pages)
        ):
            raise ValueError(f"页码越界或非法：{entry['title']}")
        pages = set(range(start, start + length))
        if occupied & pages:
            raise ValueError(f"试卷页码重叠：{entry['title']}")
        occupied.update(pages)
    return entries


def strict_schema(schema):
    # The existing Rust checker denies unknown fields; mirror that offline.
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
        for child in schema.values():
            strict_schema(child)
    elif isinstance(schema, list):
        for child in schema:
            strict_schema(child)
    return schema


def yaml_text(value):
    stream = io.StringIO()
    YAML_CODEC.dump(value, stream)
    return stream.getvalue()


def replacement(repo, previous, generated):
    previous_ids = set(previous["managed_metadata"]) if previous else set()
    if any(not re.fullmatch(MD5, value) for value in previous_ids):
        raise ValueError("旧清单包含非法文件 ID")
    managed, reused, changes = [], [], {}
    for digest, value in generated.items():
        existing = repo / "metadata" / f"{digest}.yml"
        if existing.exists() and digest not in previous_ids:
            reused.append(digest)
            continue
        managed.append(digest)
        changes[digest] = yaml_text(value)
    removed = sorted(previous_ids - set(generated))
    # Cleanup is a preview only. Read every remaining metadata reference, not titles.
    references = set(generated)
    removed_names = {f"{digest}.yml" for digest in removed}
    for path in (repo / "metadata").glob("*.y*ml"):
        if path.name in removed_names:
            continue
        item = YAML_CODEC.load(path.read_text(encoding="utf-8"))
        references.add(item["id"])
        match = re.search(r"/files/([0-9a-f]{32})\.", item["url"])
        if match:
            references.add(match[1])
    old_files = {entry["md5"] for entry in previous["files"]} if previous else set()
    if any(not re.fullmatch(MD5, value) for value in old_files):
        raise ValueError("旧清单包含非法对象 ID")
    cleanup = sorted(old_files - references)
    return managed, reused, changes, removed, cleanup


def table_cell(value):
    return (
        str(value)
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build(args):
    if not re.fullmatch(VERSION, args.version):
        raise ValueError("版本必须是 x.y.z")
    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("输出目录必须为空，避免把上次产物混入 artifact")
    out.mkdir(parents=True, exist_ok=True)
    files = out / "files"
    files.mkdir()
    pdf_url = f"{PUB}/files/bump-{args.version}.pdf"
    metadata_url = f"{PUB}/files/bump-{args.version}-metadata.json"
    if args.source_pdf is None:
        args.source_pdf = out / "source.pdf"
        download(pdf_url, args.source_pdf)
    if args.source_metadata is None:
        args.source_metadata = out / "source-metadata.json"
        download(metadata_url, args.source_metadata)
    source = read_json(args.source_metadata)
    reader = PdfReader(args.source_pdf)
    entries = validate_source(source, reader, args.version)
    front_end = min(e["page-physical"] for e in entries) - 1
    license_pages = []
    for page in range(front_end):
        text = reader.pages[page].extract_text()
        if "版权声明与授权许可" in text[:80] and "CC BY-NC-SA 4.0" in text:
            license_pages.append(page)
    if len(license_pages) != 1:
        raise ValueError("无法唯一定位原书许可页，请检查新版结构")
    schema = strict_schema(
        YAML_CODEC.load((args.repo / "schema/test.yaml").read_text(encoding="utf-8"))
    )
    validator = jsonschema.Draft7Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    generated, records, skipped = {}, [], []
    for number, entry in enumerate(entries, 1):
        start, length = entry["page-physical"], entry["length-in-page"]
        try:
            data = parse_entry(entry)
            contents = "\n".join(
                reader.pages[i].extract_text()
                for i in range(start - 1, start + length - 1)
            )
            if re.search(r"(?m)^\s*答案\s*$", contents):
                data["content"].append("答案")
        except SkipPaper as error:
            skipped.append((entry, str(error)))
            continue
        provenance = (
            f"大邮数学集 {args.version}\n{entry['title']}\n"
            f"作者：fa_555 及其他贡献者（见原书）\n源文件：{pdf_url}\n"
            f"元信息：{metadata_url}\n原始物理页：{start}-{start + length - 1}\n"
            "BYR Docs 修改：按原始页码拆分，附加原书许可页及本说明；题目页未改写。\n"
            "许可范围以附带的原书许可页为准，原始试题和第三方材料不适用该 CC 授权。\n"
            "https://creativecommons.org/licenses/by-nc-sa/4.0/\n"
        )
        writer = PdfWriter()
        writer.append(
            reader, pages=(start - 1, start + length - 1), import_outline=False
        )
        writer.append(reader, pages=license_pages, import_outline=False)
        writer.add_metadata(
            {
                "/Title": entry["title"],
                "/Author": "fa_555 及其他贡献者",
                "/Subject": provenance,
            }
        )
        writer.add_attachment("source.txt", provenance.encode("utf-8"))
        writer.compress_identical_objects(
            remove_duplicates=True, remove_unreferenced=True
        )
        buffer = io.BytesIO()
        writer.write(buffer)
        writer.close()
        raw = buffer.getvalue()
        digest = hashlib.md5(raw).hexdigest()
        value = {
            "id": digest,
            "url": f"https://byrdocs.org/files/{digest}.pdf",
            "type": "test",
            "data": data,
        }
        validator.validate(value)
        if len(PdfReader(io.BytesIO(raw)).pages) != length + 1:
            raise ValueError("输出 PDF 页数不符")
        if digest not in generated:
            pdf = files / f"{digest}.pdf"
            pdf.write_bytes(raw)
            covers.generate(pdf)
            generated[digest] = value
        records.append(
            {"ordinal": number, "page": start, "length": length, "md5": digest}
        )
        print(f"[{number}/{len(entries)}] {digest} {entry['title']}", flush=True)
    if not generated:
        raise ValueError("没有有效条目，拒绝替换旧版")
    manifest_path = args.repo / MANIFEST
    previous = read_json(manifest_path) if manifest_path.exists() else None
    managed, reused, changes, removed, cleanup = replacement(
        args.repo, previous, generated
    )
    manifest = {
        "version": args.version,
        "pdf_url": pdf_url,
        "metadata_url": metadata_url,
        "files": records,
        "managed_metadata": sorted(managed),
    }
    write_json(out / "manifest.json", manifest)
    write_json(
        out / "cleanup-preview.json",
        {
            "delete_objects": [
                f"{digest}.{ext}"
                for digest in cleanup
                for ext in ("pdf", "jpg", "webp")
            ],
            "executed": False,
        },
    )
    metadata_dir = out / "metadata"
    metadata_dir.mkdir()
    for digest, value in generated.items():
        (metadata_dir / f"{digest}.yml").write_text(yaml_text(value), encoding="utf-8")
    previous_count = len(previous["files"]) if previous else 0
    body = [
        f"# 同步大邮数学集 {args.version}",
        "",
        "单卷及元信息见本次 Actions artifact；存储上传结果由同步流程在文末报告。",
        "",
        f"源 PDF：{pdf_url}",
        f"源 metadata：{metadata_url}",
        "",
        f"- 上版收录条目：{previous_count}；本版源条目：{len(entries)}。",
        f"- 本版成功处理：{len(records)}；唯一 PDF：{len(generated)}；跳过：{len(skipped)}。",
        f"- 写入 YAML：{len(changes)}；删除旧 YAML：{len(removed)}；复用已有元信息：{len(reused)}。",
        f"- 构建阶段的旧文件清理候选：{len(cleanup)} 组；构建本身不删除存储对象。",
        "- 离线检查：现有 schema、未知字段、学年、页码、PDF 可读性、输出页数。",
        "- JPG/WebP 封面已从本地单卷按原发布流程的渲染与压缩规则生成。",
        "- 已完整重切；本版跳过项不会用上一版文件回填。",
        "",
        "## 跳过明细",
        "",
        "| 原标题 | 物理页码 | 原始标签 | 原因 |",
        "|---|---|---|---|",
    ]
    for entry, reason in skipped:
        values = [
            entry["title"],
            f"{entry['page-physical']}-{entry['page-physical'] + entry['length-in-page'] - 1}",
            "、".join(entry["tags"]),
            reason,
        ]
        body.append("| " + " | ".join(map(table_cell, values)) + " |")
    if reused:
        body += [
            "",
            "## 已有元信息冲突",
            "",
            "以下 MD5 已有非本任务管理的元信息，保留原文件：",
            "",
            *[f"- `{digest}`" for digest in reused],
        ]
    (out / "pr-body.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    # No repository mutation occurs until all source parsing and validation succeed.
    if args.apply:
        for digest in removed:
            (args.repo / "metadata" / f"{digest}.yml").unlink(missing_ok=True)
        for digest, contents in changes.items():
            (args.repo / "metadata" / f"{digest}.yml").write_text(
                contents, encoding="utf-8"
            )
        write_json(manifest_path, manifest)
    print(f"完成：{len(records)} 条，跳过 {len(skipped)} 条；产物 {out}")


def create_pr(args):
    assert_repository(args.repository)
    manifest = read_json(args.output / "manifest.json")
    version = manifest["version"]
    if not re.fullmatch(VERSION, version):
        raise ValueError("非法版本号")
    branch = BRANCH_PREFIX + version
    assert_origin(Path("."))
    existing_prs = json.loads(
        gh(
            "pr",
            "list",
            "--repo",
            args.repository,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url,isCrossRepository",
        )
    )
    for pr in existing_prs:
        if not pr["isCrossRepository"]:
            print(f"已有待审同步 PR，复用：{pr['url']}")
            return
    base = json.loads(gh("api", f"repos/{args.repository}"))["default_branch"]
    current = json.loads(gh("api", f"repos/{REPOSITORY}/git/ref/heads/{base}"))[
        "object"
    ]["sha"]
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        != current
    ):
        raise ValueError("默认分支已更新，请重新同步后创建 PR")
    subprocess.run(["git", "switch", "-c", branch], check=True)
    subprocess.run(["git", "add", "--", "metadata", str(MANIFEST)], check=True)
    if (
        subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode
        == 0
    ):
        print("元信息没有变化，无需创建 PR")
        return
    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], text=True
    ).splitlines()
    if any(
        path != MANIFEST.as_posix()
        and not re.fullmatch(r"metadata/[0-9a-f]{32}\.yml", path)
        for path in staged
    ):
        raise ValueError("待提交范围包含同步元信息之外的文件")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            f"feat: sync BUMP {version}",
        ],
        check=True,
    )
    existing = subprocess.check_output(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"], text=True
    ).split()
    lease = existing[0] if existing else ""
    subprocess.run(
        [
            "git",
            "push",
            "origin",
            f"HEAD:refs/heads/{branch}",
            f"--force-with-lease=refs/heads/{branch}:{lease}",
        ],
        check=True,
    )
    body_path = args.output / "pr-body.md"
    body = body_path.read_text(encoding="utf-8")
    body += f"\n运行与 artifact：{args.run_url}\n"
    body_path.write_text(body, encoding="utf-8")
    print(
        gh(
            "pr",
            "create",
            "--repo",
            args.repository,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            f"同步大邮数学集 {version}",
            "--body-file",
            str(body_path),
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--version", required=True)
    build_parser.add_argument("--repo", type=Path, default=Path("."))
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--source-pdf", type=Path)
    build_parser.add_argument("--source-metadata", type=Path)
    build_parser.add_argument("--apply", action="store_true")
    pr_parser = commands.add_parser("pr")
    pr_parser.add_argument("--repository", required=True)
    pr_parser.add_argument("--output", type=Path, required=True)
    pr_parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    {"build": build, "pr": create_pr}[args.command](args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
