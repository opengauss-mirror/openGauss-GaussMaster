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

import logging
import os
import shutil
from datetime import datetime
from typing import List

import psycopg2
import sqlparse

from GaussMaster import global_vars
from GaussMaster.common.async_executor import run_in_thread_pool, async_write_file
from GaussMaster.common.configs.kb_config import KB_TABLE_NAME, DS_TABLE_NAME, QA_TABLE_NAME, KT_TABLE_CONFIG, \
    KB_TABLE_CONFIG, DS_TABLE_CONFIG, QA_TABLE_CONFIG
from GaussMaster.common.metadatabase.schema.document import Document
from GaussMaster.common.utils.base import adjust_timezone
from GaussMaster.common.utils.cli import print_progress
from GaussMaster.utils.doc_util import load_knowledge_from_file, deduplication

DEFAULT_DISTANCE_STRATEGY = "l2"  # l2/cosine/inner
DEFAULT_NCLUS = 16
DEFAULT_QUEUE_SIZE = 100
DEFAULT_NUM_PARALLELS = 30
DB_KEY = 'VECTOR'
CREATE_KEY = 'create_time'
FILE_KEY = 'filename'

# vector_sql_dialect in [VECTOR]: gaussdb (Huawei GaussDB vector SQL) or opengauss (openGauss DataVec)
VECTOR_SQL_DIALECT_GAUSSDB = 'gaussdb'
VECTOR_SQL_DIALECT_OPENGAUSS = 'opengauss'


def get_vector_sql_dialect():
    raw = (global_vars.configs.get(DB_KEY, 'vector_sql_dialect', fallback='gaussdb') or 'gaussdb').strip().lower()
    if raw in ('opengauss', 'datavec', 'og'):
        return VECTOR_SQL_DIALECT_OPENGAUSS
    return VECTOR_SQL_DIALECT_GAUSSDB


def get_vector_sql_backend():
    """Return dialect handler for vector/BM25 SQL (GaussDB vs openGauss DataVec)."""
    if get_vector_sql_dialect() == VECTOR_SQL_DIALECT_OPENGAUSS:
        return OpenGaussDataVecSqlBackend()
    return GaussDbVectorSqlBackend()


class VectorSqlBackend:
    """Builds dialect-specific vector and BM25 SQL fragments."""

    dialect_id: str = VECTOR_SQL_DIALECT_GAUSSDB

    def vector_column_type_sql(self, dimension: int) -> str:
        raise NotImplementedError

    def query_vector_literal(self, embedding_strs: List[str]) -> str:
        raise NotImplementedError

    def insert_vector_literal(self, embedding_strs: List[str]) -> str:
        return self.query_vector_literal(embedding_strs)

    def empty_vector_insert_fragment(self) -> str:
        raise NotImplementedError

    def distance_operator(self, distance_strategy: str) -> str:
        raise NotImplementedError

    def build_vector_index_sql(
            self, table_q: str, col_q: str, distance_strategy: str, embedding_dimensions: int) -> str:
        raise NotImplementedError

    def bm25_index_sqls(self, table_q: str, bm25_fields: List[str]) -> List[str]:
        raise NotImplementedError

    def build_search_text_sql(
            self, table_q: str, bm25_field: List[str], query: str, topk: int, version: str = '') -> str:
        raise NotImplementedError


class GaussDbVectorSqlBackend(VectorSqlBackend):
    """GaussDB: floatvector, gsdiskann, ### BM25."""

    dialect_id = VECTOR_SQL_DIALECT_GAUSSDB

    def vector_column_type_sql(self, dimension: int) -> str:
        return f'floatvector({dimension})'

    def query_vector_literal(self, embedding_strs: List[str]) -> str:
        return '\'[' + ','.join(embedding_strs) + ']\''

    def empty_vector_insert_fragment(self) -> str:
        return '\'[]\''

    def distance_operator(self, distance_strategy: str) -> str:
        return '<->'

    def build_vector_index_sql(
            self, table_q: str, col_q: str, distance_strategy: str, embedding_dimensions: int) -> str:
        return (
            f'CREATE INDEX ON {table_q} USING '
            f'gsdiskann({col_q} {distance_strategy}) '
            f'WITH (pq_nseg={embedding_dimensions}, '
            f'pq_nclus={DEFAULT_NCLUS}, queue_size={DEFAULT_QUEUE_SIZE}, '
            f'num_parallels={DEFAULT_NUM_PARALLELS}, enable_pq=true)')

    def bm25_index_sqls(self, table_q: str, bm25_fields: List[str]) -> List[str]:
        if not bm25_fields:
            return []
        bm25_fields_joined = ','.join(bm25_fields)
        return [
            (f'CREATE INDEX ON {table_q} USING bm25({bm25_fields_joined}) '
             f'WITH (num_parallels={DEFAULT_NUM_PARALLELS})')
        ]

    def build_search_text_sql(
            self, table_q: str, bm25_field: List[str], query: str, topk: int, version: str = '') -> str:
        if not bm25_field:
            raise ValueError('bm25_field is empty; text search is not configured for this table.')
        bm25_fields = ','.join(bm25_field)
        sql = f'SELECT /*+ no tablescan({table_q})*/ * FROM {table_q} '
        if version:
            sql += f'WHERE version=\'{escape_single_quote(version)}\' '
        sql += (f'ORDER BY "{escape_double_quote(bm25_fields)}" ### '
                f'\'{escape_single_quote(query)}\' desc LIMIT {topk}')
        return sql


