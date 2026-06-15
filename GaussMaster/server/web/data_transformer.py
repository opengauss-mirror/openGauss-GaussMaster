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

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from GaussMaster import global_vars
from GaussMaster.common.configs import kb_config
from GaussMaster.common.exceptions import ApiClientException
from GaussMaster.common.http import create_requests_session
from GaussMaster.common.http.dbmind_request import create_new_session, update_session
from GaussMaster.common.http.ssl import get_ssl_context
from GaussMaster.common.metadatabase.dao import gaussdb_vector
from GaussMaster.common.metadatabase.dao.clusters import select_managed_cluster, \
    ClusterNameConflict, update_managed_cluster
from GaussMaster.common.metadatabase.dao.dao_diagnostic_report import select_diagnostic_report
from GaussMaster.common.metadatabase.utils import get_db_instance
from GaussMaster.common.utils.base import adjust_timezone, timer_decorator, validate_filename
from GaussMaster.common.utils.checking import split_ip_port
from GaussMaster.common.utils.cli import check_url_connectivity, check_connectivity_parallely
from GaussMaster.constants import GAUSSMASTER_PATH, VERSION_SUPPORT_LIST, LANGUAGES_SUPPORT_LIST, CUSTOM_KB_PREFIX, \
    REPORT_SUPPORT_LIST, FILE_MAX_SIZE, SUFFIX_SUPPORT_LIST, KB_TYPE_SUPPORT_LIST, SECTION_SAFETY
from GaussMaster.llms.model_utils import resolve_api_key, resolve_remote_model_name
from GaussMaster.llms.llm_utils import InteractionType
from GaussMaster.multiagents.agents.dba import interact
from GaussMaster.multiagents.tools import base_tools
from GaussMaster.multiagents.tools.dbmind_interface import get_cluster_list, fetch_all_alarms, retrieve_cluster_status
from GaussMaster.server.web.context_manager import set_user_current_instance, set_default_llm, set_user_current_llm, \
    current_instance
from GaussMaster.server.web.jsonify_utils import sqlalchemy_query_jsonify
from GaussMaster.utils.prompt_util import get_hyde_prompt, get_query_transform_prompt, get_infer_prompt
from GaussMaster.utils.retriever_util import (
    BaseRetriever,
    DisabledOnlineEmbedding,
    DisabledOnlineReranker,
    OnlineEmbedding,
    OnlineReranker,
)
from GaussMaster.utils.ui_output_util import formatter_divider, formatter_str, formatter_table, formatter_title

ORDER_LIST_PATTERN = r'^\d+\.(\s+)'  # 有序列表
TYPE = 'type'
PROGRESS = 'progress'
DATA = 'data'
RETRIEVER_FAIL_RES = 'Failed to retrieve related knowledge from the knowledge base. ' \
                     'Answers cannot be directly generated. ' \
                     'Check whether the question is related to the content scope of the knowledge base.'


def register_cluster(cluster):
    cluster_name = cluster.cluster_name
    host = cluster.host
    port = cluster.port
    username = cluster.username
    password = cluster.password

    instance = f'{host}:{port}'
    clusters_dict = get_cluster_list()
    if instance in clusters_dict:
        session_instance = create_new_session(instance, username, password)
        try:
            session_instance.login()
        except ValueError:
            return {"status": 503, "msg": "无法连接到此集群"}
        try:
            update_managed_cluster(cluster_name, clusters_dict, instance, password, username)
        except ClusterNameConflict as e:
            return {"status": 1002, "msg": "该集群名已被占用"}
        update_session(instance, session_instance)
        return {"status": 0, "msg": "注册成功"}
    return {"status": 1001, "msg": "dbmind没有纳管此集群，无法注册"}


def transfer_record_to_dict(header, row):
    """
    transfer record to dict
    """
    if row is None:
        return {}
    if len(header) != len(row):
        raise ValueError('header and row must have same length')
    return dict(zip(header, row))


async def get_history_report(current, pagesize, user_id=None, **kwargs) -> list:
    """get history report overview"""
    offset = max(0, (current - 1) * pagesize)
    reports_record = await select_diagnostic_report(all_column=False, user_id=user_id, offset=offset, limit=pagesize,
                                                    **kwargs)
    reports_detail = sqlalchemy_query_jsonify(reports_record)
    reports_list = []
    header = reports_detail.get('header', [])
    for item in reports_detail.get('rows', []):
        reports_list.append(transfer_record_to_dict(header, item))
    return reports_list


async def diagnostic_replay(report_id):
    """replay diagnostic procedure"""
    report_record = await select_diagnostic_report(all_column=True, report_id=report_id, limit=1)
    report_detail = sqlalchemy_query_jsonify(report_record)
    if len(report_detail.get('rows', [])) == 0:
        return {}
    return transfer_record_to_dict(report_detail.get('header', []), report_detail.get('rows', [])[0])


def update_llm(model_name: str, user_id: str = None, session_id: str = None):
    """initialize llm"""
    if not user_id and not session_id:
        set_default_llm(model_name)
    else:
        global_vars.user_session_llm[user_id][session_id] = model_name
        set_user_current_llm(user_id, model_name)


def get_all_models() -> dict:
    """get all models"""
    llms = defaultdict(list)
    llms['online'] = get_all_available_online_llms()
    llms['local'] = []
    return llms


def _embedding_reranker_service_ready(cfg: dict) -> bool:
    if not cfg.get("enable", True):
        return False
    url = (cfg.get("api_url") or "").strip()
    if not url or "*" in url:
        return False
    return True


