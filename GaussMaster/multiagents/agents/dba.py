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

import json
import time
import uuid
import logging
from datetime import datetime, timedelta

from GaussMaster import global_vars
from GaussMaster.common.metadatabase.dao.dao_interaction_memory import (
    add_to_local_memory,
    select_interaction_memory,
    insert_interaction_memory
)
from GaussMaster.common.metadatabase.schema import InteractionMemory
from GaussMaster.common.security import Encryption
from GaussMaster.common.utils.base import adjust_timezone, stream_exception_catcher
from GaussMaster.llms import llm_registry
from GaussMaster.llms.model_utils import resolve_model_backend, resolve_api_key, resolve_remote_model_name
from GaussMaster.llms.executor import infer_tool_name, check_is_no_param_tool, \
    check_has_valid_tool, call_tool, infer_arguments, verify_arguments
from GaussMaster.llms.llm_utils import InteractionType, LLMMsgKey, LLMRole
from GaussMaster.multiagents.agents.base_agent import BaseAgent
from GaussMaster.multiagents.agents.prompt.dba_prompt import (
    TOOL_RESULT_INTERPRET_SYSTEM_ZH, TOOL_RESULT_INTERPRET_SYSTEM_EN,
    construct_tool_result_interpret_prompt
)
from GaussMaster.server.web.jsonify_utils import sqlalchemy_query_jsonify
from GaussMaster.utils.ui_output_util import formatter_str, DONE_FLAG, formatter_progress

logger = logging.getLogger(__name__)

async def interact(user_id, session_id, query, mode, model_name, history_len=1, lang='zh'):
    """
    unscheduled diagnostic and tool interaction
    :param model_name: large language model name
    :param session_id: session id
    :param user_id: user id
    :param query: user question
    :param mode: choices: ['tool_interaction', 'fault_diagnostic']
    :param history_len: history conversation length
    :param lang: language
    """
    dba = DBA(user_id=user_id, session_id=session_id, question=query, mode=mode, llm_name=model_name,
              history_len=history_len, lang=lang)
    async for step_output in dba.interaction():
        yield step_output
    yield [DONE_FLAG]


def instantiate_llm(model_name: str):
    """instantiate a llm object"""
    llm_config = global_vars.llm_config
    # 初始化llm
    model_params = llm_config.get('online_llm').get(model_name, None)
    if not model_params:
        raise ValueError(f"The model {model_name} is not registered.")

    model_type = resolve_model_backend(model_params)
    api_url = model_params.get('api_url')
    api_key = resolve_api_key(model_params)
    remote_model_name = resolve_remote_model_name(model_name, model_params)
    request_defaults = model_params.get('request_defaults') or model_params.get('recommended_config') or {}
    llm_cls = llm_registry.get(model_type)
    if not llm_cls:
        raise ValueError(f"The model backend {model_type} is not registered.")
    if model_type == 'OpenAICompatible':
        llm = llm_cls(remote_model_name, api_url, api_key=api_key, request_defaults=request_defaults)
    else:
        llm = llm_cls(remote_model_name, api_url, api_key=api_key)
    return llm


