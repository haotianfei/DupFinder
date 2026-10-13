# DupFinder

## 简介
`DupFinder` 是一个用于查找并处理重复文件的 Python 工具。它可以通过计算文件的哈希值来识别重复文件，并根据指定的保留优先级规则（修改时间、路径等）保留唯一文件，同时根据需求对其他重复文件执行删除或移动操作。

## 功能特性
- **查找重复文件**：按大小与哈希值（默认 SHA-256，可切换算法）识别重复文件。哈希值相同但大小不同的极端情形（摘要碰撞，极罕见）以警告呈现并整组保留，交人工甄别。
- **大小预过滤**：先按文件大小分组，仅大小相同的候选才计算哈希；大小全局唯一的文件不读内容，其缓存条目记录专用哨兵值，后续运行继续短路。
- **扫描安全基线**：0 字节文件与软链接默认不参与去重（`--min-size` 可一般化）；单个不可读或中途消失的文件只产生一条警告并继续整轮扫描。
- **保留文件关键字过滤**：`--retain` 按路径分量匹配——关键字须等于某个完整路径分量、或作为连续分量序列出现；例如 `dirA` 命中 `dirA/...` 下的文件，但不会命中兄弟目录 `dirA-nm/...`。
- **排除关键字过滤**：`--exclude` 按路径子串匹配，命中的文件在扫描阶段即排除。
- **跨目录专属去重**：`--cross-dirs-only` 只处理成员分布在两个及以上扫描根之间的重复组，目录自身内部的重复整组跳过。
- **执行前审查**：`--export-candidates` 在任何动作前把"将被处理的文件"逐行导出为纯文本清单，与实际处理队列逐行一致；可配合 `--try-run` 只导清单不执行。
- **可回滚的移动**：使用 `--move-to-dir` 的移动会把每条成功移动即时记入回收目录根部的 `moves_<时间戳>.jsonl` 清单；`--undo` 按清单反向恢复，原位已被占用则跳过不覆盖，并清理空目录骨架、输出计数核对。
- **哈希缓存**：`file_cache.json` 批量写（`--cache-batch`），原子替换落盘；中断（Ctrl-C / SIGTERM）时已算好的条目不丢；缓存条目按哈希算法命名空间隔离，默认 sha256 与既有历史条目逐字节兼容。
- **同步目录警示**：扫描根的直接子项含常见外部同步工具的目录标记（`.stfolder`、`.sync` 前缀）时，在处理动作前打印"移动/删除会向对端设备传播"警示——只提示不阻塞。
- **日志**：控制台输出 INFO 级里程碑；扫描按每 1000 个文件一条进度记录，不再有每文件级日志；文件日志保存在 `logs/DupFinder.log`（按天轮转，保留 3 天）。
- **生成文件**：程序执行时可能生成以下文件：
  - `file_cache.json`：哈希缓存，供后续运行免重算；
  - `duplicates_YYYYMMDD_HHMMSS.json`：每次查找结果（`--try-run` 不写）；
  - `<回收目录>/moves_YYYYMMDD_HHMMSS_ffffff.jsonl`：移动清单（仅实际发生移动时产生，供 `--undo` 使用）。

## 技术栈
- 编程语言：Python 3.11+（哈希流式读取使用 `hashlib.file_digest`）
- 主要模块：`os`, `sys`, `json`, `glob`, `stat`, `shutil`, `signal`, `hashlib`, `tempfile`, `argparse`, `subprocess`, `datetime`, `time`, `logging`
- 第三方依赖：`filelock`

## 安装
1. 克隆仓库：
   ```bash
   git clone https://github.com/your-repo/dup_finder.git
   cd dup_finder
   ```

2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
   或连同开发依赖以可编辑方式安装（含 pytest 测试入口）：
   ```bash
   pip install -e .[dev]
   ```

3. 运行测试：
   ```bash
   python -m pytest            # 全量；快速档可加 -m "not slow"
   ```

## 使用方法
### 命令行参数
`DupFinder` 提供了多种命令行参数以满足不同需求：

