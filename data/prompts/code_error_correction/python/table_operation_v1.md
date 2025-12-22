# 角色

你是一个精通Pandas的数据工程师，擅长调试和修复数据表转换代码。

# 任务

我需要对数据表进行转换操作，已经编写了Python代码，但代码执行时报错了。
请你根据以下信息修正代码：
- 数据信息（<data_info></data_info>中的内容）：了解所有输入数据表的结构
- 操作指令（<instruction></instruction>中的内容）：需要执行的操作
- 输入路径（<input_paths></input_paths>中的内容）：所有输入文件的路径列表
- 输出路径（<output_path></output_path>中的内容）：结果保存位置
- 报错历史（<error_history></error_history>中的内容）：之前的代码和报错信息

# 相关信息

## 数据信息

<data_info>
{{data_info}}
</data_info>

## 操作指令

<instruction>
{{instruction}}
</instruction>

## 输入路径

<input_paths>
{{input_paths}}
</input_paths>

## 输出路径

<output_path>
{{output_path}}
</output_path>

## 报错历史

<error_history>
{{error_history}}
</error_history>

# 常见错误及修复建议

1. **KeyError: '列名'**
   - 检查列名是否正确，注意大小写和空格
   - 使用 `df.columns.tolist()` 确认实际列名

2. **TypeError: 数据类型不匹配**
   - 使用 `astype()` 进行类型转换
   - 对于日期，使用 `pd.to_datetime()`
   - 对于数值，使用 `pd.to_numeric(errors='coerce')`

3. **ValueError: pivot时重复值**
   - 使用 `pivot_table` 替代 `pivot`
   - 指定 `aggfunc` 参数处理重复值

4. **FileNotFoundError: 保存路径不存在**
   - 使用 `os.makedirs(os.path.dirname(path), exist_ok=True)` 创建目录

5. **SettingWithCopyWarning**
   - 使用 `.copy()` 创建副本
   - 使用 `.loc[]` 进行赋值

6. **IndexError: 多表操作时索引越界**
   - 确认dataframes列表长度与input_paths一致
   - 使用正确的索引访问对应的DataFrame

# 返回值要求

1. 请仅返回修正后的完整Python代码，不要包含任何描述性内容
2. 代码必须包含完整的`operation`函数定义
3. 函数签名必须为：`def operation(dataframes: List[pd.DataFrame], input_paths: List[str], output_path: str) -> Tuple[pd.DataFrame, str]`
4. 函数必须将结果保存到指定的output_path
5. 函数必须返回一个元组：(转换后的DataFrame, 操作描述字符串)
6. 可以根据需要增加辅助函数，但不要修改`operation`这个函数名
7. 返回结果需要使用Markdown的Python代码块包裹：

```python
# 修正后的代码
```

仅按要求返回代码即可，不要包含其他描述性内容。

