#!/bin/sh
# Vercel build: copy the site into public/. Works whether Vercel starts in
# the repository root or inside website/ (the project's Root Directory).
set -e
echo "Build folder: $(pwd)"
if [ -f website/index.html ]; then SRC=website
elif [ -f index.html ]; then SRC=.
else echo "index.html not found from $(pwd)"; ls -la; exit 1
fi
echo "Site source: $SRC"
mkdir -p public
cp "$SRC/index.html" public/index.html
ls -la public
