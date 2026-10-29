#!/usr/bin/env python3
"""Afterlight: extract the files an agent session touched and render them as an interactive tree (HTML).

Supported sources:
  - Claude Code  ~/.claude/projects/<proj>/<session>.jsonl  (including subagents/*.jsonl)
  - Codex CLI    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Usage:
  python3 afterlight.py --list                  # list recent sessions
  python3 afterlight.py --latest                # most recent Claude Code session
  python3 afterlight.py <session.jsonl|id> -o out.html
  python3 afterlight.py <...> --json            # emit event JSON only
"""
import argparse
import glob
import json
import os
import re
import shlex
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude", "projects")
CODEX_DIR = os.path.join(HOME, ".codex", "sessions")
HERE = os.path.dirname(os.path.abspath(__file__))

# Operation types, ordered by strength: a tree node takes the color of its strongest op
OPS = ["delete", "create", "edit", "read", "list", "search", "hit", "ref"]


# ---------------------------------------------------------------- Bash heuristics

READ_CMDS = {"cat", "head", "tail", "less", "more", "wc", "bat", "nl", "file", "stat",
             "diff", "cmp", "md5", "shasum", "sha256sum", "jq", "open", "xxd", "od", "strings",
             "plutil", "sqlite3", "source", "."}
SEARCH_CMDS = {"grep", "rg", "ag", "ack", "egrep", "fgrep"}
LIST_CMDS = {"ls", "find", "tree", "fd", "du", "exa", "eza"}
EXEC_CMDS = {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "deno", "bun", "tsx", "ts-node"}
SKIP_CMDS = {"echo", "printf", "cd", "export", "true", "false", "sleep", "which", "type",
             "curl", "wget", "git", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv", "brew",
             "kill", "pkill", "ps", "env", "date", "pwd", "mkdir", "chmod", "test", "["}
WRAPPERS = {"sudo", "time", "nohup", "env", "command", "xargs", "timeout"}

PATHISH = re.compile(r"^(~|/|\./|\.\./)|[\w\-]\.[A-Za-z0-9]{1,6}$|/")


def looks_like_path(tok):
    if not tok or tok.startswith("-") or len(tok) > 400:
        return False
    if any(c in tok for c in "$`*?{}()<>|;&=\n\"'") or tok.startswith("http") or "://" in tok:
        return False
    return bool(PATHISH.search(tok))


def split_segments(cmd):
    """Split into command segments on && || ; | and newlines (not inside quotes); skip heredoc bodies."""
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b", lambda m: m.group(0).split("\n", 1)[0], cmd, flags=re.S)
    segs, buf, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            q = None if c == q else q
        elif c in "'\"":
            q = c
        elif c in ";|\n&":
            segs.append("".join(buf)); buf = []
            i += 1 + (cmd[i:i + 2] in ("&&", "||"))
            continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return [x for x in segs if x.strip()]