class OpenGaussDataVecSqlBackend(VectorSqlBackend):
    """openGauss DataVec: vector type, diskann/ivfflat/hnsw, <&> BM25."""

    dialect_id = VECTOR_SQL_DIALECT_OPENGAUSS

    @staticmethod
    def _index_method() -> str:
        v = (global_vars.configs.get(DB_KEY, 'opengauss_vector_index', fallback='diskann') or 'diskann').strip().lower()
        if v not in ('diskann', 'ivfflat', 'hnsw'):
            return 'diskann'
        return v

    @staticmethod
    def _opclass(distance_strategy: str) -> str:
        s = (distance_strategy or DEFAULT_DISTANCE_STRATEGY).lower()
        if s == 'cosine':
            return 'vector_cosine_ops'
        if s == 'inner':
            return 'vector_ip_ops'
        return 'vector_l2_ops'

    @staticmethod
    def _order_op(distance_strategy: str) -> str:
        s = (distance_strategy or DEFAULT_DISTANCE_STRATEGY).lower()
        if s == 'cosine':
            return '<=>'
        if s == 'inner':
            return '<#>'
        return '<->'

    def vector_column_type_sql(self, dimension: int) -> str:
        return f'vector({dimension})'

    def query_vector_literal(self, embedding_strs: List[str]) -> str:
        return '\'[' + ','.join(embedding_strs) + ']\'::vector'

    def empty_vector_insert_fragment(self) -> str:
        return '\'[]\'::vector'

    def distance_operator(self, distance_strategy: str) -> str:
        return self._order_op(distance_strategy)

    def build_vector_index_sql(
            self, table_q: str, col_q: str, distance_strategy: str, embedding_dimensions: int) -> str:
        opclass = self._opclass(distance_strategy)
        method = self._index_method()
        cfg = global_vars.configs
        if method == 'ivfflat':
            lists = int((cfg.get(DB_KEY, 'opengauss_ivfflat_lists', fallback='100') or '100'))
            return (f'CREATE INDEX ON {table_q} USING ivfflat ({col_q} {opclass}) '
                    f'WITH (lists = {lists})')
        if method == 'hnsw':
            m = int((cfg.get(DB_KEY, 'opengauss_hnsw_m', fallback='16') or '16'))
            ef = int((cfg.get(DB_KEY, 'opengauss_hnsw_ef_construction', fallback='64') or '64'))
            return (f'CREATE INDEX ON {table_q} USING hnsw ({col_q} {opclass}) '
                    f'WITH (m = {m}, ef_construction = {ef})')
        index_size = int((cfg.get(DB_KEY, 'opengauss_diskann_index_size', fallback='50') or '50'))
        return (f'CREATE INDEX ON {table_q} USING diskann ({col_q} {opclass}) '
                f'WITH (index_size = {index_size})')

    def bm25_index_sqls(self, table_q: str, bm25_fields: List[str]) -> List[str]:
        if not bm25_fields:
            return []
        return [
            f'CREATE INDEX ON {table_q} USING bm25("{escape_double_quote(col)}")'
            for col in bm25_fields
        ]

    def build_search_text_sql(
            self, table_q: str, bm25_field: List[str], query: str, topk: int, version: str = '') -> str:
        if not bm25_field:
            raise ValueError('bm25_field is empty; text search is not configured for this table.')
        doc_col = bm25_field[0]
        if len(bm25_field) > 1:
            logging.warning(
                'openGauss BM25 supports a single TEXT column per index; using first bm25_field %r.', doc_col)
        sql = f'SELECT * FROM {table_q} '
        if version:
            sql += f'WHERE version=\'{escape_single_quote(version)}\' '
        sql += (f'ORDER BY "{escape_double_quote(doc_col)}" <&> '
                f'\'{escape_single_quote(query)}\' DESC LIMIT {topk}')
        return sql


# Anti-injection handler function
def escape_single_quote(x):
    """Function escape single quote."""
    if not x:
        return ''
    return x.replace("'", "''")


def escape_double_quote(x):
    """Function escape double quote."""
    if not x:
        return ''
    return x.replace('"', '""')


def escape_back_quote(x):
    """Function escape back quote."""
    if not x:
        return ''
    return x.replace('`', '``')


def sql_jsonfy(col_names, sql_result):
    """Function jsonfy sql."""
    res_list = []
    for record in sql_result:
        if len(record) != len(col_names):
            raise Exception('sql result is not correspond to the cols.')
        res_dict = {}
        for i, item in enumerate(record):
            if item is None:
                item = ""
            res_dict[col_names[i]] = item
        res_list.append(res_dict)
    return res_list


def get_column_name(vector_config):
    """Function get column name."""
    column_list = list(vector_config['text_field'])
    for vector_column in vector_config["vector_field"]:
        column_list.append(vector_column + '_vector')
    return column_list


def check_sql_valid(sql):
    """Function checking sql valid."""
    sql_len = sqlparse.split(sql)
    if len(sql_len) != 1:
        raise Exception('the sql is not valid, please check the kb_config.')