| 参数 | 类型 | 描述 |
| --- | --- | --- |
| `-d, --directories` | 除 `--undo` 外必选 | 查找重复文件的目录，可以输入多个目录，以空格分隔 |
| `--action` | 可选，默认为 `move` | 对重复文件执行的操作，可选值为 `delete` 或 `move` |
| `--priority-order, -p` | 可选 | 自定义优先级顺序，默认为 `modified_time path`：更早修改的文件优先保留（可能是更有保留价值的原件），路径层级更深的文件优先保留（更细的整理位置） |
| `--move-to-dir, -m` | 可选 | 移动文件的目标目录；未指定时就地改名追加 `.dup_finder` 后缀 |
| `--try-run, -n` | 可选 | 尝试运行模式：仅打印操作，不实际执行，也不写哈希缓存与结果文件 |
| `--min-size` | 可选，默认 1（字节） | 参与重复判定的最小文件字节数；更小的文件（默认即 0 字节文件）在扫描阶段跳过并计入汇总 |
| `--cache-batch` | 可选，默认 2000 | 内存中累积多少条新哈希后批量重写一次缓存文件；扫描结束与中断时各再落盘一次 |
| `--hash-algo` | 可选，默认 `sha256` | 识别重复内容的哈希算法（可选 `sha256/sha512/sha1/md5/blake2b/blake2s`）。缓存条目按算法命名空间隔离：非默认算法以 `algo:` 前缀存储，不与其它算法的运行互相命中；默认 sha256 保持历史裸十六进制格式逐字节可复用 |
| `--min-free-percent` | 可选，默认 5 | 目标卷剩余空间低于该百分比时拒绝移动；剩余字节不足以容纳单个文件时同样拒绝 |
| `--cross-dirs-only` | 可选，默认关闭 | 只处理成员分布在两个及以上扫描根的重复组；单一目录内部的重复整组跳过 |
| `--exclude` | 可选 | 排除关键字，路径**包含**该关键字（子串）的文件在扫描阶段即排除，适合临时文件等 |
| `--exclude-file` | 可选 | 同上，但将排除关键字写入一个文件中，每行一个关键字 |
| `--retain` | 可选 | 强制保留关键字，按**路径分量**匹配（等于某个完整分量、或连续分量序列），命中的文件提升到最高优先级 0；例如 `dirA` 只命中 `dirA/...`，不会命中 `dirA-nm/...` |
| `--retain-file` | 可选 | 同上，但将强制保留关键字写入一个文件中，每行一个关键字 |
| `--duplicates-result-file` | 可选 | 从既有结果 JSON 载入分组继续处理（跳过扫描）；载入前会校验结构，缺 `path`/`priority` 等字段时逐条报错并零动作退出 |
| `--export-candidates` | 可选（PATH） | 在任何动作执行前，把将被处理的文件清单（绝对源路径，一行一个）写入 PATH，与实际操作队列逐行一致；父目录自动创建；与 `--try-run` 组合即可"只导清单不执行" |
| `--undo` | 可选（PATH） | 回滚模式：读取 `moves_*.jsonl` 清单（单个文件或含清单的目录），把每条记录从目标位置搬回原位；原位已被占用则跳过不覆盖（保护回滚期间新数据），结束后清理回收目录内变空的目录骨架并打印 restored/skipped_conflict/missing/total 计数。与 `-d` 互斥 |

### 文件移动增强功能
- **完整路径保存**：目标目录下会创建以 `file_id` 命名的子目录，并保留原始文件的完整目录结构
- **彻底解决文件名长度限制**：通过保持原始文件名和目录结构，完全规避操作系统对文件名长度（255字节）的限制
- **空间检查机制**：移动前自动检查目标目录可用空间（`--min-free-percent`），避免因空间不足导致操作失败
- **可追溯与可回滚**：每条成功移动即时写入 `moves_*.jsonl`（`{"src": ..., "dst": ...}`），`--undo` 一键恢复

### 优先级排序规则
- **修改时间排序**：更早修改的文件具有更高优先级（保留价值更高的原件通常更早定稿）
- **路径深度优先**：路径层级更深的文件优先保留
- **保留关键字规则**：命中 `--retain`/`--retain-file` 的文件始终具有最高优先级（0）

