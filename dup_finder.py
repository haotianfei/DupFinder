import os
import sys
import json
import glob
import stat
import shutil
import signal
import hashlib
import tempfile
import argparse
import subprocess
import datetime
from filelock import FileLock
from logger import configure_logger


logger = configure_logger("DupFinder")


# 缓存哨兵：标记"文件大小全局唯一，未读内容、永不可能是重复项"。
# 硬约束：必须永远不可能等于任何十六进制摘要（sha256=64 位小写 hex；此处含非 hex 字符
# ':' 与 'Z'，且长度 25，任意 hash 输出都不与之相等），因此不会被误当作有效 file_id
# 参与分组比较，也不会在增量运行中被当成真实缓存命中。
UNIQUE_SIZE_SENTINEL = "Z:unique-size"


class CacheInterrupted(Exception):
    """Raised when SIGINT/SIGTERM arrives while the scan is running.

    The scan loop catches it, flushes the pending cache entries once inside the
    lock, then re-raises so the process exits with a non-zero status without
    touching any files in the action phase.
    """

    def __init__(self, signum):
        super().__init__(f"interrupted by signal {signum}")
        self.signum = signum


def get_file_hash(file_path, hash_algo=hashlib.sha256):
    """Calculate the hash of a file, digesting it as a stream via hashlib.file_digest.

    `hash_algo` accepts an algorithm name string or a no-argument hashlib-style
    constructor; the resulting hex digest is byte-for-byte identical to the
    previous manual chunked update loop, so cached values stay compatible.
    """
    algorithm = hash_algo if isinstance(hash_algo, str) else hash_algo().name
    with open(file_path, 'rb') as f:
        return hashlib.file_digest(f, algorithm).hexdigest()

def generate_file_identifier(file_path):
    """Generate a unique identifier for a file."""
    file_hash = get_file_hash(file_path, hashlib.sha256)
    return file_hash

def write_cache_to_file(cache, cache_file):
    """Write the cache to the cache file atomically.

    The temporary file lives in the cache file's own directory, so the final
    os.replace is both same-filesystem and atomic: readers of the cache file
    only ever see a complete JSON snapshot, never a half-written state. If the
    process dies mid-write, at most an orphaned '*.tmp' file is left next to
    the cache (the cache itself is untouched) and later runs can simply
    overwrite or ignore it.
    """
    cache_dir = os.path.dirname(os.path.abspath(cache_file))
    with tempfile.NamedTemporaryFile(
        'w', dir=cache_dir,
        prefix=os.path.basename(cache_file) + '.', suffix='.tmp',
        delete=False,
    ) as temp_file:
        json.dump(cache, temp_file)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file_path = temp_file.name
    os.replace(temp_file_path, cache_file)

def load_cache(cache_file):
    """Load the cache from the cache file."""
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, 'r') as f:
            return json.load(f)
    except json.decoder.JSONDecodeError:
        os.remove(cache_file)
        return {}

def update_cache(cache, file_path, file_id, st=None):
    """Update the cache with the file information.

    Pass `st` (an os.stat_result already collected for this path) to reuse its
    size/mtime, so each path is stat'ed only once per scan. The on-disk JSON
    entry format is unchanged: {'file_id', 'modified_time', 'size'}.
    """
    if st is None:
        st = os.stat(file_path)
    cache[file_path] = {
        'file_id': file_id,
        'modified_time': st.st_mtime,
        'size': st.st_size
    }

def get_file_id(file_path, cache, st=None):
    """Process a single file and return its file ID.

    The cache hit check reuses the same stat snapshot (`st`) instead of extra
    getmtime/getsize calls: when cached modified_time and size match the
    snapshot and the cached value is a real digest (not UNIQUE_SIZE_SENTINEL),
    the cached file_id is returned as-is (no re-hash). A sentinel never counts
    as a hit: if this file's size became shared with another file, its content
    must actually be hashed now. Otherwise the hash is recomputed and the cache
    entry updated from the same snapshot.
    Raises OSError (incl. PermissionError) for unreadable/vanished files, which
    the caller handles per file.
    """
    if st is None:
        st = os.stat(file_path)

    file_info = cache.get(file_path)
    if file_info:
        cached_modified_time = file_info.get('modified_time')
        cached_size = file_info.get('size')
        cached_file_id = file_info.get('file_id')

        if cached_file_id != UNIQUE_SIZE_SENTINEL and \
                cached_modified_time == st.st_mtime and cached_size == st.st_size:
            return cached_file_id

    file_id = generate_file_identifier(file_path)
    update_cache(cache, file_path, file_id, st)
    return file_id

def parse_exclude_file(exclude_file):
    """Parse the exclude file and return a list of keywords."""
    try:
        with open(exclude_file, 'r') as file:
            return [line.strip() for line in file if line.strip()]
    except Exception as e:
        logger.error(f"Error reading exclude file {exclude_file}: {e}")
        return []


# 同步文件夹标记：扫描根直接子项中名字以下列前缀开头的条目（标记本体及其变体
# 后缀都能覆盖）。只用于警示识别，不参与任何过滤或决策。
SYNC_FOLDER_MARKER_PREFIXES = (".stfolder", ".sync")


