from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GPU_INFO = ROOT / "src/neu_box/worker/resources/info/gpu_info.sh"


def _run_gpu_info(
    tmp_path: Path,
    *,
    visible_minors: list[int],
    busy_minors: set[int],
) -> dict[str, object]:
    proc_root = tmp_path / "proc" / "driver" / "nvidia" / "gpus"
    pci_by_minor: dict[int, str] = {}
    for minor in range(8):
        pci = f"0000:{minor + 1:02x}:00.0"
        pci_by_minor[minor] = pci
        gpu_dir = proc_root / pci
        gpu_dir.mkdir(parents=True)
        (gpu_dir / "information").write_text(
            f"Model: Test GPU\nDevice Minor: {minor}\nBus Location: {pci}\n",
            encoding="utf-8",
        )

    # nvidia-smi 的可见顺序可能被重排；输出只保留稳定的 PCI Bus ID。
    rows = []
    for minor in visible_minors:
        used = 1024 if minor in busy_minors else 0
        utilization = 80 if minor in busy_minors else 0
        rows.append(
            f"00000000:{pci_by_minor[minor][5:]}, 65536, {used}, {utilization}"
        )
    fixture = tmp_path / "nvidia-smi-output"
    fixture.write_text("\n".join(rows) + "\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_nvidia_smi = fake_bin / "nvidia-smi"
    fake_nvidia_smi.write_text(
        "#!/usr/bin/env bash\ncat \"$NVIDIA_SMI_FIXTURE\"\n",
        encoding="utf-8",
    )
    fake_nvidia_smi.chmod(0o755)

    environment = os.environ.copy()
    environment.update({
        "NVIDIA_PROC_ROOT": str(proc_root),
        "NVIDIA_SMI_FIXTURE": str(fixture),
        "PATH": f"{fake_bin}:{environment['PATH']}",
    })
    result = subprocess.run(
        [str(GPU_INFO)],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_hidden_leading_gpus_keep_physical_minor_ids(tmp_path):
    result = _run_gpu_info(
        tmp_path,
        # 前四张不可见，后四张的返回顺序也与物理 minor 相反。
        visible_minors=[7, 6, 5, 4],
        busy_minors={6, 7},
    )

    assert result == {
        "total": 8,
        "idle": 2,
        "busy_ids": [0, 1, 2, 3, 6, 7],
    }


def test_all_visible_gpus_map_busy_state_by_pci_not_output_position(tmp_path):
    result = _run_gpu_info(
        tmp_path,
        visible_minors=[3, 1, 7, 0, 6, 2, 5, 4],
        busy_minors={1, 6},
    )

    assert result == {"total": 8, "idle": 6, "busy_ids": [1, 6]}
