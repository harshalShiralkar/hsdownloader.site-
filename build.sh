#!/usr/bin/env bash
set -o errexit

# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Deno (Required for yt-dlp to bypass bot challenges & generate tokens)
curl -fsSL https://deno.land/install.sh | sh

# 3. Add Deno to the system PATH for the build and runtime environments
export DENO_INSTALL="/opt/render/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"