def find_sync_marker(directory: str) -> str | None:
    """Return the name of the first sync-folder marker among the direct entries
    of a scan root, or None when there is no marker / the listing fails."""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return None
    for entry in entries:
        if entry.startswith(SYNC_FOLDER_MARKER_PREFIXES):
            return entry
    return None


def warn_if_sync_marker(directory: str) -> None:
    """Warn (never block) when a scan root looks like an active synchronized folder.

    move/delete actions inside such directories propagate to the other devices
    sharing the folder, so the operator sees this before the action phase.
    """
    marker = find_sync_marker(directory)
    if marker:
        logger.warning(
            "Scan root %s appears to be an active synchronized folder (marker %s): "
            "move/delete actions will propagate to peer devices.",
            directory, marker
        )

def find_duplicates(directories, cache_file='file_cache.json', batch_size=2000, exclude_keywords=None, min_size=1, try_run=False, cross_dirs_only=False):
    """Find duplicate files in the given directories.

    Two-phase size prefiltering:
    - phase one walks the trees collecting one os.stat snapshot per entry and
      groups candidates by size only — no file content is read at all;
    - a file whose size is unique across the whole scan can never have a
      duplicate, so its content is never hashed: it gets the
      UNIQUE_SIZE_SENTINEL cache entry instead and never enters any grouping
      comparison;
    - phase two hashes (via cache-aware get_file_id) only the files sharing a
      size with at least one other candidate. Sentinel cache entries never
      count as hits: when such a file later shares its size with another one,
      its content is hashed for real.

    Scan safety baseline:
    - entries that are symlinks, or smaller than `min_size` bytes (0-byte files
      are excluded with the default min_size=1), never become duplicate
      candidates; they are counted and reported in a final summary line;
    - a per-file OSError (e.g. unreadable or vanished mid-scan) yields exactly
      one warning and the scan continues;
    - each path is os.stat'ed once and the snapshot feeds cache comparison,
      sentinel bookkeeping and the result dict alike, avoiding multi-stat race
      windows.

    Cache write policy:
    - sentinel entries and hashed entries go through the same batched flush:
      the cache file is rewritten once `batch_size` accumulated entries have
      built up, once again when the scan finishes normally, and once more if
      SIGINT/SIGTERM interrupts the scan (the flush happens inside the lock;
      afterwards CacheInterrupted is raised and no action is taken on the
      partially scanned data);
    - `try_run` writes nothing at all: the existing cache is still read and
      reused in memory, but no cache file, result file or temp file is created.

    Files skipped before any bookkeeping (symlinks, below min-size, errored)
    get no cache entries, so the existing cache file format and all reusable
    sha256 entries stay untouched.

    Cross-directory scope:
    - with `cross_dirs_only` enabled, a duplicate group only survives when its
      members are spread across 2 or more of the `-d` scan roots; groups that
      are entirely internal to one root are dropped after grouping (they never
      enter the result dict nor the action phase). Off by default, in which
      case this step does not run and behavior is exactly as before.
    """
    lock_file = f"{cache_file}.lock"
    lock = FileLock(lock_file)

    n_empty = 0
    n_small = 0
    n_symlink = 0
    n_error = 0
    n_dirs_scanned = 0
    interrupted = None
    # 信号处理器只记录信号号；异常在扫描循环的安全点抛出。
    # 不在 handler 里直接 raise：中断可能恰好落在 logging/文件写入等库函数
    # 内部，那些函数有自己的宽泛异常处理，会把外来异常吞掉，导致中断丢失。
    pending_signals = []

    def _stop_on_signal(signum, frame):
        pending_signals.append(signum)

    def _check_interrupt():
        if pending_signals:
            raise CacheInterrupted(pending_signals.pop(0))

    def _log_interrupt(signum):
        # 中断落盘措辞按模式区分：try-run 本就不写缓存，不能谎称在 flush
        if try_run:
            logger.info("Scan interrupted by signal %d (try-run, no cache written)", signum)
        else:
            logger.info("Scan interrupted by signal %d; flushing pending cache entries", signum)

    with lock:
        cache = load_cache(cache_file)
        file_dict = {}
        cache_updates = []

        previous_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _stop_on_signal)
        try:
            # ---- 阶段一：仅收集 (size, stat 快照)，不读任何文件内容 ----
            size_groups = {}
            directories = list(set(directories))
            for directory in directories:
                n_dirs_scanned += 1
                logger.info("Processing directory: %s", directory)
                warn_if_sync_marker(directory)
                for root, _, files in os.walk(directory):
                    for file in files:
                        # 每个文件开头检查一次待处理信号（安全点，粒度为单文件，响应为毫秒级）
                        _check_interrupt()
                        file_path = os.path.join(root, file)
                        logger.debug("Collecting file: %s", file_path)
                        # 检查文件路径是否包含排除关键字
                        if exclude_keywords and any(keyword in file_path for keyword in exclude_keywords):
                            logger.debug(f"Excluding file: {file_path}")
                            continue
                        try:
                            # 软链接条目不参与重复判定
                            if os.path.islink(file_path):
                                n_symlink += 1
                                logger.debug("Skipping symlink: %s", file_path)
                                continue
                            # 单一路径一次 os.stat，快照贯穿缓存比对、哨兵登记与结果字典
                            st = os.stat(file_path)
                            if not stat.S_ISREG(st.st_mode):
                                logger.debug("Ignoring non-regular file: %s", file_path)
                                continue
                            # 小于阈值的文件（默认 min_size=1，即排除 0 字节文件）跳过
                            if st.st_size < min_size:
                                if st.st_size == 0:
                                    n_empty += 1
                                n_small += 1
                                logger.debug("Skipping file below min-size: %s", file_path)
                                continue
                        except (OSError, PermissionError) as e:
                            # 连 stat 都失败的坏文件（中途消失等）也只警告一次并继续整轮扫描
                            logger.warning(f"Error accessing file: {file_path} - {e}")
                            n_error += 1
                            continue
                        size_groups.setdefault(st.st_size, []).append((file_path, st))

            # ---- size 全局唯一的文件：登记哨兵，不读内容，永不进分组 ----
            hash_queue = []
            for i, size in enumerate(sorted(size_groups)):
                if i % 500 == 0:
                    _check_interrupt()
                group = size_groups[size]
                if len(group) == 1:
                    file_path, st = group[0]
                    update_cache(cache, file_path, UNIQUE_SIZE_SENTINEL, st)
                    cache_updates.append(file_path)
                    logger.debug("Unique size, recorded sentinel without hashing: %s", file_path)
                else:
                    hash_queue.extend(group)
            _check_interrupt()
            # 哨兵条目与真实哈希条目共用同一批量落盘通道
            if len(cache_updates) >= batch_size:
                if not try_run:
                    write_cache_to_file(cache, cache_file)
                cache_updates.clear()

            # ---- 阶段二：仅同 size 候选 >= 2 的文件才真正读取内容做哈希 ----
            for file_path, st in hash_queue:
                _check_interrupt()
                try:
                    file_id = get_file_id(file_path, cache, st)
                except (OSError, PermissionError) as e:
                    # 不可读/中途消失的文件：一条警告后继续，缓存不写其条目
                    logger.warning(f"Error accessing file: {file_path} - {e}")
                    n_error += 1
                    continue

                file_info = {
                    'path': file_path,
                    'size': st.st_size,
                    'type': os.path.splitext(file_path)[1],
                    'modified_time': st.st_mtime
                }
                logger.debug("Process File ID: %s, File Info: %s", file_id, file_info)

                if file_id in file_dict:
                    file_dict[file_id].append(file_info)
                else:
                    file_dict[file_id] = [file_info]
                # Track updated cache entries
                cache_updates.append(file_path)

                # Write cache to file if batch size is reached
                if len(cache_updates) >= batch_size:
                    if not try_run:
                        write_cache_to_file(cache, cache_file)
                    cache_updates.clear()
            # 最后一个文件之后仍可能有信号到达，在结束扫描前再检查一次
            _check_interrupt()
        except CacheInterrupted as exc:
            interrupted = exc
            _log_interrupt(exc.signum)
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)

        # 正常结束与被中断退出都在锁内落盘一次残留条目；try-run 模式零正式写入
        if cache_updates and not try_run:
            write_cache_to_file(cache, cache_file)

        # 兜住"最后一次 _check_interrupt 已返回、finally 恢复旧 handler 之前"到达的
        # 信号：它只进了 pending_signals 却无人再消费，若不在此处收口就会被静默吞掉，
        # 中断会被误当作正常完成。
        if interrupted is None and pending_signals:
            interrupted = CacheInterrupted(pending_signals.pop(0))
            _log_interrupt(interrupted.signum)

        if interrupted is not None:
            # 中断路径同样给出跳过计数汇总（当前计数即中断时刻的快照），保持与正常路径一致的可观测性
            logger.info(
                "Scan interrupted after %d dirs scanned: skipped small/empty=%d (empty=%d), symlinks=%d, errors=%d",
                n_dirs_scanned, n_small, n_empty, n_symlink, n_error
            )
            raise interrupted

        # Filter out file_ids with only one element
        file_dict = {file_id: file_info_list for file_id, file_info_list in file_dict.items() if len(file_info_list) > 1}

    logger.info(
        "Scan summary: skipped small/empty=%d (empty=%d), symlinks=%d, errors=%d",
        n_small, n_empty, n_symlink, n_error
    )

    if cross_dirs_only:
        file_dict = apply_cross_dirs_scope(file_dict, directories)

    return file_dict

