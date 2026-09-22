"""Exercise release measurement, refusal paths, and the command-line contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts import check_release_sync as gate

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release_sync.py"
DEFAULT = "# Changelog\n\n## [Unreleased]\n\n## [1.1.0] - 2026-09-11\n"
Inputs = tuple[Path, Path]
Result = tuple[int, str, str]


@pytest.fixture
def inputs(tmp_path: Path) -> Inputs:
    changelog = tmp_path / "changes.md"
    project = tmp_path / "project.toml"
    changelog.write_text(DEFAULT, encoding="utf-8")
    project.write_text('[project]\nversion = "1.1.0"\n', encoding="utf-8")
    return changelog, project


def invoke(inputs: Inputs, capsys: pytest.CaptureFixture[str]) -> Result:
    changelog, project = inputs
    code = gate.main(["--changelog", str(changelog), "--pyproject", str(project)])
    output = capsys.readouterr()
    return code, output.out, output.err


def assert_unable(result: Result, path: Path, fact: str) -> None:
    code, stdout, stderr = result
    assert code == 2
    assert stdout == ""
    assert stderr.startswith("UNABLE TO MEASURE: ")
    assert str(path) in stderr
    assert fact in stderr
    assert stderr.endswith("A gate that cannot measure is red, not green.\n")
    assert "Traceback" not in stderr


def assert_checked(
    result: Result,
    code: int = 0,
    *,
    lines: int = 5,
    headings: int = 1,
    current: str = "1.1.0",
    newest: str = "1.1.0",
    content: int = 0,
) -> None:
    assert result[0] == code
    assert result[1] == (
        f"CHECKED: {lines} changelog lines, {headings} release headings, "
        f"pyproject={current}, changelog_latest={newest}, unreleased_content_lines={content}\n"
    )
    if code == 0:
        assert result[2] == ""


@pytest.mark.parametrize(
    ("pending", "current", "expected"),
    [
        pytest.param(True, "1.2.0", 0, id="pending-ahead"),
        pytest.param(True, "1.1.0", 1, id="pending-equal"),
        pytest.param(True, "1.0.0", 1, id="pending-behind"),
        pytest.param(False, "1.2.0", 1, id="empty-ahead"),
        pytest.param(False, "1.1.0", 0, id="empty-equal"),
        pytest.param(False, "1.0.0", 1, id="empty-behind"),
    ],
)
def test_decision_matrix(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], pending: bool, current: str, expected: int
) -> None:
    changelog, project = inputs
    if pending:
        changelog.write_text(DEFAULT.replace("\n\n## [1.1.0]", "\n- Change\n## [1.1.0]"))
    project.write_text(f'[project]\nversion = "{current}"\n')
    result = invoke(inputs, capsys)
    assert_checked(result, expected, current=current, content=int(pending))
    if expected == 1:
        for text in (str(changelog), str(project), current, "1.1.0"):
            assert text in result[2]
        assert "Move the version" in result[2]
        assert "move the entries under a dated release heading" in result[2]


@pytest.mark.parametrize("version", ["0.0.0", "0.1.0", "1.1.0", "10.20.30"])
def test_valid_versions_pin_both_marker_versions(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    inputs[0].write_text(DEFAULT.replace("1.1.0", version))
    inputs[1].write_text(f'[project]\nversion = "{version}"')
    assert_checked(invoke(inputs, capsys), current=version, newest=version)


@pytest.mark.parametrize(
    ("current", "newest"), [("1.10.0", "1.9.9"), ("2.0.0", "1.99.99"), ("1.1.10", "1.1.9")]
)
def test_versions_compare_as_integer_triples(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], current: str, newest: str
) -> None:
    inputs[0].write_text(f"## [Unreleased]\nChange\n## [{newest}] - 2026-09-11\n")
    inputs[1].write_text(f'[project]\nversion = "{current}"')
    assert_checked(invoke(inputs, capsys), lines=3, current=current, newest=newest, content=1)


@pytest.mark.parametrize(
    "line",
    [
        "- Change",
        "* Change",
        "+ Change",
        "1. Change",
        "1) Change",
        "    - Change",
        "\t- Change",
        "-\tChange",
        "Bare prose",
        "    Indented prose",
        "> Quote",
        "<!-- note -->",
        "[link]: https://example.com",
        "#### Detail",
        "###",
        "###   ",
        "###Added",
        "##",
        "  ## Indented text",
    ],
)
def test_unknown_content_is_counted(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    inputs[0].write_text(f"## [Unreleased]\n{line}\n## [1.1.0] - 2026-09-11\n")
    assert_checked(invoke(inputs, capsys), 1, lines=3, content=1)


@pytest.mark.parametrize("body", ["", "\n \t\n", "### Added\n", "### Fixed\n\n### Added  \n"])
def test_empty_categories_and_whitespace_are_structure(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    text = f"## [Unreleased]\n{body}## [1.1.0] - 2026-09-11\n"
    inputs[0].write_text(text)
    assert_checked(invoke(inputs, capsys), lines=len(text.splitlines()))


@pytest.mark.parametrize("count", [2, 5])
def test_content_marker_counts_multiple_lines(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], count: int
) -> None:
    inputs[0].write_text(
        "## [Unreleased]\n### Fixed\n" + "Prose\n" * count + "## [1.1.0] - 2026-09-11\n"
    )
    assert_checked(invoke(inputs, capsys), 1, lines=count + 3, content=count)


def test_marker_counts_four_releases(inputs: Inputs, capsys: pytest.CaptureFixture[str]) -> None:
    inputs[0].write_text(
        DEFAULT
        + "## [1.0.0] - 2026-09-11\n"
        + "## [0.2.0] - 2026-09-12\n"
        + "## [0.1.0] - 2026-01-01\n"
    )
    assert_checked(invoke(inputs, capsys), lines=8, headings=4)


@pytest.mark.parametrize("extra", ["Preamble\n", "Extra\n\ntext\n\n"])
def test_line_marker_changes_without_changing_verdict(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], extra: str
) -> None:
    inputs[0].write_text(extra + DEFAULT)
    assert_checked(invoke(inputs, capsys), lines=5 + len(extra.splitlines()))


@pytest.mark.parametrize("heading", ["## [unreleased]", "## [UNRELEASED]   ", "## [UnReLeAsEd]\t"])
def test_unreleased_case_and_trailing_whitespace(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], heading: str
) -> None:
    inputs[0].write_text(DEFAULT.replace("## [Unreleased]", heading))
    assert_checked(invoke(inputs, capsys))


@pytest.mark.parametrize("opening", ["```python", "~~~~", "    ````text", "\t~~~text"])
def test_fenced_headings_are_content(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], opening: str
) -> None:
    character = opening.lstrip()[0]
    inputs[0].write_text(
        f"## [Unreleased]\n{opening}\n## [Unreleased]\n## [9.9.9] - 2026-01-01\n"
        f"## not a release\n### Added\n \t\n{character * 6}\n## [1.1.0] - 2026-09-11\n"
    )
    assert_checked(invoke(inputs, capsys), 1, lines=9, content=6)


def test_fence_requires_same_character_and_sufficient_length(
    inputs: Inputs, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs[0].write_text(
        "## [Unreleased]\n````\n~~~\n```\n## hidden\n`````\n## [1.1.0] - 2026-09-11\n"
    )
    assert_checked(invoke(inputs, capsys), 1, lines=7, content=5)


def test_empty_fence_delimiters_count(inputs: Inputs, capsys: pytest.CaptureFixture[str]) -> None:
    inputs[0].write_text("## [Unreleased]\n```\n\n```\n## [1.1.0] - 2026-09-11\n")
    assert_checked(invoke(inputs, capsys), 1, content=2)


def test_fences_outside_unreleased_are_still_tracked(
    inputs: Inputs, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs[0].write_text("~~~\n## [Unreleased]\n~~~\n" + DEFAULT + "```\n## invalid\n```\n")
    assert_checked(invoke(inputs, capsys), lines=11)


@pytest.mark.parametrize("opening", ["```", "~~~", "````\n```", "~~~\n````"])
@pytest.mark.parametrize("position", ["before", "pending", "after"])
def test_unclosed_fences_are_unmeasurable(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], opening: str, position: str
) -> None:
    if position == "before":
        text = opening + "\n" + DEFAULT
    elif position == "pending":
        text = DEFAULT.replace("\n\n## [1.1.0]", f"\n{opening}\n## [1.1.0]")
    else:
        text = DEFAULT + opening + "\n"
    inputs[0].write_text(text)
    assert_unable(invoke(inputs, capsys), inputs[0], "unclosed code fence")


@pytest.mark.parametrize(
    ("text", "fact"),
    [
        pytest.param("## [1.1.0] - 2026-09-11\n", "missing [Unreleased]", id="missing-unreleased"),
        pytest.param(
            "## [Unreleased]\n" + DEFAULT, "duplicate [Unreleased]", id="duplicate-unreleased"
        ),
        pytest.param("## [Unreleased]\n", "missing release", id="missing-release"),
        pytest.param(
            "## [1.1.0] - 2026-09-11\n## [Unreleased]\n",
            "no release heading after",
            id="no-release-after",
        ),
        pytest.param(
            "## [1.2.0] - 2026-09-12\n" + DEFAULT,
            "release heading before",
            id="release-before",
        ),
        pytest.param(
            DEFAULT + "## [1.1.0] - 2026-09-12\n", "duplicate release", id="duplicate-release"
        ),
        pytest.param(
            DEFAULT + "## [1.2.0] - 2026-09-10\n", "not descending", id="unordered-releases"
        ),
    ],
)
def test_structure_rules(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], text: str, fact: str
) -> None:
    inputs[0].write_text(text)
    assert_unable(invoke(inputs, capsys), inputs[0], fact)


@pytest.mark.parametrize(
    "heading",
    [
        "## [1.0.0]",
        "## [1.0.0] -",
        "## [1.0.0] – 2026-09-11",
        "## [1.0.0]-2026-09-11",
        "## [1.0.0]  - 2026-09-11",
        "## [1.0.0] - 2026-9-11",
        "## [1.0.0] - 2026-09-11 trailing",
        "## [1.0.0] - 2026-09-11 ",
        "## Unexpected",
        "##[1.0.0] - 2026-09-11",
        "## [Unreleased] extra",
        "## [Unreleased] - 2026-09-11",
    ],
)
def test_unknown_level_two_headings_are_refused_and_named(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], heading: str
) -> None:
    inputs[0].write_text(DEFAULT + heading + "\n")
    result = invoke(inputs, capsys)
    assert_unable(result, inputs[0], "heading" if "Unreleased] -" not in heading else "version")
    assert heading in result[2]


@pytest.mark.parametrize("date", ["2026-13-45", "2026-02-29", "2026-04-31", "0000-01-01"])
def test_dates_are_real_calendar_dates(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], date: str
) -> None:
    inputs[0].write_text(DEFAULT.replace("2026-09-11", date))
    assert_unable(invoke(inputs, capsys), inputs[0], "invalid calendar date")


def test_leap_day_is_valid(inputs: Inputs, capsys: pytest.CaptureFixture[str]) -> None:
    inputs[0].write_text(DEFAULT.replace("2026-09-11", "2024-02-29"))
    assert_checked(invoke(inputs, capsys))


@pytest.mark.parametrize("location", ["project", "latest", "older"])
@pytest.mark.parametrize(
    "version",
    [
        "1.0",
        "1.0.0.0",
        "1",
        "",
        "   ",
        "1.01.0",
        "01.0.0",
        "1.0.00",
        "1.0.-0",
        "2.-1.0",
        "+1.0.0",
        "١.0.0",
        pytest.param("1١.0.0", id="interior-arabic-indic"),
        "1.1١.0",
        "1.0.1١",
        "1.0.0-dev",
        "1.0.0rc1",
        "1.0.0.post1",
        "1.0.0+local",
        " 1.0.0",
        "1.0.0 ",
    ],
)
def test_invalid_versions_are_unmeasurable(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], location: str, version: str
) -> None:
    if location == "project":
        path = inputs[1]
        path.write_text(f"[project]\nversion = {json.dumps(version, ensure_ascii=False)}\n")
    else:
        path = inputs[0]
        text = (
            DEFAULT.replace("1.1.0", version)
            if location == "latest"
            else DEFAULT + f"## [{version}] - 2026-09-10\n"
        )
        path.write_text(text, encoding="utf-8")
    assert_unable(invoke(inputs, capsys), path, "version")


@pytest.mark.parametrize("location", ["project", "changelog"])
def test_integer_conversion_limit_is_unmeasurable(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], location: str
) -> None:
    version = "1" * 5000 + ".0.0"
    if location == "project":
        path = inputs[1]
        path.write_text(f'[project]\nversion = "{version}"')
    else:
        path = inputs[0]
        path.write_text(DEFAULT.replace("1.1.0", version))
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        assert_unable(invoke(inputs, capsys), path, "cannot be converted to an integer")
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize(
    ("text", "fact"),
    [
        ("[project", "parse TOML"),
        ("", "project must be a table"),
        ("project = 1", "project must be a table"),
        ("project = []", "project must be a table"),
        ('project = "wrong"', "project must be a table"),
        ("[project]", "non-empty string"),
        ("[project]\nversion = 1", "non-empty string"),
        ("[project]\nversion = true", "non-empty string"),
        ("[project]\nversion = []", "non-empty string"),
        ("[project]\nversion = {}", "non-empty string"),
    ],
)
def test_invalid_project_data_is_unmeasurable(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], text: str, fact: str
) -> None:
    inputs[1].write_text(text)
    assert_unable(invoke(inputs, capsys), inputs[1], fact)


@pytest.mark.parametrize("index", [0, 1], ids=["changelog", "project"])
@pytest.mark.parametrize("problem", ["missing", "directory", "utf8", "permission"])
def test_read_errors_are_unmeasurable(
    inputs: Inputs, capsys: pytest.CaptureFixture[str], index: int, problem: str
) -> None:
    path = inputs[index]
    if problem == "missing":
        path.unlink()
    elif problem == "directory":
        path.unlink()
        path.mkdir()
    elif problem == "utf8":
        path.write_bytes(b"\xff")
    else:
        original = Path.read_text

        def deny(target: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if target == path:
                raise PermissionError(13, "Permission denied", str(path))
            return original(target, encoding=encoding, errors=errors)

        # chmod is not a reliable permission denial when tests run as root.
        with patch.object(Path, "read_text", deny):
            assert_unable(invoke(inputs, capsys), path, "could not read UTF-8 text")
        return
    assert_unable(invoke(inputs, capsys), path, "could not read UTF-8 text")


@pytest.mark.parametrize("arguments", [["--unknown"], ["--changelog"], ["--pyproject"]])
def test_bad_arguments_exit_two(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_defaults_are_relative_to_working_directory(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(DEFAULT)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.1.0"')
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True
    )
    assert_checked((result.returncode, result.stdout, result.stderr))


def test_the_real_repository_is_in_sync() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.startswith("CHECKED: ")
