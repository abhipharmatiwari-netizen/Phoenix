from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _parse_requirement(line: str) -> tuple[str, str]:
    requirement = line.split(";", 1)[0].strip()
    if "==" not in requirement:
        raise ValueError(f"Requirement must use exact pinning: {line}")
    name, version = requirement.split("==", 1)
    return name.strip(), version.strip()


def _load_components(requirements_path: Path) -> list[dict[str, str]]:
    components: list[dict[str, str]] = []
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, version = _parse_requirement(line)
        purl_name = name.lower().replace("[", "%5B").replace("]", "%5D")
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{purl_name}@{version}",
                "scope": "required",
            }
        )
    return components


def build_sbom(requirements_path: Path) -> dict[str, object]:
    components = _load_components(requirements_path)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "serialNumber": f"urn:uuid:{uuid4()}",
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "component": {
                "type": "application",
                "name": "Phoenix_v9",
            },
            "tools": [
                {
                    "vendor": "OpenAI",
                    "name": "Codex",
                }
            ],
        },
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a minimal CycloneDX SBOM from requirements.txt."
    )
    parser.add_argument(
        "--requirements",
        default="requirements.txt",
        help="Path to the pinned requirements file.",
    )
    parser.add_argument(
        "--output",
        default="sbom.json",
        help="Path to write the SBOM JSON artifact.",
    )
    args = parser.parse_args()

    requirements_path = Path(args.requirements)
    output_path = Path(args.output)
    sbom = build_sbom(requirements_path)
    output_path.write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