def split_path_components(path: str) -> list[str]:
    """Split a path into its directory/filename components.

    Both POSIX and Windows separators are normalized before splitting and
    empty components (leading or repeated separators) are dropped, so
    '/a/b', 'a/b' and 'a\\\\b' all yield the same component sequence.
    """
    return [component for component in path.replace("\\", "/").split("/") if component]


def path_matches_keyword(path: str, keyword: str) -> bool:
    """Component-aware keyword matching (replaces the old bare-substring rule).

    The keyword's component sequence matches when it occurs as a contiguous
    run of the path's components: a single-component keyword must equal one
    whole component; a multi-component keyword must align with consecutive
    components. Trailing separators on the keyword are ignored, so 'dirA' and
    'dirA/' behave identically. Because whole components are compared, 'dirA'
    matches files under the directory component 'dirA' but never a sibling
    named 'dirA-nm' — the bare substring rule matched both, which is how
    adjacent directories with a shared name prefix used to be caught in the
    same net. An empty keyword matches nothing.
    """
    keyword_components = split_path_components(keyword)
    if not keyword_components:
        return False
    path_components = split_path_components(path)
    width = len(keyword_components)
    return any(
        path_components[start:start + width] == keyword_components
        for start in range(len(path_components) - width + 1)
    )


def scan_root_components(directories: list[str]) -> list[list[str]]:
    """Normalized absolute component sequence for each scan root given by -d."""
    return [split_path_components(os.path.abspath(directory)) for directory in directories]


