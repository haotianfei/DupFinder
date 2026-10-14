# DupFinder 贡献指南

<!-- 本文件为分块双语：先完整中文指南，分隔线之后为 Complete English Guide。 -->

## 开发环境

要求 Python **3.11 及以上**（运行依赖 `hashlib.file_digest`）。

```bash
git clone https://github.com/haotianfei/DupFinder.git
cd DupFinder
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]        # 运行时依赖 filelock；dev 附加：pytest、mypy
```

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `dup_finder.py` | 主程序：两阶段扫描、哈希缓存、动作执行、移动清单与回滚 |
| `pathlogic.py` | 纯函数：路径分量匹配与结果文件结构校验，无日志与 IO 副作用 |
| `logger.py` | 统一日志配置：控制台 INFO 里程碑 + 按天轮转的文件日志 |
| `change_priority.py` | 独立脚本：回喂处理前微调结果 JSON 中的优先级 |

请保持此边界：`pathlogic.py` 必须保持"导入零副作用"，辅助脚本才不会连带
日志/缓存机制。

## 本地检查

CI 只跑以下两道门禁，推送前请本地先跑：

```bash
# 1) 测试套件
python3 -m pytest tests/ -q

# 快测档（跳过 slow 标记的长集成用例）
python3 -m pytest tests/ -q -m "not slow"

# 2) 静态类型检查
python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py
```

测试约定：

- **唯一 seam 是 CLI 进程边界。** 所有用例以子进程方式驱动工具，只断言外部行为
  （退出码、日志里程碑、文件系统终态、产物文件：缓存 JSON、结果 JSON、移动清单）。
  不要断言内部实现，也不要 import 被测模块去戳私有细节。
- **每个用例跑在私有临时树。** `tests/conftest.py` 的 `DupTree` fixture 会把脚本
  副本放进独立目录并只扫描其中内容。不要让测试指向真实用户数据，也不要向仓库根写
  任何产物——用例期间仓库根的既有产物（`file_cache.json`、`logs/`、`out/`、
  `duplicates_*.json`）若发生变化，autouse 守卫会让用例直接失败。
- **`slow` 标记**用于耗时较长的集成用例（大树、缓存兼容验证）：本地快速回测可
  跳过，CI 仍会全量执行，必须保持通过。

## 提交规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：
`类型(范围): 一句话描述（祈使句）`。类型：`feat`、`fix`、`test`、`refactor`、
`chore`、`docs`。示例：

```text
feat(cli): add --min-free-percent guard to move actions
fix(cache): flush pending entries before exiting on SIGTERM
test(undo): cover manifests with occupied source locations
docs(readme): refresh parameter table to match --help
refactor(pathlogic): extract component matcher from dup_finder
chore(ci): pin mypy in dev extras
```

一个提交只做一件事，保持 diff 可读。

## PR 流程

1. 非小改动**先提 issue**（缺陷、设计讨论、新增用户可见参数），讨论清楚再动手。
2. 从 `main` 切分支，按上述规范做聚焦的小步提交。
3. 行为改动必须带测试（新增或适配既有用例），并同步文档：CLI 面变化时更新
   README 参数表与逐字帮助块，同时在 [CHANGELOG.md](CHANGELOG.md) 的
   `[Unreleased]` 增加条目。
4. 全量测试与 mypy 必须通过；CI 绿后才进入评审。
5. 维护者以 squash 合并；分支名请简短达意。

### 自查清单

- [ ] 提交信息符合 Conventional Commits 规范
- [ ] `python3 -m pytest tests/ -q` 全量通过
- [ ] `python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py` 通过
- [ ] 行为改动有 CLI seam 测试覆盖
- [ ] README / CHANGELOG 已同步

## 补充说明

- **运行产物全部在 .gitignore 内。** 哈希缓存、日志、结果 JSON、字节码与
  pytest 缓存都只是本地状态，不提交；手动验证时也不要用承受不起移动的数据。
- **代码风格**以就近一致为准：公共函数保留 docstring，命名用 snake_case，
  四空格缩进；不强制额外格式化工具。
