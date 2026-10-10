#!/usr/bin/env python3
"""Codebase dashboard: point it at a folder and get stats and telemetry in the browser.

    python dashboard.py                  start the dashboard (http://127.0.0.1:8765) and open it
    python dashboard.py G:\\some\\repo     same, opening straight on that folder

Standard library only. Uses git when the folder is a repository (file list, activity, churn).
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

PORT = 8765
HERE = Path(__file__).resolve().parent
RECENT = HERE / "recent.json"
RECENT_MAX = 30

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next",
             "target", ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
             "coverage", "site-packages", ".gradle", "bin", "obj", ".cache", ".parcel-cache"}

# extension -> (language, line-comment prefix, block comment (start, end) or None)
LANGS = {
    ".py": ("Python", "#", None), ".pyw": ("Python", "#", None),
    ".js": ("JavaScript", "//", ("/*", "*/")), ".mjs": ("JavaScript", "//", ("/*", "*/")),
    ".cjs": ("JavaScript", "//", ("/*", "*/")), ".jsx": ("JavaScript", "//", ("/*", "*/")),
    ".ts": ("TypeScript", "//", ("/*", "*/")), ".tsx": ("TypeScript", "//", ("/*", "*/")),
    ".html": ("HTML", None, ("<!--", "-->")), ".htm": ("HTML", None, ("<!--", "-->")),
    ".css": ("CSS", None, ("/*", "*/")), ".scss": ("CSS", "//", ("/*", "*/")),
    ".java": ("Java", "//", ("/*", "*/")), ".kt": ("Kotlin", "//", ("/*", "*/")),
    ".cs": ("C#", "//", ("/*", "*/")), ".go": ("Go", "//", ("/*", "*/")), ".rs": ("Rust", "//", ("/*", "*/")),
    ".c": ("C/C++", "//", ("/*", "*/")), ".h": ("C/C++", "//", ("/*", "*/")), ".cpp": ("C/C++", "//", ("/*", "*/")),
    ".hpp": ("C/C++", "//", ("/*", "*/")), ".swift": ("Swift", "//", ("/*", "*/")),
    ".rb": ("Ruby", "#", None), ".php": ("PHP", "//", ("/*", "*/")), ".lua": ("Lua", "--", None),
    ".sh": ("Shell", "#", None), ".bash": ("Shell", "#", None), ".ps1": ("PowerShell", "#", ("<#", "#>")),
    ".bat": ("Batch", "REM", None), ".cmd": ("Batch", "REM", None),
    ".sql": ("SQL", "--", ("/*", "*/")), ".r": ("R", "#", None),
    ".md": ("Markdown", None, None), ".json": ("JSON", None, None), ".yaml": ("YAML", "#", None),
    ".yml": ("YAML", "#", None), ".toml": ("TOML", "#", None), ".xml": ("XML", None, ("<!--", "-->")),
    ".ipynb": ("Notebook", None, None), ".vue": ("Vue", "//", ("<!--", "-->")), ".svelte": ("Svelte", "//", ("<!--", "-->")),
}
NOT_CODE = {"Markdown", "JSON", "YAML", "TOML", "XML", "Notebook"}  # counted, but not as "code"
TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
TEST_RE = re.compile(r"(^|[\\/])(tests?|__tests__|spec)([\\/])|(^|[\\/])test_[^\\/]*$|_test\.\w+$|\.(test|spec)\.\w+$",
                     re.I)
MAX_TEXT = 2_000_000  # bytes; larger files count toward size only


# ----------------------------------------------------------------------------- scanning

def git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def list_files(root: Path, is_git: bool) -> list[Path]:
    if is_git:
        out = git(root, "ls-files", "-co", "--exclude-standard", "-z")
        if out is not None:
            files = [root / p for p in out.split("\0") if p]
            return [f for f in files if f.is_file() and not (set(f.relative_to(root).parts[:-1]) & SKIP_DIRS)]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        files += [Path(dirpath) / f for f in filenames]
    return files


def count_lines(text: str, line_c: str | None, block: tuple[str, str] | None) -> tuple[int, int, int]:
    code = comment = blank = 0
    in_block = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            blank += 1
        elif in_block:
            comment += 1
            if block and block[1] in s:
                in_block = False
        elif block and s.startswith(block[0]):
            comment += 1
            in_block = block[1] not in s[len(block[0]):]
        elif line_c and (s.upper().startswith("REM ") or s.startswith("::") if line_c == "REM" else s.startswith(line_c)):
            comment += 1
        else:
            code += 1
    return code, comment, blank


def dependencies(root: Path) -> list[tuple[str, int, list[str]]]:
    found = []

    def add(rel: str, names: list[str]):
        names = sorted({n for n in names if n})
        found.append((rel, len(names), names))

    for f in list(root.glob("requirements*.txt")) + list(root.glob("*/requirements*.txt")):
        names = [re.split(r"[<>=!~\[; ]", ln.strip(), maxsplit=1)[0] for ln in f.read_text(errors="replace").splitlines()
                 if ln.strip() and not ln.strip().startswith(("#", "-"))]
        add(str(f.relative_to(root)), names)
    for f in list(root.glob("package.json")) + list(root.glob("*/package.json")):
        if "node_modules" in f.parts:
            continue
        try:
            d = json.loads(f.read_text(errors="replace"))
            add(str(f.relative_to(root)), list(d.get("dependencies", {})) + list(d.get("devDependencies", {})))
        except ValueError:
            pass
    for f in list(root.glob("pyproject.toml")) + list(root.glob("*/pyproject.toml")):
        try:
            d = tomllib.loads(f.read_text(errors="replace"))
            deps = d.get("project", {}).get("dependencies", []) + list(d.get("tool", {}).get("poetry", {}).get("dependencies", {}))
            add(str(f.relative_to(root)), [re.split(r"[<>=!~\[; ]", x, maxsplit=1)[0] for x in deps if x != "python"])
        except (tomllib.TOMLDecodeError, TypeError):
            pass
    for f in root.glob("Cargo.toml"):
        try:
            add("Cargo.toml", list(tomllib.loads(f.read_text(errors="replace")).get("dependencies", {})))
        except tomllib.TOMLDecodeError:
            pass
    for f in root.glob("go.mod"):
        add("go.mod", [ln.split()[0] for ln in f.read_text(errors="replace").splitlines()
                       if ln.startswith("\t") and "." in ln.split()[0]])
    return found


def weekday_counts(root: Path) -> list[int]:
    """Commits per weekday over the last year, Monday first."""
    days = [0] * 7
    for ln in (git(root, "log", "--since=1.year", "--date=format:%w", "--pretty=%ad") or "").split():
        if ln.isdigit():
            days[(int(ln) + 6) % 7] += 1  # %w: 0 = Sunday
    return days


def growth_series(root: Path, lines_now: int) -> list[tuple[float, int]]:
    """Approximate size of the source code (all lines of code-language files) over time, worked backwards
    from today's count using git's added/removed line totals. Daily points for repos younger than ~10
    weeks, otherwise weekly points for the last 26 weeks."""
    stamps = (git(root, "log", "--reverse", "--pretty=%ct") or "").split()
    age_days = (time.time() - int(stamps[0])) / 86400 if stamps and stamps[0].isdigit() else 0
    period, buckets = (86400, int(age_days) + 2) if age_days <= 70 else (7 * 86400, 26)
    out = git(root, "log", f"--since={buckets * period // 86400}.days", "--numstat", "--pretty=format:@%ct") or ""
    code_ext = {e for e, (lang, _, _) in LANGS.items() if lang not in NOT_CODE}
    now = time.time()
    net = [0] * buckets  # index 0 = the current period
    stamp = now
    for ln in out.splitlines():
        if ln.startswith("@"):
            stamp = int(ln[1:])
            continue
        parts = ln.split("\t")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        path = parts[2].replace("{", "").replace("}", "").split(" => ")[-1]
        if Path(path).suffix.lower() not in code_ext or set(Path(path).parts[:-1]) & SKIP_DIRS:
            continue
        k = int((now - stamp) // period)
        if 0 <= k < buckets:
            net[k] += int(parts[0]) - int(parts[1])
    series, size = [], lines_now
    for k in range(buckets):  # walk back in time
        series.append((now - k * period, size))
        size -= net[k]
    series.append((now - buckets * period, size))
    return [(t, max(0, s)) for t, s in reversed(series)]


def scan(root: Path) -> dict:
    t0 = time.perf_counter()
    is_git = git(root, "rev-parse", "--is-inside-work-tree") is not None
    files = list_files(root, is_git)
    langs = defaultdict(lambda: {"files": 0, "code": 0, "comment": 0, "blank": 0})
    sizes, src_lines, todos = [], [], Counter()
    todo_kinds = Counter()
    src_meta = {}  # rel (forward slashes) -> (all lines, code lines, bytes, language)
    total_bytes = 0
    test = {"files": 0, "lines": 0}
    src = {"files": 0, "lines": 0}
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        rel = str(f.relative_to(root))
        total_bytes += size
        sizes.append((size, rel))
        lang = LANGS.get(f.suffix.lower())
        if not lang or size > MAX_TEXT:
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        name, line_c, block = lang
        code, comment, blank = count_lines(text, line_c, block)
        L = langs[name]
        L["files"] += 1; L["code"] += code; L["comment"] += comment; L["blank"] += blank
        if name in NOT_CODE:
            continue
        src_lines.append((code, rel, name))
        src_meta[rel.replace("\\", "/")] = (code + comment + blank, code, size, name)
        bucket = test if TEST_RE.search(rel) else src
        bucket["files"] += 1; bucket["lines"] += code
        for m in TODO_RE.finditer(text):
            todos[rel] += 1
            todo_kinds[m.group(1)] += 1

    info = {"root": str(root), "name": root.name, "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "is_git": is_git, "n_files": len(files), "bytes": total_bytes,
            "langs": dict(langs), "largest_files": sorted(sizes, reverse=True)[:10],
            "largest_src": sorted(src_lines, reverse=True)[:10],
            "todos": todos.most_common(8), "todo_total": sum(todos.values()), "todo_kinds": dict(todo_kinds),
            "test": test, "src": src, "deps": dependencies(root),
            "code_lines_total": sum(v[0] for v in src_meta.values())}

    churn = Counter()
    if is_git:
        now = time.time()
        stamps = [int(x) for x in (git(root, "log", "--since=26.weeks", "--pretty=%ct") or "").split()]
        weeks = [0] * 26
        for s in stamps:
            k = int((now - s) // (7 * 86400))
            if 0 <= k < 26:
                weeks[25 - k] += 1
        last = (git(root, "log", "-1", "--pretty=%cr%x1f%s%x1f%an") or "").strip().split("\x1f")
        authors = Counter((git(root, "log", "--since=1.year", "--pretty=%an") or "").splitlines())
        churn = Counter(ln for ln in (git(root, "log", "--since=90.days", "--name-only", "--pretty=format:") or "").splitlines()
                        if ln.strip())
        line_of = {rel.replace("\\", "/"): code for code, rel, _ in src_lines}
        info["git"] = {
            "weeks": weeks, "commits_26w": len(stamps),
            "commits_total": int((git(root, "rev-list", "--count", "HEAD") or "0").strip() or 0),
            "first": (git(root, "log", "--reverse", "--pretty=%cs") or "").split("\n")[0],
            "last": last if len(last) == 3 else None,
            "authors": authors.most_common(6),
            "branch": (git(root, "branch", "--show-current") or "").strip(),
            "dirty": len([ln for ln in (git(root, "status", "--porcelain") or "").splitlines() if ln.strip()]),
            "churn": [(n, p, line_of.get(p)) for p, n in churn.most_common(8)],
            "weekday": weekday_counts(root),
            "growth": growth_series(root, info["code_lines_total"]),
        }
    # Hotspots: big files that keep changing rank highest (without git, size alone).
    hot = [{"path": rel, "lines": code, "bytes": size, "lang": lang, "churn": churn.get(rel, 0)}
           for rel, (_, code, size, lang) in src_meta.items()]
    mx_c = max((h["churn"] for h in hot), default=0) or 1
    mx_l = max((h["lines"] for h in hot), default=0) or 1
    for h in hot:
        h["score"] = (0.6 * h["churn"] / mx_c + 0.4 * h["lines"] / mx_l) if churn else h["lines"] / mx_l
    info["hotspots"] = sorted(hot, key=lambda h: -h["score"])[:8]
    info["seconds"] = round(time.perf_counter() - t0, 2)
    return info


# ----------------------------------------------------------------------------- history

HIST_DIR = HERE / "history"


def summarize(d: dict) -> dict:
    return {"at": time.time(), "label": d["scanned_at"],
            "loc": sum(v["code"] for k, v in d["langs"].items() if k not in NOT_CODE),
            "files": d["n_files"], "bytes": d["bytes"],
            "test_ratio": round(d["test"]["lines"] / max(1, d["src"]["lines"]), 3),
            "todos": d["todo_total"], "deps": sum(n for _, n, _ in d["deps"]),
            "commits_26w": (d.get("git") or {}).get("commits_26w")}


def record(d: dict) -> tuple[list[dict], dict | None]:
    """Save this scan to the folder's history. Returns (scans oldest first, the scan to compare with).
    Rescans within an hour replace the latest entry, so refreshing doesn't wipe out the comparison."""
    path = HIST_DIR / (hashlib.sha1(d["root"].lower().encode()).hexdigest()[:12] + ".json")
    try:
        scans = json.loads(path.read_text())["scans"]
    except (OSError, ValueError, KeyError):
        scans = []
    cur = summarize(d)
    if scans and cur["at"] - scans[-1]["at"] < 3600:
        scans[-1] = cur
    else:
        scans.append(cur)
    scans = scans[-200:]
    try:
        HIST_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps({"root": d["root"], "scans": scans}, indent=1))
    except OSError:
        pass
    return scans, (scans[-2] if len(scans) >= 2 else None)


