# Release-sync gate — specification

Status: specification, to be implemented from scratch. Supersedes the first attempt, which was
rejected: it could print its own success marker and exit 0 without having interpreted the content
it claimed to measure.

## 1. What the gate is for

Between `v1.0.0` and `v1.1.0` eight commits landed on `main` — including one feature and one
security fix — while `pyproject.toml` still said `1.0.0`. Nothing in CI asked about the version, so
nobody noticed. This gate makes that question mechanical.

The rule, in both directions:

- `[Unreleased]` holds content → the declared version MUST be strictly ahead of the newest version
  already written in the CHANGELOG (a release is being prepared).
- `[Unreleased]` holds no content → the declared version MUST equal it (nothing is pending).

## 2. The failure mode this gate must resist

The dangerous state is the normal one. Once the debt is paid down to zero, "I counted no pending
content" and "I could not count" produce the same number, and that number agrees with the
expectation. A gate whose measurement silently breaks therefore turns green.

Two design consequences, and they are the whole point of this document:

**(a) Anything the parser does not recognise must be refused, never dropped.** The rejected
implementation counted only the lines matching one bullet pattern; every other shape of pending
content — a `+` bullet, a numbered item, an indented item, bare prose — was silently worth zero,
which reads as "nothing pending". Recognition is inverted below: content is everything that is not
a known structural element, so no unknown shape can be worth zero.

**(b) Every failure to establish a fact must raise, and raising must be a distinct exit code.**
"Out of sync" and "unable to measure" are different facts demanding different actions, and a
non-zero exit alone does not distinguish them — a missing file, a bad argument and a broken
interpreter all produce one.

## 3. Interface

```
python scripts/check_release_sync.py [--changelog PATH] [--pyproject PATH]
```

Defaults: `CHANGELOG.md`, `pyproject.toml`, both relative to the working directory.

| Exit | Meaning |
|---|---|
| 0 | in sync |
| 1 | out of sync |
| 2 | unable to measure |

Exit 2 is also what `argparse` produces for a malformed command line. That collision is accepted
and deliberate: a gate that was not invoked correctly did not measure either.

Standard library only. No new runtime or test dependency; the CI step runs the script with a plain
interpreter.

## 4. Version grammar

One grammar, applied identically to the `[project] version` field of `pyproject.toml` and to every
release heading in the CHANGELOG:

```
^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$        matched with re.ASCII
```

Accepted: `0.1.0`, `1.1.0`, `10.20.30`.

Rejected — each producing exit 2, never a skip and never a normalisation:

- fewer or more than three components (`1.0`, `1.0.0.0`)
- an empty string, or whitespace-only
- leading zeros (`1.01.0`)
- a sign (`1.0.-0`, `2.-1.0`, `+1.0.0`)
- non-ASCII digits (the `int()` builtin accepts them; `re.ASCII` is what excludes them, so the flag
  is load-bearing and has a mutant of its own in §9)
- any suffix: `-dev`, `rc1`, `.post1`, `+local`

The project has historically tagged `0.1.0-dev` builds, so a development suffix is not an
impossible input — it is a deliberately excluded one. A version outside this grammar, wherever it
appears, means the gate cannot establish which release is newest, and that is exit 2.

## 5. CHANGELOG structural contract

The file is read once as UTF-8 text and split into lines. Fenced code blocks are tracked first,
because a heading inside a fence is not a heading.

### 5.1 Fences

A fence opens on a line whose first non-whitespace run is three or more backticks or three or more
tildes, and closes on a later line whose run uses the same character and is at least as long. Lines
from the opening delimiter to the closing delimiter inclusive are fenced.

A fence that is never closed → **exit 2**. An unclosed fence means the gate cannot tell where any
section ends, and the rejected implementation's behaviour in that case was to truncate its scan and
pass.

### 5.2 Headings

Only unfenced lines are considered. Every line matching `^##[^#]` (a level-2 heading, whatever its
text) must be exactly one of:

- `## [Unreleased]` — case-insensitive, optional trailing whitespace
- `## [X.Y.Z] - YYYY-MM-DD` — version per §4; the date mandatory, separated by exactly ` - `, and a
  real calendar date (parsed, not merely digit-shaped: `2026-13-45` is refused)

