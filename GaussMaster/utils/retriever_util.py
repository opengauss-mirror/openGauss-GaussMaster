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

from GaussMaster.common.http import create_requests_session


def reciprocal_rank_fusion(search_results_dict, k=60):
    """reciprocal rank fusion"""
    fused_scores = {}
    for _, doc_scores in search_results_dict.items():
        for rank, (doc, _) in enumerate(sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)):
            if doc not in fused_scores:
                fused_scores[doc] = 0
            fused_scores[doc] += 1 / (rank + k)


def union_adjacent_text(prev_text, text):
    """union adjacent text"""
    overlap_length = 0
    split_length = 1
    match_status = False
    max_iter = min(len(prev_text), len(text))
    while split_length < max_iter:
        window_prev = prev_text[-split_length:]
        window_cur = text[:split_length]
        if window_prev == window_cur:
            overlap_length = split_length
            match_status = True
        elif match_status:
            break
        split_length += 1
    return prev_text + text[overlap_length:]


class OnlineEmbedding():
    """Class representing online embedding"""

    def __init__(self, url, ssl_context, api_key=None):
        self.url = url
        self.ssl_context = ssl_context
        self.headers = {'Content-Type': 'application/json'}
        if api_key:
            self.headers['Authorization'] = f'Bearer {api_key}'

    async def query_embedding(self, query):
        """Function get query embedding"""
        embedding_list = await self.__embedding(query)
        return embedding_list[0]

    async def doc_embedding(self, queries):
        """Function get queries embedding"""
        embedding_list = await self.__embedding(queries)
        return embedding_list

    def get_embedding_dimensions(self):
        """Function get query dimensions"""
        return 1024

    async def __embedding(self, queries):
        """Function excute embedding"""
        data = {
            "query": queries
        }
        try:
            with ThreadPoolExecutor() as pool:
                with create_requests_session(ssl_context=self.ssl_context) as session:
                    embedding_response = functools.partial(
                        session.post, url=self.url, data=json.dumps(data), headers=self.headers)
                    response = await asyncio.get_running_loop().run_in_executor(pool, embedding_response)
                    response = response.json()
                    return response
        except Exception as e:
            raise Exception(f'embedding service is not accessable, because {e}') from e


class OnlineReranker():
    """Class representing online reranker"""

    def __init__(self, url, ssl_context, api_key=None):
        self.url = url
        self.ssl_context = ssl_context
        self.headers = {'Content-Type': 'application/json'}
        if api_key:
            self.headers['Authorization'] = f'Bearer {api_key}'

    async def compute_score(self, pairs):
        """Function compute scores of pairs"""
        data = {
            "pairs": pairs
        }
        try:
            with ThreadPoolExecutor() as pool:
                with create_requests_session(ssl_context=self.ssl_context) as session:
                    rerank_response = functools.partial(
                        session.post, url=self.url, data=json.dumps(data), headers=self.headers)
                    response = await asyncio.get_running_loop().run_in_executor(pool, rerank_response)
                    response = response.json()
                    return response
        except Exception as e:
            raise Exception(f'reranker service is not accessible, because {e}') from e


class DisabledOnlineEmbedding:
    """Placeholder when embedding is disabled or URL is unset; infer-only flows avoid vector search."""

    def __init__(self, url=None, ssl_context=None, api_key=None):
        pass

    async def query_embedding(self, query):
        return [0.0] * 1024

    async def doc_embedding(self, queries):
        n = 1 if isinstance(queries, str) else len(queries)
        return [[0.0] * 1024 for _ in range(n)]

    def get_embedding_dimensions(self):
        return 1024


class DisabledOnlineReranker:
    """Placeholder when reranker is disabled; returns neutral scores."""

    def __init__(self, url=None, ssl_context=None, api_key=None):
        pass

    async def compute_score(self, pairs):
        return [0.0] * len(pairs) if pairs else []


