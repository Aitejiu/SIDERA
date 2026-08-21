import argparse
import json
from pathlib import Path
from typing import Any

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings


def export_openapi(output: Path) -> dict[str, Any]:
    settings = Settings(
        database_url=(
            "postgresql+psycopg://openapi:openapi@127.0.0.1:1/openapi"
        )
    )
    document = create_app(settings).openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the SIDERA OpenAPI contract")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_openapi(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
