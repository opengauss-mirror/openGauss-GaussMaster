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
import functools
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Union

import requests

from GaussMaster.llms import llm_registry
from GaussMaster.llms.base.BaseLLM import BaseLLM
from GaussMaster.llms.llm_utils import check_message, LLMRole, LLMType


def postprocess_output(answer):
    """post process the response of pangu llm """
    txt = answer.decode('utf-8')
    body = json.loads(txt[len('data:'):])
    if body.get('errCode') != 0:
        return body.get('errMessage'), {}
    msg = body.get('result').get('choices')[0].get('message')
    result = msg.get('content')
    function_call = msg.get('function_call', {})
    return result, function_call


@llm_registry(name='Pangu')
class Pangu(BaseLLM):
    """
    pangu of Noah's Ark Laboratory
    """
    temperature: float = 0.0
    url = ""
    headers = {'Content-Type': 'application/json; charset=utf-8', 'Accept-Charset': 'utf-8'}
    data = {}

    def __init__(self, model, url, temperature=0.0, stream=False, api_key=None):
        super().__init__(model, url, temperature, stream)
        if api_key:
            self.headers['Authorization'] = f'Bearer {api_key}'
        self.data = {
            'stream': stream,
            'messages': [],
            'messageId': 'test123456',
            'model': model,
            'userId': 'user_123456',
            'top_p': 0.0,
            'temperature': temperature
        }

    @property
    def llm_type(self) -> str:
        return LLMType.PANGU

    @check_message
    async def invoke(self, prompt: Union[list, str]) -> tuple:
        """
        :return:
            result: str, llm plain response
            function_call: dict, {'name': 'xx', 'arguments':{}}
        """
        if isinstance(prompt, str):
            prompt = [{LLMRole.USER: prompt}]
            logging.debug(prompt)
        else:
            for message in prompt:
                logging.debug(message)
        self.data['messages'] = prompt
        with ThreadPoolExecutor() as pool:
            llm_response = functools.partial(requests.post, url=self.url, data=json.dumps(self.data),
                                             headers=self.headers)
            response = await asyncio.get_running_loop().run_in_executor(pool, llm_response)
        if response.status_code == 200:
            content, function_call = postprocess_output(response.content)
            logging.debug('content: %s', content)
            logging.debug('function_call: %s', function_call)
            return content, function_call
        return f'Pangu service error: {response.status_code}', {}
