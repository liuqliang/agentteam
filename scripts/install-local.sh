#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="${HOME}/.local/bin"
state_dir="${HOME}/.local/share/agentteam"
launcher="${repo_root}/agentteam"
target="${bin_dir}/agentteam"

mkdir -p "${bin_dir}"
mkdir -p "${state_dir}"
cp "${launcher}" "${target}"
chmod +x "${target}"
printf '{"development_repo_root": "%s"}\n' "${repo_root}" > "${state_dir}/launcher.json"

printf 'Installed %s from %s\n' "${target}" "${launcher}"
printf 'Make sure %s is on PATH.\n' "${bin_dir}"