def initialize_embedding_model():
    """
    initialize embedding and reranker model
    """
    embedding_config = global_vars.llm_config.get("embedding_model", {})
    reranker_config = global_vars.llm_config.get("reranker_model", {})
    embedding_model_api_url = embedding_config.get("api_url")
    reranker_model_api_url = reranker_config.get("api_url")
    embedding_api_key = resolve_api_key(embedding_config)
    reranker_api_key = resolve_api_key(reranker_config)
    connectivity_params = []
    ssl_ctx = get_ssl_context()
    for url, model_name, model_class, api_key, model_cfg, disabled_cls in zip(
        [embedding_model_api_url, reranker_model_api_url],
        ["embedding_model", "reranker_model"],
        [OnlineEmbedding, OnlineReranker],
        [embedding_api_key, reranker_api_key],
        [embedding_config, reranker_config],
        [DisabledOnlineEmbedding, DisabledOnlineReranker],
    ):
        if not _embedding_reranker_service_ready(model_cfg):
            setattr(global_vars, model_name, disabled_cls(None, ssl_ctx, api_key=api_key))
            continue
        setattr(global_vars, model_name, model_class(url, ssl_ctx, api_key=api_key))
        connectivity_params.append((url, ssl_ctx, model_name, api_key, model_cfg))
    if not connectivity_params:
        return True
    return check_connectivity_parallely(connectivity_params, True)


def get_all_available_online_llms(terminal_output=False) -> list:
    """get all available online llms Concurrently"""
    llm_candidates_params = []
    for llm_name, detail in global_vars.llm_config.get('online_llm').items():
        if detail.get('enable'):
            api_url = detail.get('api_url')
            model_config = {**detail}
            model_config['model_name'] = resolve_remote_model_name(llm_name, detail)
            llm_candidates_params.append(
                (api_url, get_ssl_context(), llm_name, resolve_api_key(detail), model_config)
            )
    available_llms = check_connectivity_parallely(llm_candidates_params, terminal_output)
    return available_llms


def switch_llm(name, user_id, session_id):
    """switch large language model"""
    try:
        if name not in global_vars.llm_config.get('online_llm').keys():
            raise ValueError('LLM name is not correct.')
        if not global_vars.llm_config.get('online_llm').get(name).get('enable'):
            raise ValueError(f'The LLM service of {name} is not available.')
        model_config = global_vars.llm_config.get('online_llm').get(name)
        api_url = model_config.get('api_url')
        api_key = resolve_api_key(model_config)
        validate_config = {**model_config}
        validate_config['model_name'] = resolve_remote_model_name(name, model_config)
        is_valid, _, msg = check_url_connectivity(api_url, get_ssl_context(), name, api_key, validate_config)
        if not is_valid:
            logging.error(msg)
            return False
        update_llm(model_name=name, user_id=user_id, session_id=session_id)
    except Exception as e:
        logging.error('Failed to switch llm, because %s', str(e))
        return False
    logging.info("The llm has been switched successfully by user %s - %s", user_id, session_id)
    return True


# 获取检索结果：向量检索+文本检索+重排序
async def search(question, user_id, session_id, vector_topk, text_topk, rerank_topk, kb_id, version, lang, history_len):
    """Function retriver result for question."""
    if not question:
        raise ValueError('question can not be empty.')
    if not user_id:
        raise ValueError('user_id can not be empty.')
    if not session_id:
        raise ValueError('session_id can not be empty.')
    if vector_topk < 1 or vector_topk > 10:
        raise ValueError('Incorrect value for parameter vector_topk, should be in [1, 10].')
    if text_topk < 1 or text_topk > 10:
        raise ValueError('Incorrect value for parameter text_topk, should be in [1, 10].')
    if rerank_topk < 1 or rerank_topk > 10:
        raise ValueError('Incorrect value for parameter rerank_topk, should be in [1, 10].')
    if version not in VERSION_SUPPORT_LIST:
        raise ValueError(f'Incorrect value for parameter version, should be in {VERSION_SUPPORT_LIST}.')
    if lang not in LANGUAGES_SUPPORT_LIST:
        raise ValueError(f'Incorrect value for parameter lang, should be in {LANGUAGES_SUPPORT_LIST}.')
    if history_len < 0 or history_len > 3:
        raise ValueError('Incorrect value for parameter history_len, should be in [0, 3].')
    try:
        if kb_id == 0:
            table_name = kb_config.KT_TABLE_NAME + "_" + lang
        else:
            table_name = CUSTOM_KB_PREFIX + str(kb_id)
            version = ""
        gaussdb = get_db_instance(table_name, kb_config.KT_TABLE_CONFIG)
        retriever = BaseRetriever(gaussdb, global_vars.reranker_model)
        column_list = gaussdb.cols
        start_time = time.time()
        vector_result = await retriever.search_vector_result_gaussdb(question, vector_topk, version)
        vector_time = time.time()
        text_result = retriever.search_text_result_gaussdb(question, text_topk, version)
        text_time = time.time()
        reranker_scores, reranker_result = await retriever.reranker_search_result(question, vector_result, text_result,
                                                                                  rerank_topk)
        reranker_time = time.time()
        search_res = []
        cur_index = 0
        for index, answer in enumerate(reranker_result):
            knowledge_dict = {}
            knowledge_dict['knowledge_id'] = answer[column_list.index('uuid')]
            knowledge_dict['content'] = answer[column_list.index('text')]
            knowledge_dict['field'] = answer[column_list.index('field')]
            knowledge_dict['sub_field'] = answer[column_list.index('sub_field')]
            knowledge_dict['source'] = answer[column_list.index('source')]
            knowledge_dict['version'] = answer[column_list.index('version')]
            knowledge_dict['product_format'] = answer[column_list.index('product_format')]
            knowledge_dict['doc_location'] = answer[column_list.index('doc_location')]
            knowledge_dict['title'] = answer[column_list.index('title')]
            knowledge_dict['visualize'] = answer[column_list.index('visualize')]
            knowledge_dict['link'] = answer[column_list.index('link')]
            knowledge_dict['context'] = answer[column_list.index('context')]
            knowledge_dict['keyword'] = answer[column_list.index('keyword')]
            knowledge_dict['confidence'] = answer[column_list.index('confidence')]
            knowledge_dict['Top No.'] = cur_index
            knowledge_dict['score'] = reranker_scores[index]
            knowledge_dict['media'] = ""
            search_res.append(knowledge_dict)
            cur_index += 1
            if cur_index >= rerank_topk:
                break
        total_time = time.time()
        res_dict = {}
        res_dict['search_res'] = search_res
        res_dict['vector_search_time'] = round(vector_time - start_time, 6)
        res_dict['text_search_time'] = round(text_time - vector_time, 6)
        res_dict['rerank_search_time'] = round(reranker_time - text_time, 6)
        res_dict['total_time'] = round(total_time - start_time, 6)
        question_id = str(uuid.uuid4())
        res_dict['question_id'] = question_id
        return res_dict
    except Exception as e:
        raise Exception(f'can not get search result, because: {e}.') from e


