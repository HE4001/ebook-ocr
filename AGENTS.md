# Repository Guidelines

## Project Structure & Module Organization

This Windows-first, local ebook OCR application uses Python/FastAPI and React/TypeScript.

- `backend/`: API (`main.py`), OCR pipeline, model clients, SQLite storage, content/layout contracts, and LaTeX/PDF exports. Tests live in `backend/tests/`.
- `frontend/src/`: React components, API helpers, types, and CSS; regression tests live in `frontend/tests/`.
- `scripts/`: PowerShell service management, dependency preparation, and launcher tests; `ocr.bat` is the user entry point.
- `docs/`: workflow specifications, implementation notes, usage, and release instructions. `examples/` contains a historical JSON export.
- `backend/data/`: ignored runtime books, credentials, and databases; `.cache/launcher/` holds launcher logs.

## Build, Test, and Development Commands

Run from the repository root unless specified. Use Python 3.11+ and Node 22.18+ for frontend tests.

- `python -m venv .venv`: create the Python environment.
- `.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt`: install backend and pytest dependencies.
- `.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000`: start the API.
- In `frontend/`, run `pnpm install --frozen-lockfile`, then `pnpm run dev` for Vite on port 5173.
- In `frontend/`, `pnpm run build` runs TypeScript checks and creates the production bundle; `pnpm test` runs regression tests.
- `.\ocr.bat Start -NoBrowser`: prepare dependencies and start both services; `Status`, `Stop`, and `Restart` manage them. PDF rendering requires XeLaTeX.

## Coding Style & Naming Conventions

Match surrounding code: Python uses four-space indentation, type annotations, and `snake_case`; TypeScript uses two spaces, single quotes, and generally omits semicolons. Use PascalCase component filenames (`PageEditor.tsx`) and camelCase helpers (`pageSelection.ts`). Keep backend contracts aligned with `frontend/src/types.ts`. No dedicated formatter or lint command is configured.

## Testing Guidelines

The commands below are contributor references; the agent restrictions in the final section take precedence during delegated development.

Backend tests use pytest and unittest: run `.\.venv\Scripts\python.exe -m pytest backend/tests -q`. Use `test_*.py` and frontend `*.test.mjs` naming. Frontend tests use Node's built-in runner. Launcher checks include `.\scripts\test-launcher.ps1`.

Use temporary `EBOOK_OCR_DATA_DIR` storage and mocked model responses. No coverage threshold is configured. Check `docs/OCR_V2_IMPLEMENTATION.md` for task-specific static-review restrictions. Latest OCR changes remain unverified; historical passes do not establish current correctness.

## Commit & Pull Request Guidelines

History mixes imperative summaries with `feat:`, `fix:`, and `chore:` prefixes. Keep commits focused. PRs should explain behavior changes, relevant issues, validation performed or omitted, and migration implications; include screenshots for UI changes.

## Security & Configuration Tips

Keep services on loopback. Never commit API keys, book sources, databases, or full backups: credentials are stored in plaintext. Use `EBOOK_OCR_DATA_DIR` for alternate storage and `EBOOK_OCR_XELATEX` for a compiler override. Preserve source files, manual edits, and migration backups.

## 代理分工与代码审查

- 主代理须按模块拆分开发任务，明确各模块的目标、接口、文件归属及验收条件，交由 GPT-6.1 Sol（`gpt-6.1-sol`）子代理实现；互不依赖的模块可同时并行，共享文件指定唯一修改者。
- 每次派发任务必须明确告知子代理：理解需求并思考实现后直接编码；禁止任何测试和运行验证，包括测试、构建、类型检查、lint、试编译、应用启动、接口探测、模型调用及迁移试跑。
- 所有编码子代理禁止使用 computer use，不得操作浏览器或桌面界面；通过源码和文件工具完成实现。
- 禁止过度设计和防御性编程。围绕当前需求采用直接、简洁、可维护的实现，不添加假设性兼容层、兜底逻辑或无必要的抽象。
- 主代理只负责需求、分工、协调和静态代码审查，不参与基础编码。必须阅读实际实现和接口衔接后决定是否接受；不接受时提出具体问题、修改要求及理由，交由原子代理返工，再次审查。
- 子代理交付时须说明修改文件、关键行为和剩余问题，并明确标注“未测试、未运行验证”；不得将静态审查接受表述为测试通过或功能验收通过。