def bash_touches(cmd, cwd):
    """Return [(op, abs_path)]; every result is inferred."""
    out = []
    for seg in split_segments(cmd):
        # Redirect writes: > file / >> file (excluding 2>&1 and >/dev/null)
        for m in re.finditer(r"(?<![0-9&])>>?\s*([^\s&|;<>]+)", seg):
            p = m.group(1).strip("'\"")
            if p and not p.startswith(("/dev/", "&")) and looks_like_path(p):
                out.append(("edit", p))
        seg_clean = re.sub(r"\d?>>?\s*[^\s&|;<>]+", " ", seg)
        try:
            toks = shlex.split(seg_clean, comments=False)
        except ValueError:
            toks = seg_clean.split()
        while toks and (toks[0] in WRAPPERS or re.match(r"^\w+=", toks[0])):
            toks = toks[1:]
        if not toks:
            continue
        prog = os.path.basename(toks[0])
        args = [t for t in toks[1:] if not t.startswith("-")]
        if prog == "cd" and args:
            if "$" not in args[0]:
                cwd = resolve(args[0], cwd)
            continue
        if prog in ("ssh", "scp", "docker", "kubectl"):  # paths on a remote host or in a container are not local
            continue
        if prog in SKIP_CMDS:
            continue
        paths = [a for a in args if looks_like_path(a)]
        if prog == "sed":
            op = "edit" if any(t.startswith("-i") for t in toks) else "read"
            out += [(op, p) for p in paths]
        elif prog in ("rm", "rmdir", "trash"):
            out += [("delete", p) for p in paths]
        elif prog in ("mv", "cp", "rsync", "ln") and len(paths) >= 2:
            out += [("read", p) for p in paths[:-1]] + [("create", paths[-1])]
        elif prog in ("touch", "tee"):
            out += [("create" if prog == "touch" else "edit", p) for p in paths]
        elif prog in READ_CMDS:
            out += [("read", p) for p in paths]
        elif prog in SEARCH_CMDS:
            # The first non-option argument is the pattern; search targets follow it
            targets = [a for a in args[1:] if looks_like_path(a)]
            out += [("search", p) for p in (targets or (["."] if prog in ("rg", "ag") else []))]
        elif prog in LIST_CMDS:
            out += [("list", p) for p in (paths or ["."])]
        elif prog in EXEC_CMDS:
            out += [("read", p) for p in paths[:1]] + [("ref", p) for p in paths[1:]]
        else:
            out += [("ref", p) for p in paths]
    return [(op, resolve(p, cwd)) for op, p in out]


def resolve(p, cwd):
    p = os.path.expanduser(p.strip("'\",;"))
    if not os.path.isabs(p):
        p = os.path.join(cwd or "/", p)
    return os.path.normpath(p)


# ---------------------------------------------------------------- Claude Code