Any other level-2 heading → **exit 2**, naming the offending line. This is the heading-level form of
rule (a): a truncated or differently punctuated release heading must not be quietly skipped, because
skipping it shifts "the newest release" onto an older one.

### 5.3 Structure rules

Each violation is **exit 2**:

1. No `## [Unreleased]` heading.
2. More than one `## [Unreleased]` heading.
3. No release heading at all.
4. No release heading after the `[Unreleased]` heading.
5. A release heading appearing before the `[Unreleased]` heading.
6. The same version used by two release headings.
7. Release versions not in strictly descending order from top to bottom.

Rule 7 is what closes the counterexample where a newer release hidden below an older one made the
gate compare against the wrong baseline.

Release **dates** are validated individually (§5.2) but deliberately **not** ordered. Date ordering
carries no information the version ordering does not already carry, and a gate that blocks CI over a
back-dated line teaches people to bypass it.

`newest_release` is the version of the first release heading after `[Unreleased]`.

### 5.4 What counts as pending content

The `[Unreleased]` section is the lines strictly between the `[Unreleased]` heading and the first
release heading after it. Each line is classified into a closed set:

| Line | Classification |
|---|---|
| whitespace-only | structure |
| unfenced `### <text>` with non-empty text, e.g. `### Fixed` | structure |
| anything else, fenced or not, including fence delimiters | **content** |

`unreleased_content_lines` is the number of content lines. The verdict depends only on whether that
number is zero.

An empty `### Added` left behind after a release is structure and does not block: it is sloppy, not
a pending change, and precision in both directions is a property of a gate people will keep.

Everything else is content by default. A `+` bullet, a numbered item, an indented item, a tab after
the dash, bare prose, a line inside a fence: all content. No syntax of pending content can be worth
zero, because nothing is matched against a list of accepted bullet forms.

## 6. The decision

With `pending = unreleased_content_lines > 0`, `current` the parsed pyproject version and `newest`
the parsed newest release version, compared as integer triples:

| | `current > newest` | `current == newest` | `current < newest` |
|---|---|---|---|
| `pending` | 0 — release in preparation | 1 — bump missing | 1 — version behind |
| `not pending` | 1 — bump with nothing to ship | 0 — just released | 1 — version behind |

Each exit-1 message names both versions, both file paths, and the two ways out (move the version, or
move the entries under a dated release heading).

## 7. The CHECKED marker

On a successful measurement — and only then — the script prints one line to stdout:

```
CHECKED: <changelog_lines> changelog lines, <release_headings> release headings, \
pyproject=<current>, changelog_latest=<newest>, unreleased_content_lines=<n>
```

The marker must never be printed on a run that exits 2.

The marker exists to make "I measured nothing" visible, so it must be **computed from quantities
other than the one deciding the exit code**, and it must be able to differ while the verdict stays
the same. `changelog_lines` is the strongest of the five: the size of what was read is independent
of every comparison the gate makes.

This is exactly where the rejected implementation failed its own check. Two ablations left its whole
suite green — replacing the heading count with a constant, and removing one bullet form from the
recognition regex — which means the marker was asserted but never pinned. §9 makes each of the five
numbers separately provable.

## 8. Error protocol

Every path that reads or interprets an input raises a single internal exception carrying a message
that names the file and the fact it could not establish. `main` catches that exception together with
`OSError` and `UnicodeError`, prints to **stderr**

```
UNABLE TO MEASURE: <what could not be established>
A gate that cannot measure is red, not green.
```

and returns 2.

Enumerated, because each of these currently escapes as an unhandled traceback or is absent:

- the file does not exist, is a directory, or cannot be read (permissions)
- the bytes are not valid UTF-8
- the TOML does not parse
- `project` is absent, or is not a table (`project = 1`, `project = []`, `project = "wrong"` all
  raise `TypeError` rather than `KeyError`)
- `version` is absent, or is not a non-empty string (`1`, `true`, `[]`, `{}`)
- a version component exceeds the interpreter's integer-conversion limit
- any violation listed in §4 and §5

## 9. Required verification

A green suite does not close this. The deliverable includes an ablation run whose result is reported
per mutant.

### 9.1 Test matrix

