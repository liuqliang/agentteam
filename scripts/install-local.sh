#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="${HOME}/.local/bin"
launcher="${repo_root}/agentteam"
target="${bin_dir}/agentteam"

mkdir -p "${bin_dir}"
ln -sfn "${launcher}" "${target}"

printf 'Installed %s -> %s\n' "${target}" "${launcher}"
printf 'Make sure %s is on PATH.\n' "${bin_dir}"
