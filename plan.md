在现有“文本解析”流程基础上，增加 **Speaker 检查功能**，同时完善检查结果文件、待解析列表状态以及后续流程的文件引用。

### 1. Speaker 检查逻辑

用户可以在“文本解析”页面配置 **上下文窗口大小**。

例如上下文窗口设置为 `4` 时，每次检查一条 JSON：

```text
前 4 条
当前条 ← 检查目标
后 4 条
```

共向 LLM 提供 9 条 JSON。

LLM 根据完整上下文重新判断**当前条**的 speaker，并只返回当前条正确的 speaker。

程序将 LLM 返回的 speaker 与当前 JSON 原有的 speaker 进行比较：

* 相同：保持不变
* 不同：修改当前条的 speaker

上下文中的其他 JSON 仅用于辅助判断，不进行修改。

### 2. 检查结果文件

文本解析完成后，生成原始 JSON，并保持现有文件命名。

执行 Speaker 检查后：

* 不覆盖原始 JSON
* 基于原始 JSON 生成新的 JSON
* 新文件名在原 JSON 文件名末尾添加 `_checked`
* `_checked.json` 作为 Speaker 检查完成后的最新结果

例如：

```text
chapter_001.json
        ↓ Speaker 检查
chapter_001_checked.json
```

原始 `chapter_001.json` 必须保持不变。

`_checked.json` 应完整保留原 JSON 内容，仅允许修改经过检查的 `speaker` 字段。

### 3. 待解析列表状态

待解析列表需要根据实际处理结果同步更新状态：

```text
章节1
↓
章节1 [已完成]
↓
章节1 [已完成][已检查]
```

状态规则：

* 文本解析完成 → 添加 `[已完成]`
* Speaker 检查完成 → 在 `[已完成]` 后继续添加 `[已检查]`

状态必须与实际文件处理结果保持一致，不要仅依赖前端内存状态。

### 4. 后续流程文件引用

Speaker 检查完成后，所有需要读取解析 JSON 的后续流程，都必须优先使用 `_checked.json`。

规则：

```text
存在 chapter_001_checked.json
        ↓
使用 chapter_001_checked.json

不存在 chapter_001_checked.json
        ↓
使用 chapter_001.json
```

例如：

```text
文本解析
    ↓
chapter_001.json
    ↓
Speaker 检查
    ↓
chapter_001_checked.json
    ↓
TTS / 音频处理等后续流程
    ↓
读取 chapter_001_checked.json
```

请检查项目中所有引用解析 JSON 的后续逻辑，确保存在 `_checked.json` 时不会继续读取原始 JSON。

### 5. 文本解析页面增加“检查提示词”

在现有“文本解析”页面中增加一个独立的 **检查提示词** 文本域。

该提示词专门用于 Speaker 检查，与现有的“解析提示词”完全独立：

* 文本解析使用“解析提示词”
* Speaker 检查使用“检查提示词”
* 两者分别编辑、分别保存
* 不要复用、覆盖或修改现有解析提示词

执行 Speaker 检查时，使用用户配置的“检查提示词”，并在其基础上追加当前 JSON 的上下文窗口数据。

“检查提示词”需要接入现有的配置保存/读取逻辑，确保用户修改后重新打开页面仍能保留配置。

### 6. Speaker 检查数据处理原则

Speaker 检查过程中：

* 不修改 `text`
* 不修改 `instruct`
* 不修改其他 JSON 字段
* 只允许修改 `speaker`
* 不修改上下文中的其他条目
* 原始 JSON 不覆盖
* 检查完成后统一生成 `_checked.json`

最终形成：

```text
原始解析结果
    │
    ├── chapter_001.json       ← 保留原始结果
    │
    └── chapter_001_checked.json ← Speaker 检查后的结果
                                  ↓
                             后续流程使用
```