# ----------------------------------------------------------------------------- rendering helpers

ICONS = {
    "code": '<polyline points="8 6 2 12 8 18"/><polyline points="16 6 22 12 16 18"/>',
    "disk": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    "commit": '<circle cx="12" cy="12" r="3.5"/><line x1="2" y1="12" x2="8.5" y2="12"/><line x1="15.5" y1="12" x2="22" y2="12"/>',
    "flask": '<path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3"/><line x1="7" y1="15" x2="17" y2="15"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><polyline points="20 4 20 11 13 11"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
            '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/>'
                '<line x1="8" y1="3" x2="8" y2="7"/><line x1="16" y1="3" x2="16" y2="7"/>',
    "dots": '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
    "branch": '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="8" r="2.5"/>'
              '<path d="M6 8.5v7M18 10.5c0 4-6 3-11 6"/>',
    "trend": '<polyline points="3 17 9 11 13 15 21 7"/><polyline points="15 7 21 7 21 13"/>',
}
LANG_COLORS = ["#3366ff", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4"]


def icon(name: str, size: int = 16) -> str:
    return (f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def compact(n: float) -> str:
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 10_000:
        return f"{n / 1000:.1f}K"
    return f"{n:,.0f}"


def pill(cur: float | None, prev: float | None, invert: bool = False) -> str:
    """Change pill like '▲ 4.2%'. Green means better: up, or down when invert (e.g. TODOs)."""
    if cur is None or prev is None:
        return ""
    if prev == 0:
        return '<span class="pill flat">new</span>' if cur else '<span class="pill flat">0%</span>'
    pct = (cur - prev) / abs(prev) * 100
    if abs(pct) < 0.05:
        return '<span class="pill flat">0%</span>'
    good = (pct > 0) != invert
    return f'<span class="pill {"up" if good else "down"}">{"▲" if pct > 0 else "▼"} {abs(pct):.1f}%</span>'


def split_path(rel: str) -> tuple[str, str]:
    rel = rel.replace("\\", "/")
    d, _, f = rel.rpartition("/")
    return f, d


# ----------------------------------------------------------------------------- charts (inline SVG)

def area_chart(series: list[tuple[float, float]]) -> str:
    if len(series) < 2:
        return '<p class="muted empty">Not enough history yet.</p>'
    W, H, L, R, T, B = 640, 210, 46, 8, 12, 178
    vals = [v for _, v in series]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.08
    lo, hi = max(0, lo - pad), hi + pad
    xs = [L + i * (W - L - R) / (len(series) - 1) for i in range(len(series))]
    ys = [B - (v - lo) / (hi - lo) * (B - T) for v in vals]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{xs[0]:.1f},{B} {line} {xs[-1]:.1f},{B}"
    grid = ""
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        y = B - (B - T) * i / 3
        grid += (f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" class="gl"/>'
                 f'<text x="{L - 8}" y="{y + 4:.1f}" class="ax" text-anchor="end">{compact(v)}</text>')
    labels = ""
    for i in sorted({round(k * (len(series) - 1) / 4) for k in range(5)}):
        labels += (f'<text x="{xs[i]:.1f}" y="{H - 8}" class="ax" text-anchor="middle">'
                   f'{datetime.fromtimestamp(series[i][0]).strftime("%b %d").replace(" 0", " ")}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" class="chart"><defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="#3366ff" stop-opacity=".28"/><stop offset="1" stop-color="#3366ff" stop-opacity="0"/>'
            f'</linearGradient></defs>{grid}<polygon points="{area}" fill="url(#ga)"/>'
            f'<polyline points="{line}" fill="none" stroke="#3366ff" stroke-width="2.2" stroke-linejoin="round"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4.5" fill="#fff" stroke="#3366ff" stroke-width="2.2"/>'
            f'{labels}</svg>')


def weekday_chart(days: list[int]) -> str:
    W, H, B = 300, 190, 156
    mx = max(days) or 1
    top = days.index(max(days))
    out = ""
    for i, (v, name) in enumerate(zip(days, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])):
        x = 14 + i * 40
        h = max(10, v / mx * 120)
        cls = "bar hi" if i == top and v else "bar"
        out += f'<rect x="{x}" y="{B - h:.1f}" width="28" height="{h:.1f}" rx="9" class="{cls}"><title>{v} commits</title></rect>'
        out += f'<text x="{x + 14}" y="{H - 12}" class="ax{" axhi" if i == top else ""}" text-anchor="middle">{name}</text>'
        if i == top and v:
            out += f'<text x="{x + 14}" y="{B - h - 8:.1f}" class="barval" text-anchor="middle">{v}</text>'
    return f'<svg viewBox="0 0 {W} {H}" class="chart">{out}</svg>'


def gauge(ratio: float) -> str:
    n, cx, cy, r1, r2 = 41, 120, 112, 74, 96
    f = min(1.0, ratio)
    ticks = ""
    for i in range(n):
        a = math.pi * (1 - i / (n - 1))
        on = i / (n - 1) <= f + 1e-9
        ticks += (f'<line x1="{cx + r1 * math.cos(a):.1f}" y1="{cy - r1 * math.sin(a):.1f}" '
                  f'x2="{cx + r2 * math.cos(a):.1f}" y2="{cy - r2 * math.sin(a):.1f}" class="{"tk on" if on else "tk"}"/>')
    return (f'<svg viewBox="0 0 240 128" class="chart gauge">{ticks}'
            f'<text x="{cx}" y="{cy - 14}" class="gv" text-anchor="middle">{ratio:.2f}</text>'
            f'<text x="{cx}" y="{cy + 6}" class="ax" text-anchor="middle">test lines per source line</text></svg>')


# ----------------------------------------------------------------------------- page

def shell(title: str, content: str, active_root: str = "", top_right: str = "", value: str = "") -> str:
    recent = load_recent()
    nav = "".join(
        f'<a class="nav{" on" if p.lower() == active_root.lower() else ""}" href="/scan?path={quote(p)}" title="{html.escape(p)}">'
        f'{icon("folder")}<span>{html.escape(Path(p).name or p)}</span></a>' for p in recent) \
        or '<p class="muted navempty">Scanned folders show up here.</p>'
    rescan = (f'<a class="btn white" href="/scan?path={quote(active_root)}">{icon("refresh", 14)} Rescan now</a>'
              if active_root else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"><style>{CSS}</style></head>
<body><div class="app"><aside class="side"><a class="logo" href="/"><span class="mark">{icon("grid", 15)}</span>codestats</a>
<a class="nav{"" if active_root else " on"}" href="/">{icon("grid")}<span>Overview</span></a>
<div class="navlabel">Recent codebases</div><nav>{nav}</nav><div class="grow"></div>
<div class="promo"><div class="promoic">{icon("trend", 18)}</div><b>Trends build with every scan</b>
<p>Each scan is saved. Rescan after changes to see what moved.</p>{rescan}</div></aside>
<div class="main"><div class="top"><form action="/scan" method="get" class="search">{icon("search")}
<input name="path" value="{html.escape(value)}" placeholder="Scan a folder, e.g. C:\\code\\my-project" autocomplete="off"><kbd>Enter</kbd></form>
<div class="topr">{top_right}</div></div><div class="content">{content}</div></div></div></body></html>"""


def render_home(error: str = "") -> str:
    recent = load_recent()
    cards = "".join(f'<a class="card recent" href="/scan?path={quote(p)}">{icon("folder", 20)}<b>{html.escape(Path(p).name or p)}</b>'
                    f'<span class="muted">{html.escape(p)}</span></a>' for p in recent)
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = (f'<div class="head"><div><h1>Overview</h1><div class="muted">Paste a folder path in the search bar to scan it.</div></div></div>'
            f'{err}<div class="recentgrid">{cards or "<p class=muted>No scans yet.</p>"}</div>')
    return shell("codestats", body)


def render_dashboard(d: dict, scans: list[dict], prev: dict | None) -> str:
    e = html.escape
    cur = scans[-1]
    g = d.get("git")
    langs = sorted(((k, v) for k, v in d["langs"].items() if k not in NOT_CODE and v["code"]), key=lambda kv: -kv[1]["code"])
    comment_total = sum(v["comment"] for _, v in langs)
    density = comment_total / max(1, cur["loc"] + comment_total) * 100
    vs = (lambda v, unit="": f'vs. {v}{unit} last scan') if prev else (lambda v, unit="": "first scan · trends start next time")

    # Header
    meta = [e(d["root"])]
    if g:
        meta.append(f'{icon("branch", 13)} {e(g["branch"] or "detached")}')
        if g["last"]:
            meta.append(f'last commit {e(g["last"][0])}')
        if g["dirty"]:
            meta.append(f'<span class="tag warn">{g["dirty"]} uncommitted</span>')
    compare = (f'{icon("calendar", 14)} Compared with {e(prev["label"])}' if prev else f'{icon("calendar", 14)} First scan of this folder')
    head = (f'<div class="head"><div><h1>{e(d["name"])}</h1><div class="meta">{" · ".join(meta)}</div></div>'
            f'<div class="actions"><span class="chip">{compare}</span>'
            f'<a class="btn" href="/scan?path={quote(d["root"])}">{icon("refresh", 14)} Rescan</a></div></div>')

    # KPI row
    def kpi(title, ic, color, value, pl, foot):
        return (f'<div class="card kpi"><div class="kt">{title}<span class="ki" style="color:{color}">{icon(ic, 17)}</span></div>'
                f'<div class="kv">{value}{pl}</div><div class="kf">{foot}</div></div>')
    p = prev or {}
    kpis = (kpi("Lines of code", "code", "#3366ff", f'{cur["loc"]:,}', pill(cur["loc"], p.get("loc")),
                vs(f'{p.get("loc", 0):,}') if prev else f'{comment_total:,} comments · {density:.0f}% density')
            + kpi("Disk size", "disk", "#10b981", fmt_bytes(cur["bytes"]), pill(cur["bytes"], p.get("bytes")),
                  vs(fmt_bytes(p["bytes"])) if prev else f'{cur["files"]:,} files')
            + (kpi("Commits", "commit", "#8b5cf6", f'{g["commits_26w"]:,}', pill(cur["commits_26w"], p.get("commits_26w")),
                   "last 26 weeks · " + (vs(p.get("commits_26w")) if prev else f'{g["commits_total"]:,} total'))
               if g else kpi("Commits", "commit", "#8b5cf6", "–", "", "not a git repository"))
            + kpi("Test ratio", "flask", "#f59e0b", f'{cur["test_ratio"]:.2f}', pill(cur["test_ratio"], p.get("test_ratio")),
                  vs(f'{p.get("test_ratio", 0):.2f}') if prev else f'{d["test"]["files"]} test files'))

    # Growth card (git history, or saved scans for non-git folders)
    if g:  # git counts all lines; scale to lines of code so the chart matches the Lines of code card
        k = cur["loc"] / max(1, d["code_lines_total"])
        series = [(t_, round(v * k)) for t_, v in g["growth"]]
    else:
        series = [(s["at"], s["loc"]) for s in scans]
    if g:
        while len(series) > 2 and series[0][1] == 0 and series[1][1] == 0:
            series = series[1:]  # skip the empty stretch before the first commit
    unit_days = (series[-1][0] - series[0][0]) / 86400 if len(series) > 1 else 0
    span = f"{round(unit_days / 7)} weeks" if unit_days > 70 else f"{round(unit_days)} days"
    start = series[0][1] if series else 0
    now_v = series[-1][1] if series else 0
    growth_pill = (f'<span class="pill up">▲ +{compact(now_v)}</span>' if start == 0 and now_v
                   else pill(now_v, start)) if len(series) > 1 else ""
    seg = ""
    for i, (k, v) in enumerate(langs[:3]):
        c = LANG_COLORS[i]
        seg += (f'<div class="seg" style="--c:{c}"><div class="segt"><span class="dot"></span>{e(k)}</div>'
                f'<div class="segv">{v["code"]:,}</div><div class="muted">{v["files"]} files · {v["code"] / max(1, cur["loc"]) * 100:.0f}%</div></div>')
    rest = sum(v["code"] for _, v in langs[3:])
    if rest:
        seg += (f'<div class="seg" style="--c:#cbd2dc"><div class="segt"><span class="dot"></span>Other</div>'
                f'<div class="segv">{rest:,}</div><div class="muted">{len(langs) - 3} languages</div></div>')
    growth = (f'<div class="card"><div class="ct">Codebase size<span class="cs">'
              f'{"lines of code, estimated from git history" if g else "lines of code per saved scan"}</span>{icon("dots")}</div>'
              f'<div class="growth"><div class="gl-left"><div class="big">{compact(now_v)}</div>{growth_pill}'
              f'<div class="muted">{"vs. " + span + " ago" if len(series) > 1 else "rescan later to see a trend"}</div>'
              f'<div class="mini"><span>{d["n_files"]:,}</span> files<br><span>{len(langs)}</span> languages</div></div>'
              f'<div class="gl-right">{area_chart(series)}</div></div>'
              f'<div class="sub"><div class="subt">Languages {icon("dots")}</div><div class="segs">{seg}</div></div></div>')

    # Hotspot table
    rows = ""
    hs = d.get("hotspots", [])
    mx_churn = max((h["churn"] for h in hs), default=0) or 1
    for i, h in enumerate(hs, 1):
        f, folder = split_path(h["path"])
        heat = h["churn"] / mx_churn
        cls = "hot3" if heat > 0.66 else "hot2" if heat > 0.33 else "hot1"
        score = max(1, math.ceil(h["score"] * 5))
        dots = "".join(f'<i class="{"on" if k < score else ""}"></i>' for k in range(5))
        rows += (f'<tr><td class="muted">#{i:02}</td><td><div class="fn">{e(f)}</div><div class="muted small">{e(folder) or "/"}</div></td>'
                 f'<td><span class="lang">{e(h["lang"])}</span></td><td class="r">{h["lines"]:,}</td>'
                 f'<td class="r"><span class="pill {cls}">{h["churn"]}×</span></td><td class="r muted">{fmt_bytes(h["bytes"])}</td>'
                 f'<td><span class="score">{dots}</span></td></tr>')
    table = (f'<div class="card"><div class="ct">Hotspot files<span class="cs">size × how often it changed (90 days); '
             f'big files that keep changing are where bugs cluster</span>{icon("dots")}</div>'
             f'<table><thead><tr><th>Rank</th><th>File</th><th>Language</th><th class="r">Lines</th><th class="r">Commits 90d</th>'
             f'<th class="r">Size</th><th>Hotspot</th></tr></thead><tbody>{rows or "<tr><td colspan=7 class=muted>No source files.</td></tr>"}</tbody></table></div>')

    # Heaviest files + TODO pair
    heavy = d["largest_files"][:6]
    top_b = heavy[0][0] if heavy else 1
    heavy_rows = "".join(
        f'<div class="hrow"><div class="hn" title="{e(p_)}">{e(split_path(p_)[0])}<span class="muted small"> {e(split_path(p_)[1])}</span></div>'
        f'<div class="track"><span style="width:{s / top_b * 100:.1f}%"></span></div><div class="r small">{fmt_bytes(s)}</div></div>'
        for s, p_ in heavy)
    kinds = " · ".join(f"{k} {v}" for k, v in sorted(d["todo_kinds"].items(), key=lambda kv: -kv[1]))
    todo_rows = "".join(f'<div class="trow"><span title="{e(p_)}">{e(split_path(p_)[0])}</span><b>{n}</b></div>' for p_, n in d["todos"][:6])
    pair = (f'<div class="pair"><div class="card"><div class="ct">Heaviest files<span class="cs">on disk</span></div>{heavy_rows}</div>'
            f'<div class="card"><div class="ct">TODO / FIXME</div><div class="kv">{d["todo_total"]:,}'
            f'{pill(cur["todos"], p.get("todos"), invert=True)}</div><div class="kf">{kinds or "none in source files"}</div>'
            f'<div class="tlist">{todo_rows}</div></div></div>')

    # Right column
    right = ""
    if g:
        days = g["weekday"]
        busiest = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][days.index(max(days))] if any(days) else "–"
        authors = ", ".join(f"{e(a)} ({n})" for a, n in g["authors"][:3])
        right += (f'<div class="card"><div class="ct">Most active day<span class="cs">commits by weekday, last year</span>{icon("dots")}</div>'
                  f'{weekday_chart(days)}<div class="foot"><b>{busiest}</b> · {sum(days):,} commits · {authors}</div></div>')
    t_ratio = cur["test_ratio"]
    verdict = "thin" if t_ratio < 0.2 else "fair" if t_ratio < 0.5 else "well tested"
    right += (f'<div class="card"><div class="ct">Test ratio{icon("dots")}</div>{gauge(t_ratio)}'
              f'<div class="foot center"><span class="tag {"good" if t_ratio >= 0.5 else "warn"}">{verdict}</span> '
              f'{d["test"]["files"]} test files · {d["test"]["lines"]:,} lines<br><span class="muted small">Target 0.5 or more. Found by names like tests/, test_*.py, *.test.js</span></div></div>')
    dep_total = sum(n for _, n, _ in d["deps"])
    deps = "".join(f'<details><summary><span>{e(f)}</span><b>{n}</b></summary><p class="muted small">{e(", ".join(names)) or "–"}</p></details>'
                   for f, n, names in d["deps"]) or '<p class="muted small">No manifest found (requirements.txt, pyproject.toml, package.json, Cargo.toml, go.mod).</p>'
    right += (f'<div class="card"><div class="ct">Dependencies<span class="cs">declared in manifests</span></div>'
              f'<div class="kv">{dep_total}{pill(cur["deps"], p.get("deps"), invert=True)}</div>{deps}</div>')
    hist = ""
    for i in range(len(scans) - 1, max(-1, len(scans) - 7), -1):
        s = scans[i]
        before = scans[i - 1] if i > 0 else None
        hist += (f'<div class="hist"><span>{e(s["label"])}</span><b>{s["loc"]:,}</b>'
                 f'{pill(s["loc"], before["loc"]) if before else "<span class=muted>first</span>"}</div>')
    right += (f'<div class="card"><div class="ct">Scan history<span class="cs">lines of code per scan</span></div>{hist}'
              f'<div class="muted small" style="margin-top:8px">Scanned in {d["seconds"]} s</div></div>')

    content = (f'{head}<div class="kpis">{kpis}</div><div class="cols"><div class="stack">{growth}{table}{pair}</div>'
               f'<div class="stack">{right}</div></div>')
    return shell(f'{e(d["name"])} · codestats', content, active_root=d["root"], value=d["root"])


CSS = """
:root{--bg:#f4f6fa;--card:#fff;--fg:#0f172a;--muted:#6b7280;--line:#e7eaf0;--soft:#f1f4f9;--accent:#3366ff;
--accent-soft:#eaf0ff;--green:#16a34a;--green-bg:#e7f6ec;--red:#dc2626;--red-bg:#fdecec}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13.5px/1.45 Inter,system-ui,"Segoe UI",sans-serif}
a{color:inherit;text-decoration:none}.ic{flex:none;vertical-align:-3px}
.app{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
.side{background:#fff;border-right:1px solid var(--line);padding:18px 14px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow:auto}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;padding:4px 8px 18px}
.mark{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#3366ff,#5b8cff);color:#fff}
.nav{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:10px;color:#374151;font-weight:500;margin:1px 0;position:relative}
.nav span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.nav:hover{background:var(--soft)}
.nav.on{background:var(--accent-soft);color:var(--accent)}.nav.on::before{content:"";position:absolute;left:-14px;top:8px;bottom:8px;width:3px;border-radius:0 3px 3px 0;background:var(--accent)}
.navlabel{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:#9aa1ad;margin:18px 10px 6px}
.navempty{padding:0 10px}.grow{flex:1;min-height:20px}
.promo{background:linear-gradient(160deg,#2b59f0,#1e3fae);color:#fff;border-radius:16px;padding:16px}
.promo b{display:block;margin:10px 0 4px}.promo p{margin:0 0 12px;font-size:12px;opacity:.85}
.promoic{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:rgba(255,255,255,.18)}
.main{min-width:0}.top{height:64px;display:flex;align-items:center;gap:16px;padding:0 28px;background:#fff;border-bottom:1px solid var(--line)}
.search{display:flex;align-items:center;gap:8px;flex:1;max-width:520px;background:var(--soft);border:1px solid transparent;border-radius:12px;padding:0 12px;color:var(--muted)}
.search:focus-within{border-color:#c7d4ff;background:#fff}
.search input{flex:1;border:0;background:transparent;padding:10px 0;font:inherit;color:var(--fg);outline:none}
kbd{font:11px Inter,system-ui;border:1px solid var(--line);border-radius:6px;padding:2px 6px;background:#fff;color:var(--muted)}
.topr{margin-left:auto;color:var(--muted)}.content{padding:24px 28px 48px;max-width:1440px}
.head{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;justify-content:space-between;margin-bottom:20px}
h1{margin:0 0 4px;font-size:24px;font-weight:700;letter-spacing:-.01em}.meta{color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.chip{display:inline-flex;gap:7px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-weight:500;font-size:12.5px}
.btn{display:inline-flex;gap:7px;align-items:center;background:var(--accent);color:#fff;border-radius:10px;padding:8px 14px;font-weight:600;font-size:12.5px;box-shadow:0 1px 2px rgba(51,102,255,.3)}
.btn.white{background:#fff;color:#1e3fae;box-shadow:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px;box-shadow:0 1px 2px rgba(15,23,42,.04);min-width:0}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.kt{display:flex;justify-content:space-between;align-items:center;font-weight:600}.ki{display:grid;place-items:center}
.kv{display:flex;align-items:center;gap:10px;font-size:26px;font-weight:700;letter-spacing:-.02em;margin:10px 0 4px}
.kf{color:var(--muted);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:3px;font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:999px;letter-spacing:0}
.pill.up{background:var(--green-bg);color:var(--green)}.pill.down{background:var(--red-bg);color:var(--red)}.pill.flat{background:var(--soft);color:var(--muted)}
.pill.hot1{background:var(--soft);color:#475569}.pill.hot2{background:#fff4e0;color:#b45309}.pill.hot3{background:var(--red-bg);color:var(--red)}
.cols{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:16px;margin-top:16px;align-items:start}
.stack{display:flex;flex-direction:column;gap:16px;min-width:0}
.ct{display:flex;align-items:baseline;gap:10px;font-weight:600;font-size:14px;margin-bottom:12px}.ct>.ic{margin-left:auto;color:#9aa1ad}
.cs{color:var(--muted);font-weight:400;font-size:12px}
.growth{display:grid;grid-template-columns:200px minmax(0,1fr);gap:12px;align-items:center}
.big{font-size:38px;font-weight:700;letter-spacing:-.03em;line-height:1.1;margin-bottom:8px}
.gl-left .pill{margin-right:6px}.gl-left .muted{display:inline;font-size:12px}
.mini{margin-top:22px;color:var(--muted);font-size:12px;line-height:1.9}.mini span{color:var(--fg);font-weight:600}
.chart{width:100%;height:auto;display:block}.gl{stroke:#eef1f6;stroke-width:1}.ax{fill:#9aa1ad;font-size:11px;font-family:Inter,system-ui}
.axhi{fill:var(--accent);font-weight:600}.bar{fill:#eceff5}.bar.hi{fill:var(--accent)}.barval{font-size:12px;font-weight:700;fill:var(--fg);font-family:Inter,system-ui}
.tk{stroke:#e5e8ee;stroke-width:4;stroke-linecap:round}.tk.on{stroke:#34c27a}.gv{font-size:30px;font-weight:700;fill:var(--fg);font-family:Inter,system-ui}
.gauge{max-width:280px;margin:0 auto}
.sub{border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-top:14px}.subt{display:flex;justify-content:space-between;font-weight:600;margin-bottom:10px;color:#374151}.subt .ic{color:#9aa1ad}
.segs{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.seg{padding-bottom:10px;border-bottom:4px solid var(--c);border-radius:0 0 3px 3px}
.segt{display:flex;align-items:center;gap:7px;font-weight:600;color:#374151}.dot{width:8px;height:8px;border-radius:3px;background:var(--c)}
.segv{font-size:20px;font-weight:700;margin:4px 0 0}.seg .muted{font-size:11.5px}
table{width:100%;border-collapse:collapse}th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#9aa1ad;font-weight:600;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid #f1f3f7;vertical-align:middle}tr:last-child td{border-bottom:0}
.r{text-align:right;font-variant-numeric:tabular-nums}.fn{font-weight:600;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.small{font-size:11.5px}.muted{color:var(--muted)}.lang{font-size:11.5px;background:var(--soft);padding:2px 8px;border-radius:6px;color:#475569}
.score{display:inline-flex;gap:3px}.score i{width:8px;height:8px;border-radius:50%;background:#e5e8ee}.score i.on{background:#f59e0b}
.pair{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:16px}
.hrow{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(60px,1fr) auto;gap:10px;align-items:center;margin:8px 0}
.hn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.track{height:7px;background:var(--soft);border-radius:4px;overflow:hidden}.track span{display:block;height:100%;background:#10b981;border-radius:4px}
.tlist{margin-top:10px}.trow{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-top:1px solid #f1f3f7}.trow span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.foot{margin-top:8px;color:var(--muted);font-size:12px}.foot b{color:var(--fg)}.center{text-align:center}
.tag{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:999px}.tag.good{background:var(--green-bg);color:var(--green)}.tag.warn{background:#fff4e0;color:#b45309}
details{border-top:1px solid #f1f3f7;padding:7px 0}summary{display:flex;justify-content:space-between;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
.hist{display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center;padding:7px 0;border-top:1px solid #f1f3f7;font-size:12.5px}
.empty{padding:40px 0;text-align:center}.err{color:var(--red);font-weight:500}
.recentgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}
.recent{display:flex;flex-direction:column;gap:6px;color:var(--fg)}.recent .ic{color:var(--accent)}.recent:hover{border-color:#c7d4ff}
@media (max-width:1180px){.cols{grid-template-columns:minmax(0,1fr)}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:760px){.app{grid-template-columns:1fr}.side{display:none}.kpis,.pair{grid-template-columns:1fr}
.growth{grid-template-columns:1fr}.content{padding:16px}.top{padding:0 16px}th:nth-child(3),td:nth-child(3),th:nth-child(6),td:nth-child(6){display:none}}
"""


def load_recent() -> list[str]:
    try:
        return json.loads(RECENT.read_text())[:RECENT_MAX]
    except (OSError, ValueError):
        return []


def remember(path: str) -> None:
    items = [path] + [p for p in load_recent() if p.lower() != path.lower()]
    try:
        RECENT.write_text(json.dumps(items[:RECENT_MAX], indent=1))
    except OSError:
        pass


# ----------------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/scan":
            raw = (parse_qs(url.query).get("path") or [""])[0].strip().strip('"')
            root = Path(raw).expanduser()
            if not raw or not root.is_dir():
                return self.send(render_home(f"Not a folder: {raw}" if raw else ""))
            remember(str(root.resolve()))
            try:
                d = scan(root.resolve())
                return self.send(render_dashboard(d, *record(d)))
            except Exception as ex:  # show the error instead of a dead page
                return self.send(render_home(f"Scan failed: {type(ex).__name__}: {ex}"))
        if url.path == "/":
            return self.send(render_home())
        self.send_error(404)

    def send(self, body: str):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--no-browser"]
    open_browser = "--no-browser" not in sys.argv
    target = args[0] if args else ""
    url = f"http://127.0.0.1:{PORT}/" + (f"scan?path={quote(target)}" if target else "")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # already running: just open the page
        if open_browser:
            webbrowser.open(url)
        return
    print(f"Codebase dashboard on http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
