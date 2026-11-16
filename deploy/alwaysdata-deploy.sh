#!/usr/bin/env bash
set -euo pipefail

expected_sha="${1:?expected commit SHA required}"
if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Invalid commit SHA' >&2
  exit 2
fi

cd "$HOME/synapse"
if [[ "$(git branch --show-current)" != dev ]]; then
  echo 'Server checkout is not on dev' >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo 'Server checkout has tracked local changes; refusing to overwrite them' >&2
  exit 1
fi

git fetch origin dev
fetched_sha="$(git rev-parse FETCH_HEAD)"
if [[ "$fetched_sha" != "$expected_sha" ]]; then
  echo "origin/dev moved to $fetched_sha while deploying $expected_sha; a newer run will deploy it" >&2
  exit 1
fi

if ! git diff --quiet HEAD FETCH_HEAD -- requirements.txt; then
  if [[ ! -x .venv/bin/python ]]; then
    echo 'Server Python virtualenv is missing at $HOME/synapse/.venv' >&2
    exit 1
  fi
  requirements_file="$(mktemp "$HOME/admin/tmp/synapse-requirements.XXXXXX")"
  trap 'rm -f "$requirements_file"' EXIT
  git show FETCH_HEAD:requirements.txt > "$requirements_file"
  .venv/bin/python -m pip install --disable-pip-version-check --no-input \
    -r "$requirements_file"
  echo 'Server dependencies updated'
fi

git merge --ff-only FETCH_HEAD
test "$(git rev-parse HEAD)" = "$expected_sha"
echo "Checked out $expected_sha"

# Одноразовая уборка дубликата, добавленного вручную при настройке CI.
# Удаляем только если файл содержит ровно эту одну строку.
temporary_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHY+gFkDWOhJt7aQJkkjUQJ/de/lRrhhDD/g97fU5OGK synapse-github-actions-2026-11'
if [[ -f "$HOME/.ssh/authorized_keys" ]] &&
   [[ "$(wc -l < "$HOME/.ssh/authorized_keys")" -eq 1 ]] &&
   grep -Fxq "$temporary_key" "$HOME/.ssh/authorized_keys"; then
  rm -- "$HOME/.ssh/authorized_keys"
  rmdir -- "$HOME/.ssh" 2>/dev/null || true
  echo 'Removed duplicate authorized_keys entry'
fi