class GaussDB:
    def __init__(
            self,
            embedding_function,
            db_config,
            vector_config,
            distance_strategy=DEFAULT_DISTANCE_STRATEGY,
    ):
        self.distance_strategy = distance_strategy
        self.embedding_function = embedding_function
        self.db_host = db_config['host']
        self.db_user = db_config['user']
        self.db_port = db_config['port']
        self.db_name = db_config['dbname']
        self.db_pwd = db_config['password']
        self.db_index = vector_config["table_name"]
        self.embedding_keys = vector_config["vector_field"]
        self.bm25_field = vector_config["bm25_field"]
        self.fields = vector_config["text_field"]
        self.serial_field = vector_config.get('serial_field', [])
        self.int_field = vector_config.get('int_field', [])
        self.cols = get_column_name(vector_config)
        self.vector_sql_backend = get_vector_sql_backend()

        self.mem_db_conn = None
        self.cur = None

    def get_fields_with_vector(self, embedding_keys: str, dimension: int):
        """Function get fields with vector."""
        fields = []
        for field in self.fields:
            if field in self.serial_field:
                fields.append(f'"{escape_double_quote(field)}"' + ' serial')
            elif field in self.int_field:
                fields.append(f'"{escape_double_quote(field)}"' + ' Integer')
            else:
                fields.append(f'"{escape_double_quote(field)}"' + ' TEXT')
        vtype = self.vector_sql_backend.vector_column_type_sql(dimension)
        for embedding_key in embedding_keys:
            fields.append(f'"{escape_double_quote(embedding_key + "_vector")}" {vtype}')
        return ','.join(fields)

    def _get_table_count(self):
        """Function getting the record num of table."""
        sql = f'SELECT count(*) FROM "{escape_double_quote(self.db_index)}"'
        check_sql_valid(sql)
        self.cur.execute(sql)
        result = self.cur.fetchall()
        return result[0][0]

    def _drop_table(self):
        """Function droping table."""
        sql = f'DROP TABLE IF EXISTS "{escape_double_quote(self.db_index)}"'
        check_sql_valid(sql)
        self.cur.execute(sql)

    def load_table(self):
        """Function load table."""
        self._connect()
        self._create_table()
        self._close()

    def drop_table(self):
        """Function drop table."""
        self._connect()
        self._drop_table()
        self._close()

    async def load_documents(self, docs: List, need_index=False):
        """Function load documents."""
        self._connect()
        self._create_table()
        if self._get_table_count():
            self._close()
            return
        await self._batch_insert(docs)
        if need_index:
            self._create_index()
        self._close()

    async def add_documents(self, docs: List):
        """Function add documents."""
        self._connect()
        await self._batch_insert(docs)
        self._close()

    async def search_vector(self, query: str, topk: int, version="", embedding_key=None, distance=2):
        """Function vector search."""
        if not embedding_key:
            embedding_key = self.embedding_keys[0]
        embedding = await self.embedding_function.query_embedding(query)
        embedding_str = [str(num) for num in embedding]
        query_sql = self.vector_sql_backend.query_vector_literal(embedding_str)
        dist_op = self.vector_sql_backend.distance_operator(self.distance_strategy)
        sql = f'SELECT * FROM "{escape_double_quote(self.db_index)}" '
        if version:
            sql += f'WHERE version=\'{escape_single_quote(version)}\' '
        sql += (f'ORDER BY "{escape_double_quote(embedding_key + "_vector")}" '
                f'{dist_op} {query_sql} LIMIT {topk}')
        if distance != 2:
            cur_fields = [f'"{escape_double_quote(field)}"' for field in self.fields]
            sql = (f'SELECT "{escape_double_quote(embedding_key + "_vector")}" '
                   f'{dist_op} {query_sql} as dist, '
                   f'{(",".join(cur_fields))} FROM "{escape_double_quote(self.db_index)}" '
                   f'where dist<={distance} ORDER BY dist LIMIT {topk}')
        check_sql_valid(sql)
        self._connect()
        self.cur.execute(sql)
        result = self.cur.fetchall()
        self._close()
        return result

    def search_text(self, query: str, topk: int, version=""):
        """Function text search."""
        table_q = f'"{escape_double_quote(self.db_index)}"'
        sql = self.vector_sql_backend.build_search_text_sql(
            table_q, self.bm25_field, query, topk, version)
        self._connect()
        check_sql_valid(sql)
        self.cur.execute(sql)
        result = self.cur.fetchall()
        self._close()
        return result

    def get_content_from_uuid(self, uuid: str):
        """Function get record from uuid."""
        sql = f'SELECT * FROM "{escape_double_quote(self.db_index)}" WHERE uuid=\'{escape_single_quote(uuid)}\''
        self._connect()
        check_sql_valid(sql)
        self.cur.execute(sql)
        result = self.cur.fetchall()
        self._close()
        return result

    def get_content_from_uuid_list(self, uuid_list):
        """Function get record from uuid list."""
        if not uuid_list:
            return []
        uuid_string = '(\'' + '\',\''.join(uuid_list) + '\')'
        sql = f'SELECT * FROM "{escape_double_quote(self.db_index)}" WHERE uuid in {uuid_string}'
        self._connect()
        check_sql_valid(sql)
        self.cur.execute(sql)
        result = self.cur.fetchall()
        self._close()
        return result

    def get_uuid_from_table(self):
        """Function get all uuid."""
        sql = f'SELECT uuid, title, text FROM "{escape_double_quote(self.db_index)}"'
        self._connect()
        check_sql_valid(sql)
        self.cur.execute(sql)
        result = self.cur.fetchall()
        self._close()
        return result

    def save_local(self, file_name):
        """Function saving table to local file."""
        self._connect()
        fd = os.open(file_name, os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            self.cur.copy_to(f, self.db_index, sep=',')
        self._close()

    def load_local(self, file_name, need_index=False, truncate_table=False):
        """Function loadding table from local file."""
        self._connect()
        if truncate_table:
            self._drop_table()
        self._create_table()

        self._copy_from(file_name)

        if need_index:
            self._create_index()

        self._close()

    def get_history_record(self, user_id, session_id, history_len):
        """Function get history record"""
        sql = f'SELECT * FROM "{escape_double_quote(self.db_index)}" WHERE ' \
              f'user_id=\'{escape_single_quote(user_id)}\' AND session_id=\'{escape_single_quote(session_id)}\' ' \
              f'ORDER BY create_time desc LIMIT {history_len}'
        self._connect()
        check_sql_valid(sql)
        self.cur.execute(sql)
        result = self.cur.fetchall()
        self._close()
        return result

    def get_user_info(self, user_name):
        """Function get user info"""
        sql = f'SELECT user_name, encrypted_password FROM "{escape_double_quote(self.db_index)}" WHERE ' \
              f'user_name=\'{escape_single_quote(user_name)}\''
        self._connect()
        check_sql_valid(sql)
        self.cur.execute(sql)
        result = self.cur.fetchall()
        self._close()
        return result

    def _connect(self):
        self.mem_db_conn = psycopg2.connect(dbname=self.db_name, user=self.db_user, password=self.db_pwd,
                                            host=self.db_host, port=self.db_port, client_encoding="utf8")
        self.cur = self.mem_db_conn.cursor()

    def _create_table(self):
        """Function creating table."""
        fields = self.get_fields_with_vector(self.embedding_keys,
                                             self.embedding_function.get_embedding_dimensions())
        sql = f'CREATE TABLE IF NOT EXISTS "{escape_double_quote(self.db_index)}" ({fields}) ' \
              f'with (orientation=ROW, storage_type=astore)'
        check_sql_valid(sql)
        self.cur.execute(sql)

    async def _doc_to_db_string(self, doc):
        """Function trans doc to db record."""
        insert_string = ""
        value = []
        embedding_list = []
        for field in self.fields:
            f_content = doc.metadata.get(field, '')
            if isinstance(f_content, list):
                value.append(escape_single_quote('{\"' + '\",\"'.join(f_content) + '\"}'))
            elif not f_content:
                value.append('')
            else:
                value.append(escape_single_quote(str(f_content)))
            if field in self.embedding_keys:
                embedding = await self.embedding_function.query_embedding(f_content)
                embedding_str = [str(num) for num in embedding]
                embedding_list.append(embedding_str)
        insert_string = '(\'' + '\',\''.join(value) + '\''
        if embedding_list:
            for embedding_str in embedding_list:
                insert_string += ',' + self.vector_sql_backend.insert_vector_literal(embedding_str)
        else:
            for _ in self.embedding_keys:
                insert_string += ',' + self.vector_sql_backend.empty_vector_insert_fragment()
        insert_string += ')'
        return insert_string

    async def _batch_insert(self, docs: List):
        """Function batch insert record."""
        sql = f'INSERT INTO "{escape_double_quote(self.db_index)}" VALUES'
        count = 0
        values = []
        for doc in docs:
            cur_value = await self._doc_to_db_string(doc)
            values.append(cur_value)
            count = count + 1
            if count % 1000 == 0:
                cur_sql = sql + ','.join(values)
                self.cur.execute(cur_sql)
                values = []

        if values:
            cur_sql = sql + ','.join(values)
            self.cur.execute(cur_sql)

    def _create_index_vector(self):
        """Function creating vector index."""
        if not self.embedding_keys:
            return
        self.cur.execute('set maintenance_work_mem=1024000')
        table_q = f'"{escape_double_quote(self.db_index)}"'
        for embedding_key in self.embedding_keys:
            col_q = f'"{escape_double_quote(embedding_key + "_vector")}"'
            index_sql = self.vector_sql_backend.build_vector_index_sql(
                table_q, col_q, self.distance_strategy, self.embedding_function.get_embedding_dimensions())
            check_sql_valid(index_sql)
            self.cur.execute(index_sql)

    def _create_index_bm25(self):
        """Function creating bm25 index."""
        if not self.bm25_field:
            return
        table_q = f'"{escape_double_quote(self.db_index)}"'
        for bm25_index_sql in self.vector_sql_backend.bm25_index_sqls(table_q, self.bm25_field):
            check_sql_valid(bm25_index_sql)
            self.cur.execute(bm25_index_sql)

    @print_progress("Creating index.")
    def _create_index(self):
        self._create_index_vector()
        self._create_index_bm25()

    @print_progress("Copying from.")
    def _copy_from(self, file_name):
        with open(file_name, "r", encoding='utf-8') as f:
            self.cur.copy_from(f, self.db_index, sep=',')

    def _close(self):
        try:
            self.mem_db_conn.commit()
            self.cur.close()
            self.mem_db_conn.close()
        except Exception as e:
            raise Exception(f"close database failed because: {str(e)}") from e


class DBExecutor:
    def __init__(
            self,
            db_config
    ):
        self.db_host = db_config['host']
        self.db_user = db_config['user']
        self.db_port = db_config['port']
        self.db_name = db_config['dbname']
        self.db_pwd = db_config['password']
        self.mem_db_conn = None
        self.cur = None

    def connect(self):
        """Function connect db."""
        self.mem_db_conn = psycopg2.connect(dbname=self.db_name, user=self.db_user, password=self.db_pwd,
                                            host=self.db_host, port=self.db_port, client_encoding="utf8")
        self.cur = self.mem_db_conn.cursor()

    def close(self, commit=True):
        """Function close con."""
        if commit:
            self.mem_db_conn.commit()
        self.cur.close()
        self.mem_db_conn.close()

    def exec_sql(self, sql, need_result=False):
        """Function exec sql."""
        self.cur.execute(sql)
        if need_result:
            result = self.cur.fetchall()
            return result
        return []

    def exec_sql_list(self, sql_list, need_result=False):
        """Function exec sql."""
        result_list = []
        for sql in sql_list:
            self.cur.execute(sql)
            if need_result:
                result = self.cur.fetchall()
                result_list.append(result)
        if need_result:
            return result_list
        return result_list

    def save_local(self, table_name, file_name):
        """Function save local."""
        fd = os.open(file_name, os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            self.cur.copy_to(f, table_name, sep=',')

    def load_local(self, table_name, file_name):
        """Function load local."""
        with open(file_name, "r", encoding='utf-8') as f:
            self.cur.copy_from(f, table_name, sep=',')


class SQLCreator:
    def __init__(
            self,
            embedding_function,
            table_name,
            table_config,
            distance_strategy=DEFAULT_DISTANCE_STRATEGY
    ):
        self.distance_strategy = distance_strategy
        self.embedding_function = embedding_function
        self.table_name = table_name
        self.embedding_keys = table_config.get('vector_field', [])
        self.bm25_field = table_config.get('bm25_field', [])
        self.fields = table_config.get('text_field', [])
        self.serial_field = table_config.get('serial_field', [])
        self.cols = get_column_name(table_config)
        self.vector_sql_backend = get_vector_sql_backend()

    def get_create_table_sql(self):
        """Function get create table sql."""
        fields = []
        for field in self.fields:
            if field in self.serial_field:
                fields.append(f'"{escape_double_quote(field)}"' + ' serial')
            else:
                fields.append(f'"{escape_double_quote(field)}"' + ' TEXT')
        dim = 1024
        if hasattr(self.embedding_function, 'get_embedding_dimensions'):
            dim = self.embedding_function.get_embedding_dimensions()
        vtype = self.vector_sql_backend.vector_column_type_sql(dim)
        for embedding_key in self.embedding_keys:
            fields.append(f'"{escape_double_quote(embedding_key + "_vector")}" {vtype}')
        fields_str = ','.join(fields)
        sql = f'CREATE TABLE IF NOT EXISTS "{escape_double_quote(self.table_name)}" ({fields_str}) ' \
              f'with (orientation=ROW, storage_type=astore)'
        return sql

    def get_table_count_sql(self):
        """Function getting the record num of table."""
        sql = f'SELECT count(*) FROM "{escape_double_quote(self.table_name)}"'
        return sql

    def get_drop_table_sql(self):
        """Function droping table."""
        sql = f'DROP TABLE "{escape_double_quote(self.table_name)}"'
        return sql

    async def get_batch_insert_sql(self, docs):
        """Function get batch insert sql."""
        sql_list = []
        sql = f'INSERT INTO "{escape_double_quote(self.table_name)}" VALUES'
        count = 0
        values = []
        for doc in docs:
            cur_value = await self._doc_to_db_string(doc)
            values.append(cur_value)
            count = count + 1
            if count % 1000 == 0:
                cur_sql = sql + ','.join(values)
                sql_list.append(cur_sql)
                values = []

        if values:
            cur_sql = sql + ','.join(values)
            sql_list.append(cur_sql)
        return sql_list

    def get_create_index_vector_sql(self):
        """Function creating vector index."""
        sql_list = []
        if not self.embedding_keys:
            return sql_list
        sql_list.append('set maintenance_work_mem=1024000')
        table_q = f'"{escape_double_quote(self.table_name)}"'
        for embedding_key in self.embedding_keys:
            col_q = f'"{escape_double_quote(embedding_key + "_vector")}"'
            index_sql = self.vector_sql_backend.build_vector_index_sql(
                table_q, col_q, self.distance_strategy,
                self.embedding_function.get_embedding_dimensions())
            sql_list.append(index_sql)
        return sql_list

    def get_create_index_bm25_sqls(self):
        """Return BM25 index DDL statements (openGauss: one per TEXT column)."""
        table_q = f'"{escape_double_quote(self.table_name)}"'
        return self.vector_sql_backend.bm25_index_sqls(table_q, self.bm25_field)

    async def get_search_vector_sql(self, query: str, topk: int, embedding_key=None, distance=2):
        """Function vector search."""
        if not embedding_key:
            embedding_key = self.embedding_keys[0]
        embedding = await self.embedding_function.query_embedding(query)
        embedding_str = [str(num) for num in embedding]
        query_sql = self.vector_sql_backend.query_vector_literal(embedding_str)
        dist_op = self.vector_sql_backend.distance_operator(self.distance_strategy)
        sql = (f'SELECT * FROM "{escape_double_quote(self.table_name)}" '
               f'ORDER BY "{escape_double_quote(embedding_key + "_vector")}" '
               f'{dist_op} {query_sql} LIMIT {topk}')
        if distance != 2:
            cur_fields = [f'"{escape_double_quote(field)}"' for field in self.fields]
            sql = (f'SELECT "{escape_double_quote(embedding_key + "_vector")}" '
                   f'{dist_op} {query_sql} as dist, '
                   f'{(",".join(cur_fields))} FROM "{escape_double_quote(self.table_name)}" '
                   f'where dist<={distance} ORDER BY dist LIMIT {topk}')
        return sql

    def get_search_text_sql(self, query: str, topk: int):
        """Function text search."""
        table_q = f'"{escape_double_quote(self.table_name)}"'
        return self.vector_sql_backend.build_search_text_sql(table_q, self.bm25_field, query, topk, '')

    def get_content_from_uuid_sql(self, uuid: str):
        """Function get record from uuid."""
        sql = f'SELECT * FROM "{escape_double_quote(self.table_name)}" WHERE uuid=\'{escape_single_quote(uuid)}\''
        return sql

    def get_content_from_uuid_list_sql(self, uuid_list):
        """Function get record from uuid list."""
        if not uuid_list:
            return ""
        uuid_string = '(\'' + '\',\''.join(uuid_list) + '\')'
        sql = f'SELECT * FROM "{escape_double_quote(self.table_name)}" WHERE uuid in {uuid_string}'
        return sql

    def get_uuid_from_table_sql(self):
        """Function get all uuid."""
        sql = f'SELECT uuid, title, text FROM "{escape_double_quote(self.table_name)}"'
        return sql

    def get_qa_record_sql(self, answer_id, update_type):
        """Function get qs record sql."""
        sql = f''
        if update_type == 0:
            sql = f'SELECT "like" '
        else:
            sql = f'SELECT hate '
        sql += f'FROM "{escape_double_quote(self.table_name)}" WHERE answer_id=\'{escape_single_quote(answer_id)}\''
        return sql

    def get_update_qa_sql(self, answer_id, update_type, report_type, update_info, record_state):
        """Function get update qa sql."""
        sql = f'UPDATE "{escape_double_quote(self.table_name)}" SET '
        record_state = 0 if record_state else 1
        if update_type == 0:
            sql += f'"like"=\'{record_state}\' '
        elif update_type == 1:
            sql += f'hate=\'{record_state}\' '
        elif update_type == 2:
            sql += f'feedback_info=\'{escape_single_quote(update_info)}\' '
        elif update_type == 3:
            sql += f'report_type=\'{escape_single_quote(report_type)}\', ' \
                   f'report_info=\'{escape_single_quote(update_info)}\' '
        else:
            raise Exception(f'update qa failed, update_type is wrong: {update_type}')
        sql += f'WHERE answer_id=\'{escape_single_quote(answer_id)}\''
        return sql

    def get_insert_qa_sql(self, record_dict):
        """Function insert qa sql."""
        sql = f'INSERT INTO "{escape_double_quote(self.table_name)}" VALUES'
        value = []
        for field in self.fields:
            f_content = record_dict.get(field, '')
            if not f_content:
                value.append('')
            else:
                value.append(escape_single_quote(str(f_content)))
        insert_string = '(\'' + '\',\''.join(value) + '\')'
        sql += insert_string
        return sql

    def get_add_table_info_sql(self, record_dict):
        """Function inserting record."""
        value = []
        col_names = []
        for field in self.fields:
            if field in self.serial_field:
                continue
            f_content = record_dict.get(field, '')
            if isinstance(f_content, list):
                value.append(escape_single_quote('{\"' + '\",\"'.join(f_content) + '\"}'))
            else:
                value.append(escape_single_quote(str(f_content)))
            col_names.append(field)
        insert_string = '(\'' + '\',\''.join(value) + '\')'
        col_list = [f'"{escape_double_quote(col_name)}"' for col_name in col_names]
        col_string = '(' + ','.join(col_list) + ')'
        sql = f'INSERT INTO "{escape_double_quote(self.table_name)}" {col_string} VALUES'
        sql += insert_string
        if self.serial_field:
            return_string = ' RETURNING ' + self.serial_field[0]
        else:
            return_string = ' RETURNING -1'
        sql += return_string
        return sql

    def get_knowledge_info_sql(self, kb_id, user_id):
        """Function getting knowledge base info."""
        sql = f'SELECT * FROM "{escape_double_quote(self.table_name)}" WHERE kb_id={kb_id} AND ' \
              f'user_id=\'{escape_single_quote(user_id)}\''
        return sql

    def get_check_knowledge_exist_sql(self, kb_id):
        """Function getting knowledge base info."""
        sql = f'SELECT * FROM "{escape_double_quote(self.table_name)}" WHERE kb_id={kb_id}'
        return sql

    def get_update_knowledge_sql(self, kb_id, name, description, user_id, context):
        """Function get update knowledge sql."""
        tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
        update_time = str(datetime.now(tz))
        sql = f'UPDATE "{escape_double_quote(self.table_name)}" SET name=\'{escape_single_quote(name)}\', ' \
              f'description=\'{escape_single_quote(description)}\', context=\'{escape_single_quote(context)}\', ' \
              f'update_time=\'{escape_single_quote(update_time)}\' ' \
              f'WHERE kb_id={kb_id} and user_id=\'{escape_single_quote(user_id)}\' RETURNING 1'
        return sql

    def get_delete_knowledge_sql(self, kb_id, user_id):
        """Function delete knowledge sql."""
        sql = f'DELETE FROM "{escape_double_quote(self.table_name)}" WHERE kb_id={kb_id} AND ' \
              f'user_id=\'{escape_single_quote(user_id)}\' RETURNING 1'
        return sql

    def get_list_knwoledge_sql(self, user_id):
        """Function get list knowledge sql."""
        sql = f'SELECT * FROM "{escape_double_quote(self.table_name)}" WHERE ' \
              f'user_id=\'{escape_single_quote(user_id)}\' ORDER BY create_time, kb_id ASC'
        return sql

    def get_datasource_info_sql(self, ds_id, related_kb_id):
        """Function get datasource info sql."""
        sql = f'SELECT * FROM "{escape_double_quote(self.table_name)}" WHERE ds_id={ds_id} AND ' \
              f'related_kb_id={related_kb_id}'
        return sql

    def get_update_datasource_sql(self, ds_id, name, description, related_kb_id):
        """Function updating datasource info."""
        tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
        update_time = str(datetime.now(tz))
        sql = f'UPDATE "{escape_double_quote(self.table_name)}" SET name=\'{escape_single_quote(name)}\', ' \
              f'description=\'{escape_single_quote(description)}\', ' \
              f'update_time=\'{escape_single_quote(update_time)}\' ' \
              f'WHERE ds_id={ds_id} and related_kb_id={related_kb_id} RETURNING 1'
        return sql

    def get_delete_datasource_sql(self, ds_id, related_kb_id):
        """Function deleting datasource info."""
        sql = f'DELETE FROM "{escape_double_quote(self.table_name)}" WHERE ds_id={ds_id} AND ' \
              f'related_kb_id={related_kb_id} RETURNING file_name'
        return sql

    def get_list_datasource_sql(self, related_kb_id):
        """Function listing datasource info."""
        sql = f'SELECT * FROM "{escape_double_quote(self.table_name)}" WHERE ' \
              f'related_kb_id={related_kb_id} ORDER BY create_time, ds_id ASC'
        return sql

    def get_delete_knowledge_info_sql(self, ds_id):
        """Function get delete knowledge info sql."""
        sql = f'DELETE FROM "{escape_double_quote(self.table_name)}" WHERE ds_id={ds_id} RETURNING 1'
        return sql

    async def _doc_to_db_string(self, doc):
        """Function trans doc to db record."""
        insert_string = ""
        value = []
        embedding_list = []
        for field in self.fields:
            f_content = doc.metadata.get(field, '')
            if isinstance(f_content, list):
                value.append(escape_single_quote('{\"' + '\",\"'.join(f_content) + '\"}'))
            elif not f_content:
                value.append('')
            else:
                value.append(escape_single_quote(str(f_content)))
            if field in self.embedding_keys:
                embedding = await self.embedding_function.query_embedding(f_content)
                embedding_str = [str(num) for num in embedding]
                embedding_list.append(embedding_str)
        insert_string = '(\'' + '\',\''.join(value) + '\''
        if embedding_list:
            for embedding_str in embedding_list:
                insert_string += ',' + self.vector_sql_backend.insert_vector_literal(embedding_str)
        else:
            for _ in self.embedding_keys:
                insert_string += ',' + self.vector_sql_backend.empty_vector_insert_fragment()
        insert_string += ')'
        return insert_string


def get_global_db():
    """Function get global db."""
    gaussdb_config = {
        'host': global_vars.configs.get(DB_KEY, 'host'),
        'port': global_vars.configs.get(DB_KEY, 'port'),
        'user': global_vars.configs.get(DB_KEY, 'user'),
        'dbname': global_vars.configs.get(DB_KEY, 'vector_dbname'),
        'password': global_vars.configs.get(DB_KEY, 'password')
    }
    db_executor = DBExecutor(gaussdb_config)
    return db_executor


def get_sql_creator(table_name, table_config):
    """Function get sql creator."""
    gaussdb_embedding = global_vars.embedding_model
    sql_creator = SQLCreator(gaussdb_embedding, table_name, table_config)
    return sql_creator


def create_table(table_name, table_config):
    """Function create table."""
    try:
        db_executor = get_global_db()
        sql_creator = get_sql_creator(table_name, table_config)
        sql = sql_creator.get_create_table_sql()
        db_executor.connect()
        db_executor.exec_sql(sql)
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"create table failed because: {str(e)}") from e
    finally:
        db_executor.close()


async def init_documents(docs, table_name, table_config, need_index=False):
    """Function init documents."""
    try:
        db_executor = get_global_db()
        sql_creator = get_sql_creator(table_name, table_config)
        db_executor.connect()
        sql = sql_creator.get_create_table_sql()
        db_executor.exec_sql(sql)
        sql = sql_creator.get_table_count_sql()
        sql_result = db_executor.exec_sql(sql, need_result=True)
        record_num = sql_result[0][0]
        if record_num:
            db_executor.close()
            return
        sql_list = await sql_creator.get_batch_insert_sql(docs)
        db_executor.exec_sql_list(sql_list)
        if need_index:
            sql_list = sql_creator.get_create_index_vector_sql()
            db_executor.exec_sql_list(sql_list)
            for bm25_sql in sql_creator.get_create_index_bm25_sqls():
                db_executor.exec_sql(bm25_sql)
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"init document failed because: {str(e)}") from e
    finally:
        db_executor.close()


