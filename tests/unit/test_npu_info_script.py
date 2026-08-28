from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NPU_INFO = ROOT / "src/neu_box/worker/resources/info/npu_info.sh"


def _npu_smi_output(visible_ids: list[int], busy_ids: list[int]) -> str:
    lines = [
        "+===========================+=======================+",
        "| NPU   Name                | Health                |",
        "| Chip                      | Bus-Id                |",
        "+===========================+=======================+",
    ]
    for npu_id in visible_ids:
        lines.extend([
            f"| {npu_id}     Ascend910B        | OK                    |",
            "| 0                         | 0000:00:00.0          |",
            "+---------------------------+-----------------------+",
        ])
    lines.extend([
        "| NPU     Chip        Process id      Process name       |",
        "+=========================================================+",
    ])
    if busy_ids:
        for offset, npu_id in enumerate(busy_ids):
            lines.append(
                f"| {npu_id}       0           {1000 + offset}            python             |"
            )
    else:
        lines.append("| No running processes found in NPU 0                  |")
    lines.append("+=========================================================+")
    return "\n".join(lines) + "\n"


def _run_npu_info(
    tmp_path: Path,
    *,
    device_ids: list[int],
    visible_ids: list[int],
    busy_ids: list[int],
) -> dict[str, object]:
    device_root = tmp_path / "dev"
    device_root.mkdir()
    for npu_id in device_ids:
        (device_root / f"davinci{npu_id}").touch()

    fixture = tmp_path / "npu-smi-output"
    fixture.write_text(
        _npu_smi_output(visible_ids, busy_ids), encoding="utf-8"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_npu_smi = fake_bin / "npu-smi"
    fake_npu_smi.write_text(
        "#!/usr/bin/env bash\ncat \"$NPU_SMI_FIXTURE\"\n",
        encoding="utf-8",
    )
    fake_npu_smi.chmod(0o755)

    environment = os.environ.copy()
    environment.update({
        "NPU_DEVICE_ROOT": str(device_root),
        "NPU_SMI_FIXTURE": str(fixture),
        "PATH": f"{fake_bin}:{environment['PATH']}",
    })
    result = subprocess.run(
        [str(NPU_INFO)],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_hidden_allocated_cards_do_not_renumber_visible_busy_cards(tmp_path):
    result = _run_npu_info(
        tmp_path,
        device_ids=list(range(8)),
        visible_ids=[4, 5, 6, 7],
        busy_ids=[6, 7],
    )

    assert result == {
        "total": 8,
        "idle": 2,
        "busy_ids": [0, 1, 2, 3, 6, 7],
    }


def test_all_visible_cards_only_report_process_owners_as_busy(tmp_path):
    result = _run_npu_info(
        tmp_path,
        device_ids=list(range(8)),
        visible_ids=list(range(8)),
        busy_ids=[6, 7],
    )

    assert result == {"total": 8, "idle": 6, "busy_ids": [6, 7]}


def test_non_contiguous_npu_smi_ids_are_not_compressed_without_device_nodes(
    tmp_path,
):
    result = _run_npu_info(
        tmp_path,
        device_ids=[],
        visible_ids=[4, 5, 6, 7],
        busy_ids=[6, 7],
    )

    assert result == {"total": 4, "idle": 2, "busy_ids": [6, 7]}
