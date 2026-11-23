@echo off
rem ============================================================================
rem  NSG demo bring-up - run this ONCE before a demo. It brings up, in order:
rem   engine distro : Docker daemon + nsg-engine container (+ a restart that
rem                   rebinds the WSL port-proxy so the engine is reachable from
rem                   the platform distro - a cold dockerd start leaves it dead)
rem   platform distro: backend (uvicorn :8000) + frontend (next dev :3000)
rem  All logic lives in per-distro shell scripts (kept out of this .bat so cmd's
rem  quoting can't mangle it). Open http://localhost:3000 when it prints all UP.
rem ============================================================================
echo === NSG demo bring-up ===
echo.
wsl.exe -d Ubuntu-2004-nsoran -u root -e bash /root/nsoran/demo_engine_up.sh
wsl.exe -d Ubuntu -u chancelor -e bash /home/chancelor/nsg_ai_platform/tools/demo_app_up.sh
echo.
echo ============================================================================
echo  Bring-up done. If everything above is UP, open:  http://localhost:3000
echo  Stuck live session / free the engine:
echo      wsl -d Ubuntu-2004-nsoran -u root docker restart nsg-engine
echo ============================================================================
echo.
echo  ^>^>^> KEEP THIS WINDOW OPEN during the demo. ^<^<^<
echo  WSL shuts an idle distro down and kills the backend/frontend with it, so
echo  this window holds the distro open. Close it (or Ctrl+C) to stop the platform.
echo.
rem TWO keepalives, because there are TWO distros. The platform one was here from
rem the start; the ENGINE one was not, and an idle engine distro takes dockerd
rem down with it - observed mid-run: "Cannot connect to the Docker daemon". The
rem backend then answers fine while every engine call fails, which reads as a
rem broken engine rather than a suspended distro.
start "" /b wsl.exe -d Ubuntu-2004-nsoran -u root -e bash -lc "tail -f /dev/null"
rem Blocking keepalive: an active WSL session keeps the distro (and the setsid
rem backend/frontend) alive. Starts <1s after bring-up, well inside WSL's idle grace.
wsl.exe -d Ubuntu -u chancelor -e bash -lc "tail -f /dev/null"