async def insert_documents(docs, table_name, table_config):
    """Function insert documents."""
    try:
        db_executor = get_global_db()
        sql_creator = get_sql_creator(table_name, table_config)
        sql_list = await sql_creator.get_batch_insert_sql(docs)
        db_executor.connect()
        db_executor.exec_sql_list(sql_list)
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"insert document failed because: {str(e)}") from e
    finally:
        db_executor.close()


def drop_table(table_name, table_config):
    """Function drop table."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(table_name, table_config)
    sql = sql_creator.get_drop_table_sql()
    db_executor.connect()
    db_executor.exec_sql(sql)
    db_executor.close()


async def search_vector(table_name, table_config, query, topk, embedding_key=None, distance=2):
    """Function search vector."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(table_name, table_config)
    sql = await sql_creator.get_search_vector_sql(query, topk, embedding_key, distance)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    return sql_result


def search_text(table_name, table_config, query, topk):
    """Function search text."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(table_name, table_config)
    sql = sql_creator.get_search_text_sql(query, topk)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    return sql_result


def get_content_from_uuid(table_name, table_config, uuid):
    """Function get content from uuid."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(table_name, table_config)
    sql = sql_creator.get_content_from_uuid_sql(uuid)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    return sql_result


def get_content_from_uuid_list(table_name, table_config, uuid_list):
    """Function get content from uuid list."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(table_name, table_config)
    sql = sql_creator.get_content_from_uuid_list_sql(uuid_list)
    if not sql:
        return []
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    return sql_result


def get_uuid_from_table(table_name, table_config):
    """Function get uuid from table."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(table_name, table_config)
    sql = sql_creator.get_uuid_from_table_sql()
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    return sql_result


