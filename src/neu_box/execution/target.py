"""任务执行目标的结构化描述。

两种目标:

  host    — 宿主机上直接跑命令。
  docker  — 起一个一次性容器跑命令。受管容器一律由 neu-box 起，因为容器
            必须在第一个 NPU 进程之前完成归属登记，而"别人已经起好的
            容器"没有这个时机，所以不存在"在已有容器里 exec"这种目标。
"""

from __future__ import annotations

import posixpath
import re
from typing import Any


TARGET_HOST = 'host'
TARGET_DOCKER = 'docker'
TARGET_TYPES = {TARGET_HOST, TARGET_DOCKER}

_IMAGE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}$')
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class TargetValidationError(ValueError):
    """结构化执行目标不合法。"""


def _reject_unknown(raw: dict, allowed: set[str]):
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise TargetValidationError(
            f'target 包含不支持的字段: {", ".join(unknown)}'
        )


def _normalize_workdir(value: Any) -> str:
    if value in (None, ''):
        return ''
    if not isinstance(value, str) or '\x00' in value:
        raise TargetValidationError('target.workdir 必须是合法字符串')
    if not value.startswith('/'):
        raise TargetValidationError('target.workdir 必须是容器内绝对路径')
    value = posixpath.normpath(value)
    if len(value) > 4096:
        raise TargetValidationError('target.workdir 过长')
    return value


def _normalize_user(value: Any) -> str:
    if value in (None, ''):
        return ''
    if not isinstance(value, str):
        raise TargetValidationError('target.user 必须是字符串')
    value = value.strip()
    if not value or len(value) > 128 or '\x00' in value:
        raise TargetValidationError('target.user 无效')
    return value


def _normalize_environment(value: Any) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise TargetValidationError('target.env 必须是对象')
    if len(value) > 128:
        raise TargetValidationError('target.env 最多包含 128 项')
    result = {}
    for key, raw_value in value.items():
        if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
            raise TargetValidationError(f'target.env 变量名无效: {key!r}')
        if not isinstance(raw_value, (str, int, float, bool)):
            raise TargetValidationError(
                f'target.env[{key!r}] 必须是字符串或标量'
            )
        normalized = str(raw_value)
        if '\x00' in normalized or len(normalized) > 32768:
            raise TargetValidationError(
                f'target.env[{key!r}] 的值无效或过长'
            )
        result[key] = normalized
    return result


def normalize_execution_target(raw: Any) -> dict:
    """校验并返回可写入 SQLite 的执行目标。"""
    if raw in (None, {}):
        return {'type': TARGET_HOST}
    if not isinstance(raw, dict):
        raise TargetValidationError('target 必须是对象')

    target_type = str(raw.get('type') or '').strip().lower()
    if target_type == '':
        target_type = TARGET_HOST
    if target_type not in TARGET_TYPES:
        raise TargetValidationError(
            'target.type 必须是 host 或 docker；在已有容器里执行已不再支持'
        )
    if target_type == TARGET_HOST:
        _reject_unknown(raw, {'type'})
        return {'type': TARGET_HOST}

    _reject_unknown(raw, {'type', 'image', 'workdir', 'env', 'user'})
    image = raw.get('image')
    if not isinstance(image, str):
        raise TargetValidationError('docker 目标必须提供 target.image')
    image = image.strip()
    if not _IMAGE_RE.fullmatch(image):
        raise TargetValidationError('target.image 名称或标签无效')
    return {
        'type': TARGET_DOCKER,
        'image': image,
        'workdir': _normalize_workdir(raw.get('workdir')),
        'env': _normalize_environment(raw.get('env')),
        'user': _normalize_user(raw.get('user')),
    }


def public_execution_target(target: dict | None) -> dict:
    target = target or {'type': TARGET_HOST}
    result = {'type': target.get('type', TARGET_HOST)}
    if result['type'] == TARGET_DOCKER:
        result['image'] = target.get('image', '')
    return result