# 结果文件结构校验：消费（cross-dirs 裁剪 / 优先级选择 / 动作执行）之前的前置守卫。
# 选择"报错退出"而非"自动补排"：载入的语义是尊重文件里既有的优先级决定，
# 静默重排会改变使用者的意图；结构不合法时必须零动作把问题逐条讲清楚。
RESULT_SCHEMA_PROBLEM_LIMIT = 20


def validate_result_schema(data) -> list:
    """Return human-readable problems found in a loaded duplicates-result structure.

    Requirements: top level is a dict of group-key -> list of entry dicts; every
    entry carries a str 'path' and an int 'priority' (bool is rejected although
    it is an int subclass). An empty list means the structure is consumable.
    """
    if not isinstance(data, dict):
        return [f"top level must be an object mapping group keys to entry lists, "
                f"got {type(data).__name__}"]
    problems = []
    for group_key, entries in data.items():
        if not isinstance(entries, list):
            problems.append(f"group {group_key!r}: value must be a list of entries, "
                            f"got {type(entries).__name__}")
            continue
        for index, entry in enumerate(entries):
            location = f"group {group_key!r} entry #{index}"
            if not isinstance(entry, dict):
                problems.append(f"{location}: entry must be an object, "
                                f"got {type(entry).__name__}")
                continue
            path = entry.get("path")
            if not isinstance(path, str):
                problems.append(f"{location}: field 'path' missing or not a string")
            priority = entry.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int):
                problems.append(f"{location} (path={path!r}): "
                                f"field 'priority' missing or not an integer")
    return problems


def report_result_schema_problems(problems, source) -> None:
    """Log schema problems one per line, capped at RESULT_SCHEMA_PROBLEM_LIMIT,
    with the full total always stated."""
    logger.error("Invalid duplicates result file %s: %d problem(s) found",
                 source, len(problems))
    for problem in problems[:RESULT_SCHEMA_PROBLEM_LIMIT]:
        logger.error("  - %s", problem)
    if len(problems) > RESULT_SCHEMA_PROBLEM_LIMIT:
        logger.error("  ... and %d more problem(s) not listed",
                     len(problems) - RESULT_SCHEMA_PROBLEM_LIMIT)


def attribute_path_to_root(file_path: str, roots: list[list[str]]) -> int | None:
    """Index of the scan root that owns `file_path`, or None if under no root.

    Attribution is a component-boundary prefix match on absolute paths, and the
    most exact (longest) matching root wins so a nested root resolves to the
    inner one. Because whole components are compared, a root 'A' never claims a
    file that actually lives under a sibling root 'A-nm' — the same protection
    path_matches_keyword gives against prefix-shared siblings.
    """
    components = split_path_components(os.path.abspath(file_path))
    best_index: int | None = None
    best_len = -1
    for index, root in enumerate(roots):
        width = len(root)
        if width > best_len and components[:width] == root:
            best_index = index
            best_len = width
    return best_index


def filter_cross_root_groups(
    file_dict: dict[str, list[dict]], directories: list[str]
) -> tuple[dict[str, list[dict]], int]:
    """Keep only duplicate groups whose members span 2 or more scan roots.

    A group where every file belongs to the same scan root (a directory's own
    internal duplicates) is dropped as a whole and never reaches the result file
    or the action phase. Returns the kept groups and the number of same-root
    groups that were skipped.
    """
    roots = scan_root_components(directories)
    kept: dict[str, list[dict]] = {}
    n_same_root_skipped = 0
    for file_id, files in file_dict.items():
        owners = {attribute_path_to_root(file_info['path'], roots) for file_info in files}
        if len(owners) >= 2:
            kept[file_id] = files
        else:
            n_same_root_skipped += 1
    return kept, n_same_root_skipped