def insert_qa_record(record_dict):
    """Function insert qa record."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(QA_TABLE_NAME, QA_TABLE_CONFIG)
    sql = sql_creator.get_insert_qa_sql(record_dict)
    db_executor.connect()
    db_executor.exec_sql(sql)
    db_executor.close()


# 更新反馈状态，0-like/1-hate/2-feedback/3-report
def update_qa_record(answer_id, update_type, report_type=None, update_info=None):
    """Function update qa record."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(QA_TABLE_NAME, QA_TABLE_CONFIG)
    record_state = False

    sql = sql_creator.get_qa_record_sql(answer_id, update_type)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    if len(sql_result) != 1 or len(sql_result[0]) != 1:
        raise Exception(f'can not get the qa record, res is {sql_result}')
    if update_type in (0, 1):
        record_state = sql_result[0][0]
        if not record_state:
            record_state = False
        else:
            record_state = True
    sql = sql_creator.get_update_qa_sql(answer_id, update_type, report_type, update_info, record_state)
    db_executor.exec_sql(sql)
    db_executor.close()


# 首先添加知识库管理表信息，获取到知识库的id
# 然后创建对应的知识表
# 随后遍历文件列表，每个文件均添加数据源管理表信息
# 然后把文件存储下来，解析成知识片段，添加到知识表中
async def add_knowledge(record_dict, file_list, save_root):
    """Function add knowledge."""
    try:
        db_executor = get_global_db()
        sql_creator = get_sql_creator(KB_TABLE_NAME, KB_TABLE_CONFIG)
        sql = sql_creator.get_add_table_info_sql(record_dict)
        db_executor.connect()
        sql_result = db_executor.exec_sql(sql, need_result=True)
        if len(sql_result) != 1 or len(sql_result[0]) != 1 or sql_result[0][0] == -1:
            raise Exception(f'can not get the kb_id, res is {sql_result}')
        kb_id = sql_result[0][0]
        save_dir = os.path.join(save_root, 'custom_knowledge_' + str(kb_id))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        kt_table_name = 'custom_knowledge_' + str(kb_id)
        kt_sql_creator = get_sql_creator(kt_table_name, KT_TABLE_CONFIG)
        sql = kt_sql_creator.get_create_table_sql()
        db_executor.exec_sql(sql)
        for file_dict in file_list:
            filename = file_dict['filename']
            ds_record_dict = {
                'name': os.path.splitext(filename)[0],
                'related_kb_id': kb_id,
                'description': '',
                'file_name': filename,
                'create_time': record_dict[CREATE_KEY],
                'update_time': record_dict[CREATE_KEY]
            }
            ds_sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
            sql = ds_sql_creator.get_add_table_info_sql(ds_record_dict)
            sql_result = db_executor.exec_sql(sql, need_result=True)
            if len(sql_result) != 1 or len(sql_result[0]) != 1 or sql_result[0][0] == -1:
                raise Exception(f'can not get the ds_id, res is {sql_result}')
            ds_id = sql_result[0][0]
            if not ds_id:
                raise Exception(f'can not get the ds_id')
            file_path = os.path.join(save_dir, filename)
            if os.path.exists(file_path):
                continue
            await async_write_file(file_path, file_dict['content'])
            os.chmod(os.path.join(save_dir, filename), 0o600)
            content_list = await run_in_thread_pool(load_knowledge_from_file, os.path.join(save_dir, filename), ds_id)
            unique_content_list = await run_in_thread_pool(deduplication, content_list)
            docs = []
            for data_dict in unique_content_list:
                doc = Document()
                doc.metadata = data_dict
                docs.append(doc)
            sql_list = await kt_sql_creator.get_batch_insert_sql(docs)
            db_executor.exec_sql_list(sql_list)
        for bm25_sql in kt_sql_creator.get_create_index_bm25_sqls():
            db_executor.exec_sql(bm25_sql)
        db_executor.close()
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"add knowledge failed because: {str(e)}") from e


