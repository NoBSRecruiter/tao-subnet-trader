@echo off
rem taotrader launcher for Windows (WP12; DESIGN.md section 12.1).
rem
rem   scripts\run.cmd <command> [options]       e.g.  scripts\run.cmd paper --config config\books.paper.toml
rem
rem Runs the project's own venv (created by `uv sync --frozen --all-extras`) from the repository root, so relative
rem paths (config\, data\, logs\, reports\) resolve the same way whether it is started by hand or by Task Scheduler.
rem The exit code of taotrader is passed through (0 ok, 1 check failed, 2 usage/config, 3 live gate, 4 locked,
rem 5 replay divergence). Nothing here changes system settings.
setlocal
set "TT_ROOT=%~dp0.."
pushd "%TT_ROOT%" || exit /b 2
if not exist ".venv\Scripts\python.exe" (
  echo taotrader: .venv not found in %CD% - run "uv sync --frozen --all-extras" first 1>&2
  popd
  exit /b 2
)
".venv\Scripts\python.exe" -m taotrader %*
set "TT_RC=%ERRORLEVEL%"
popd
exit /b %TT_RC%
