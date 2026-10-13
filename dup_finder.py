import os
import sys
import json
import stat
import shutil
import hashlib
import tempfile
import argparse
import subprocess
import datetime
from filelock import FileLock
from logger import configure_logger


logger = configure_logger("DupFinder")


def get_file_hash(file_path, hash_algo=hashlib.sha256):
    """Calculate the hash of a file."""
    hash_obj = hash_algo()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_obj.update(chunk)
    return hash_obj.hexdigest()

def generate_file_identifier(file_path):
    """Generate a unique identifier for a file."""
    file_hash = get_file_hash(file_path, hashlib.sha256)
    return file_hash

def write_cache_to_file(cache, cache_file):
    """Write the cache to the cache file atomically."""
    with tempfile.NamedTemporaryFile('w', delete=False) as temp_file:
        json.dump(cache, temp_file)
        temp_file_path = temp_file.name
    shutil.move(temp_file_path, cache_file)

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
    snapshot, the cached file_id is returned as-is (no re-hash); otherwise the
    hash is recomputed and the cache entry updated from the same snapshot.
    Raises OSError (incl. PermissionError) for unreadable/vanished files, which
    the caller handles per file.
    """
    if st is None:
        st = os.stat(file_path)

    file_info = cache.get(file_path)
    if file_info:
        cached_modified_time = file_info.get('modified_time')
        cached_size = file_info.get('size')

        if cached_modified_time == st.st_mtime and cached_size == st.st_size:
            return file_info['file_id']

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

def find_duplicates(directories, cache_file='file_cache.json', batch_size=10, exclude_keywords=None, min_size=1):
    """Find duplicate files in the given directories.

    Scan safety baseline:
    - entries that are symlinks, or smaller than `min_size` bytes (0-byte files
      are excluded with the default min_size=1), never become duplicate
      candidates; they are counted and reported in a final summary line;
    - a per-file OSError (e.g. unreadable or vanished mid-scan) yields exactly
      one warning and the scan continues;
    - each path is os.stat'ed once and the snapshot feeds cache comparison and
      the result dict alike, avoiding multi-stat race windows.
    Excluded files get no cache entries, so the existing cache file format and
    all reusable entries stay untouched.
    """
    lock_file = f"{cache_file}.lock"
    lock = FileLock(lock_file)

    n_empty = 0
    n_small = 0
    n_symlink = 0
    n_error = 0

    with lock:
        cache = load_cache(cache_file)
        file_dict = {}
        cache_updates = []

        directories = list(set(directories))
        for directory in directories:
            logger.info("Processing directory: %s", directory)
            for root, _, files in os.walk(directory):
                for file in files:
                    file_path = os.path.join(root, file)
                    logger.debug("Processing file: %s", file_path)
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
                        # 单一路径一次 os.stat，size/mtime 快照贯穿缓存比对与结果字典
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
                        file_id = get_file_id(file_path, cache, st)
                    except (OSError, PermissionError) as e:
                        # 单个坏文件（不可读/中途消失等）只警告一次并继续整轮扫描
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
                        write_cache_to_file(cache, cache_file)
                        cache_updates.clear()
                    
        # Final write for any remaining updates
        if cache_updates:
            write_cache_to_file(cache, cache_file)

        # Filter out file_ids with only one element
        file_dict = {file_id: file_info_list for file_id, file_info_list in file_dict.items() if len(file_info_list) > 1}

    logger.info(
        "Scan summary: skipped small/empty=%d (empty=%d), symlinks=%d, errors=%d",
        n_small, n_empty, n_symlink, n_error
    )

    return file_dict

def assign_priorities(file_dict, retain_keywords, priority_order=None):
    """Assign priorities to files based on the given criteria."""
    if priority_order is None:
        # Default priority order
        priority_order = ['modified_time', 'path']

    for file_id, files in file_dict.items():
        # 检查同一 file_id 下的文件大小是否一致
        file_sizes = {file['size'] for file in files}
        if len(file_sizes) > 1:
            # 如果文件大小不一致，将所有文件的优先级设置为 0
            for file_info in files:
                file_info['priority'] = 0
            # 打印文件大小不一致的 file_id 及文件列表
            logger.error("File ID with inconsistent sizes: %s", file_id)
            for file_info in files:
                logger.warning(f"  Path: {file_info['path']}, Size: {file_info['size']}")
            continue

        priority_counter = 1  # Start from 1 for non-retained files
        files.sort(
            key=lambda x: tuple(
                x[order] if order == 'modified_time'  # 修改时间字段升序排列
                else (-x[order] if order != 'path'  # 其他数值字段降序排列
                      else -x[order].count(os.sep))  # 路径字段按目录深度降序
                for order in priority_order
            )
        )
        # Assign priorities to all files
        for file_info in files:
            # 检查文件路径是否包含 retain_keywords
            if retain_keywords and any(keyword in file_info['path'] for keyword in retain_keywords):
                file_info['priority'] = 0
            else:
                file_info['priority'] = priority_counter
                priority_counter += 1


def retain_files(file_dict, action, move_to_dir=None, try_run=False):
    """Retain files based on the priority and process the rest."""
    for file_id, files in file_dict.items():
        # Sort files by priority (lowest priority number first)
        files.sort(key=lambda x: x['priority'])
        # Find the highest priority (lowest priority number)
        highest_priority = files[0]['priority']
        
        # Process files with priority higher than the highest retained priority
        for file in files:
            if file['priority'] > highest_priority:
                process_file(file, action, move_to_dir, try_run, file_id)


# def check_filename_length(filename):
#     """检查文件名字节长度是否超过限制（255字节）"""
#     byte_length = len(filename.encode('utf-8'))
#     return byte_length <= 255


def process_file(file, action, move_to_dir=None, try_run=False, file_id=None):
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
def main(directories, action, priority_order=None, move_to_dir=None, try_run=False, exclude_keywords=None, retain_keywords=None, file_dict_path=None, min_size=1):
    if file_dict_path:
        # 从指定文件中加载 file_dict
        with open(file_dict_path, 'r') as f:
            file_dict = json.load(f)
        logger.info(f"Loaded file_dict from {file_dict_path}")
    else:
        # 找到重复文件
        file_dict = find_duplicates(directories, exclude_keywords=exclude_keywords, min_size=min_size)
        assign_priorities(file_dict, retain_keywords, priority_order=priority_order)
        # 保存 file_dict 到文件
        current_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"duplicates_{current_time}.json"
        with open(output_file, 'w') as f:
            json.dump(file_dict, f, indent=4)
        logger.info(f"Saved file_dict to {output_file}")

    retain_files(file_dict, action, move_to_dir, try_run)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find and process duplicate files.")
    parser.add_argument("--directories", "-d", nargs='+', required=True, help="Directories to search for duplicate files")
    parser.add_argument("--action", choices=['delete', 'move'], required=False, default='move', help="Action to process files (default: move)")
    parser.add_argument("--priority-order", "-p", nargs='+', required=False, help="Custom priority order: default is modified_time, path")
    parser.add_argument("--move-to-dir", "-m", required=False, help="Directory to move files to (if not specified, rename files with .dup_finder suffix)")
    parser.add_argument("--try-run", "-n", action='store_true', required=False, help="Try run mode: only print actions without executing them")
    parser.add_argument("--min-size", type=int, default=1, required=False,
                        help="Minimum file size in bytes to participate in duplicate detection; "
                             "files smaller than this (including 0-byte files, by default) are "
                             "skipped during scanning and counted in the scan summary (default: 1)")
    # 添加 exclude 和 exclude-file 参数
    parser.add_argument("--exclude", nargs='+', required=False, help="Exclude files use keywords, ")
    parser.add_argument("--exclude-file", required=False, help="File containing exclude keywords, one per line")
    # 添加 retain 和 retain-file 参数
    parser.add_argument("--retain", nargs='+', required=False, help="Retain keywords, ")
    parser.add_argument("--retain-file", required=False, help="File containing retain keywords, one per line")
    # 添加 duplicates-result-file 参数
    parser.add_argument("--duplicates-result-file", required=False, help="File containing the duplicates result JSON data")

    args = parser.parse_args()
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
    main(args.directories, args.action, args.priority_order, args.move_to_dir, args.try_run, exclude_keywords=exclude_keywords, retain_keywords=retain_keywords, file_dict_path=args.duplicates_result_file, min_size=args.min_size)