# 模型推理
async def infer(question, question_id, user_id, session_id, switch, model_name, lang, history_len, model_config,
                search_res):
    """Function generate result from question and context."""
    if not question:
        raise ValueError('question can not be empty.')
    if not question_id:
        raise ValueError('question_id can not be empty.')
    if not user_id:
        raise ValueError('user_id can not be empty.')
    if not session_id:
        raise ValueError('session_id can not be empty.')
    if not switch:
        raise ValueError(
            'GaussMaster currently does not support answering directly through LLM, please set param switch to True.')
    if not global_vars.llm_config.get('online_llm').get(model_name):
        raise ValueError('model_name is not correct.')
    if lang not in LANGUAGES_SUPPORT_LIST:
        raise ValueError(f'Incorrect value for parameter lang, should be in {LANGUAGES_SUPPORT_LIST}.')
    if history_len < 0 or history_len > 3:
        raise ValueError('Incorrect value for parameter history_len, should be in [0, 3].')
    start_time = time.time()
    answer_id = str(uuid.uuid4())
    qa_dict = {}
    qa_dict['question_id'] = question_id
    qa_dict['question'] = question
    qa_dict['answer_id'] = answer_id
    qa_dict['user_id'] = user_id
    qa_dict['session_id'] = session_id
    qa_dict['switch'] = switch
    qa_dict['model_name'] = model_name
    qa_dict['model_config'] = model_config
    qa_dict['lang'] = lang
    qa_dict['task_type'] = 'QA'
    qa_dict['like'] = 0
    qa_dict['hate'] = 0
    tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
    create_time = str(datetime.now(tz))
    qa_dict['create_time'] = create_time
    qa_dict['update_time'] = create_time
    answer_list = []
    answer = ""
    try:
        history = get_history_chat(user_id, session_id, history_len)
        status, messages = create_infer_prompt(question, search_res, history, lang)
        if not status:
            yield messages[0]
            return
        async for item in generate_answer(messages, model_name):
            answer += item
            yield {'type': 'answer', 'data': answer}
        end_time = time.time()
        yield {'type': 'complete', 'data': {'time': round(end_time - start_time, 6), 'answer_id': answer_id}}
        answer_list.append({'type': 'answer', 'data': answer})
        answer_list.append(
            {'type': 'complete', 'data': {'time': round(end_time - start_time, 6), 'answer_id': answer_id}})
        qa_dict['answer'] = answer_list
    except Exception as e:
        raise Exception(f'can not get llm result, because: {e}.') from e
    finally:
        try:
            gaussdb_vector.insert_qa_record(qa_dict)
        except Exception:
            logging.warning('insert_qa_record failed (metadatabase may be offline).', exc_info=True)


def create_infer_prompt_direct(question, search_res, history, lang):
    """Function create infer prompt direct."""
    context_list = []
    for search_dict in search_res:
        context_list.append(search_dict['content'])

    messages = []
    if not context_list:
        if lang == "zh":
            messages.append({'type': 'answer',
                             'data': '无法从知识库中检索到相关知识，暂不支持直接生成答案，请确认问题与知识库内容范围是否相关'})
        elif lang == "en":
            messages.append({'type': 'answer',
                             'data': RETRIEVER_FAIL_RES})
        else:
            messages.append({'type': 'answer', 'data': '暂不支持当前语言，请确认lang参数是否正确'})
        return False, messages

    messages = get_infer_prompt(question, context_list, history, lang)
    if not messages:
        messages.append({'type': 'answer', 'data': '提示构造失败，请确认查询/上下文是否过长！'})
        return False, messages
    return True, messages


def create_infer_prompt(question, search_res, history, lang):
    """Function create infer prompt."""
    messages = []
    if not search_res:
        if lang == "zh":
            messages.append({'type': 'answer',
                             'data': '无法从知识库中检索到相关知识，暂不支持直接生成答案，请确认问题与知识库内容范围是否相关'})
        elif lang == "en":
            messages.append({'type': 'answer',
                             'data': RETRIEVER_FAIL_RES})
        else:
            messages.append({'type': 'answer', 'data': '暂不支持当前语言，请确认lang参数是否正确'})
        return False, messages
    context_list = []
    for search_dict in search_res:
        context_list.append(search_dict['content'])
    messages = get_infer_prompt(question, context_list, history, lang)
    if not messages:
        messages.append({'type': 'answer', 'data': '提示构造失败，请确认查询/上下文是否过长！'})
        return False, messages
    return True, messages


