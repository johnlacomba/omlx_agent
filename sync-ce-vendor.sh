#!/usr/bin/env bash
# Refresh the vendored compound-engineering plugin sources.
#
# omlx_agent vendors the official EveryInc/compound-engineering-plugin
# agents/ and skills/ directories so it can stay in lockstep parity with
# upstream prompts. This script clones the upstream repo and overwrites
# our local copy.
#
# Pinned SHA is recorded in compound-engineering/VENDOR_SHA.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="$SCRIPT_DIR/compound-engineering"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

REF="${1:-main}"

echo "[ce-vendor] Cloning EveryInc/compound-engineering-plugin@${REF}..."
git clone --depth 1 --branch "$REF" \
  https://github.com/EveryInc/compound-engineering-plugin.git \
  "$TMP_DIR/plugin"

UPSTREAM_SHA="$(cd "$TMP_DIR/plugin" && git rev-parse HEAD)"

echo "[ce-vendor] Replacing vendored agents/ and skills/..."
rm -rf "$VENDOR_DIR/agents" "$VENDOR_DIR/skills"
mkdir -p "$VENDOR_DIR"
cp -R "$TMP_DIR/plugin/plugins/compound-engineering/agents" "$VENDOR_DIR/agents"
cp -R "$TMP_DIR/plugin/plugins/compound-engineering/skills" "$VENDOR_DIR/skills"

echo "$UPSTREAM_SHA" > "$VENDOR_DIR/VENDOR_SHA"

AGENT_COUNT="$(ls "$VENDOR_DIR/agents" | wc -l | tr -d ' ')"
SKILL_COUNT="$(ls "$VENDOR_DIR/skills" | wc -l | tr -d ' ')"

echo "[ce-vendor] Done. Pinned to $UPSTREAM_SHA"
echo "[ce-vendor] Agents: $AGENT_COUNT, Skills: $SKILL_COUNT"
