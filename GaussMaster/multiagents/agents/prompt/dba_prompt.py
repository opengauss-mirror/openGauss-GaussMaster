# Copyright (c) 2025 Huawei Technologies Co.,Ltd.
#
# openGauss is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

intent_prompt_zh = """
请判断用户问题是否属于要对指定故障现象诊断根因的问题，一般该类问题用户问题中包含根因|原因字眼。

回复格式为布尔值[Ture|False]：
若是对指定故障现象诊断根因类问题，回复True, 其他回复False

用户问题如下：{question}

"""

intent_prompt_en = """
Please judge whether user problems belong to diagnose the problem of returning to specify breakdown phenomenon, generally the user problems contained in the returning | for words.

Response format for Boolean value (true | False) :
If the specified fault phenomenon diagnoses the root cause problem, reply True, other reply False

The user asks the following question: {question}
"""

ALERT_MATCHING_PROMPT = {
    "zh": """
判断用户输入属于下面哪个巡检类别
{inspection}

用户的输入：
-------------
{question}

回复格式指导
-------------
若判定用户输入属于上述某个巡检类别，则直接输出对应巡检类别，否则输出##未匹配成功##

    """,
    "en": """
Determine which of the following type of inspection the user input belongs to
{inspection}

User input:
-------------
{question}

Reply Format Guide
-------------
If the user input is determined to belong to one of the above inspection categories, the corresponding inspection category will be directly output, otherwise the output ## failed to match ##
    """
}

ALERT_MATCHING_PROMPT_EN = """
Determine which of the following type of inspection the user input belongs to
{inspection}

User input:
-------------
{question}

Reply Format Guide
-------------
If the user input is determined to belong to one of the above inspection categories, the corresponding inspection category will be directly output, otherwise the output ## failed to match ##"""

EXTRACT_PARAMS_REPLY_ZH = """
回复格式指导
--------------------
请根据工具的参数集，从用户的问题中提取出相应的参数，用户的问题中可能没有包含全部的参数，能提取到几个就回复几个。请不要捏造用户没有提供的参数。
请按照MARKDOWN格式的代码片段返回dict，如下所示：
```json
{{
   "param1": value1, \\\\提取到的参数1
   "param2": value2, \\\\提取到的参数2
}}
```
如果用户的问题中缺少对应的参数，回复中不要提及该参数。如下例:
如果工具需要3个参数：start_time, end_time, name，但是用户的问题中只提及了开始时间是2024-05-30 00:00:00。则回复的例子如下
```json
{{
   "start_time": "2024-05-30 00:00:00"
}}
```
注意：
1.涉及时间的参数，时间格式为##%Y-%m-%d %H:%M:%S##
2.请不要捏造用户没有提供的参数值
3.参数中要求的格式不要作为回复内容输出！！！用户输入中没有给定该参数不能输出该参数！！
"""

EXTRACT_PARAMS_REPLY_EN = """
"""

EXTRACT_PROMPT = {
    "zh": """"
请从用户给定的输入中抽取出工具所需的如下参数集
-------------
{params}

用户的输入：
-------------
{question}

当前时间为{time}

    """ + EXTRACT_PARAMS_REPLY_ZH,
    "en": """
    """ + EXTRACT_PARAMS_REPLY_EN
}

FORMAT_INSTRUCTIONS_ZH = """回复格式指导
--------------------

当生成回复时，请使用下面两种格式之一进行输出：

**格式 1:**
如果你想使用工具，使用该格式。采用MARKDOWN格式的代码片段：

```json
{
    "action": string, \\\\ 需要执行的工具的名称
    "action_input": dict \\\\ 请根据工具的参数信息从问题中提取参数，用字典表示。如果用户提供的信息缺少参数，不要捏造参数值
}
```

**格式 2:**
如果你想直接对人类生成回复或提示缺少参数，使用该格式。采用MARKDOWN格式的代码片段：

```json
{
    "action": "Final Answer",
    "action_input": string \\\\ 请将你需要返回给人类的回复填充在此处
}
```"""

