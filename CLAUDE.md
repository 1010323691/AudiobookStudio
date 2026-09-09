# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

AudiobookStudio 是一个 Web 应用，在一个地方完成中文有声书的制作流程：
**文本排版 → 分册切割 → 文本解析 → 角色配音 → 音频合成 → 音频合并 → 音频分集**，
外加开始（工作空间管理）和设置。它是一个两层结构的应用（纯 Web 前后端）：

- **`backend/`** — Python **FastAPI** 服务（端口 `127.0.0.1:8642`）。**所有真正的逻辑都在这里。**
- **`src/`** — **Vue 3** 前端（TypeScript、Vite、Tailwind、Pinia、vue-router）。它是一个*瘦客户端*：只负责渲染 UI 并通过 HTTP 调用后端，本身不含任何处理逻辑。

前端是纯 Web 瘦客户端，开发和运行本应用**不需要任何原生外壳或额外工具链**（见「命令」）。

## 命令

### 后端（Python）

虚拟环境是 `.venv/`（本机为 Python 3.14）。`requirements.txt` 刻意保持精简（fastapi、uvicorn、pydantic、python-multipart）以兼容 CPython 3.14 —— 它**没有**锁定 pytest，但 pytest 已经装进 `.venv` 里了。

```
# 启动 API（Windows）
.venv\Scripts\python -m backend.main        # → http://127.0.0.1:8642
# 启动 API（macOS / Linux）
python -m backend.main

# 运行整个测试套件（164 个测试）—— 在项目根目录运行
.venv/Scripts/python -m pytest backend/tests/ -v
# 单个文件
.venv/Scripts/python -m pytest backend/tests/test_book.py -v
# 单个测试
.venv/Scripts/python -m pytest backend/tests/test_book.py::test_range_char_count_excludes_newlines
```

`backend/tests/conftest.py` 会把项目根目录加进 `sys.path`，因此测试以 `backend.engines.*` 的方式导入包，且可以从任意目录启动。

### 前端（npm，项目根目录）

```
npm run dev         # Vite 开发服务器，:5173（strictPort）
npm run build       # vue-tsc --noEmit（类型检查）+ vite build → dist/
npm run typecheck   # 仅 vue-tsc --noEmit
npm run preview     # 预览构建产物
```

前端没有配置 ESLint/linter；`npm run typecheck`（vue-tsc，`strict: true`）就是类型门槛。Python 侧同样没有配置 linter。

### 运行应用（两种方式）

1. **浏览器 + Vite（开发态）** —— 启动后端，再 `npm run dev`，打开 `http://localhost:5173`。
2. **浏览器 + 后端托管（最简单）** —— `npm run build` 后启动后端，打开 `http://127.0.0.1:8642`。后端会自行托管构建出的 `dist/`，因此整个控制台只靠 Python 就能跑。

## 架构

### 逻辑在哪里

前端是纯 Web 表现层。实现或改动流水线行为时，**改 Python 后端**（`backend/engines/` 放逻辑，`backend/api/` 放端点）。前端改动只涉及 UI。

前端作为瘦客户端有一个具体含义：它用隐藏的 `<input type=file>` 让用户选文件，经 `POST /api/files/upload` 上传进布局的 `01_input/` 并取得该**绝对路径**，再交给后端完成所有读取、解码、写入 —— 下游代码路径都基于后端返回的绝对路径。

### 后端结构

- **`backend/main.py`** — FastAPI 应用入口。注册所有路由、全开放的 CORS（仅面向本地客户端）、`GET /api/health`，以及一个兜底的 SPA 路由来托管 `dist/`（**注册在最后**，因此每个 `/api/...` 路由都优先命中；未知的 `/api/*` 返回真实的 404，而不是 SPA 外壳）。
- **`backend/core/`** — 共享基础设施：
  - `paths.py` — `Layout`（单根）：一切跟随用户选择的工作空间根（`paths.working_dir` = 根 `app.json` 里的指针），持有 `00_temp/` … `07_output/`（八个管线目录）外加 `config/` + `logs/`（工程自己的配置与日志）。`ensure()` 仅当设置了工作空间时执行（只 `mkdir exist_ok`，绝不删除 / 覆盖）；未设置时返回惰性 `Layout(None)`（所有路径属性为 `None`，不落地任何目录）。未设置工作空间时流水线锁定：后端写端点统一经 `require_workspace()`（`api/_common.py`）返回 409，前端以同一条件禁用入口；工作空间在开始页经 `GET/PUT /api/workspace` 设置 / 清除（只读端点不受影响）。
  - `config.py` — 持久化 JSON 配置，拆成两个文件：根 `app.json` = **通用配置模板 + 工作空间指针**（`paths.working_dir` 是唯一允许在根级写入的字段，其余字段只读，作为新工作空间的种子）；`<workspace>/config/app.json` = **当前工程独立配置**（设置工作空间时从根模板复制，已存在则不覆盖；此后所有读写都只针对它，`update_config` 绝不改根模板，并把 `working_dir` 强制为该工作空间）。Pydantic 模型，线程安全（`RLock`）；根文件缺失时由代码默认值自动种子（全新克隆）。
  - `tasks.py` — 异步**任务系统**（见下文）。
  - `logging_setup.py` — 日志跟随工作空间：轮转文件 handler 写入 `<workspace>/logs/app.log`，可在运行时重定向（设置 / 清除工作空间时）；未设置工作空间时仅控制台（不落地文件）。级别取自配置。