def get_knowledge_info(kb_id, user_id):
    """Function get knowledge info."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(KB_TABLE_NAME, KB_TABLE_CONFIG)
    sql = sql_creator.get_knowledge_info_sql(kb_id, user_id)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    res_list = sql_jsonfy(sql_creator.cols, sql_result)
    return res_list


def update_knowledge_info(kb_id, name, user_id, description, context):
    """Function update knowledge info."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(KB_TABLE_NAME, KB_TABLE_CONFIG)
    sql = sql_creator.get_update_knowledge_sql(kb_id, name, description, user_id, context)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    return sql_result


def delete_knowledge(kb_id, user_id, save_root):
    """Function delete knowledge."""
    try:
        db_executor = get_global_db()
        sql_creator = get_sql_creator(KB_TABLE_NAME, KB_TABLE_CONFIG)
        sql = sql_creator.get_delete_knowledge_sql(kb_id, user_id)
        db_executor.connect()
        sql_result = db_executor.exec_sql(sql, need_result=True)
        if len(sql_result) != 1 or len(sql_result[0]) != 1 or sql_result[0][0] != 1:
            raise Exception(f'user {user_id} does not own {kb_id} knowledge base.')
        ds_sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
        sql = ds_sql_creator.get_list_datasource_sql(kb_id)
        ds_list = db_executor.exec_sql(sql, need_result=True)
        for ds_record in ds_list:
            ds_id = ds_record[0]
            sql = ds_sql_creator.get_delete_datasource_sql(ds_id, kb_id)
            db_executor.exec_sql(sql)
        kt_table_name = 'custom_knowledge_' + str(kb_id)
        ds_sql_creator = get_sql_creator(kt_table_name, KT_TABLE_CONFIG)
        sql = ds_sql_creator.get_drop_table_sql()
        db_executor.exec_sql(sql)
        save_dir = os.path.join(save_root, 'custom_knowledge_' + str(kb_id))
        shutil.rmtree(save_dir, ignore_errors=True)
        db_executor.close()
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"delete knowledge failed because: {str(e)}") from e


