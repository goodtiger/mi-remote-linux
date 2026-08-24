# Contributing

感谢参与 MiRemote Linux。

## 开发环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pytest -q
```

涉及 ATVV 或 ADPCM 的修改，请说明对应的协议依据，并同时添加自动化测试。涉及真机行为
的修改，请在 PR 中注明遥控器型号、固件表现、Linux 发行版、BlueZ 版本及桌面会话类型。

请勿提交蓝牙 MAC、访问令牌、录音、模型文件、崩溃转储或其他个人数据。
