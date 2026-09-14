# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

AudiobookStudio 是一个 Web 应用，在一个地方完成中文有声书的制作流程：
**开始（工作空间）→ 文本排版 → 分册切割 → 文本解析（LLM→JSON）→ 段落混合检查（拆多主体 / 删纯标点）→ 角色匹配检查（重判 speaker）→ 角色配音（LLM 语音推理 + TTS 克隆两阶段）→ 音频合成（批量）→ 音频合并（两阶段）→ 音频分集**，
外加设置。它是三层结构的应用：

- **`backend/`** — Python **FastAPI** 服务（端口 `127.0.0.1:8642`，CPython 3.14）。**所有真正的逻辑都在这里。**
- **`src/`** — **Vue 3** 前端（TypeScript、Vite、Tailwind、Pinia、vue-router）。*瘦客户端*：只渲染 UI 并调 HTTP，不含处理逻辑。
- **`tts-engine/` + `.venv-tts/`** — 本地 Qwen3-TTS 引擎。独立虚拟环境（**Python 3.10** + torch(CUDA) + qwen-tts），以**一次性子进程**运行；3.14 后端**永不 import torch**，ML 依赖与应用包完全隔离。

开发和运行本应用**不需要任何原生外壳或额外工具链**。

## 命令

### 后端（Python）

虚拟环境是 `.venv/`（Python 3.14）。依赖清单是 **`backend/requirements.txt`**（不在根目录），刻意精简（fastapi、uvicorn、pydantic、python-multipart）以兼容 CPython 3.14 —— 没有锁定 pytest，但 pytest 已装进 `.venv`。

```
# 启动 API（Windows）
.venv\Scripts\python -m backend.main        # → http://127.0.0.1:8642
# 启动 API（macOS / Linux）
python -m backend.main

# 运行整个测试套件（394 个测试）—— 在项目根目录运行
.venv/Scripts/python -m pytest backend/tests/ -v
# 单个文件 / 单个测试
.venv/Scripts/python -m pytest backend/tests/test_book.py -v
.venv/Scripts/python -m pytest backend/tests/test_book.py::test_range_char_count_excludes_newlines
```

`backend/tests/conftest.py` 把项目根目录加进 `sys.path`，测试以 `backend.engines.*` 方式导入，可从任意目录启动。`tts_worker.py` 的测试用 `importlib` 单独加载其**纯函数**（worker 顶层保持 stdlib-only，3.14 环境可 import），不碰 torch。

### 前端（npm，项目根目录）

```
npm run dev         # Vite 开发服务器，:5173（strictPort）
npm run build       # vue-tsc --noEmit（类型检查）+ vite build → dist/
npm run typecheck   # 仅 vue-tsc --noEmit
npm run preview     # 预览构建产物
```

前端没有 ESLint/linter；`npm run typecheck`（vue-tsc，`strict: true`）就是类型门槛。Python 侧同样没有 linter。

### TTS 独立环境（可选，仅「音频合成 / 音频合并 / 角色配音·克隆」需要）

```
powershell -ExecutionPolicy Bypass -File install_tts_env.ps1
```

重建 `.venv-tts`（幂等：先删后建）：Python 3.10 + torch/torchaudio 2.11.0（**PyTorch cu128 索引 = CUDA 构建**）+ qwen-tts 0.1.1 + transformers 4.57.3 + accelerate + soundfile/pydub/numpy/huggingface_hub。**安装顺序是承重墙**：先装 PyPI 其余依赖（Windows 上会顺带装进 CPU 版 torch——预期内），**最后**用 `--no-deps` 从 cu128 索引强制重装 CUDA torch 三元组，否则 CPU 轮子会静默盖掉 CUDA 构建；脚本第 4 步**断言 `cuda_available=True`**（不能只查退出码——python 一行打印 False 也退出 0）。脚本刻意 ASCII-only（PS 5.1 按系统 ANSI 码页读 `.ps1`，非 ASCII 会坏）。

### 运行应用

1. **开发态** —— 启动后端 + `npm run dev` → `http://localhost:5173`（`start.bat` 一键做这三件事）。
2. **后端托管（最简）** —— `npm run build` 后只启动后端 → `http://127.0.0.1:8642`。`main.py` 的兜底 SPA 路由托管 `dist/`（**注册在所有 API 路由之后**；未知 `/api/*` 一律真 404——GET 由 SPA 路由内判断、非 GET 由专门的 `api_not_found` 路由兜住，不会误返 SPA 外壳；`dist/` 不存在时返 503 提示先 build）。

## 目录结构

```
项目根/
├── backend/                 # Python 后端（所有处理逻辑）
│   ├── main.py              # FastAPI 入口：路由注册、CORS、/api/health、SPA 兜底托管
│   ├── api/                 # 瘦路由：text / book / audio / tts / script + 横切 tasks / config / files / workspace；_common.py 共享助手
│   ├── core/                # 基础设施：paths / pathio / config / tasks / concurrency / logging_setup
│   ├── engines/             # 真正的算法（见「流水线逻辑链」）
│   ├── resources/           # 捆绑默认提示词：default_prompts.txt（解析）、default_mix_check_prompts.txt（混合检查）、default_check_prompts.txt（角色匹配检查）
│   ├── tests/               # pytest 套件（394 个测试）
│   └── requirements.txt     # 精简依赖（fastapi / uvicorn / pydantic / python-multipart）
├── src/                     # Vue 3 前端（瘦客户端）
│   ├── api/                 # 唯一 HTTP 客户端 client.ts + 每模块一个封装
│   ├── views/               # 9 个页面（每模块一个）
│   ├── stores/              # Pinia：app / project / settings / task
│   ├── components/          # DirPicker + components/ui/ 原子组件（shadcn-vue 风格）
│   ├── composables/         # useAudioBus / useWorkspaceGate
│   └── utils/               # fileops / format / log-follow
├── tts-engine/tts_worker.py # TTS 工作进程（跑在 .venv-tts，一次性子进程；5 种 --mode）
├── install_tts_env.ps1      # 重建 .venv-tts
├── start.bat                # 一键启动（后端 + npm dev + 开浏览器）
├── .venv/                   # 3.14 应用环境（gitignore）
├── .venv-tts/               # 3.10 ML 环境（gitignore）
├── app.json                 # 根配置模板 + 工作空间指针（gitignore；唯一可写根级字段是 paths.working_dir）
└── dist/                    # 前端构建产物（gitignore），后端直接托管
```

**工作空间**（用户在「开始」页选的文件夹，一切产物所在）固定子目录：

```
<workspace>/
├── 00_temp/        # 引擎子进程暂存（段表 / 段清单 / part WAV，用完即清）
├── 01_input/       # 原始上传 + 排版结果（<原stem>_排版<ext>）
├── 02_split_text/  # 分册（<base> 分册NN 第XXX章 ~ 第YYY章.txt [+ <base>.zip]）
├── 03_parsed_json/ # 解析 JSON（<stem>.json 基文件永不改写 [+ <stem>_checked.json 两检查共享产物]）
├── 04_voice_profiles/  # voice_config.json + designed_voices/*.wav 试听
├── 05_audio_chunk/     # 每包一子目录 <源JSON stem>/：逐行 mp3 + manifest.json
├── 06_audio_merge/     # <包名>.mp3（编码失败兜底 <包名>.wav）
├── 07_output/          # 每源一子目录 <源stem>/：分集文件 + <base>.zip
├── logs/           # app.log（轮转）+ tts_batch_*.log（合成排障镜像）
└── config/app.json # 当前工程独立配置
```

## 核心约定（承重墙 —— 改代码前必读）

