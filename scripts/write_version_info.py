from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected")
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    init_text = (root / "src/azure_health_beacon/__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^__version__ = "([^"]+)"$', init_text, re.MULTILINE)
    if not match or match.group(1) != version:
        raise SystemExit("pyproject.toml and __version__ do not match")
    if arguments.expected and arguments.expected != version:
        raise SystemExit(
            f"release tag version {arguments.expected} does not match source {version}"
        )
    parts = tuple(int(part) for part in version.split("."))
    if len(parts) != 3:
        raise SystemExit("version must use MAJOR.MINOR.PATCH")
    major, minor, patch = parts
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Anders Olesen'),
        StringStruct('FileDescription', 'Azure Health Beacon'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'AzureHealthBeacon'),
        StringStruct('OriginalFilename', 'AzureHealthBeacon.exe'),
        StringStruct('ProductName', 'Azure Health Beacon'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(content, encoding="utf-8")
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
