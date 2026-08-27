#!/usr/bin/env python3
"""Generate and validate the manifest-owned notebook pages.

The public notebook manifest is the only source of notebook membership and
ordering. This script never discovers publishable notebooks by sorting a
directory listing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote


SCHEMA_VERSION = 1
SECTION_ORDER = ("introductory", "lecture", "probss", "assignment")
ASSIGNMENT_STATES = ("unreleased", "released")
SITE_ROOT = "https://datascience-intro.github.io/1MS041-2026"

SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.ipynb$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PRIVATE_FILENAME_RE = re.compile(
    r"(?:_TEST|_solution[^.]*)\.ipynb$", re.IGNORECASE
)
PRIVATE_SOURCE_RE = re.compile(
    r"^\s*#{1,6}\s*(?:(?i:assignment)\s+\d+\s*,\s*)?(?:SOLUTION|TEST)\b",
    re.MULTILINE,
)
PRIVATE_RENDERED_RE = re.compile(
    r"\bAssignment\s+\d+\s*,\s*(?:SOLUTION|TEST)\b"
)
PRIVATE_VARIANT_TEXT_RE = re.compile(
    r"\bAssignment_\d+_(?:problem_TEST|solution[A-Za-z0-9_-]*)\b",
    re.IGNORECASE,
)
PRIVATE_ASSET_NAME_RE = re.compile(
    r"^Assignment_\d+_(?:problem_TEST|solution[^.]*)(?:\.|$)",
    re.IGNORECASE,
)
SECRET_METADATA_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|token|secret|password|"
    r"authorization|cookie)(?:$|[_-])",
    re.IGNORECASE,
)
GENERATED_PAGE_MARKERS = (
    'title: ""',
    "permalink: /",
    'sidebar:\n  nav: "lMenu"',
)


class ManifestError(ValueError):
    """A public manifest or one of its approved artifacts is invalid."""


@dataclass(frozen=True)
class NotebookEntry:
    filename: str
    section: str
    order: int
    assignment_release_state: str | None
    label: str

    @property
    def stem(self) -> str:
        return Path(self.filename).stem

    @property
    def html_filename(self) -> str:
        return f"{self.stem}.html"

    @property
    def markdown_filename(self) -> str:
        return f"{self.stem}.md"


@dataclass(frozen=True, order=True)
class PlannedChange:
    action: str
    relative_path: str


class _HTMLTextExtractor(HTMLParser):
    """Collect rendered text, including script payloads, for leak markers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_unknown_keys(
    value: dict[str, Any], allowed: set[str], context: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ManifestError(
            f"{context} contains unsupported field(s): {', '.join(unknown)}"
        )


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{context} must be a JSON object")
    return value


def _default_label(filename: str, section: str, order: int) -> str:
    stem = Path(filename).stem
    if section == "assignment":
        match = re.fullmatch(r"Assignment_(\d+)", stem)
        return match.group(1) if match else str(order)
    prefix = stem.split("-", 1)[0]
    if section in {"lecture", "probss", "introductory"} and prefix:
        return prefix
    return str(order)


def load_manifest(manifest_path: Path) -> list[NotebookEntry]:
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest does not exist: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"invalid JSON in {manifest_path}: line {exc.lineno}, column {exc.colno}"
        ) from exc

    document = _require_mapping(raw, "manifest")
    _reject_unknown_keys(document, {"schema_version", "notebooks"}, "manifest")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(
            f"schema_version must be {SCHEMA_VERSION}, got "
            f"{document.get('schema_version')!r}"
        )

    raw_entries = document.get("notebooks")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ManifestError("notebooks must be a non-empty JSON array")

    entries: list[NotebookEntry] = []
    filenames: set[str] = set()
    normalized_filenames: set[str] = set()
    positions: set[tuple[str, int]] = set()

    for index, raw_entry in enumerate(raw_entries):
        context = f"notebooks[{index}]"
        item = _require_mapping(raw_entry, context)
        _reject_unknown_keys(
            item,
            {
                "filename",
                "section",
                "order",
                "assignment_release_state",
                "label",
            },
            context,
        )

        filename = item.get("filename")
        if not isinstance(filename, str) or not SAFE_FILENAME_RE.fullmatch(filename):
            raise ManifestError(
                f"{context}.filename must be a safe .ipynb basename, got {filename!r}"
            )
        if PRIVATE_FILENAME_RE.search(filename):
            raise ManifestError(
                f"{context}.filename names a private notebook variant: {filename}"
            )
        if Path(filename).stem.casefold() in {"index", "readme"}:
            raise ManifestError(
                f"{context}.filename would overwrite a reserved site page: {filename}"
            )
        if filename in filenames:
            raise ManifestError(f"duplicate notebook filename: {filename}")
        filenames.add(filename)
        normalized_filename = filename.casefold()
        if normalized_filename in normalized_filenames:
            raise ManifestError(
                f"notebook filenames must also be unique ignoring case: {filename}"
            )
        normalized_filenames.add(normalized_filename)

        section = item.get("section")
        if section not in SECTION_ORDER:
            raise ManifestError(
                f"{context}.section must be one of {SECTION_ORDER}, got {section!r}"
            )

        order = item.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            raise ManifestError(f"{context}.order must be a positive integer")
        position = (section, order)
        if position in positions:
            raise ManifestError(f"duplicate order {order} in section {section!r}")
        positions.add(position)

        if "assignment_release_state" not in item:
            raise ManifestError(f"{context}.assignment_release_state is required")
        release_state = item["assignment_release_state"]
        if section == "assignment":
            if release_state not in ASSIGNMENT_STATES:
                raise ManifestError(
                    f"{context}.assignment_release_state must be one of "
                    f"{ASSIGNMENT_STATES}"
                )
        elif release_state is not None:
            raise ManifestError(
                f"{context}.assignment_release_state must be null outside the "
                "assignment section"
            )

        label = item.get("label", _default_label(filename, section, order))
        if not isinstance(label, str) or not SAFE_LABEL_RE.fullmatch(label):
            raise ManifestError(
                f"{context}.label must contain only letters, digits, dot, "
                "underscore, or hyphen"
            )

        entries.append(
            NotebookEntry(
                filename=filename,
                section=section,
                order=order,
                assignment_release_state=release_state,
                label=label,
            )
        )

    rank = {section: index for index, section in enumerate(SECTION_ORDER)}
    ordered_entries = sorted(
        entries, key=lambda entry: (rank[entry.section], entry.order)
    )

    assignments = [
        entry for entry in ordered_entries if entry.section == "assignment"
    ]
    if assignments:
        orders = [entry.order for entry in assignments]
        if orders != list(range(1, len(assignments) + 1)):
            raise ManifestError("assignment orders must be consecutive starting at 1")
        for assignment in assignments:
            expected_filename = f"Assignment_{assignment.order}.ipynb"
            if assignment.filename != expected_filename:
                raise ManifestError(
                    f"assignment order {assignment.order} must use filename "
                    f"{expected_filename}"
                )
        seen_unreleased = False
        for assignment in assignments:
            if assignment.assignment_release_state == "unreleased":
                seen_unreleased = True
            elif seen_unreleased:
                raise ManifestError(
                    "assignment release states must be cumulative from Assignment 1"
                )

    return ordered_entries


