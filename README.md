# Afterlight

[English](#english) · [中文](#中文)

## English

### Why

We give an agent a goal, and it reads files, runs commands, edits code and sends subagents off on subtasks by itself. Twenty minutes later it tells us it is done.

But which files did it touch? Did it step outside the project? Did it delete anything? Most of the time we don't know, and we don't seem to care. Agents keep gaining autonomy, while our ability to see what they do has barely moved.

As agents grow more autonomous, how much "not knowing" are we willing to accept as the price of efficiency?

### What it does

Afterlight draws every file a coding agent session touched as an interactive tree: which files were read, edited, created, deleted or searched, when it happened, and whether the main agent or a subagent did it.

Pure Python standard library, no dependencies. The output is a single self-contained HTML file.

### Usage

**Live workbench** (recommended):

```bash
python3 server.py                    # open http://127.0.0.1:8770
python3 server.py --active-minutes 60
```

- The canvas shows all **active sessions** (written to in the last 30 minutes) by default; a session moves to the **library** 30 minutes after it stops writing.
- Click any session in the library to pin it to the canvas; active sessions can be hidden individually.
- The server pushes changes over SSE, refreshing about every 2 seconds. Newly touched files flash and the camera follows the latest operation. Listens on 127.0.0.1 only.

**Static export** of a single session:

```bash
python3 afterlight.py --list                          # list recent sessions
python3 afterlight.py <session-id prefix|jsonl path> -o trace.html
python3 afterlight.py <...> --json                    # export normalized events only
```

### Interface

- The **canvas** fills the window. Hierarchy: session card → project folder / outside the project → folders → files. Status dot on a session card: solid and blinking means written to in the last 2 minutes, hollow means active but idle, grey means from the library. With more than 3 sessions, only the most recent one is expanded.
- The **bottom taskbar** is the single entry point: Active / Library / Outline on the left (opens the left drawer; click again to close), the timeline in the middle (drag the band itself; Live button beside it), and Filters (popover), Inspector (right drawer), zoom, expand/collapse, theme and language on the right. Press `\` to collapse or restore all drawers.
- **Bilingual**: English by default; the 中 / EN button at the right end of the taskbar switches language, saved in the browser.
- Monochrome with thin lines; only write operations use status colors (edit = warning, create = positive, delete = negative).
- Shortcuts: Space play/pause · ←/→ step · `F` fit to window · `L` back to live · `\` collapse all panels · `Esc` clear selection.

### Data sources and confidence

| Source | Extraction | Confidence |
|---|---|---|
| Claude Code `Read/Write/Edit/MultiEdit/NotebookEdit/Grep/Glob` | `file_path` / `path` in tool arguments | exact |
| Claude Code `toolUseResult.bashEditDiff` | Files actually changed by a Bash command | exact |
| Claude Code `Write` result `type: create` | Distinguishes create from overwrite | exact |
| Claude Code subagents (`<session>/subagents/*.jsonl`) | Same as above, tagged `sub:<description>` | exact |
| Codex `apply_patch` | `*** Add/Update/Delete File:` | exact |
| Bash / `exec_command` command text | Heuristic: `cat/sed/grep/rg/ls/rm/cp/mv`, `>` redirects, etc. | inferred |

## 中文

### 为什么

我们给 Agent 一个目标，它自己读文件、跑命令、改代码，还会派 subagent 去做子任务。二十分钟后，它告诉我们完成了。

但是它碰过哪些文件？有没有走出项目目录？删过什么？大多数时候，我们不知道，好像也并不在乎。Agent 的自主权越来越大，我们看见它在做什么的能力却几乎没有长进。

当 Agent 越来越自主，我们愿意把多少"不知道"当作效率的代价？

### 它做什么

Afterlight 把一次 coding agent 会话触及过的文件画成一棵可交互的结构树：哪些文件被读、改、新建、删除、搜索，什么时候发生，是主 agent 还是 subagent 做的。

纯 Python 标准库，没有依赖。输出是单个自包含的 HTML 文件。

### 用法

**实时工作台**（推荐）：

```bash
python3 server.py                    # 打开 http://127.0.0.1:8770
python3 server.py --active-minutes 60
```

- 画布默认显示所有**活跃会话**（30 分钟内有写入）；会话停止写入 30 分钟后自动移入**库**。
- 在库里点任意会话可以把它固定到画布上；活跃会话也可以单独隐藏。
- 服务端通过 SSE 推送变化，约 2 秒刷新一次；新碰到的文件会闪一下，镜头跟着最新操作移动。只监听 127.0.0.1。

**静态导出**单个会话：

```bash
python3 afterlight.py --list                          # 列出最近的会话
python3 afterlight.py <session-id 前缀|jsonl 路径> -o trace.html
python3 afterlight.py <...> --json                    # 只导出归一化后的事件
```

### 界面

- **画布**铺满窗口。层级是：会话卡 → 项目目录 / 项目之外 → 目录 → 文件。会话卡上的状态点：实心闪烁表示 2 分钟内有写入，空心表示活跃但暂时空闲，灰色表示来自库。会话超过 3 个时，只展开最近的一个。
- **底部任务栏**是唯一入口：左边是 Active / Library / Outline（打开左抽屉，再点一次关闭），中间是时间轴（色带本身可以拖动，旁边是 Live 按钮），右边是 Filters（弹出浮层）、Inspector（打开右抽屉）、缩放、展开/收起、明暗和语言切换。按 `\` 一次收起或恢复所有抽屉。
- **双语**：默认英文，任务栏右端的 中 / EN 按钮切换，选择保存在浏览器本地。
- 黑白单色、细线，只有写操作用状态色（修改=warning、新建=positive、删除=negative）。
- 快捷键：空格 播放/暂停 · ←/→ 单步 · `F` 适应窗口 · `L` 回到实时 · `\` 折叠全部面板 · `Esc` 取消选择。

### 数据来源与置信度

| 来源 | 提取方式 | 置信度 |
|---|---|---|
| Claude Code `Read/Write/Edit/MultiEdit/NotebookEdit/Grep/Glob` | 工具参数里的 `file_path` / `path` | exact |
| Claude Code `toolUseResult.bashEditDiff` | Bash 命令实际改动的文件 | exact |
| Claude Code `Write` 的结果 `type: create` | 区分新建和覆盖 | exact |
| Claude Code subagent（`<session>/subagents/*.jsonl`） | 同上，标记为 `sub:<描述>` | exact |
| Codex `apply_patch` | `*** Add/Update/Delete File:` | exact |
| Bash / `exec_command` 命令文本 | 启发式：`cat/sed/grep/rg/ls/rm/cp/mv`、重定向 `>` 等 | inferred |