class BaseRetriever():
    def __init__(self, gaussdb, reranker):
        self.gaussdb = gaussdb
        self.reranker = reranker

    async def search_vector_result_gaussdb(self, query, topk=3, version=""):
        """search vector result gaussdb"""
        results = await self.gaussdb.search_vector(query, topk, version)
        return results

    def search_text_result_gaussdb(self, query, topk=3, version=""):
        """search text result gaussdb"""
        results = self.gaussdb.search_text(query, topk, version)
        return results

    def get_reranker_pairs(self, query, vector_result, text_result):
        """get reranker pairs"""
        dedup_list = list(set(vector_result + text_result))
        column_list = self.gaussdb.cols
        pairs = []
        for result in dedup_list:
            pair = [query, result[column_list.index('text')]]
            pairs.append(pair)
        return pairs, dedup_list

    async def get_sorted_results(self, query, vector_result, text_result):
        """get sorted results"""
        dedup_list = list(set(vector_result + text_result))
        column_list = self.gaussdb.cols
        pairs = []
        for result in dedup_list:
            pair = [query, result[column_list.index('text')]]
            pairs.append(pair)
        if not pairs:
            return [], []
        scores = await self.reranker.compute_score(pairs)
        if not scores:
            return [], []
        zipped = zip(scores, dedup_list)
        sort_zipped = sorted(zipped, key=lambda x: x[0], reverse=True)
        sort_result = zip(*sort_zipped)
        sorted_scores, sorted_answer_list = [list(x) for x in sort_result]
        return sorted_scores, sorted_answer_list

    async def reranker_search_result(self, query, vector_result, text_result, topk=3):
        """reranker search result"""
        sorted_scores, sorted_answer_list = await self.get_sorted_results(query, vector_result, text_result)
        res_score_list = []
        res_ans_list = []
        for index, answer in enumerate(sorted_answer_list):
            if sorted_scores[index] < 0:
                continue
            res_score_list.append(sorted_scores[index])
            res_ans_list.append(answer)
        return res_score_list[:topk], res_ans_list[:topk]

    async def get_search_result(self, query, vector_topk, text_topk, rerank_topk=3):
        """get search result"""
        vector_result = await self.search_vector_result_gaussdb(query, vector_topk)
        text_result = self.search_text_result_gaussdb(query, text_topk)
        reranker_scores, reranker_result = await self.reranker_search_result(query, vector_result, text_result,
                                                                             rerank_topk)
        return reranker_scores, reranker_result


class AdjacentRetriever(BaseRetriever):
    def __init__(self, gaussdb, reranker):
        super().__init__(gaussdb, reranker)

    def union_adjacent_result(self, results):
        """union adjacent result"""
        column_list = self.gaussdb.cols

        adjacent_uuid_list = set()
        for result in results:
            prev_uuid = result[column_list.index('prev_uuid')]
            if prev_uuid:
                adjacent_uuid_list.add(prev_uuid)
            next_uuid = result[column_list.index('next_uuid')]
            if next_uuid:
                adjacent_uuid_list.add(next_uuid)

        adjacent_results = self.gaussdb.get_content_from_uuid_list(list(adjacent_uuid_list))

        adjacent_content_dict = {}
        for adjacent_result in adjacent_results:
            adjacent_content_dict[adjacent_result[column_list.index('uuid')]] = adjacent_result

        context_list = []
        for result in results:
            uuid_list = []
            text = result[column_list.index('text')]
            uuid = result[column_list.index('uuid')]
            uuid_list.append(uuid)
            prev_uuid = result[column_list.index('prev_uuid')]
            if prev_uuid and prev_uuid in adjacent_content_dict.keys():
                prev_results = adjacent_content_dict[prev_uuid]
                text = union_adjacent_text(prev_results[column_list.index('text')], text)
                uuid_list.append(prev_uuid)
            next_uuid = result[column_list.index('next_uuid')]
            if next_uuid and next_uuid in adjacent_content_dict.keys():
                next_results = adjacent_content_dict[next_uuid]
                text = union_adjacent_text(text, next_results[column_list.index('text')])
                uuid_list.append(next_uuid)
            new_result = []
            for index, item in enumerate(result):
                if index == column_list.index('text'):
                    new_result.append(text)
                elif index == column_list.index('uuid'):
                    new_result.append(tuple(uuid_list))
                else:
                    new_result.append(item)
            context_list.append(tuple(new_result))
        return context_list

    def search_text_result_gaussdb(self, query, topk=3, version=""):
        """search text result gaussdb"""
        results = self.gaussdb.search_text(query, topk, version)
        text_result = self.union_adjacent_result(results)
        return text_result