def apply_cross_dirs_scope(file_dict: dict[str, list[dict]], directories: list[str]) -> dict[str, list[dict]]:
    """Apply the cross-dirs-only scope to an assembled result: keep only groups
    spanning 2 or more scan roots, and report a one-line summary only when at
    least one same-root group was actually dropped (log stays clean otherwise).
    """
    kept, n_same_root_skipped = filter_cross_root_groups(file_dict, directories)
    if n_same_root_skipped:
        logger.info(
            "Cross-dirs-only: skipped %d same-root duplicate group(s)",
            n_same_root_skipped
        )
    return kept


def assign_priorities(file_dict, retain_keywords, priority_order=None):
    """Assign priorities to files based on the given criteria.

    Retain keywords and the 'path' priority field share the same component
    machinery: keyword hits are decided by path_matches_keyword (contiguous
    whole components, separator-style normalized), and the path sort depth is
    measured in normalized components via split_path_components.
    """
    if priority_order is None:
        # Default priority order
        priority_order = ['modified_time', 'path']

    for file_id, files in file_dict.items():
        # 检查同一 file_id 下的文件大小是否一致
        file_sizes = {file['size'] for file in files}
        if len(file_sizes) > 1:
            # 哈希一致但大小不同：极罕见的摘要碰撞情形，不是故障——
            # 整组全部保留（priority 0），交给使用者人工甄别
            for file_info in files:
                file_info['priority'] = 0
            logger.warning(
                "Same digest with differing sizes (hash collision is rare, not fatal); "
                "all files in this group are kept for manual review: %s", file_id
            )
            for file_info in files:
                logger.warning(f"  Path: {file_info['path']}, Size: {file_info['size']}")
            continue

        priority_counter = 1  # Start from 1 for non-retained files
        files.sort(
            key=lambda x: tuple(
                x[order] if order == 'modified_time'  # 修改时间字段升序排列
                else (-len(split_path_components(x[order]))  # 路径字段按分量深度降序
                      if order == 'path'               # （斜杠风格归一，与关键字匹配共用分量切分）
                      else -x[order])                  # 其他数值字段降序排列
                for order in priority_order
            )
        )
        # Assign priorities to all files
        for file_info in files:
            # 保留关键字按路径分量匹配（连续整分量序列；见 path_matches_keyword）
            if retain_keywords and any(
                    path_matches_keyword(file_info['path'], keyword)
                    for keyword in retain_keywords):
                file_info['priority'] = 0
            else:
                file_info['priority'] = priority_counter
                priority_counter += 1


def select_files_for_processing(file_dict) -> list:
    """Return [(file_id, file_info), ...] in the exact order and set retain_files acts on.

    Selection rule (unchanged from the action phase itself): within each
    duplicate group, files are ordered by priority ascending; the lowest
    priority value is the retained baseline and every file with a strictly
    higher (worse) priority number joins the processing queue. Shared by
    retain_files and the candidate-list export so the exported list cannot
    drift from what actually gets processed.
    """
    queue = []
    for file_id, files in file_dict.items():
        files.sort(key=lambda x: x['priority'])
        highest_priority = files[0]['priority']
        for file in files:
            if file['priority'] > highest_priority:
                queue.append((file_id, file))
    return queue


def export_candidates(paths, out_path: str) -> bool:
    """Write the pending-processing list, one absolute source path per line.

    Pure output of the already-decided processing queue (cross-root filtering,
    retain keywords and priority selection have all run beforehand); it changes
    no selection itself. Missing parent directories of out_path are created.
    Returns True on success; on failure logs the error and returns False, and
    the caller stops before any action so a requested review artifact is never
    silently missing.
    """
    absolute_out = os.path.abspath(out_path)
    try:
        os.makedirs(os.path.dirname(absolute_out), exist_ok=True)
        with open(absolute_out, "w") as out_file:
            for path in paths:
                out_file.write(os.path.abspath(path) + "\n")
    except OSError as e:
        logger.error("Cannot write candidate list to %s: %s", out_path, e)
        return False
    logger.info("Candidate list exported: %s (%d file(s))", out_path, len(paths))
    return True


def retain_files(file_dict, action, move_to_dir=None, try_run=False, manifest=None):
    """Retain files based on the priority and process the rest."""
    for file_id, file in select_files_for_processing(file_dict):
        process_file(file, action, move_to_dir, try_run, file_id, manifest)


# def check_filename_length(filename):
#     """检查文件名字节长度是否超过限制（255字节）"""
#     byte_length = len(filename.encode('utf-8'))
#     return byte_length <= 255