@timer_decorator
def get_history_chat(user_id, session_id, history_len):
    """Function get history chat"""
    try:
        gaussdb = get_db_instance(kb_config.QA_TABLE_NAME, kb_config.QA_TABLE_CONFIG)
        column_list = gaussdb.cols
        history_list = gaussdb.get_history_record(user_id, session_id, history_len)
        history = []
        for history_info in history_list:
            history_dict = {}
            history_dict['question'] = history_info[column_list.index('question')]
            history_dict['answer'] = history_info[column_list.index('answer')]
            history_dict['lang'] = history_info[column_list.index('lang')]
            history.append(history_dict)
        return history
    except Exception:
        logging.warning('get_history_chat failed; continuing without chat history.', exc_info=True)
        return []


def request_from_llm(url, headers, params):
    """Function request from llm."""
    try:
        with create_requests_session(ssl_context=get_ssl_context()) as session:
            with session.request(method="POST", url=url, headers=headers, json=params,
                                 stream=True) as response:
                if response.status_code >= 400:
                    text = response.text[:500] if response.text else ''
                    raise Exception(
                        f'LLM HTTP {response.status_code}: {text}'
                    )
                for chunk in response.iter_content(decode_unicode=True):
                    yield chunk
    except Exception as e:
        raise Exception(f'can not request from llm, because: {e}.') from e


async def thread_request_from_llm(url, headers, params):
    """Function thread request from llm."""
    try:
        with ThreadPoolExecutor() as pool:
            result = await asyncio.get_running_loop().run_in_executor(pool, request_from_llm, url, headers, params)
            return result
    except Exception as e:
        raise Exception(f'can not request from llm in thread, because: {e}.') from e


def iter_next(generator):
    """Function iter next."""
    try:
        return next(generator)
    except StopIteration:
        return -1


def _sse_line_to_delta_text(line: str) -> str:
    """Parse one SSE line (OpenAI-compatible) and return delta/message text, or ''."""
    line = line.strip()
    if not line or not line.startswith('data:'):
        return ''
    payload = line[5:].strip()
    if payload == '[DONE]':
        return ''
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return ''
    choices = data.get('choices') or []
    if not choices:
        return ''
    delta = choices[0].get('delta')
    if isinstance(delta, dict) and delta.get('content'):
        return delta.get('content')
    message = choices[0].get('message')
    if isinstance(message, dict) and message.get('content'):
        return message.get('content')
    return ''


def _iter_sse_delta_texts(body_chunk: str, line_buf: list) -> list:
    """
    Append body_chunk to line_buf[0], split on newlines, return delta texts for complete lines.
    Incomplete tail remains in line_buf[0].
    """
    if not isinstance(body_chunk, str):
        body_chunk = body_chunk.decode('utf-8', errors='replace') if isinstance(body_chunk, (bytes, bytearray)) else str(
            body_chunk)
    line_buf[0] += body_chunk
    out = []
    while '\n' in line_buf[0]:
        line, _, rest = line_buf[0].partition('\n')
        line_buf[0] = rest
        t = _sse_line_to_delta_text(line)
        if t:
            out.append(t)
    return out


async def generate_answer(messages, model_name):
    """Function generate answer."""
    if global_vars.local_llm:
        async for item in global_vars.local_llm.invoke(messages):
            yield item
    else:
        llm_config = global_vars.llm_config.get('online_llm').get(model_name, {})
        request_defaults = llm_config.get('request_defaults') or llm_config.get('recommended_config') or {}
        params = {
            'model': resolve_remote_model_name(model_name, llm_config),
            'messages': messages
        }
        params.update(request_defaults)
        params.setdefault('stream', True)
        headers = {"Accept": "text/event-stream", "Connection": "keep-alive", "Cache-Control": "no-cache"}
        api_key = resolve_api_key(llm_config)
        key_optional = bool(llm_config.get('api_key_optional'))
        if not api_key and not key_optional:
            env_hint = llm_config.get('api_key_env') or 'LLM_API_KEY'
            raise ApiClientException(
                f"{model_name}: API key missing. Set environment variable {env_hint}, "
                f"or create a .env file in the GaussMaster config directory with that key, "
                f"or set api_key_optional: true for local OpenAI-compatible servers without auth."
            )
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

        url = llm_config.get('api_url')
        try:
            llm_generator = await thread_request_from_llm(url, headers, params)
            line_buf = ['']
            while True:
                chunk = await asyncio.get_running_loop().run_in_executor(None, iter_next, llm_generator)
                if chunk == -1:
                    break
                for text in _iter_sse_delta_texts(chunk, line_buf):
                    yield text
            if line_buf[0].strip():
                tail = _sse_line_to_delta_text(line_buf[0])
                if tail:
                    yield tail
        except Exception as e:
            raise Exception(f'can not generate answer, because: {str(e)}.') from e