- **`backend/api/`** — 瘦 FastAPI 路由，委托给 engines：`text.py`、`book.py`、`audio.py`、`tts.py`（四个流水线模块）外加横切的 `tasks.py`、`config.py`、`files.py`；`_common.py` 放共享助手（`read_decoded_file`、`partial_copy`）。
- **`backend/engines/`** — 行为保持不变的移植（真正的算法，见下）。

### engines 是行为保持不变的移植 —— 这些不变量是承重墙

每个 engine 都从既有的 JS 工具 1:1 移植而来，测试套件固化了这些工具所保证的不变量。**改动时请保持这些不变量** —— 它们既是测试断言的内容，也是用户依赖的行为：

- `engines/text.py`（源自 `TextFormatter`）：确定性的空白/段落/标点/章节规则。不变量：**内容保持 + 幂等**（对已排版文本再排版是空操作）。
- `engines/book.py`（源自 `BookChunker/chunker.js`）：章节**铺满整段文本**，切分**只落在章节边界**，章节**从不重编号**，**没有章节 → 停止**（绝不由字数强切），且**把所有分册拼接起来能精确复现原文**。
- `engines/audio.py`（源自 `mp3-cue`）：均匀切分使每段都落在目标时长以内；停顿对齐只移动**内部**边界、保持**单调**、从不改变段数；切割是**无损 `-c copy`**（不重编码）。
- `engines/tts.py`：本地 Qwen3-TTS 单条合成（独立 `.venv-tts` 子进程编排，ML 依赖与 3.14 后端隔离）。前端单条合成页已移除——主链路走批量合成 `tts_batch.py` 与合并 `merge.py`。

两个移植细节很容易改坏，且很关键：

- **字数统计 = 去除换行后的 Unicode 码点数** —— 与文本上的 Python `len()` 一致（JS 原版统计的是 UTF-16 单元；Python 字符串是码点序列，所以是直接移植）。不要改成字节长度，也不要改成包含换行的统计。
- **写文件用 `write_bytes` / `.encode("utf-8")`，而非 `write_text`** —— `write_text` 在 Windows 上会把 `\n` 翻译成 `\r\n`，从而破坏往返/无损保证。

与源工具唯一*刻意*的改动：音频 engine 把**原生 `ffmpeg`/`ffprobe`** 作为流式子进程运行（从 `config.ffmpeg` 解析，否则取 `PATH`），而不是浏览器里的 `ffmpeg.wasm` —— 命令行相同、没有内存上限、离线可用、更快。

### 任务系统（长时任务）

长时任务（音频停顿检测、切割、角色配音、音频合成、音频合并）作为一个 `Task` 在**工作线程**里运行（`core/tasks.py`）：它暴露状态、进度、实时日志、start/pause/resume/cancel/retry 控制，并**通过 SSE 把事件推给 UI**（`GET /api/tasks/{id}/stream`，连接时先回放快照）。engine 代码拿到一个 `TaskHandle` 来上报进度/日志，并在每段工作之间调用 `handle.check()` 以实现协作式取消/暂停。失败的任务被标记为 `failed` 并**隔离** —— 绝不会把整个控制台带崩。各流程页内联显示所启动任务的实时进度 / 日志与取消控制（任务中心页面已移除）。

### 前端要点

- `api/client.ts` 是唯一的 HTTP 客户端；每次调用都指向**绝对**后端源 `http://127.0.0.1:8642`（后端 CORS 全开放）。可用 `VITE_API_BASE` 覆盖源地址。
- 路由使用 **hash history**（在没有任何服务端路由处理的静态托管下也能工作）；所有模块都挂在持久化的 `MainLayout` 之下，因此侧边栏保持固定。
- 路径别名 `@` → `src/`。状态放在 Pinia `stores/`（`app`、`project`、`settings`、`task`）；`views/` 每个模块一个；`components/ui/` 是 shadcn-vue 风格的原子组件。