def process_file(file, action, move_to_dir=None, try_run=False, file_id=None, manifest=None):
    # 操作类型校验
    if action not in ['delete', 'move']:
        logger.warning(f"Unsupported action: {action}. Skipping file: {file['path']}")
        return

    if action == 'delete':
        if try_run:
            logger.info(f"[TRY RUN] Would delete: {file['path']}")
        else:
            try:
                os.remove(file['path'])
                logger.info(f"Deleted: {file['path']}")
            except Exception as e:
                logger.error(f"Error deleting {file['path']}: {e}")
    elif action == 'move':
        if move_to_dir:
            # 创建file_id目录
            target_dir = os.path.join(move_to_dir, file_id)
            # 获取源文件的相对路径（基于系统根目录）
            source_dir = os.path.dirname(file['path'])
            relative_dir = os.path.relpath(source_dir, start='/')  # 转换为相对路径以避免绝对路径问题
            # 构建目标子目录
            target_subdir = os.path.join(target_dir, relative_dir)
            
            if not os.path.exists(target_subdir):
                os.makedirs(target_subdir)
            
            # 保持原始文件名
            file_name = os.path.basename(file['path'])
            new_path = os.path.join(target_subdir, file_name)
            
            if try_run:
                logger.info(f"[TRY RUN] Would move: {file['path']} to {new_path}")
            else:
                # 新增空间检查逻辑（开始）
                total, used, free = shutil.disk_usage(move_to_dir)
                file_size = os.path.getsize(file['path'])
                free_percent = (free / total * 100) if total > 0 else 0
                logger.debug(f"移动文件需要空间：{file_size} 字节，剩余空间: {free} 字节 ({free_percent:.1f}%)")

                # 空间不足判断
                if free < file_size or free_percent < 5:
                    logger.error(f"空间不足阻止移动：{file['path']} -> {new_path}")
                    logger.error(f"需要空间: {file_size} 字节 | 剩余空间: {free} 字节 ({free_percent:.1f}%)")
                    return None
                # 新增空间检查逻辑（结束）
                try:
                    shutil.move(file['path'], new_path)
                    logger.info(f"Moved: {file['path']} to {new_path}")
                    # 每条实际成功的移动即时写入 JSONL 清单（rename 模式无回收目录，不产清单）
                    if manifest is not None:
                        manifest.record(file['path'], new_path)
                except Exception as e:
                    logger.error(f"Error moving {file['path']} to {new_path}: {e}")
        else:
            new_path = file['path'] + '.dup_finder'
            if try_run:
                logger.info(f"[TRY RUN] Would rename: {file['path']} to {new_path}")
            else:
                try:
                    shutil.move(file['path'], new_path)
                    logger.info(f"Renamed: {file['path']} to {new_path}")
                except Exception as e:
                    logger.error(f"Error renaming {file['path']} to {new_path}: {e}")
def main(directories, action, priority_order=None, move_to_dir=None, try_run=False, exclude_keywords=None, retain_keywords=None, file_dict_path=None, min_size=1, cache_batch=2000, cross_dirs_only=False, undo=None, export_candidates_path=None):
    if undo is not None:
        # --undo 与扫描互斥：只按清单反向恢复，不扫描、不读缓存、不产结果文件
        manifests = collect_move_manifests(undo)
        if not manifests:
            logger.error("Undo target has no moves_*.jsonl manifest to replay: %s", undo)
            sys.exit(1)
        counts = undo_moves(manifests)
        logger.info(
            "Undo summary: restored=%d skipped_conflict=%d missing=%d total=%d",
            counts["restored"], counts["skipped_conflict"],
            counts["missing"], counts["total"]
        )
        return

    manifest = None
    if action == 'move' and move_to_dir and not try_run:
        # 清单落在回收目录根部，随数据自包含；rename 模式（无 -m）与演练/删除不产清单
        # 微秒粒度：同秒多次运行各自产出独立清单
        current_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        manifest = MoveManifest(os.path.join(move_to_dir, f"moves_{current_time}.jsonl"))

    if file_dict_path:
        # 从指定文件中加载 file_dict
        with open(file_dict_path, 'r') as f:
            file_dict = json.load(f)
        # 前置守卫：结构不合法则逐条报错并退出，绝不带着坏数据进入裁剪/选择/动作阶段
        problems = validate_result_schema(file_dict)
        if problems:
            report_result_schema_problems(problems, file_dict_path)
            sys.exit(1)
        logger.info(f"Loaded file_dict from {file_dict_path}")
        if cross_dirs_only:
            # 载入的历史结果同样按本次传入的扫描根做跨目录专属裁剪
            file_dict = apply_cross_dirs_scope(file_dict, directories)
    else:
        # 找到重复文件（try-run 时扫描全程不写正式缓存）
        file_dict = find_duplicates(directories, exclude_keywords=exclude_keywords,
                                    min_size=min_size, try_run=try_run,
                                    batch_size=cache_batch,
                                    cross_dirs_only=cross_dirs_only)
        assign_priorities(file_dict, retain_keywords, priority_order=priority_order)
        if try_run:
            # 演练模式不写重复结果文件，正式状态零污染
            logger.info("Try run mode: %d duplicate group(s) found; no cache or result file written",
                        len(file_dict))
        else:
            # 保存 file_dict 到文件
            current_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"duplicates_{current_time}.json"
            with open(output_file, 'w') as f:
                json.dump(file_dict, f, indent=4)
            logger.info(f"Saved file_dict to {output_file}")

    # 待处理候选清单：在处理队列确定（cross-dirs 过滤 + retain/优先级选择）之后、
    # 任何动作执行之前导出；写失败则停止，绝不带着未审查的清单继续执行
    if export_candidates_path is not None:
        pending_paths = [file_info['path'] for _file_id, file_info in select_files_for_processing(file_dict)]
        if not export_candidates(pending_paths, export_candidates_path):
            sys.exit(1)

    retain_files(file_dict, action, move_to_dir, try_run, manifest)
    if manifest is not None:
        manifest.log_summary()