1. **字数统计 = 去除换行后的 Unicode 码点数**。分册的 `range_char_count` 对换行位置表做二分（非 BMP 计 1）；排版 stats 的 `chars` 是去空白长度。不要改成字节长度，不要计入换行。
2. **写文件用 `write_bytes` / `.encode("utf-8")`，绝不用 `write_text`** —— Windows 上 `write_text` 把 `\n` 翻译成 `\r\n`，破坏往返/无损保证（分册拼接复原原文、zip 字节还原都依赖它）。
3. **路径模型（`core/pathio.py`）= 工程可整体搬家的承重墙**。工作空间**内部**路径在成果物 JSON 里一律存**工作空间相对**的正斜杠串（`05_audio_chunk/s/0001.mp3`、`voice_config.json[].ref_audio`）；运行时读取一律 `pathio.resolve_path(值, 当前工作空间根)`。明确外部资源（`ffmpeg_path`/`ffprobe_path`、工作空间指针）**保持绝对、永不转换**（`to_workspace_relative` 对外部值返回 `None` 即此约定）。旧绝对路径透明兼容：读取按原值解析，加载/保存时经 `migrate_entries_in` **幂等**迁移为相对形式；工程搬家后失效路径按「原目录结构尾部（首个 `01_input/`…`07_output/`、`logs/`、`config/` 之后重锚）→ 全目录唯一文件名」两级恢复；恢复不了才抛**清晰**的 `PathNotFoundError`（提示重新选择工作目录），**绝不静默失败**。相对值经 `..` 越出工作空间 → `PathOutsideWorkspace`（绝不持久化）。API 响应给前端的路径仍是**绝对**路径（瘦客户端直接回传/拼下载 URL）；给 worker 的临时清单（`00_temp/merge_segments_*.json` 等）也带绝对路径（一次性，不持久化）——worker 另经 `--workspace` 参数解析 voice_config 里的相对 `ref_audio`。
4. **绝不删除用户数据**：`Layout.ensure()` 只 `mkdir(exist_ok)`；切换/清除工作空间不移动、不删除任何旧文件；引擎只清理自己在 `00_temp/` 的暂存物。
5. **LLM 调用走 stdlib `urllib.request`**（OpenAI 兼容 `chat/completions`，body/headers 与 openai SDK 字节级一致），**不装 openai SDK**（保持 3.14 依赖精简）。入口都在 `engines/script.py` 的 `_llm_chat_completion(_stream)`，`speaker_check` / `voices` 复用它。
6. **TTS 隔离**：3.14 后端永不 import torch。一切 ML 工作 = 一次性 `.venv-tts` 子进程（`engines/tts.py::run_worker` 统一编排）。子进程强制 `PYTHONUTF8=1`+`PYTHONIOENCODING=utf-8`（防 GBK 码页搞乱 `[progress]`/`[result]`/`[segment]` 行里的中文与路径）；取消时 Windows 用 `taskkill /F /T` 杀**进程树**（否则 worker 的 ffmpeg 孙进程成孤儿）。
7. **任务隔离**：长时任务任何异常 → 该任务 `failed`（`task.error = str(exc)`），**绝不把控制台带崩**；取消 → `cancelled`（不算失败）。
8. **确定性三引擎的不变量**（源自 JS 工具 1:1 移植，测试固化，用户依赖）：
   - `text.py`（←TextFormatter）：确定性空白/段落/标点/章节规则；**内容保持 + 幂等**（对已排版文本再排版是空操作）。
   - `book.py`（←BookChunker）：章节**铺满整段文本**（首章 start=0、末章 end=len、`ch[i].end==ch[i+1].start`）；切分**只落章节边界**；**从不重编号**（缺口保留原号，如 1,2,5 → `第001章 ~ 第005章`）；**无章节 → 停止**（绝不由字数强切）；**所有分册拼接 == 原文**。
   - `audio.py`（←mp3-cue）：均分后**每段 ≤ 目标时长**（+1e-6 容差）且铺满 `[0,total]`；停顿对齐只移动**内部**边界、保持**严格单调**（棘轮防交叉）、**从不改变段数**（窗口无停顿则留在原均分位，计 fallback）；切割是**无损 `-c copy`** 流拷贝（不重编码；ffmpeg/ffprobe 从 `config.ffmpeg` 解析否则取 PATH）。
9. **两阶段检查的不变量**（`mix_check.py` + `speaker_check.py` 共享 `<stem>_checked.json`）：原 `<stem>.json` **永不改写**。段落混合检查（先跑）基于**基文件**重建 `_checked.json`：纯标点条目**确定性删除**（`is_deletable_text`，不经 LLM）；多主体条目按 LLM 判定**拆条**，拆分段须过**四道校验门**（≥2 段成形 / 各段 text 去空白后**逐字拼回**原文 / 各段 speaker ∈ `NARRATOR`∪全书角色花名册 / ≥2 个不同 speaker）——任一失败 → WARNING + 记入重试清单，**不投票**（拆条全有或全无）；整批零有效判定（调用失败 / 响应不可解析）→ **立即原地重试一次**（同一窗口重发；仍失败 → 整批保留原样、不进尾部重试）；批循环结束后对**所有**未过门条目**重试一次**（按 ≤`batch_size` 分组、各带 ±`context_window` 上下文、提示词注明上轮失败的门原因；重试判定须再过同一套门），仍失败 → 放弃、保持原样（只重试这一次）；结果按**原始数组下标**键控、按原顺序重建 → 新位置天然唯一连续（不以固定下标作持久标识）。角色匹配检查（后跑）**就地更新同一个 `_checked.json`**（输入优先取 mtime ≥ 基文件的 `_checked`），**只允许 `speaker` 字段变化**（text/instruct 不动）；仅当动态投票出现**严格多数**（≥2 票且唯一领先）且 ≠ 原值才改；无共识/解析失败 → 保留原值，**从不猜**。两阶段角色名都**禁止翻译/音译/本地化/改写**（提示词层强约束：必须逐字复制窗口/花名册内既有 speaker，中文角色名保持原汉字）；混合检查的 `keep` 对**标错但单主体**的条目不动（纠错是匹配检查的职责）。两检查**共用一份批几何**（`speaker_check.batch_size` / `context_window`），提示词各自独立（`mix_check.*` / `speaker_check.*`）。下游一切阶段经 `resolve_parsed_json` **透明优先读 `_checked` 副本**（`_checked_variant`；已是 checked 名不二次加后缀）。
10. **合并不变量**：输入是 batch 写好的逐行 mp3（**不重解码**，pydub 拼接）；两阶段（每 100 段一批 part → 整书）的**批间间隙必须与单遍合并逐一样本一致**（`boundary_gap_ms`：段级 `pause_after` override 优先 > 同人 `same_ms` > 换人 `pause_ms`）；全链路只有一次 ffmpeg 编码（`-c:a libmp3lame`，刻意**不带 `-b:a`**，与 pydub 默认导出一致）；合并顺序恒等于解析 JSON 行序（按 `index` 重排）。

## 后端架构

### `backend/main.py`

`ROUTERS`（注册顺序）：tasks、config、files、text、book、audio、tts、script、workspace。CORS 全开放（仅面向本机回环客户端）。`GET /api/health`。SPA 兜底见「运行应用」。

### `backend/core/`

- **`paths.py`** — `Layout`（单根）：跟随工作空间根（根 `app.json` 的 `paths.working_dir` 指针），持有 8 个管线目录 + `logs/` + `config/`。`get_layout()`：未设置 → 惰性 `Layout(None)`（一切属性 None，`ensure`/`dirs` 空转）；已设置但文件夹已不在（工程被移动/删除）→ **同样保持惰性，不在旧位置重建空骨架**（由 `GET /api/workspace` 的 `exists: false` 与写端点 409 告知用户重选）。`resolve_parsed_json(script=None)`：解析下游该读哪个脚本 JSON——给定文件名则用之（基名透明升级为 `_checked`），省略则取 mtime 最新的基文件（再升 `_checked`），最后兜底遗留名 `annotated_script.json`。`resolve_parsed_json_all()`：「全部文件」（整书聚合）= 所有基文件按 `(mtime, name)` 排序（mtime ≈ 分册生成序 ≈ 阅读顺序，抗中文数字乱序），逐个升 `_checked`。哨兵 **`ALL_PARSED_JSON = "__all__"`**（前后端同一字面量；**只用于角色配音**，音频合成收到它直接 400）。
- **`pathio.py`** — 路径序列化/解析统一入口（详见核心约定 #3）。异常：`PathOutsideWorkspace`、`PathNotFoundError`（用户可读文案）。
- **`config.py`** — 持久化配置，**两个文件一个指针**：根 `app.json` = 通用配置模板 + 工作空间指针（`paths.working_dir` 是唯一允许在根级写入的字段，其余只读，作为新工作空间的种子）；`<workspace>/config/app.json` = 当前工程独立配置（设置工作空间时从根模板复制、**已存在则绝不覆盖**；此后所有读写只针对它，`update_config` 深合并 patch 后强制 `working_dir` 为该工作空间）。Pydantic `AppConfig`，线程安全（`RLock`）；根文件缺失由代码默认值自动种子。`get_config()` 每次读时把内存里的 `working_dir` 自愈为根指针当前值（工作空间被移动重选后 UI 不显示陈旧指针）；`reset_config_cache()` 在指针变化后调用。读取链：工作空间配置 → 根模板 → 纯代码默认（**读永不写**；文件损坏/缺失静默降级）。
- **`tasks.py`** — 任务系统（详见下文专节）。
- **`concurrency.py`** — 进程级 `ConcurrencyGate`（`threading.Condition` 而非 Semaphore，**上限可增可减**；恒 clamp ≥1，**没有"无限"模式**，保证 acquire/release 严格配平）。`set_concurrency(n)` 在每个解析/检查批次开始时按 `config.generation.max_concurrency` 调。语义：**文件间并行、文件内 LLM 调用串行**，每文件全程持有 1 个槽（`gate().acquire()` 进 / `finally release()` 出）。
- **`logging_setup.py`** — 轮转文件 handler 跟随工作空间（`<workspace>/logs/app.log`，2MB×5 份），设置/清除工作空间时重定向；未设置 → 仅控制台；指针指向的文件夹已不在 → **降级控制台，不复活幽灵 `logs/` 树**。

### `backend/api/`（瘦路由 → engines）

