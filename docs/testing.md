# Neu Box Worker 测试

构建完成、发布前必须执行测试。以下命令假设已执行 `uv sync --frozen --all-groups`。

## 单元测试

```bash
uv run --frozen pytest -q tests/unit
```

## 集成测试

```bash
uv run --frozen pytest -q tests/integration
```

未提供 hook 二进制时，跨仓库用例会 skip；发布前建议显式指定：

```bash
NEU_BOX_HOOK_BIN=/path/to/neu-box-hook uv run --frozen pytest -q tests/integration
```

## native sandbox 单测

```bash
make -C native/sandbox BUILD_DIR="$PWD/build/native-sandbox" test
```

完整构建会执行 native `make all test`、PyInstaller 打包和验收套件 `--self-check`：

```bash
uv run --frozen --group build deploy/build_release.py
```

## 部署后验收

部署后验收套件在维护窗口以 root 执行，详见 [部署与升级手册](deployment.md)：

```bash
sudo neuboxctl test
```
