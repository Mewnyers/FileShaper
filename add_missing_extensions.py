#!/usr/bin/env python3
"""Add extensions to extensionless files by inspecting their contents with `file`.

The script scans subdirectories recursively and runs in dry-run mode by default.
Pass --apply to rename files.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


BATCH_MAX_FILES = 100
BATCH_MAX_COMMAND_CHARS = 24_000

UNKNOWN_FILE_EXTENSIONS = {"", "???", "unknown"}
AMBIGUOUS_MIME_TYPES = {
    "application/octet-stream",
    "application/x-empty",
    "inode/x-empty",
}

# Canonical extensions for MIME types where Python's mimetypes result is
# absent, platform-dependent, or not the extension normally expected by users.
MIME_EXTENSION_OVERRIDES: dict[str, str] = {
    # Video
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
    "video/webm": "webm",
    "video/x-msvideo": "avi",
    "video/mpeg": "mpeg",
    "video/x-flv": "flv",
    "video/3gpp": "3gp",
    "video/3gpp2": "3g2",
    # Audio
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/webm": "webm",
    "audio/x-matroska": "mka",
    # Images
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/avif": "avif",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    # Documents and text
    "application/pdf": "pdf",
    "application/json": "json",
    "application/ld+json": "jsonld",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "txt",
    "text/csv": "csv",
    "text/html": "html",
    "text/css": "css",
    "text/javascript": "js",
    "application/javascript": "js",
    "text/markdown": "md",
    "text/x-python": "py",
    "text/x-script.python": "py",
    "text/x-shellscript": "sh",
    "application/x-sh": "sh",
    "text/x-perl": "pl",
    "text/x-ruby": "rb",
    "text/x-php": "php",
    # Archives and packages
    "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/vnd.rar": "rar",
    "application/x-rar": "rar",
    "application/x-rar-compressed": "rar",
    "application/x-tar": "tar",
    "application/gzip": "gz",
    "application/x-gzip": "gz",
    "application/x-bzip2": "bz2",
    "application/x-xz": "xz",
    "application/zstd": "zst",
    "application/x-zstd": "zst",
    "application/vnd.android.package-archive": "apk",
    "application/x-iso9660-image": "iso",
    # Office formats
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
}

# If `file --extension` offers multiple aliases, prefer the conventional one.
EXTENSION_PRIORITY = [
    "mp4", "mkv", "webm", "mov", "avi", "mpeg", "mp3", "m4a", "flac", "wav",
    "jpg", "png", "gif", "webp", "bmp", "tiff", "heic", "avif", "pdf", "txt",
    "json", "xml", "html", "csv", "zip", "7z", "rar", "tar", "gz", "bz2", "xz",
    "docx", "xlsx", "pptx", "doc", "xls", "ppt", "apk", "iso",
]


@dataclass(frozen=True)
class Detection:
    extension: str | None
    mime_type: str
    file_extensions: tuple[str, ...]


def iter_path_batches(paths: Sequence[Path]) -> Iterable[list[Path]]:
    """Yield command-line-safe batches for the Windows `file` command."""
    batch: list[Path] = []
    command_chars = len("file --brief --mime-type -- ")

    for path in paths:
        path_chars = len(str(path)) + 3
        if batch and (
            len(batch) >= BATCH_MAX_FILES
            or command_chars + path_chars > BATCH_MAX_COMMAND_CHARS
        ):
            yield batch
            batch = []
            command_chars = len("file --brief --mime-type -- ")

        batch.append(path)
        command_chars += path_chars

    if batch:
        yield batch


def extract_single_file_result(path: Path, stdout: str) -> str | None:
    """Extract a valid result when Windows `file` emits an extra diagnostic line."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    path_prefix = f"{path}:"

    prefixed_results = [
        line[len(path_prefix):].strip()
        for line in lines
        if line.startswith(path_prefix) and line[len(path_prefix):].strip()
    ]
    if len(prefixed_results) == 1:
        return prefixed_results[0]

    diagnostic_markers = ("cannot open `", "cannot open '", "error:")
    result_lines = [
        line
        for line in lines
        if not any(marker in line.lower() for marker in diagnostic_markers)
    ]
    if len(result_lines) == 1:
        return result_lines[0]

    return None