class DBA(BaseAgent):

    def __init__(self, question, user_id, session_id, mode, llm_name, history_len, lang):
        self.question = question
        self.mode = mode
        self.embedding = global_vars.embedding_model
        # Thread-level isolation
        self.memory = global_vars.MEMORY
        self.alarm: dict = {}
        self.user_id = user_id
        self.session_id = session_id
        self.llm = instantiate_llm(llm_name)
        self.history_len = history_len
        self.lang = lang

    @stream_exception_catcher
    async def interaction(self):
        # 工具交互场景
        if self.question in ['当前数据库运行状况', '当前有哪些告警']:
            tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
            end_time = datetime.now(tz)
            # 默认收集最近一小时内的告警
            start_time = end_time - timedelta(minutes=60)
            res = await call_tool('summary_alarms', params={
                'start_time': start_time.strftime('%Y-%m-%d %H:%M:%S'),
                'end_time': end_time.strftime('%Y-%m-%d %H:%M:%S')
            })
            yield [formatter_progress('结果解读中...')]
            yield await self.interpret_tool_result('summary_alarms', {
                'start_time': start_time.strftime('%Y-%m-%d %H:%M:%S'),
                'end_time': end_time.strftime('%Y-%m-%d %H:%M:%S')
            }, res)

        elif self.mode == InteractionType.TOOL_INTERACTION:
            async for step in self.interact_with_tool():
                yield step
        elif self.mode == InteractionType.FAULT_DIAGNOSTIC:
            # 开放故障诊断场景
            yield [formatter_str("暂不支持此模式")]

    async def generate_record_and_save(self, answer, function_call: str = None):
        """generate qa record and save to local memory and metadatabase"""
        qa_record = InteractionMemory(
            qa_record_id=str(uuid.uuid4().hex),
            user_id=self.user_id,
            session_id=self.session_id,
            question=self.question,
            answer=answer,
            llm_name=self.llm.name,
            function_call=function_call,
            created_at=int(time.time() * 1000)
        )
        add_to_local_memory(global_vars.SESSION_QA_HISTORY, self.history_len,
                            qa_record)
        await insert_interaction_memory(
            qa_record_id=qa_record.qa_record_id,
            user_id=qa_record.user_id,
            session_id=qa_record.session_id,
            question=Encryption.encrypt(qa_record.question),
            created_at=qa_record.created_at,
            llm_name=qa_record.llm_name,
            answer=Encryption.encrypt(qa_record.answer),
            function_call=Encryption.encrypt(qa_record.function_call) if qa_record.function_call else None
        )

    async def interpret_tool_result(self, tool_name: str, tool_params: dict, tool_result):
        """Generate an LLM interpretation of the tool result."""
        if not isinstance(tool_result, str) or not tool_result.strip():
            return tool_result
        if tool_result.startswith('[Tool execution error]') or 'unavailable' in tool_result:
            return tool_result
        try:
            system_prompt = (TOOL_RESULT_INTERPRET_SYSTEM_EN if self.lang == 'en'
                             else TOOL_RESULT_INTERPRET_SYSTEM_ZH)
            prompt = construct_tool_result_interpret_prompt(
                question=self.question, tool_name=tool_name,
                tool_params=tool_params, tool_result=tool_result, lang=self.lang
            )
            message_inputs = [
                {LLMMsgKey.ROLE: LLMRole.SYSTEM, LLMMsgKey.CONTENT: system_prompt},
                {LLMMsgKey.ROLE: LLMRole.USER, LLMMsgKey.CONTENT: prompt},
            ]
            answer, _ = await self.llm.invoke(message_inputs)
            answer = (answer or '').strip()
            if not answer:
                logger.warning("Tool '%s' result interpretation is empty; falling back to the raw result", tool_name)
                return tool_result
            return answer
        except Exception:
            logger.exception("Tool '%s' result interpretation failed; falling back to the raw result", tool_name)
            return tool_result

    async def interact_with_tool(self):
        """interact with third party tools"""
        # 1. check the session intention
        intention_tool = global_vars.SESSION_TOOL_HISTORY.get(self.user_id, {}).get(self.session_id, None)
        if intention_tool is None:
            # 2. the last intention is done, need to address the user's new intention
            # 2.1 match tool
            yield [formatter_progress('工具匹配中...')]
            matched_tool = await infer_tool_name(self.question, self.user_id, self.session_id, self.llm)
            # 2.2 verify whether the question can be tackled by given tools
            is_valid = check_has_valid_tool(matched_tool)
            if not is_valid:
                content_resp = '用户提问的问题无法用第三方工具解答。'
                await self.save_assistant_resp(content_resp=content_resp)
                yield [formatter_str(content_resp)]
                return
            # 2.3 verify whether matched tool does not need param
            no_need_param = check_is_no_param_tool(matched_tool)
            if no_need_param:
                yield [formatter_progress('工具调用中...')]
                tool_result = await call_tool(matched_tool)
                yield [formatter_progress('结果解读中...')]
                answer = await self.interpret_tool_result(matched_tool, {}, tool_result)
                content_resp = f'将为您调用工具{matched_tool}'
                await self.save_assistant_resp(content_resp=content_resp, tool_name=matched_tool)
                yield answer
                return
            intention_tool = matched_tool
        # 3. the intention is not None, now need to infer arguments
        yield [formatter_progress('提取参数中...')]
        qa_record_history = await self.get_qa_history()
        content_resp, function_call = await infer_arguments(self.question, intention_tool, qa_record_history, self.llm)
        # 4. call the concrete tool
        if function_call:
            is_complete_params, correct_params, need_prams = verify_arguments(function_call)
            if is_complete_params:
                yield [formatter_progress('工具调用中...')]
                tool_result = await call_tool(intention_tool, correct_params)
                yield [formatter_progress('结果解读中...')]
                answer = await self.interpret_tool_result(intention_tool, correct_params, tool_result)
                await self.save_assistant_resp(content_resp=content_resp, tool_name=intention_tool,
                                               tool_params=correct_params)
                yield answer
            else:
                content_resp = f'缺少参数{",".join(list(need_prams.keys()))}， 请一次性提供完整'
                await self.save_assistant_resp(content_resp=content_resp, intent_tool=intention_tool)
                yield [formatter_str(content_resp)]
        else:
            await self.save_assistant_resp(content_resp=content_resp, intent_tool=intention_tool)
            yield [formatter_str(content_resp)]

    async def save_assistant_resp(self, content_resp, intent_tool: str = None, tool_name: str = None,
                                  tool_params: dict = None):
        """
        save assistant answer to meta_database and vector db
        :param intent_tool: intent tool of current dialogue
        :param content_resp: content response
        :param tool_name: tool name of the function_call
        :param tool_params: tool params of the function_call
        """
        global_vars.SESSION_TOOL_HISTORY.update({self.user_id: {self.session_id: intent_tool}})
        if tool_name:
            function_call = {
                'tool_name': tool_name,
                'arguments': tool_params if tool_params else {}
            }
            await self.generate_record_and_save(content_resp, json.dumps(function_call))
            function_call['question'] = self.question
        else:
            await self.generate_record_and_save(content_resp, None)

    async def get_qa_history(self):
        """
        get qa history from local memory or metadatabase
            1. get qa history form local memory
            2. if there is no record in local memory, search from metadatabase
        """
        qa_list = global_vars.SESSION_QA_HISTORY.get(self.user_id, {}).get(self.session_id, [])[:self.history_len]
        if not qa_list:
            raw_qa_records = await select_interaction_memory(self.user_id, self.session_id, self.history_len)
            qa_records = sqlalchemy_query_jsonify(raw_qa_records)
        else:
            return qa_list
        history = []
        header = qa_records.get('header')
        qa_rows = list(reversed(qa_records.get('rows')))
        for qa in qa_rows:
            qa_params = dict(zip(header, qa))
            qa_params.update({
                'question': Encryption.decrypt(qa_params.get('question')),
                'answer': Encryption.decrypt(qa_params.get('answer')),
                'function_call': Encryption.decrypt(qa_params.get('function_call')) if qa_params.get(
                    'function_call') else None
            })
            history.append(InteractionMemory(**qa_params))
        global_vars.SESSION_QA_HISTORY[self.user_id] = {self.session_id: history}
        return history
