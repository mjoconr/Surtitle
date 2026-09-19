#!/bin/sh
# The checks that must pass before anything is committed.
set -eu
python3 -c "import ast, pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('src').rglob('*.py')]"
echo "checks passed"
