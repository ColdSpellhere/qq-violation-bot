#!/usr/bin/env bash
set -euo pipefail

server_remote="${CARROT_SERVER_REMOTE:-server}"
deploy_host="${CARROT_DEPLOY_HOST:?CARROT_DEPLOY_HOST is required}"
deploy_user="${CARROT_DEPLOY_USER:?CARROT_DEPLOY_USER is required}"

[[ -z "$(git status --porcelain)" ]] || { echo "worktree must be clean" >&2; exit 1; }
candidate_sha="$(git rev-parse --verify HEAD)"
[[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "HEAD is not a full commit" >&2; exit 1; }
remote_url="$(git remote get-url "$server_remote")"
if [[ "$remote_url" == *github.com* ]]; then
  echo "refusing GitHub remote for CArroT candidate" >&2
  exit 1
fi

test_group_id="$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')"
while git grep -q "$test_group_id"; do
  test_group_id="$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')"
done
export TARGET_GROUP_ID="$test_group_id"
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q bot.py plugins scripts tests
.venv/bin/python scripts/check_public_tree.py
.venv/bin/python scripts/check_public_tree.py --history

git push "$server_remote" "HEAD:refs/heads/release/carrot-candidate"
ssh -o BatchMode=yes "$deploy_user@$deploy_host" \
  "sudo /opt/qq-bots/repository/.venv/bin/python /opt/qq-bots/repository/scripts/deploy_instance.py --instance carrot --sha '$candidate_sha'"
