# FileShaper

FileShaper is a collection of small Python scripts for cleaning up files and directory structures.

## Scripts

### `add_missing_extensions.py`

Recursively scans a directory for files without extensions and adds the appropriate extension based on the file content.

The script uses the `file` command to detect file types.

#### Dry run

```bash
python add_missing_extensions.py "path/to/directory"
```

#### Apply changes

```bash
python add_missing_extensions.py "path/to/directory" --apply
```

---

### `flatten_single_child_folders.py`

Converts directories with a single child directory into a flatter structure.

Example:

```text
root/
└── identifier/
    └── title/
        └── files
```

Becomes:

```text
root/
└── title(identifier)/
    └── files
```

Directories containing multiple entries or files directly inside them are skipped.

#### Dry run

```bash
python flatten_single_child_folders.py "path/to/root"
```

#### Apply changes

```bash
python flatten_single_child_folders.py "path/to/root" --apply
```

## Requirements

* Python 3.10 or later
* `file` command for `add_missing_extensions.py`

## Safety

Both scripts run in dry-run mode by default.

No files or directories are changed unless the `--apply` option is provided.
