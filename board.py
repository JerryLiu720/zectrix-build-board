#!/usr/bin/env python3
"""NOTE4 构建与待评审看板

两页：
  1 概览：失败几条、待评审几条
  2 明细：先失败，后待评审，每页最多 6 行

数据源：GitHub Actions（最近 7 天失败）+ 待我评审的 PR。
无 token 时不访问网络，使用 fixture.json 并在页脚标注「示例数据」。

用法：
  python3 board.py --preview             # 生成两张预览 PNG，不连设备
  python3 board.py --page 0 --out x.png  # 渲染单页
  python3 board.py --daemon              # 按 ui_state.txt 出图，定时重拉
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.json")
FIXTURE = os.path.join(BASE, "fixture.json")
CACHE = os.path.join(BASE, "cache.json")
STATUS_PNG = os.path.join(BASE, "status.png")
STATE = os.path.join(BASE, "ui_state.txt")
PAGES_FILE = os.path.join(BASE, "pages.txt")
FORCE = os.path.join(BASE, "force.req")
CONN = os.path.join(BASE, "conn.txt")

W, H = 400, 300
PAGES = 2
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
KEYCHAIN_SERVICE = "zectrix-build-board"
WEEK = 7 * 86400
API = "https://api.github.com"

INK, DARK, MID, LIGHT, FAINT = 0, 45, 85, 140, 225


# ---------------- 数据 ----------------

def get_token():
    """环境变量优先，其次 macOS 钥匙串；永不打印。"""
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def api_get(path, token):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "zectrix-build-board",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def load_config():
    if not os.path.exists(CONFIG):
        raise SystemExit("缺少 config.json：复制 config.example.json 后填入仓库列表")
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    repos = [r.strip() for r in cfg.get("repos", []) if "/" in r][:10]
    return {"repos": repos, "interval": int(cfg.get("interval", 300))}


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    data["demo"] = True
    return data


def _within_week(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return False
    return time.time() - t <= WEEK


def runs_to_failures(repo, runs, now=None):
    """纯函数：筛出 7 天内失败，供测试直接调用。"""
    now = now or time.time()
    out = []
    for r in runs:
        if r.get("conclusion") != "failure":
            continue
        if not _within_week(r.get("created_at")):
            continue
        out.append({
            "repo": repo,
            "workflow": r.get("name") or "workflow",
            "branch": r.get("head_branch") or "?",
            "time": r.get("created_at"),
        })
    return out


def summarize_runs(runs):
    """从最近几次运行里提取：运行中数量、上次成功时间、结论序列。"""
    running = 0
    last_success = None
    recent = []
    for r in runs:
        status = r.get("status") or ""
        concl = r.get("conclusion") or ""
        if status in ("in_progress", "queued", "waiting", "requested", "pending"):
            running += 1
            recent.append("running")
            continue
        if concl:
            recent.append(concl)
        if concl == "success" and last_success is None:
            last_success = r.get("created_at")
    latest = None
    if runs:
        r0 = runs[0]
        latest = {
            "conclusion": "running" if (r0.get("status") in ("in_progress", "queued", "waiting")) else (r0.get("conclusion") or "unknown"),
            "time": r0.get("created_at"),
            "workflow": r0.get("name") or "workflow",
        }
    return {"running": running, "last_success": last_success, "recent": recent, "latest": latest}


def search_to_reviews(repo, items):
    """纯函数：把 search/issues 的结果整理成待评审列表。"""
    return [{
        "repo": repo,
        "title": it.get("title") or "",
        "author": (it.get("user") or {}).get("login") or "?",
        "updated": it.get("updated_at"),
        "number": it.get("number"),
    } for it in items]


def fetch_repo(repo, token, now):
    """返回 (failures, reviews, ok)。"""
    failures, reviews, ok = [], [], True

    summary = {"running": 0, "last_success": None, "recent": [], "latest": None}
    try:
        data = api_get(f"/repos/{repo}/actions/runs?per_page=5", token)
        runs = data.get("workflow_runs", [])
        failures = runs_to_failures(repo, runs, now)
        summary = summarize_runs(runs)
    except Exception:
        ok = False

    try:
        q = urllib.parse.quote(f"is:pr is:open review-requested:@me repo:{repo}")
        data = api_get(f"/search/issues?q={q}&per_page=20", token)
        reviews = search_to_reviews(repo, data.get("items", []))
    except Exception:
        ok = False

    return failures, reviews, summary, ok


def _sort_key(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def collect(cfg, token, now=None):
    """拉取并整理；失败时用缓存。返回 (data, ok)。"""
    now = now or time.time()
    prev = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = {}

    failures, reviews, all_ok = [], [], True
    running_total, last_success, recent, repo_status = 0, None, [], []
    for repo in cfg["repos"]:
        f_new, r_new, summ, ok = fetch_repo(repo, token, now)
        if ok:
            running_total += summ.get("running", 0)
            recent = recent or summ.get("recent", [])
            repo_status.append({"repo": repo, "latest": summ.get("latest")})
            ts = summ.get("last_success")
            if ts and (not last_success or _sort_key(ts) > _sort_key(last_success)):
                last_success = ts
            failures += f_new
            reviews += r_new
        else:
            all_ok = False
            # 保留该仓库上一份成功结果
            for x in prev.get("failures", []):
                if x.get("repo") == repo:
                    failures.append(x)
            for x in prev.get("reviews", []):
                if x.get("repo") == repo:
                    reviews.append(x)

    failures.sort(key=lambda x: _sort_key(x.get("time")), reverse=True)
    reviews.sort(key=lambda x: _sort_key(x.get("updated")), reverse=True)
    # fetched_at 只表示「上次成功拉取」的时间
    last_ok = now if all_ok else prev.get("fetched_at")
    data = {"fetched_at": last_ok, "failures": failures, "reviews": reviews,
            "stale": not all_ok, "demo": False,
            "running": running_total, "last_success": last_success, "recent": recent,
            "repo_status": repo_status,
            # 从没成功过，也没有缓存：不能假装「构建是绿的」
            "unknown": (not all_ok) and not prev.get("fetched_at")}

    if all_ok:
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: data[k] for k in ("fetched_at", "failures", "reviews")},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE)
    return data, all_ok


# ---------------- 绘制 ----------------

_fonts = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(FONT, size, index=1 if bold else 0)
        except Exception:
            _fonts[key] = ImageFont.truetype(FONT, size, index=0)
    return _fonts[key]


def fit(d, text, size, max_w, bold=False):
    f = font(size, bold)
    s = str(text)
    while d.textlength(s, font=f) > max_w and len(s) > 1:
        s = s[:-1]
    return s if s == str(text) else s[:-1] + "…"


def ago(ts):
    if not ts:
        return "—"
    if isinstance(ts, (int, float)):
        t = float(ts)
    else:
        t = _sort_key(ts)
    if not t:
        return "—"
    d = max(0, time.time() - t)
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


def connected():
    try:
        return time.time() - os.path.getmtime(CONN) < 30
    except Exception:
        return False


def header(d, data, title):
    d.text((16, 6), title, font=font(15, True), fill=INK)
    st = "已连接" if connected() else "未连接"
    when = ago(data.get("fetched_at"))
    right = f"{st} · {when}"
    d.text((W - 16 - d.textlength(right, font=font(10)), 12), right, font=font(10), fill=MID)
    d.line([(16, 32), (W - 16, 32)], fill=LIGHT, width=1)


def footer(d, page, data, extra=""):
    d.line([(16, 268), (W - 16, 268)], fill=LIGHT, width=1)
    left = "↑↓ 翻页 · 确认键刷新"
    if data.get("refreshing"):
        left = "正在刷新…"
    elif data.get("unknown"):
        left = "尚未获取到数据 · 确认键重试"
    elif data.get("stale"):
        left = "数据可能过期 · 确认键重试"
    d.text((16, 276), left, font=font(10), fill=MID)
    tag = f"{page + 1}/{PAGES}"
    if data.get("demo"):
        tag = "示例数据 · " + tag
    d.text((W - 16 - d.textlength(tag, font=font(10)), 276), tag, font=font(10), fill=MID)


def bottom_band(d, data, by=248):
    d.text((16, by), "最近 5 次", font=font(10), fill=MID)
    recent = (data.get("recent") or [])[:5]
    x = 80
    for c in recent:
        if c == "success":
            d.rectangle([x, by - 1, x + 11, by + 10], fill=INK)
        elif c == "failure":
            d.rectangle([x, by - 1, x + 11, by + 10], fill=255, outline=INK, width=1)
            d.line([(x + 2, by + 1), (x + 9, by + 8)], fill=INK, width=1)
            d.line([(x + 9, by + 1), (x + 2, by + 8)], fill=INK, width=1)
        elif c == "running":
            d.rectangle([x, by - 1, x + 11, by + 10], fill=MID)
        else:
            d.rectangle([x, by - 1, x + 11, by + 10], fill=FAINT)
        x += 15
    if not recent:
        d.text((80, by), "—", font=font(10), fill=MID)
    ls = data.get("last_success")
    if ls:
        txt = f"上次成功 {ago(ls)}"
        d.text((W - 16 - d.textlength(txt, font=font(10)), by), txt, font=font(10), fill=MID)


def page_overview(d, data):
    header(d, data, "BUILD BOARD")

    if data.get("unknown"):
        d.text((16, 64), "数据暂时不可用", font=font(24, True), fill=INK)
        d.text((16, 102), "检查 token 权限与网络，按确认键重试", font=font(11), fill=MID)
        footer(d, 0, data)
        return

    nf, nr = len(data["failures"]), len(data["reviews"])
    running = data.get("running", 0)

    # 状态行：第一眼回答「有没有事」
    if nf:
        status, filled = "需要处理", True
    elif running:
        status, filled = "正在构建", True
    elif nr:
        status, filled = "有 PR 等你", True
    else:
        status, filled = "一切正常", False

    f_status = font(22, True)
    d.text((40, 40), status, font=f_status, fill=INK)
    bbox = d.textbbox((40, 40), status, font=f_status)   # 按实际墨迹算中心
    cy = (bbox[1] + bbox[3]) / 2
    r = 7
    if filled:
        d.ellipse([23 - r, cy - r, 23 + r, cy + r], fill=INK)
    else:
        d.ellipse([23 - r, cy - r, 23 + r, cy + r], outline=INK, width=2)

    # 数字摘要：优先 14px，放不下就依次去掉「运行中」、退回 11px
    bits = [f"{nf} 失败", f"{nr} 待评审"]
    if running:
        bits.append(f"{running} 运行中")
    sub_x = 40 + d.textlength(status, font=font(22, True)) + 14
    avail = W - 16 - sub_x
    sub, size = " · ".join(bits), 14
    if d.textlength(sub, font=font(size)) > avail:
        bits = bits[:2]
        sub = " · ".join(bits)
    if d.textlength(sub, font=font(size)) > avail:
        size = 11
    d.text((W - 16 - d.textlength(sub, font=font(size)), 44 if size == 14 else 46), sub, font=font(size), fill=DARK)
    d.line([(16, 72), (W - 16, 72)], fill=FAINT, width=1)

    # 全绿：改为每仓状态列表，避免大片空白
    if not nf and not nr:
        d.text((16, 78), "仓库状态", font=font(10), fill=MID)
        y = 94
        for item in (data.get("repo_status") or [])[:4]:
            name = item["repo"].split("/")[-1]
            latest = item.get("latest") or {}
            concl = latest.get("conclusion", "none")
            label = {"success": "通过", "failure": "失败", "running": "运行中"}.get(concl, "无流水线")
            d.text((16, y), fit(d, name, 14, 250, True), font=font(14, True), fill=INK)
            d.text((W - 16 - d.textlength(label, font=font(11)), y + 2), label, font=font(11), fill=DARK)
            sub = f"{latest.get('workflow', '—')} · {ago(latest.get('time'))}" if latest else "尚未运行过 Actions"
            d.text((16, y + 15), fit(d, sub, 11, 340), font=font(11), fill=MID)
            y += 32
        d.line([(16, y + 4), (W - 16, y + 4)], fill=FAINT, width=1)
        d.text((16, y + 12), f"关注 {len(data.get('repo_status') or [])} 个仓库", font=font(10), fill=MID)
        bottom_band(d, data)
        footer(d, 0, data)
        return

    # 失败
    d.text((16, 78), f"失败 {nf}", font=font(10), fill=MID)
    y = 92
    if nf:
        for f in data["failures"][:2]:
            d.text((16, y), fit(d, f["repo"].split("/")[-1], 14, 290, True), font=font(14, True), fill=INK)
            d.text((W - 16 - d.textlength(ago(f["time"]), font=font(10)), y + 2), ago(f["time"]), font=font(10), fill=MID)
            d.text((16, y + 14), fit(d, f"{f['workflow']} · {f['branch']}", 11, 340), font=font(11), fill=MID)
            y += 30
    else:
        d.text((16, y), "最近 7 天没有失败的 Actions", font=font(11), fill=DARK)
        y += 22

    # 待评审
    y = max(y + 6, 160)
    d.line([(16, y - 6), (W - 16, y - 6)], fill=FAINT, width=1)
    d.text((16, y), f"待评审 {nr}", font=font(10), fill=MID)
    y += 14
    if nr:
        for r in data["reviews"][:2]:
            d.text((16, y), fit(d, f"#{r.get('number', '?')} {r['title']}", 14, 300, True), font=font(14, True), fill=INK)
            d.text((W - 16 - d.textlength(ago(r["updated"]), font=font(10)), y + 2), ago(r["updated"]), font=font(10), fill=MID)
            d.text((16, y + 14), fit(d, f"{r['repo'].split('/')[-1]} · {r['author']}", 11, 340), font=font(11), fill=MID)
            y += 30
    else:
        d.text((16, y), "没有待评审的 PR", font=font(11), fill=DARK)

    bottom_band(d, data)

    footer(d, 0, data)


def page_detail(d, data):
    header(d, data, "明细")
    rows = []
    for f in data["failures"]:
        rows.append(("失败", f"{f['repo'].split('/')[-1]} · {f['workflow']}",
                     f"{f['branch']} · {ago(f['time'])}"))
    for r in data["reviews"]:
        rows.append(("评审", r["title"],
                     f"{r['repo'].split('/')[-1]} #{r.get('number', '?')} · {r['author']} · {ago(r['updated'])}"))

    if not rows:
        msg = "数据暂时不可用" if data.get("unknown") else "没有需要处理的事"
        d.text((16, 90), msg, font=font(16), fill=MID)
        footer(d, 1, data)
        return

    shown = rows[:6]
    y = 42
    for kind, line1, line2 in shown:
        d.text((16, y + 2), kind, font=font(11, True), fill=INK if kind == "失败" else MID)
        d.text((50, y), fit(d, line1, 15, 334, True), font=font(15, True), fill=INK)
        d.text((50, y + 18), fit(d, line2, 11, 334), font=font(11), fill=MID)
        d.line([(50, y + 32), (W - 16, y + 32)], fill=FAINT, width=1)
        y += 36

    rest = len(rows) - len(shown)
    if rest > 0:
        d.text((16, 250), f"还有 {rest} 条", font=font(10), fill=MID)
    footer(d, 1, data)


def render(page, data, out):
    page = page % PAGES
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    if page == 0:
        page_overview(d, data)
    else:
        page_detail(d, data)
    tmp = out + ".tmp"
    img.save(tmp, format="PNG", optimize=True)
    new = open(tmp, "rb").read()
    old = open(out, "rb").read() if os.path.exists(out) else b""
    if new == old:
        os.remove(tmp)
        return False
    os.replace(tmp, out)
    with open(PAGES_FILE, "w") as f:
        f.write(str(PAGES))
    return True


# ---------------- CLI ----------------

def preview(data):
    outs = []
    for p in range(PAGES):
        out = os.path.join(BASE, f"preview-{p + 1}.png")
        img = Image.new("L", (W, H), 255)
        d = ImageDraw.Draw(img)
        (page_overview if p == 0 else page_detail)(d, data)
        img.save(out, format="PNG", optimize=True)
        outs.append(out)
    return outs


def daemon(cfg, token):
    """示例模式也要响应翻页；确认键先给回执再拉数据。"""
    demo = not cfg["repos"] or not token
    fixed = load_fixture() if demo else None
    if demo:
        print("无 token 或无仓库：使用示例数据（不访问网络）", flush=True)

    last_page, last_fetch, last_data = None, 0.0, fixed
    while True:
        try:
            page = int(open(STATE).read().strip()) if os.path.exists(STATE) else 0
            forced = os.path.exists(FORCE)
            if forced:
                os.remove(FORCE)

            if forced and last_data is not None:
                # 立刻给回执：屏幕先显示「正在刷新…」
                busy = dict(last_data)
                busy["refreshing"] = True
                render(page, busy, STATUS_PNG)
                time.sleep(0.4)

            if demo:
                if page != last_page or forced:
                    fixed.pop("refreshing", None)
                    render(page, fixed, STATUS_PNG)
                    last_data, last_page = fixed, page
                    print(f"推送第 {page + 1} 页（示例数据）", flush=True)
            elif last_page is None or page != last_page or forced or time.time() - last_fetch >= cfg["interval"]:
                data, ok = collect(cfg, token)
                render(page, data, STATUS_PNG)
                last_data, last_page, last_fetch = data, page, time.time()
                print(f"推送第 {page + 1} 页（拉取{'成功' if ok else '失败，可能过期'}）", flush=True)
        except Exception as e:
            print(f"循环异常：{e}", file=sys.stderr, flush=True)
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--page", type=int)
    ap.add_argument("--out", default=STATUS_PNG)
    ap.add_argument("--daemon", action="store_true")
    a = ap.parse_args()

    cfg = load_config()
    token = get_token()

    if a.preview or (a.page is not None and not a.daemon):
        use_fixture = not cfg["repos"] or not token
        data = load_fixture() if use_fixture else collect(cfg, token)[0]
        if a.preview:
            for p in preview(data):
                print(p)
        else:
            render(a.page, data, a.out)
            print(a.out)
        return

    if a.daemon:
        daemon(cfg, token)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
