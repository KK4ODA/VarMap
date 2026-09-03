@echo off
setlocal
:: Cut a release:  release.bat 0.2.0
:: Bumps the version in varmap\__init__.py and pyproject.toml, commits, tags v0.2.0 and pushes.
:: GitHub Actions then builds the Windows installer, portable zip, macOS and Linux packages
:: and publishes them on https://github.com/KK4ODA/VarMap/releases
cd /d "%~dp0"
if "%~1"=="" (
    echo Usage: release.bat ^<version^>     e.g. release.bat 0.2.0
    exit /b 1
)
set VER=%~1
git diff --quiet || (echo Working tree has uncommitted changes. Commit or stash them first. & exit /b 1)
python -c "import re,io;p='varmap/__init__.py';s=open(p,encoding='utf-8').read();s=re.sub(r'__version__\s*=\s*\"[^\"]+\"','__version__ = \"%VER%\"',s);open(p,'w',encoding='utf-8').write(s);p='pyproject.toml';s=open(p,encoding='utf-8').read();s=re.sub(r'^version\s*=\s*\"[^\"]+\"','version = \"%VER%\"',s,flags=re.M);open(p,'w',encoding='utf-8').write(s)" || exit /b 1
python -m pytest -q tests || (echo Tests failed; release aborted. & git checkout -- varmap/__init__.py pyproject.toml & exit /b 1)
git add varmap/__init__.py pyproject.toml
git commit -m "Release v%VER%" || exit /b 1
git tag -a "v%VER%" -m "VarMap v%VER%" || exit /b 1
git push origin main --follow-tags || exit /b 1
echo.
echo Tagged v%VER% and pushed. Watch the build at:
echo   https://github.com/KK4ODA/VarMap/actions
echo The release appears at:
echo   https://github.com/KK4ODA/VarMap/releases/tag/v%VER%
endlocal
