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

"""llm executor"""
import inspect
import json
from datetime import datetime

from GaussMaster import global_vars
from GaussMaster.common.utils.base import adjust_timezone, timer_decorator
from GaussMaster.global_vars import LANGUAGE
from GaussMaster.llms.llm_utils import LLMMsgKey, LLMRole, WEEK_ZH, WEEK_CN, LLMType
from GaussMaster.llms.prompt import TOOL_INTERACT_ZH, TOOL_INTERACT_EN, TOOL_DES_ZH
from GaussMaster.multiagents.agents.prompt.dba_prompt import construct_extract_tool_params_prompt
from GaussMaster.multiagents.mcp_integration import MCPToolAdapter, all_tools
from GaussMaster.multiagents.tools.utils import has_correct_params
from GaussMaster.utils.parse import output_parser


@timer_decorator
async def llm_call(user_prompt, llm, system_prompt: str = None):
    """
    llm invoke entrance
    """
    message_inputs = []
    if system_prompt:
        message_inputs.append({LLMMsgKey.ROLE: LLMRole.SYSTEM, LLMMsgKey.CONTENT: system_prompt})
    message_inputs.append({LLMMsgKey.ROLE: LLMRole.USER, LLMMsgKey.CONTENT: user_prompt})
    response, _ = await llm.invoke(message_inputs)
    return response


@timer_decorator
async def infer_tool_name(question: str, user_id, session_id, llm):
    """
    infer tool_name according to question
    """
    if llm.llm_type == LLMType.PANGUCLOUD:
        _, detail_without_param_dict_list = all_tools.detail_dict_list
        tools_des = '\n'.join([json.dumps(tool_des, ensure_ascii=False) for tool_des in detail_without_param_dict_list])
        tools_des = tools_des.replace('{', '{{').replace('}', '}}')
    else:
        _, detail_without_param_str_list = all_tools.detail_str_list
        tools_des = '\n'.join(detail_without_param_str_list)

    tools_des_prompt = (TOOL_DES_ZH.format(functions=tools_des) if LANGUAGE == 'zh'
                        else TOOL_INTERACT_EN.format(functions=tools_des))
    message_input = [
        {LLMMsgKey.ROLE: LLMRole.SYSTEM, LLMMsgKey.CONTENT: tools_des_prompt},
        {LLMMsgKey.ROLE: LLMRole.USER, LLMMsgKey.CONTENT: question},
    ]
    tool_name = global_vars.SESSION_TOOL_HISTORY.get(user_id, {}).get(session_id, None)
    if tool_name is None:
        tool_name, _ = await llm.invoke(message_input)
        tool_name = tool_name.strip()
    return tool_name


def check_is_no_param_tool(tool_name):
    """
    check whether the tool need params
    :param tool_name: tool name inferred by llm
    :return: 1: is no_param tool 2: response content 3: tool_dict
    """
    target_tool = all_tools.get(tool_name)
    if target_tool and len(target_tool.__param_dict_list__) == 0:
        return True
    return False


def check_has_valid_tool(tool_name):
    """
    check whether the question can be tackled by given tools
    :return: 1. whether the question can be tackled by given tools, 2. target_tool detail description
    """
    target_tool = all_tools.get(tool_name)
    return target_tool is not None


def verify_arguments(function_call: dict = None):
    """
    Check whether the arguments of function_call are correct
    :param function_call: function_call result inferred by llm
    :return:1. whether is correct, 2. correct_params, 3. need_prams
    """
    func_name = function_call.get('name')
    try:
        arguments = json.loads(function_call.get('arguments'))
    except TypeError:
        arguments = function_call.get('arguments')
    func = all_tools.get(func_name)
    if func is None:
        return False, {}, {func_name or 'unknown': False}
    if isinstance(func, MCPToolAdapter):
        return func.has_correct_params(arguments)
    return has_correct_params(arguments, func)


@timer_decorator
async def call_tool(tool_name: str, params=None):
    """
    With the tool_name and params ready, start calling the tool.
    """
    if params is None:
        params = {}
    tool = all_tools.get(tool_name)
    if tool is None:
        return (f"Tool '{tool_name}' is unavailable "
                f"(not found or service connection interrupted), please try again later")
    result = tool(**params)
    if inspect.isawaitable(result):
        result = await result
    return result


@timer_decorator
async def infer_arguments(question, intention_tool, qa_record_history, llm):
    """
    With the tool_name ready, start inferring the parameters of intention_tool
    :param question: user's question
    :param intention_tool: intention tool name
    :param qa_record_history: qa record history
    :return: content_resp, function_call
    """
    target_tool = all_tools.get(intention_tool)
    if target_tool is None:
        return f'Tool {intention_tool} is unavailable (service connection interrupted), please try again later', {}
    if llm.llm_type == LLMType.PANGUCLOUD:
        target_tool_des = json.dumps(target_tool.__detail_with_param_dict__)
        target_tool_des = target_tool_des.replace('{', '{{').replace('}', '}}')
    else:
        target_tool_des = target_tool.__detail_with_param_str__

    tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
    propose_prompt_dict = {
        "year": str(datetime.now(tz).year),
        "date": str(datetime.now(tz).date()),
        "current": str(datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')),
        "weekday": f"星期{WEEK_ZH[datetime.now(tz).weekday()]}" if LANGUAGE == 'zh'
        else WEEK_CN[datetime.now(tz).weekday()]
    }
    if llm.llm_type == LLMType.PANGUCLOUD:
        target_tool_des = '\n'.join([target_tool_des for _ in range(5)])
    if llm.llm_type in [LLMType.PANGUCLOUD, LLMType.PANGU]:
        propose_prompt_dict['functions'] = target_tool_des
        propose_prompt = TOOL_INTERACT_ZH.format(
            **propose_prompt_dict) if LANGUAGE == 'zh' else TOOL_INTERACT_EN.format(
            **propose_prompt_dict)

        if llm.llm_type != LLMType.PANGUCLOUD:
            propose_prompt = propose_prompt.replace("<unused1><unused0>", "")

        message_inputs = [{LLMMsgKey.ROLE: LLMRole.SYSTEM, LLMMsgKey.CONTENT: propose_prompt}]
        history = []
        for qa in qa_record_history:
            question_dict = {LLMMsgKey.ROLE: LLMRole.USER, LLMMsgKey.CONTENT: qa.question}
            history.append(question_dict)
            answer_dict = {LLMMsgKey.ROLE: LLMRole.ASSISTANT, LLMMsgKey.CONTENT: qa.answer}
            if qa.function_call:
                answer_dict[LLMMsgKey.FUNCTION_CALL] = qa.function_call
            history.append(answer_dict)
        message_inputs.extend(history)
        message_inputs.append({LLMMsgKey.ROLE: LLMRole.USER, LLMMsgKey.CONTENT: question})
        content_resp, function_call = await llm.invoke(message_inputs)
    else:
        prompt = construct_extract_tool_params_prompt(
            user_question=question,
            tools_description=target_tool_des,
            qa_history=qa_record_history,
            time_args=propose_prompt_dict
        )
        message_inputs = [{LLMMsgKey.ROLE: LLMRole.USER, LLMMsgKey.CONTENT: prompt}]
        result, _ = await llm.invoke(message_inputs)
        arguments, success = output_parser.parse_params(result)
        if success:
            content_resp = f'将调用工具{intention_tool}'
            params_dict = arguments if isinstance(arguments, dict) else {}
            function_call = {'name': intention_tool, 'arguments': params_dict}
        else:
            content_resp = f'工具参数解析失败，请重新提供参数：{result}'
            function_call = {}
    return content_resp, function_call
