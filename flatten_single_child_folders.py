#!/usr/bin/env python3
"""
Flatten folders that contain exactly one child folder.

This converts the following structure:

  Before:
    root/
      identifier/
        title/
          file1.mp4
          file2.mp4

  After:
    root/
      title(identifier)/
        file1.mp4
        file2.mp4

Rules:
  - Only direct children of the specified root directory are inspected.
  - A folder is converted only when it contains exactly one entry and that
    entry is a directory.
  - Folders containing files, multiple entries, or no entries are left unchanged.
  - The default mode is a dry run. Use --apply to perform the renames.

Usage:
  # Dry run only
  python flatten_single_child_folders.py "P:\\path\\to\\root"

  # Actually apply changes
  python flatten_single_child_folders.py "P:\\path\\to\\root" --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


FLATTENED_NAME_RE = re.compile(r"^.+\([A-Za-z0-9_-]+\)$")


@dataclass
class Plan:
    container_dir: Path
    child_dir: Path
    target_dir: Path


@dataclass
class Summary:
    scanned: int = 0
    candidates: int = 0
    converted: int = 0
    skipped: int = 0
    errors: int = 0


def is_probably_flattened(name: str) -> bool:
    """Return True for folder names such as 'title(identifier)'."""
    return bool(FLATTENED_NAME_RE.match(name))


def list_children(path: Path) -> list[Path]:
    """List direct children. Raise if the directory cannot be read."""
    return list(path.iterdir())


def build_plan(container_dir: Path) -> tuple[Plan | None, str]:
    """
    Decide whether container_dir can be flattened.

    Returns:
      (Plan, "") if convertible.
      (None, reason) if it should be skipped.
    """
    if not container_dir.is_dir():
        return None, "not a directory"

    if is_probably_flattened(container_dir.name):
        return None, "already flattened"

    children = list_children(container_dir)

    if len(children) != 1:
        return None, f"folder has {len(children)} entries"

    only_child = children[0]
    if not only_child.is_dir():
        return None, "single entry is not a folder"

    target_name = f"{only_child.name}({container_dir.name})"
    target_dir = container_dir.parent / target_name

    if target_dir.exists():
        return None, f"target already exists: {target_dir.name}"

    return Plan(
        container_dir=container_dir,
        child_dir=only_child,
        target_dir=target_dir,
    ), ""


def unique_temp_path(root_dir: Path, identifier: str) -> Path:
    """Return an unused temporary directory path under root_dir."""
    process_id = os.getpid()
    base = root_dir / f".__folder_flatten_tmp_{identifier}_{process_id}"
    candidate = base
    index = 1

    while candidate.exists():
        candidate = root_dir / f"{base.name}_{index}"
        index += 1

    return candidate


def apply_plan(plan: Plan) -> None:
    """
    Apply the conversion using directory renames instead of per-file moves.

    Steps:
      1. Move the only child folder to a temporary path under the root.
      2. Remove the now-empty container folder.
      3. Rename the temporary folder to title(identifier).
    """
    root_dir = plan.container_dir.parent
    temp_dir = unique_temp_path(root_dir, plan.container_dir.name)

    plan.child_dir.rename(temp_dir)

    try:
        plan.container_dir.rmdir()
    except Exception:
        # Restore the original structure when the container cannot be removed.
        if temp_dir.exists() and not plan.child_dir.exists():
            temp_dir.rename(plan.child_dir)
        raise

    try:
        temp_dir.rename(plan.target_dir)
    except Exception:
        # Best-effort rollback.
        plan.container_dir.mkdir(exist_ok=False)
        if temp_dir.exists() and not plan.child_dir.exists():
            temp_dir.rename(plan.child_dir)
        raise


def flatten_folders(root_dir: Path, apply: bool) -> Summary:
    summary = Summary()

    if not root_dir.exists():
        raise FileNotFoundError(f"root directory does not exist: {root_dir}")
    if not root_dir.is_dir():
        raise NotADirectoryError(f"root path is not a directory: {root_dir}")

    print(f"Target root: {root_dir}")
    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}")
    print()

    for container_dir in sorted(root_dir.iterdir(), key=lambda path: path.name.lower()):
        summary.scanned += 1

        try:
            plan, reason = build_plan(container_dir)
        except Exception as exc:
            summary.errors += 1
            print(f"[ERROR] {container_dir.name}: failed to inspect: {exc}")
            continue

        if plan is None:
            summary.skipped += 1
            print(f"[SKIP]  {container_dir.name}: {reason}")
            continue

        summary.candidates += 1
        relative_before = f"{plan.container_dir.name}\\{plan.child_dir.name}"
        relative_after = plan.target_dir.name

        if not apply:
            print(f"[DRY]   {relative_before} -> {relative_after}")
            continue

        try:
            apply_plan(plan)
        except Exception as exc:
            summary.errors += 1
            print(f"[ERROR] {relative_before}: failed to convert: {exc}")
            continue

        summary.converted += 1
        print(f"[OK]    {relative_before} -> {relative_after}")

    print()
    print("Summary")
    print(f"  scanned:    {summary.scanned}")
    print(f"  candidates: {summary.candidates}")
    print(f"  converted:  {summary.converted}")
    print(f"  skipped:    {summary.skipped}")
    print(f"  errors:     {summary.errors}")

    if not apply:
        print()
        print("No changes were made. Re-run with --apply to rename the folders.")

    return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert identifier/title folders into title(identifier) folders "
            "when the identifier folder contains exactly one child folder."
        )
    )
    parser.add_argument(
        "root_dir",
        type=Path,
        help="Root directory containing the folders to inspect.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename folders. Without this option, only a dry run is shown.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    try:
        flatten_folders(args.root_dir, apply=args.apply)
    except Exception as exc:
        print(f"Fatal: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
