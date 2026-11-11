# 一键推到 GitHub（在 Git Bash 里运行）
# 用法： bash push.sh 你的GitHub用户名
# 前置：已在该用户名下创建空仓库 wenjia-home-fix-mcp（public，不要勾 README）
set -euo pipefail
GH_USER="${1:?用法: bash push.sh 你的GitHub用户名}"
cd "$(dirname "$0")"

# 把占位符换成真实用户名（server.json / glama.json）；README 里也换
for f in server.json glama.json; do
  [ -f "$f" ] && sed -i "s/YOUR-GITHUB-USERNAME/${GH_USER}/g" "$f"
done

git init -q 2>/dev/null || true
git config user.name "Wenjia"
git config user.email "noreply@wenjia.net"
git add -A
git commit -qm "Wenjia home-fix MCP server v0.1.0 (40 categories, 200 symptoms, CC BY 4.0 data)" || echo "（无新变更）"
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${GH_USER}/wenjia-home-fix-mcp.git"
git push -u origin main

echo
echo "[OK] 已推送： https://github.com/${GH_USER}/wenjia-home-fix-mcp"
echo "下一步：仓库 Settings → Topics 添加 mcp-server（Glama 靠这个索引）"
