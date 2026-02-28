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
from concurrent.futures import ThreadPoolExecutor
from typing import Union

import requests

from GaussMaster.llms import llm_registry
from GaussMaster.llms.base.BaseLLM import BaseLLM
from GaussMaster.llms.llm_utils import check_message, LLMRole, LLMType


@llm_registry(name='Baichuan')
class Baichuan(BaseLLM):
    """
    local baichuan llm
    """
    url = ""

    def __init__(self, model, url, temperature=0, stream=True, api_key=None):
        super().__init__(model, url, temperature, stream)
        self.api_key = api_key
        self.data = {
            'messages': [],
        }

    @property
    def llm_type(self) -> str:
        return LLMType.BAICHUAN

    @check_message
    async def invoke(self, prompt: Union[list, str]) -> tuple:
        if isinstance(prompt, str):
            prompt = [{LLMRole.USER: prompt}]
        self.data['messages'] = prompt
        headers = {"Accept": "text/event-stream", "Connection": "keep-alive", "Cache-Control": "no-cache"}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        result = ''
        with ThreadPoolExecutor() as pool:
            llm_response = functools.partial(requests.post, url=self.url,
                                             data=json.dumps(self.data),
                                             stream=self.stream,
                                             headers=headers)
            response = await asyncio.get_running_loop().run_in_executor(pool, llm_response)
            for chunk in response.iter_content(decode_unicode=True):
                result += chunk
        return result, {}
