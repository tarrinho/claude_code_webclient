#!/bin/bash
set -euo pipefail

echo "=== Deploy WebConsole to Kali3 ==="
echo "Target: 100.119.178.29 (Kali3)"
echo ""

FILES=(
    "bin/wc-claude.sh"
    "bin/wc-backend-env.py"
    "backend_env.py"
)

echo "Files to copy to Kali3:"
for f in "${FILES[@]}"; do
    echo "  $f"
done

echo ""
echo "Run on Kali3:"
echo "  1. scp the 3 files above to /home/kali/projects/claude-code-webconsole/"
echo "  2. Copy patched DB: scp data/webconsole.db kali@100.119.178.29:~/"
echo "  3. mv ~/webconsole.db /home/kali/projects/claude-code-webconsole/data/"
echo "  4. echo 'alias claude="\$HOME/projects/claude-code-webconsole/bin/wc-claude.sh"' >> ~/.bashrc"
echo "  5. source ~/.bashrc  (or open a new shell)"
echo "  6. Restart claude_proxy.py"