| 模块 | 端点（全部前缀 `/api/...`） |
|---|---|
| `_common.py` | `require_workspace()`（写端点守卫：未设置 409；目录已不存在 409）、`resolve_inbound_path()`（400 系）、`read_decoded_file()`（自动编码探测，400 系）、`partial_copy()`（只应用已知字段，防客户端脏键） |
| `files.py` | `GET /files/list/{module}`（module = **磁盘目录名**如 `02_split_text`，7 个可列目录，`00_temp` 不可寻址）、`GET /files/download/{module}/{name}`（FileResponse，防穿越）、`POST /files/upload`（FormData → `01_input/`，返回绝对路径） |
| `workspace.py` | `GET /workspace`（`{set, path, exists, is_default, dirs}`）、`PUT /workspace`（path 空 = 清除并重新锁定；先 mkdir 校验再落指针 → 400 不会写坏指针） |
| `config.py` | `GET /config`（空 prompts/check-prompts 在**响应里**种子默认值，不落盘）、`PUT /config`（409 守卫；深合并 patch 持久化到工作空间配置） |
| `tasks.py` | `GET /tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/{action}`（cancel/pause/resume/retry）、`GET /tasks/{id}/stream`（SSE） |
| `text.py` / `book.py` / `audio.py` / `tts.py` / `script.py` | 见下「流水线逻辑链」各阶段 |

### `backend/engines/`

| 文件 | 职责 / 入口 |
|---|---|
| `text.py` | 确定性排版。`format_text(text, cfg) → {text, stats}` |
| `book.py` | 分册。`decode_buffer`（编码探测，全后端共用）、`analyze_text`、`compute_volumes`、`volume_content`（单一字节切片）、`make_volume_filenames`、`check_chapter_sequence`、`build_zip` |
| `audio.py` | 分集。`probe_duration`、`detect_silences`（流式 stderr）、`parse_silence_log`、`build_plan`、`snap_boundaries`、`build_aligned_plan`、`cut_segments`（`-c copy`）、`output_name` |
| `script.py` | LLM→JSON 解析。`split_into_chunks`、`process_chunk`（重试/修复/抢救）、`generate_file`（Task worker）；`_llm_chat_completion(_stream)`（**全后端 LLM 传输的唯一实现**）；`clean_json_string`/`repair_json_array`/`salvage_json_entries`/`fix_mojibake` |
| `mix_check.py` | 段落混合检查。`is_deletable_text`（纯标点删除，确定性、不经 LLM）、`build_roster`（全书角色花名册）、`parse_mix_map`（keep/split 判定解析）、`validate_split_parts`（四道校验门）、`group_retry_indices`（未过门条目的重试分组）、`rebuild_entries`（按原序重建重编号）、`mix_check_file`（Task worker：批内零判定即时重试 + 尾部一次重试）；批几何复用 `speaker_check` 配置 |
| `speaker_check.py` | 角色匹配检查。`build_batch_window`（可选 `skip` 参数 = 混合检查的纯标点条目集，不标 target）、`parse_speaker_map`、`_pick_majority`、`check_file`（Task worker；输入已是 `_checked` 时**就地更新**，不二次加缀） |
| `script_prompts.py` / `mix_check_prompts.py` / `check_prompts.py` / `persona_prompts.py` | 捆绑默认提示词（`load_default_prompts` / `load_default_mix_prompts` / `load_default_check_prompts` 在 **import 时**加载 `resources/*.txt`，分隔符不恰有一个 `---SEPARATOR---` 会 RuntimeError）；`PERSONA_SYSTEM_PROMPT`/`PERSONA_USER_PROMPT` |
| `voices.py` | 角色配音两阶段。`prepare_foundations`（阶段 1，纯 LLM）、`make_clones`（阶段 2，纯 TTS）、`_design_preview`、`_fold_aliases`、`_ChildReg`（子进程注册表）、纯函数族（`extract_json_object`/`_select_target_bands`/`pick_ref_text`/`_sanitize`…） |
| `tts.py` | TTS 族**基础模块**（不再是"单条合成"）：`resolve_engine()`（`.venv-tts` 解释器 + worker 脚本；env 覆盖 `AUDIOTTS_PYTHON`/`AUDIOTTS_WORKER` 供测试桩用）、`run_worker()`（一次性子进程编排器：pump 线程、`[progress]`/其余行分流、协作取消/暂停、`log_file` 镜像、进程树 kill、temp 清理）、`WorkerWatchdogTimeout` |
| `tts_batch.py` | 批量合成编排。`synthesize`（Task worker，含看门狗缩批/隔离重启循环）、`_build_cmd`、`_build_segments`、`package_for`、`load_manifest`/`build_manifest`（增量写）、`is_done`、`plan_to_synthesize`、`count_completion`、`clamp_concurrency`（[1,64]） |
| `merge.py` | 两阶段合并编排。`run`（Task worker）、`_find_manifest`、`collect_segments`、`_output_name`、`MERGE_BATCH_SIZE=100` |

## 流水线逻辑链（需求 → 代码 精确映射）

> 每个阶段给出：页面 → 端点 → 引擎链 → 产物 → 不变量/测试。改需求时先在这里定位链路，再进具体函数。
> 前端页面 = `src/views/<X>.vue`；发起任务的端点一律返回 `{"task_id"}`，UI 经任务 SSE 跟踪（见「任务系统」）。

### 阶段 0 · 开始 / 设置（Dashboard.vue `/dashboard`，Settings.vue `/settings`）

`GET/PUT /api/workspace` → `core.config.set_workspace_pointer` / `init_workspace_config` / `logging_setup.setup_logging`。PUT 顺序：**先 mkdir 校验（400）→ 种子工作空间配置（不覆盖已有）→ 写根指针 → 重指日志**。PUT 空 path = 清除（重新锁定流水线、日志回控制台；不触碰任何文件）。
`GET/PUT /api/config` → `core.config.get_config` / `update_config`（409 守卫；`working_dir` 强制）。设置页把配置 JSON 深拷贝为 draft 编辑，保存 = 部分深合并；主题即时生效、保存才持久化。
前端门控：`useWorkspaceGate().workspaceSet`（读 `settings.config.paths.working_dir`）——7 个流水线页顶部 `WorkspaceGateAlert`、所有动作按钮 `:disabled`、`DirPicker` 整体不渲染。工作空间目录失效（`exists:false`）时**门控不重新锁定**（指针仍非空），仅 Dashboard 显示琥珀警告要求重选——重选后各页自然恢复。

### 阶段 1 · 文本排版（TextFormat.vue `/text`）

`POST /api/text/format {path, config?}` →（同步）`require_workspace` → `read_decoded_file`（`book.decode_buffer` 编码探测）→ `partial_copy(config.text, req.config)` → `text.format_text` → 写 `01_input/<原stem>_排版<ext>`（`write_bytes`，**原件保留**）→ 响应 `{source, encoding, output_path, stats, preview(前2000字), full_length}`。
引擎流水线：CRLF/CR→`\n` → 逐行 `normalize_line`（`keep_single_space` 决定空白折叠）→ `apply_punct`（`…{3,}`/`。{2,}`→`……`、重复 `,，!！?？` 折叠、`--`→`——`、孤立半角标点**仅当邻接 CJK** 才转全角——保护英文/URL）→ `is_chapter_title`（`CHAPTER_RE`：第N章/节/回/卷/集/部/篇/幕/场/折、卷N、Chapter N、楔子/序章/引子…；行 ≤40 字；`detect_chapters` 门控）→ 段落装配状态机（空行=硬边界；`dialogue_separate` 引号行起新段；`sentence_break` 句末断行；章节标题强制起新段；其余行内拼接）→ `punct_quotes` 直引号转弯引 → `"\n\n".join`。
10 个开关（`config.text`）：`keep_single_space`(F) / `sentence_break`(T) / `dialogue_separate`(T) / `detect_chapters`(T) / `punct_ellipsis`(T) / `punct_repeated`(T) / `punct_lone_ascii`(F) / `punct_quotes`(F) / `punct_dash`(F) / `live`(T，UI 即时重排)。
不变量与测试：内容保持 + 幂等 + 各开关行为 → `test_text.py`。前端完成后 `project.recordText`，「前往下一步」→ `/book`。

### 阶段 2 · 分册切割（BookSplit.vue `/book`）

`POST /api/book/analyze {path, target_chars?}`（**只读预览，无 409 守卫**；无章节 → HTTP 200 + `error` 字段）与 `POST /api/book/split {path, target_chars?, base?, as_zip?}`（`require_workspace`；无章节 → **400**）。
链：`read_decoded_file` → `analyze_text`（`build_newline_positions` → `detect_chapters`（`BOOK_CHAPTER_RE` 多行 + `filter_spurious_chapters` 仅删「孤立下凹」假章 + 铺满区间）→ 每章 `range_char_count`）→ `compute_volumes(chapters, target)`（`choose_volume_count`：floor/ceil 中偏差最小者、平局取小、钳 [1,N]；K−1 个内部切割点 `near_boundary` 对前缀和二分）→ 逐册 `volume_content`（**原文单一字节切片**）`write_bytes` 进 `02_split_text/` → 可选 `build_zip`（`ZIP_STORED` 无压缩，UTF-8 文件名）。
目标字数 `config.book.target_chars` 默认 100000（请求可覆盖）。文件名 `<base> 分册{K:zfill≥2} 第{first:zfill w}章 ~ 第{last:zfill w}章.txt`（w = max(3, 最大章号位数)；不可解析章号保留原 `numStr` 不补零；`sanitize_file_name` 换非法字符）。`check_chapter_sequence` 只报告缺号/重号/乱序（信息性，不改切分）。
不变量与测试：见核心约定 #8 → `test_book.py`。前端 `project.recordBook`，「前往下一步」→ `/script`。

