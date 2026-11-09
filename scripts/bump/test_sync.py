"""Persistent contracts for the archive importer: admission, format and ownership."""

import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import covers
import jsonschema
import sync
from PIL import Image
from pypdf import PageObject
from pypdf.errors import PdfReadError

ROOT = Path(__file__).resolve().parents[2]


class CoverTests(unittest.TestCase):
    """Long-lived importer contract: local first-page previews follow publication policy."""

    def test_first_page_aspect_ratio_and_original_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "paper.pdf"
            for size in ((300, 600), (600, 300)):
                with self.subTest(size=size):
                    with (
                        Image.new("RGB", size, "red") as first,
                        Image.new("RGB", size, "blue") as second,
                    ):
                        first.save(
                            pdf, format="PDF", save_all=True, append_images=[second]
                        )
                    original = pdf.read_bytes()
                    covers.generate(pdf)
                    self.assertEqual(pdf.read_bytes(), original)
                    for extension, kind in (("jpg", "JPEG"), ("webp", "WEBP")):
                        with Image.open(pdf.with_suffix("." + extension)) as image:
                            self.assertEqual(image.format, kind)
                            self.assertEqual(
                                image.size,
                                (1000, 2000) if size[0] < size[1] else (2000, 1000),
                            )
                            red, green, blue = image.convert("RGB").getpixel((500, 500))
                            self.assertGreater(red, 220)
                            self.assertLess(green + blue, 30)

    def test_detailed_cover_uses_size_based_webp_compression(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "detailed.pdf"
            pixels = random.Random(7).randbytes(1000 * 1000 * 3)
            with Image.frombytes("RGB", (1000, 1000), pixels) as image:
                image.save(pdf, format="PDF")
            covers.generate(pdf)
            # Compare to the original policy's initial quality-100 encoding, not a
            # promised 50 KiB hard cap: that policy performs only one quality retry.
            with covers.pdfium.PdfDocument(str(pdf)) as document:
                page = document[0]
                bitmap = page.render(scale=2)
                with bitmap.to_pil().convert("RGB") as image:
                    full = io.BytesIO()
                    image.save(full, format="WEBP", quality=100, method=4)
                bitmap.close()
                page.close()
            self.assertGreater(full.tell(), 50 * 1024)
            self.assertLess(pdf.with_suffix(".webp").stat().st_size, full.tell())
            with Image.open(pdf.with_suffix(".webp")) as image:
                image.load()
                self.assertEqual(image.size, (2000, 2000))


def entry(
    title="2024–2025 学年第一学期《高等数学 A（上）》期末考试试题（A 卷）",
    tags=None,
    page=2,
    length=2,
):
    return {
        "title": title,
        "tags": ["分析上期末"] if tags is None else tags,
        "page-physical": page,
        "length-in-page": length,
    }


class MetadataTests(unittest.TestCase):
    def test_titles_produce_only_existing_schema_fields(self):
        cases = [
            (entry(), "高等数学A（上）", "First"),
            (
                entry("2024–2025《工科数学分析（上）》期中考试试题", []),
                "工科数学分析（上）",
                None,
            ),
            (
                entry("2017–2018 数分（下）期中试题", ["分析下期中", "无准确标题"]),
                "数学分析（下）",
                None,
            ),
            (
                entry("2020–2021 学年第一学期《高等数学 A》（上）期末考试试题（A1）"),
                "高等数学A（上）",
                "First",
            ),
            (
                entry(
                    "《矩阵分析与应用》期末考试试题 2011/2012 学年",
                    ["研究生课", "矩阵论期末", "无准确标题"],
                ),
                "矩阵分析与应用",
                None,
            ),
            (entry("2024–2025 高等数学考试试卷", []), "高等数学", None),
        ]
        schema = sync.strict_schema(
            sync.YAML_CODEC.load(
                (ROOT / "schema/test.yaml").read_text(encoding="utf-8")
            )
        )
        validator = jsonschema.Draft7Validator(schema)
        for source, course, semester in cases:
            with self.subTest(title=source["title"]):
                data = sync.parse_entry(source)
                self.assertEqual(data["filetype"], "pdf")
                self.assertEqual(data["external_source"], "bump")
                self.assertEqual(data["course"]["name"], course)
                self.assertEqual(data["time"].get("semester"), semester)
                value = {
                    "id": "a" * 32,
                    "url": "https://byrdocs.org/files/" + "a" * 32 + ".pdf",
                    "type": "test",
                    "data": data,
                }
                validator.validate(value)
        self.assertNotIn("stage", data["time"])
        ordinary = {**value, "data": {**data}}
        del ordinary["data"]["external_source"]
        validator.validate(ordinary)
        for filetype, source in [("bump", "bump"), ("pdf", "other")]:
            with self.subTest(filetype=filetype, external_source=source):
                invalid = {
                    **value,
                    "data": {**data, "filetype": filetype, "external_source": source},
                }
                self.assertFalse(validator.is_valid(invalid))
        data["title"] = "must not extend metadata"
        self.assertFalse(validator.is_valid(value))

    def test_question_numbers_do_not_imply_answers_in_generated_metadata(self):
        # Long-lived admission regression: only explicit answer text adds the answer label.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "metadata").mkdir()
            (root / "schema").mkdir()
            (root / "schema/test.yaml").write_bytes(
                (ROOT / "schema/test.yaml").read_bytes()
            )
            pdf = root / "source.pdf"
            with sync.PdfWriter() as writer:
                writer.add_blank_page(width=200, height=200)
                writer.add_blank_page(width=100, height=100)
                writer.write(pdf)
            source = root / "source.json"
            sync.write_json(
                source,
                {
                    "config": {"release-type": "release", "version-number": "1.5.0"},
                    "problemsets": [entry(page=2, length=1)],
                },
            )
            for number, (text, expected) in enumerate(
                [
                    ("一、1. 求函数的导数。", ["原题"]),
                    ("一、1. 求函数的导数。\n答案\n1. x", ["原题", "答案"]),
                ]
            ):
                with (
                    self.subTest(text=text),
                    patch.object(
                        PageObject,
                        "extract_text",
                        lambda page, text=text: (
                            "版权声明与授权许可 CC BY-NC-SA 4.0"
                            if page.mediabox.width == 200
                            else text
                        ),
                    ),
                ):
                    out = root / f"build-{number}"
                    sync.build(
                        SimpleNamespace(
                            repo=root,
                            output=out,
                            version="1.5.0",
                            source_pdf=pdf,
                            source_metadata=source,
                            apply=False,
                        )
                    )
                    yaml = next((out / "metadata").glob("*.yml"))
                    item = sync.YAML_CODEC.load(yaml.read_text(encoding="utf-8"))
                    self.assertEqual(item["data"]["content"], expected)
                    self.assertEqual(item["data"]["filetype"], "pdf")
                    self.assertEqual(item["data"]["external_source"], "bump")
                    self.assertEqual(
                        item["url"], f"https://byrdocs.org/files/{item['id']}.pdf"
                    )
                    pages = sync.PdfReader(out / "files" / f"{item['id']}.pdf").pages
                    self.assertEqual(
                        [page.mediabox.width for page in pages], [100, 200]
                    )

    def test_dates_and_missing_semester(self):
        for title, expected in [
            ("高等数学期末试题 (A) 2004.1", ("2003", "2004", "First")),
            ("高级数理逻辑 2008 年 12 月", ("2008", "2009", "First")),
            ("高数 B（下）2026 春期中", ("2025", "2026", "Second")),
            ("24–25 学年秋季学期高等代数（上）期中考试", ("2024", "2025", "First")),
            ("2017 高等数学（上）期中试卷", ("2017", "2017", None)),
            ("2018–2019 学年线性代数期末试题（A）", ("2018", "2019", None)),
        ]:
            with self.subTest(title=title):
                time = sync.parse_time(title)
                self.assertEqual(
                    (time["start"], time["end"], time.get("semester")), expected
                )

    def test_admission_starts_at_academic_year_2016(self):
        # Long-lived admission contract: use the parsed academic start year.
        for date, accepted in (
            ("2015", False),
            ("2015–2016 学年", False),
            ("2016 年 1 月", False),
            ("2016", True),
            ("2016–2017 学年", True),
            ("2016 年 9 月", True),
        ):
            with self.subTest(date=date):
                source = entry(f"{date} 高等数学期末考试")
                data = sync.parse_entry(source)
                if accepted:
                    sync.check_admission(data)
                    self.assertEqual(data["time"]["start"], "2016")
                else:
                    with self.assertRaisesRegex(sync.SkipPaper, "早于 2016 年"):
                        sync.check_admission(data)

    def test_unparseable_and_out_of_scope_entries_are_skippable(self):
        for source in [
            entry("高等数学期末考试"),
            entry("2024–2025《未知课程》期末"),
            entry("2024–2025 高等数学学习笔记", []),
            entry(tags=["新分类"]),
            entry(tags=["高考"]),
            entry(tags=["附加"]),
            entry("2022–2023 学年第 1 学期《运筹学》期末限时作业", ["运筹学期末"]),
            entry("2024–2025 高等数学期中", ["分析上期末"]),
        ]:
            with self.subTest(source=source), self.assertRaises(sync.SkipPaper):
                sync.parse_entry(source)

    def test_research_and_explicit_college(self):
        data = sync.parse_entry(
            entry(
                "2024–2025 第一学期《概率论与随机过程》期末", ["研究生课", "研概随期末"]
            )
        )
        self.assertEqual(data["course"]["type"], "研究生")
        data = sync.parse_entry(
            entry(
                "2019–2020 学年第 2 学期《概率论与数理统计》试题（B 卷，自动化学院用）",
                ["概统期末"],
            )
        )
        self.assertEqual(data["college"], ["智能工程与自动化学院"])
        self.assertEqual(data["time"]["stage"], "期末")

    def test_college_names_and_aliases(self):
        # Long-lived metadata contract: explicit names and aliases map to existing colleges.
        for college in sync.COLLEGES:
            with self.subTest(college=college):
                data = sync.parse_entry(entry(f"2024–2025 {college}高等数学期末试题"))
                self.assertEqual(data["college"], [college])
        cases = [
            ("元班", ["未来学院"]),
            ("未来学院（元班）", ["未来学院"]),
            ("自动化学院", ["智能工程与自动化学院"]),
            ("智能工程与自动化学院（自动化学院）", ["智能工程与自动化学院"]),
            ("计算机学院", ["计算机学院（国家示范性软件学院）"]),
            (
                "计算机学院、计算机学院（国家示范性软件学院）",
                ["计算机学院（国家示范性软件学院）"],
            ),
            (
                "计算机学院(国家示范性软件学院)",
                ["计算机学院（国家示范性软件学院）"],
            ),
            (
                "计算机学院、自动化学院用",
                ["智能工程与自动化学院", "计算机学院（国家示范性软件学院）"],
            ),
            ("未知学院", None),
            ("", None),
        ]
        for label, expected in cases:
            with self.subTest(label=label):
                data = sync.parse_entry(entry(f"2024–2025 高等数学期末试题（{label}）"))
                self.assertEqual(data.get("college"), expected)
        for title in (
            "2025–2026 学年第二学期元班《数学分析（下）》期末考试试题（A 卷）",
            "2023–2024 学年第二学期元班《数学分析（下）》",
        ):
            with self.subTest(title=title):
                data = sync.parse_entry(entry(title, ["分析下期末"]))
                self.assertEqual(data["college"], ["未来学院"])


class ReleaseTests(unittest.TestCase):
    def test_feed_ignores_preview_and_unrelated_updates(self):
        xml = "<rss><channel><item><title>大邮数学集快照版 261105</title><guid>https://pub.fa-555.com/rss#bump-preview-261105</guid></item><item><title>大邮数学集正式版 1.5.0 更新</title><guid>https://pub.fa-555.com/rss#bump-1.5.0</guid></item><item><title>其他项目正式版 9.0.0</title></item><item><title>大邮数学集正式版 1.4.3 更新</title><guid>https://pub.fa-555.com/rss#bump-1.4.3</guid></item></channel></rss>"
        self.assertEqual(sync.release_from_feed(xml), "1.5.0")
        with self.assertRaises(ValueError):
            sync.release_from_feed("<rss><channel /></rss>")

    def test_repeated_closed_open_and_downgrade_runs(self):
        closed = [{"state": "closed", "head": {"ref": "automation/bump-1.5.0"}}]
        opened = [{"state": "open", "head": {"ref": "automation/bump-1.4.3"}}]
        cases = [
            ("1.5.0", None, [], False, True),
            ("1.5.0", {"version": "1.5.0"}, [], False, False),
            ("1.5.0", None, closed, False, False),
            ("1.5.0", None, closed, True, True),
            ("1.5.0", None, opened, True, False),
            ("1.4.3", {"version": "1.5.0"}, [], True, False),
        ]
        for version, manifest, prs, retry, expected in cases:
            with self.subTest(version=version, manifest=manifest, prs=prs):
                self.assertEqual(
                    sync.should_build(version, manifest, prs, retry)[0], expected
                )

    def test_source_bounds_and_overlap_are_fatal(self):
        source = {
            "config": {"release-type": "release", "version-number": "1.5.0"},
            "problemsets": [entry()],
        }
        reader = SimpleNamespace(pages=[None] * 4)
        self.assertEqual(len(sync.validate_source(source, reader, "1.5.0")), 1)
        for changes in [
            {"problemsets": [entry(page=0)]},
            {"problemsets": [entry(page=4, length=2)]},
            {"problemsets": [entry(), entry(page=3)]},
            {"config": {"release-type": "preview", "version-number": "1.5.0"}},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sync.validate_source({**source, **changes}, reader, "1.5.0")


class OwnershipTests(unittest.TestCase):
    def test_zero_valid_entries_and_corrupt_pdf_leave_repository_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "metadata").mkdir(parents=True)
            (repo / "schema").mkdir()
            (repo / "schema/test.yaml").write_bytes(
                (ROOT / "schema/test.yaml").read_bytes()
            )
            original = repo / "metadata" / ("a" * 32 + ".yml")
            original.write_text("unchanged", encoding="utf-8")
            source_path = root / "source.json"
            source_path.write_text(
                json.dumps(
                    {
                        "config": {
                            "release-type": "release",
                            "version-number": "1.5.0",
                        },
                        "problemsets": [entry("未知课程", [], length=1)],
                    }
                ),
                encoding="utf-8",
            )
            pdf = root / "bad.pdf"
            pdf.write_bytes(b"not a PDF")
            args = SimpleNamespace(
                repo=repo,
                output=root / "zero",
                source_pdf=pdf,
                source_metadata=source_path,
                version="1.5.0",
                apply=True,
            )
            reader = SimpleNamespace(
                pages=[
                    SimpleNamespace(
                        extract_text=lambda: "版权声明与授权许可 CC BY-NC-SA 4.0"
                    ),
                    None,
                ]
            )
            with (
                patch.object(sync, "PdfReader", return_value=reader),
                self.assertRaisesRegex(ValueError, "没有有效条目"),
            ):
                sync.build(args)
            args.output = root / "corrupt"
            with self.assertRaises(PdfReadError):
                sync.build(args)
            self.assertEqual(original.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse((repo / sync.MANIFEST).exists())

    def test_replace_only_owned_metadata_and_preview_unreferenced_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "metadata").mkdir()
            old, shared, new = "a" * 32, "b" * 32, "c" * 32

            def item(digest):
                return {
                    "id": digest,
                    "url": f"https://byrdocs.org/files/{digest}.pdf",
                    "type": "test",
                    "data": sync.parse_entry(entry()),
                }

            for digest in (old, shared):
                (root / "metadata" / f"{digest}.yml").write_text(
                    sync.yaml_text(item(digest)), encoding="utf-8"
                )
            previous = {
                "managed_metadata": [old],
                "files": [{"md5": old}, {"md5": shared}],
            }
            managed, reused, changes, removed, cleanup = sync.replacement(
                root, previous, {new: item(new), shared: item(shared)}
            )
            self.assertEqual(managed, [new])
            self.assertEqual(reused, [shared])
            self.assertEqual(set(changes), {new})
            self.assertEqual(removed, [old])
            self.assertEqual(cleanup, [old])
            self.assertTrue((root / "metadata" / f"{old}.yml").exists())
            self.assertTrue((root / "metadata" / f"{shared}.yml").exists())

    def test_only_upstream_is_allowed_to_create_data_prs(self):
        sync.assert_repository("byrdocs/byrdocs-archive")
        with self.assertRaises(ValueError):
            sync.assert_repository("Shxiao111/byrdocs-archive")

    def test_pr_retry_reuses_existing_upstream_pr_before_git_mutations(self):
        # Long-lived PR boundary: retrying the final step must not rewrite an open PR.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            sync.write_json(output / "manifest.json", {"version": "1.5.0"})
            with (
                patch.object(sync, "assert_origin"),
                patch.object(
                    sync,
                    "gh",
                    return_value=json.dumps(
                        [
                            {
                                "url": "https://github.com/byrdocs/byrdocs-archive/pull/1",
                                "isCrossRepository": False,
                            },
                        ]
                    ),
                ) as gh,
                patch.object(
                    sync.subprocess, "run", side_effect=AssertionError("git mutation")
                ),
                patch.object(
                    sync.subprocess,
                    "check_output",
                    side_effect=AssertionError("git access"),
                ),
            ):
                sync.create_pr(
                    SimpleNamespace(repository=sync.REPOSITORY, output=output)
                )
            self.assertEqual(gh.call_args.args[:2], ("pr", "list"))


if __name__ == "__main__":
    unittest.main()
