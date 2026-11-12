"""超时重试有界、可取消；不重试鉴权、输出长度或校验失败。"""
import re

RETRY_DELAYS = (2, 5)


class ModelTimeout(RuntimeError):
    pass


def is_timeout(value):
    return bool(re.search(r"timed?\s*out|timeout|超时", str(value), re.IGNORECASE))
