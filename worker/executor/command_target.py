"""Host 与现有 Docker 容器命令的结构化执行目标。"""

from __future__ import annotations

import posixpath
import re
from typing import Any


TARGET_HOST = 'host'
TARGET_DOCKER_EXISTING = 'docker_existing'
TARGET_TYPES = {TARGET_HOST, TARGET_DOCKER_EXISTING}

_CONTAINER_SELECTOR_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
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
    if target_type not in TARGET_TYPES:
        raise TargetValidationError(
            'target.type 必须是 host 或 docker_existing'
        )
    if target_type == TARGET_HOST:
        _reject_unknown(raw, {'type'})
        return {'type': TARGET_HOST}

    _reject_unknown(raw, {'type', 'container', 'workdir', 'env', 'user'})
    selector = raw.get('container')
    if not isinstance(selector, str):
        raise TargetValidationError(
            'docker_existing 必须提供 target.container'
        )
    selector = selector.strip()
    if not _CONTAINER_SELECTOR_RE.fullmatch(selector):
        raise TargetValidationError('target.container 名称或 ID 无效')
    return {
        'type': TARGET_DOCKER_EXISTING,
        'container': selector,
        'workdir': _normalize_workdir(raw.get('workdir')),
        'env': _normalize_environment(raw.get('env')),
        'user': _normalize_user(raw.get('user')),
    }


def public_execution_target(target: dict | None) -> dict:
    target = target or {'type': TARGET_HOST}
    result = {'type': target.get('type', TARGET_HOST)}
    if result['type'] == TARGET_DOCKER_EXISTING:
        result['container'] = target.get('container', '')
    return result


def public_runtime_metadata(metadata: dict | None) -> dict:
    metadata = metadata or {}
    return {
        key: metadata[key]
        for key in ('target_type', 'container_name', 'phase')
        if metadata.get(key) not in (None, '')
    }