def list_knwoledge(user_id):
    """Function list knowledge."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(KB_TABLE_NAME, KB_TABLE_CONFIG)
    sql = sql_creator.get_list_knwoledge_sql(user_id)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    res_list = sql_jsonfy(sql_creator.cols, sql_result)
    return res_list


def batch_delete_knowledge(kb_id_list, user_id, save_root):
    """Function batch delete knowledge."""
    try:
        db_executor = get_global_db()
        db_executor.connect()
        for kb_id in kb_id_list:
            sql_creator = get_sql_creator(KB_TABLE_NAME, KB_TABLE_CONFIG)
            sql = sql_creator.get_delete_knowledge_sql(kb_id, user_id)
            sql_result = db_executor.exec_sql(sql, need_result=True)
            if len(sql_result) != 1 or len(sql_result[0]) != 1 or sql_result[0][0] != 1:
                raise Exception(f'user {user_id} does not own {kb_id} knowledge base.')
            ds_sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
            sql = ds_sql_creator.get_list_datasource_sql(kb_id)
            ds_list = db_executor.exec_sql(sql, need_result=True)
            for ds_record in ds_list:
                ds_id = ds_record[0]
                sql = ds_sql_creator.get_delete_datasource_sql(ds_id, kb_id)
                db_executor.exec_sql(sql)
            kt_table_name = 'custom_knowledge_' + str(kb_id)
            ds_sql_creator = get_sql_creator(kt_table_name, KT_TABLE_CONFIG)
            sql = ds_sql_creator.get_drop_table_sql()
            db_executor.exec_sql(sql)
            save_dir = os.path.join(save_root, 'custom_knowledge_' + str(kb_id))
            shutil.rmtree(save_dir, ignore_errors=True)
        db_executor.close()
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"delete knowledge failed because: {str(e)}") from e


def get_datasource_info(ds_id, related_kb_id):
    """Function get datasource info."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
    sql = sql_creator.get_datasource_info_sql(ds_id, related_kb_id)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    res_list = sql_jsonfy(sql_creator.cols, sql_result)
    return res_list


