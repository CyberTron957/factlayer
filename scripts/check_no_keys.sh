#!/bin/bash
# Pre-commit key hygiene: fail if a LlamaCloud-style key (llx- + 10 or more
# key characters) appears in tracked files. The bare prefix in .gitignore
# patterns does not match.
if git grep -qE "llx-[A-Za-z0-9]{10,}" -- . 2>/dev/null; then
  echo "BLOCKED: possible API key in tracked files:"; git grep -lE "llx-[A-Za-z0-9]{10,}" -- .; exit 1
fi
echo "key check OK"
