#!/bin/bash
# NOTE4 构建与待评审看板 · 安装脚本
# 作用：检查依赖 → 准备 SDK → 编译推送程序 → 把 token 存进钥匙串 → 生成两张预览图
# 不会自动推送、不改固件、不写入开机自启。
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SDK_DIR="$APP_DIR/zectrix-local-push-sdk"

echo "==> [1/5] 检查 Python 与 Pillow"
PY="$(command -v python3 || true)"
[ -z "$PY" ] && { echo "缺少 python3"; exit 1; }
"$PY" - <<'EOF'
import sys
try:
    import PIL  # noqa
except ImportError:
    sys.exit("缺少 Pillow：python3 -m pip install pillow -i https://pypi.tuna.tsinghua.edu.cn/simple")
EOF

echo "==> [2/5] 检查 Rust"
if [ -x "$HOME/.cargo/bin/cargo" ]; then export PATH="$HOME/.cargo/bin:$PATH"; fi
if ! command -v cargo >/dev/null 2>&1; then
  echo "    未找到 Rust，使用国内镜像安装（约 1 分钟）"
  curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y --profile minimal
  export PATH="$HOME/.cargo/bin:$PATH"
fi
command -v cargo >/dev/null || { echo "Rust 安装失败，请手动安装后重试"; exit 1; }

echo "==> [3/5] 准备 Zectrix SDK 0.1.0（配套固件 3.7.2）"
if [ ! -d "$SDK_DIR" ]; then
  TMPZ="$APP_DIR/sdk.zip"
  OK=0
  for URL in \
    "https://codeload.github.com/itopinion/zectrix-local-push-sdk/zip/refs/heads/main" \
    "https://ghproxy.net/https://github.com/itopinion/zectrix-local-push-sdk/archive/refs/heads/main.zip"; do
    if curl -sSL --max-time 120 -o "$TMPZ" "$URL" && [ "$(stat -f%z "$TMPZ" 2>/dev/null || echo 0)" -gt 10000 ]; then OK=1; break; fi
  done
  [ "$OK" = 1 ] || { echo "SDK 下载失败，请手动下载解压为 $SDK_DIR"; exit 1; }
  unzip -q "$TMPZ" -d "$APP_DIR"
  mv "$APP_DIR/zectrix-local-push-sdk-main" "$SDK_DIR"
  rm -f "$TMPZ"
fi
echo "    SDK 就绪：$SDK_DIR"

echo "==> [4/5] 编译推送程序（首次约 1 分钟）"
export CMAKE_POLICY_VERSION_MINIMUM=3.5
if ! command -v cmake >/dev/null 2>&1; then
  "$PY" -m pip install -q cmake -i https://pypi.tuna.tsinghua.edu.cn/simple || true
fi
CMAKE_BIN="$("$PY" -c 'import cmake,os;print(os.path.join(os.path.dirname(cmake.__file__),"data","bin"))' 2>/dev/null || true)"
export PATH="${CMAKE_BIN:-}:$PATH"

( cd "$APP_DIR/pusher" && cargo build --release )
echo "    已生成：$APP_DIR/pusher/target/release/build-board-pusher"

echo "==> [5/5] 配置与 token"
if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
  echo "    已生成 config.json，请编辑 repos（最多 10 个，owner/name）"
fi

if security find-generic-password -s zectrix-build-board -w >/dev/null 2>&1; then
  echo "    钥匙串里已有 token（服务名 zectrix-build-board），跳过"
else
  echo "    可选：输入 GitHub token（只需所选仓库的 Actions 只读 + Pull requests 只读）"
  echo "    直接回车则跳过，稍后用示例数据预览。输入不显示。"
  printf "    token: "
  read -r -s TOKEN || true
  echo
  if [ -n "${TOKEN:-}" ]; then
    security add-generic-password -a "$USER" -s zectrix-build-board -w "$TOKEN" -U
    unset TOKEN
    echo "    已写入钥匙串（不会出现在任何文件里）"
  else
    echo "    已跳过：无 token 时使用 fixture.json，页脚标注「示例数据」"
  fi
fi

echo "==> 生成预览"
( cd "$APP_DIR" && "$PY" board.py --preview )
echo
echo "✅ 完成。请先看 preview-1.png 与 preview-2.png。"
echo "   确认后："
echo "     1) python3 board.py --daemon        # 出图（默认 5 分钟重拉）"
echo "     2) ./pusher/target/release/build-board-pusher <设备IP> status.png"
echo "   设备 IP 在 NOTE4「设置 → 网络」中查看；需开启「待机保持 Wi-Fi」。"