### 命令行帮助
```
$ python dup_finder.py --help
usage: dup_finder.py [-h] [--directories DIRECTORIES [DIRECTORIES ...]]
                     [--action {delete,move}]
                     [--priority-order PRIORITY_ORDER [PRIORITY_ORDER ...]]
                     [--move-to-dir MOVE_TO_DIR] [--try-run]
                     [--min-size MIN_SIZE] [--cache-batch CACHE_BATCH]
                     [--hash-algo {sha256,sha512,sha1,md5,blake2b,blake2s}]
                     [--min-free-percent PERCENT] [--cross-dirs-only]
                     [--exclude EXCLUDE [EXCLUDE ...]]
                     [--exclude-file EXCLUDE_FILE]
                     [--retain RETAIN [RETAIN ...]]
                     [--retain-file RETAIN_FILE]
                     [--duplicates-result-file DUPLICATES_RESULT_FILE]
                     [--undo PATH] [--export-candidates PATH]

Find and process duplicate files.

options:
  -h, --help            show this help message and exit
  --directories, -d DIRECTORIES [DIRECTORIES ...]
                        Directories to search for duplicate files (required
                        unless --undo is used)
  --action {delete,move}
                        Action to process files (default: move)
  --priority-order, -p PRIORITY_ORDER [PRIORITY_ORDER ...]
                        Custom priority order: default is modified_time, path
  --move-to-dir, -m MOVE_TO_DIR
                        Directory to move files to (if not specified, rename
                        files with .dup_finder suffix)
  --try-run, -n         Try run mode: only print actions without executing
                        them
  --min-size MIN_SIZE   Minimum file size in bytes to participate in duplicate
                        detection; files smaller than this (including 0-byte
                        files, by default) are skipped during scanning and
                        counted in the scan summary (default: 1)
  --cache-batch CACHE_BATCH
                        Number of newly hashed files to accumulate in memory
                        before the hash cache file is rewritten in one batch;
                        the cache is also flushed once when the scan ends and
                        once more if the scan is interrupted (default: 2000)
  --hash-algo {sha256,sha512,sha1,md5,blake2b,blake2s}
                        Hash algorithm used to identify duplicate content
                        (default: sha256). Cache entries are namespaced per
                        algorithm: ids stored by a non-default algorithm are
                        prefixed 'algo:' and are never exchanged with other
                        algorithms' runs, while the default sha256 keeps the
                        historical bare-hex cache format fully reusable byte-
                        for-byte
  --min-free-percent PERCENT
                        Refuse a move when the destination volume's free space
                        drops below this percentage of its total capacity
                        (default: 5); a move is also refused when the free
                        bytes cannot hold the single file
  --cross-dirs-only     Only act on duplicate groups whose files span two or
                        more of the scan roots given by -d; groups lying
                        entirely inside one root are skipped as a whole.
                        Example: dup_finder.py -d A B --cross-dirs-only
                        --action move -m trash moves B's files that also exist
                        in A while leaving A itself, and B's own internal
                        duplicates, untouched. (default: off)
  --exclude EXCLUDE [EXCLUDE ...]
                        Exclude files use keywords,
  --exclude-file EXCLUDE_FILE
                        File containing exclude keywords, one per line
  --retain RETAIN [RETAIN ...]
                        Retain keywords,
  --retain-file RETAIN_FILE
                        File containing retain keywords, one per line
  --duplicates-result-file DUPLICATES_RESULT_FILE
                        File containing the duplicates result JSON data
  --undo PATH           Rollback mode: replay a moves_*.jsonl manifest (a
                        file path, or a directory containing them) and move
                        each recorded file from its destination back to its
                        original source. Never overwrites a source location
                        that is already occupied, prunes emptied directory
                        skeletons under the recycle dir, and prints a
                        restored/skipped/missing/total count summary at the
                        end. Mutually exclusive with -d/--directories.
                        Example: dup_finder.py --undo trash
  --export-candidates PATH
                        Before any action runs, write the list of files that
                        will be processed to PATH, one absolute source path
                        per line, matching exactly the selected processing
                        queue (after cross-root filtering and priority/retain
                        selection). Works with delete, move or --try-run;
                        combine with --try-run to only review the list, since
                        try-run still writes no cache, result or manifest
                        files. The output file is a user-requested artifact;
                        its parent directories are created as needed, and the
                        run stops before acting if the list cannot be written.
```