# QA v1
# 进行敏感词检测，判断用户问题是否包含敏感的关键词
# 直接检索，有结果交由大模型判断是否敏感，敏感拒答，不敏感直接回答
# 无结果，进行查询优化，重新检索，有结果与上述过程相同
# 无结果，直接返回知识库中没有相关知识
# v2 后续验证效果
# 先直接检索，有结果且大模型认为相关，直接返回回答
# 无结果/大模型认为不相关，进行查询转换、假设性回答，重新检索，将上下文与前面的上下文（如果有）一起传给LLM，相关则直接回答
# 还没有结果/不相关，返回知识库中没有相关知识，并去FAQ库检索相关问题，给出推荐问题
async def ask_gauss(question, user_id, session_id, switch, vector_topk, text_topk, rerank_topk, kb_id, version,
                    model_name, lang, history_len, model_config):
    """Function e2e qa process."""
    if not question:
        raise ValueError('question can not be empty.')
    if not user_id:
        raise ValueError('user_id can not be empty.')
    if not session_id:
        raise ValueError('session_id can not be empty.')
    if not switch:
        raise ValueError(
            'GaussMaster currently does not support answering directly through LLM, please set param switch to True.')
    if vector_topk < 1 or vector_topk > 10:
        raise ValueError('Incorrect value for parameter vector_topk, should be in [1, 10].')
    if text_topk < 1 or text_topk > 10:
        raise ValueError('Incorrect value for parameter text_topk, should be in [1, 10].')
    if rerank_topk < 1 or rerank_topk > 10:
        raise ValueError('Incorrect value for parameter rerank_topk, should be in [1, 10].')
    if version not in VERSION_SUPPORT_LIST:
        raise ValueError(f'Incorrect value for parameter version, should be in {VERSION_SUPPORT_LIST}.')
    if not global_vars.llm_config.get('online_llm').get(model_name):
        raise ValueError('model_name is not correct.')
    if lang not in LANGUAGES_SUPPORT_LIST:
        raise ValueError(f'Incorrect value for parameter lang, should be in {LANGUAGES_SUPPORT_LIST}.')
    if history_len < 0 or history_len > 3:
        raise ValueError('Incorrect value for parameter history_len, should be in [0, 3].')

    # 判断用户问题是否包含敏感的关键词
    if (global_vars.configs.get(SECTION_SAFETY, 'safety_check').strip().upper() == 'TRUE'
            and global_vars.DFA_DETECTOR.is_unsafe_text(question)):

        if lang == "zh":
            security_warn_zh = '作为一个 GaussDB 专家，我无法回答与 GaussDB 无关的安全敏感话题！'
            yield {'type': 'answer', 'data': security_warn_zh}
        elif lang == "en":
            security_warn_en = "As a GaussDB expert, I can't answer security sensitive topics \
                               that have nothing to do with GaussDB!"
            yield {'type': 'answer', 'data': security_warn_en}

        yield yield_progress_message('答案生成完成', 'Answer generation complete', lang)

    else:
        start_time = time.time()
        answer_id = str(uuid.uuid4())
        qa_dict = {}
        qa_dict['question'] = question
        qa_dict['answer_id'] = answer_id
        qa_dict['user_id'] = user_id
        qa_dict['session_id'] = session_id
        qa_dict['switch'] = switch
        qa_dict['vector_topk'] = vector_topk
        qa_dict['text_topk'] = text_topk
        qa_dict['rerank_topk'] = rerank_topk
        qa_dict['kb_id'] = kb_id
        qa_dict['version'] = version
        qa_dict['model_name'] = model_name
        qa_dict['model_config'] = model_config
        qa_dict['lang'] = lang
        qa_dict['task_type'] = 'QA'
        qa_dict['like'] = 0
        qa_dict['hate'] = 0
        tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
        create_time = str(datetime.now(tz))
        qa_dict['create_time'] = create_time
        qa_dict['update_time'] = create_time

        # 直接检索
        yield yield_progress_message('问题检索中...', 'Retrieving relevant knowledges...', lang)
        res_dict = await search(question, user_id, session_id, vector_topk, text_topk,
                                rerank_topk, kb_id, version, lang, history_len)
        question_id = res_dict['question_id']
        qa_dict['question_id'] = question_id

        # 判断
        try:
            history = get_history_chat(user_id, session_id, history_len)
            search_res = res_dict['search_res']
            answer_list = []
            # 无检索结果，进入查询优化阶段，生成最终结果
            answer_statu = False
            if not search_res:
                async for item in query_opt_process(question, user_id, session_id, vector_topk, text_topk, rerank_topk,
                                                    kb_id, version, lang, history_len, history, model_name):
                    if item['type'] == 'answer':
                        if not answer_statu:
                            answer_list.append(item)
                            answer_statu = True
                        else:
                            answer_list[-1] = item
                    else:
                        answer_list.append(item)
                    yield item
            else:
                async for item in llm_generation(question, search_res, model_name, history, lang):
                    if item['type'] == 'answer':
                        if not answer_statu:
                            answer_list.append(item)
                            answer_statu = True
                        else:
                            answer_list[-1] = item
                    else:
                        answer_list.append(item)
                    yield item
            end_time = time.time()
            complete_dict = {
                'type': 'complete',
                'data': {
                    'time': round(end_time - start_time, 6),
                    'question_id': question_id,
                    'answer_id': answer_id
                }
            }
            yield complete_dict
            answer_list.append(complete_dict)
            qa_dict['answer'] = answer_list
        except Exception as e:
            raise Exception(f'can not get gauss qa result, because: {e}.')
        finally:
            try:
                gaussdb_vector.insert_qa_record(qa_dict)
            except Exception:
                logging.warning('insert_qa_record failed (metadatabase may be offline).', exc_info=True)


async def llm_generation(question, search_res, model_name, history, lang):
    """Function llm generation."""
    yield {'type': 'refrences', 'data': json.dumps(search_res, ensure_ascii=False)}
    yield yield_progress_message('检索完成', 'Retrival complete', lang)
    status, messages = create_infer_prompt_direct(question, search_res, history, lang)

    yield yield_progress_message('答案生成中...', 'Generating answer...', lang)
    if not status:
        yield messages[0]
        yield yield_progress_message('答案生成完成', 'Answer generation complete', lang)
        return
    answer = ""
    async for item in generate_answer(messages, model_name):
        answer += item
        yield {'type': 'answer', 'data': answer}
    yield yield_progress_message('答案生成完成', 'Answer generation complete', lang)


# 根据语言生成返回信息
def yield_progress_message(zh_message: str, en_message: str, lang):
    """Function generating progress message."""
    if lang == "en":
        return {'type': 'progress', 'data': en_message}
    # 默认中文。如果其它语言也回复中文
    return {'type': 'progress', 'data': zh_message}


