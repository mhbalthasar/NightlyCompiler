#!/usr/bin/env bash
# Upload one build unit's outputs to S3 instead of an Actions artifact.
#
# Lives in a script, not inline in the workflow, because the three build-lN jobs
# are already byte-identical copies of each other: pasting a 30-line block into
# all three would give exactly the "same thing written three times, one edited"
# failure mode this repo's own comments warn about.
#
# Mirrors actions/upload-artifact@v4 semantics that ci.yml relies on:
#   * one archive per upload name, whose contents are relative to the source
#     path, not including it           -> one sub-prefix per name
#   * if-no-files-found: error          -> read the prefix back and fail on 0
#                                          objects (aws s3 sync exits 0 on an
#                                          empty source, so this must be explicit)
#
# Env in: DP_S3_PREFIX BUCKET S3_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
#         NAME PATHS IF_NO_FILES
set -euo pipefail

for v in DP_S3_PREFIX BUCKET S3_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY NAME; do
  [ -n "${!v:-}" ] || { echo "::error::$v is unset (S3 mode needs the four TIGRIS_* secrets)" >&2; exit 1; }
done
# Hard guard rather than letting sync fail obscurely: if the runner image stops
# shipping the CLI, this reports a sentence instead of silently uploading nothing.
command -v aws >/dev/null 2>&1 || { echo "::error::aws CLI not found on this runner" >&2; exit 1; }
# Otherwise the CLI asks EC2 IMDS for credentials and only fails after a long
# timeout, which turns a missing secret into a mysterious stall.
export AWS_EC2_METADATA_DISABLED=true

DEST="s3://$BUCKET/$DP_S3_PREFIX/$NAME/"
srcs=()
while IFS= read -r p; do
  [ -n "$p" ] && srcs+=("$p")
done <<< "${PATHS:-}"
[ "${#srcs[@]}" -gt 0 ] || { echo "::error::no upload paths for $NAME" >&2; exit 1; }

for p in "${srcs[@]}"; do
  if [ -d "$p" ]; then
    # ci.yml writes `path: build/x/` with a trailing slash; strip it so the sync
    # source does not come out as `build/x//`.
    echo "sync ${p%/}/ -> $DEST"
    aws s3 sync "${p%/}/" "$DEST" --endpoint-url "$S3_URL" --only-show-errors --no-progress
  elif [ -f "$p" ]; then
    # sync takes a directory only; ci.yml's bundle unit uploads a single .zip.
    echo "cp $p -> $DEST$(basename "$p")"
    aws s3 cp "$p" "$DEST$(basename "$p")" --endpoint-url "$S3_URL" --only-show-errors --no-progress
  else
    echo "::error::upload path '$p' is neither a file nor a directory" >&2
    exit 1
  fi
done

# Read-back by name: sync "succeeding" against an empty or mistyped prefix is a
# real failure mode, and it looks exactly like success.
n="$(aws s3 ls "$DEST" --recursive --endpoint-url "$S3_URL" | grep -c . || true)"
if [ "${n:-0}" = "0" ]; then
  if [ "${IF_NO_FILES:-error}" = "error" ]; then
    echo "::error::$NAME uploaded 0 objects to $DEST" >&2
    exit 1
  fi
  echo "::warning::$NAME uploaded 0 objects to $DEST"
fi
echo "ok: $n object(s) at $DEST"

# Artifacts are downloadable from the run page; objects are not, and the long
# lived keys stay on the server side (GitHub secrets are write-only, so a local
# script cannot sign one). Hand back a short-lived URL for each loose file.
if [ "${#srcs[@]}" = "1" ] && [ -f "${srcs[0]}" ]; then
  obj="$DEST$(basename "${srcs[0]}")"
  url="$(aws s3 presign "$obj" --expires-in 3600 --endpoint-url "$S3_URL")"
  [ -n "$url" ] || { echo "::error::presign failed for $obj" >&2; exit 1; }
  {
    echo "### $NAME -> S3"
    echo
    echo '```'
    echo "$obj"
    echo '```'
    echo
    echo "**可直接下载（1 小时有效）**："
    echo
    echo '```'
    echo "curl -L -o '$(basename "${srcs[0]}")' '$url'"
    echo '```'
  } >> "$GITHUB_STEP_SUMMARY"
fi
