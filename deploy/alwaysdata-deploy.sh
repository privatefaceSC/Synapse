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