async def query_opt_process(question, user_id, session_id, vector_topk, text_topk, rerank_topk,
                            kb_id, version, lang, history_len, history, model_name):
    """Function query opt process."""
    yield yield_progress_message('知识库无相关知识', 'No relevant knowledge found', lang)
    yield yield_progress_message('进行查询优化', 'Optimizing the query for better retrieval', lang)
    query_list = []
    hyde_messages = get_hyde_prompt(question, [], lang)
    if not hyde_messages:
        msg_zh = '假设性提问构造失败，请确认查询是否过长！'
        msg_en = "Hypothetical question construction failed. Please make sure your question is not too long."
        yield yield_progress_message(msg_zh, msg_en, lang)
    hyde_result = ""
    start_time = time.time()
    async for item in generate_answer(hyde_messages, model_name):
        hyde_result += item
    if hyde_result:
        query_list.append(hyde_result)
    yield yield_progress_message('假设性回答生成完成', 'Hypothetical answer generation completed.', lang)
    hyde_time = time.time()
    logging.info("Finished 'Hyde' in %f secs", round(hyde_time - start_time, 6))
    query_trans_messages = get_query_transform_prompt(question, [], lang)
    if not query_trans_messages:
        msg_zh = '查询改写构造失败，请确认查询是否过长！'
        msg_en = "Query transformation construction failed! Please make sure your question is not too long."
        yield yield_progress_message(msg_zh, msg_en, lang)
    query_trans_result = ""
    async for item in generate_answer(query_trans_messages, model_name):
        query_trans_result += item
    query_trans_results = query_trans_result.split('\n')
    for sub_query in query_trans_results:
        query_match = re.search(ORDER_LIST_PATTERN, sub_query)
        if not query_match:
            continue
        query_list.append(sub_query[query_match.span()[1]:].strip())
    query_trans_time = time.time()
    logging.info("Finished 'query_trans' in %f secs", round(query_trans_time - hyde_time, 6))
    if not query_list:
        msg_zh = f'查询优化后无法找到相关查询'
        msg_en = f'No relevant queries found after query transformation.'
        yield yield_progress_message(msg_zh, msg_en, lang)
        return
    msg_zh = f'查询优化后相关问题生成完成，数量为{len(query_list)}'
    msg_en = f'Query optimization completed. Found {len(query_list)} relevant questions'
    yield yield_progress_message(msg_zh, msg_en, lang)
    score_list = []
    res_list = []
    msg_zh = '查询优化检索中，耗时相对较长，请等待...'
    msg_en = 'Retrieving optimized queries, this may take long, please wait...'
    yield yield_progress_message(msg_zh, msg_en, lang)
    for query in query_list:
        res_dict = await search(query, user_id, session_id, vector_topk, text_topk, rerank_topk, kb_id, version, lang,
                                history_len)
        search_res = res_dict.get('search_res', [])
        for answer in search_res:
            score_list.append(answer['score'])
            res_list.append(answer)
    msg_zh = f'查询优化检索完成，相关知识数量为{len(res_list)}'
    msg_en = f'Query optimization completed. Found {len(res_list)} relevant paragraphs'
    yield yield_progress_message(msg_zh, msg_en, lang)
    opt_search_time = time.time()
    logging.info("Finished 'opt_search' in %f secs", round(opt_search_time - query_trans_time, 6))

    sorted_list = []
    if not res_list:
        if lang == "zh":
            ans_zh = '无法从知识库中检索到相关知识，暂不支持直接生成答案，请确认问题与知识库内容范围是否相关'
            yield {'type': 'answer', 'data': ans_zh}
        elif lang == "en":
            yield {'type': 'answer', 'data': RETRIEVER_FAIL_RES}
        yield yield_progress_message('答案生成完成', 'Answer generation complete', lang)
    else:
        zipped = zip(score_list, res_list)
        sort_zipped = sorted(zipped, key=lambda x: x[0], reverse=True)
        sort_result = zip(*sort_zipped)
        _, sorted_list = [list(x) for x in sort_result]
        sorted_list = sorted_list[:rerank_topk]
    yield yield_progress_message('开始生成答案', 'Start generating answer', lang)
    async for item in llm_generation(question, sorted_list, model_name, history, lang):
        yield item
    end_time = time.time()
    logging.info("Finished 'generate_answer' in %f secs", round(end_time - opt_search_time, 6))


# 获取前端反馈信息，插入/更新记录
def update_like_info(answer_id):
    """Function update like."""
    if not answer_id:
        raise ValueError('Incorrect value for parameter answer_id: {}.'.format(answer_id))
    try:
        gaussdb_vector.update_qa_record(answer_id, update_type=0)
        return 'feedback like success.'
    except Exception as e:
        raise Exception(f'feedback like failed, because: {e}.') from e


def update_hate_info(answer_id):
    """Function update hate."""
    if not answer_id:
        raise ValueError('Incorrect value for parameter answer_id: {}.'.format(answer_id))
    try:
        gaussdb_vector.update_qa_record(answer_id, update_type=1)
        return 'feedback hate success.'
    except Exception as e:
        raise Exception(f'feedback hate failed, because: {e}.') from e


def update_feedback_info(answer_id, feedback_info):
    """Function update feedback."""
    if not answer_id:
        raise ValueError('Incorrect value for parameter answer_id: {}.'.format(answer_id))
    if not feedback_info:
        raise ValueError('Incorrect value for parameter feedback_info: {}.'.format(feedback_info))
    try:
        gaussdb_vector.update_qa_record(answer_id, update_type=2, update_info=feedback_info)
        return 'feedback info success.'
    except Exception as e:
        raise Exception(f'feedback info failed, because: {e}.') from e


def update_report_info(answer_id, report_type, report_info):
    """Function update report."""
    if not answer_id:
        raise ValueError('Incorrect value for parameter answer_id: {}.'.format(answer_id))
    if not report_type:
        raise ValueError('Incorrect value for parameter report_type: {}.'.format(report_type))
    if report_type not in REPORT_SUPPORT_LIST:
        raise ValueError(f'Incorrect value for parameter report_type, should be in {REPORT_SUPPORT_LIST}.')
    if not report_info:
        raise ValueError('Incorrect value for parameter report_info: {}.'.format(report_info))
    try:
        gaussdb_vector.update_qa_record(answer_id, update_type=3, report_type=report_type, update_info=report_info)
        return 'feedback report success.'
    except Exception as e:
        raise Exception(f'feedback report failed, because: {e}.') from e


