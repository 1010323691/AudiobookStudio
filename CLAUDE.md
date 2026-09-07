# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

AudiobookStudio 是一个桌面应用，在一个地方完成中文有声书的制作流程：
**文本排版 → 分册切割 → TTS（占位）→ 音频分集**，
外加任务中心和设置。它是一个三层结构的应用：

- **`backend/`** — Python **FastAPI** 服务（端口 `127.0.0.1:8642`）。**所有真正的逻辑都在这里。**
- **`src/`** — **Vue 3** 前端（TypeScript、Vite、Tailwind、Pinia、vue-router）。它是一个*瘦客户端*：只负责渲染 UI 并通过 HTTP 调用后端，本身不含任何处理逻辑。
- **`src-tauri/`** — **Tauri 2** 原生外壳（Rust）。它只负责打开一个承载 Vue 应用的窗口，并注册 `plugin-dialog`（原生文件选择器）+ `plugin-shell`（在系统中打开文件/文件夹）。不含业务逻辑。

这两个非 Python 层都可以用普通浏览器替代，因此开发和运行本应用**完全不需要 Rust 工具链**（见「命令」）。

## 命令

### 后端（Python）

虚拟环境是 `.venv/`（本机为 Python 3.14）。`requirements.txt` 刻意保持精简（fastapi、uvicorn、pydantic、python-multipart）以兼容 CPython 3.14 —— 它**没有**锁定 pytest，但 pytest 已经装进 `.venv` 里了。

```
# 启动 API（Windows）
.venv\Scripts\python -m backend.main        # → http://127.0.0.1:8642
# 启动 API（macOS / Linux）
python -m backend.main

# 运行整个测试套件（68 个测试）—— 在项目根目录运行
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
npm run tauri dev   # 完整桌面应用：先起 Vite 再开 Tauri 窗口（需要 Rust）
npm run tauri build # 生产桌面安装包 → src-tauri/target/release/bundle/
```

前端没有配置 ESLint/linter；`npm run typecheck`（vue-tsc，`strict: true`）就是类型门槛。Python 侧同样没有配置 linter。

### 运行应用（三种方式）

1. **桌面（Tauri）** —— 需要 Rust 工具链（Windows 上为 rustup + MSVC）。终端 1：启动后端；终端 2：`npm run tauri dev`。外壳会轮询 `GET /api/health`，就绪后加载界面。也可以双击根目录的 `start-dev.bat` 一键启动：后端开在独立窗口，`npm run tauri dev` 跑在当前窗口，关闭 Tauri 窗口后脚本会自动停止后端进程。
2. **浏览器 + Vite（开发态）** —— 启动后端，再 `npm run dev`，打开 `http://localhost:5173`。
3. **浏览器 + 后端托管（无 Rust，最简单）** —— `npm run build` 后启动后端，打开 `http://127.0.0.1:8642`。后端会自行托管构建出的 `dist/`，因此整个控制台只靠 Python 就能跑。

## 架构

### 逻辑在哪里

前端和 Tauri 外壳都是表现层。实现或改动流水线行为时，**改 Python 后端**（`backend/engines/` 放逻辑，`backend/api/` 放端点）。前端改动只涉及 UI；Tauri 改动只涉及窗口/插件。

前端作为瘦客户端有一个具体含义：它把**绝对文件路径**（通过原生 Tauri 对话框选中）交给后端，由后端完成所有读取、解码、写入。在普通浏览器（没有原生对话框）里，兜底是 `POST /api/files/upload`，它把文件存进布局的 `input/` 并返回该路径 —— 走的是同一条下游代码路径。

### 后端结构

- **`backend/main.py`** — FastAPI 应用入口。注册所有路由、全开放的 CORS（仅面向本地桌面客户端）、`GET /api/health`，以及一个兜底的 SPA 路由来托管 `dist/`（**注册在最后**，因此每个 `/api/...` 路由都优先命中；未知的 `/api/*` 返回真实的 404，而不是 SPA 外壳）。
- **`backend/core/`** — 共享基础设施：
  - `paths.py` — `Layout`：一个工作目录（默认 `workspace/`，可通过 `paths.working_dir` 覆盖），其下有 `input/`、`output/{text,books,tts,audio}`、`temp/`、`logs/`、`config/`。
  - `config.py` — 一个持久化 JSON 配置（`config/app.json`，Pydantic 模型，线程安全的 get/`update_config`）。它始终存放在*默认*工作目录，因此不会追着它自己的 `working_dir` 设置跑。
  - `tasks.py` — 异步**任务系统**（见下文）。
  - `logging_setup.py` — 写入 `logs/` 的日志。
