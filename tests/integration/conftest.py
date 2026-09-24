"""Registration for integration-test markers without changing project config."""


def pytest_configure(config):
    config.addinivalue_line(
        'markers', 'integration: tests spanning multiple Worker/client layers',
    )
    config.addinivalue_line(
        'markers', 'docker: requires a reachable Docker daemon',
    )
    config.addinivalue_line(
        'markers', 'privileged: requires root, BPF, namespaces, or NPU',
    )
