#!/bin/bash
# deploy-addon.sh — commit + push rápido para ha-addons
# Uso: ./deploy-addon.sh "mensagem do commit"

set -e

cd /mnt/FASTTRUENAS/ha-addons

if [ -z "$1" ]; then
  echo "Erro: mensagem de commit obrigatória"
  echo "Uso: ./deploy-addon.sh \"mensagem\""
  exit 1
fi

# Mostra o que vai ser commitado
echo "=== Alterações ==="
git status --short

if [ -z "$(git status --porcelain)" ]; then
  echo "Nada para commitar."
  exit 0
fi

git add -A
git commit -m "$1"
git push origin main

echo ""
echo "✅ Push feito. Vai ao Supervisor → Add-on Store → ⋮ → Reload para forçar update."
