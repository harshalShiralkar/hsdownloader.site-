#!/usr/bin/env bash
set -o errexit

# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Deno INSIDE the project folder so it is still there at runtime.
#    (DENO_INSTALL must be set BEFORE running the installer.)
export DENO_INSTALL="$PWD/.deno"
curl -fsSL https://deno.land/install.sh | sh -s -- -y

# 3. Confirm it installed
"$DENO_INSTALL/bin/deno" --version
