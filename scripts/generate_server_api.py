"""Download the SchoolCloud v3 OpenAPI document and generate its Python client."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC_URL = "https://main.niedersachsen.schulcloud-verbund.org/api/v3/docs-json"
DEFAULT_SPEC_PATH = PROJECT_ROOT / "specs" / "docs-json"
DEFAULT_OUTPUT = PROJECT_ROOT / "generated" / "server-v3"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-url", default=DEFAULT_SPEC_URL)
    parser.add_argument("--spec-path", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--refresh-spec", action="store_true", help="Download the OpenAPI document before generating.")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing generated client directory.")
    args = parser.parse_args()

    spec_path = args.spec_path.resolve()
    if args.refresh_spec or not spec_path.exists():
        try:
            with urlopen(args.spec_url, timeout=30) as response:
                document = json.load(response)
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            print(f"Could not download OpenAPI spec from {args.spec_url}: {error}", file=sys.stderr)
            return 1
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    output_path = args.output.resolve()
    if output_path.exists() and not args.overwrite:
        print(f"Output already exists: {output_path} (pass --overwrite to regenerate)", file=sys.stderr)
        return 2

    command = [
        sys.executable,
        "-m",
        "openapi_python_client",
        "generate",
        "--path",
        str(spec_path),
        "--output-path",
        str(output_path),
        "--overwrite",
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
