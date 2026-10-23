#!/usr/bin/env bash
# Publishes a run's screenshots on the `screenshots` branch and, for a pull request, posts them on it
# next to main's. Run by .github/workflows/screenshots.yml; one comment per pull request, updated in
# place. Adapted from clementine-player/Clementine's tools/screenshots/post_screenshots.sh, which
# hosts its images in an R2 bucket: here they're committed with the job's GITHUB_TOKEN instead, so
# there's no stored secret.
#
# Usage: post_screenshots.sh <dir> <pull request number | main>
#
# <dir> has light/*.png and dark/*.png (scripts/screenshots.py). They replace pr-<n>/ (or main/) on
# the branch, and the comment links them by commit, so GitHub's image cache never shows an earlier
# run's and old comments keep theirs.
#
# Needs: GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_SERVER_URL, GH_TOKEN, the GitHub CLI, and a
# checkout of the repository (with actions/checkout's credentials) as the working directory.
set -euo pipefail
export LC_ALL=C

dir=$(realpath "$1")
target=$2
branch=screenshots
marker='<!-- screenshots -->'
# How wide each screenshot is shown, four to a row; they're 1280 wide.
width=240

if ! compgen -G "$dir/light/*.png" > /dev/null; then
  echo "No screenshots to post"
  exit 0
fi
folder=$([ "$target" = main ] && echo main || echo "pr-$target")

# A worktree of the branch, which shares the checkout's credentials. The first run starts it.
work=$RUNNER_TEMP/screenshots-branch
if git fetch --depth=1 origin "$branch"; then
  git worktree add --detach "$work" FETCH_HEAD
else
  git worktree add --orphan -b "$branch" "$work"
  printf '%s\n' "Screenshots for pull requests, from .github/workflows/screenshots.yml. Not code." > "$work/README.md"
fi
git -C "$work" config user.name "github-actions[bot]"
git -C "$work" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
rm -rf "${work:?}/$folder"
mkdir -p "$work/$folder"
cp -R "$dir/light" "$dir/dark" "$work/$folder/"
git -C "$work" add -A
git -C "$work" commit -q -m "Screenshots for $folder from run $GITHUB_RUN_ID" || echo "Same screenshots as before"

# Other pull requests' runs push to the branch too: on a race, replay this commit onto theirs.
for attempt in 1 2 3 4 5; do
  if git -C "$work" push -q origin "HEAD:refs/heads/$branch"; then
    break
  fi
  [ "$attempt" = 5 ] && exit 1
  sleep $((attempt * 2))
  git -C "$work" fetch -q --depth=1 origin "$branch"
  git -C "$work" rebase -q FETCH_HEAD
done
sha=$(git -C "$work" rev-parse HEAD)
echo "Screenshots are on $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/tree/$sha/$folder"
[ "$target" = main ] && exit 0

# github.com URLs, not raw.githubusercontent.com: those render for anyone who can see the
# repository, private or not.
raw="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/raw/$sha"
img() { echo "<img src=\"$raw/$1\" width=\"$width\">"; }

# A cell with main's $1 (light/01-login.png, say), or a dash if main has none yet.
main_cell() {
  if [ -f "$work/main/$1" ]; then img "main/$1"; else echo "–"; fi
}

run="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
{
  echo "$marker"
  echo "### Screenshots"
  echo
  echo "From [run $GITHUB_RUN_ID]($run), with the fixture data in \`scripts/screenshots.py\`. Light, then dark: main next to this pull request."
  echo
  echo "| Page | main | This PR | main, dark | This PR, dark |"
  echo "| --- | --- | --- | --- | --- |"
  for shot in "$work/$folder/light"/*.png; do
    name=$(basename "$shot")
    cells=""
    for scheme in light dark; do
      cells+=" $(main_cell "$scheme/$name") |"
      if [ -f "$work/$folder/$scheme/$name" ]; then cells+=" $(img "$folder/$scheme/$name") |"; else cells+=" – |"; fi
    done
    base=${name%.png}
    echo "| \`${base#*-}\` |$cells"
  done
} > "$RUNNER_TEMP/screenshots-comment.md"

existing=$(gh api "repos/$GITHUB_REPOSITORY/issues/$target/comments" --paginate \
  --jq ".[] | select(.user.login == \"github-actions[bot]\" and (.body | startswith(\"$marker\"))) | .id" \
  | head -n 1)
if [ -n "$existing" ]; then
  gh api -X PATCH "repos/$GITHUB_REPOSITORY/issues/comments/$existing" \
    -F "body=@$RUNNER_TEMP/screenshots-comment.md" > /dev/null
else
  gh api "repos/$GITHUB_REPOSITORY/issues/$target/comments" \
    -F "body=@$RUNNER_TEMP/screenshots-comment.md" > /dev/null
fi
