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

"""
Pangu Large Language Model provided by Huawei Cloud
"""
import asyncio
import copy
import functools
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Union

import requests

from GaussMaster.llms import llm_registry
from GaussMaster.llms.base.BaseLLM import BaseLLM
from GaussMaster.llms.llm_utils import check_message, LLMRole, LLMType


@llm_registry(name='PanguCloud')
class PanguCloud(BaseLLM):
    """
    pangu of huawei cloud
    """
    temperature: float = 0.0
    url = ""
    headers = {'Content-Type': 'application/json; charset=utf-8', 'Accept-Charset': 'utf-8'}
    data = {}

    def __init__(self, model, url, temperature=0, stream=False, api_key=None):
        super().__init__(model, url, temperature, stream)
        if api_key:
            self.headers['Authorization'] = f'Bearer {api_key}'
        self.data = {
            'messages': [],
        }

    @property
    def llm_type(self) -> str:
        return LLMType.PANGUCLOUD

    @check_message
    async def invoke(self, prompt: Union[list, str]) -> tuple:
        """
        :return:
            result: str, llm plain response
            function_call: dict, {'name': 'xx', 'arguments':{}}
        """
        if isinstance(prompt, str):
            prompt = [{LLMRole.USER: prompt}]
        self.data['messages'] = prompt
        with ThreadPoolExecutor() as pool:
            llm_response = functools.partial(requests.post, url=self.url, data=json.dumps(self.data),
                                             headers=self.headers)
            response = await asyncio.get_running_loop().run_in_executor(pool, llm_response)
        if response.status_code == 200:
            content, function_call = postprocess_output(response.text)
            return content, function_call
        return f'PanguCloud service error: {response.status_code}', {}


def postprocess_output(answer):
    """
    postprocess the output of raw answer
    """
    pattern = '<unused2>(.*?)<unused3>'
    time_pattern = r'^\d{4}-\d{2}-\d{2}\d{2}:\d{2}:\d{2}$'
    match = re.search(pattern, answer, re.DOTALL)
    if match:
        function_call = json.loads(analyze_function_call(match.group(1)))
        source_arguments = function_call.get('arguments')
        target_arguments = copy.deepcopy(source_arguments)
        for key, value in source_arguments.items():
            if isinstance(value, str) and re.match(time_pattern, value):
                target_arguments[key] = value[:10] + " " + value[10:]
        function_call['arguments'] = target_arguments
        answer = answer.replace(match.group(0), "")
    else:
        function_call = {}
    return answer, function_call


def analyze_function_call(call):
    """
    extract the func_name and arguments from function_str
    """
    function_call = dict()
    name, arguments = call.split('|')
    function_call['name'] = name
    function_call['arguments'] = json.loads(arguments)
    return json.dumps(function_call)