- 有疑问直接提 issue，维护者都会看到。

---

# Complete English Guide

## Development environment

Requirements: Python **3.11+** (the tool relies on `hashlib.file_digest`).

```bash
git clone https://github.com/haotianfei/DupFinder.git
cd DupFinder
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]        # runtime: filelock;  dev: pytest, mypy
```

## Repository layout

| File | Role |
| --- | --- |
| `dup_finder.py` | Main CLI: two-phase scan, hash cache, actions, manifests, rollback (undo) |
| `pathlogic.py` | Pure functions only (no logging, no I/O side effects): path-component matching and result-file schema validation |
| `logger.py` | Logging configuration shared by the tools: console INFO milestones plus a rotating file handler |
| `change_priority.py` | Standalone utility to tune `priority` values inside a result JSON before reprocessing |

Keep these boundaries: `pathlogic.py` must stay import-cheap so
`change_priority.py` never pulls in logging or cache machinery.

## Running the checks

CI runs exactly these two gates — run them before pushing.

```bash
# 1) Test suite
python3 -m pytest tests/ -q

# fast subset only (skip the slow-marked integration cases)
python3 -m pytest tests/ -q -m "not slow"

# 2) Static typing
python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py
```

Notes on the suite:

- **Single seam: the CLI process boundary.** Every test drives the tools as a
  subprocess and asserts on external behavior only — exit code, log milestones,
  filesystem end state, artifact files (cache JSON, result JSON, move
  manifests). Never assert on internals and never import the module under test
  to poke at it.
- **Every test runs in its own private temp tree.** The `DupTree` fixture in
  `tests/conftest.py` copies the scripts into a per-test directory and scans
  only files inside it. Do not point tests at real user data, and do not write
  into the repository root — an autouse guard fails the run if repo-level
  artifacts (`file_cache.json`, `logs/`, `out/`, `duplicates_*.json`) change
  during a test.
- **`slow` marker.** Long-running integration cases (large fixture trees,
  cache-compatibility probes) carry `@pytest.mark.slow`; keep them out of the
  default quick loop locally, but CI runs them too — they must stay green.

## Commit conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/). Type
prefix + optional scope + imperative summary. Types: `feat`, `fix`, `test`,
`refactor`, `chore`, `docs`. Examples:

```text
feat(cli): add --min-free-percent guard to move actions
fix(cache): flush pending entries before exiting on SIGTERM
test(undo): cover manifests with occupied source locations
docs(readme): refresh parameter table to match --help
refactor(pathlogic): extract component matcher from dup_finder
chore(ci): pin mypy in dev extras
```

One logical change per commit; keep diffs reviewable.

## Pull-request workflow

1. **Open an issue first** for anything non-trivial (bug, design question,
   new user-facing option). Keep the discussion there before writing code.
2. Branch from `main`, make focused commits as above.
3. **Every behavior change ships with tests** (new cases or adapted ones), and
   docs move with it: update the README parameter table / help paste when the
   command-line surface changes, and add a line under `[Unreleased]` in
   [CHANGELOG.md](CHANGELOG.md).
4. Keep the full suite and mypy green; CI must pass before review.
5. A maintainer merges via squash; please keep your branch name short and
   descriptive.

### PR checklist

- [ ] Commit messages follow Conventional Commits
- [ ] `python3 -m pytest tests/ -q` green
- [ ] `python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py` green
- [ ] Tests cover the behavior change (CLI seam)
- [ ] README / CHANGELOG updated as needed

## Additional notes

- **Runtime artifacts are gitignored.** `file_cache.json`, `logs/`, `out/`,
  `duplicates_*.json`, `*.pyc` / `__pycache__`, `.pytest_cache/` are local
  state only — never commit them, and never point the tool at data you cannot
  afford to move while testing manually.
- **Formatting.** No separate formatter is enforced; match the surrounding
  style (4-space indent, snake_case, docstrings on public functions).
- Questions? Open an issue — the maintainers read them.
