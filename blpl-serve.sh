#!/bin/bash
readenv() {
  local filePath="${HOME}/devspace/myprojects/blpl/.env"

  if [ ! -f "$filePath" ]; then
    # silently be done
    # put some error / echo if you prefer non-silent errors
    return 0
  fi

#  echo "Reading $filePath"
  while read -r line; do
    if [[ "$line" =~ ^\s*#.*$ || -z "$line" ]]; then
      continue
    fi

     # Split the line into key and value. Trim whitespace on either side.
    key=$(echo "$line" | cut -d '=' -f 1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    value=$(echo "$line" | cut -d '=' -f 2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

    # Leaving the below here... normally this works, but if you have something like
    # FOO="  string with leading and trailing  "
    # then the leading / trailing spaces are deleted. FOO="a word", FOO='a word', and FOO=a word all generally work
    # so leave the quotes
    # Remove single quotes, double quotes, and leading/trailing spaces from the value
    # value=$(echo "$value" | sed -e "s/^'//" -e "s/'$//" -e 's/^"//' -e 's/"$//' -e 's/^[[:space:]]*//;s/[[:space:]]*$//')

    # Export the key and value as environment variables
    # echo "$key=$value"
    export "$key=$value"

  done < "$filePath"
}

# Platform detection
case "$(uname -s)" in
  Darwin)
    export SHELL_OS='macOS'
    _SHELL_IS_MACOS=1
    _SHELL_IS_LINUX=0
    ;;
  Linux)
    export SHELL_OS='Linux'
    _SHELL_IS_MACOS=0
    _SHELL_IS_LINUX=1
    ;;
  *)
    export SHELL_OS='Other'
    _SHELL_IS_MACOS=0
    _SHELL_IS_LINUX=0
    ;;
esac

# macOS — bundled with KiCad.app
if [[ "$_SHELL_IS_MACOS" -eq 1 ]]; then
    export HDM_KICAD_PYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
fi


# Linux — often /usr/lib/kicad/bin/python3
if [[ "$_SHELL_IS_LINUX" -eq 1 ]]; then
    export HDM_KICAD_PYTHON=/usr/lib/kicad/bin/python3
fi

_evalBg() {
    eval "$@" &>/dev/null & disown;
}

# Kill only the process LISTENING on the blpl serve port (macOS/Linux).
# -sTCP:LISTEN excludes clients (e.g. Vivaldi) that merely have the page open.
# lsof -t prints only PIDs — no header, no column parsing needed.
_kill_port() {
    local port="${1:-7878}"
    local pids
    pids="$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null)" || true
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        echo "Killed listener(s) on port ${port}: ${pids}"
    fi
}

source ${HOME}/devspace/myprojects/blpl/.venv/bin/activate
readenv

# Projects are imported from the browser now, so this script no longer pins one.
# It used to hardcode a single board's directory, which meant "open a different
# project" was: edit this file, kill the server, start it again.
#
# Pass a projects directory to override for one run:
#     ./blpl-serve.sh ~/projects/hardware
# Otherwise `blpl serve` uses $BLPL_PROJECTS_ROOT, else its XDG data dir.
_kill_port 7878
if [[ -n "${1:-}" ]]; then
    cmd="blpl serve --workspace ${1}"
else
    cmd="blpl serve"
fi
_evalBg "${cmd}";

