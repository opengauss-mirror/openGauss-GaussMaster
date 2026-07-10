# pre-commit 提交前检查（openGauss-GaussMaster）

本仓库在 `git commit` 前自动对**本次改动**做基础检查与格式化，减少低级错误进入版本库。

## 检查项

| 类别 | 工具 | 说明 |
|------|------|------|
| 基础 | pre-commit-hooks | 去尾随空格、文件末尾换行、YAML/JSON/TOML 语法、大文件、私钥检测 |
| 拼写 | codespell | 常见英文拼写错误 |
| Lint | flake8 | 读仓库根 `flake8.conf`（行宽 120，忽略 F401/W291/W504） |
| 安全 | bandit | 读 `.bandit`；`tests/`、`GaussMaster/test/`、`docs/` 为存量豁免 |
| 格式 | darker + black | 仅格式化**本次 diff 涉及行**，行宽 120 |
| 类型 | mypy | 读 `mypy.ini`；`tests/`、`GaussMaster/test/` 为存量豁免 |

## 一键安装（推荐）

在仓库根目录执行：

```bash
bash setup-pre-commit.sh
```

脚本会：

1. 在 `.pre-commit-venv/` 创建**隔离**虚拟环境（不污染系统 Python）
2. 安装 `pre-commit`、`mypy`、`flake8`（按需）
3. 执行 `pre-commit install` 与 `pre-commit install-hooks`

可选：指定 pip 镜像

```bash
PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple bash setup-pre-commit.sh
```

## 手动安装

```bash
pip install pre-commit mypy flake8
pre-commit install
pre-commit install-hooks
```

## 日常使用

- **提交时**：`git commit` 会自动跑钩子；失败则 commit 中止，按提示修复后重试。
- **手动试跑**（不提交）：

```bash
.pre-commit-venv/bin/pre-commit run --files GaussMaster/startup.py
# 或检查全部已暂存文件
.pre-commit-venv/bin/pre-commit run
```

- **跳过单次检查**（不推荐，仅紧急场景）：

```bash
git commit --no-verify -m "message"
```

## 卸载

```bash
.pre-commit-venv/bin/pre-commit uninstall
rm -rf .pre-commit-venv
```

## 常见问题

### 1. `flake8` / `mypy` 报 command not found

使用 `setup-pre-commit.sh` 安装时，flake8/mypy 装在隔离 venv 内；`pre-commit` 会通过 `language: system` 调用 PATH 中的命令。请确保：

- 已运行 `setup-pre-commit.sh`，或
- 本机已 `pip install flake8 mypy`，且 commit 时 PATH 可找到它们

若仅使用 venv 内工具，可临时：

```bash
export PATH="$(pwd)/.pre-commit-venv/bin:$PATH"
```

### 2. 首次运行较慢

`install-hooks` 会从 gitcode 镜像拉取钩子仓库并构建 darker 等环境，属正常现象。

### 3. darker 改了很多行

darker 只改**当前 diff 触及的行**；若与 black 规则不一致，首次提交可能改动较多，属预期行为。

### 4. Windows

建议在 Git Bash 或 WSL 中运行 `setup-pre-commit.sh`。也可在 PowerShell 中手动创建 venv 并 `pre-commit install`。

## 配置文件一览

| 文件 | 作用 |
|------|------|
| `.pre-commit-config.yaml` | 钩子列表与版本 |
| `flake8.conf` | flake8 规则 |
| `.bandit` | bandit 排除目录 |
| `mypy.ini` | mypy 选项与排除目录 |
| `setup-pre-commit.sh` | 隔离环境一键安装 |

## 维护说明

- 升级钩子版本：改 `.pre-commit-config.yaml` 中各 `rev`，再执行 `pre-commit autoupdate`（可选）。
- 调整 lint 严格度：改 `flake8.conf` / `mypy.ini`，避免一次性对全仓开启过严规则。
- `.pre-commit-venv/` 已加入 `.gitignore`，勿提交到远程。
