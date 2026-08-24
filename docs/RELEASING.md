# Release process

GitHub Release 是当前唯一正式发行渠道；暂不上传 PyPI。只有稳定 SemVer 标签
`vMAJOR.MINOR.PATCH` 会触发发行工作流。

## 准备版本

1. 更新 `src/mi_remote_linux/__init__.py` 中的唯一版本号。
2. 把 `CHANGELOG.md` 的 Unreleased 内容移入对应版本，并填写日期。
3. 运行完整发行前检查：

   ```bash
   .venv/bin/python -m pip install -e ".[dev,release]"
   .venv/bin/python scripts/check_release.py v0.4.1
   .venv/bin/ruff format --check .
   .venv/bin/ruff check .
   .venv/bin/pytest -q
   .venv/bin/python -m build
   .venv/bin/twine check dist/*
   ```

4. 提交并推送，等待普通 CI 的全部 Python 版本通过。

## 发布

确认发布后创建并推送不可变的 annotated tag：

```bash
git tag -a v0.4.1 -m "MiRemote Linux v0.4.1"
git push origin v0.4.1
```

`.github/workflows/release.yml` 会再次校验 tag 与包版本、执行 lint 和全部测试、构建并
检查 wheel/sdist、隔离安装 wheel、生成 `SHA256SUMS`，最后通过 GitHub 官方 CLI 创建
Release。Release 包含 wheel、sdist、安装器和校验清单。

不要重用、移动或强制覆盖已发布标签。发现问题时保留原版本，并发布递增的补丁版本。
