import os
import logging
from logging.handlers import TimedRotatingFileHandler


# 文件日志级别收紧为 INFO：例行扫描不再向文件写每文件级 DEBUG 记录，
# 里程碑、警告与错误仍完整落盘；logger 本体保持 DEBUG，便于将来按需
# 调低 handler 级别做详细诊断。
FILE_LOG_LEVEL = logging.INFO
# 轮转保留由 7 天收紧为 3 天：单轮日志体积已大幅缩减，保留价值高于体积风险。
FILE_BACKUP_COUNT = 3


def configure_logger(log_file_name: str = __name__) -> logging.Logger:
    # 按传入名称创建 logger（修复旧版误用 __name__ 硬编码、参数只影响文件名的缺陷）
    logger = logging.getLogger(log_file_name)

    # 检查是否已经配置过日志记录器
    if not logger.hasHandlers():
        # 设置日志级别
        logger.setLevel(logging.DEBUG)

        # 创建一个控制台处理器并设置级别为 INFO
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        # 创建一个文件处理器并设置级别为 FILE_LOG_LEVEL
        logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
        os.makedirs(logs_dir, exist_ok=True)  # 确保logs目录存在
        log_file_path = os.path.join(logs_dir, f'{log_file_name}.log')

        # 创建 TimedRotatingFileHandler 实例
        handler = TimedRotatingFileHandler(log_file_path, when='midnight', interval=1,
                                           backupCount=FILE_BACKUP_COUNT)
        handler.setLevel(FILE_LOG_LEVEL)

        # 创建一个格式化器并将其添加到处理器
        log_format_attributes = [
            '%(asctime)s',
            '%(levelname)s',
            '%(filename)s',
            'line:%(lineno)d',
            '%(funcName)s',
            '%(message)s',
        ]
        log_format_string = " - ".join(log_format_attributes)
        formatter = logging.Formatter(fmt=log_format_string)  # 日志格式

        console_handler.setFormatter(formatter)
        handler.setFormatter(formatter)

        # 将处理器添加到logger对象
        logger.addHandler(console_handler)
        logger.addHandler(handler)

    return logger