class MoveManifest:
    """JSONL manifest of actually-successful moves, living at the recycle-dir root.

    One {\"src\": ..., \"dst\": ...} line per successful move, appended with the
    absolute paths and flushed immediately, so a process dying mid-run still
    leaves every completed entry on disk. A write failure only warns and is
    counted; it never aborts the run. The file itself is created lazily on the
    first recorded move, so runs without successful moves leave no manifest.
    """

    def __init__(self, path: str):
        self.path = path
        self.entries = 0
        self.write_failures = 0

    def record(self, src: str, dst: str) -> None:
        entry = {"src": os.path.abspath(src), "dst": os.path.abspath(dst)}
        try:
            with open(self.path, "a") as manifest_file:
                manifest_file.write(json.dumps(entry) + "\n")
                manifest_file.flush()
            self.entries += 1
        except OSError as e:
            self.write_failures += 1
            logger.warning(f"Failed to record move {src} -> {dst} in manifest: {e}")

    def log_summary(self) -> None:
        if self.entries:
            logger.info("Move manifest: %s (%d entries)", self.path, self.entries)
        if self.write_failures:
            logger.warning("Move manifest write failures: %d", self.write_failures)


def collect_move_manifests(target: str) -> list[str]:
    """Resolve a --undo target into manifest files.

    A directory contributes every moves_*.jsonl directly inside it; a file path
    is taken as a single manifest. Returns an empty list when nothing matches.
    """
    if os.path.isdir(target):
        return sorted(glob.glob(os.path.join(target, "moves_*.jsonl")))
    if os.path.isfile(target):
        return [target]
    return []


def prune_empty_dirs_under(boundary: str, start_paths) -> None:
    """Delete directories that became empty inside the recycle dir, bottom-up.

    Walks from each start directory upward but stops at the boundary (the
    recycle-dir root): the boundary itself is never removed and nothing outside
    it is ever touched.
    """
    boundary = os.path.abspath(boundary)
    for start in start_paths:
        current = os.path.abspath(start)
        while current.startswith(boundary + os.sep):
            try:
                os.rmdir(current)  # only removes truly empty dirs
            except OSError:
                break  # non-empty or vanished: stop climbing this branch
            current = os.path.dirname(current)