def iter_jsonl(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_claude(path):
    files = [(path, "main")]
    subdir = os.path.join(os.path.splitext(path)[0], "subagents")
    for sp in sorted(glob.glob(os.path.join(subdir, "*.jsonl"))):
        label = os.path.basename(sp)[:-6].replace("agent-", "sub:")
        meta = sp[:-6] + ".meta.json"
        if os.path.exists(meta):
            try:
                desc = json.load(open(meta)).get("description")
                if desc:
                    label = "sub:" + desc[:40]
            except Exception:
                pass
        files.append((sp, label))

    events, meta = [], {"agent": "claude-code", "source": path, "cwd": None, "title": None,
                        "prompts": [], "agents": [f[1] for f in files]}
    for fp, agent in files:
        pending = {}  # tool_use_id -> list of event indices, for back-filling results
        for d in iter_jsonl(fp):
            cwd = d.get("cwd") or meta["cwd"]
            if agent == "main":
                if cwd and not meta["cwd"]:
                    meta["cwd"] = cwd
                if d.get("type") in ("custom-title", "ai-title") and (d.get("customTitle") or d.get("aiTitle") or d.get("title")):
                    t = d.get("customTitle") or d.get("aiTitle") or d.get("title")
                    if d["type"] == "custom-title" or not meta.get("_custom"):
                        meta["title"] = t
                    meta["_custom"] = meta.get("_custom") or d["type"] == "custom-title"
            ts = d.get("timestamp")
            msg = d.get("message") or {}
            content = msg.get("content")
            if agent == "main" and d.get("type") == "user" and not d.get("isMeta"):
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
                text = text.strip()
                if text and not text.startswith("<") and ts:
                    meta["prompts"].append({"ts": ts, "text": text[:300]})
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    new = claude_tool_events(b.get("name", ""), b.get("input") or {}, cwd)
                    idxs = []
                    for op, p, conf, detail in new:
                        idxs.append(len(events))
                        events.append({"ts": ts, "agent": agent, "tool": b.get("name"), "op": op,
                                       "path": p, "conf": conf, "detail": detail})
                    pending[b.get("id")] = (idxs, b.get("name"), cwd, ts)
                elif b.get("type") == "tool_result":
                    hit = pending.pop(b.get("tool_use_id"), None)
                    r = d.get("toolUseResult")
                    if not hit or not isinstance(r, dict):
                        continue
                    idxs, name, rcwd, uts = hit
                    if name == "Write" and r.get("type") == "create":
                        for i in idxs:
                            events[i]["op"] = "create"
                    if isinstance(r.get("bashEditDiff"), dict):
                        # Actual Bash changes recorded by Claude Code: confidence exact
                        for f in r["bashEditDiff"].get("files", []):
                            n = sum(len(h.get("lines", [])) for h in f.get("hunks", []))
                            events.append({"ts": uts, "agent": agent, "tool": "Bash", "op": "edit",
                                           "path": os.path.normpath(f["filePath"]), "conf": "exact",
                                           "detail": f"bashEditDiff · {n} lines changed"})
                    for fn in (r.get("filenames") or [])[:200]:  # Grep/Glob hits
                        events.append({"ts": uts, "agent": agent, "tool": name, "op": "hit",
                                       "path": resolve(fn, rcwd), "conf": "exact", "detail": "search hit"})
    return finalize(events, meta)


def claude_tool_events(name, inp, cwd):
    """-> [(op, path, conf, detail)]"""
    fp = inp.get("file_path") or inp.get("notebook_path")
    if name == "Read" and fp:
        rng = f" L{inp['offset']}+{inp.get('limit', '')}" if inp.get("offset") else ""
        return [("read", resolve(fp, cwd), "exact", "Read" + rng)]
    if name == "Write" and fp:
        return [("edit", resolve(fp, cwd), "exact", f"Write {len(inp.get('content', ''))} chars")]
    if name in ("Edit", "MultiEdit", "NotebookEdit") and fp:
        old = inp.get("old_string", "")
        return [("edit", resolve(fp, cwd), "exact", (old.strip().splitlines() or [""])[0][:80])]
    if name in ("Grep", "Glob"):
        p = resolve(inp.get("path") or ".", cwd)
        return [("search", p, "exact", f"{name} {inp.get('pattern', '')}"[:120])]
    if name == "Bash" and inp.get("command"):
        cmd = inp["command"]
        return [(op, p, "inferred", cmd[:160]) for op, p in dedupe(bash_touches(cmd, cwd))]
    if name == "Artifact" and inp.get("file_path"):
        return [("read", resolve(inp["file_path"], cwd), "exact", "Artifact publish")]
    if name == "SendUserFile":
        return [("read", resolve(f, cwd), "exact", "SendUserFile") for f in inp.get("files", [])]
    return []


# ---------------------------------------------------------------- Codex

PATCH_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.M)


