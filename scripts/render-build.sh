#!/usr/bin/env bash
# Native Render Python runtime: install Node, build Vue, then pip install.
set -euo pipefail
NODE_VERSION="${NODE_VERSION:-22.11.0}"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" | tar -xJ
  export PATH="$PWD/node-v${NODE_VERSION}-linux-x64/bin:$PATH"
fi
node -v
npm -v
(cd web && npm ci && npm run build)
pip install -r requirements.txt
