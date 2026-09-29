# macmini 部署与测试

代码仓库：https://github.com/SleepinWei/Jev-LongSeq

- SSH：`macmini`（使用本机已有 SSH 配置）。
- 项目：`/Users/octopusz/CodeProjects/Jev-LongSeq`。
- Studio：macmini 的 `127.0.0.1:8768`，仅监听回环地址。
- macmini 的 8767 已由另一个项目使用，不要停止或覆盖。
- 原始 Ultrafast：项目下 `external/jev-ultrafast`，部署时同步本地实际使用的
  源码版本，包含该外部项目原有修改；不修改它的上游仓库。
- 模型配置：项目下 `.env`，权限 600，不进入 Git。
- 运行数据：`runs/`，不进入 Git。迁移时保留历史记录中的原始来源路径。

## 测试

此后的测试、浏览器任务及实验统一在 macmini 执行：

```bash
./scripts/test_macmini.sh
./scripts/test_macmini.sh tests/test_ultrafast.py tests/test_research_ui.py -q
```

脚本原样传递 pytest 参数，并返回远端退出码。测试使用 macmini 上隔离的 Playwright
浏览器；真实网页任务使用 macmini 自身的 Chrome 会话，不迁移其他机器的浏览器登录信息。
Codex 分析使用 macmini 的 Codex CLI 及已有登录，模型密钥由服务端 `.env` 加载。

## 更新代码及服务

先检查远端工作区；有未提交修改时先处理，不能直接覆盖。确认没有正在运行的任务后：

```bash
ssh macmini
export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH
cd ~/CodeProjects/Jev-LongSeq
git status --short
git pull --ff-only
uv sync --frozen --python /opt/homebrew/bin/python3.12 --extra dev --extra chrome
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
.venv/bin/python scripts/install_studio_service.py \
  --env-file .env --ultrafast-root external/jev-ultrafast
```

用户级 LaunchAgent `com.jev.longseq.studio` 在登录后自动启动，进程退出后重启。
安装脚本会拒绝中断正在执行的 Studio 任务。

```bash
launchctl print gui/$(id -u)/com.jev.longseq.studio
tail -n 50 runs/service/studio.err.log
curl http://127.0.0.1:8768/api/launcher
```

## 本机访问

本机 8767 空闲时，在本机建立转发，继续使用原来的 Studio 地址：

```bash
ssh -N -L 127.0.0.1:8767:127.0.0.1:8768 macmini
```

访问 <http://127.0.0.1:8767>。关闭 SSH 转发只会中断访问，不会停止远端任务。
如果本机 8767 仍有服务，可把命令中第一个端口改为 18768，并访问对应端口。