### 阶段 3 · 文本解析 + 段落混合检查 + 角色匹配检查（ScriptParse.vue `/script`）

**解析** `POST /api/script/generate-files {files: [02_split_text 裸文件名]}` → **每文件一个独立 Task**（module `script`），`set_concurrency(generation.max_concurrency)` 共享闸门 → 响应 `{task_ids, files:[{file, task_id}]}`。
`script.generate_file`（Task worker）链：`llm.model_name` 空 → **占槽前**快速失败 → `gate().acquire()` → 读文件 `decode_buffer` → `fix_mojibake`（CP1252-as-UTF8 乱码替换表）→ `split_into_chunks(chunk_size=3000)`（按 `\n\s*\n` 分段落、小段打包、超长段按 `(?<=[.!?])\s+` 分句；切分无损）→ 逐 chunk `process_chunk`：
- context = 段位置标记（`Beginning of text` / `Part n of m` / `End of text`）+ 跨 chunk 角色名册（sorted 去重、剔除 NARRATOR）+ 上段末 3 条；user 模板 `{context}`/`{chunk}` 占位（`str.format`）；
- LLM = `urllib` OpenAI 兼容调用（`llm.stream` 决定流式与否；流式走双缓冲：`reasoning_content`+`content` 进 UI「流式反馈」，返回值只含 `content` —— 与非流式**字节级一致**；合流节流 0.12s/256 字符 → `handle.llm_chunk` + `handle.llm_rate`；**每 SSE 帧后查取消**）；
- 最多 3 次尝试（`max_retries=2`）：调用异常 / JSON 不可解析 → 重试；`clean_json_string`（剥围栏/思维标签/括号计数截取/控制字符转义）→ `repair_json_array`（补逗号/去尾逗号/丢非对象条目）→ 失败再 `salvage_json_entries` 正则抢救；**`TaskCancelled` 永不重试直接上抛**；
- 单 chunk 全败 → 返回 `[]`（**不抛异常**，仅日志）；**所有** chunk 皆空才文件级 `RuntimeError("未生成任何脚本条目。")`。
产物：`03_parsed_json/<源文件 stem>.json`（一源一文件）= JSON 数组 `[{speaker, text, instruct}]`——`speaker` = 全角大写角色名或 `NARRATOR`（非对话）；`text` = TTS 实际朗读文本；`instruct` = 1–2 句声音指导。返回 `{entries, output_path, count, speakers, input_chars}`。

**段落混合检查** `POST /api/script/mix-files {files: [03_parsed_json 基文件名]}`（`_checked` 后缀先剥回基名；resolver 恒取**基文件**——本阶段永远从 pristine 基文件重推）→ 每文件一个 Task（module `mix-check`），共享同一闸门。
`mix_check.mix_check_file` 链：`llm.model_name` 空 → **占槽前**快速失败 → `gate().acquire()` → 读基文件 JSON 三重校验（非 list/空/含非 dict → RuntimeError）→ 几何取 `speaker_check.batch_size/context_window`（**两检查共用一份设置**）→ 预计算：纯标点集（`is_deletable_text`，**确定性删除、不经 LLM**，并排除出目标集）+ 全书角色花名册（`build_roster`，写入提示词；拆分段 speaker 白名单 = `NARRATOR` ∪ 花名册）→ 批循环（`handle.check()` 前置；**目标全为可删条目 → 本批零 LLM 调用**；`build_batch_window(skip=可删集)`：可删条仍在窗口出现但不标 target）→ LLM keep/split 判定（`TaskCancelled` 穿透；**整批零有效判定**（调用失败 / 响应不可解析）→ **立即原地重试一次**：同一窗口重发、`TaskCancelled` 穿透，重试判定照常过门；仍零判定 → 整批保留原样、**不进尾部重试**（每批至多重试一次））→ 每条 split 过**四道校验门**（≥2 段成形 / 各段 text 去空白后**逐字拼回**原文 / 各段 speaker ∈ 花名册白名单 / ≥2 个不同 speaker）——任一失败 → WARNING + 记入重试清单（**不投票**：拆条全有或全无）→ **批循环结束后对全部未过门条目重试一次**（`group_retry_indices` 分组：每组 ≤`batch` 条且相邻间距 ≤`context_window`，每组 = 一次 LLM 调用，窗口 = 覆盖条目的连续区段 ±`context_window`、未失败条不标 target；user 提示追加【重试提示】逐条注明上轮失败的门原因；`TaskCancelled` 穿透；重试判定**再过同一套门**，通过 → 采纳，仍失败 / 改判 keep → 放弃、保持原样；**只重试这一次**）→ `rebuild_entries` 按**原始下标**顺序重建（keep = 浅拷贝、可删 = 丢弃、split = 按序展开 parts 且 `instruct` 置空——原 instruct 针对整条混合段，会误导 TTS）→ 新位置**天然唯一连续**（不以固定下标作持久标识）→ 若 `05_audio_chunk/<包>/manifest.json` 存在且任一段 (index, text) 与重建后脚本不符 → WARNING 提示重跑 音频合成/合并（**manifest 只警示、绝不改写**；比对 (index,text) 对而非数量——「1 删 + 1 拆 2」这类数量不变的错位也能捕获）→ 已存在**非过期** `_checked`（可能含角色匹配结果）→ 覆盖前 WARNING → **整循环后单次写** `<stem>_checked.json`（取消不写；基文件字节级不动）→ 返回 `{input_name, output_name, output_path, total, kept, deleted, splits, parts, new_total, speakers, rejected, recovered, abandoned}`（`rejected` = 首轮未过门条数、`recovered` = 重试挽回、`abandoned` = 重试后仍失败而放弃）→ `finally: gate().release()`。
LLM 提示词约束（`default_mix_check_prompts.txt`）：只判 target 条；`keep` = 单主体（**即使 speaker 标错也不动**——纠错是后一阶段职责）；`split` = 旁白与对话混入或 ≥2 角色台词 → 按原文顺序拆为最少连续段；parts 的 text 必须是原条 text 的**逐字切片**（禁改写/重排/增删/翻译）；speaker 逐字取自窗口/全书花名册/NARRATOR；**NEVER translate/romanize/transliterate/localize 角色名**；输出恰好一个 `{"results": [{index, action, parts?}]}` 对象。