def parse_codex(path):
    events, meta = [], {"agent": "codex", "source": path, "cwd": None, "title": None,
                        "prompts": [], "agents": ["main"]}
    cwd = None
    for d in iter_jsonl(path):
        p, ts, t = d.get("payload") or {}, d.get("timestamp"), d.get("type")
        if t in ("session_meta", "turn_context") and p.get("cwd"):
            cwd = p["cwd"]
            meta["cwd"] = meta["cwd"] or cwd
        if t == "event_msg" and p.get("type") == "user_message" and p.get("message"):
            meta["prompts"].append({"ts": ts, "text": p["message"][:300]})
        item = p.get("item") or {}
        if t == "event_msg" and item.get("type") == "UserMessage":
            text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)).strip()
            if text:
                meta["prompts"].append({"ts": ts, "text": text[:300]})
        if t != "response_item":
            continue
        name = p.get("name")
        if p.get("type") == "function_call":
            try:
                args = json.loads(p.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            cmd = args.get("cmd") or args.get("command")
            if isinstance(cmd, list):
                cmd = cmd[-1] if cmd[:2] in (["bash", "-lc"], ["zsh", "-lc"]) else " ".join(cmd)
            wd = args.get("workdir") or cwd
            if cmd and "apply_patch" in cmd:
                events += codex_patch(cmd, wd, ts)
            elif cmd:
                for op, fp in dedupe(bash_touches(cmd, wd)):
                    events.append({"ts": ts, "agent": "main", "tool": name, "op": op, "path": fp,
                                   "conf": "inferred", "detail": cmd[:160]})
        elif p.get("type") == "custom_tool_call" and name == "apply_patch":
            events += codex_patch(p.get("input", ""), cwd, ts)
    return finalize(events, meta)


def codex_patch(text, cwd, ts):
    ops = {"Add": "create", "Update": "edit", "Delete": "delete"}
    return [{"ts": ts, "agent": "main", "tool": "apply_patch", "op": ops[k], "path": resolve(f.strip(), cwd),
             "conf": "exact", "detail": f"apply_patch {k}"} for k, f in PATCH_RE.findall(text)]


# ---------------------------------------------------------------- shared

def dedupe(pairs):
    seen, out = set(), []
    for x in pairs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


NOISE = ("/dev/", "/proc/")


def finalize(events, meta):
    events = [e for e in events if e["path"] and not e["path"].startswith(NOISE) and e["path"] != "/"]
    for e in events:
        # An inferred ref/read whose path does not exist (usually a misread argument) is downgraded
        e["exists"] = os.path.exists(e["path"])
    events.sort(key=lambda e: e["ts"] or "")
    for i, e in enumerate(events):
        e["seq"] = i
    meta["generated"] = datetime.now().isoformat(timespec="seconds")
    if events:
        meta["start"], meta["end"] = events[0]["ts"], events[-1]["ts"]
    meta["home"] = HOME
    meta.pop("_custom", None)
    return {"meta": meta, "events": events}


def detect_and_parse(path):
    head = open(path, encoding="utf-8", errors="replace").readline()
    if '"session_meta"' in head or "/.codex/" in path:
        return parse_codex(path)
    return parse_claude(path)


def list_sessions(n=20):
    rows = []
    for fp in glob.glob(os.path.join(CLAUDE_DIR, "*", "*.jsonl")):
        rows.append((os.path.getmtime(fp), "claude", fp))
    for fp in glob.glob(os.path.join(CODEX_DIR, "**", "*.jsonl"), recursive=True):
        rows.append((os.path.getmtime(fp), "codex", fp))
    rows.sort(reverse=True)
    return rows[:n]


def find_session(arg):
    if os.path.exists(arg):
        return arg
    hits = glob.glob(os.path.join(CLAUDE_DIR, "*", f"{arg}*.jsonl")) + \
        glob.glob(os.path.join(CODEX_DIR, "**", f"*{arg}*.jsonl"), recursive=True)
    if not hits:
        sys.exit(f"Session not found: {arg}")
    return hits[0]


# ---------------------------------------------------------------- session index (used by server.py)

def session_id(path):
    stem = os.path.basename(path)[:-6]
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", stem)
    return m.group(0) if m else stem


def activity_mtime(path):
    """A session's last activity time: the newest of the main log and its subagent logs."""
    mt = os.path.getmtime(path)
    subdir = os.path.join(path[:-6], "subagents")
    if os.path.isdir(subdir):
        for fp in glob.glob(os.path.join(subdir, "*.jsonl")):
            mt = max(mt, os.path.getmtime(fp))
    return mt


def _head_lines(path, max_bytes=2_000_000, max_lines=400):
    n = 0
    with open(path, "rb") as f:
        for raw in f:
            n += len(raw)
            if n > max_bytes or max_lines <= 0:
                return
            max_lines -= 1
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def _tail_lines(path, max_bytes=600_000):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        chunk = f.read().split(b"\n")[1:]
    for raw in chunk:
        try:
            yield json.loads(raw)
        except ValueError:
            continue


def _user_text(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") in ("text", "input_text"))


def quick_meta(path):
    """Read only the head and tail of the file for the list view: agent, cwd, title, first prompt."""
    kind = "codex" if "/.codex/" in path else "claude-code"
    meta = {"id": session_id(path), "agent": kind, "path": path, "cwd": None, "title": None, "prompt": None}
    if kind == "claude-code":
        for d in _head_lines(path):
            meta["cwd"] = meta["cwd"] or d.get("cwd")
            if not meta["prompt"] and d.get("type") == "user" and not d.get("isMeta"):
                t = _user_text((d.get("message") or {}).get("content")).strip()
                if t and not t.startswith("<"):
                    meta["prompt"] = t[:200]
            if meta["cwd"] and meta["prompt"]:
                break
        titles = {}
        for d in _tail_lines(path):
            if d.get("type") in ("custom-title", "ai-title"):
                titles[d["type"]] = d.get("customTitle") or d.get("aiTitle") or d.get("title")
        meta["title"] = titles.get("custom-title") or titles.get("ai-title")
    else:
        for d in _head_lines(path):
            p = d.get("payload") or {}
            if d.get("type") in ("session_meta", "turn_context"):
                meta["cwd"] = meta["cwd"] or p.get("cwd")
            t = None
            if d.get("type") == "event_msg" and p.get("type") == "user_message":
                t = p.get("message")
            elif d.get("type") == "event_msg" and (p.get("item") or {}).get("type") == "UserMessage":
                t = _user_text(p["item"].get("content"))
            if t and not meta["prompt"] and not t.strip().startswith("<"):
                meta["prompt"] = t.strip()[:200]
            if meta["cwd"] and meta["prompt"]:
                break
    return meta


def all_session_files():
    out = [(fp, "claude-code") for fp in glob.glob(os.path.join(CLAUDE_DIR, "*", "*.jsonl"))]
    out += [(fp, "codex") for fp in glob.glob(os.path.join(CODEX_DIR, "**", "*.jsonl"), recursive=True)]
    return out


def render(data, out):
    """Static export: embed a single session into viewer.html."""
    tpl = open(os.path.join(HERE, "viewer.html"), encoding="utf-8").read()
    src = data["meta"]["source"]
    payload = {"mode": "static", "sessions": [{
        "id": session_id(src), "agent": data["meta"]["agent"], "title": data["meta"].get("title"),
        "cwd": data["meta"].get("cwd"), "status": "library", "mtime": os.path.getmtime(src), "data": data}]}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    open(out, "w", encoding="utf-8").write(tpl.replace("/*__DATA__*/null", blob))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", nargs="?", help="session jsonl path or session id prefix")
    ap.add_argument("--latest", action="store_true", help="most recent Claude Code session")
    ap.add_argument("--list", action="store_true", help="list recent sessions")
    ap.add_argument("--json", action="store_true", help="emit event JSON instead of HTML")
    ap.add_argument("-o", "--out", help="output file")
    a = ap.parse_args()

    if a.list:
        for mt, kind, fp in list_sessions():
            print(f"{datetime.fromtimestamp(mt):%m-%d %H:%M}  {kind:6}  {os.path.getsize(fp) // 1024:>6}KB  {fp}")
        return
    if a.latest:
        path = next(fp for _, k, fp in list_sessions(200) if k == "claude")
    elif a.session:
        path = find_session(a.session)
    else:
        ap.print_help()
        return

    data = detect_and_parse(path)
    if a.json:
        s = json.dumps(data, ensure_ascii=False, indent=1)
        open(a.out, "w").write(s) if a.out else print(s)
        return
    out = a.out or os.path.join(HERE, "out", os.path.basename(path).replace(".jsonl", ".html"))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    render(data, out)
    ev = data["events"]
    print(f"{len(ev)} events · {len({e['path'] for e in ev})} paths → {out}")


if __name__ == "__main__":
    main()