def undo_moves(manifest_paths: list[str]) -> dict:
    """Restore recorded moves by replaying every {src, dst} entry in reverse.

    Rules (mirroring the battle-tested manual recovery procedure): an existing
    path at src is never overwritten (counted as skipped_conflict, the new
    data stays), a dst that no longer exists is counted as missing, and missing
    parent directories of src are recreated before restoring. shutil.move is
    used exactly like the forward move, so permissions and timestamps survive
    (same-device rename; cross-device falls back to copy2 + delete). Empty
    directory skeletons inside the recycle dir are pruned afterwards.
    Returns counts {\"restored\", \"skipped_conflict\", \"missing\", \"total\"}.
    """
    counts = {"restored": 0, "skipped_conflict": 0, "missing": 0, "total": 0}
    prune_starts: dict[str, set] = {}

    for manifest_path in manifest_paths:
        boundary = os.path.dirname(os.path.abspath(manifest_path))
        starts = prune_starts.setdefault(boundary, set())
        with open(manifest_path, "r") as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("manifest line is not a JSON object")
                    src = entry["src"]
                    dst = entry["dst"]
                except (ValueError, KeyError) as e:
                    logger.warning(
                        "Malformed manifest entry at %s:%d, skipped: %s",
                        manifest_path, line_number, e
                    )
                    continue

                counts["total"] += 1
                starts.add(os.path.dirname(os.path.abspath(dst)))

                if os.path.lexists(src):
                    # 原位已被占用：绝不覆盖，保护回滚期间新产生的数据
                    counts["skipped_conflict"] += 1
                    logger.warning(
                        "Undo: original location already occupied, keeping it and "
                        "leaving %s in place: %s", dst, src
                    )
                    continue
                if not os.path.lexists(dst):
                    counts["missing"] += 1
                    logger.warning("Undo: recorded destination no longer exists: %s", dst)
                    continue

                parent = os.path.dirname(os.path.abspath(src))
                os.makedirs(parent, exist_ok=True)
                try:
                    shutil.move(dst, src)
                    counts["restored"] += 1
                    logger.info("Restored: %s to %s", dst, src)
                except Exception as e:
                    logger.error("Error restoring %s to %s: %s", dst, src, e)

    for boundary, starts in prune_starts.items():
        prune_empty_dirs_under(boundary, starts)

    return counts

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find and process duplicate files.")
    parser.add_argument("--directories", "-d", nargs='+', required=False,
                        help="Directories to search for duplicate files "
                             "(required unless --undo is used)")
    parser.add_argument("--action", choices=['delete', 'move'], required=False, default='move', help="Action to process files (default: move)")
    parser.add_argument("--priority-order", "-p", nargs='+', required=False, help="Custom priority order: default is modified_time, path")
    parser.add_argument("--move-to-dir", "-m", required=False, help="Directory to move files to (if not specified, rename files with .dup_finder suffix)")
    parser.add_argument("--try-run", "-n", action='store_true', required=False, help="Try run mode: only print actions without executing them")
    parser.add_argument("--min-size", type=int, default=1, required=False,
                        help="Minimum file size in bytes to participate in duplicate detection; "
                             "files smaller than this (including 0-byte files, by default) are "
                             "skipped during scanning and counted in the scan summary (default: 1)")
    parser.add_argument("--cache-batch", type=int, default=2000, required=False,
                        help="Number of newly hashed files to accumulate in memory before the hash "
                             "cache file is rewritten in one batch; the cache is also flushed once "
                             "when the scan ends and once more if the scan is interrupted (default: 2000)")
    parser.add_argument("--cross-dirs-only", action='store_true', required=False,
                        help="Only act on duplicate groups whose files span two or more of the "
                             "scan roots given by -d; groups lying entirely inside one root are "
                             "skipped as a whole. Example: dup_finder.py -d A B --cross-dirs-only "
                             "--action move -m trash moves B's files that also exist in A while "
                             "leaving A itself, and B's own internal duplicates, untouched. "
                             "(default: off)")
    # 添加 exclude 和 exclude-file 参数
    parser.add_argument("--exclude", nargs='+', required=False, help="Exclude files use keywords, ")
    parser.add_argument("--exclude-file", required=False, help="File containing exclude keywords, one per line")
    # 添加 retain 和 retain-file 参数
    parser.add_argument("--retain", nargs='+', required=False, help="Retain keywords, ")
    parser.add_argument("--retain-file", required=False, help="File containing retain keywords, one per line")
    # 添加 duplicates-result-file 参数
    parser.add_argument("--duplicates-result-file", required=False, help="File containing the duplicates result JSON data")
    # 添加 undo 参数（与扫描互斥的恢复模式）
    parser.add_argument("--undo", required=False, metavar="PATH",
                        help="Rollback mode: replay a moves_*.jsonl manifest (a file path, or a "
                             "directory containing them) and move each recorded file from its "
                             "destination back to its original source. Never overwrites a source "
                             "location that is already occupied, prunes emptied directory skeletons "
                             "under the recycle dir, and prints a restored/skipped/missing/total "
                             "count summary at the end. Mutually exclusive with -d/--directories. "
                             "Example: dup_finder.py --undo trash")
    # 添加导出待处理候选清单参数
    parser.add_argument("--export-candidates", required=False, metavar="PATH",
                        help="Before any action runs, write the list of files that will be "
                             "processed to PATH, one absolute source path per line, matching "
                             "exactly the selected processing queue (after cross-root filtering "
                             "and priority/retain selection). Works with delete, move or "
                             "--try-run; combine with --try-run to only review the list, since "
                             "try-run still writes no cache, result or manifest files. The "
                             "output file is a user-requested artifact; its parent directories "
                             "are created as needed, and the run stops before acting if the "
                             "list cannot be written.")

    args = parser.parse_args()
    # --undo 与扫描参数互斥：恢复模式不要求 -d，也不做扫描/缓存/结果文件任何动作
    if args.undo:
        if args.directories:
            parser.error("--undo rollback mode cannot be combined with -d/--directories")
    elif not args.directories:
        parser.error("the following arguments are required: --directories/-d (or use --undo PATH)")
    # 使用 subprocess.list2cmdline 重建命令行字符串
    command_line = subprocess.list2cmdline(sys.argv)
    logger.info("Full command line: %s", command_line)
    # 解析 exclude 和 exclude-file 参数
    exclude_keywords = args.exclude if args.exclude else []
    if args.exclude_file:
        exclude_keywords_from_file = parse_exclude_file(args.exclude_file)
        exclude_keywords.extend(exclude_keywords_from_file)
    # 解析 retain 和 retain-file 参数
    retain_keywords = args.retain if args.retain else []
    if args.retain_file:
        retain_keywords_from_file = parse_exclude_file(args.retain_file)  # 使用 parse_exclude_file 函数读取 retain-file
        retain_keywords.extend(retain_keywords_from_file)
    try:
        main(args.directories, args.action, args.priority_order, args.move_to_dir, args.try_run,
             exclude_keywords=exclude_keywords, retain_keywords=retain_keywords,
             file_dict_path=args.duplicates_result_file, min_size=args.min_size,
             cache_batch=args.cache_batch, cross_dirs_only=args.cross_dirs_only,
             undo=args.undo, export_candidates_path=args.export_candidates)
    except CacheInterrupted:
        # 扫描循环已在锁内完成一次受控落盘；此处只做非零码干净退出，不打印 traceback
        sys.exit(1)
    except KeyboardInterrupt:
        # 中断发生在扫描之外（如动作阶段）时同样干净退出
        logger.info("Interrupted; exiting")
        sys.exit(1)