**角色匹配检查** `POST /api/script/check-files {files: [03_parsed_json 基文件名]}`（`_checked` 后缀先剥回基名，防二次加缀；resolver `prefer_latest`：**`_checked` 较新**（mtime ≥ 基文件，即混合检查产物）→ 就地更新它，否则从基文件（重）生成）→ 每文件一个 Task（module `speaker-check`），共享同一闸门。
`speaker_check.check_file` 链：读入 JSON（基文件或 `_checked`；非 list/空/含非 dict → RuntimeError）→ 浅拷贝（**original 保持字节级不变；所有窗口都从 ORIGINAL 构建**）→ 按 `batch_size=20` 分批，每批 = targets + 前后各 `context_window=4` 条上下文（`build_batch_window`：target 条标 `target:True`、携带 index+原 speaker+text、**丢弃 instruct** 省 token；窗口边界 clamp）→ 批前 `handle.check()` → **首判**（复用解析传输；解析失败 → 本批全保留原值）→ 有分歧 → **动态多数投票**（每条分歧至多 1 首判 + 3 重试；`_pick_majority` 严格多数才采纳）→ **只改 `speaker`** → 写 `<stem>_checked.json`（输入已是 `_checked` 时**就地更新**，绝不 `_checked_checked.json`；**取消时不写**）。
LLM 提示词约束（`default_check_prompts.txt`）：只判 target 条；**绝不发明窗口外 speaker**；值必须逐字复制窗口内既有 speaker；**NEVER translate/romanize/transliterate/localize/paraphrase/normalize 角色名**；输出恰好一个 `{"results": [{index, speaker}]}` 对象。
遥测（`TaskHandle` → SSE）：`llm_chunk`（流式原文，Task 侧 128KB 字符上限、丢最旧，重连可回放）；`llm_rate`（`cps` 瞬时 + `cps10` = 后端按真实流式字符算的 **10 秒窗均值**（`RATE_WINDOW=10.0`，`(c末-c首)/(t末-t首)`，无偏）→ 吞吐量卡）；`llm_chars`/`llm_secs`（每完成一个 chunk 上报累计原文字数 + 冻结处理时长 → 处理速度表，chunk 之间不衰减）。
测试：`test_script.py`（切分/修复/流式字节一致/取消穿透）、`test_mix_check.py`（纯标点判定/判定解析/四道门/重建重编号/全可删批零调用/取消不写/**三类输入 e2e**（旁白+单角色 / 多角色 / 单主体）/**跨阶段就地接力**（mix 写 `_checked` → 匹配检查就地更新，无 `_checked_checked`）/resolver 基文件-新鲜-过期）、`test_speaker_check.py`（窗口/解析/投票/升级/只改 speaker/取消不写）、`test_llm_rate_window.py`。

### 阶段 4 · 角色配音（Voices.vue `/voices`）

**两阶段、互斥**（前端禁用另一阶段按钮——LLM 与 TTS 不抢显存）；无独立创建/删除端点，档案由两阶段 Task 整体重写。

**阶段 1（纯 LLM）** `POST /api/tts/prepare-foundations {speakers?, new_only, overrides?, script?}` → `voices.prepare_foundations`（module `voices-foundation`）：`_load_script`（`__all__` → `resolve_parsed_json_all()` 逐文件拼接，单文件损坏仅 WARNING 跳过）→ 证据采样（`_select_target_bands`：≤24 句全进 front；否则 front 8 + back 8 + 中段均匀 8，每条带 `±4` 邻句窗口、目标行标 `★`）→ `ThreadPoolExecutor(max_concurrency)` 并行 `_llm_persona`（复用 LLM 传输；`temperature=0.3`、`max_tokens=1024`；提示词 `config.persona_prompts.*` 空则回退 `persona_prompts.py` 内置；`str.replace` 填 `{speaker}`/`{line_windows}` 防大括号炸裂）→ `extract_json_object`（手写括号配对扫描）→ `{description, ref_text(40-60字)}` → 全败 → `_fallback_persona`（固定描述 + `pick_ref_text`）→ `_fold_aliases`（归一化精确 → 子串 → Jaccard≥阈值，写 `alias_of`）→ **每完成一个角色就整体重写** `voice_config.json`（页面实时刷新）。本阶段**不启动任何 TTS 子进程**。

**阶段 2（纯 TTS）** `POST /api/tts/make-clones {speakers?, new_only, concurrency?, script?}` → `voices.make_clones`（module `voices-clone`）：选有 foundation 且非 alias 的角色（`new_only` 排除已完成、含重试失败）→ 每角色 `_design_preview`：描述/参考文本写 **`00_temp/persona_*.desc/.txt`**（避免 Windows 命令行非 ASCII）→ `run_worker` 调 worker `--mode design`（`--design-model` 等）→ 产物 `04_voice_profiles/designed_voices/<_sanitize(speaker)>_<ns>.wav` → 成功：entry = `{type: clone, ref_audio(工作空间相对), ref_text, description, character_style, clone_status: done}`；失败：`{type: design, clone_status: failed}`（**单角色失败不中断整批**）。`concurrency` 缺省 1（**后端不读 `config.tts.parallel_workers`**——该字段由前端读入后作为 `req.concurrency` 传入）；N 个渲染子进程注册在 `_ChildReg`，取消时 `kill_all` 一次释放显存。

**只读** `GET /api/tts/voices?script=`（409 守卫豁免，无工作空间降级空）：读脚本（`__all__` 折叠整书：按 speaker 名去重、累加台词数、保首见顺序）+ `voice_config.json`（`migrate_entries_in` 惰性迁移 `ref_audio`）→ 每角色 `{name, line_count, status(ready|pending), foundation_status, clone_status, type, alias_of, description, preview}`。`ready` = 有 `alias_of` 或 `_voice_usable`（clone 需 `ref_audio`；design 需非空 `description`；custom 恒真）。`preview` 是**相对 `04_voice_profiles/`** 的路径，前端经 `GET /api/files/download/04_voice_profiles/{name}` 播放（`MiniAudioPlayer`）。
`voice_config.json` 条目字段：`type`（foundation/clone/design/custom）、`description`、`ref_text`、`ref_audio`（工作空间相对）、`alias_of`、`foundation_status`/`clone_status`（done|failed）、`character_style`。
测试：`test_voices.py`（JSON 抽取/别名解析/采样带/状态推断/子进程注册表）。完成后 `project.recordVoices` → 「前往音频合成」。

### 阶段 5 · 音频合成（BatchTTS.vue `/batch`）

`POST /api/tts/batch {indices?, script?, concurrency?, seed?, force_all?}` → `tts_batch.synthesize`（module `tts-batch`）；**`script == "__all__"` → 400**（"全部"只用于角色配音）。`GET /api/tts/batch-status?script=` → `{total, completed, remaining}`（completed = manifest 里 `ok` 且**文件仍在磁盘**的段；增量写 manifest 所以运行中刷新真实）。
`synthesize` 链：`resolve_parsed_json` + `_load_script`（缺/坏/空 → RuntimeError）→ 读 `voice_config.json`（**缺失仅 WARNING**；先 `migrate_entries_in` 迁移再 spawn，worker 按 `--workspace` 解析相对 `ref_audio`）→ `out_dir = 05_audio_chunk/<包名>`（`package_for` = 源 JSON stem 剥 `_checked`，兜底 `"batch"`）→ `_build_segments`（`index` = 全脚本行位置；`speaker` 取 `entry.speaker or entry.type`；跳空文本；带 `instruct`/`pause_after`）→ `load_manifest` + `plan_to_synthesize`（显式 indices 交集 > `force_all` 全量 > 默认 **resume** 只合未完成的）→ **零段短路**（重写完整 manifest、progress 1.0、**不启动引擎**省模型加载）→ 段表写 `00_temp/batch_segments_*.json`（绝对路径，用完删）→ `run_worker`（`--mode batch`，`--concurrency` 钳 [1,64] 缺省 `config.tts.batch_concurrency=4`，`--seed` 缺省 `batch_seed=-1` 随机，`--workspace`，三个模型 id，`--ffmpeg`）→ **看门狗重启循环**：`WorkerWatchdogTimeout`（退出码 124）→ workers>1 则 `workers//2` 缩批重启；workers==1 则对 `[watchdog]` 行解析出的段记罚（**两次超时 → 隔离**：`excluded` + manifest `ok:false reason="超时（已隔离）"`）；累计 8 次未愈 → RuntimeError（已完成进度已保住）→ 收尾：最终 manifest、统计；**`completed==0` → RuntimeError**（任务 FAILED，"本次 N 段全部合成失败"）。
worker（`tts-engine/tts_worker.py --mode batch`）：按段所需 type **只加载用到的模型**（custom→`model` / clone→`base_model` / design→`design_model`；`Qwen3TTSModel.from_pretrained`，优先 HF 本地缓存；cuda→bf16+device_map；import 噪音吞进 devnull）；无配置的角色 → 段级 error「缺少角色声音配置（X）——请先在「角色声音」页生成」（**不中断整批**）；clone 组每 speaker 建一次 `voice_clone_prompt`；组内行**按长度升序**；**惰性子批规划**每轮 `plan_next_sub_batch`（`plan_row_tokens` = 字符×1.2+overhead 估 token；`VramGovernor` 纯算术反馈环按实测显存压力**减半**/宽松**增长** `vram_scale` 重标定；`LENGTH_BANDS` 长度分档 cap；单批总字符 ≤ `--max-batch-chars`(12000)；超长行独批；长度比 ≤5 防混批；**每行必入恰好一个批**）；子批超时预算 `sub_batch_timeout_seconds`（cpu `600+4·chars`∈[600,10800]；clone `120+0.7·chars`∈[300,3600]；gpu `60+0.4·chars`∈[180,1500]）+ 心跳（10s 起每 20s）→ 超时先 flush `[watchdog]` 行再 `os._exit(124)`（进程+CUDA 上下文同死）；非超时故障 → size-1 也崩则 `os._exit(124)` 交后端隔离，否则 `observe_fault` 缩容 + 清显存 + **对半递归重试**；`seed>=0` 时每子批 `torch.manual_seed(seed + 子批序号)`（可复现）。
产物：`05_audio_chunk/<包>/<index+1 零填充>.mp3`（soundfile WAV → pydub/libmp3lame；<1024B 视为坏头删）+ **`manifest.json`**（list `[{index, speaker, text, pause_after, path(工作空间相对), ok, reason}]`，**每见一条 `[segment]` 行就增量重写**——取消不丢已完成工作；加载时惰性迁移旧绝对 path）。
worker 退出码：`0` = 成功（**个别段失败也 0**，段级失败走 `[segment] i error <reason>` 行）/ `1` = 通用失败 / `2` = setup 错误（stderr `TTS_WORKER_ERROR:`）/ `124` = 看门狗。
排障：全量 stdout/stderr 镜像 `workspace/logs/tts_batch_<ts>.log`（`[out]`/`[err]` + `=== attempt started/ended rc=N ===`）——任务日志是 SSE-only 的，此文件是失败运行的磁盘证据。
测试：`test_tts_batch.py`（钳制/看门狗缩批/隔离/resume/增量 manifest/相对路径/零启动）、`test_tts_worker.py`（规划/显存/governor/超时预算）。

### 阶段 6 · 音频合并（Merge.vue `/merge`）

