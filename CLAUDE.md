# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

AudiobookStudio 是一个 Web 应用，在一个地方完成中文有声书的制作流程：
**开始（工作空间）→ 文本排版 → 分册切割 → 文本解析（LLM→JSON，内含断句失败校验 / 纯归属标签删除 / 归属抽样三阶段）→ 角色配音（LLM 语音推理 + TTS 克隆两阶段）→ 音频合成（批量）→ 音频合并（两阶段）→ 音频分集**，
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

# 运行整个测试套件（545 个测试）—— 在项目根目录运行
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
│   ├── resources/           # 捆绑默认提示词：default_prompts.txt（解析）、default_check_prompts.txt（解析内重判，不可配置）
│   ├── tests/               # pytest 套件（545 个测试）
│   └── requirements.txt     # 精简依赖（fastapi / uvicorn / pydantic / python-multipart）
├── src/                     # Vue 3 前端（瘦客户端）
│   ├── api/                 # 唯一 HTTP 客户端 client.ts + 每模块一个封装
│   ├── views/               # 9 个页面（每模块一个）
│   ├── stores/              # Pinia：app / project / settings / task
│   ├── components/          # DirPicker + components/ui/ 原子组件（shadcn-vue 风格）
│   ├── composables/         # useAudioBus / useWorkspaceGate
│   └── utils/               # fileops / format / log-follow
├── tts-engine/tts_worker.py # TTS 工作进程（跑在 .venv-tts，一次性子进程；6 种 --mode）
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
├── 03_parsed_json/ # 解析 JSON（<stem>.json 基文件 = 唯一产物；旧工程或有惰性 <stem>_checked.json 孤儿——不再读取/生成，留盘不删）
├── 04_voice_profiles/  # voice_config.json + designed_voices/*_c{k}.wav 克隆候选试听
├── 05_audio_chunk/     # 每包一子目录 <源JSON stem>/：逐行 mp3 + manifest.json
├── 06_audio_merge/     # <包名>.mp3（编码失败兜底 <包名>.wav）
├── 07_output/          # 每源一子目录 <源stem>/：分集文件 + <base>.zip
├── logs/           # app.log（轮转）+ tts_batch_*.log / tts_clone_*.log（合成/克隆排障镜像）
└── config/         # app.json 当前工程独立配置 + spot_check_history.json（归属抽样读数历史；引擎自管，不经文件 API）
```

## 核心约定（承重墙 —— 改代码前必读）

1. **字数统计 = 去除换行后的 Unicode 码点数**。分册的 `range_char_count` 对换行位置表做二分（非 BMP 计 1）；排版 stats 的 `chars` 是去空白长度。不要改成字节长度，不要计入换行。
2. **写文件用 `write_bytes` / `.encode("utf-8")`，绝不用 `write_text`** —— Windows 上 `write_text` 把 `\n` 翻译成 `\r\n`，破坏往返/无损保证（分册拼接复原原文、zip 字节还原都依赖它）。
3. **路径模型（`core/pathio.py`）= 工程可整体搬家的承重墙**。工作空间**内部**路径在成果物 JSON 里一律存**工作空间相对**的正斜杠串（`05_audio_chunk/s/0001.mp3`、`voice_config.json[].ref_audio`）；运行时读取一律 `pathio.resolve_path(值, 当前工作空间根)`。明确外部资源（`ffmpeg_path`/`ffprobe_path`、工作空间指针）**保持绝对、永不转换**（`to_workspace_relative` 对外部值返回 `None` 即此约定）。旧绝对路径透明兼容：读取按原值解析，加载/保存时经 `migrate_entries_in` **幂等**迁移为相对形式；工程搬家后失效路径按「原目录结构尾部（首个 `01_input/`…`07_output/`、`logs/`、`config/` 之后重锚）→ 全目录唯一文件名」两级恢复；恢复不了才抛**清晰**的 `PathNotFoundError`（提示重新选择工作目录），**绝不静默失败**。相对值经 `..` 越出工作空间 → `PathOutsideWorkspace`（绝不持久化）。API 响应给前端的路径仍是**绝对**路径（瘦客户端直接回传/拼下载 URL）；给 worker 的临时清单（`00_temp/merge_segments_*.json` 等）也带绝对路径（一次性，不持久化）——worker 另经 `--workspace` 参数解析 voice_config 里的相对 `ref_audio`。
4. **绝不删除用户数据**：`Layout.ensure()` 只 `mkdir(exist_ok)`；切换/清除工作空间不移动、不删除任何旧文件；引擎只清理自己在 `00_temp/` 的暂存物。
5. **LLM 调用走 stdlib `urllib.request`**（OpenAI 兼容 `chat/completions`，body/headers 与 openai SDK 字节级一致），**不装 openai SDK**（保持 3.14 依赖精简）。入口都在 `engines/script.py` 的 `_llm_chat_completion(_stream)`，`voices` 复用它。
6. **TTS 隔离**：3.14 后端永不 import torch。一切 ML 工作 = 一次性 `.venv-tts` 子进程（`engines/tts.py::run_worker` 统一编排）。子进程强制 `PYTHONUTF8=1`+`PYTHONIOENCODING=utf-8`（防 GBK 码页搞乱 `[progress]`/`[result]`/`[segment]` 行里的中文与路径）；取消时 Windows 用 `taskkill /F /T` 杀**进程树**（否则 worker 的 ffmpeg 孙进程成孤儿）。
7. **任务隔离**：长时任务任何异常 → 该任务 `failed`（`task.error = str(exc)`），**绝不把控制台带崩**；取消 → `cancelled`（不算失败）。
8. **确定性三引擎的不变量**（源自 JS 工具 1:1 移植，测试固化，用户依赖）：
   - `text.py`（←TextFormatter）：确定性空白/段落/标点/章节规则；**内容保持 + 幂等**（对已排版文本再排版是空操作）。
   - `book.py`（←BookChunker）：章节**铺满整段文本**（首章 start=0、末章 end=len、`ch[i].end==ch[i+1].start`）；切分**只落章节边界**；**从不重编号**（缺口保留原号，如 1,2,5 → `第001章 ~ 第005章`）；**无章节 → 停止**（绝不由字数强切）；**所有分册拼接 == 原文**。
   - `audio.py`（←mp3-cue）：均分后**每段 ≤ 目标时长**（+1e-6 容差）且铺满 `[0,total]`；停顿对齐只移动**内部**边界、保持**严格单调**（棘轮防交叉）、**从不改变段数**（窗口无停顿则留在原均分位，计 fallback）；切割是**无损 `-c copy`** 流拷贝（不重编码；ffmpeg/ffprobe 从 `config.ffmpeg` 解析否则取 PATH）。
9. **解析内检查的不变量**（解析任务内三阶段，见阶段 3）：基文件 `03_parsed_json/<stem>.json` 是**唯一产物**，只由解析任务自己写出——检查阶段**永不改写**它（归属抽样的修正由解析任务随基文件一并写出）。断句失败校验 / 纯归属标签删除各有一个项目级开关（`generation.revalidate_splits` / `delete_saying_tags`，**默认开** = 现有行为不变）；关闭 = **整体跳过该阶段并留一行「…已关闭（配置）」日志**（防静默被误读为阶段缺失），对应结果字段（`suspicious` / `suspicious_fixed` / `tags_deleted`）保持存在、值为 0。归属抽样重判**恒用捆绑**重判提示词（`check_prompts` + `resources/default_check_prompts.txt`，**不再用户可配**）与共享重判批协议（`script.py` 模块内函数），几何取 `generation.check_batch_size` / `check_context_window`（**UI 不露出**）。重判协议：仅**严格多数**（≥2 票且唯一领先）且 ≠ 原值才改；无共识 / 解析失败 → 保留原值，**从不猜**；**只改 `speaker`** + 台词「仅去外层引号」的机械计算值严格采纳（NARRATOR 终值不剥）；角色名**禁止翻译/音译/本地化/改写**（提示词层强约束：必须逐字复制窗口/花名册内既有 speaker，中文角色名保持原汉字）。旧工作空间遗留的 `_checked.json` 是**惰性孤儿**：resolver 永不读取（自动选取 / `__all__` 只取基文件），文件列表可见、可下载、显式指名可直读，**绝不删除**（「绝不删除用户数据」约定）。
10. **合并不变量**：输入是 batch 写好的逐行 mp3（**不重解码**，pydub 拼接）；两阶段（每 100 段一批 part → 整书）的**批间间隙必须与单遍合并逐一样本一致**（`boundary_gap_ms`：段级 `pause_after` override 优先 > 同人 `same_ms` > 换人 `pause_ms`）；全链路只有一次 ffmpeg 编码（`-c:a libmp3lame`，刻意**不带 `-b:a`**，与 pydub 默认导出一致）；合并顺序恒等于解析 JSON 行序（按 `index` 重排）。

## 后端架构

### `backend/main.py`

`ROUTERS`（注册顺序）：tasks、config、files、text、book、audio、tts、script、workspace。CORS 全开放（仅面向本机回环客户端）。`GET /api/health`。SPA 兜底见「运行应用」。

### `backend/core/`

- **`paths.py`** — `Layout`（单根）：跟随工作空间根（根 `app.json` 的 `paths.working_dir` 指针），持有 8 个管线目录 + `logs/` + `config/`。`get_layout()`：未设置 → 惰性 `Layout(None)`（一切属性 None，`ensure`/`dirs` 空转）；已设置但文件夹已不在（工程被移动/删除）→ **同样保持惰性，不在旧位置重建空骨架**（由 `GET /api/workspace` 的 `exists: false` 与写端点 409 告知用户重选）。`resolve_parsed_json(script=None)`：解析下游该读哪个脚本 JSON——给定文件名则**原样直读**（显式基名与显式 `_checked` 名都不再做任何升级/变换），省略则取 mtime 最新的**基文件**（孤儿 `_checked` 永不遮蔽基文件），最后兜底遗留名 `annotated_script.json`。`resolve_parsed_json_all()`：「全部文件」（整书聚合）= 所有**基文件**按 `(mtime, name)` 排序（mtime ≈ 分册生成序 ≈ 阅读顺序，抗中文数字乱序）；孤儿 `_checked` 文件**永不读取**（列表可见、可下载、显式指名可直读、绝不删除）。哨兵 **`ALL_PARSED_JSON = "__all__"`**（前后端同一字面量；**只用于角色配音**，音频合成收到它直接 400）。
- **`pathio.py`** — 路径序列化/解析统一入口（详见核心约定 #3）。异常：`PathOutsideWorkspace`、`PathNotFoundError`（用户可读文案）。
- **`config.py`** — 持久化配置，**两个文件一个指针**：根 `app.json` = 通用配置模板 + 工作空间指针（`paths.working_dir` 是唯一允许在根级写入的字段，其余只读，作为新工作空间的种子）；`<workspace>/config/app.json` = 当前工程独立配置（设置工作空间时从根模板复制、**已存在则绝不覆盖**；此后所有读写只针对它，`update_config` 深合并 patch 后强制 `working_dir` 为该工作空间）。Pydantic `AppConfig`，线程安全（`RLock`）；根文件缺失由代码默认值自动种子。`get_config()` 每次读时把内存里的 `working_dir` 自愈为根指针当前值（工作空间被移动重选后 UI 不显示陈旧指针）；`reset_config_cache()` 在指针变化后调用。读取链：工作空间配置 → 根模板 → 纯代码默认（**读永不写**；文件损坏/缺失静默降级）。
- **`tasks.py`** — 任务系统（详见下文专节）。
- **`concurrency.py`** — 进程级 `ConcurrencyGate`（`threading.Condition` 而非 Semaphore，**上限可增可减**；恒 clamp ≥1，**没有"无限"模式**，保证 acquire/release 严格配平）。`set_concurrency(n)` 在每个解析批次开始时按 `config.generation.max_concurrency` 调。语义：**文件间并行、文件内 LLM 调用串行**，每文件全程持有 1 个槽（`gate().acquire()` 进 / `finally release()` 出）。
- **`logging_setup.py`** — 轮转文件 handler 跟随工作空间（`<workspace>/logs/app.log`，2MB×5 份），设置/清除工作空间时重定向；未设置 → 仅控制台；指针指向的文件夹已不在 → **降级控制台，不复活幽灵 `logs/` 树**。

### `backend/api/`（瘦路由 → engines）

| 模块 | 端点（全部前缀 `/api/...`） |
|---|---|
| `_common.py` | `require_workspace()`（写端点守卫：未设置 409；目录已不存在 409）、`resolve_inbound_path()`（400 系）、`read_decoded_file()`（自动编码探测，400 系）、`partial_copy()`（只应用已知字段，防客户端脏键） |
| `files.py` | `GET /files/list/{module}`（module = **磁盘目录名**如 `02_split_text`，7 个可列目录，`00_temp` 不可寻址）、`GET /files/download/{module}/{name}`（FileResponse，防穿越）、`POST /files/upload`（FormData → `01_input/`，返回绝对路径） |
| `workspace.py` | `GET /workspace`（`{set, path, exists, is_default, dirs}`）、`PUT /workspace`（path 空 = 清除并重新锁定；先 mkdir 校验再落指针 → 400 不会写坏指针） |
| `config.py` | `GET /config`（空 prompts 在**响应里**种子捆绑默认值，不落盘）、`PUT /config`（409 守卫；深合并 patch 持久化到工作空间配置） |
| `tasks.py` | `GET /tasks`、`GET /tasks/stream`（**多路复用 SSE：一条连接推所有任务**，每事件带 `task_id`，连接先发 `snapshot_all`；**必须注册在 `/{id}` 之前**）、`GET /tasks/{id}`、`POST /tasks/{id}/{action}`（cancel/pause/resume/retry）、`GET /tasks/{id}/stream`（单任务 SSE，保留） |
| `text.py` / `book.py` / `audio.py` / `tts.py` / `script.py` | 见下「流水线逻辑链」各阶段 |

### `backend/engines/`

| 文件 | 职责 / 入口 |
|---|---|
| `text.py` | 确定性排版。`format_text(text, cfg) → {text, stats}` |
| `book.py` | 分册。`decode_buffer`（编码探测，全后端共用）、`analyze_text`、`compute_volumes`、`volume_content`（单一字节切片）、`make_volume_filenames`、`check_chapter_sequence`、`build_zip` |
| `audio.py` | 分集。`probe_duration`、`detect_silences`（流式 stderr）、`parse_silence_log`、`build_plan`、`snap_boundaries`、`build_aligned_plan`、`cut_segments`（`-c copy`）、`output_name` |
| `script.py` | LLM→JSON 解析。`split_into_chunks`（章标题防丢守卫）、`check_chunk_fidelity`（逐 chunk 忠实性：引语段骨架比对）、`split_chunk_balanced`（近中点安全边界对半切）、`process_chunk`（重试/修复/抢救 + 忠实性恢复：翻倍 max_tokens → 一级对半切 两级阶梯）、`is_suspicious_entry_text` / `suspicious_entry_indices`（断句失败检测：外层双引号包裹 + 引号内「…道 + 冒号」，`rfind` 取最宽跨度）、`_strip_leading_saying_tag`（剥开头「…道：」纯标签——忠实性门的第二允许骨架）、`_parse_entries_reply`（重判回复的清理/修复/抢救阶梯）、`_reparse_vote`（重判忠实性门 → 投票值）、`revalidate_entry`（单条多者胜：1+2 次早停、无共识第 4 次）、`validate_sentence_splits`（解析后断句校验：窗口/花名册预建 + 降序应用）、`delete_pure_saying_tags` / `_is_pure_saying_tag`（纯归属标签条确定性清理：NARRATOR + 无引号 + ≤10 字 + 五动词收尾 + 非标题 + 紧邻对白 → 删除；非级联、零 LLM 成本）、`spot_check_speakers`（解析后归属抽样：两桶抽样〔1/3 纯随机仪表 + 2/3 风险级联〕+ 复用捆绑重判提示词（`check_prompts`）/共享重判批协议重判、内存修正随基文件写出；`rng` 可注入供测试）、`spot_budget`/`select_spot_targets`/`_risk_tier`/`_has_attribution_tag`/`_tag_in`（抽样纯函数：预算两桶 / 级联选样 / 特征计数 / 归属标签检测〔知道/难道 形态守卫〕）、`_load_spot_history`/`_append_spot_history`（`config/spot_check_history.json` 读数历史：模块锁读-改-写、保留最近 50、`write_bytes`）、`merge_adjacent_narrator`（相邻旁白合并 + 章标题守卫 + <100 字上限）、`generate_file`（Task worker，可选 `rng` 参数；解析内三阶段各带开关门：`revalidate_splits` 断句校验 / `delete_saying_tags` 标签删除 / `spot_check_rate` 归属抽样（0 = 关）——关 = 跳过 + 一行日志 + 结果字段 0）；`_llm_chat_completion(_stream)`（**全后端 LLM 传输的唯一实现**）；`clean_json_string`/`repair_json_array`/`salvage_json_entries`/`fix_mojibake`；**重判批协议**（自已退役的独立检查模块整体搬入，断句失败校验 / 归属抽样共用）：`build_roster`（全书角色花名册）、`build_batch_window`（±window 上下文窗口；可选 `skip` 参数 = 组内非目标条不标 target）、`strip_outer_quotes`（协议唯一允许的 text 编辑 = 去外层引号）、`parse_speaker`（单条回复多形态解析）、`parse_speaker_map_full`（批回复解析，另捕获可选 `text` 键）、`_pick_majority`（严格多数投票）、`group_retry_indices`（按 ≤batch 分组 + 间距 ≤window 的重试分组）、`_llm_call`（重判 LLM 调用封装 = 解析传输的薄包装） |
| `script_prompts.py` / `check_prompts.py` / `persona_prompts.py` | 捆绑默认提示词（`load_default_prompts` / `load_default_check_prompts` 在 **import 时**加载 `resources/*.txt`，分隔符不恰有一个 `---SEPARATOR---` 会 RuntimeError）；`check_prompts` = 解析内重判阶段的**唯一**捆绑默认（不可配置）；`PERSONA_SYSTEM_PROMPT`/`PERSONA_USER_PROMPT` |
| `voices.py` | 角色配音两阶段。`prepare_foundations`（阶段 1，纯 LLM）、`make_clones`（阶段 2，纯 TTS，(角色,k) 候选作业池 → 单一长驻 design-batch 子进程 + 看门狗缩批/隔离重启）、`_fold_aliases`、纯函数族（`extract_json_object`/`_select_target_bands`/`pick_ref_text`/`_sanitize`/`auto_candidate_count`（自动备选数对数分档）/`effective_candidates`/`_clone_have`/`_effective_line_counts`（别名台词归并）/`_canonical_of`…） |
| `tts.py` | TTS 族**基础模块**（不再是"单条合成"）：`resolve_engine()`（`.venv-tts` 解释器 + worker 脚本；env 覆盖 `AUDIOTTS_PYTHON`/`AUDIOTTS_WORKER` 供测试桩用）、`run_worker()`（一次性子进程编排器：pump 线程、`[progress]`/其余行分流、协作取消/暂停、`log_file` 镜像、进程树 kill、temp 清理）、`WorkerWatchdogTimeout` |
| `tts_batch.py` | 批量合成编排。`synthesize`（单文件入口，同签名薄封装 → `_synthesize_one`）、`synthesize_multi`（多文件 Task worker：逐文件顺序 + 单文件错误隔离 + `_ScaledHandle` 进度窗口）、`_synthesize_one`（两种运行形态共享的每文件主体，含看门狗缩批/隔离重启循环）、`_build_cmd`、`_build_segments`、`package_for`、`load_manifest`/`build_manifest`（增量写）、`is_done`、`plan_to_synthesize`、`count_completion`、`clamp_concurrency`（[1,64]） |
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

### 阶段 3 · 文本解析（ScriptParse.vue `/script`）

**解析** `POST /api/script/generate-files {files: [02_split_text 裸文件名]}` → **每文件一个独立 Task**（module `script`），`set_concurrency(generation.max_concurrency)` 共享闸门 → 响应 `{task_ids, files:[{file, task_id}]}`。
`script.generate_file`（Task worker）链：`llm.model_name` 空 → **占槽前**快速失败 → `gate().acquire()` →（解析内三阶段的开关读自 `generation`：`revalidate_splits` / `delete_saying_tags` / `spot_check_rate`——任一关闭 = 整体跳过该阶段 + 一行「…已关闭（配置）」日志，结果字段保持 0）→ 读文件 `decode_buffer` → `fix_mojibake`（CP1252-as-UTF8 乱码替换表）→ `split_into_chunks(chunk_size=3000)`（按 `\n\s*\n` 分段落、小段打包、超长段按 `(?<=[.!?])\s+` 分句；切分无损；**章标题防丢**：标题行落在即将关闭的 chunk 尾部 100 字内 → 边界移到标题行之前，标题放到下一 chunk 开头（与它统领的章节正文同段，不孤悬在失败/截断 chunk 的尾部；独占 chunk 的标题独立成块）→ 逐 chunk `process_chunk`：
- context = 段位置标记（`Beginning of text` / `Part n of m` / `End of text`）+ 跨 chunk 角色名册（sorted 去重、剔除 NARRATOR）+ 上段末 3 条；user 模板 `{context}`/`{chunk}` 占位（`str.format`）；
- LLM = `urllib` OpenAI 兼容调用（`llm.stream` 决定流式与否；流式走双缓冲：`reasoning_content`+`content` 进 UI「流式反馈」，返回值只含 `content` —— 与非流式**字节级一致**；合流节流 0.12s/256 字符 → `handle.llm_chunk` + `handle.llm_rate`；**每 SSE 帧后查取消**）；
- 最多 3 次尝试（`max_retries=2`）：调用异常 / JSON 不可解析 → 重试；`clean_json_string`（剥围栏/思维标签/括号计数截取/控制字符转义）→ `repair_json_array`（补逗号/去尾逗号/丢非对象条目）→ 失败再 `salvage_json_entries` 正则抢救；**`TaskCancelled` 永不重试直接上抛**；
- **逐 chunk 忠实性校验 + 恢复**（拿到可解析响应之后）：`check_chunk_fidelity` —— 源 chunk 中所有 ≥4 词字符的引语段的**骨架**（只留词字符，去空白/标点/引号）必须能在输出条目 text 的拼接骨架中作子串找到：对提示词允许的引号剥离/标签并入等编辑天然免疫误报，而整块截断（JSON 尾部被截）与零星丢行都必然被抓住；任一缺失 → **阶梯第 1 步：把 max_tokens 临时翻倍再跑一次**（截断是最常见根因），通过则采纳（仍缺失但内容更全则以翻倍结果为准）；仍缺失 → **阶梯第 2 步：`split_chunk_balanced`（段落边界 > 换行 > 句末标点，尽量近中点、绝不拦腰切断）把 chunk 对半切开，两半各再跑一次**（右半继承左半条目作前文上下文；**半段不再升级——阶梯到此为止，无递归切分**）——阶梯恒为这两级，成本有界、不循环失控；不可切分 / 仍救不回 → 保留最好结果 + WARNING，**绝不静默丢弃**；
- 单 chunk 全败 → 返回 `[]`（**不抛异常**，仅日志）；**所有** chunk 皆空才文件级 `RuntimeError("未生成任何脚本条目。")`。
**断句失败校验**（`if not all_entries` 抛出之后、机械合并之前——拆出的旁白段随后照常合并；受 `generation.revalidate_splits` 门控，默认开，关闭 = 跳过 + 日志 + 结果字段 0）：条目 text 被外层双引号（弯/直任意形式）整体包裹、且引号跨度内出现「…道 + **冒号**」语气标签 → 疑似断句失败（标签被包进台词，下游 TTS 会照原样念出、再无环节纠正，只有解析阶段能就地重推）→ 逐条**重跑解析 LLM 做校验**：条目 text 进解析提示词的 `{chunk}` 槽、`build_batch_window` ±`generation.check_context_window` 条的上下文窗口进 `{context}`（user 提示注明 re-check + 注入全书角色花名册）——**复用解析提示词，无新提示词文件**；每条按**共享重判批协议的多者胜规则**裁决（基础 1 次 + 重试 2 次 = 合计 3 次，严格多数一出立即停（2:0 / 2:1），3 次无共识才再跑第 4 次）；每个回复先过**忠实性门**才计票——各段词字符骨架拼接 ∈ {原文骨架, 剥掉**开头**「…道：」标签后的骨架}（对提示词允许的引号/冒号编辑天然免疫，增/删/乱序词字符必被抓）/ 各段 speaker ∈ `NARRATOR` ∪ 全书花名册 / 多段需 ≥2 个不同 speaker——调用失败 / 响应不可解析 / 未过门均**无票**；严格多数胜出 → 条目被**整体替换**为重新推导的条目（1 段胜出 = 干净重写（剥外层包裹 / 开头标签），多段 = 重新拆分），4 次仍无共识 → 条目保持原样（**从不猜**）。花名册与全部上下文窗口按**原始**条目列表预建、胜出者按**降序下标**应用（拆分不会移动尚未处理条目的窗口下标）；取消立即上抛（文件在此步返回后才写）。LLM 调用复用解析传输（`_llm_call` = `script.py` 模块内的重判 LLM 调用封装，流式照常进 UI 面板）；**0 条命中 → 打一行日志「断句校验：0 条疑似断句失败条目（无重判，零 LLM 调用）」后直接返回**（静默快速路径会让用户误以为阶段缺失，故留痕）。
**纯归属标签条清理**（断句校验之后、归属抽样之前——抽样重判可能把标签条改判成角色 speaker，使本规则不再适用，且角色会用本人声音念第三人称标签；确定性、**零 LLM 成本**；受 `generation.delete_saying_tags` 门控，默认开，关闭 = 跳过 + 日志 + 结果字段 0）：整条内容就是纯归属标签的条目（老道瞪眼怒道。/ 杜尘暗喜，急道。/ 史蒂夫解释道。——无引号、无冒号，断句校验的机械触发器不可见；若保留会成 TTS 的独立旁白行，多一次同人停顿 + 换人停顿，悬在它引入的台词前/后）→ **删除**。删除 = 六条件合取：① `NARRATOR` ② 无引号（模块内 10 字符引号表 `_QUOTE_CHARS`，随重判批协议搬入）③ `len(text.strip()) ≤ 10`（`PURE_SAY_TAG_MAX_LEN`；阈值 = 纯标签与「叙述 + 标签」混合条的分界）④ 剥掉末尾标点后末字 ∈ 五归属动词（与归属抽样同一 `_SAY_VERBS`；「道」前一字符 ∈ 知/难 → 非标签，与归属抽样同一形态守卫）⑤ 非章标题（与机械合并同一 `is_chapter_title`）⑥ **紧邻**（前或后一条）对白条目（非空 speaker ≠ `NARRATOR`）。**非级联**单遍（邻接按删除前列表判定：隔一条的标签保留——代码无法区分纯标签与恰好以动词收尾的短叙述句，保守留）；删除**不会**制造新的 NARRATOR/NARRATOR 相邻对（邻接条件保证一侧是台词条）→ 下游机械合并不受影响。命中 >0 任务日志一行（含 ≤3 个被删例），0 命中也留一行（与断句校验同——静默退出会被误读成阶段缺失）；结果新增 `tags_deleted` 字段。
**归属抽样**（断句校验之后、机械合并之前——重判可把 NARRATOR 条改成角色条，合并必须看到改后 speaker；`generation.spot_check_rate` 默认 0.05，0 = 关闭）：所有 chunk 解析完后，从**全部**条目按两桶**不相交**抽样重判 speaker——总预算 `max(1, round(rate·N))` 封顶 N，其中 ~1/3 **纯随机**（全条目均匀，= 整书错误率的无偏「仪表」，唯一可据以判断「采样率能不能降」的读数）+ ~2/3 **风险加权**（按命中风险特征数 3→2→1→0 **级联**抽取、tier 内均匀随机，tier 0 = 未抽中剩余、预算恒用满）。风险特征（代码可判）：① 无显式归属标签——条目自身及紧邻前一条（仅当其为 NARRATOR）无「2~3 字 CJK 人名 + 说/道/问/喊/答」或「他/她 + 五动词」（`道` 前一字符为 知/难 = 知道/难道 的**形态守卫**）；② 极短 `len(text.strip()) ≤ 10`；③ 多角色场景——±10 条窗口内 ≥4 个不同非 NARRATOR 说话人。重判**复用捆绑重判提示词与共享批协议**（`check_prompts` 捆绑默认，**不再用户可配**；几何取 `generation.check_batch_size` / `check_context_window`；目标按 `group_retry_indices` 分组 = 每次调用一个窗口、组内非目标条作未标记上下文；首判 → 分歧条目动态多数投票 [原值, 首判] + 至多 3 次同窗口重试、严格多数一出即停、无共识保留原值；**只改 `speaker`** + 台词「仅去外层引号」的机械值严格采纳，NARRATOR 终值不剥）——**无新提示词文件**；高置信改判在内存中生效、随解析任务自己的基文件写出（不违反不变量 #9——基文件本就是解析任务自己的产物）；**取消上抛、不落盘任何文件**（基文件与历史都只在阶段整体返回后写）。每本的**纯随机桶**首判分歧率 = 错误读数 → 任务日志一行 + 基文件落盘成功后追加 `<workspace>/config/spot_check_history.json`（`{"runs":[{file, ts, rate, random:{n,errors,rate}, risk:{n,errors}}]}`，模块锁读-改-写、保留最近 50 本、`write_bytes` 全量重写、写失败仅 WARNING）——**采样率永不自动降**，由用户在设置页按读数手动调（风险桶读数天然偏高，**不用于**判断能否降率）。
chunk 循环后**机械后处理** `merge_adjacent_narrator`（确定性，不经 LLM）：连续 NARRATOR 条目合并为一条（text = 直接拼接、instruct 取首条；链式合并；输入不动）——去掉 TTS 会在相邻旁白间多插的同人停顿与段边界；**章标题守卫**：标题行前后两侧一律不合并（章节边界的停顿正是可听的章节分界；合并出的条目结构上不可能构成新标题，故链式合并免再判已合并侧）；合并后长度 <100 字（缺省上限；100 字行的 TTS 超时预算已相当宽裕，紧上限让合并行保持短小——单行失败代价小、节奏更细，也杜绝无界链式合并）。
产物：`03_parsed_json/<源文件 stem>.json`（一源一文件）= JSON 数组 `[{speaker, text, instruct}]`——`speaker` = 全角大写角色名或 `NARRATOR`（非对话）；`text` = TTS 实际朗读文本；`instruct` = 1–2 句声音指导。返回 `{entries, output_path, count, merged_narrator, suspicious, suspicious_fixed, tags_deleted, speakers, input_chars, spot_checked, spot_fixed, spot_rate, spot_random_n, spot_random_errors, spot_random_rate}`（`suspicious` = 检出疑似断句失败条数、`suspicious_fixed` = 经投票改写条数、`tags_deleted` = 确定性删除的独立纯归属标签条数——对应开关关闭时保持 0；`spot_*` = 归属抽样的抽查数 / 更正数 / 采样率 / 纯随机桶 n / 错误数 / 错误率（空桶 → null））。

遥测（`TaskHandle` → SSE）：`llm_chunk`（流式原文，Task 侧 128KB 字符上限、丢最旧，重连可回放）；`llm_rate`（`cps` 瞬时 + `cps10` = 后端按真实流式字符算的 **10 秒窗均值**（`RATE_WINDOW=10.0`，`(c末-c首)/(t末-t首)`，无偏）→ 吞吐量卡）；`llm_chars`/`llm_secs`（每完成一个 chunk 上报累计原文字数 + 冻结处理时长 → 处理速度表，chunk 之间不衰减）。
测试：`test_script.py`（切分/修复/流式字节一致/取消穿透/解析内三阶段开关门与 e2e（含关闭语义：零 LLM 调用 + 「已关闭（配置）」日志 + 结果字段 0）/**重判批协议**（自已退役的独立检查模块整体搬入：`build_batch_window` 窗口/边界 clamp/`skip` 语义、`parse_speaker` 多形态、`parse_speaker_map_full` text 捕获、`strip_outer_quotes` 6 种引号对、`_pick_majority` 严格多数、`group_retry_indices` 间距 + 批上限））、`test_llm_rate_window.py`。

### 阶段 4 · 角色配音（Voices.vue `/voices`）

**两阶段、互斥**（前端禁用另一阶段按钮——LLM 与 TTS 不抢显存）；无独立创建/删除端点，档案由两阶段 Task 整体重写。

**阶段 1（纯 LLM）** `POST /api/tts/prepare-foundations {speakers?, new_only, overrides?, script?}` → `voices.prepare_foundations`（module `voices-foundation`）：`_load_script`（`__all__` → `resolve_parsed_json_all()` **直读基文件**逐文件拼接，单文件损坏仅 WARNING 跳过）→ 证据采样（`_select_target_bands`：≤24 句全进 front；否则 front 8 + back 8 + 中段均匀 8，每条带 `±4` 邻句窗口、目标行标 `★`）→ `ThreadPoolExecutor(max_concurrency)` 并行 `_llm_persona`（复用 LLM 传输；`temperature=0.3`、`max_tokens=1024`；提示词 `config.persona_prompts.*` 空则回退 `persona_prompts.py` 内置；`str.replace` 填 `{speaker}`/`{line_windows}` 防大括号炸裂）→ `extract_json_object`（手写括号配对扫描）→ `{description, ref_text(40-60字)}` → 全败 → `_fallback_persona`（固定描述 + `pick_ref_text`）→ `_fold_aliases`（归一化精确 → 子串 → Jaccard≥阈值，写 `alias_of`）→ **每完成一个角色就整体重写** `voice_config.json`（页面实时刷新）。本阶段**不启动任何 TTS 子进程**。

**阶段 2（纯 TTS）** `POST /api/tts/make-clones {speakers?, new_only, concurrency?, script?, candidate_count?}` → `voices.make_clones`（module `voices-clone`）：选有 foundation 且非 alias 的角色 → 每角色目标候选数 = 固定 `candidate_count`（None=自动 / 2 / 4 / 6 / 8，请求校验）或 `auto_candidate_count(有效台词数)`（**绝对对数分档，无工程相对分母**——旁白可达主角 10 倍量级，相对比例必把主角压成 2~3 条；改按角色**自身**台词量级分档：`<20` 龙套=1，`20+`≥2，≈100→3、≈200→4、≈500→5、≈1000→6、≈2000→7、≈2300+→8，即 `int(-2.1 + 3·log10(台词数))` 钳 1..8）（**别名台词归并进 canonical**（`_effective_line_counts`，沿 `alias_of` 链 ≤8 跳防环）——`speakers`/`new_only` 过滤不移位，单角色重做与整批同预算）→ `new_only` 只重做「已有候选数 < 目标」的角色 → **每 (角色, k) 一个作业**（`jobs` 池；产物路径 `04_voice_profiles/designed_voices/<_sanitize(speaker)>_<ns>_c{k}.wav` 运行前定死——每角色一个 `time.time_ns()` ns 防 `_sanitize` 文件名碰撞，**跨重启稳定**）→ **断点采纳**：盘上已有候选（WAV ≥1024B，坏头守卫）直接采纳（seed 记 −1，不重渲染）；**全部已采纳 → 零作业短路**（结算 + 保存 + progress 1.0，不启动引擎）→ `base_seed = secrets.randbelow(2**31)` **整个运行只算一次**（跨重启稳定）→ `rows_cap = clamp_concurrency(concurrency or config.tts.batch_concurrency)`（缺省 4）→ **一个长驻 worker 子进程**（`run_worker` + `--mode design-batch` + `--disabled-checks`（五道规划检查与音频合成共用 `config.tts.planner_*` 开关，全开时省略该参数，见阶段 5）；模型只加载一次；候选行**按角色**组 tensor 子批（角色按候选数降序处理、角色内行按长度升序）+ `VramGovernor`——长度分档 + 总字符上限 + 实测显存反馈，逐行 instruct；`base_seed + 子批序号` 播种，**同子批候选共享 seed 但仍互异**（各行独立采样），重跑互异靠 run 级 `base_seed`）→ 作业清单写 `00_temp/design_jobs_*.json`（绝对路径，用完删）→ `remaining` = 未排除、未 ok **且属未结算角色**的作业（看门狗重渲染不会在已结算 entry 下留孤儿文件；未结算角色的失败作业重启时重做）→ **角色级原子结算**：某角色全部候选作业落定才整体写一次 entry（在途角色保持运行前状态；**取消时未结算角色文件不动**）→ 成功（≥1 候选，按 k 排序重编号 1..m）：entry = `{type: clone, ref_audio(工作空间相对), candidates: [{id, ref_audio, seed}], selected_audio_id: None, ref_text, description, character_style, clone_status: done}`；全失败：`{type: design, candidates: [], selected_audio_id: None, clone_status: failed}` 但**顶层 `ref_audio` 保留**（最后已知良好的音频仍可试听；**单角色失败不中断整批**）。**不变量：顶层 `ref_audio` ≡ 当前生效候选**（已选 = 选中项，未选 = 第 1 条）——worker batch 只读顶层 `ref_audio`/`ref_text`，状态推断与路径迁移也都只认它 → 保持同步则 worker / tts_batch / merge / pathio **零改动**。**看门狗重启循环**（退出码 124，与音频合成同形）：`rows_cap>1` → 缩批一半重启；`rows_cap==1` → 对 `[watchdog]` 行里的 (角色,k) 行记罚，**两次超时 → 隔离**（不再渲染，记 `超时（已隔离）`，其余继续）；`attempt>8` → RuntimeError（已完成进度已保住，提示调小「批内行数」）。进度由 worker 的 `[progress]` 行驱动（`(offset+done)/total`，`--done-offset` = 已落定作业数 → **重启后不回跳**；后端只在终末发「完成」）。取消：`run_worker` 主循环每 0.15s `handle.check()` + finally 杀**进程树**（Windows `taskkill /F /T`），已生成 WAV 留盘成孤儿（与旧实现一致）。排障：全量 stdout/stderr 镜像 `workspace/logs/tts_clone_<ts>.log`（每 attempt 一个 `=== attempt ===` 段）。`concurrency` 缺省 `tts.batch_concurrency=4`（前端读作 Voices 页「批内行数（上限）」初值后作为 `req.concurrency` 传入）。

**只读** `GET /api/tts/voices?script=`（409 守卫豁免，无工作空间降级空）：读脚本（`__all__` 折叠整书：按 speaker 名去重、累加台词数、保首见顺序）+ `voice_config.json`（`migrate_entries_in` 惰性迁移 `ref_audio`）→ 每角色 `{name, line_count, status(ready|pending), foundation_status, clone_status, type, alias_of, description, preview, candidates: [{id, preview, seed}], selected_audio_id}`。列表按 `line_count` **降序**（稳定——同数保持首见顺序；无脚本时计数全 0 = voice_config 键序不变），主角恒在列表顶部。`ready` = 有 `alias_of` 或 `_voice_usable`（clone 需 `ref_audio`；design 需非空 `description`；custom 恒真）。`preview` 是**相对 `04_voice_profiles/`** 的路径，前端经 `GET /api/files/download/04_voice_profiles/{name}` 播放（`MiniAudioPlayer`）；`candidates` 经 `V.effective_candidates` 派生（旧格式单候选合成 1 项、无克隆 `[]`），`selected_audio_id` = 用户选择（`null` = 默认第 1 条）。
`PUT /api/tts/voices/select {speaker, audio_id?}`（**同步写，非任务**）：记录某角色的克隆候选选择——`selected_audio_id` 与**顶层 `ref_audio`（生效参考）一次文件重写同步更新**，后续音频合成恒用选中条；`audio_id` 空/None = 清除选择回默认第 1 条。守卫链：工作空间 409 → **voices-foundation/voices-clone 任务在途 409**（两阶段都整体重写 `voice_config.json`，防并发写互踩）→ 文件/角色缺失 404 → 无候选或 `audio_id` 非法 400。试听走既有 `GET /api/files/download/04_voice_profiles/{name}`（零新路由）。前端「选择音色」弹层（页内 `fixed inset-0 z-50` overlay）：候选行 = radio + `MiniAudioPlayer`（`useAudioBus` 全局单播）+ 生效项「当前」Badge；候选 <2 的角色（自动模式龙套、旧格式单候选）按钮禁用。
`voice_config.json` 条目字段：`type`（foundation/clone/design/custom）、`description`、`ref_text`、`ref_audio`（工作空间相对，**≡ 生效候选**）、`candidates`（`[{id, ref_audio, seed}]`，写入即工作空间相对）、`selected_audio_id`（`null` = 默认第 1 条）、`alias_of`、`foundation_status`/`clone_status`（done|failed）、`character_style`。
测试：`test_voices.py`（JSON 抽取/别名解析/采样带/状态推断/**自动备选数分档/候选簿记/别名台词归并**/**make_clones 全 e2e**（fake-worker 真子进程经真 `run_worker`：固定/自动计数、部分失败重编号、全失败保留旧 ref_audio、取消角色级原子、new_only 差额、**进度单调（worker 驱动、重启不回跳）**、看门狗缩批重启（二轮 `--concurrency` 减半）、size-1 两次超时隔离毒行、8 次上限 RuntimeError、断点采纳零作业短路（seed=−1、不启动引擎）、批内多行共享 seed（候选仍互异））/select 端点（选择/清除/400/404/409 工作空间/409 任务在途）/list_voices（候选字段 + 列表台词数降序））。完成后 `project.recordVoices` → 「前往音频合成」。