EXTRACT_TOOL_NAME_ZH = """回复格式指导
--------------------
请根据用户的问题，合理选择最匹配的工具进行使用，无需使用工具直接回复。
如果需要使用工具，请按照MARKDOWN格式的代码片段返回工具的名称，不要返回参数信息，如下所示：
```json
{{{{
   "tool_name": string, \\\\需要使用的工具的名称，必须从{tool_names}中挑选一个
}}}}
```
如果不需要使用工具，请直接输出最终结果
```json
{{{{
   "answer": string, \\\\你的回复
}}}}
```
注意，不要输出任何参数信息
"""

EXTRACT_TOOL_PARAMS_ZH = """
工具的详细信息
--------------------
{tools}
""" + EXTRACT_PARAMS_REPLY_ZH

SYSTEM_PROMPT_ZH = """你是一个极有用的智能助手，可以借助工具回复用户的问题。"""

TOOL_PROMPT_ZH = """可用的工具集信息如下：
--------------------
{tools}
"""

HISTORY_PROMPT_ZH = """
你和用户的对话历史如下：
--------------------
{history}

"""

USER_INPUT_ZH = """
用户的输入
--------------------
如下是用户的输入（请用MARKDOWN格式中的json块的方式进行回复，不要回复其他内容）

{input}
"""

TOOL_INTERACT_THIRD_LLM = """你是一个非常有用的信息提取助手，能够根据工具的描述信息，从用户的问题提取出相应的参数。
时间设定：今年设定为{year}年，今天的日期设定为{date}，当前时间是{current}，{weekday}。
"""

TOOL_RESULT_INTERPRET_SYSTEM_ZH = """你是数据库智能运维助手的呈现环节。请基于工具返回的原始结果，面向用户生成简洁、准确的结论解读：
先直接回答用户的问题，再补充关键数据支撑；只使用原始结果中存在的信息，不要编造；保留重要的数字、状态与操作建议。"""

TOOL_RESULT_INTERPRET_SYSTEM_EN = """You are the presentation stage of a database intelligent O&M assistant. Based on
the raw tool result, produce a concise and accurate interpretation for the user:
answer the user's question directly first, then add key data support; use only information
present in the raw result, never fabricate; keep important numbers, statuses and suggestions."""

TOOL_RESULT_INTERPRET_PROMPT = {
    "zh": """
用户问题：{question}

调用工具：{tool_name}

工具参数：{tool_params}

工具原始结果：
--------------------
{tool_result}
--------------------
请输出面向用户的结论解读。
""",
    "en": """
User question: {question}

Tool used: {tool_name}

Tool arguments: {tool_params}

Raw tool result:
--------------------
{tool_result}
--------------------
Please output the interpretation for the user.
"""
}


def construct_tool_result_interpret_prompt(question, tool_name, tool_params, tool_result, lang='zh') -> str:
    """Build the user prompt for tool result interpretation."""
    template = TOOL_RESULT_INTERPRET_PROMPT.get(lang) or TOOL_RESULT_INTERPRET_PROMPT['zh']
    return template.format(
        question=question,
        tool_name=tool_name,
        tool_params=tool_params if tool_params else 'None',
        tool_result=tool_result
    )


def construct_tool_messages(system_message,
                            user_question,
                            history: str = None,
                            tools_description: str = None,
                            format_instructions: str = None) -> str:
    messages = []
    if history:
        messages.append(HISTORY_PROMPT_ZH.format(history=history))
    messages.append(system_message)
    if tools_description:
        messages.append(TOOL_PROMPT_ZH.format(tools=tools_description))
    if format_instructions:
        messages.append(format_instructions)
    messages.append(USER_INPUT_ZH.format(input=user_question))

    return '\n'.join(messages)


def construct_extract_tool_params_prompt(user_question, tools_description, qa_history, time_args) -> str:
    conversation_list = [
        f'用户: {qa.question}\n助手: {qa.answer}'
        for qa in list(qa_history)
    ]
    history = '\n'.join(conversation_list)
    messages = [HISTORY_PROMPT_ZH.format(history=history)] if history else []

    messages.extend([TOOL_INTERACT_THIRD_LLM.format(**time_args),
                     EXTRACT_TOOL_PARAMS_ZH.format(tools=tools_description),
                     USER_INPUT_ZH.format(input=user_question)])
    return '\n'.join(messages)
