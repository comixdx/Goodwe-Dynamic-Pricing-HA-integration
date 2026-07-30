#!/usr/bin/env bash
#
# Publică integrarea pe GitHub și creează release-ul pe care îl citește HACS.
#
#   ./publish.sh [nume-repo]
#
# Utilizatorul se ia din `gh auth status`, deci nu trebuie scris nicăieri și
# niciun token nu ajunge în vreun fișier. Cere GitHub CLI autentificat:
#   gh auth login
#
set -euo pipefail

REPO_NAME="${1:-goodwe-ems-hacs}"
VERSION="$(sed -n 's/.*"version": "\(.*\)".*/\1/p' custom_components/goodwe_ems/manifest.json)"

command -v gh >/dev/null || { echo "Lipsește GitHub CLI: https://cli.github.com"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Rulează întâi: gh auth login"; exit 1; }

USER_LOGIN="$(gh api user --jq .login)"
echo "==> Utilizator : $USER_LOGIN"
echo "==> Repo       : $USER_LOGIN/$REPO_NAME"
echo "==> Versiune   : v$VERSION"
echo

if gh repo view "$USER_LOGIN/$REPO_NAME" >/dev/null 2>&1; then
  echo "Repo-ul există deja. Oprește-te sau alege alt nume."; exit 1
fi

read -rp "Se creează un repo PUBLIC cu conținutul de mai sus. Continui? [y/N] " ok
[[ "$ok" == [yY] ]] || { echo "Anulat."; exit 0; }

# --- Câmpurile pe care le verifică hassfest și HACS -------------------------
MANIFEST="custom_components/goodwe_ems/manifest.json"
sed -i.bak \
  -e "s|\"codeowners\": \[\"@[^\"]*\"\]|\"codeowners\": [\"@$USER_LOGIN\"]|" \
  -e "s|\"documentation\": \"[^\"]*\"|\"documentation\": \"https://github.com/$USER_LOGIN/$REPO_NAME\"|" \
  -e "s|\"issue_tracker\": \"[^\"]*\"|\"issue_tracker\": \"https://github.com/$USER_LOGIN/$REPO_NAME/issues\"|" \
  "$MANIFEST"
rm -f "$MANIFEST.bak"
python3 -c "import json,sys; json.load(open('$MANIFEST'))" \
  || { echo "manifest.json a ieșit invalid, verifică-l manual"; exit 1; }
echo "==> manifest.json actualizat"

# --- Git --------------------------------------------------------------------
[ -d .git ] || git init -q -b main
git add -A
git commit -qm "GoodWe EMS v$VERSION" || echo "==> Nimic nou de comis"

gh repo create "$REPO_NAME" --public --source=. --remote=origin --push
echo "==> Cod urcat"

# --- Release (fără el, HACS oferă doar branch-ul principal) -----------------
git tag -f "v$VERSION"
git push -f origin "v$VERSION"
gh release create "v$VERSION" \
  --title "v$VERSION" \
  --notes "Integrare GoodWe EMS: control invertor, telemetrie, dispecerizare pe preț PZU."
echo "==> Release v$VERSION publicat"

echo
echo "Gata. În HACS: Custom repositories -> https://github.com/$USER_LOGIN/$REPO_NAME -> categoria Integration"