### 阶段 5 · 音频合成（BatchTTS.vue `/batch`）

`POST /api/tts/batch {indices?, script?, scripts?, concurrency?, seed?}` → module `tts-batch`；**`script`/`scripts` 含 `"__all__"` → 400**（"全部"只用于角色配音）；`indices` 且多文件 → 400。分发：0/1 文件 → `tts_batch.synthesize`（旧单文件路径，行为逐字节不变）；**>1 文件 → `tts_batch.synthesize_multi`**（一个任务内逐文件顺序合成）。`POST /api/tts/batch-reset {scripts}`（**同步写，非任务**——「重新全部合成」第一步）：删除选中文件的 `05_audio_chunk/<包>/`（mp3 + manifest；`package_for` 与合成包名同一函数——`_checked` 名映射到同一包；包名 = 文件名 stem，构造上不可能越出目录）→ 前端随后发**与一键合成完全相同**的请求（默认 resume：删后全段未完成 → 全部重做）；旧的 `force_all` 分支（请求字段 / `plan_to_synthesize` 全量分支 / 独立任务标签）已移除。守卫链：工作空间 409 → `__all__`/空列表 400 → **tts-batch 任务在途 409**（引擎正在写包目录）。`GET /api/tts/batch-status?script=` → `{total, completed, remaining}`（单文件 / 缺省最近）；`GET /api/tts/batch-status?scripts=…`（重复参数，多文件）→ `{files: [{name, total, completed, remaining, complete, speakers, ready, missing}]}`（按请求顺序；`complete` = total>0 且 completed==total = 行的【已合成】；ready/missing 与角色配音页同一规则（`alias_of` 或 `_voice_usable`）；completed = manifest 里 `ok` 且**文件仍在磁盘**的段；增量写 manifest 所以运行中刷新真实）。
`_synthesize_one` 链（`synthesize` / `synthesize_multi` 共享的每文件主体）：`resolve_parsed_json` + `_load_script`（缺/坏/空 → RuntimeError）→ 读 `voice_config.json`（**缺失仅 WARNING**；先 `migrate_entries_in` 迁移再 spawn，worker 按 `--workspace` 解析相对 `ref_audio`）→ `out_dir = 05_audio_chunk/<包名>`（`package_for` = 源 JSON stem 剥 `_checked`，兜底 `"batch"`）→ `_build_segments`（`index` = 全脚本行位置；`speaker` 取 `entry.speaker or entry.type`；跳空文本；带 `instruct`/`pause_after`）→ `load_manifest` + `plan_to_synthesize`（显式 indices 交集 > 默认 **resume** 只合未完成的；「重新全部合成」= 先 `batch-reset` 删包目录再走本路径，无独立分支）→ **零段短路**（重写完整 manifest、progress 1.0、**不启动引擎**省模型加载）→ 段表写 `00_temp/batch_segments_*.json`（绝对路径，用完删）→ `run_worker`（`--mode batch`，`--concurrency` 钳 [1,64] 缺省 `config.tts.batch_concurrency=4`，`--seed` 缺省 `batch_seed=-1` 随机，`--workspace`，三个模型 id，`--ffmpeg`，`--disabled-checks`（关闭的规划检查逗号清单，全开省略））→ **看门狗重启循环**：`WorkerWatchdogTimeout`（退出码 124）→ workers>1 则 `workers//2` 缩批重启；workers==1 则对 `[watchdog]` 行解析出的段记罚（**两次超时 → 隔离**：`excluded` + manifest `ok:false reason="超时（已隔离）"`）；累计 8 次未愈 → RuntimeError（已完成进度已保住）→ 收尾：最终 manifest、统计；**`completed==0` → RuntimeError**（任务 FAILED，"本次 N 段全部合成失败"）。
`synthesize_multi` 链（多文件，待合成卡的多选）：逐文件循环：`handle.check()` **最先**（文件间取消/暂停点；`TaskCancelled` **先于**通用异常捕获直接上抛——取消是任务级结局，绝不变成「该文件失败，继续跑」，否则任务会以 SUCCEEDED 结束，违反任务隔离不变量 #7）→ `_ScaledHandle` 代理把本文件进度映射进窗口 `[i/N, (i+1)/N]`（总体单调不回跳；步骤标签带**文件名前缀**防上一文件的「完成」滞留）→ 日志 `文件 i/N：<name>` → `_synthesize_one`（上面整条链；每文件一个一次性 `.venv-tts` 子进程、模型逐文件加载一次；**全部完成的文件走零段短路 → 根本不启动引擎**）→ 通用异常 → ERROR 日志 + 该文件零值条目 `error: str(exc)`（**单文件失败隔离**，其余文件继续——与角色配音「单角色失败不中断整批」同原则）→ 聚合结果 `{total: Σ, completed: Σ, failed: 各文件失败段展平并加 `script` 字段, files: [{script, total, completed, failed: 计数, output_dir, manifest_path, done_count, all_count, error: str|null}]}`（`files` 顺序 = 请求顺序）；失败判定：`completed==0` 且**没有任何文件** `total>0 或 error` → RuntimeError（任务 FAILED）——全空脚本 / 全部已完成 = 正常成功（每文件的零段短路语义）。代价：每文件一个子进程、模型逐文件加载一次（已完成文件零代价；前端卡片描述与页面文案注明）。
worker（`tts-engine/tts_worker.py --mode batch`）：按段所需 type **只加载用到的模型**（custom→`model` / clone→`base_model` / design→`design_model`；`Qwen3TTSModel.from_pretrained`，优先 HF 本地缓存；cuda→bf16+device_map；import 噪音吞进 devnull）；无配置的角色 → 段级 error「缺少角色声音配置（X）——请先在「角色声音」页生成」（**不中断整批**）；**按（type, 角色）分组**：**台词数降序处理**（台词最多的角色先跑，平手按首见序；角色内按字数升序；一个角色的行绝不与另一角色混批——解码上限随批内最长行，长短行混批会让短行按长行的上限跑）；clone 组每角色建一次 `voice_clone_prompt`；**惰性子批规划**每轮 `plan_next_sub_batch`（`plan_row_tokens` = 字符×1.2+overhead 估 token；`VramGovernor` 纯算术反馈环按实测显存压力**减半**/宽松**增长** `vram_scale` 重标定；`LENGTH_BANDS` 长度分档 cap；单批总字符 ≤ `--max-batch-chars`(12000)；超长行独批；长度比 ≤3 防混批（自 5 收紧——解码上限随批内最长行，混批短行会跑长行全程）；以上五道检查（分档/总字符/超长独批/长度比/显存静态估算）可经 `config.tts.planner_*` 逐项关闭（**设置页开关**，默认全开 = 行为不变；关闭项经共用 `--disabled-checks` 逗号清单传给 worker〔全开省略〕，各留一行「…已关闭（配置）」运行日志；实测显存的 VramGovernor 不受开关影响、恒生效）；**每行必入恰好一个批**）；**解码上限随批内最长行缩放** `min(2048, max(128, 字数×6))`（批量模式模型不按行提前停 EOS，固定 2048 上限会让短行批跑满全程——2026-09-17 一键合成卡死的根因）；子批超时预算 `sub_batch_timeout_seconds`（GPU〔cuda/mps〕= `chars/15` 秒〔实测 ~15 字/秒，clone/custom/design 同一率〕，floor 30s / cap 3600s；cpu `600+4·chars`∈[600,10800]）+ 心跳（10s 起每 20s）→ 超时先 flush `[watchdog]` 行再 `os._exit(124)`（进程+CUDA 上下文同死）；非超时故障 → size-1 也崩则 `os._exit(124)` 交后端隔离，否则 `observe_fault` 缩容 + 清显存 + **对半递归重试**；`seed>=0` 时每子批 `torch.manual_seed(seed + 子批序号)`（可复现）。
产物：`05_audio_chunk/<包>/<index+1 零填充>.mp3`（soundfile WAV → pydub/libmp3lame；<1024B 视为坏头删）+ **`manifest.json`**（list `[{index, speaker, text, pause_after, path(工作空间相对), ok, reason}]`，**每见一条 `[segment]` 行更新内存态，整份重写落盘节流至最多每 2 秒一次**（`MANIFEST_FLUSH_INTERVAL`；首行必落盘，取消 / 引擎失败 / 看门狗重启 / 收尾强制落盘）——取消不丢已完成工作，后端被强杀最多丢 ~2s 进度（其 mp3 留盘，续合只补 manifest 缺的段）；加载时惰性迁移旧绝对 path）。
worker 退出码：`0` = 成功（**个别段失败也 0**，段级失败走 `[segment] i error <reason>` 行）/ `1` = 通用失败 / `2` = setup 错误（stderr `TTS_WORKER_ERROR:`）/ `124` = 看门狗。
排障：全量 stdout/stderr 镜像 `workspace/logs/tts_batch_<ts>.log`（`[out]`/`[err]` + `=== attempt started/ended rc=N ===`）——任务日志是 SSE-only 的，此文件是失败运行的磁盘证据。
测试：`test_tts_batch.py`（钳制/看门狗缩批/隔离/resume/增量 manifest/相对路径/零启动/**多文件**（`synthesize_multi`：已完成文件短路不启动引擎、全完成零子进程、零段文件成功、失败段带 `script` 标签、进度按文件窗口单调、单文件致命错误隔离、全文件失败 → RuntimeError、文件间取消 → `TaskCancelled` 穿透且已完成文件 manifest 保留）/**batch-status 多文件**（计数/complete/ready/missing、缺失名零字典、`__all__` 400、无工作空间降级）/**run_batch 分发**（多 → `synthesize_multi` + 「N 个文件」标签、单 → 旧路径逐字节不变、`indices`+多 → 400））、`test_tts_worker.py`（规划/显存/governor/超时预算）。

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
**SSE 协议**：前端只用**多路复用端点** `GET /api/tasks/stream`——**每标签页恰好一条连接**推所有任务（浏览器对单源的 HTTP/1.1 并发连接上限 ≈6，本控制台常开多个窗口/标签页，旧的「每任务一条 EventSource」在几个并发解析后耗尽连接上限：超限的 EventSource 永不连上，对应窗口一条日志都不显示而后台正常跑）。连接时**先取快照再订阅**（回放与直播不重叠）→ 先发 `snapshot_all{tasks:[快照…]}`（全部任务，重连即全量自愈）→ 此后**永不自行结束**（任务随时会新建），每事件带 `task_id`，15s 无事件发 `ping` 保活。`TaskManager` 持有进程级总线（`subscribe_all` 收 `(task, event)` 元组；`Task._broadcast` 钩子在 `Task._emit` 里把每个事件同时转发给总线；总线队列 maxsize 3000，**满时只丢纯展示事件** `llm_chunk`/`llm_rate`（快照/final 回放自愈），关键事件（`log`/`progress`/`status`/`final`）先驱逐最旧展示事件再入队）。事件 `type`：`snapshot_all` / `snapshot`（仅单任务端点）/ `progress{progress,current}` / `log{level,msg,t}`（时序追加，新行在下）/ `llm_chunk{data}` / `llm_rate{cps,cps10}` / `llm_chars{chars,secs}` / `status{status[,task]}`（**终态事件带完整快照**——客户端据此原子读取 result/error）/ `final{task}` / `ping`——多路复用流里一律额外带 `task_id`。前端 `task` store 用原生 `EventSource` 连复用端点（裸 `data:` 行统一 `onmessage`），`applyEvent(task_id, e)` 按 type 分发（`snapshot_all` → 整表替换）；**流仅在「至少一个任务非终态」时保持**（最后一个任务终态事件即关流，空闲标签页不占连接；`refresh()`/`control()`/建 store 在有活动任务时重开）；断流 → `refresh()` 对齐列表并按需重开。**刷新恢复（F5）**：任务内存常驻（进程生命周期内），运行中任务的快照含完整 `progress/current/logs/llm_stream`——刷新后 store 的 `refresh()` 全量对齐并按需重开流，五个发起任务的页面在 `onMounted`（`await taskStore.refresh()` 后）用 `activeTasks(module)` 把**非终态**任务按 module/label 重新挂回本地 id 引用（`fileJobs`/`taskId`/`busy` 等），进度条 / 日志 / 流式反馈 / 取消钮 / 完成 watcher 随之恢复；终态任务不恢复（磁盘产物 + 页面既有徽章已表达完成态）；label 格式依赖见「已知陷阱」。单任务端点 `GET /api/tasks/{id}/stream` 保留（快照 `snapshot` 回放、`final` 即止，供一次性消费者）。
控制：`POST /api/tasks/{id}/{cancel|pause|resume|retry}`（404 任务不存在；400 未知 action）。任务内存常驻（进程生命周期内），`GET /api/tasks` 全量快照。
当前使用任务的阶段：文本解析（每文件一任务，内含断句校验/标签删除/归属抽样三阶段）、角色配音两阶段、音频合成、音频合并、音频停顿检测/切割。

### 横切 · TTS 子进程编排（`engines/tts.py`）

`resolve_engine()` → `(python, worker)`：`PROJECT_ROOT/.venv-tts/{Scripts/python.exe | bin/python}` + `tts-engine/tts_worker.py`（env 覆盖 `AUDIOTTS_PYTHON`/`AUDIOTTS_WORKER`，测试桩用）；缺失 → 可操作的 RuntimeError（"请先运行 install_tts_env.ps1…"）。
`run_worker(cmd, handle, on_line, *, temp_files, fail_prefix, watchdog_code, log_file)`：`Popen(cwd=PROJECT_ROOT, env={PYTHONUTF8=1, PYTHONIOENCODING=utf-8})` → 双 daemon reader 线程泵 stdout/stderr 进队列 → 主循环 0.15s：`handle.check()`（协作取消/暂停）→ 抽干队列（**每行再 `handle.check()`**——满载机器上行处理慢、队列积成大积压，取消绝不能等整个积压抽干才被看到；延迟 = 一行处理时间）：`[progress] frac label` → `handle.progress`；其余 stdout → `on_line`（stage 解析 `[result]`/`[segment]`/`[watchdog]`）；stderr → WARNING 级日志 + `stderr_tail(40)` → `log_file` 时全量镜像（**块缓冲 64KB**、attempt 结束统一 flush——行缓冲 = 每行一次落盘 + AV 扫描，本身就在热路径上放大积压）→ 进程退出且双 EOF 后收工 → **finally**：存活则 `_kill_worker_tree`（Windows `taskkill /F /T /PID`，POSIX 普通 kill）→ wait → 关管道 → 删 `temp_files` → 非零退出：`RuntimeError(f"{fail_prefix}失败（退出码 N）：{stderr尾500}")`，**恰为 `watchdog_code` 时改抛 `WorkerWatchdogTimeout`**（供 batch 缩批重启）。
worker（`tts-engine/tts_worker.py`）6 种 `--mode`：`custom` / `design` / `clone`（前三者**不被后端调用**——后端只用 design-batch（角色配音·克隆）、batch、merge；前三者保留供手动/历史）、`design-batch`、`batch`、`merge`。stdout 协议行：`[progress] <frac> <label>`（所有 mode 驱动任务进度）、`[result] <abs path>`（恰好一次 = 最终文件；merge/单条 mode）、`[segment] <i> ok|error <…>`（batch）、`[design] <i> ok <seed> <abs path>` / `[design] <i> error <reason>`（design-batch）、`[watchdog] timeout batch=<label> indices=[…] elapsed=<s>`（先 flush 再 `os._exit(124)`）。stderr 错误前缀 `TTS_WORKER_ERROR:`。规划检查开关协议：`--disabled-checks <逗号清单>` 为 `batch` / `design-batch` 专用参数，值域 = 五个规范名 `length_bands,batch_chars,seq_chars,length_ratio,vram`（对应 `config.tts.planner_*`，后端经 `tts_batch.disabled_planner_checks` 映射）；全开时**整体省略**（默认配置下 worker 命令逐字节不变）；未知名 → `TTS_WORKER_ERROR` 退出码 2（fail-fast，防拼错静默关门）；该参数恒为 `--flag value` 成对出现（见「已知陷阱」）。模型常量 `DEFAULT_MODEL/BASE_MODEL/DESIGN_MODEL` = Qwen3-TTS-12Hz-1.7B-{CustomVoice,Base,VoiceDesign}；`DEFAULT_SPEAKER="serena"`、`DEFAULT_LANGUAGE="chinese"`。

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
| `tts.parallel_workers` | `1` | **遗留**（无读取方，仅为旧配置 round-trip 保留） |
| `tts.batch_concurrency` | `4` | 音频合成与角色配音·克隆共用的「批内行数」上限（请求 `concurrency` 缺省时用；钳 [1,64]） |
| `tts.batch_seed` | `-1` | 合成可复现 seed（`seed + 子批序号` 播种；-1 = 随机） |
| `tts.planner_length_bands` | `True` | 子批规划开关·段长分档（**设置页开关**；关闭 = 规划跳过该约束 + 运行日志一行「…已关闭（配置）」，经 `--disabled-checks` 传给 worker） |
| `tts.planner_batch_chars` | `True` | 子批规划开关·单批总字符上限（同上） |
| `tts.planner_seq_chars` | `True` | 子批规划开关·超长行（>2500 字）独批（同上） |
| `tts.planner_length_ratio` | `True` | 子批规划开关·批内最长/最短 ≤3（同上） |
| `tts.planner_vram` | `True` | 子批规划开关·静态 L² 显存估算门（**只关静态估算**；实测显存的 VramGovernor 动态调节恒生效、不可关） |
| `tts.api_base/api_key/voice/concurrency` | 空/1 | **遗留** API 字段，本地引擎不读（保证旧配置 round-trip） |
| `llm.base_url` / `api_key` | `http://localhost:11434/v1` / `"local"` | OpenAI 兼容端点（默认本机 Ollama）；远程 API 需真 key |
| `llm.model_name` | `""` | **必须用户设置**（空 → 解析/阶段 1 各自快速失败或兜底） |
| `llm.stream` | `True` | 流式（「流式反馈」面板）；服务器拒 `stream:true` 时置 false 回退非流式 |
| `prompts.system_prompt/user_prompt` | `""` | 解析提示词（空 → `resources/default_prompts.txt`；user 模板含 `{context}`/`{chunk}` 占位） |
| `persona_prompts.system/user/advanced_prompt` | `""` | 角色配音·阶段 1（空 → `persona_prompts.py` 内置；user 含 `{speaker}`/`{line_windows}`，`str.replace` 填充） |
| `generation.chunk_size` / `max_tokens` | `3000` / `4096` | 解析切块 / 单次完成上限 |
| `generation.temperature/top_p/top_k/min_p/presence_penalty/banned_tokens` | `0.6/0.8/0/0.0/0.0/[]` | LLM 采样（0 值的 top_k/min_p 不发；`banned_tokens` 非空才发） |
| `generation.max_concurrency` | `3` | 进程级 LLM 闸门（解析共享；`set_concurrency` 每批设置；另被阶段 1 `ThreadPoolExecutor` 用作 worker 数） |
| `generation.spot_check_rate` | `0.05` | 解析后归属抽样率（0 = 关闭）：全条目重判 speaker 的抽样比例，1/3 纯随机（整书错误率仪表）+ 2/3 风险加权（无标签/≤10 字/多角色场景，特征数级联）；每本纯随机桶读数记入任务日志与 `config/spot_check_history.json`，**降率由用户手动**（不自动降） |
| `generation.revalidate_splits` | `True` | 断句失败校验开关（**设置页开关**；关闭 = 解析时跳过该阶段并记录日志，`suspicious`/`suspicious_fixed` 保持 0） |
| `generation.delete_saying_tags` | `True` | 纯归属标签删除开关（**设置页开关**；关闭 = 解析时跳过该阶段并记录日志，`tags_deleted` 保持 0） |
| `generation.check_batch_size` | `20` | 解析内重判批大小（断句失败校验 / 归属抽样共用；**UI 不露出**） |
| `generation.check_context_window` | `4` | 解析内重判上下文窗口（断句失败校验 / 归属抽样共用；**UI 不露出**） |
| `ffmpeg.ffmpeg_path/ffprobe_path` | `""` | 空 = 从 PATH 解析（**绝对路径，永不相对化**） |
| `log.level` | `"INFO"` | 根 logger 级别（运行时可改） |
| `ui.theme` | `"system"` | 前端 `applyTheme`（system 挂 matchMedia 监听） |

## 前端要点

- **`src/api/client.ts` 是唯一 HTTP 客户端**：`API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8642'`（绝对源直连，CORS 后端全开）；fetch + JSON（响应先取 text 再试解析）；`!res.ok` 抛 `ApiError`。SSE 不在 client 里：`api/tasks.ts::streamAllTasks` 用**原生 `EventSource`** 连**多路复用端点** `/api/tasks/stream`（每标签页一条连接覆盖所有任务，事件带 `task_id`；永不 `final` 终止，由 store 主动 abort；`CLOSED` 回调；断开自动重连 → 后端重发 `snapshot_all` 全量自愈）——浏览器单源 HTTP/1.1 连接上限 ≈6，多窗口 × 每任务一条流的旧设计会超限致窗口无日志（见「任务系统」SSE 协议）；`streamTask`（单任务端点）保留共用同一 `openSse` 管道。唯一例外：`uploadFile` 走 FormData。
- **路由 = hash history**（静态托管下可工作），全挂持久 `MainLayout`（侧边栏固定 + `keep-alive`）：`/dashboard` 开始 · `/text` 排版 · `/book` 分册 · `/script` 解析 · `/voices` 角色配音 · `/batch` 合成 · `/merge` 合并 · `/audio` 分集 · `/settings` 设置（`/` → redirect dashboard）。
- **stores**（Pinia setup 风格）：`app`（`backendUp` 状态灯，3s 轮询 health）；`project`（流水线交接：`textOutput/bookOutputs/activeScript/voiceResult/batchResult/mergeResult/audioOutputs` + `record*` 动作 + computed `bookInput/scriptInput/audioInput`——「前往下一步」按钮的数据源；`activeScript` 被角色配音与音频合成**共享**（合成页的多选是**本地**状态，仅把**首个选中**文件同步到这里——`lastSynced` 守卫防多选被自身同步坍缩；外部页面改它则选择坍缩为该文件））；`settings`（`config` 全量 + `load/save/applyTheme`）；`task`（**应用级单例**：`refresh()` 全量对齐 + **一条多路复用 SSE 流覆盖所有任务**（`streamAllTasks` 连 `/api/tasks/stream`；仅在「至少一个任务非终态」时保持，最后一个任务终态即关流，空闲标签页不占浏览器 ≈6 条/源的连接额度；`refresh()`/`control()` 有活动任务时重开）；`applyEvent(task_id, e)` 按 type 分发（`snapshot_all` → 整表替换；日志超 1000 条裁最旧、`llm_stream` 客户端 128KB 上限与后端对齐）；`control`/`stopAll`/`startStream`（遗留别名 → 确保应用流）；`activeTasks(module?)` 返回非终态任务（可选按 module 过滤），供五个发起任务的页面在 `onMounted`（`await taskStore.refresh()` 后）把刷新后丢失的本地 id 引用重新挂回在途任务——进度/日志/取消随之恢复，label 格式依赖见「已知陷阱」；store 创建时即 `refresh()`）。
- **文件选择 = 隐藏 `<input type=file>` → `uploadFile` → 绝对路径**（`utils/fileops.pickFile`）；目录/文件浏览用 `DirPicker`（扫 `GET /api/files/list/{目录名}`，支持 default/`__all__` 行、pick-dirs、后缀过滤，`@scanned` 回传绝对路径）；下载 = 导航 `GET /api/files/download/{目录名}/{文件名}`。
- **组件**：`LiveLogPanel`（进度条 + 当前步骤 + 分级着色日志 + 自动跟随 + actions 插槽放取消钮）、`LiveStreamPanel`（原始 LLM 流回看）、`MiniAudioPlayer`（`useAudioBus` 全局单例保证同屏只播一个试听）、`WorkspaceGateAlert`、`Toaster`。`utils/log-follow.useLogAutoFollow`：仅当用户本就在近底（≤48px）时才自动滚底——翻看历史不被拽回。
- 页面任务展示惯例：解析页每文件一行（状态 = 待处理 / 解析中 / 已完成 / 失败 / 已取消 / 已暂停；状态 Badge + 进度 + `LiveLogPanel` + 完成后 `LiveStreamPanel` 可回看；顶部 3 指标卡 = 并发数 / 吞吐量 Σ`llm_cps_10s` / 处理速度 Σ`llm_chars`÷Σ`llm_secs`，**纯 SSE 驱动无定时器**）；文件行徽章：「已完成」= `03_parsed_json/` 已有该文件基文件（可再次勾选重新解析）；角色配音 / 音频合成页保留 `_checked.json` 选择过滤，作为旧工程遗留孤儿的守卫（防旧孤儿被选为源）；角色配音/合成/合并页各自 `LiveLogPanel`（角色配音 watch 任务 progress 变化即重拉角色表）；分集页用行内 Alert（`task.current` + 进度条 + 取消）；合成页每行「已合成/总段落 · 角色 · 已就绪声音」用 `batchStatusFiles` **3s 轮询**（读增量 manifest 真实计数，不走 SSE；文件全部完成瞬间行上出现【已合成】徽章；轮询只在运行中任务非终态时进行）。本页是 keep-alive 缓存页：**`onUnmounted` 在导航时不触发**，行刷新与轮询定时器都挂在 `onActivated`/`onDeactivated` 上（隐藏时定时器停发、不产生请求；重新进入时刷新行并在任务仍在跑时恢复轮询）。

## 测试（545 个，`backend/tests/`）

| 文件 | 固化的行为 |
|---|---|
| `test_text.py` | 排版：内容保持、幂等、空白/省略号/重复标点、孤立 ASCII 保护（URL）、对话/叙述分段、句断开关、章节隔离与正负样例、10 开关逐一、端到端样例 |
| `test_book.py` | 分册：章节铺满/真实章头/无章节即停、册数选择、连续分区、**无损往返拼接 == 原文**、字数排除换行（非 BMP 计 1）、命名格式/补零/缺口不重编号、编码探测（BOM/UTF-8/GB18030）、中文数字解析、序号体检、zip STORED 往返 |
| `test_audio.py` | 分集：时长解析、均分铺满且每段≤目标、只动内部边界/严格单调/窗口回退、**段数不变**、分集命名（`{}`/无 `{}`/非数字 start）、停顿日志解析（负 start/尾部封顶/排序）、真实 ffmpeg 集成（缺则 skip） |
| `test_merge.py` | 合并编排：命令含 `--tmp-dir`/`--merge-batch-size 100`、两阶段日志、成功/失败路径都清暂存、WAV 兜底搬迁、manifest 缺失/为空/全缺文件错误、缺文件跳过 + 警告、遗留输出名 |
| `test_script.py` | 解析：切分无损/打包/分句、**章标题防丢**（尾部标题 → 下一 chunk 开头 / 独占标题独立成块）、JSON 清理/修复/正则抢救、乱码修复、默认提示词加载、**流式与非流式字节级一致**、思维链不进返回值、流中取消穿透不重试、**忠实性校验**（忠实输出通过 / 丢行必抓 / 引号标签剥离免疫误报 / 短引语忽略）、**对半切**（段落/换行/句末回退、无边界 → 保留整块）、**忠实性恢复 e2e**（丢行 → 翻倍 max_tokens 挽回、请求体钉死 100→200 共 2 次调用 / 两轮皆败 → 一级对半切、调用数钉死 4 / 不可再切 → 保留最好结果共 2 次调用）、**相邻旁白合并**（链式合并保留首条 instruct / 输入不动 / 标题两侧不合并 / 长度上限：<100 缺省挡 120、放 4000 挡 4100）、**断句失败校验**（检测器正负钉死：包裹 + 引号内「…道 + 冒号」触发 / 无包裹 / 无冒号 / 包裹外标签不触发 / 空包裹 / 非 dict 跳过、开头标签剥离（含「知道：」形态 / 中段不剥 / 未包裹不动）、忠实性门（标签丢弃单段 / 标签保留双段 / 原样包裹 通过；丢词 / 增词 / 花名册外角色 / 多段同主体 / 段序颠倒 / 空段 / 缺 text / 空 / 非 dict 拒绝）、投票协议（2:0 早停共 2 调 / 2:1 第 3 调定 / 1:1:1 → 第 4 调 / 4 异不猜 / 不可解析与未过门无票 / 取消零调用上抛）、无标记零 LLM 调用、**降序应用钉死**（1→2 拆分不移位后续窗口 / 输入列表保持原样）、无共识原对象原样返回、**纯归属标签条清理**（谓词正/负钉死：五动词×标点形态命中（。/，/：/！/……/无标点）/ 知道·难道 形态守卫 / 混合长条（>10 字）/ 带引号 / 章标题（空格形态）/ 角色条 / 空·纯标点·非 dict 不中；邻接：台词前后均删 / 孤立留 / 双旁白夹留 / 隔一条留 / 非级联（双标签只删紧邻台词者）/ 空 speaker 非对白、**e2e 删除**（[旁白, 标签, 台词] → 2 条、解析外零 LLM 调用、无合并）/ **e2e 先删后抽样**（rate=1.0 全量抽样窗口里无标签条、零分歧 1 调零改动））、**generate_file 全 e2e**（1 解析 + 2×2 校验 = 5 调用、拆出的旁白段 = 独立纯标签被确定性删除（`tags_deleted=1` / `merged_narrator=0`）、直引号形态单条重写、「知道」负例原样、结果含 suspicious / suspicious_fixed / tags_deleted；**必须传 `spot_check_rate=0.0`**——默认 0.05 会跑归属抽样打破调用数断言）、**归属抽样**（预算表钉死 @0.05 各 N + rate=0/负/空关闭 + rate≥1 全量、标签检测正例〔人名×五动词 / 他她×五动词〕与负例钉死〔他知道 / 她知道 / 不知道 / 谁知道 / 他难道 / 这道理 / 林某知道 / 纯旁白 → False〕、前一条规则〔标签可在前一条 NARRATOR；前一条是角色台词不算〕、tier 组合 0/1/2/3 钉死 + 短边界 10 中 11 不中 + 多角色 4 中 3 不中 + i=0 窗口 clamp、两桶不相交/同 seed 同选择/换 seed 不同（N=200 固定 seed）、级联高 tier 先抽 + tier 0 补足用满预算 + 级联不变量、历史文件 55 追加留最近 50 + schema 钉死 + 损坏/缺失 → `[]`、关闭零 LLM 调用、协议〔零分歧 1 调零改动原对象 / 2:1 首判+1 重试即停只改 speaker / 4 调无共识保留原对象 / 去引号三规则〔严格一致采纳 / 不符 WARNING 忽略 / NARRATOR 终值不剥〕/ 取消上抛输入不动〕、**spot 全 e2e**（1 解析 + 2 spot = 3 调用、纯随机桶目标 2:1 改判随基文件写出、六 `spot_*` 字段、历史文件读数 + 03_parsed_json 不被历史文件污染）、**取消 e2e**（取消落 spot 阶段 → 基文件与历史都不落盘）、**开关关闭语义**（`revalidate_splits=False` → 校验 LLM 零调用 + `suspicious=0` + 日志「已关闭（配置）」；`delete_saying_tags=False` → 标签条保留（与邻接旁白合并）+ `tags_deleted=0` + 同一日志）、**重判批协议**（自已退役的独立检查模块整体搬入：`build_batch_window`〔target 标记/边界 clamp/零 n 只含目标/`skip` 条不标 target/原 speaker+text 透传〕、`parse_speaker`〔对象/围栏/思维标签/单元素数组/裸 token/垃圾→None〕、`parse_speaker_map_full`〔text 捕获/非 target 与空白丢弃/speaker 投影〕、`strip_outer_quotes`〔6 种引号对/内层引号保留/未包裹或空内部→None〕、`_pick_majority`〔严格多数/平票/孤票/异值→None〕、`group_retry_indices`〔间距 + 批上限分组〕） |
| `test_llm_rate_window.py` | 吞吐量 10 秒窗：样本数/零跨度防除零、真实均值、年轻任务部分跨度、累计单调、样本逐出、SSE 事件 |
| `test_task_bus.py` | 多路复用 SSE 总线：manager 创建的任务事件全量转发为 `(task, event)` 元组、退订即停、独立 Task 无广播钩子、**队列满时丢纯展示事件（`llm_chunk`/`llm_rate`）保关键事件**（驱逐最旧展示事件重试 / 无可驱逐则丢弃新事件而非打乱既有关键事件）、`/stream` 路由（`snapshot_all` 回放含新建任务 + 直播事件带 `task_id`） |
| `test_voices.py` | 角色配音：JSON 对象抽取、说话人名归一化（CJK 保留）、Jaccard、采样带/窗口、`pick_ref_text`/fallback、`_sanitize`、foundation/clone 状态推断、**自动备选数对数分档**（龙套<20=1/量级锚点 100→3·200→4·500→5·1000→6·2000→7·2300+→8/单调有界/真实规模 e2e：旁白 2 万条不压主角）、**候选簿记**（新格式清洗/旧格式合成 1/无克隆空）、**别名台词归并**（链防环/出域丢弃）、**make_clones 全 e2e**（fake-worker 真子进程经真 `run_worker`：固定/自动计数、部分失败重编号、全失败保留旧 ref_audio、取消角色级原子、new_only 差额、进度单调（worker 驱动、重启不回跳）、看门狗缩批重启、size-1 两次超时隔离、8 次上限、断点采纳零作业短路、批内共享 seed、**规划开关 `--disabled-checks`**（缺省省略 / 关一项 → `vram` / e2e 带 flag 经真 `run_worker` + fake worker 跑完全 ok、磁盘 run 日志证明 flag 到达子进程命令行）、select 端点（选择/清除/400/404/409 工作空间/409 任务在途）、list_voices（候选字段 + 列表台词数降序） |
| `test_tts_batch.py` | 合成编排：段构建/`_voice_usable`、concurrency 钳制（含 0 与负值）、看门狗缩批重启成功、workers=1 两次超时隔离（manifest 记 ok:false）、8 次上限、运行日志镜像、resume/显式 indices、**增量写 manifest（逐条内存态 + 节流落盘、收尾强制落盘）+ 工作空间相对路径**、零段不启动引擎、**`--disabled-checks`**（缺省省略 / 关闭项按规范序入值 / `disabled_planner_checks` 助手：全开 `""`·单项·全关五名规范序）、`batch-status`、**多文件**（已完成文件短路零引擎/全完成零子进程/零段文件成功/失败段带 script 标签/进度按文件窗口单调/单文件致命错误隔离/全失败 RuntimeError/文件间取消穿透且 manifest 保留）、**batch-status 多文件**（计数/complete/ready/missing/零字典/`__all__` 400/无工作空间降级）、**run_batch 分发**（多→`synthesize_multi`/单→旧路径/`indices`+多 400/`__all__` 400）、**reset_batch**（删包目录/无包空操作/`_checked`→基包/`__all__` 400/无工作空间 409/任务在途 409/**e2e：重置后同一条普通调用重做全部**） |
| `test_tts_worker.py` | worker 纯函数（importlib 加载，无 torch）：子批规划（行必入恰一批/长度分档/字符上限/超长独批/比值拆批/**长度比 3 边界**（恰 3x 同批 / 超 3x 拆）/design 行缺省独行、`force_rows_cap` 才共享）、**按角色分组**（`order_speaker_groups`：台词数降序 / 平手首见序 / 角色内长度升序 / 绝不混角色）、**长度比例解码上限**（`max_new_tokens_for_chars` 短行 floor / 中段比例 / 长行顶格 / 单调 / `_generate_rows` 三 vtype 透传钉死）、显存估算、`band_cap_for_chars` 钉死值、**规划开关 `--disabled-checks`**（`parse_disabled_checks` 空白容错/去重/未知名抛错含合法名列表；vram 关 = 用户真实 32×50 字场景开时首批 2 段·关时单批 32 段 / 分档关 / 总字符关 / 超长独批关的诚实交互〔3000 字行仍被分档+比值强制独批，三项同关才合批〕/ 比值关 / 五项全关 = 纯手动上限）、`VramGovernor`（减半/增长/floor/故障/标定）、超时预算（cpu/clone/gpu 曲线）、看门狗、design-batch 纯函数（`_generate_rows` 逐行 instruct + `force_do_sample` 透传、`_save_and_report_design` 协议行 + 逐行容错）、合并纯函数（批覆盖/`boundary_gap`==单遍/进度带单调）、**顶层 stdlib-only + 旧调度器已删除**的模块约束 |
| `test_tts_stress.py` | 压测引擎（临时测试入口，纯函数；Task worker 经页面手工覆盖）：自动生成文本（精确长度 / 零与负 → 空 / 仅 CJK+句读 / 行偏移错开 / 超大偏移可切片 / 2500 上限）、轮次判定（限时 = 字数 ÷ 10 字/秒 / 限时内与恰界通过 / 超时带「10 字/秒」/ None → 无法测得即失败）、克隆音色选择（自动取首个可用克隆 / 显式非克隆或缺 ref 报错 / 无克隆报错） |
| `test_pathio.py` | 路径模型：相对/绝对/外部资源、`..` 越界拒绝、工程搬家两级恢复、幂等迁移、`migrate_entries_in` list/dict；voice_config 含 candidates 时只迁顶层 `ref_audio`（嵌套候选相对路径不动） |
| `test_paths.py` | Layout 惰性/ensure、指针解析（相对→项目根）、`resolve_parsed_json(_all)`（只读基文件/显式 `_checked` 名直读/孤儿惰性〔指名基名忽略孤儿、自动选取取最新基文件、`__all__` 排除孤儿、仅孤儿目录 → 遗留名兜底/空列表〕/`__all__` mtime 序/遗留名兜底） |
| `test_config.py` | 配置 round-trip、两文件模型（根只写指针/工作空间独立/不覆盖已有）、TTS 字段缺省（三模型 id、500/250、parallel_workers=1 ≠ batch_concurrency=4、**五个子批规划开关 planner_*=True**）、GenerationConfig 解析内检查缺省（revalidate_splits/delete_saying_tags = True、check_batch_size=20、check_context_window=4）、遗留字段保留 |
| `test_concurrency.py` | 闸门：clamp≥1、增/减上限、acquire/release 配平、唤醒等待者 |

## 已知陷阱 / 历史遗留（改动时留意）

> 以下均为**有意的设计**而非缺陷——改动时别"顺手修正"；真正可修的陈旧文案（误导日志 / 测试 docstring / README 测试数）已清理。

- **解析的逐 chunk 忠实性校验 = 引语段骨架比对，而非字数比对**（源中 ≥4 词字符引语段的骨架 = 只留词字符，须在输出 text 拼接骨架中作子串找到）——别改回全文字数/多重集对比：模型对引号剥离/标签并入的合法编辑会令字数对比**系统性误报**，触发无谓重发与调用成本；而截断/丢行在引语段骨架下必被抓住。同理 `merge_adjacent_narrator` 的合并长度上限（**<100 字**，与 TTS 行超时预算 `gpu chars/15`〔floor 30s〕联动——100 字行的预算仍宽裕（≈4× 预期时长））是有意取紧：合并行保持短小 → 单行失败代价小、节奏更细、顺带杜绝无界链式合并——别去掉上限或放大到数千字量级。忠实性恢复阶梯**有意保持短**（只有两级：翻倍 max_tokens 重跑一次 → 一级对半切各跑一次；无 temp-0 重发、无深层递归切分，半段跑完即止）——截断是最常见的忠实性失败根因，翻倍预算 + 对半缩块已覆盖主要情形且调用成本有界；别再加升级阶段（多轮投票 / 递归切分 / 逐级翻倍），调用成本与行为可预测性都会失控。
- **解析后的断句失败校验 = 启发式触发 + 门控投票，不是规则化重写**：检测 = 外层双引号包裹（弯/直任意形式）且引号跨度内「…道 + **冒号**」（`rfind` 取最宽跨度，嵌套同款引号也可见）——冒号是硬条件，`知道/难道/道理` 的「道」后无冒号不触发；但带冒号的 `知道：` **会**触发，此时由忠实性门 + 多者胜保护内容（重推骨架拼不回就无票），别在检测器里加词表黑名单。忠实性门接受**两种**骨架：原文、以及剥掉**开头**「…道：」标签后的原文（重判复用解析提示词，纯标签整段丢弃是其编辑 (e) 的合法产出）——剥后的形态保留内层开引号（`““…`），无害（骨架忽略引号，该形态只进门、永不落盘）；**别把剥标签扩到中段**（中段「…道：」是另一种混合形态，重判协议不会拆条——保持原样，保守不动）。**1 段胜出必须应用**（干净重写），不能当 keep——否则规范失败形态（其正确重判恰是单条剥标签条目）永远修不好。校验位于 `merge_adjacent_narrator` **之前**（拆出的旁白段随后照常合并）；**复用解析提示词**（无新提示词文件，`prompts.*` 仍是唯一来源）；窗口/花名册按原始列表预建 + 胜出者**降序下标**应用——别改成「每应用一条重取窗口」（下标会指错条目）。
- **纯归属标签条清理 = 六条件合取的确定性删除，不是启发式重写**（解析任务内、断句校验之后、归属抽样之前，见阶段 3）：**≤10 字阈值是精度的主要滤网**——真实语料里上千条以「…说道。」收尾的 NARRATOR 条目绝大多数是「叙述 + 标签」的长混合条（>10 字，绝不被删）或合法的独立标签行，**别放大阈值**（会吞掉混合叙述）；**无引号条件是台词保护**——带引号的短条（“快说。”）是台词内容，删了 = 整句台词丢失，比重判错归属（后有解析内重判/抽样兜底）严重得多，**别去掉**；末字「道」必过**知道/难道 形态守卫**（他不知道。是真实叙述）与**章标题守卫**（第5回 问道 是标题不是标签——注意 CHAPTER_RE 要求「第N回」与标题之间有分隔符）；**非级联**是有意设计（代码无法区分纯标签与恰好以动词收尾的短叙述句，隔一条的宁可留）；**位置在归属抽样之前**是有意设计（抽样重判可把标签条翻成角色 speaker，规则便不再适用，且角色会用本人声音念第三人称标签）；删除不会制造新的旁白相邻对，机械合并不受影响；受 `generation.delete_saying_tags` 开关（**默认开**，开启时零 LLM 成本；关 = 跳过 + 一行日志 + `tags_deleted=0`）；0 命中也留一行日志（静默退出会被用户误读成阶段缺失）。
- **解析后的归属抽样 = 两桶抽样 + 复用检查协议，不是新检查阶段**：`generation.spot_check_rate` 默认 0.05 是**活的**——任何钉死 `generate_file` LLM 调用数的测试必须显式传 `GenerationConfig(spot_check_rate=0.0)`，否则抽样阶段会多出的调用打破断言。归属标签检测的 知道/难道 排除是**形态守卫**（人名组贪婪会把 知/难 吞进名字，故查「道」**前一字符** ∈ {知,难} → 跳过该匹配），**别改成开放词表黑名单**（知道/难道 之外的同形态词会漏，词表会腐）；负例由测试钉死（他知道/她知道/不知道/谁知道/他难道/这道理/林某知道 → False）。**历史文件必须在 `config/`，绝不进 `03_parsed_json/`**——后者被 `resolve_parsed_json_all` 整体 glob 当解析产物，混入一个统计 JSON 就污染角色配音的「全部文件」聚合；`config/` 不经文件列表 API。解析任务把抽样修正**烙进自己的基文件**不违反不变量 #9（不变量 #9 说基文件是解析任务的唯一产物、检查阶段永不改写它——基文件本就是解析任务正在写出的文件）。**只记录读数、永不自动降率**：历史文件与任务日志是用户的「仪表」，降不降由用户在设置页手动决定——别加自动降率逻辑。**解析内抽样修正直接烙进基文件，即最终态**（独立检查阶段已退役，不再有后续全量重判兜底——严格多数投票 + 只改 speaker 的协议就是其精度保证）。
- **子批规划五道检查是合取且相互交互的**（段长分档 / 单批总字符 / 超长行独批 / 批内长度比 / 显存静态估算，均可经 `config.tts.planner_*` 单独关闭）：关一项**不保证**出现预期的合并——例如 >2500 字的行同时被长度分档与长度比两道检查强制独批，只关「超长行独批」它仍不与短行同批（须 seq_chars / length_bands / length_ratio 三项同关才合批）。别把这种交互当 bug，也别「顺手」为达预期合并而去削弱其余检查（它们各有独立的显存/时长保护动机）。
- **worker 命令恒为 `--flag value` 成对**：`test_voices.py` 的 fake design worker 用通配「见 `--` 开头即取下一 token 作值」解析，混进一个**无值** flag 会把下一个 flag 名吞作它的值，静默改坏命令（如 `--concurrency` 丢失落回默认 1，测试与实跑行为漂移而无任何报错）。后端拼 worker 命令时永不发无值 flag（`--disabled-checks` 协议同样恒带值；全开时整体省略而非发空值）。
- worker 的 `--mode custom/design/clone` **不被后端任何端点调用**（主链路只用 `design-batch`（角色配音·克隆）、`batch`、`merge`）——保留供手动/历史用途；改 worker 时别误以为它们有调用方。
- `config.tts.parallel_workers` 是**遗留字段（无读取方）**——前端初值改读 `tts.batch_concurrency`（音频合成与角色配音·克隆共用的批内行数上限，单进程 GPU 张量批）；仅为旧配置 round-trip 保留，文档/注释里别再当它生效。
- 音频分集页参数的「自动保存」发生在**前端**（fire-and-forget `PUT /api/config`）；后端只按「请求字段 or 配置缺省」取值，自己从不写配置。
- `merge` 的 `m4b=True` 目前只是 WARNING（输出 MP3）——M4B 是后续阶段。
- `config.py` 的读取**永不失败**：文件缺失/损坏/类型不符 → 静默降级（工作空间配置 → 根模板 → 代码默认）；`_load_config_file` 吞掉一切解析异常。写入才是严格路径。
- 角色配音页的「全部文件」（`__all__`）是**该页局部**选择，不写入共享的 `project.activeScript`；音频合成页的多选同样是**局部状态**（`selected` map，键 = 文件名），只在**用户交互**时把**首个选中**文件同步到 `activeScript`（`lastSynced` 守卫防自身同步坍缩多选；角色配音页改它 → 本缓存页选择坍缩为该文件），且拒绝 `__all__`。别把整个多选写进 `activeScript`（单值字段会碾掉多选）。
- `GET /api/files/list/{module}` 的 `module` 参数是**磁盘目录名**（`02_split_text` 等），不是 Layout 属性名。
- **遗留 `_checked` 孤儿 = 惰性文件，resolver 永不读取**（两独立检查阶段退役后应用内再无代码写 `_checked.json`）：旧工作空间的 `_checked.json` 孤儿在自动选取 / `__all__` 聚合里被排除（glob 排除 `_checked` 名），文件列表可见、可下载，显式指名 `_checked` 名仍可直读；**下游结果陈旧时重跑解析（重写基文件）即恢复**，孤儿继续留盘（「绝不删除用户数据」约定，勿顺手清理）。
- worker 的 `--seed` 现为 **batch / design / design-batch 三处共用**：design / design-batch 按 `base_seed + 子批序号` 播种（**同子批多候选共享一个 seed**——复现条件 = 同输入 + 同 seed + 同批布局；design-batch 另加固 `do_sample=True`，防个别 checkpoint 关采样致候选逐字节相同；断点采纳的候选 seed 记 −1）；batch 为 `seed + 子批序号`。别当 batch 专属。
- **角色配音·克隆的任务进度由 worker 的 `[progress]` 行驱动**（后端运行中不发进度，只在终末发「完成」）；worker 经 `--done-offset`/`--total` 参数上报 `(offset+done)/total`，**看门狗重启后进度不回跳**——别重新引入后端侧的进度驱动。
- **`run_worker` 的取消在排空循环内逐行检查**（`handle.check()`，不是只在主循环顶部）：满载机器（Defender 实时扫描 + 磁盘/GPU 争抢）下行处理慢，worker 全速喷出的行在队列里积成大积压——取消若只在顶部检查，点取消后要等整个积压抽干（分钟级）才杀进程树，用户看到日志还在滚动 = 「取消停不下来」（2026-09 实测事故；`test_tts_batch.py::test_run_worker_cancel_not_stalled_by_backlog` 钉死）。同理 run 镜像 `log_file` **块缓冲**（行缓冲的每行 flush 是积压放大器之一）——别改回「只在主循环顶部 check」或「行缓冲镜像」。
- 旧的每作业子进程编排 `_design_preview` / `_ChildReg` **已删除**（唯一调用方 make_clones；阶段 2 现为单一长驻 design-batch 子进程）——别把「每候选一个子进程」的形态恢复回来。
- **自动备选数 = 角色自身台词的绝对对数分档**（`auto_candidate_count(台词数)`，无工程相对分母）：旁白可达主角 10 倍量级，任何"相对全脚本最大值"的比例都会把主角压成 2~3 条——别改回 ratio，也别把分档改回"随工程规模缩放"；锚点（100→3 / 200→4 / 500→5 / 1000→6 / 2000→7 / 2300+→8）与真实规模 e2e（`test_make_clones_auto_counts_follow_ladder`：旁白 2 万条 → 8 且不压主角）钉死该行为。
- **多候选升级后首次 new_only 重渲染**：旧单候选角色 `have=1 < 目标≥2` → 首次「批量制作（仅新增）」会把它们全部重渲染为多候选（特性升级路径，且收敛：重渲染后 have=目标，之后只碰真新角色）；自动模式龙套（目标=1）恒跳过。
- **切换备选数 = 整组差额重渲染**：重跑整体替换候选组，**旧候选文件留盘成孤儿**——`04_voice_profiles/designed_voices/` 随时间累积，页面只列当前候选；「绝不删除」约定下勿顺手清理。
- `selected_audio_id` 在**重渲染后重置为 None**（旧选择对新候选组无意义）；候选 <2 的角色（自动模式龙套、旧格式单候选）「选择音色」按钮**禁用**——均为有意行为。
- `migrate_entries_in` **非递归**：dict 形态只迁每 entry 顶层 `ref_audio`，嵌套 `candidates[].ref_audio` 不动（候选写入时即工作空间相对，永不需迁移）。
- **路由函数的裸 `list` 参数 ≠ 查询参数**（FastAPI 0.141 行为）：`scripts: list[str] | None = None`（不带 `Query()`）会被当成 **JSON 请求体**——URL 里的 `?scripts=…` 被静默忽略（端点落回单文件分支，前端待合成行全 0）；必须写 `Annotated[list[str] | None, Query()] = None`（Annotated 保住普通默认值 `None`，测试才能继续直调函数——测试套件全部直调、不走 HTTP 层，这类参数绑定 bug 测试抓不到，改路由签名后用真实 HTTP 探测验证）。
- **各页的 F5 刷新恢复依赖任务 label 格式**（见「横切·任务系统」刷新恢复段）：文本解析页按 `文本解析（{文件名}）`（全角括号，`ScriptParse.reattachJobs` 正则）取回文件名；音频分集页按 label **前缀** `停顿检测：` / `音频分集：`（全角冒号）区分两类任务；音频合并页按 label **尾部** `：{package}` 还原所选包；音频合成 / 角色配音只按 module 匹配。**改这些 label（`api/script.py` / `api/audio.py` / `api/tts.py`）会静默破坏对应页面的刷新恢复**（页面无报错，只是刷新后看不到在途任务）——改 label 时同步改前端挂接逻辑。