def check_knowledge_exist(db_executor, related_kb_id):
    """
    :param db_executor:
    :param related_kb_id:
    :return:
    """
    sql_creator = get_sql_creator(KB_TABLE_NAME, KB_TABLE_CONFIG)
    sql = sql_creator.get_check_knowledge_exist_sql(related_kb_id)
    sql_result = db_executor.exec_sql(sql, need_result=True)
    if len(sql_result) != 1:
        db_executor.close(commit=False)
        raise Exception(f'can not get the related knowledge base, res is {sql_result}')


def insert_ds_record(db_executor, record_dict):
    """Function insert ds record."""
    sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
    sql = sql_creator.get_add_table_info_sql(record_dict)
    sql_result = db_executor.exec_sql(sql, need_result=True)
    if len(sql_result) != 1 or len(sql_result[0]) != 1 or sql_result[0][0] == -1:
        raise Exception(f'can not get the ds_id, res is {sql_result}')
    ds_id = sql_result[0][0]
    if not ds_id:
        raise Exception(f'can not get the ds_id')
    return ds_id


def insert_knowledge_table(file_path, file_dict, ds_id):
    """Function insert knowledge table."""
    with open(file_path, "wb") as f:
        f.write(file_dict['content'])
        f.close()
    os.chmod(file_path, 0o600)
    content_list = load_knowledge_from_file(file_path, ds_id)
    unique_content_list = deduplication(content_list)
    docs = []
    for data_dict in unique_content_list:
        doc = Document()
        doc.metadata = data_dict
        docs.append(doc)
    return docs


# 首先判断知识库是否存在
# 然后遍历文件列表，每个文件均添加数据源管理表信息
# 把文件存储下来，解析成知识片段，添加到知识表中
async def add_datasource(record_dict, file_list, save_root):
    """Function add datasource."""
    try:
        db_executor = get_global_db()
        db_executor.connect()
        related_kb_id = record_dict['related_kb_id']
        check_knowledge_exist(db_executor, related_kb_id)
        sql_creator = get_sql_creator('custom_knowledge_' + str(related_kb_id), KT_TABLE_CONFIG)
        save_dir = os.path.join(save_root, 'custom_knowledge_' + str(related_kb_id))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        for file_dict in file_list:
            name = record_dict['name']
            ds_record_dict = {
                'name': name if name else os.path.splitext(file_dict[FILE_KEY])[0],
                'related_kb_id': related_kb_id,
                'description': record_dict['description'],
                'file_name': file_dict[FILE_KEY],
                'create_time': record_dict[CREATE_KEY],
                'update_time': record_dict[CREATE_KEY]
            }
            ds_id = insert_ds_record(db_executor, ds_record_dict)
            file_path = os.path.join(save_dir, file_dict[FILE_KEY])
            if os.path.exists(file_path):
                continue
            docs = await run_in_thread_pool(insert_knowledge_table, file_path, file_dict, ds_id)
            sql_list = await sql_creator.get_batch_insert_sql(docs)
            db_executor.exec_sql_list(sql_list)
        db_executor.close()
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"add datasource failed because: {str(e)}") from e


def update_datasource_info(ds_id, name, related_kb_id, description):
    """Function update datasource info."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
    sql = sql_creator.get_update_datasource_sql(ds_id, name, description, related_kb_id)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    return sql_result


def delete_datasource(ds_id, related_kb_id, save_root):
    """Function delete datasource."""
    try:
        db_executor = get_global_db()
        sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
        sql = sql_creator.get_delete_datasource_sql(ds_id, related_kb_id)
        db_executor.connect()
        sql_result = db_executor.exec_sql(sql, need_result=True)
        if len(sql_result) != 1 or len(sql_result[0]) != 1:
            raise Exception(f'can not get the filename, res is {sql_result}')
        kt_table_name = 'custom_knowledge_' + str(related_kb_id)
        kt_sql_creator = get_sql_creator(kt_table_name, KT_TABLE_CONFIG)
        sql = kt_sql_creator.get_delete_knowledge_info_sql(ds_id)
        db_executor.exec_sql(sql)
        save_dir = os.path.join(save_root, 'custom_knowledge_' + str(related_kb_id))
        file_path = os.path.join(save_dir, sql_result[0][0])
        if os.path.exists(file_path):
            os.remove(file_path)
        db_executor.close()
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"delete datasource failed because: {str(e)}") from e


def list_datasource(related_kb_id):
    """Function list datasource."""
    db_executor = get_global_db()
    sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
    sql = sql_creator.get_list_datasource_sql(related_kb_id)
    db_executor.connect()
    sql_result = db_executor.exec_sql(sql, need_result=True)
    db_executor.close()
    res_list = sql_jsonfy(sql_creator.cols, sql_result)
    return res_list


def batch_delete_datasource(ds_id_list, related_kb_id, save_root):
    """Function batch delete datasource."""
    try:
        db_executor = get_global_db()
        db_executor.connect()
        for ds_id in ds_id_list:
            sql_creator = get_sql_creator(DS_TABLE_NAME, DS_TABLE_CONFIG)
            sql = sql_creator.get_delete_datasource_sql(ds_id, related_kb_id)
            sql_result = db_executor.exec_sql(sql, need_result=True)
            if len(sql_result) != 1 or len(sql_result[0]) != 1:
                raise Exception(f'can not get the filename, res is {sql_result}')
            kt_table_name = 'custom_knowledge_' + str(related_kb_id)
            kt_sql_creator = get_sql_creator(kt_table_name, KT_TABLE_CONFIG)
            sql = kt_sql_creator.get_delete_knowledge_info_sql(ds_id)
            db_executor.exec_sql(sql)
            save_dir = os.path.join(save_root, 'custom_knowledge_' + str(related_kb_id))
            file_path = os.path.join(save_dir, sql_result[0][0])
            if os.path.exists(file_path):
                os.remove(file_path)
        db_executor.close()
    except Exception as e:
        db_executor.close(commit=False)
        raise Exception(f"delete datasource failed because: {str(e)}") from e