The suite must contain, at minimum, a case for every row of §6 (all six cells), every rule of §5.3,
every bullet-form and prose shape named in §5.4 asserted as **counted**, the empty-`###` case
asserted as **not counted**, both fence cases of §5.1 and §5.2, every entry of §8, and every
rejected form of §4. Negative cases assert the exit code **2** specifically, never merely "non-zero".

The marker is pinned with fixtures whose shape differs from the default: a changelog with four
release headings must print `4`, one with a different line count must print that count, and the
content count must be asserted at more than one non-zero value.

`test_the_real_repository_is_in_sync` is kept: the gate must be green on the tree it ships in.

### 9.2 The lockfile, appended verbatim to `tests/unit/test_smoke.py`

The committed lockfile carries the project version too, and nothing checks it. `tomllib` and
`PYPROJECT` are already imported in that module.

```python
def test_lock_version_matches_pyproject():
    """The committed lockfile must describe the same editable project version."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads(PYPROJECT.with_name("uv.lock").read_text(encoding="utf-8"))
    roots = [
        package
        for package in lock["package"]
        if package["name"] == project["name"] and package.get("source") == {"editable": "."}
    ]
    assert len(roots) == 1
    assert roots[0]["version"] == project["version"]
```

### 9.3 Ablation

Each mutant below is applied to the **product** code alone, the suite is re-run, and the report
gives the number of failures and the name of at least one failing test. A mutant that leaves the
suite green is a missing case, and the case is added before the work is called done.

1. `pending` forced to `False`; 2. `pending` forced to `True`
3. the ahead-comparison forced to `False`; 4. forced to `True`
5. the equality check of the not-pending row removed
6. `changelog_lines` in the marker replaced by a constant
7. `release_headings` in the marker replaced by a constant
8. `unreleased_content_lines` in the marker replaced by a constant
9. `changelog_latest` in the marker replaced by a constant
10. `pyproject` in the marker replaced by a constant
11. the `re.ASCII` flag removed from the version grammar
12. the leading-zero guard relaxed to `\d+`
13. the three-component check relaxed to accept any number of components
14. fence tracking disabled (fence lines treated as ordinary lines)
15. the unclosed-fence check removed
16. the duplicate-`[Unreleased]` check removed
17. the duplicate-release check removed
18. the descending-order check removed
19. the unrecognised-level-2-heading check relaxed from "refuse" to "skip"
20. date validation relaxed to a digit-shaped regex
21. `###` reclassified as content instead of structure
22. each `raise` in a read path, one at a time, replaced by a benign default

Mutants 6 to 10 are the ones the previous attempt could not survive. Mutants 11 to 21 all target
behaviour that did not exist before, which is precisely where an untested new instrument hides.

### 9.4 Proving the ablation tool itself

An ablation harness that reports "all mutants survived" is far more likely to be broken than to be
telling the truth. Two mechanisms have produced exactly that false reading here before:

- A substitution command that exits 0 **without having changed any file**. Every mutation is
  therefore verified as a fact — compare the file before and after and assert it differs — and never
  inferred from an exit status.
- A stale bytecode cache. A same-length substitution can leave the source size unchanged, and if
  application and restore fall inside the same mtime-granularity second the interpreter serves the
  old `.pyc` and measures the unmutated file. The harness runs with bytecode writing disabled and
  clears `__pycache__` on both application and restore.

### 9.5 Gates that must be green

`pytest` (full suite, the count compared against the baseline on the branch point), `ruff check`,
`ruff format --check`, `mypy --strict`, `bash scripts/audit_english.sh`.

## 10. CI wiring

Re-add to `.github/workflows/ci.yml`, after the English-only audit and before the coverage upload:

```yaml
      - name: Release sync (CHANGELOG vs version)
        run: python scripts/check_release_sync.py
```

The step is skipped when an earlier step fails, which is GitHub's implicit `success()` condition. A
skipped step must be read as *not executed*, never as a measurement that passed — the job is already
red in that case, so nothing is lost.

## 11. Out of scope

- `price_tracker.__version__`, already pinned to `pyproject.toml` by
  `tests/unit/test_smoke.py::test_version_matches_pyproject`. The absence of a check here is not the
  absence of a check.
- Git tags, release artefacts, and anything that talks to the network.
- Changing the CHANGELOG format itself.
