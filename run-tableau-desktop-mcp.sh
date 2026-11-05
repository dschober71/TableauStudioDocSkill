#!/usr/bin/env bash
# Starts the Tableau Studio / Desktop authoring MCP server bundled with Studio.
# The binary lives in a versioned folder, so pick the newest one at startup.
set -euo pipefail

base="$HOME/Library/Application Support/Tableau/Tableau/WorkspaceExtensionApplications/TableauAgent/versions"

bin="$(ls -d "$base"/*/bin/tableau-mcp-desktop 2>/dev/null | sort -t/ -k1,1 -V | tail -n 1 || true)"
if [ -z "$bin" ]; then
  echo "No tableau-mcp-desktop found under $base. Is Tableau Studio installed?" >&2
  exit 1
fi

export TRANSPORT=stdio
export TABLEAU_EXTERNAL_API=true

exec "$bin"
