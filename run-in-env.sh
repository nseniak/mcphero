#!/usr/bin/env bash
# Prepare the Python environment, then optionally run a command in it.
#
# This repo's dependency manager is Poetry. Create an environment however
# you prefer (venv, conda, uv, ...), run `poetry install` into it, and
# activate it before invoking the project's scripts. This wrapper assumes
# the active Python is already the project environment.
#
# For local convenience, drop a gitignored `run-in-env.local.sh` beside
# this file to activate your environment automatically (e.g. a
# `conda activate`); it is sourced here if present.
#
# Usage:
#   bash run-in-env.sh <command> [args...]   # run command in the env
#   source run-in-env.sh                     # prepare env in current shell

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${_here}/run-in-env.local.sh" ]; then
    # shellcheck disable=SC1091
    source "${_here}/run-in-env.local.sh"
fi

# When executed directly (not sourced), run the provided command.
if [ "${BASH_SOURCE[0]}" = "$0" ] && [ $# -gt 0 ]; then
    exec "$@"
fi