`POST /api/tts/merge {m4b=false, package?}` → `merge.run`（module `merge`）。`m4b=true` 目前仅 WARNING（"M4B 后续阶段支持；本次生成 MP3"）。
`run` 链：`_find_manifest`（`05_audio_chunk/<package>/manifest.json` → 缺省取 mtime 最新包 → 再退遗留顶层 `05_audio_chunk/manifest.json`；无 → RuntimeError「未找到合成结果清单」）→ 解析/空 → RuntimeError → `migrate_entries_in` → `collect_segments`（只留 `ok` 且文件真实存在，**按 index 重排**；segs 空 →「没有可合并的音频」；missing>0 → WARNING「N 段…缺失，将跳过」）→ 停顿 `pause_ms = tts.pause_between_speakers_ms or 500` / `same_ms = tts.pause_same_speaker_ms or 250` → 批数 `ceil(N/100)`（>1 时日志「两阶段合并：N 段 → M 批（每批 100 段）→ 整书」）→ 暂存 `00_temp/merge_segments_*.json` + `00_temp/merge_tmp_*/` → `run_worker`（`--mode merge`，`--pause-ms/--same-same-ms/--tmp-dir/--merge-batch-size 100`，无 watchdog）→ `[result]` 捕获产物 → **finally（所有退出路径）**：`[result]` 指向 tmp 内 WAV（编码失败兜底）则搬进 `06_audio_merge/`；`rmtree(tmp_dir)` → 返回 `{file, path, segments, size}`。
worker（`_run_merge`）：**Stage 1** 每批 pydub `combine_audio_with_pauses`（`pause_after` override > 同人 `same_ms` > 换人 `pause_ms`；末段 override 被忽略——存入 part 元数据 `last_pause_after` 供批间用）→ `part_NNN.wav`；进度带 `merge_stage1_frac` 0.05→0.80。**Stage 2** 单 part → **`os.replace` 原子改名**即整书 WAV（免重解码快路径）；多 part → 批间间隙全由 `boundary_gap_ms`（与单遍合并同一规则）显式 override → 整书 WAV；`merge_stage2_frac` 0.80→0.95。**编码** `_encode_mp3_streaming`：`ffmpeg -y -i <整书WAV> -c:a libmp3lame <out.mp3>`（**全链路唯一一次编码**；Windows 绑 KILL_ON_JOB_CLOSE Job Object 防孤儿 ffmpeg）；进度双源（stderr `time=` 行 + 每 ~2s ffprobe 测增长中输出文件真实时长——Windows stderr 块缓冲兜底）；`merge_encode_frac` 0.95→1.00；失败（无 ffmpeg/非零退出/输出 <1KiB）→ 保留 WAV 经 `[result]` 上报 → 后端搬走。
产物：`06_audio_merge/<包名>.mp3`（包名清洗 Windows 非法字符；遗留顶层 manifest → `cloned_audiobook.mp3`；兜底 `.wav`）。
测试：`test_merge.py`（命令/两阶段日志/tmp 成败双清/WAV 兜底/错误路径）+ `test_tts_worker.py` 合并纯函数（批覆盖/`boundary_gap` == 单遍/进度带单调连续）。前端读包 manifest 预检（段数/就绪）→ 合并 → 内嵌 `<audio>` 试听 → `project.recordMerge` → 「前往音频分集」。

### 阶段 7 · 音频分集（AudioSplit.vue `/audio`）

同步：`POST /api/audio/probe {path}`（ffprobe 时长/大小/扩展名/MIME）；`POST /api/audio/plan {path, target_duration?}`（均分方案预览，`build_plan`）。
任务：`POST /api/audio/silences {path, target_duration?, align_tolerance?}` → `_silences_worker`（probe → `detect_silences` → `build_aligned_plan`；结果含 `pauses`、`snapped`、`fallbacks`、`shifts`——「智能对齐」预览）；`POST /api/audio/cut {path, target_duration?, smart_align?, align_tolerance?, naming_format?, start_number?, segments?}` → `_cut_worker`（**传了 `segments` 就直接用**（与页面预览一致）；否则按 `smart_align`（缺省 `config.audio.smart_align=True`）重探测停顿并建方案）→ `cut_segments` → 产物进 **`07_output/<源文件 stem>/`**（包名 = 源 stem））。
引擎细节：`probe_duration`（`ffprobe -show_entries format=duration`，timeout 120）；`detect_silences`（`ffmpeg -af silencedetect=noise=-30dB:d=0.5 -f null -`，reader 线程流读 stderr 折算进度，主循环 0.1s 轮询取消 → `proc.kill()`）；`parse_silence_log`（正则兼容 ffmpeg 4/5/6 与 `[silencedetect @ 0x…]` 前缀；负 `silence_start`（编码器延迟）忽略；`end≤start` 丢；**尾部未闭合静默封顶到总时长**；按 start 排序）；`build_plan`（`count = max(1, ceil(total/target − 1e-6))`，段平铺 `[0,total]` 末段钉住终点；`parse_duration_to_seconds` 支持 `MM:SS`/`HH:MM:SS`/裸秒）；`snap_boundaries`（只动内部边界；窗口 `W = min(tolerance, 段宽/2 − 0.5)`；窗口 `[max(ideal−W, prev+0.5), min(ideal+W, 下一ideal−W−0.5)]` **单调棘轮** → 永不交叉；取窗口内离 ideal 最近的**停顿中点**再钳回；无候选 → 留原位计 fallback）；`build_aligned_plan` = 均分 + snap（**段数不变**；`count<2` 时 `aligned=False`）；`cut_segments`（每段 `ffmpeg -y -ss S -i in -t D -c copy out`；stderr **按字节**读（Windows GBK locale 会炸 UTF-8 日志）；段间查取消；非零退出 → RuntimeError 带 stderr 尾 300 字）。
**分集命名**（`output_name`，commit f7db438）：**`naming_format` 即完整文件名**（默认 `第 {} 集`），`{}` → `(start+index)`、按 `start_number` 输入位数零填充；**不再拼源文件名前缀**（`"重活了 第 {} 集"` + start `"1"` → `重活了 第 1 集.mp3`）；无 `{}` → 追加 `_<n>` 防碰撞；`start` 非数字 → 1（`"10x"` → 10）。
常量：`SILENCE_NOISE_DB=-30`、`SILENCE_MIN_DURATION=0.5`、`DEFAULT_TOLERANCE=15`（钳 5..30）、`MAX_SEGMENTS=1000`（`/cut` 超限 → RuntimeError）。
切割后同步端点：`POST /api/audio/zip {base?, files[]}` → `07_output/<base>/<base>.zip`（`ZIP_STORED`）；`POST /api/audio/export {source_path, files[]}` → **源音频旁建 `分集/` 文件夹** `copy2` 拷入（构造文件名越界 `Path(name).name != name` → 400）。
测试：`test_audio.py`（均分/棘轮/段数不变/命名/停顿日志 + 真实 ffmpeg 集成测试，缺 ffmpeg 自动 skip）。参数（目标时长/命名/起始号/智能对齐/容差）由**前端** fire-and-forget 存回 `config.audio`（后端只按「请求字段 or 配置缺省」取值，自己不写配置）。

### 横切 · 任务系统（`core/tasks.py` + `api/tasks.py`）

发起：`TaskManager.create(module, label, func, *args, **kwargs)` → 立即返回 `{task_id}`（HTTP 200；**错误全部发生在任务内**）→ daemon 线程跑 `func(handle, …)`。engine 代码用 `TaskHandle`：`progress(frac, current)`、`log(msg, level)`、`check()`（**每段工作之间调**——cancel 置位 → `raise TaskCancelled`；pause 置位 → 0.1s 忙等；暂停不作用于进行中的 HTTP 流读）、`llm_chunk/llm_rate/llm_chars`（纯展示遥测）。
状态机：`pending → running → {paused ⇄ running} → 终态 {cancelled, succeeded, failed}`；`retry` 仅终态（清空 logs/result/LLM 状态后起新线程重跑）。
**SSE 协议**（`GET /api/tasks/{id}/stream`）：连接时**先取快照再订阅**（回放与直播不重叠）→ 先发 `snapshot`（含 `llm_stream` 全文回放、速率/字数遥测 → 面板跨刷新存活）→ 终态则止；否则转发事件直至 `final`（携带完整快照）；15s 无事件发 `ping` 保活。事件 `type`：`snapshot` / `progress{progress,current}` / `log{level,msg,t}`（时序追加，新行在下）/ `llm_chunk{data}` / `llm_rate{cps,cps10}` / `llm_chars{chars,secs}` / `status{status[,task]}`（**终态事件带完整快照**——客户端据此原子读取 result/error）/ `final{task}` / `ping`。前端 `task` store 用原生 `EventSource` 订阅（裸 `data:` 行统一 `onmessage`），`applyEvent` 按 type 分发；`final`/断流 → 关流 + `refresh()` 对齐列表。
控制：`POST /api/tasks/{id}/{cancel|pause|resume|retry}`（404 任务不存在；400 未知 action）。任务内存常驻（进程生命周期内），`GET /api/tasks` 全量快照。
当前使用任务的阶段：文本解析（每文件一任务）、段落混合检查（每文件一任务）、角色匹配检查（每文件一任务）、角色配音两阶段、音频合成、音频合并、音频停顿检测/切割。

