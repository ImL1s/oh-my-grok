#!/usr/bin/env bash
# Immutable oh-my-grok installer.
#
# Convenient path (no checkout):
#   curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/install.sh | bash
#
# Explicit manual/offline path (same Python transaction engine):
#   bash install.sh --offline --archive ./oh-my-grok-0.6.0.tar.gz --checksums ./SHA256SUMS
set -euo pipefail

REPOSITORY="${OMG_INSTALL_REPOSITORY:-ImL1s/oh-my-grok}"
# The online path resolves ``latest`` once, validates an exact semantic tag,
# then downloads both files from that immutable tag.  Never mix the mutable
# ``releases/latest/download`` alias across two requests.
LATEST_API_URL="${OMG_INSTALL_LATEST_API_URL:-https://api.github.com/repos/${REPOSITORY}/releases/latest}"
RELEASES_URL="${OMG_INSTALL_RELEASES_URL:-https://github.com/${REPOSITORY}/releases}"
ARCHIVE=""
CHECKSUMS=""
EXPECTED_SHA=""
OFFLINE=0
SOURCE_TAG="${OMG_INSTALL_TAG:-}"
SOURCE_URI="${RELEASES_URL}"

usage() {
  cat <<'EOF'
Usage:
  install.sh
  install.sh --archive ./oh-my-grok-X.Y.Z.tar.gz --checksums ./SHA256SUMS [--offline]
  install.sh --archive ./oh-my-grok-X.Y.Z.tar.gz --asset-sha256 <sha256> [--offline]

Without --archive, the installer resolves the latest GitHub release to one
exact tag, then downloads SHA256SUMS and its archive only from that tag.
Manual/offline mode performs the same verify -> immutable stage -> plugin/CLI
switch -> strict doctor -> receipt transaction; it is not a shortcut around
verification or rollback.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) [[ $# -ge 2 ]] || { echo "error: --archive requires a path" >&2; exit 2; }; ARCHIVE="$2"; shift 2 ;;
    --checksums) [[ $# -ge 2 ]] || { echo "error: --checksums requires a path" >&2; exit 2; }; CHECKSUMS="$2"; shift 2 ;;
    --asset-sha256) [[ $# -ge 2 ]] || { echo "error: --asset-sha256 requires a digest" >&2; exit 2; }; EXPECTED_SHA="$2"; shift 2 ;;
    --source-tag) [[ $# -ge 2 ]] || { echo "error: --source-tag requires a tag" >&2; exit 2; }; SOURCE_TAG="$2"; shift 2 ;;
    --offline) OFFLINE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "error: python3 >= 3.11 is required" >&2; exit 1; }
PY_MAJOR_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' \
  || { echo "error: python ${PY_MAJOR_MINOR} < 3.11" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/omg-install.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT HUP INT TERM

download() {
  local url="$1" destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      --retry 3 --connect-timeout 15 --max-time 300 --output "$destination" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget --https-only --timeout=300 --tries=3 --output-document="$destination" "$url"
  else
    echo "error: curl or wget is required for the online install path" >&2
    return 1
  fi
}

if [[ -z "$ARCHIVE" ]]; then
  [[ "$OFFLINE" -eq 0 ]] || { echo "error: --offline requires --archive" >&2; exit 2; }
  if [[ -z "$SOURCE_TAG" ]]; then
    RELEASE_JSON="$WORK/latest-release.json"
    echo "==> resolving latest release to one immutable tag"
    download "$LATEST_API_URL" "$RELEASE_JSON"
    SOURCE_TAG="$(python3 - "$RELEASE_JSON" <<'PY'
import json, re, sys
from pathlib import Path

try:
    value=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f'invalid latest-release response: {type(exc).__name__}')
tag=value.get('tag_name') if isinstance(value,dict) else None
semver=r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
if not isinstance(tag,str) or re.fullmatch(r'v'+semver,tag) is None:
    raise SystemExit('latest release tag must be v<semantic-version>')
print(tag)
PY
)"
  fi
  python3 - "$SOURCE_TAG" <<'PY'
import re, sys
semver=r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
if re.fullmatch(r'v'+semver,sys.argv[1]) is None:
    raise SystemExit('release tag must be v<semantic-version>')
PY
  DOWNLOAD_BASE="${RELEASES_URL%/}/download/${SOURCE_TAG}"
  SOURCE_URI="${RELEASES_URL%/}/tag/${SOURCE_TAG}"
  CHECKSUMS="$WORK/SHA256SUMS"
  echo "==> downloading checksum manifest for ${SOURCE_TAG}"
  download "$DOWNLOAD_BASE/SHA256SUMS" "$CHECKSUMS"
  ASSET_NAME="$(python3 - "$CHECKSUMS" <<'PY'
import re, sys
from pathlib import Path
rows=[]
for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
    m=re.fullmatch(r'([0-9A-Fa-f]{64})[ \t]+[*]?(oh-my-grok-[0-9A-Za-z.+-]+\.tar\.gz)', line)
    if m: rows.append(m.group(2))
if len(rows) != 1:
    raise SystemExit('SHA256SUMS must name exactly one oh-my-grok archive')
print(rows[0])
PY
)"
  ASSET_VERSION="${ASSET_NAME#oh-my-grok-}"
  ASSET_VERSION="${ASSET_VERSION%.tar.gz}"
  [[ "v${ASSET_VERSION}" == "$SOURCE_TAG" ]] || {
    echo "error: release tag ${SOURCE_TAG} differs from checksum archive ${ASSET_NAME}" >&2
    exit 1
  }
  ARCHIVE="$WORK/$ASSET_NAME"
  echo "==> downloading $ASSET_NAME"
  download "$DOWNLOAD_BASE/$ASSET_NAME" "$ARCHIVE"
