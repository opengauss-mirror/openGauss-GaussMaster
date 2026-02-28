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

"""Llama3 model"""
import asyncio
import functools
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Union

import requests

from GaussMaster.llms import llm_registry
from GaussMaster.llms.base.BaseLLM import BaseLLM
from GaussMaster.llms.llm_utils import LLMRole, check_message, LLMType


@llm_registry(name='Llama3')
class Llama3(BaseLLM):
    """
    Llama3 llm
    """
    url = ""
    headers = {'Content-Type': 'application/json'}

    def __init__(self, model, url, temperature=0, stream=False, api_key=None):
        super().__init__(model, url, temperature, stream)
        if api_key:
            self.headers['Authorization'] = f'Bearer {api_key}'
        self.data = {
            'messages': [],
        }

    @property
    def llm_type(self) -> str:
        return LLMType.LLAMA3

    @check_message
    async def invoke(self, prompt: Union[list, str]) -> tuple:
        if isinstance(prompt, str):
            prompt = [{LLMRole.USER: prompt}]
        self.data['messages'] = prompt
        with ThreadPoolExecutor() as pool:
            llm_response = functools.partial(requests.post, url=self.url,
                                             data=json.dumps(self.data),
                                             headers=self.headers,
                                             timeout=120)
            resp = await asyncio.get_running_loop().run_in_executor(pool, llm_response)
        result = resp.text
        return result, {}