### 横切 · TTS 子进程编排（`engines/tts.py`）

`resolve_engine()` → `(python, worker)`：`PROJECT_ROOT/.venv-tts/{Scripts/python.exe | bin/python}` + `tts-engine/tts_worker.py`（env 覆盖 `AUDIOTTS_PYTHON`/`AUDIOTTS_WORKER`，测试桩用）；缺失 → 可操作的 RuntimeError（"请先运行 install_tts_env.ps1…"）。
`run_worker(cmd, handle, on_line, *, temp_files, fail_prefix, watchdog_code, log_file)`：`Popen(cwd=PROJECT_ROOT, env={PYTHONUTF8=1, PYTHONIOENCODING=utf-8})` → 双 daemon reader 线程泵 stdout/stderr 进队列 → 主循环 0.15s：`handle.check()`（协作取消/暂停）→ 抽干队列：`[progress] frac label` → `handle.progress`；其余 stdout → `on_line`（stage 解析 `[result]`/`[segment]`/`[watchdog]`）；stderr → WARNING 级日志 + `stderr_tail(40)` → `log_file` 时全量镜像 → 进程退出且双 EOF 后收工 → **finally**：存活则 `_kill_worker_tree`（Windows `taskkill /F /T /PID`，POSIX 普通 kill）→ wait → 关管道 → 删 `temp_files` → 非零退出：`RuntimeError(f"{fail_prefix}失败（退出码 N）：{stderr尾500}")`，**恰为 `watchdog_code` 时改抛 `WorkerWatchdogTimeout`**（供 batch 缩批重启）。
worker（`tts-engine/tts_worker.py`，1986 行）5 种 `--mode`：`custom` / `design` / `clone`（前三者**不被后端调用**——后端只用 design（角色配音·克隆）、batch、merge；保留供手动/历史）、`batch`、`merge`。stdout 协议行：`[progress] <frac> <label>`、`[result] <abs path>`（恰好一次 = 最终文件）、`[segment] <i> ok|error <…>`、`[watchdog] timeout batch=<label> indices=[…] elapsed=<s>`（先 flush 再 `os._exit(124)`）。stderr 错误前缀 `TTS_WORKER_ERROR:`。模型常量 `DEFAULT_MODEL/BASE_MODEL/DESIGN_MODEL` = Qwen3-TTS-12Hz-1.7B-{CustomVoice,Base,VoiceDesign}；`DEFAULT_SPEAKER="serena"`、`DEFAULT_LANGUAGE="chinese"`。

## 配置项速查（`<workspace>/config/app.json`；根 `app.json` 为只读模板）

| 配置段.字段 | 默认 | 消费处 |
|---|---|---|
| `paths.working_dir` | `""` | 工作空间指针（**仅根文件可写**；工作空间配置里恒被强制为当前工作空间） |
| `text.*` | 见阶段 1 | `POST /api/text/format`（`partial_copy` 合并请求覆盖） |
| `book.target_chars` | `100000` | 分册 analyze/split 缺省 |
| `audio.target_duration` | `"10:00"` | 分集 probe/plan/silences/cut 缺省 |
| `audio.naming_format` / `start_number` | `"第 {} 集"` / `"1"` | 分集 `output_name`（完整文件名语义，见阶段 7） |
| `audio.smart_align` / `align_tolerance` | `True` / `15` | 停顿对齐（`snap_boundaries` 窗口，钳 5..30） |
| `tts.model` / `base_model` / `design_model` | Qwen3-TTS-12Hz-1.7B-{CustomVoice,Base,VoiceDesign} | batch `--model/--base-model/--design-model`（空 → worker 同名默认） |
| `tts.speaker` / `language` / `device` | `serena` / `chinese` / `auto` | worker 缺省（auto\|cuda\|cpu\|mps） |
| `tts.pause_between_speakers_ms` / `pause_same_speaker_ms` | `500` / `250` | merge `--pause-ms/--same-same-ms`（0 → 回退该默认） |
| `tts.parallel_workers` | `1` | **仅前端读**（Voices 页 make-clones 的 concurrency 初值）；后端引擎不读 |
| `tts.batch_concurrency` | `4` | 合成「批内段数」上限（请求 `concurrency` 缺省时用；钳 [1,64]；与 parallel_workers 是两种并行） |
| `tts.batch_seed` | `-1` | 合成可复现 seed（`seed + 子批序号` 播种；-1 = 随机） |
| `tts.api_base/api_key/voice/concurrency` | 空/1 | **遗留** API 字段，本地引擎不读（保证旧配置 round-trip） |
| `llm.base_url` / `api_key` | `http://localhost:11434/v1` / `"local"` | OpenAI 兼容端点（默认本机 Ollama）；远程 API 需真 key |
| `llm.model_name` | `""` | **必须用户设置**（空 → 解析/检查/阶段 1 各自快速失败或兜底） |
| `llm.stream` | `True` | 流式（「流式反馈」面板）；服务器拒 `stream:true` 时置 false 回退非流式 |
| `prompts.system_prompt/user_prompt` | `""` | 解析提示词（空 → `resources/default_prompts.txt`；user 模板含 `{context}`/`{chunk}` 占位） |
| `persona_prompts.system/user/advanced_prompt` | `""` | 角色配音·阶段 1（空 → `persona_prompts.py` 内置；user 含 `{speaker}`/`{line_windows}`，`str.replace` 填充） |
| `speaker_check.batch_size` / `context_window` | `20` / `4` | 检查批大小 / 每侧上下文条数（一批最多 `batch + 2×window` 条；**段落混合检查共用此两项**） |
| `speaker_check.system_prompt/user_prompt` | `""` | 角色匹配检查提示词（空 → `resources/default_check_prompts.txt`；user 含 `{context}`） |
| `mix_check.system_prompt/user_prompt` | `""` | 段落混合检查提示词（空 → `resources/default_mix_check_prompts.txt`；user 含 `{context}`）；批几何与角色匹配检查共用，此段**无**几何字段 |
| `generation.chunk_size` / `max_tokens` | `3000` / `4096` | 解析切块 / 单次完成上限 |
| `generation.temperature/top_p/top_k/min_p/presence_penalty/banned_tokens` | `0.6/0.8/0/0.0/0.0/[]` | LLM 采样（0 值的 top_k/min_p 不发；`banned_tokens` 非空才发） |
| `generation.max_concurrency` | `3` | 进程级 LLM 闸门（解析 + 检查共享；`set_concurrency` 每批设置；另被阶段 1 `ThreadPoolExecutor` 用作 worker 数） |
| `ffmpeg.ffmpeg_path/ffprobe_path` | `""` | 空 = 从 PATH 解析（**绝对路径，永不相对化**） |
| `log.level` | `"INFO"` | 根 logger 级别（运行时可改） |
| `ui.theme` | `"system"` | 前端 `applyTheme`（system 挂 matchMedia 监听） |

## 前端要点

- **`src/api/client.ts` 是唯一 HTTP 客户端**：`API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8642'`（绝对源直连，CORS 后端全开）；fetch + JSON（响应先取 text 再试解析）；`!res.ok` 抛 `ApiError`。SSE 不在 client 里：`api/tasks.ts::streamTask` 用**原生 `EventSource`** 连 `/api/tasks/{id}/stream`（后端帧全是裸 `data:` 行 → 统一 `onmessage`，按 JSON `type` 分发；`final`/`CLOSED` 关闭并回调；断开自动重连）。唯一例外：`uploadFile` 走 FormData。
- **路由 = hash history**（静态托管下可工作），全挂持久 `MainLayout`（侧边栏固定 + `keep-alive`）：`/dashboard` 开始 · `/text` 排版 · `/book` 分册 · `/script` 解析 · `/voices` 角色配音 · `/batch` 合成 · `/merge` 合并 · `/audio` 分集 · `/settings` 设置（`/` → redirect dashboard）。
- **stores**（Pinia setup 风格）：`app`（`backendUp` 状态灯，3s 轮询 health）；`project`（流水线交接：`textOutput/bookOutputs/activeScript/voiceResult/batchResult/mergeResult/audioOutputs` + `record*` 动作 + computed `bookInput/scriptInput/audioInput`——「前往下一步」按钮的数据源；`activeScript` 被角色配音与音频合成**共享**）；`settings`（`config` 全量 + `load/save/applyTheme`）；`task`（**应用级单例**：`refresh()` 全量对齐 + 对每个非终态任务开 SSE 流；`applyEvent` 按 type 分发（日志超 1000 条裁最旧、`llm_stream` 客户端 128KB 上限与后端对齐）；`control`/`stopAll`；store 创建时即 `refresh()`）。
- **文件选择 = 隐藏 `<input type=file>` → `uploadFile` → 绝对路径**（`utils/fileops.pickFile`）；目录/文件浏览用 `DirPicker`（扫 `GET /api/files/list/{目录名}`，支持 default/`__all__` 行、pick-dirs、后缀过滤，`@scanned` 回传绝对路径）；下载 = 导航 `GET /api/files/download/{目录名}/{文件名}`。
- **组件**：`LiveLogPanel`（进度条 + 当前步骤 + 分级着色日志 + 自动跟随 + actions 插槽放取消钮）、`LiveStreamPanel`（原始 LLM 流回看）、`MiniAudioPlayer`（`useAudioBus` 全局单例保证同屏只播一个试听）、`WorkspaceGateAlert`、`Toaster`。`utils/log-follow.useLogAutoFollow`：仅当用户本就在近底（≤48px）时才自动滚底——翻看历史不被拽回。
- 页面任务展示惯例：解析页每文件每阶段一行（解析/混合检查/角色匹配检查三阶段共用同一行状态机 `kind: 'parse' | 'mix' | 'check'`，running 文案分别为 解析中 / 混合检查中 / 检查中；状态 Badge + 进度 + `LiveLogPanel` + 完成后 `LiveStreamPanel` 可回看；顶部 3 指标卡 = 并发数 / 吞吐量 Σ`llm_cps_10s` / 处理速度 Σ`llm_chars`÷Σ`llm_secs`，**纯 SSE 驱动无定时器**）；文件行徽章：「已混合」= 任务存储里存在该文件成功的 mix-check 任务（内存态信号，仅当前后端运行内有效——磁盘共享 `_checked` 无法区分「仅混合」与「混合+匹配」，后端重启后该徽章消失）；「已处理」= 磁盘存在 `_checked.json` 产物（两检查阶段共享的持久兜底）——精确状态看实时任务行；角色配音/合成/合并页各自 `LiveLogPanel`（角色配音 watch 任务 progress 变化即重拉角色表）；分集页用行内 Alert（`task.current` + 进度条 + 取消）；合成的「已合成/总段落」用 `batchStatus` **3s 轮询**（读增量 manifest 真实计数，不走 SSE）。

