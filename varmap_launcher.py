"""PyInstaller entry point (a package __main__ cannot be the script)."""
import sys

from varmap.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
