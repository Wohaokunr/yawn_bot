# ruff: noqa: T201
"""Fail closed on unreviewed runtime dependency licenses."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from importlib import metadata
from pathlib import Path
from typing import Iterable

_PYTHON_LICENSE_OVERRIDES = {
    # Upstream offers Artistic OR GPL; YawnBot selects the Artistic option.
    "text-unidecode": ("1.3", "Artistic License"),
    # These versions have incomplete/UNKNOWN wheel metadata and were reviewed
    # against their upstream license files.
    "cookiecutter": ("2.7.1", "BSD-3-Clause"),
    "nonestorage": ("0.1.0", "MIT"),
}

_REVIEWED_COPYLEFT = {
    "nonebot-plugin-htmlkit": (
        "0.1.0rc5",
        ("MIT AND LGPL-3.0-or-later",),
    ),
    "certifi": ("2026.7.22", ("MPL", "Mozilla Public License")),
    "tqdm": ("4.70.0", ("MPL", "Mozilla Public License")),
}

_RESTRICTIVE_RE = re.compile(
    r"(?i)(\bAGPL(?:[-v]?\d+(?:\.\d+)?)?\b|\bAffero\b|"
    r"\bSSPL(?:[-v]?\d+(?:\.\d+)?)?\b|Server Side Public|Business Source|"
    r"\bBUSL(?:[-v]?\d+(?:\.\d+)?)?\b|Commons Clause|Non[- ]?Commercial|"
    r"\bProprietary\b)"
)
_WEAK_COPYLEFT_RE = re.compile(
    r"(?i)(\bLGPL(?:[-v]?\d+(?:\.\d+)?)?(?:-or-later|-only)?\b|"
    r"Lesser General Public|\bMPL(?:[-v]?\d+(?:\.\d+)?)?\b|Mozilla Public|"
    r"\bEPL(?:[-v]?\d+(?:\.\d+)?)?\b|Eclipse Public|"
    r"\bCDDL(?:[-v]?\d+(?:\.\d+)?)?\b)"
)
_GPL_RE = re.compile(
    r"(?i)((?<!L)\bGPL(?:[-v]?\d+(?:\.\d+)?)?(?:-or-later|-only)?\b|"
    r"GNU General Public License)"
)
_PERMISSIVE_RE = re.compile(
    r"(?i)(\bMIT(?:-0)?\b|\bBSD(?:-[0-9]-Clause)?\b|\bApache(?:-2\.0)?\b|"
    r"\bISC\b|\bPSF(?:-2\.0)?\b|Python Software Foundation|Artistic|Unlicense|"
    r"Public Domain|\bZlib\b|Boost Software License|\bOFL(?:-1\.1)?\b|"
    r"Open Font License)"
)


def _license_from_distribution(dist: metadata.Distribution) -> str:
    values: list[str] = []
    expression = dist.metadata.get("License-Expression")
    if expression:
        values.append(expression)
    license_field = dist.metadata.get("License")
    if license_field and license_field.lower() not in {"unknown", "none", "n/a"}:
        values.append(license_field)
    classifiers = dist.metadata.get_all("Classifier") or []
    values.extend(
        classifier.removeprefix("License :: ")
        for classifier in classifiers
        if classifier.startswith("License :: ")
    )
    return " ; ".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def _audit_python() -> int:
    failures: list[str] = []
    counts: Counter[str] = Counter()
    seen: set[str] = set()

    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        normalized = name.lower().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        version = dist.version
        raw_license = _license_from_distribution(dist)

        override = _PYTHON_LICENSE_OVERRIDES.get(normalized)
        if override is not None:
            reviewed_version, reviewed_license = override
            if version != reviewed_version:
                failures.append(
                    f"{name} changed from reviewed version {reviewed_version} to {version}"
                )
                continue
            raw_license = reviewed_license

        counts[raw_license or "<missing>"] += 1

        if not raw_license:
            failures.append(f"{name}=={version}: missing/unknown license metadata")
            continue
        if _RESTRICTIVE_RE.search(raw_license):
            failures.append(f"{name}=={version}: restrictive license {raw_license!r}")
            continue
        if _WEAK_COPYLEFT_RE.search(raw_license):
            review = _REVIEWED_COPYLEFT.get(normalized)
            if review is None:
                failures.append(
                    f"{name}=={version}: copyleft license {raw_license!r} has not "
                    "been reviewed"
                )
                continue
            reviewed_version, expected_fragments = review
            if reviewed_version != version:
                failures.append(
                    f"{name}=={version}: copyleft license {raw_license!r} has not "
                    "been reviewed for this version"
                )
                continue
            if not any(
                fragment.lower() in raw_license.lower()
                for fragment in expected_fragments
            ):
                failures.append(
                    f"{name}=={version}: reviewed license expression changed to "
                    f"{raw_license!r}"
                )
            continue
        if _GPL_RE.search(raw_license):
            failures.append(f"{name}=={version}: GPL license requires manual review")
            continue
        if not _PERMISSIVE_RE.search(raw_license):
            failures.append(
                f"{name}=={version}: unrecognized license {raw_license!r}; review required"
            )

    print(f"PYTHON_RUNTIME_PACKAGE_COUNT={len(seen)}")
    for license_name, count in sorted(counts.items()):
        print(json.dumps({"license": license_name, "count": count}, ensure_ascii=False))
    for failure in failures:
        print(f"LICENSE_AUDIT_ERROR: {failure}")
    return 1 if failures else 0


def _iter_node_packages(root: Path) -> Iterable[dict[str, object]]:
    stack = [root]
    seen: set[tuple[str, str]] = set()
    while stack:
        directory = stack.pop()
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if not child.is_dir() or child.name == ".bin":
                continue
            if child.name.startswith("@"):
                stack.append(child)
                continue
            package_json = child / "package.json"
            if package_json.is_file():
                try:
                    package = json.loads(package_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    package = {}
                name = str(package.get("name") or "")
                version = str(package.get("version") or "")
                if name and version and (name, version) not in seen:
                    seen.add((name, version))
                    yield package
            nested = child / "node_modules"
            if nested.is_dir():
                stack.append(nested)


def _audit_npm(root: Path) -> int:
    failures: list[str] = []
    counts: Counter[str] = Counter()
    package_count = 0
    for package in _iter_node_packages(root):
        package_count += 1
        name = str(package.get("name") or "")
        version = str(package.get("version") or "")
        license_value = package.get("license")
        if isinstance(license_value, str):
            license_name = license_value.strip()
        else:
            license_name = json.dumps(license_value, sort_keys=True) if license_value else ""
        counts[license_name or "<missing>"] += 1

        if not license_name:
            failures.append(f"{name}@{version}: missing license metadata")
            continue
        if _RESTRICTIVE_RE.search(license_name) or _GPL_RE.search(license_name):
            failures.append(f"{name}@{version}: license {license_name!r} requires review")
            continue
        if _WEAK_COPYLEFT_RE.search(license_name):
            failures.append(
                f"{name}@{version}: weak-copyleft license {license_name!r} requires review"
            )
            continue
        if not _PERMISSIVE_RE.search(license_name):
            failures.append(
                f"{name}@{version}: unrecognized license {license_name!r}; review required"
            )

    print(f"NPM_RUNTIME_PACKAGE_COUNT={package_count}")
    for license_name, count in sorted(counts.items()):
        print(json.dumps({"license": license_name, "count": count}, ensure_ascii=False))
    for failure in failures:
        print(f"LICENSE_AUDIT_ERROR: {failure}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("python")
    npm_parser = subparsers.add_parser("npm")
    npm_parser.add_argument("node_modules", type=Path)
    args = parser.parse_args()

    if args.mode == "python":
        return _audit_python()
    return _audit_npm(args.node_modules)


if __name__ == "__main__":
    raise SystemExit(main())