## 调整优先级工具
我们提供 `change_priority.py` 用于对生成的 duplicates.json 进行优先级微调：

### 使用场景
当自动设置的优先级不符合预期时，可通过路径关键字批量调整优先级值（数值越小优先级越高）。关键字匹配与 `--retain` 同语义（按路径分量）。

### 命令行参数
```bash
$ python change_priority.py --help
usage: change_priority.py [-h] -i INPUT -o OUTPUT -p PATH -a ADJUST

调整重复文件优先级

options:
  -h, --help           show this help message and exit
  -i, --input INPUT    输入JSON文件路径
  -o, --output OUTPUT  输出JSON文件路径
  -p, --path PATH      目标路径关键字，按路径分量匹配（与 dup_finder --retain
                       同语义）：关键字须等于某个完整路径分量、或作为连续分量序列出现；例如 dirA 命中 dirA/...
                       但不会命中兄弟目录 dirA-nm/...
  -a, --adjust ADJUST  调整值。优先级数值越小越优先保留；本工具计算 priority - adjustment（下限
                       0），所以正值提升保留优先级、负值降低
```

### 示例
1. 查找并移动重复文件到指定目录：
   ```bash
   python dup_finder.py -d /path/to/dir1 /path/to/dir2 --action move --move-to-dir /path/to/move_dir
   ```

2. 查找并删除重复文件，保留包含特定关键字的文件：
   ```bash
   python dup_finder.py -d /path/to/dir1 --retain important --action delete
   ```

3. 使用自定义优先级顺序查找并处理重复文件：
   ```bash
   python dup_finder.py -d /path/to/dir1 --priority-order modified_time path --action move
   ```

4. 尝试运行模式，仅打印操作，不实际执行：
   ```bash
   python dup_finder.py -d /path/to/dir1 --try-run
   ```

5. 使用简写形式尝试运行模式：
   ```bash
   python dup_finder.py -d /path/to/dir1 -n
   ```

6. 排除包含特定关键字的文件，不进行文件重复判断：
   ```bash
   python dup_finder.py -d /path/to/dir1 --exclude temp --exclude pdf
   ```

7. 使用排除文件排除特定关键字的文件：
   ```bash
   python dup_finder.py -d /path/to/dir1 --exclude-file exclude_keywords.txt
   ```

8. 调整重复文件生成结果文件的优先级：
   ```bash
   # 提升路径分量含 "seagate4000g" 的文件的保留优先级（数值减少 1）
   python change_priority.py -i duplicates.json -o adjusted.json -p seagate4000g -a 1
   ```

9. 只审查不执行：导出待处理清单（一行一个绝对路径），人工确认后再正式运行：
   ```bash
   python dup_finder.py -d A B --action move -m trash --export-candidates review_list.txt --try-run
   less review_list.txt      # 确认无误后去掉 --try-run 再执行
   ```

10. 跨目录专属去重：去掉 B 中与 A 重复的文件，A 自身与 B 内部重复都不动：
    ```bash
    python dup_finder.py -d /backup/a /backup/b --cross-dirs-only --action move -m /backup/b_trash
    ```

11. 误移之后一键回滚（原位已被占用的条目自动跳过不覆盖）：
    ```bash
    python dup_finder.py --undo /backup/b_trash
    ```

12. 换用更长摘要算法做更严格的比对（缓存条目按算法隔离，sha256 历史缓存不受影响）：
    ```bash
    python dup_finder.py -d /path/to/dir1 --hash-algo sha512 --try-run
    ```

## 注意事项
- 对疑似活跃同步目录（扫描根下存在 `.stfolder` / `.sync` 前缀标记）执行移动或删除，会随同步传播到对端设备；工具会在动作前给出警示，请留意日志。
- 处理前建议先用 `--try-run` 与 `--export-candidates` 审查待处理清单。

## 贡献
欢迎贡献代码！请阅读 [贡献指南](CONTRIBUTING.md) 以了解如何参与开发。

## 许可证
本项目采用 GPL-3.0 许可证，详情参见 [LICENSE](LICENSE) 文件。

---

希望这个 README 文件能帮助你更好地理解和使用 `DupFinder`。如果有任何问题或建议，请随时提 issue 或者联系开发者.
