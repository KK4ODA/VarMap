"""Set the version in varmap/__init__.py and pyproject.toml.  Used by release.bat;
a script file so the regexes do not depend on cmd/PowerShell quoting."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(version: str) -> int:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"bad version {version!r}: expected X.Y.Z")
        return 1
    targets = [
        (ROOT / "varmap" / "__init__.py", r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"'),
        (ROOT / "pyproject.toml", r'(?m)^version\s*=\s*"[^"]+"', f'version = "{version}"'),
    ]
    for path, pattern, repl in targets:
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(pattern, repl, text, count=1)
        if n != 1:
            print(f"version line not found in {path.name}")
            return 1
        path.write_text(new, encoding="utf-8")
        print(f"{path.name}: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
