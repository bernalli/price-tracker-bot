"""Check that pending changelog content agrees with the declared project version."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# The grammar enforces triples and ASCII digits before integer conversion.
Version = tuple[int, ...]
VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
UNRELEASED = re.compile(r"## \[Unreleased\]\s*", re.IGNORECASE)
RELEASE = re.compile(r"## \[([^\]]*)\] - ([0-9]{4}-[0-9]{2}-[0-9]{2})")
LEVEL_TWO = re.compile(r"^##(?!#)")
CATEGORY = re.compile(r"###\s+\S.*")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


class MeasurementError(Exception):
    """An input did not establish a fact required by the gate."""


@dataclass(frozen=True)
class Changelog:
    lines: int
    release_headings: int
    newest: Version
    content_lines: int


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MeasurementError(f"{path}: could not read UTF-8 text: {exc}") from exc


def parse_version(value: object, path: Path | str) -> Version:
    if not isinstance(value, str) or not value:
        raise MeasurementError(f"{path}: version must be a non-empty string")
    if VERSION.fullmatch(value) is None:
        raise MeasurementError(f"{path}: invalid version {value!r}; expected ASCII X.Y.Z")
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise MeasurementError(
            f"{path}: version component cannot be converted to an integer"
        ) from exc


def read_project_version(path: Path) -> Version:
    try:
        document = tomllib.loads(read_text(path))
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise MeasurementError(f"{path}: could not parse TOML: {exc}") from exc
    project = document.get("project")
    if not isinstance(project, dict):
        raise MeasurementError(f"{path}: project must be a table")
    return parse_version(project.get("version"), path)


def parse_release(line: str, number: int, path: Path) -> Version:
    location = f"{path}: line {number}: {line!r}"
    match = RELEASE.fullmatch(line)
    if match is None:
        raise MeasurementError(f"{location}: unrecognised level-2 heading")
    version = parse_version(match[1], location)
    try:
        date.fromisoformat(match[2])
    except ValueError as exc:
        raise MeasurementError(f"{location}: invalid calendar date") from exc
    return version


def read_changelog(path: Path) -> Changelog:
    lines = read_text(path).splitlines()
    fence: str | None = None
    unreleased_line: int | None = None
    releases: list[tuple[int, Version]] = []
    seen: set[Version] = set()
    in_unreleased = False
    content_lines = 0

    for number, line in enumerate(lines, start=1):
        delimiter = FENCE.match(line)
        fenced = fence is not None or delimiter is not None
        if delimiter is not None:
            run = delimiter[1]
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None

        if not fenced and LEVEL_TWO.match(line):
            if UNRELEASED.fullmatch(line):
                if unreleased_line is not None:
                    raise MeasurementError(f"{path}: line {number}: duplicate [Unreleased] heading")
                unreleased_line = number
                in_unreleased = True
            else:
                version = parse_release(line, number, path)
                if version in seen:
                    raise MeasurementError(f"{path}: line {number}: duplicate release {line!r}")
                # Equality is rejected by the duplicate check above.
                if releases and version > releases[-1][1]:
                    raise MeasurementError(f"{path}: line {number}: releases are not descending")
                seen.add(version)
                releases.append((number, version))
                in_unreleased = False
        elif in_unreleased and line.strip() and (fenced or not CATEGORY.fullmatch(line)):
            content_lines += 1

    if fence is not None:
        raise MeasurementError(f"{path}: unclosed code fence")
    if unreleased_line is None:
        raise MeasurementError(f"{path}: missing [Unreleased] heading")
    if not releases:
        raise MeasurementError(f"{path}: missing release heading")
    if not any(number > unreleased_line for number, _version in releases):
        raise MeasurementError(f"{path}: no release heading after [Unreleased]")
    if releases[0][0] < unreleased_line:
        raise MeasurementError(f"{path}: release heading before [Unreleased]")
    return Changelog(len(lines), len(releases), releases[0][1], content_lines)


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)
    try:
        current = read_project_version(args.pyproject)
        changelog = read_changelog(args.changelog)
    except (MeasurementError, OSError, UnicodeError) as exc:
        print(f"UNABLE TO MEASURE: {exc}", file=sys.stderr)
        print("A gate that cannot measure is red, not green.", file=sys.stderr)
        return 2

    current_text = format_version(current)
    newest_text = format_version(changelog.newest)
    print(
        f"CHECKED: {changelog.lines} changelog lines, "
        f"{changelog.release_headings} release headings, "
        f"pyproject={current_text}, changelog_latest={newest_text}, "
        f"unreleased_content_lines={changelog.content_lines}"
    )
    pending = changelog.content_lines > 0
    ahead = current > changelog.newest
    in_sync = ahead if pending else current == changelog.newest
    if in_sync:
        return 0
    print(
        f"OUT OF SYNC: {args.pyproject} declares {current_text}; "
        f"{args.changelog} latest release is {newest_text}, "
        f"with {changelog.content_lines} pending content lines. "
        "Move the version to agree with the pending content, or move the entries "
        "under a dated release heading that matches the declared version.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
