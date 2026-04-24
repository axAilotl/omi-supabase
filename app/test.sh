#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

missing_files=()
required_files=(
  "lib/env/dev_env.g.dart"
  "lib/env/prod_env.g.dart"
)

for file in "${required_files[@]}"; do
  if [[ ! -f "$file" ]]; then
    missing_files+=("$file")
  fi
done

if [[ ${#missing_files[@]} -gt 0 ]]; then
  echo "Missing generated files: ${missing_files[*]}"
  echo "Running setup prerequisites..."

  echo "API_BASE_URL=https://api.omiapi.com/" > .dev.env
  echo "USE_WEB_AUTH=true" >> .dev.env
  echo "USE_AUTH_CUSTOM_TOKEN=true" >> .dev.env
  echo "STAGING_API_URL=" >> .dev.env

  flutter pub get
  dart run build_runner build --delete-conflicting-outputs
fi

flutter test