# 获取用户下特定知识库信息
def get_knowledge_info(kb_id, user_id):
    """Function getting knowledge base info."""
    if not kb_id:
        raise ValueError('Incorrect value for parameter kb_id: {}.'.format(kb_id))
    if not user_id:
        raise ValueError('Incorrect value for parameter user_id: {}.'.format(user_id))
    try:
        return gaussdb_vector.get_knowledge_info(kb_id, user_id)
    except Exception as e:
        raise Exception(f'get_knowledge_info failed, because: {e}.') from e


# 为指定用户添加知识库信息，同时创建对应表格，保证原子操作
@timer_decorator
async def add_knowledge(name, user_id, file, kb_type, description, context):
    """Function adding knowledge base info."""
    if not name:
        raise ValueError('Incorrect value for parameter name: {}.'.format(name))
    if not user_id:
        raise ValueError('Incorrect value for parameter user_id: {}.'.format(user_id))
    if not file:
        raise ValueError('Incorrect value for parameter file: {}.'.format(file))
    if len(file) > 10:
        raise ValueError('file num exceeds 10.')
    if kb_type not in KB_TYPE_SUPPORT_LIST:
        raise ValueError(f'Unsupported kb type, should be in {KB_TYPE_SUPPORT_LIST}.')
    try:
        create_time = str(datetime.now(adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))))
        record_dict = {
            'name': name,
            'user_id': user_id,
            'kb_type': kb_type,
            'description': description,
            'context': context,
            'create_time': create_time,
            'update_time': create_time
        }
        file_list = []
        file_size = 0
        for file_content in file:
            file_dict = {}
            contents = []
            while True:
                chunk = await file_content.read(1024 * 1024)
                if not chunk:
                    break
                contents.append(chunk)
                if len(contents) > FILE_MAX_SIZE / 1024 / 1024:
                    raise ValueError(f'file size exceeds {FILE_MAX_SIZE} B.')
            file_dict['content'] = b''.join(contents)
            file_dict['filename'] = validate_filename(file_content.filename)
            file_extension = os.path.splitext(file_dict['filename'])[1][1:]
            if file_extension not in SUFFIX_SUPPORT_LIST:
                raise ValueError(f'Unsupported file type, should be in {SUFFIX_SUPPORT_LIST}.')
            if len(file_dict['content']) == 0:
                raise ValueError(f'{file_dict["filename"]} file size is 0 B, the file cannot be empty.')
            file_size += len(file_dict['content'])
            file_list.append(file_dict)
        if file_size > FILE_MAX_SIZE:
            raise ValueError(f'file size exceeds {FILE_MAX_SIZE} B.')
        await gaussdb_vector.add_knowledge(record_dict, file_list, os.path.join(GAUSSMASTER_PATH, 'knowledge_base'))
        return 'add knowledge success.'
    except Exception as e:
        raise Exception(f'add_knowledge failed in stage add knowledge, because: {e}.') from e


# 更新知识库信息，仅限名称、描述、上下文
def update_knowledge(kb_id, name, user_id, description, context):
    """Function updating knowledge base info."""
    if not kb_id:
        raise ValueError('Incorrect value for parameter kb_id: {}.'.format(kb_id))
    if not user_id:
        raise ValueError('Incorrect value for parameter user_id: {}.'.format(user_id))
    if not name:
        raise ValueError('Incorrect value for parameter name: {}.'.format(name))
    try:
        update_res = gaussdb_vector.update_knowledge_info(kb_id, name, user_id, description, context)
    except Exception as e:
        raise Exception(f'update_knowledge failed, because: {e}.') from e
    if len(update_res) == 1 and len(update_res[0]) == 1 and update_res[0][0] == 1:
        return 'update knowledge success.'
    raise Exception(f'update_knowledge failed, please check the kb_id and user_id. res is {update_res}')


# 删除知识库记录，同时删除知识库对应表格，同时需要删除知识库下所有数据源
def delete_knowledge(kb_id, user_id):
    """Function deleting knowledge base info."""
    if not kb_id:
        raise ValueError('Incorrect value for parameter kb_id: {}.'.format(kb_id))
    if not user_id:
        raise ValueError('Incorrect value for parameter user_id: {}.'.format(user_id))
    try:
        save_root = os.path.join(GAUSSMASTER_PATH, 'knowledge_base')
        gaussdb_vector.delete_knowledge(kb_id, user_id, save_root)
        return 'delete knowledge success.'
    except Exception as e:
        raise Exception(f'delete_knowledge failed, because: {e}.') from e


# 展示用户下的知识库列表
def list_knowledge(user_id):
    """Function listing knowledge base info."""
    if not user_id:
        raise ValueError('Incorrect value for parameter user_id: {}.'.format(user_id))
    try:
        return gaussdb_vector.list_knwoledge(user_id)
    except Exception as e:
        raise Exception(f'list_knowledge failed, because: {e}.') from e


# 批量删除用户下的知识库
def batch_delete_knowledge(kb_id_list, user_id):
    """Function batch deleting knowledge base info."""
    if not kb_id_list:
        raise ValueError('Incorrect value for parameter kb_id_list: {}.'.format(kb_id_list))
    if not user_id:
        raise ValueError('Incorrect value for parameter user_id: {}.'.format(user_id))
    try:
        save_root = os.path.join(GAUSSMASTER_PATH, 'knowledge_base')
        gaussdb_vector.batch_delete_knowledge(kb_id_list, user_id, save_root)
        return 'batch delete knowledge success.'
    except Exception as e:
        raise Exception(f'batch_delete_knowledge failed, because: {e}.') from e


