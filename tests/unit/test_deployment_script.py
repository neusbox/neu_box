"""已部署 Worker 验收脚本的离线契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / 'tests' / 'test_deployment.sh'


def test_deployment_script_is_valid_bash():
    result = subprocess.run(
        ['bash', '-n', str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_deployment_script_reads_nested_task_returncode():
    """GET /tasks/<id> 将退出码放在 result.returncode。"""
    source = SCRIPT.read_text(encoding='utf-8')
    paths = re.findall(
        r'json_value "\$TASK_RESULT" ([A-Za-z0-9_.]+)',
        source,
    )
    assert paths == ['result.returncode', 'result.returncode']

    response = json.loads(
        '{"status":"completed","result":{"returncode":0}}'
    )
    for path in paths:
        value = response
        for part in path.split('.'):
            value = value[part]
        assert value == 0


def test_deployment_script_requires_the_reviewed_worker_version():
    source = SCRIPT.read_text(encoding='utf-8')

    assert 'NEU_BOX_EXPECTED_WORKER_VERSION' in source
    assert '--expected-version' in source
    assert 'WORKER_VERSION" == "$EXPECTED_WORKER_VERSION' in source
    assert source.index(
        'WORKER_VERSION" == "$EXPECTED_WORKER_VERSION'
    ) < source.index("test_title '资源状态与设备基线'")
