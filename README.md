# UserDefinedPhrase

一个用于生成 Windows 11 微软拼音输入法“用户自定义短语”DAT 文件的轻量工具。

本项目只生成一个导入文件：

```text
UserDefinedPhrase.dat
```

## 文件说明

- `UserDefinedPhrase.txt`：可编辑的词源文件
- `UserDefinedPhrase.dat`：生成的用户自定义短语文件
- `vocab_pipeline.py`：生成和校验脚本
- `vocab_sources.json`：在线候选词配置
- `requirements.txt`：Python 依赖

## 使用方法

```powershell
python -m pip install -r requirements.txt
python vocab_pipeline.py build
python vocab_pipeline.py validate
```

生成后，在 Windows 11 中进入：

```text
设置 → 时间和语言 → 语言和区域 → 中文（简体）→ 选项 → 微软拼音 → 词库和自学习 → 用户自定义短语 → 导入
```

选择 `UserDefinedPhrase.dat`。

## 词源格式

直接写中文词语时，脚本自动生成无声调全拼：

```text
深度求索
赛博对账
班味
```

也可以使用自定义触发码。格式为：

```text
触发码<TAB>词条<TAB>候选位置
```

示例：

```text
deepseekcn	深度求索	1
chatgpt	ChatGPT	1
mcp	模型上下文协议	1
```

规则：

- 触发码只能使用小写英文字母，最长 32 个字符；
- 触发码不能以 `u` 或 `v` 开头；
- 候选位置范围为 1—9；
- 同一触发码可以设置多个候选词；
- 词条最长 64 个字符；
- `#` 开头的内容为注释。

## 重要说明

`UserDefinedPhrase.dat` 是“用户自定义短语”格式，不是“自学习词汇”格式。

- 自定义短语支持 `deepseekcn → 深度求索` 这样的固定触发码；
- 自定义短语支持英文产品名、缩写和特殊写法；
- Windows 11 的“自学习词汇”使用另一种 DAT 格式；
- 一个 DAT 文件不能同时兼容这两个导入入口。

本项目只生成用户自定义短语格式，不生成自学习词汇文件。

## 词源维护建议

优先添加：

- 拼音歧义大、容易被微软拼音选错的词；
- 网络流行语和谐音梗；
- 英文产品名、缩写和特殊写法；
- 专有名词、项目术语和职场缩写；
- 具有明确自定义触发码价值的词。

不建议添加普通问候语、日期、数字和输入法本身已经能稳定打出的普通词。

## 许可证

本项目使用 [MIT License](LICENSE)。