# 获取知识库中的特定数据源信息
def get_datasource_info(ds_id, related_kb_id):
    """Function getting datasource info."""
    if not ds_id:
        raise ValueError('Incorrect value for parameter ds_id: {}.'.format(ds_id))
    if not related_kb_id:
        raise ValueError('Incorrect value for parameter related_kb_id: {}.'.format(related_kb_id))
    try:
        return gaussdb_vector.get_datasource_info(ds_id, related_kb_id)
    except Exception as e:
        raise Exception(f'get_datasource_info failed, because: {e}.') from e


# 为指定知识库添加数据源信息，同时插入数据到对应表格，保证原子操作
@timer_decorator
async def add_datasource(related_kb_id, file, name, description):
    """Function adding datasource info."""
    if not related_kb_id:
        raise ValueError('Incorrect value for parameter related_kb_id: {}.'.format(related_kb_id))
    if not file:
        raise ValueError('Incorrect value for parameter file: {}.'.format(file))
    try:
        create_time = str(datetime.now(adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))))
        record_dict = {
            'name': name,
            'related_kb_id': related_kb_id,
            'description': description,
            'create_time': create_time,
            'update_time': create_time
        }
        file_list = []
        file_size = 0
        for file_content in file:
            file_dict = {}
            contents = []
            while True:
                chunk = await file_content.read(1024 * 1024)
                if not chunk:
                    break
                contents.append(chunk)
                if len(contents) > FILE_MAX_SIZE / 1024 / 1024:
                    raise ValueError(f'file size exceeds {FILE_MAX_SIZE} B.')
            file_dict['content'] = b''.join(contents)
            file_dict['filename'] = validate_filename(file_content.filename)
            file_extension = os.path.splitext(file_dict['filename'])[1][1:]
            if file_extension not in SUFFIX_SUPPORT_LIST:
                raise ValueError(f'Unsupported file type, should be in {SUFFIX_SUPPORT_LIST}.')
            file_size += len(file_dict['content'])
            file_list.append(file_dict)
        if file_size > FILE_MAX_SIZE:
            raise ValueError(f'file size exceeds {FILE_MAX_SIZE} B.')
        await gaussdb_vector.add_datasource(record_dict, file_list, os.path.join(GAUSSMASTER_PATH, 'knowledge_base'))
        return 'add datasource success.'
    except Exception as e:
        raise Exception(f'add_datasource failed in stage add datasource, because: {e}.') from e


# 更新数据源信息，仅限名称、描述、文件名
def update_datasource(ds_id, name, related_kb_id, description):
    """Function updating datasource info."""
    if not ds_id:
        raise ValueError('Incorrect value for parameter ds_id: {}.'.format(ds_id))
    if not related_kb_id:
        raise ValueError('Incorrect value for parameter related_kb_id: {}.'.format(related_kb_id))
    if not name:
        raise ValueError('Incorrect value for parameter name: {}.'.format(name))
    try:
        update_res = gaussdb_vector.update_datasource_info(ds_id, name, related_kb_id, description)
    except Exception as e:
        raise Exception(f'update_datasource failed, because: {e}.') from e
    if len(update_res) == 1 and len(update_res[0]) == 1 and update_res[0][0] == 1:
        return 'update datasource success.'
    raise Exception(f'update_datasource failed, please check the ds_id and related_kb_id. res is {update_res}')


# 删除数据源信息，同时删除知识表中对应记录
def delete_datasource(ds_id, related_kb_id):
    """Function deleting datasource info."""
    if not ds_id:
        raise ValueError('Incorrect value for parameter ds_id: {}.'.format(ds_id))
    if not related_kb_id:
        raise ValueError('Incorrect value for parameter related_kb_id: {}.'.format(related_kb_id))
    try:
        save_root = os.path.join(GAUSSMASTER_PATH, 'knowledge_base')
        gaussdb_vector.delete_datasource(ds_id, related_kb_id, save_root)
        return 'delete datasource success.'
    except Exception as e:
        raise Exception(f'delete_datasource failed, because: {e}.') from e


# 展示知识库下的数据源列表
def list_datasource(related_kb_id):
    """Function listing datasource info."""
    try:
        return gaussdb_vector.list_datasource(related_kb_id)
    except Exception as e:
        raise Exception(f'list_datasource failed, because: {e}.') from e


# 批量删除知识库下的数据源
def batch_delete_datasource(ds_id_list, related_kb_id):
    """Function batch deleting datasource info."""
    if not ds_id_list:
        raise ValueError('Incorrect value for parameter ds_id_list: {}.'.format(ds_id_list))
    if not related_kb_id:
        raise ValueError('Incorrect value for parameter related_kb_id: {}.'.format(related_kb_id))
    try:
        save_root = os.path.join(GAUSSMASTER_PATH, 'knowledge_base')
        gaussdb_vector.batch_delete_datasource(ds_id_list, related_kb_id, save_root)
        return 'batch delete datasource success.'
    except Exception as e:
        raise Exception(f'batch_delete_datasource failed, because: {e}.') from e


def update_session_cluster(user_id: str, session_id: str, instance: str):
    """
    Update the user session cluster information

    Parameters:
    user_id (str): User ID
    session_id (str): Session ID
    instance (str): Instance information, including IP and port, like 'ip:port'

    Returns:
    bool: If the selected cluster is managed, return True, otherwise return False
    """
    ip, port = split_ip_port(instance)
    if select_managed_cluster(ip, port):
        global_vars.user_session_instance[user_id][session_id] = instance
        set_user_current_instance(user_id, instance)
        return True
    logging.error("Failed to switch instance. Because cluster %s is not managed by DBMind or not be registered.",
                  instance)
    return False