## 测试（394 个，`backend/tests/`）

| 文件 | 固化的行为 |
|---|---|
| `test_text.py` | 排版：内容保持、幂等、空白/省略号/重复标点、孤立 ASCII 保护（URL）、对话/叙述分段、句断开关、章节隔离与正负样例、10 开关逐一、端到端样例 |
| `test_book.py` | 分册：章节铺满/真实章头/无章节即停、册数选择、连续分区、**无损往返拼接 == 原文**、字数排除换行（非 BMP 计 1）、命名格式/补零/缺口不重编号、编码探测（BOM/UTF-8/GB18030）、中文数字解析、序号体检、zip STORED 往返 |
| `test_audio.py` | 分集：时长解析、均分铺满且每段≤目标、只动内部边界/严格单调/窗口回退、**段数不变**、分集命名（`{}`/无 `{}`/非数字 start）、停顿日志解析（负 start/尾部封顶/排序）、真实 ffmpeg 集成（缺则 skip） |
| `test_merge.py` | 合并编排：命令含 `--tmp-dir`/`--merge-batch-size 100`、两阶段日志、成功/失败路径都清暂存、WAV 兜底搬迁、manifest 缺失/为空/全缺文件错误、缺文件跳过 + 警告、遗留输出名 |
| `test_script.py` | 解析：切分无损/打包/分句、JSON 清理/修复/正则抢救、乱码修复、默认提示词加载、**流式与非流式字节级一致**、思维链不进返回值、流中取消穿透不重试 |
| `test_mix_check.py` | 混合检查：纯标点判定（删/留）、判定解析（results 对象/裸 list/下标键/垃圾→keep/非 target 丢弃/单目标回退）、四道门（逐字拼回通过/乱序丢添拒绝/花名册白名单/同主体拒绝）、**重试**（批内即时：垃圾响应/调用异常/两次皆败放弃 · 尾部：分组规则纯函数/挽回/改判 keep 放弃/仍失败放弃/远距双失败双调用/提示带门原因）、重建重编号（顺序/instruct 空/原文件不动）、窗口 skip、**三类输入 e2e**（旁白+单角色 / 多角色 / 单主体）、同主体拆分拒绝（重试改判 keep）、拼接不符保留（重试再败）、**全可删批零 LLM 调用**、**取消不写文件**、多批钉死调用数、空模型名 fail-fast、**跨阶段接力**（mix 写 `_checked` → 匹配检查就地更新，无 `_checked_checked`、基文件不动）、resolver（基文件恒取/新鲜/过期回退） |
| `test_speaker_check.py` | 检查：窗口构建/边界 clamp、speaker 解析（多形态/垃圾→None）、多数投票（平票/孤票→None）、无分歧 1 次调用、2:1 一次重试、1:1:1 升级、**只有 speaker 变**、重试失败不中止、多批推进、**取消不写文件**、空模型名 fail-fast |
| `test_llm_rate_window.py` | 吞吐量 10 秒窗：样本数/零跨度防除零、真实均值、年轻任务部分跨度、累计单调、样本逐出、SSE 事件 |
| `test_voices.py` | 角色配音纯函数：JSON 对象抽取、说话人名归一化（CJK 保留）、Jaccard、采样带/窗口、`pick_ref_text`/fallback、`_sanitize`、foundation/clone 状态推断、`_ChildReg` kill 语义 |
| `test_tts_batch.py` | 合成编排：段构建/`_voice_usable`、concurrency 钳制（含 0 与负值）、看门狗缩批重启成功、workers=1 两次超时隔离（manifest 记 ok:false）、8 次上限、运行日志镜像、resume/force_all/显式 indices、**增量写 manifest + 工作空间相对路径**、零段不启动引擎、`batch-status` |
| `test_tts_worker.py` | worker 纯函数（importlib 加载，无 torch）：子批规划（行必入恰一批/长度分档/字符上限/超长独批/比值拆批）、显存估算、`band_cap_for_chars` 钉死值、`VramGovernor`（减半/增长/floor/故障/标定）、超时预算（cpu/clone/gpu 曲线）、看门狗、合并纯函数（批覆盖/`boundary_gap`==单遍/进度带单调）、**顶层 stdlib-only + 旧调度器已删除**的模块约束 |
| `test_pathio.py` | 路径模型：相对/绝对/外部资源、`..` 越界拒绝、工程搬家两级恢复、幂等迁移、`migrate_entries_in` list/dict |
| `test_paths.py` | Layout 惰性/ensure、指针解析（相对→项目根）、`resolve_parsed_json(_all)`（`_checked` 升级/`__all__`/mtime 序/遗留名兜底） |
| `test_config.py` | 配置 round-trip、两文件模型（根只写指针/工作空间独立/不覆盖已有）、TTS 字段缺省（三模型 id、500/250、parallel_workers=1 ≠ batch_concurrency=4）、遗留字段保留 |
| `test_concurrency.py` | 闸门：clamp≥1、增/减上限、acquire/release 配平、唤醒等待者 |

## 已知陷阱 / 历史遗留（改动时留意）

> 以下均为**有意的设计**而非缺陷——改动时别"顺手修正"；真正可修的陈旧文案（误导日志 / 测试 docstring / README 测试数）已清理。

- worker 的 `--mode custom/clone` **不被后端任何端点调用**（主链路只用 `design`（角色配音·克隆）、`batch`、`merge`）——保留供手动/历史用途；改 worker 时别误以为它们有调用方。
- `config.tts.parallel_workers` **后端不读**（只被前端 `Voices.vue` 读作 make-clones 并发初值）；合成用的「批内段数」是另一个字段 `tts.batch_concurrency`。两者是两种不同的并行（并行子进程 vs GPU 张量批），文档/注释里别混。
- 音频分集页参数的「自动保存」发生在**前端**（fire-and-forget `PUT /api/config`）；后端只按「请求字段 or 配置缺省」取值，自己从不写配置。
- `merge` 的 `m4b=True` 目前只是 WARNING（输出 MP3）——M4B 是后续阶段。
- `config.py` 的读取**永不失败**：文件缺失/损坏/类型不符 → 静默降级（工作空间配置 → 根模板 → 代码默认）；`_load_config_file` 吞掉一切解析异常。写入才是严格路径。
- 角色配音页的「全部文件」（`__all__`）是**该页局部**选择，不写入共享的 `project.activeScript`；音频合成页的 DirPicker 才绑定 `activeScript`（且拒绝 `__all__`）。
- `GET /api/files/list/{module}` 的 `module` 参数是**磁盘目录名**（`02_split_text` 等），不是 Layout 属性名。
- 段落混合检查**覆盖**既有 `_checked`（自基文件重建）：若用户先跑角色匹配检查、再跑混合检查，匹配结果会被重建冲掉——任务日志 WARNING 提示，随后重跑角色匹配检查即恢复；正确顺序 = 解析 → 混合检查 → 匹配检查。
- 下游 `_checked_variant`（`resolve_parsed_json` 链）**无 mtime 守卫**：`_checked` 存在即优先于基文件（重解析后陈旧 `_checked` 仍会被角色配音/音频合成读到）——仅 `check-files` 路由的 resolver 有 mtime 守卫；重跑段落混合检查（自新基文件重建 `_checked`）即恢复一致。