- **`backend/api/`** — 瘦 FastAPI 路由，委托给 engines：`text.py`、`book.py`、`audio.py`、`tts.py`（四个流水线模块）外加横切的 `tasks.py`、`config.py`、`files.py`；`_common.py` 放共享助手（`read_decoded_file`、`partial_copy`）。
- **`backend/engines/`** — 行为保持不变的移植（真正的算法，见下）。

### engines 是行为保持不变的移植 —— 这些不变量是承重墙

每个 engine 都从既有的 JS 工具 1:1 移植而来，测试套件固化了这些工具所保证的不变量。**改动时请保持这些不变量** —— 它们既是测试断言的内容，也是用户依赖的行为：

- `engines/text.py`（源自 `TextFormatter`）：确定性的空白/段落/标点/章节规则。不变量：**内容保持 + 幂等**（对已排版文本再排版是空操作）。
- `engines/book.py`（源自 `BookChunker/chunker.js`）：章节**铺满整段文本**，切分**只落在章节边界**，章节**从不重编号**，**没有章节 → 停止**（绝不由字数强切），且**把所有分册拼接起来能精确复现原文**。
- `engines/audio.py`（源自 `mp3-cue`）：均匀切分使每段都落在目标时长以内；停顿对齐只移动**内部**边界、保持**单调**、从不改变段数；切割是**无损 `-c copy`**（不重编码）。
- `engines/tts.py`：一个刻意的**占位接缝**（`IMPLEMENTED = False`，`synthesize` 抛异常）。路由、配置段、导航项都已就位，因此接入真实 TTS 是增量改动 —— 实现 `synthesize`、翻转 `IMPLEMENTED` 即可。

两个移植细节很容易改坏，且很关键：

- **字数统计 = 去除换行后的 Unicode 码点数** —— 与文本上的 Python `len()` 一致（JS 原版统计的是 UTF-16 单元；Python 字符串是码点序列，所以是直接移植）。不要改成字节长度，也不要改成包含换行的统计。
- **写文件用 `write_bytes` / `.encode("utf-8")`，而非 `write_text`** —— `write_text` 在 Windows 上会把 `\n` 翻译成 `\r\n`，从而破坏往返/无损保证。

与源工具唯一*刻意*的改动：音频 engine 把**原生 `ffmpeg`/`ffprobe`** 作为流式子进程运行（从 `config.ffmpeg` 解析，否则取 `PATH`），而不是浏览器里的 `ffmpeg.wasm` —— 命令行相同、没有内存上限、离线可用、更快。

### 任务系统（长时任务）

长时任务（音频停顿检测、切割、未来的 TTS）作为一个 `Task` 在**工作线程**里运行（`core/tasks.py`）：它暴露状态、进度、实时日志、start/pause/resume/cancel/retry 控制，并**通过 SSE 把事件推给 UI**（`GET /api/tasks/{id}/stream`，连接时先回放快照）。engine 代码拿到一个 `TaskHandle` 来上报进度/日志，并在每段工作之间调用 `handle.check()` 以实现协作式取消/暂停。失败的任务被标记为 `failed` 并**隔离** —— 绝不会把整个控制台带崩。

### 前端要点

- `api/client.ts` 是唯一的 HTTP 客户端；每次调用都指向**绝对**后端源 `http://127.0.0.1:8642`（全开放的 CORS 让同一份代码在 WebView 和浏览器里都能工作）。可用 `VITE_API_BASE` 覆盖源地址。
- 路由使用 **hash history**（在 WebView 和没有服务端路由处理的普通浏览器里都能工作）；所有七个模块都挂在持久化的 `MainLayout` 之下，因此侧边栏保持固定。
- 路径别名 `@` → `src/`。状态放在 Pinia `stores/`（`app`、`project`、`settings`、`task`）；`views/` 每个模块一个；`components/ui/` 是 shadcn-vue 风格的原子组件。

### Tauri 要点

- `tauri.conf.json`：`beforeDevCommand` = `npm run dev`，`devUrl` = `http://localhost:5173`，`beforeBuildCommand` = `npm run build`，`frontendDist` = `../dist`。
- `src-tauri/src/lib.rs` 只注册 `dialog` 和 `shell` 两个插件。`capabilities/default.json` 授予 `dialog:default` + `shell:allow-open` —— 如果文件选择 / 打开文件夹失效，请保留这两项。
- 发布包**不包含** Python 后端；正式分发需要把它打包成 sidecar（如 PyInstaller）—— 目前尚未做，但不影响本地开发。
