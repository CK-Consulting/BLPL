#!/bin/bash
_evalBg() {
    eval "$@" &>/dev/null & disown;
}
source ${HOME}/projects/blpl/.venv/activate

cmd="blpl serve"
_evalBg "${cmd}";

