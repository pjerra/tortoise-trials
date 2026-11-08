#!/bin/bash
exec python3 -I "$(dirname "$(readlink -f "$0")")/trials.py" "$@"
