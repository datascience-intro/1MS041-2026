import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_mds", REPOSITORY_ROOT / "generate_mds.py"
)
generate_mds = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = generate_mds
SPEC.loader.exec_module(generate_mds)


def markdown_cell(source="# Public notebook\n", metadata=None):
    return {
        "cell_type": "markdown",
        "metadata": metadata or {},
        "source": [source],
    }


def code_cell(source="pass\n", metadata=None):
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": metadata or {},
        "outputs": [],
        "source": [source],
    }


def notebook(cells=None):
    return {
        "cells": cells or [markdown_cell()],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def entry(
    filename,
    *,
    section="lecture",
    order=1,
    state=None,
    label=None,
):
    result = {
        "filename": filename,
        "section": section,
        "order": order,
        "assignment_release_state": state,
    }
    if label is not None:
        result["label"] = label
    return result


class TemporaryPublicRoot:
    def __init__(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.notebooks = self.root / "notebooks"
        self.html = self.root / "html"
        self.notebooks.mkdir()
        self.html.mkdir()

    def close(self):
        self._temporary_directory.cleanup()

    def write_manifest(self, entries, filename="notebooks-manifest.json"):
        path = self.root / filename
        path.write_text(
            json.dumps({"schema_version": 1, "notebooks": entries}),
            encoding="utf-8",
        )
        return path

    def write_artifact(self, filename, cells=None):
        notebook_path = self.notebooks / filename
        notebook_path.write_text(json.dumps(notebook(cells)), encoding="utf-8")
        (self.html / Path(filename).with_suffix(".html").name).write_text(
            "<!doctype html><title>notebook</title>\n", encoding="utf-8"
        )
        return notebook_path


class GenerateMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.public = TemporaryPublicRoot()

    def tearDown(self):
        self.public.close()

    def run_main(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = generate_mds.main(["--root", str(self.public.root), *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_manifest_order_controls_index_and_check_mode(self):
        entries = [
            entry("20-Second.ipynb", order=2, label="20"),
            entry("10-First.ipynb", order=1, label="10"),
        ]
        self.public.write_manifest(entries)
        self.public.write_artifact("10-First.ipynb")
        self.public.write_artifact("20-Second.ipynb")

        result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        index = (self.public.root / "index.md").read_text(encoding="utf-8")
        self.assertLess(index.index("10-First"), index.index("20-Second"))
        self.assertIn("[10-First](10-First/)", index)
        self.assertNotIn("10-First.md", index)

        result, stdout, stderr = self.run_main("--check")
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("No generated-page or orphan changes.", stdout)

    def test_check_reports_stale_output_without_writing(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")

        result, stdout, stderr = self.run_main("--check")
        self.assertEqual(result, 1)
        self.assertIn("CREATE: 01-One.md", stdout)
        self.assertIn("CREATE: index.md", stdout)
        self.assertIn("stale", stderr)
        self.assertFalse((self.public.root / "01-One.md").exists())

    def test_dry_run_previews_orphans_and_normal_run_removes_only_managed_files(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        self.public.write_artifact("99-Orphan.ipynb")

        orphan_entry = generate_mds.NotebookEntry(
            filename="99-Orphan.ipynb",
            section="lecture",
            order=99,
            assignment_release_state=None,
            label="99",
        )
        orphan_page = self.public.root / "99-Orphan.md"
        orphan_page.write_text(generate_mds.generated_page(orphan_entry), encoding="utf-8")
        custom_page = self.public.root / "maintainer-notes.md"
        custom_page.write_text("Keep this hand-authored page.\n", encoding="utf-8")

        result, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("REMOVE: notebooks/99-Orphan.ipynb", stdout)
        self.assertIn("REMOVE: html/99-Orphan.html", stdout)
        self.assertIn("REMOVE: 99-Orphan.md", stdout)
        self.assertTrue(orphan_page.exists())
        self.assertFalse((self.public.root / "01-One.md").exists())

        result, _, stderr = self.run_main()
        self.assertEqual((result, stderr), (0, ""))
        self.assertFalse((self.public.notebooks / "99-Orphan.ipynb").exists())
        self.assertFalse((self.public.html / "99-Orphan.html").exists())
        self.assertFalse(orphan_page.exists())
        self.assertTrue(custom_page.exists())
        self.assertTrue((self.public.root / "01-One.md").is_file())

    def test_relative_manifest_path_is_resolved_from_root(self):
        self.public.write_manifest([entry("01-One.ipynb")], filename="payload.json")
        self.public.write_artifact("01-One.ipynb")
        result, stdout, stderr = self.run_main(
            "--manifest", "payload.json", "--dry-run"
        )
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn(str(self.public.root / "payload.json"), stdout)

    def test_manifest_must_be_contained_in_public_root(self):
        self.public.write_artifact("01-One.ipynb")
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name) / "manifest.json"
            outside.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "notebooks": [entry("01-One.ipynb")],
                    }
                ),
                encoding="utf-8",
            )
            result, _, stderr = self.run_main(
                "--manifest", str(outside), "--dry-run"
            )

        self.assertEqual(result, 2)
        self.assertIn("outside the public root", stderr)

    def test_cli_can_run_outside_the_public_root(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        with tempfile.TemporaryDirectory() as unrelated_directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "generate_mds.py"),
                    "--root",
                    str(self.public.root),
                    "--dry-run",
                ],
                cwd=unrelated_directory,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("CREATE: 01-One.md", result.stdout)

    def test_page_links_use_manifest_filename(self):
        approved = generate_mds.NotebookEntry(
            filename="01-One.ipynb",
            section="lecture",
            order=1,
            assignment_release_state=None,
            label="01",
        )
        page = generate_mds.generated_page(approved)
        self.assertIn("/notebooks/01-One.ipynb", page)
        self.assertIn("/html/01-One.html", page)
        self.assertIn("?path=01-One.ipynb", page)

    def test_nested_unapproved_notebook_cannot_enter_jupyterlite(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        nested_directory = self.public.notebooks / "data"
        nested_directory.mkdir()
        (nested_directory / "private.ipynb").write_text(
            json.dumps(notebook()), encoding="utf-8"
        )

        result, _, stderr = self.run_main("--dry-run")
        self.assertEqual(result, 2)
        self.assertIn("outside the manifest-owned top level", stderr)

    def test_case_variant_orphan_is_detected_and_removed(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        rogue = self.public.notebooks / "private.IPYNB"
        rogue.write_text(json.dumps(notebook()), encoding="utf-8")

        result, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("REMOVE: notebooks/private.IPYNB", stdout)
        self.assertTrue(rogue.exists())

        result, _, stderr = self.run_main()
        self.assertEqual((result, stderr), (0, ""))
        self.assertFalse(rogue.exists())

    def test_legacy_html_export_is_removed_from_notebooks_directory(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        legacy = self.public.notebooks / "01-One.html"
        legacy.write_text(
            "<!doctype html><title>legacy export</title>\n", encoding="utf-8"
        )

        result, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("REMOVE: notebooks/01-One.html", stdout)
        self.assertTrue(legacy.exists())

        result, _, stderr = self.run_main()
        self.assertEqual((result, stderr), (0, ""))
        self.assertFalse(legacy.exists())
        self.assertTrue((self.public.html / "01-One.html").is_file())

    def test_symlinked_notebooks_directory_cannot_delete_external_files(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.notebooks.rmdir()
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            approved = outside / "01-One.ipynb"
            approved.write_text(json.dumps(notebook()), encoding="utf-8")
            approved.with_suffix(".html").write_text(
                "<!doctype html><title>notebook</title>\n", encoding="utf-8"
            )
            private = outside / "Assignment_1_problem_TEST.ipynb"
            private.write_text("private\n", encoding="utf-8")
            self.public.notebooks.symlink_to(outside, target_is_directory=True)

            result, _, stderr = self.run_main()

            self.assertEqual(result, 2)
            self.assertIn("must not contain symlinks", stderr)
            self.assertEqual(private.read_text(encoding="utf-8"), "private\n")

    def test_symlinked_generated_page_cannot_overwrite_external_file(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name) / "keep.md"
            outside.write_text("keep me\n", encoding="utf-8")
            (self.public.root / "01-One.md").symlink_to(outside)

            result, _, stderr = self.run_main()

            self.assertEqual(result, 2)
            self.assertIn("must not contain symlinks", stderr)
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep me\n")

    def test_hand_authored_markdown_collision_is_not_overwritten(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        page = self.public.root / "01-One.md"
        page.write_text("hand-authored notes\n", encoding="utf-8")

        result, _, stderr = self.run_main()

        self.assertEqual(result, 2)
        self.assertIn("refusing to overwrite hand-authored Markdown", stderr)
        self.assertEqual(page.read_text(encoding="utf-8"), "hand-authored notes\n")

    def test_private_variant_supporting_asset_is_rejected(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        data_directory = self.public.notebooks / "data"
        data_directory.mkdir()
        (data_directory / "Assignment_1_solution_instructor.py").write_text(
            "private\n", encoding="utf-8"
        )

        result, _, stderr = self.run_main("--dry-run")
        self.assertEqual(result, 2)
        self.assertIn("private assignment variants", stderr)

    def test_failed_apply_rolls_back_pages_and_orphan_removals(self):
        self.public.write_manifest([entry("01-One.ipynb")])
        self.public.write_artifact("01-One.ipynb")
        self.public.write_artifact("99-Orphan.ipynb")
        approved_page = self.public.root / "01-One.md"
        approved_entry = generate_mds.NotebookEntry(
            filename="01-One.ipynb",
            section="lecture",
            order=1,
            assignment_release_state=None,
            label="01",
        )
        old_approved_page = generate_mds.generated_page(approved_entry) + "stale\n"
        approved_page.write_text(old_approved_page, encoding="utf-8")
        index = self.public.root / "index.md"
        index.write_text("old index\n", encoding="utf-8")
        orphan_entry = generate_mds.NotebookEntry(
            filename="99-Orphan.ipynb",
            section="lecture",
            order=99,
            assignment_release_state=None,
            label="99",
        )
        orphan_page = self.public.root / "99-Orphan.md"
        orphan_page.write_text(generate_mds.generated_page(orphan_entry), encoding="utf-8")

        real_replace = generate_mds._atomic_replace

        def fail_index_install(source, destination):
            if source.parent.name == "new" and destination.name == "index.md":
                raise OSError("injected install failure")
            real_replace(source, destination)

        with mock.patch.object(
            generate_mds, "_atomic_replace", side_effect=fail_index_install
        ):
            result, _, stderr = self.run_main()

        self.assertEqual(result, 2)
        self.assertIn("injected install failure", stderr)
        self.assertEqual(approved_page.read_text(encoding="utf-8"), old_approved_page)
        self.assertEqual(index.read_text(encoding="utf-8"), "old index\n")
        self.assertTrue((self.public.notebooks / "99-Orphan.ipynb").is_file())
        self.assertTrue((self.public.html / "99-Orphan.html").is_file())
        self.assertTrue(orphan_page.is_file())


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.public = TemporaryPublicRoot()

    def tearDown(self):
        self.public.close()

    def assert_manifest_error(self, entries, text):
        path = self.public.write_manifest(entries)
        with self.assertRaisesRegex(generate_mds.ManifestError, text):
            generate_mds.load_manifest(path)

    def test_rejects_duplicate_filename(self):
        self.assert_manifest_error(
            [entry("01-One.ipynb", order=1), entry("01-One.ipynb", order=2)],
            "duplicate notebook filename",
        )

    def test_rejects_case_insensitive_filename_collision(self):
        self.assert_manifest_error(
            [entry("01-One.ipynb", order=1), entry("01-one.ipynb", order=2)],
            "unique ignoring case",
        )

    def test_rejects_notebooks_that_collide_with_reserved_site_pages(self):
        for filename in ("index.ipynb", "README.ipynb", "Index.ipynb"):
            with self.subTest(filename=filename):
                self.assert_manifest_error([entry(filename)], "reserved site page")

    def test_rejects_unknown_manifest_and_entry_fields(self):
        path = self.public.root / "notebooks-manifest.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "notebooks": [entry("01-One.ipynb")],
                    "canvas_token": "must-not-be-published",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(generate_mds.ManifestError, "unsupported field"):
            generate_mds.load_manifest(path)

        unexpected = entry("01-One.ipynb")
        unexpected["private_notes"] = "must-not-be-published"
        self.assert_manifest_error([unexpected], "unsupported field")

    def test_rejects_duplicate_json_keys(self):
        path = self.public.root / "notebooks-manifest.json"
        path.write_text(
            '{"schema_version": 1, "schema_version": 1, "notebooks": []}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(generate_mds.ManifestError, "duplicate JSON key"):
            generate_mds.load_manifest(path)

    def test_rejects_duplicate_section_order(self):
        self.assert_manifest_error(
            [entry("01-One.ipynb"), entry("02-Two.ipynb")],
            "duplicate order",
        )

    def test_rejects_path_traversal_and_private_variant_names(self):
        for filename in (
            "../01-One.ipynb",
            "Assignment_1_problem_TEST.ipynb",
            "Assignment_1_solution.ipynb",
        ):
            with self.subTest(filename=filename):
                self.assert_manifest_error([entry(filename)], "safe|private")

    def test_training_testing_name_is_not_mistaken_for_private_test_variant(self):
        path = self.public.write_manifest(
            [entry("12-Training_Testing_Metrics.ipynb")]
        )
        entries = generate_mds.load_manifest(path)
        self.assertEqual(entries[0].filename, "12-Training_Testing_Metrics.ipynb")

    def test_rejects_markup_in_index_label(self):
        self.assert_manifest_error(
            [entry("01-One.ipynb", label="[click](bad)")],
            "label must contain only",
        )

    def test_requires_valid_release_state_for_every_entry(self):
        missing_state = entry("01-One.ipynb")
        del missing_state["assignment_release_state"]
        self.assert_manifest_error([missing_state], "is required")
        self.assert_manifest_error(
            [entry("01-One.ipynb", state="released")], "must be null"
        )
        self.assert_manifest_error(
            [
                entry(
                    "Assignment_1.ipynb",
                    section="assignment",
                    state="pending",
                )
            ],
            "must be one of",
        )

    def test_assignment_manifest_is_consecutive_and_cumulative(self):
        self.assert_manifest_error(
            [
                entry(
                    "Assignment_2.ipynb",
                    section="assignment",
                    order=2,
                    state="released",
                )
            ],
            "consecutive",
        )
        self.assert_manifest_error(
            [
                entry(
                    "Assignment_1.ipynb",
                    section="assignment",
                    order=1,
                    state="unreleased",
                ),
                entry(
                    "Assignment_2.ipynb",
                    section="assignment",
                    order=2,
                    state="released",
                ),
            ],
            "cumulative",
        )


class LeakValidationTests(unittest.TestCase):
    def setUp(self):
        self.public = TemporaryPublicRoot()
        self.entry = generate_mds.NotebookEntry(
            filename="Assignment_1.ipynb",
            section="assignment",
            order=1,
            assignment_release_state="released",
            label="1",
        )

    def tearDown(self):
        self.public.close()

    def assert_leak(self, cells, text):
        path = self.public.write_artifact("Assignment_1.ipynb", cells)
        with self.assertRaisesRegex(generate_mds.ManifestError, text):
            generate_mds.validate_notebook(self.entry, path)

    def test_rejects_private_problem_type_metadata(self):
        for problem_type in ("SOLUTION", "TEST"):
            with self.subTest(problem_type=problem_type):
                self.assert_leak(
                    [code_cell(metadata={"lx_problem_cell_type": problem_type})],
                    problem_type,
                )

    def test_rejects_private_tags(self):
        self.assert_leak(
            [code_cell(metadata={"tags": ["SOLUTION"]})], "private SOLUTION/TEST"
        )

    def test_rejects_private_source_marker(self):
        self.assert_leak(
            [markdown_cell("## Assignment 1, SOLUTION 1\n")], "assignment marker"
        )

    def test_rejects_stored_code_output_and_execution_count(self):
        leaked = code_cell()
        leaked["execution_count"] = 1
        leaked["outputs"] = [
            {"name": "stdout", "output_type": "stream", "text": ["private\n"]}
        ]
        self.assert_leak([leaked], "output-free")

    def test_rejects_credential_like_notebook_metadata(self):
        path = self.public.notebooks / "Assignment_1.ipynb"
        leaked_notebook = notebook([markdown_cell()])
        leaked_notebook["metadata"] = {"canvas_token": "must-not-be-published"}
        path.write_text(json.dumps(leaked_notebook), encoding="utf-8")
        with self.assertRaisesRegex(generate_mds.ManifestError, "credential-like"):
            generate_mds.validate_notebook(self.entry, path)

    def test_rejects_private_markers_in_matching_html(self):
        html_path = self.public.html / "Assignment_1.html"
        html_path.write_text(
            "<!doctype html><h2>Assignment 1, SOLUTION 1</h2>\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(generate_mds.ManifestError, "private SOLUTION"):
            generate_mds.validate_html(html_path)

        html_path.write_text(
            "<!doctype html><script>Assignment_1_problem_TEST.ipynb</script>\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(generate_mds.ManifestError, "private assignment"):
            generate_mds.validate_html(html_path)

    def test_allows_local_test_marker_in_matching_html(self):
        html_path = self.public.html / "Assignment_1.html"
        html_path.write_text(
            "<!doctype html><h2>Assignment 1, Test 1</h2>\n",
            encoding="utf-8",
        )
        generate_mds.validate_html(html_path)

    def test_allows_local_test_cells_in_released_assignment(self):
        path = self.public.write_artifact(
            "Assignment_1.ipynb",
            [
                markdown_cell(
                    "# ASSIGNMENT 1, PROBLEM 1, POINTS 1\n",
                    {"lx_problem_cell_type": "PROBLEM"},
                ),
                code_cell(
                    "# ASSIGNMENT 1, Test 1, POINTS 1\n",
                    {"lx_problem_cell_type": "Test"},
                ),
            ],
        )
        generate_mds.validate_notebook(self.entry, path)

    def test_unreleased_assignment_must_be_one_cell_placeholder(self):
        unreleased = generate_mds.NotebookEntry(
            filename="Assignment_1.ipynb",
            section="assignment",
            order=1,
            assignment_release_state="unreleased",
            label="1",
        )
        path = self.public.write_artifact(
            "Assignment_1.ipynb", [markdown_cell(), code_cell()]
        )
        with self.assertRaisesRegex(generate_mds.ManifestError, "one-cell placeholder"):
            generate_mds.validate_notebook(unreleased, path)

    def test_released_assignment_must_contain_a_problem(self):
        path = self.public.write_artifact("Assignment_1.ipynb", [markdown_cell()])
        with self.assertRaisesRegex(generate_mds.ManifestError, "no problem cells"):
            generate_mds.validate_notebook(self.entry, path)


if __name__ == "__main__":
    unittest.main()
