#!/bin/bash
# Pre-commit key hygiene: fail if a LlamaCloud-style key appears in tracked files.
if git grep -q "llx-" -- ':!.env' . 2>/dev/null; then
  echo "BLOCKED: possible API key in tracked files:"; git grep -l "llx-" -- ':!.env' .; exit 1
fi
echo "key check OK"