def _read_notebook(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"invalid notebook JSON in {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc
    notebook = _require_mapping(raw, f"notebook {path.name}")
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise ManifestError(f"notebook has no cells array: {path}")
    return notebook


def _source_text(cell: dict[str, Any], path: Path) -> str:
    source = cell.get("source", "")
    if isinstance(source, str):
        return source
    if isinstance(source, list) and all(isinstance(part, str) for part in source):
        return "".join(source)
    raise ManifestError(f"cell source is not text in {path}")


def _reject_secret_metadata(value: Any, context: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if SECRET_METADATA_KEY_RE.search(key):
                raise ManifestError(
                    f"{context} contains credential-like metadata key {key!r}"
                )
            _reject_secret_metadata(nested, context)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_metadata(nested, context)


def _private_cell_reason(cell: dict[str, Any], path: Path) -> str | None:
    metadata = cell.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ManifestError(f"cell metadata is not an object in {path}")
    _reject_secret_metadata(metadata, f"cell metadata in {path}")

    problem_type = metadata.get("lx_problem_cell_type")
    if problem_type in {"SOLUTION", "TEST"}:
        return f"private lx_problem_cell_type={problem_type} metadata"

    tags = metadata.get("tags", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        raise ManifestError(f"cell metadata.tags is not an array in {path}")
    if any(tag in {"SOLUTION", "TEST"} for tag in tags):
        return "private SOLUTION/TEST cell tag"

    if PRIVATE_SOURCE_RE.search(_source_text(cell, path)):
        return "private SOLUTION/TEST assignment marker"
    return None


def validate_notebook(entry: NotebookEntry, path: Path) -> None:
    notebook = _read_notebook(path)
    cells = notebook["cells"]
    notebook_metadata = notebook.get("metadata", {})
    if not isinstance(notebook_metadata, dict):
        raise ManifestError(f"notebook metadata is not an object in {path}")
    _reject_secret_metadata(notebook_metadata, f"notebook metadata in {path}")

    for cell_index, raw_cell in enumerate(cells):
        if not isinstance(raw_cell, dict):
            raise ManifestError(f"cell {cell_index} is not an object in {path}")
        reason = _private_cell_reason(raw_cell, path)
        if reason:
            raise ManifestError(f"{path.name} cell {cell_index}: {reason}")
        if raw_cell.get("cell_type") == "code":
            outputs = raw_cell.get("outputs", [])
            if not isinstance(outputs, list):
                raise ManifestError(
                    f"{path.name} cell {cell_index}: outputs is not an array"
                )
            if outputs or raw_cell.get("execution_count") is not None:
                raise ManifestError(
                    f"{path.name} cell {cell_index}: public notebooks must be "
                    "output-free with null execution counts"
                )

    if entry.section != "assignment":
        return

    if entry.assignment_release_state == "unreleased":
        if len(cells) != 1:
            raise ManifestError(
                f"unreleased assignment must be a one-cell placeholder: {path.name}"
            )
        return

    problem_cells = 0
    for raw_cell in cells:
        metadata = raw_cell.get("metadata", {})
        if metadata.get("lx_problem_cell_type") == "PROBLEM":
            problem_cells += 1
            continue
        source = _source_text(raw_cell, path)
        if re.search(r"^\s*#\s*ASSIGNMENT\s+\d+\s*,\s*PROBLEM\b", source, re.M):
            problem_cells += 1
    if problem_cells == 0:
        raise ManifestError(f"released assignment contains no problem cells: {path.name}")


def validate_html(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"HTML export is not UTF-8 text: {path}") from exc

    parser = _HTMLTextExtractor()
    try:
        parser.feed(content)
        parser.close()
    except Exception as exc:  # HTMLParser can surface malformed entity errors.
        raise ManifestError(f"could not inspect HTML export {path}: {exc}") from exc

    rendered_text = parser.text()
    if PRIVATE_RENDERED_RE.search(rendered_text):
        raise ManifestError(
            f"{path.name} contains a private SOLUTION/TEST assignment marker"
        )
    if PRIVATE_VARIANT_TEXT_RE.search(content) or PRIVATE_VARIANT_TEXT_RE.search(
        rendered_text
    ):
        raise ManifestError(f"{path.name} names a private assignment variant")


def generated_page(entry: NotebookEntry) -> str:
    encoded_filename = quote(entry.filename, safe="")
    encoded_html = quote(entry.html_filename, safe="")
    return f'''---
title: ""
permalink: /{entry.stem}/
sidebar:
  nav: "lMenu"
---

Download as a jupyter [ipynb notebook]({SITE_ROOT}/notebooks/{encoded_filename}) or view it as [html]({SITE_ROOT}/html/{encoded_html}).

Open and run this notebook in [JupyterLite]({SITE_ROOT}/lite/lab/index.html?path={encoded_filename}).

<iframe src="{SITE_ROOT}/html/{encoded_html}" width="1080" height="1080" frameborder="0"></iframe>
'''


def _index_lines(entries: Sequence[NotebookEntry], section: str) -> list[str]:
    return [
        f"{entry.label}. [{entry.stem}]({quote(entry.stem, safe='')}/)"
        for entry in entries
        if entry.section == section
    ]


def generated_index(entries: Sequence[NotebookEntry]) -> str:
    introductory = _index_lines(entries, "introductory")
    lectures = _index_lines(entries, "lecture")
    probss = _index_lines(entries, "probss")
    assignments = _index_lines(entries, "assignment")

    parts = [
        """---
layout: default
title: Introduction to Data Science 1MS041
---

## Introduction to Data Science 1MS041

You can download the Lecture notes [here](https://datascience-intro.github.io/1MS041-2026/Files/LectureNotes1MS041.pdf).

Open [JupyterLite](https://datascience-intro.github.io/1MS041-2026/lite/lab/index.html) to create, open, and run the course notebooks in your browser.
"""
    ]

    if introductory:
        parts.append(
            """
### Introductory Jupyter .ipynb Notebooks
These notebooks contain the basic theory of how to work with Python and BASH that will be needed in this course.

"""
            + "\n".join(introductory)
            + "\n"
        )

    parts.append(
        """
### Individual Jupyter .ipynb lecture Notebooks

These notebooks are numbered according to the lecture they accompany and will be updated after the lectures. Before a lecture, its notebook may be preliminary.

"""
        + "\n".join(lectures)
        + "\n"
    )
    parts.append(
        """
### Problem Solving Sessions

These notebooks are numbered according to the problem-solving session they accompany.

"""
        + "\n".join(probss)
        + "\n"
    )
    parts.append(
        """
### Starting package
* Download the [Starting package](Files/first_lecture_and_data.zip)
* Unzip this into the folder that you will use as the course base folder.
* Put each later `*.ipynb` download in the same folder so that its relative data and image paths continue to work.

### Assignment notebooks

Unreleased assignments are one-cell placeholders. They are replaced by problem notebooks only when the corresponding assignment is released.

"""
        + "\n".join(assignments)
        + "\n"
    )
    return "".join(parts)


def _contained_path(root: Path, path: Path, context: str) -> Path:
    """Return a lexical absolute path after containment and symlink checks."""

    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"{context} is outside the public root: {path}") from exc

    cursor = root
    for component in relative.parts:
        cursor /= component
        if cursor.is_symlink():
            raise ManifestError(f"{context} must not use a symlink: {cursor}")

    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"{context} resolves outside the public root: {path}") from exc
    return lexical


def _validate_publication_symlinks(root: Path) -> None:
    """Reject links that a site or JupyterLite build could dereference."""

    excluded_at_root = {
        ".git",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        "_site",
    }
    for current_directory, directory_names, filenames in os.walk(
        root, followlinks=False
    ):
        current = Path(current_directory)
        if current == root:
            directory_names[:] = [
                name for name in directory_names if name not in excluded_at_root
            ]
        for name in [*directory_names, *filenames]:
            path = current / name
            if path.is_symlink():
                raise ManifestError(
                    f"public payload must not contain symlinks: {_relative(root, path)}"
                )


def _is_generated_page(path: Path) -> bool:
    if not path.is_file() or path.name in {"README.md", "index.md"}:
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return all(marker in content for marker in GENERATED_PAGE_MARKERS)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _expected_content(entries: Sequence[NotebookEntry], root: Path) -> dict[Path, str]:
    expected = {root / entry.markdown_filename: generated_page(entry) for entry in entries}
    expected[root / "index.md"] = generated_index(entries)
    return expected


def _orphan_paths(entries: Sequence[NotebookEntry], root: Path) -> list[Path]:
    notebooks_dir = root / "notebooks"
    html_dir = root / "html"
    expected_ipynb = {entry.filename for entry in entries}
    expected_html = {entry.html_filename for entry in entries}
    expected_markdown = {entry.markdown_filename for entry in entries}

    orphans: set[Path] = set()
    if notebooks_dir.is_dir():
        for path in notebooks_dir.iterdir():
            if not path.is_file():
                continue
            suffix = path.suffix.casefold()
            if suffix == ".ipynb" and path.name not in expected_ipynb:
                orphans.add(path)
            if suffix == ".html":
                orphans.add(path)

    if html_dir.is_dir():
        for path in html_dir.iterdir():
            if path.is_file() and (
                path.suffix.casefold() != ".html" or path.name not in expected_html
            ):
                orphans.add(path)

    for path in root.glob("*.md"):
        if path.name not in expected_markdown and _is_generated_page(path):
            orphans.add(path)
    return sorted(orphans, key=lambda path: _relative(root, path))


def validate_artifacts(entries: Sequence[NotebookEntry], root: Path) -> None:
    _validate_publication_symlinks(root)
    notebooks_dir = root / "notebooks"
    html_dir = root / "html"
    if not notebooks_dir.is_dir():
        raise ManifestError(f"notebooks directory does not exist: {notebooks_dir}")
    if not html_dir.is_dir():
        raise ManifestError(f"HTML directory does not exist: {html_dir}")

    private_assets = sorted(
        _relative(root, path)
        for directory in (notebooks_dir, html_dir)
        for path in directory.rglob("*")
        if path.is_file() and PRIVATE_ASSET_NAME_RE.search(path.name)
    )
    if private_assets:
        raise ManifestError(
            "private assignment variants are not allowed in the public payload: "
            + ", ".join(private_assets)
        )

    nested_exports = sorted(
        path.relative_to(notebooks_dir).as_posix()
        for path in notebooks_dir.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in {".ipynb", ".html"}
        and path.parent != notebooks_dir
    )
    if nested_exports:
        raise ManifestError(
            "notebook artifacts outside the manifest-owned top level are not "
            "allowed: "
            + ", ".join(nested_exports)
        )

    nested_html = sorted(
        path.relative_to(html_dir).as_posix()
        for path in html_dir.rglob("*")
        if path.is_file() and path.parent != html_dir
    )
    if nested_html:
        raise ManifestError(
            "HTML artifacts outside the manifest-owned top level are not allowed: "
            + ", ".join(nested_html)
        )

    for stale_html in notebooks_dir.glob("*.html"):
        validate_html(stale_html)

    for entry in entries:
        notebook_path = notebooks_dir / entry.filename
        html_path = html_dir / entry.html_filename
        if not notebook_path.is_file():
            raise ManifestError(f"manifest notebook is missing: {notebook_path}")
        if not html_path.is_file():
            raise ManifestError(f"matching HTML export is missing: {html_path}")
        validate_notebook(entry, notebook_path)
        validate_html(html_path)


def plan_changes(
    entries: Sequence[NotebookEntry], root: Path
) -> tuple[list[PlannedChange], dict[Path, str], list[Path]]:
    expected_content = _expected_content(entries, root)
    orphans = _orphan_paths(entries, root)
    changes = [PlannedChange("REMOVE", _relative(root, path)) for path in orphans]

    for path, content in expected_content.items():
        if not path.exists():
            action = "CREATE"
        elif path.read_text(encoding="utf-8") != content:
            if path.name != "index.md" and not _is_generated_page(path):
                raise ManifestError(
                    "refusing to overwrite hand-authored Markdown with a generated "
                    f"notebook page: {_relative(root, path)}"
                )
            action = "UPDATE"
        else:
            continue
        changes.append(PlannedChange(action, _relative(root, path)))

    changes.sort(key=lambda change: (change.relative_path, change.action))
    return changes, expected_content, orphans


def _atomic_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def apply_changes(
    root: Path, expected_content: dict[Path, str], orphans: Iterable[Path]
) -> None:
    """Apply all managed changes transactionally, rolling back I/O failures."""

    updates = {
        path: content
        for path, content in expected_content.items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    }
    orphan_paths = list(orphans)
    targets = sorted(
        {*updates, *orphan_paths}, key=lambda path: _relative(root, path)
    )
    if not targets:
        return

    for target in targets:
        _contained_path(root, target, f"managed target {_relative(root, target)}")

    with tempfile.TemporaryDirectory(prefix=".generate-mds-", dir=root) as temp_name:
        transaction_root = Path(temp_name)
        new_directory = transaction_root / "new"
        backup_directory = transaction_root / "backup"
        new_directory.mkdir()
        backup_directory.mkdir()

        staged_updates: dict[Path, Path] = {}
        for index, (target, content) in enumerate(
            sorted(updates.items(), key=lambda item: _relative(root, item[0]))
        ):
            staged = new_directory / f"{index:04d}.md"
            with staged.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            staged_updates[target] = staged

        backups: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        try:
            for index, target in enumerate(targets):
                if target.exists():
                    backup = backup_directory / f"{index:04d}"
                    _atomic_replace(target, backup)
                    backups.append((target, backup))
            for target, staged in staged_updates.items():
                _atomic_replace(staged, target)
                installed.append(target)
        except OSError as exc:
            backed_up = {target for target, _ in backups}
            rollback_errors: list[str] = []
            for target in reversed(installed):
                if target not in backed_up:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
            for target, backup in reversed(backups):
                try:
                    os.replace(backup, target)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                raise ManifestError(
                    "publication update failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise


def print_summary(
    root: Path,
    manifest_path: Path,
    entries: Sequence[NotebookEntry],
    changes: Sequence[PlannedChange],
) -> None:
    states = [
        f"{entry.label}={entry.assignment_release_state}"
        for entry in entries
        if entry.section == "assignment"
    ]
    print(f"Root: {root}")
    print(f"Manifest: {manifest_path}")
    print(f"Approved notebooks: {len(entries)}")
    print("Assignment releases: " + (", ".join(states) if states else "none"))
    if changes:
        for change in changes:
            print(f"{change.action}: {change.relative_path}")
    else:
        print("No generated-page or orphan changes.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate public notebook pages from notebooks-manifest.json."
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="public repository or staged payload root (default: script directory)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="manifest path within ROOT (default: ROOT/notebooks-manifest.json)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and preview page/orphan changes without writing",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate and fail if generated pages or orphan cleanup are pending",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    script_root = Path(__file__).resolve().parent
    root = (args.root or script_root).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: root does not exist: {root}", file=sys.stderr)
        return 2

    if args.manifest is None:
        manifest_candidate = root / "notebooks-manifest.json"
    elif args.manifest.is_absolute():
        manifest_candidate = args.manifest.expanduser()
    else:
        manifest_candidate = root / args.manifest

    try:
        manifest_path = _contained_path(root, manifest_candidate, "manifest")
        entries = load_manifest(manifest_path)
        validate_artifacts(entries, root)
        changes, expected_content, orphans = plan_changes(entries, root)
    except (ManifestError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print_summary(root, manifest_path, entries, changes)
    if args.check:
        if changes:
            print(
                "ERROR: generated notebook pages are stale or orphan cleanup is pending",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.dry_run:
        return 0

    try:
        apply_changes(root, expected_content, orphans)
    except (ManifestError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