else
  ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd -P)/$(basename "$ARCHIVE")"
fi

[[ -f "$ARCHIVE" ]] || { echo "error: archive not found" >&2; exit 1; }
ARCHIVE_NAME="$(basename "$ARCHIVE")"
if [[ -z "$SOURCE_TAG" && "$ARCHIVE_NAME" =~ ^oh-my-grok-([0-9A-Za-z.+-]+)\.tar\.gz$ ]]; then
  SOURCE_TAG="v${BASH_REMATCH[1]}"
fi
if [[ -n "$CHECKSUMS" ]]; then
  CHECKSUMS="$(cd "$(dirname "$CHECKSUMS")" && pwd -P)/$(basename "$CHECKSUMS")"
  [[ -f "$CHECKSUMS" ]] || { echo "error: SHA256SUMS not found" >&2; exit 1; }
fi
[[ -n "$CHECKSUMS" || -n "$EXPECTED_SHA" ]] \
  || { echo "error: archive install requires --checksums or --asset-sha256" >&2; exit 2; }

echo "==> verifying archive before extraction"
# Bootstrap verification is intentionally independent from archive code.  The
# extracted setup_cmd repeats this check before the transaction begins.
ASSET_SHA="$(python3 - "$ARCHIVE" "$CHECKSUMS" "$EXPECTED_SHA" <<'PY'
import hashlib, re, sys
from pathlib import Path
asset=Path(sys.argv[1]); sums=sys.argv[2]; explicit=sys.argv[3].lower()
h=hashlib.sha256()
with asset.open('rb') as f:
    for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
actual=h.hexdigest(); expected=[]
if sums:
    matches=[]
    for line in Path(sums).read_text(encoding='utf-8').splitlines():
        m=re.fullmatch(r'([0-9A-Fa-f]{64})[ \t]+[*]?([^\r\n]+)', line)
        if not m: raise SystemExit('malformed SHA256SUMS')
        if m.group(2)==asset.name: matches.append(m.group(1).lower())
    if len(matches)!=1: raise SystemExit('SHA256SUMS must contain exactly one archive record')
    expected += matches
if explicit:
    if not re.fullmatch(r'[0-9a-f]{64}', explicit): raise SystemExit('invalid explicit SHA-256')
    expected.append(explicit)
if not expected or any(value != actual for value in expected): raise SystemExit('archive checksum mismatch')
print(actual)
PY
)"

echo "==> extracting bounded link-free archive"
UNPACK="$WORK/unpack"
mkdir -p "$UNPACK"
PACKAGE_ROOT="$(python3 - "$ARCHIVE" "$UNPACK" <<'PY'
import json, pathlib, shutil, sys, tarfile
asset=pathlib.Path(sys.argv[1]); out=pathlib.Path(sys.argv[2]).resolve(); total=0
with tarfile.open(asset, 'r:gz') as tf:
    members=tf.getmembers()
    if not members or len(members)>50000: raise SystemExit('unsafe archive member count')
    for m in members:
        name=m.name.rstrip('/'); p=pathlib.PurePosixPath(name)
        if not name or p.is_absolute() or '..' in p.parts or str(p)!=name or m.issym() or m.islnk() or not (m.isdir() or m.isfile()):
            raise SystemExit(f'unsafe archive member: {m.name!r}')
        total += m.size
        if total>512*1024*1024 or m.size>64*1024*1024: raise SystemExit('unsafe archive size')
        target=out.joinpath(*p.parts)
        if m.isdir(): target.mkdir(parents=True, exist_ok=True); continue
        target.parent.mkdir(parents=True, exist_ok=True)
        src=tf.extractfile(m)
        if src is None: raise SystemExit('missing archive payload')
        with src, target.open('wb') as dst: shutil.copyfileobj(src,dst,1024*1024)
        target.chmod(0o755 if m.mode & 0o111 else 0o644)
candidates=[]
for plugin in out.rglob('plugin.json'):
    try: data=json.loads(plugin.read_text(encoding='utf-8'))
    except Exception: continue
    if isinstance(data,dict) and data.get('name')=='oh-my-grok': candidates.append(plugin.parent.resolve())
if len(candidates)!=1: raise SystemExit('archive must contain exactly one oh-my-grok root')
print(candidates[0])
PY
)"

INSTALL_ARGS=(install-release --source-root "$PACKAGE_ROOT" --asset "$ARCHIVE" --asset-sha256 "$ASSET_SHA")
[[ -z "$CHECKSUMS" ]] || INSTALL_ARGS+=(--checksums "$CHECKSUMS")
[[ -z "$SOURCE_TAG" ]] || INSTALL_ARGS+=(--source-tag "$SOURCE_TAG")
INSTALL_ARGS+=(--source-uri "$SOURCE_URI")

echo "==> immutable stage -> Grok plugin/CLI switch -> strict doctor -> receipt"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PACKAGE_ROOT" \
  python3 -m omg_cli.setup_cmd "${INSTALL_ARGS[@]}"

# This banner is deliberately after the hard-gated Python transaction.  With
# set -e it is unreachable after checksum, switch, doctor or rollback failure.
echo "==> installed and exactly verified"
echo "    restart Grok Build, then run: omg setup && omg doctor --strict"
