"""Check the OneDrive handoff without importing the agent or contacting Azure."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import sys


DEMO_DIR = Path(__file__).resolve().parents[1]
MANIFEST = Path("run") / "handoff" / "manifest.json"


def verify(demo_dir: Path) -> list[str]:
    root = demo_dir.resolve()
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise ValueError("Missing run/handoff/manifest.json; finish OneDrive sync first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported handoff manifest.")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("The handoff file list must not be empty.")
    failures = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid handoff file entry.")
        name = entry.get("path")
        expected = entry.get("sha256")
        if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts):
            raise ValueError("Manifest paths must be relative to the demo folder.")
        key = str(PurePosixPath(name)).casefold()
        if key in seen:
            raise ValueError("Duplicate handoff file entry.")
        seen.add(key)
        if (not isinstance(expected, str) or len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)):
            raise ValueError("Invalid SHA-256 in handoff manifest.")
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Manifest path escapes the demo folder.")
        if not path.is_file():
            failures.append(f"MISSING: {name}")
            continue
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            failures.append(f"CHANGED: {name}")
    return failures


def main() -> int:
    try:
        failures = verify(DEMO_DIR)
    except (OSError, ValueError) as error:
        print(f"Handoff verification failed: {error}", file=sys.stderr)
        return 1
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print("Stop. Resolve missing/changed files before resuming.", file=sys.stderr)
        return 1
    print("Handoff files match. Authentication, tools, and Azure state still need review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