def run_file_command_batch(
    paths: Sequence[Path],
    option: str,
) -> tuple[dict[Path, str], dict[Path, str]]:
    """Run `file` for multiple paths and isolate failures by splitting batches."""
    outputs: dict[Path, str] = {}
    errors: dict[Path, str] = {}

    def run_batch(batch: Sequence[Path]) -> None:
        completed = subprocess.run(
            ["file", "--brief", option, "--", *(str(path) for path in batch)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]

        if completed.returncode == 0 and len(lines) == len(batch):
            outputs.update(zip(batch, lines))
            return

        if len(batch) > 1:
            midpoint = len(batch) // 2
            run_batch(batch[:midpoint])
            run_batch(batch[midpoint:])
            return

        recovered_result = extract_single_file_result(batch[0], stdout)
        if recovered_result is not None:
            outputs[batch[0]] = recovered_result
            return

        message = stderr.strip()
        if not message and len(lines) != 1:
            message = (
                f"file {option} returned an unexpected number of non-empty "
                f"output lines ({len(lines)} instead of 1); "
                f"stdout={stdout!r}"
            )
        elif message:
            message = f"file {option} failed: {message}; stdout={stdout!r}"
        errors[batch[0]] = message or f"file {option} failed"

    for batch in iter_path_batches(paths):
        run_batch(batch)

    return outputs, errors

def normalize_extension(value: str) -> str | None:
    extension = value.strip().lower().lstrip(".")
    if extension in UNKNOWN_FILE_EXTENSIONS:
        return None
    if not extension.replace("-", "").replace("_", "").isalnum():
        return None
    return extension


def choose_file_extension(raw_extensions: str) -> tuple[str | None, tuple[str, ...]]:
    candidates = tuple(
        extension
        for extension in (
            normalize_extension(item) for item in raw_extensions.split("/")
        )
        if extension is not None
    )
    if not candidates:
        return None, ()

    for preferred in EXTENSION_PRIORITY:
        if preferred in candidates:
            return preferred, candidates
    return candidates[0], candidates


def extension_from_mime(mime_type: str) -> str | None:
    if mime_type in AMBIGUOUS_MIME_TYPES:
        return None

    override = MIME_EXTENSION_OVERRIDES.get(mime_type)
    if override is not None:
        return override

    guessed = mimetypes.guess_extension(mime_type, strict=False)
    if guessed is None:
        return None
    return normalize_extension(guessed)


def detect_extensions(
    paths: Sequence[Path],
) -> tuple[dict[Path, Detection], dict[Path, str]]:
    """Detect extensions in batches, using `--extension` only as a fallback."""
    detections: dict[Path, Detection] = {}
    errors: dict[Path, str] = {}

    mime_outputs, mime_errors = run_file_command_batch(paths, "--mime-type")
    errors.update(mime_errors)

    fallback_paths: list[Path] = []
    for path in paths:
        mime_output = mime_outputs.get(path)
        if mime_output is None:
            continue

        mime_type = mime_output.lower()
        mime_extension = extension_from_mime(mime_type)
        if mime_extension is not None:
            detections[path] = Detection(mime_extension, mime_type, ())
        else:
            fallback_paths.append(path)

    extension_outputs, extension_errors = run_file_command_batch(
        fallback_paths, "--extension"
    )
    errors.update(extension_errors)

    for path in fallback_paths:
        raw_extensions = extension_outputs.get(path)
        if raw_extensions is None:
            continue

        file_extension, candidates = choose_file_extension(raw_extensions)
        mime_type = mime_outputs[path].lower()
        detections[path] = Detection(file_extension, mime_type, candidates)

    return detections, errors

def is_extensionless(path: Path) -> bool:
    return path.suffix == ""



def iter_files(
    directory: Path,
    recursive: bool,
    include_hidden: bool,
) -> Iterable[Path]:
    """Yield regular files using scandir to avoid redundant stat calls."""
    pending_directories = [directory]

    while pending_directories:
        current_directory = pending_directories.pop()
        try:
            with os.scandir(current_directory) as entries:
                for entry in entries:
                    if not include_hidden and entry.name.startswith("."):
                        continue

                    try:
                        if entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                        elif recursive and entry.is_dir(follow_symlinks=False):
                            pending_directories.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue

        if not recursive:
            break

def numbered_target(target: Path) -> Path:
    counter = 1
    while True:
        candidate = target.with_name(f"{target.stem}_{counter}{target.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def build_target(path: Path, extension: str) -> Path:
    base_name = path.name.rstrip(".")
    if not base_name:
        base_name = path.name
    return path.with_name(f"{base_name}.{extension}")


def rename_extensionless_files(args: argparse.Namespace) -> int:
    directory = args.directory.expanduser().resolve()
    if not directory.exists():
        print(f"[ERROR] Directory does not exist: {directory}", file=sys.stderr)
        return 2
    if not directory.is_dir():
        print(f"[ERROR] Not a directory: {directory}", file=sys.stderr)
        return 2
    if shutil.which("file") is None:
        print("[ERROR] The 'file' command was not found.", file=sys.stderr)
        return 2

    files_seen = 0
    scanned = 0
    renamed = 0
    unresolved = 0
    conflicts = 0
    errors = 0
    error_details: list[tuple[Path, str]] = []

    print(
        f"[SCAN] {directory} "
        f"(recursive={'yes' if args.recursive else 'no'}, "
        f"hidden={'included' if args.include_hidden else 'excluded'})"
    )

    extensionless_paths: list[Path] = []
    for path in iter_files(directory, args.recursive, args.include_hidden):
        files_seen += 1
        if is_extensionless(path):
            extensionless_paths.append(path)

    scanned = len(extensionless_paths)
    detections, detection_errors = detect_extensions(extensionless_paths)

    for path in extensionless_paths:
        detection_error = detection_errors.get(path)
        if detection_error is not None:
            errors += 1
            error_details.append((path, detection_error))
            print(f"[ERROR] {path}: {detection_error}")
            continue

        detection = detections[path]
        if detection.extension is None:
            unresolved += 1
            print(f"[SKIP]  {path} (unresolved: {detection.mime_type})")
            continue

        target = build_target(path, detection.extension)
        if target.exists():
            if args.collision == "number":
                target = numbered_target(target)
            else:
                conflicts += 1
                print(f"[SKIP]  {path} (target exists: {target.name})")
                continue

        action = "RENAME" if args.apply else "DRY-RUN"
        print(f"[{action}] {path} -> {target}")

        if args.apply:
            try:
                path.rename(target)
                renamed += 1
            except OSError as exc:
                errors += 1
                message = f"Failed to rename: {exc}"
                error_details.append((path, message))
                print(f"[ERROR] {path}: {message}")

    planned = renamed if args.apply else scanned - unresolved - conflicts - errors
    print(
        "Summary: "
        f"files_seen={files_seen}, "
        f"extensionless={scanned}, "
        f"{'renamed' if args.apply else 'planned'}={planned}, "
        f"unresolved={unresolved}, conflicts={conflicts}, errors={errors}"
    )

    if error_details:
        print(f"\nError details ({len(error_details)}):")
        for path, message in error_details:
            print(f"[ERROR] {path}: {message}")

    if args.apply:
        if renamed > 0:
            print(f"\nRenamed {renamed} file(s).")
        elif scanned == 0:
            print("\nNo extensionless files were found.")
        else:
            print("\nNo files were renamed.")
    elif planned > 0:
        print(f"\n[DRY-RUN] {planned} file(s) would be renamed.")
        print("No files were changed.")
        print("\nRun again with --apply to rename the files.")
    elif scanned == 0:
        print("\nNo extensionless files were found.")
    else:
        print("\nNo files can be renamed with the current results.")

    return 1 if errors else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect extensionless files with the file command and append their "
            "probable extensions. Subdirectories are scanned recursively; "
            "dry-run is the default."
        )
    )
    parser.add_argument("directory", type=Path, help="Directory to scan")
    recursion_group = parser.add_mutually_exclusive_group()
    recursion_group.add_argument(
        "--recursive",
        dest="recursive",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    recursion_group.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="Only scan files directly inside the specified directory",
    )
    parser.set_defaults(recursive=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename files",
    )
    hidden_group = parser.add_mutually_exclusive_group()
    hidden_group.add_argument(
        "--include-hidden",
        dest="include_hidden",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    hidden_group.add_argument(
        "--exclude-hidden",
        dest="include_hidden",
        action="store_false",
        help="Exclude hidden files and files inside hidden directories",
    )
    parser.set_defaults(include_hidden=True)
    parser.add_argument(
        "--collision",
        choices=("skip", "number"),
        default="skip",
        help="How to handle an existing target name (default: skip)",
    )
    return parser.parse_args()


def main() -> int:
    return rename_extensionless_files(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
