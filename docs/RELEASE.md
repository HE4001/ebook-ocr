# 本地发布准备记录

日期：2026-09-27。源码版本：0.1.0（来自 `frontend/package.json`）。本次为本地整合与发布准备，没有推送远端、创建线上 Release 或执行真实模型调用。

## 本次整合

仓库只有 `main` 和一个工作目录，无其他本地分支需要 merge。本次将既有未提交开发成果、补充测试和发布文档一起提交到 `main`，保留原有提交历史。

- 整合 PDF / 图片导入、项目编排、Responses / Gemini 页面识别、封面封底处理、人工校对、纸型及打印排版。
- 整合并发识别期间校对保护、模型响应校验、透明图片导入、公式显示和状态刷新等已有改动。
- 使用 `ocr.bat` 作为统一启动、停止、重启及状态入口。
- 补齐前端回归测试的 `editLocked` 状态，新增 `pnpm test` 入口。
- README 改为安装和使用入口；原详细说明保存在 [REFERENCE.md](REFERENCE.md)。

## 当前版本验证

| 检查 | 本次结果 |
| --- | --- |
| 后端完整 pytest 测试 | 43 passed，1 项依赖弃用警告 |
| 前端 `pnpm test` | 12 passed |
| PowerShell 启动器隔离测试 | 18 项场景通过 |
| `pnpm run build` | TypeScript 和 Vite 构建通过 |

验证环境：Windows、Python 3.12.14、Node.js 24.17.0、pnpm 11.19.0。后端使用现有项目依赖和可用 Python / pytest，在临时目录运行测试以隔离实际书库；具体命令如下：

```powershell
@'
import sys, pathlib, tempfile, os
sys.path.insert(0, str(pathlib.Path('.venv/Lib/site-packages').resolve()))
import pytest
with tempfile.TemporaryDirectory(prefix='ocr-release-tests-') as tmp:
    os.environ['EBOOK_OCR_DATA_DIR'] = tmp
    sys.exit(pytest.main(['backend/tests', '-q']))
'@ | python -
```

本机旧 `.venv` 的基础解释器路径失效，更新引用后已实际验证项目 Python 启动和后端依赖导入；完整 `venv --upgrade` 因正在运行的解释器文件被占用而未完成，因此不把它记录为全量重建成功。跨电脑仍应重建环境。

后端警告为 Starlette TestClient 对当前 httpx 用法的弃用提醒；前端构建保留现有单个 JS 包超过 500 kB 的提示。二者未导致本次验证失败。没有重新验收真实模型、全部浏览器交互和打印机输出，[人工验收清单](acceptance.md) 中未执行项目保持原状态。

## 生成公开源码包

在项目根目录完成提交后执行；每次使用新的提交编号命名，避免覆盖旧发布包：

```powershell
$revision = git rev-parse --short HEAD
$outputDir = '.cache/releases'
New-Item -ItemType Directory -Force $outputDir | Out-Null
$archive = Join-Path $outputDir "ocr-source-$revision.zip"
if (Test-Path -LiteralPath $archive) { throw '发布包已存在，请先检查已有产物。' }
git archive --format=zip --prefix=ocr/ -o $archive HEAD
if ($LASTEXITCODE -ne 0) { throw '源码打包失败。' }
Get-FileHash -Algorithm SHA256 -LiteralPath $archive
```

`git archive` 只包括提交中的文件；`.gitattributes` 额外排除根目录个人开发需求笔记。公开源码包不含 `.git`、`.venv`、`node_modules`、运行数据、API 密钥数据库、日志或缓存。后端依赖使用版本范围，前端依赖使用已提交的 pnpm 锁文件；这不是完全固定所有环境的二进制发行版。

完整 U 盘副本包含 Git 历史、工作文件、运行数据和本机依赖，仅供个人保存或迁移。两者用途不同。迁移步骤见 [README](../README.md#数据保存与迁移)。

## 对外发布前的人工事项

- 如要以开源许可分发，由作者确定并添加 LICENSE；本次不代选许可证。
- 使用目标 API 和真实样本核对识别效果，按需要完成尚未执行的人工验收。
- 对外分享源码 ZIP；个人完整备份中的书籍和凭据不进入公开发布。
