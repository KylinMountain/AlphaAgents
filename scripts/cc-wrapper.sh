#!/bin/bash
# Wrapper script to run open-source Claude Code with OpenAI-compatible provider
# Used by Claude Agent SDK via cli_path option

exec bun run --define 'MACRO={"VERSION":"2.1.0","PACKAGE_URL":""}' \
  /Users/evilkylin/Projects/claude-code/src/entrypoints/cli.tsx \
  "$@"
