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

"""utils related to metadatabase"""
from enum import Enum
from urllib import parse

from GaussMaster import global_vars
from GaussMaster.common.metadatabase.dao.gaussdb_vector import GaussDB


class DbType(Enum):
    SQLITE = 'sqlite'
    OPENGAUSS = 'opengauss'
    GAUSSDB = 'gaussdb'
    POSTGRESQL = 'postgresql'
    MYSQL = 'mysql'


DB_TYPES = [item.value for item in DbType]


def create_dsn(
        db_type,
        database,
        host=None,
        port=None,
        username=None,
        password=None,
        schema='public'
):
    """Generate a DSN (Data Source Name) according to the user's given parameters.
    Meanwhile, GaussMaster will adapt some interfaces to SQLAlchemy, such as openGauss."""
    db_type = db_type.lower().strip()
    if db_type not in DB_TYPES:
        raise ValueError("Not supported database type '%s'." % db_type)
    if db_type in [DbType.OPENGAUSS.value, DbType.GAUSSDB.value]:
        db_type = DbType.POSTGRESQL.value
        # The following method must be override, or SQLAlchemy will raise an exception about unknown version.
        from sqlalchemy.dialects.postgresql.base import PGDialect
        PGDialect._get_server_version_info = lambda *args: (9, 2)
    if db_type == DbType.SQLITE.value:
        dsn = '{}:///{}?check_same_thread=False'.format(db_type, database)
    else:
        username = parse.quote(username or '')
        password = parse.quote(password or '')
        host = parse.quote(host or '')
        database = parse.quote(database or '')
        dsn = '{}://{}:{}@{}:{}/{}'.format(db_type, username, password, host, port, database)
    if db_type == DbType.POSTGRESQL.value:
        dsn = dsn + '?options=-csearch_path%3D{}&client_encoding=utf8'.format(schema)  # only use the fixed schema
    return dsn


def get_vector_db(vector_config):
    """get vector db engine"""
    gaussdb_config = {
        'host': global_vars.configs.get('VECTOR', 'host'),
        'port': global_vars.configs.get('VECTOR', 'port'),
        'user': global_vars.configs.get('VECTOR', 'user'),
        'dbname': global_vars.configs.get('VECTOR', 'vector_dbname'),
        'password': global_vars.configs.get('VECTOR', 'password')
    }

    gaussdb_embedding = global_vars.embedding_model
    return GaussDB(gaussdb_embedding, gaussdb_config, vector_config)


def get_db_instance(table_name, table_config):
    """Function get knowledge table instance."""
    vector_config = table_config.copy()
    vector_config["table_name"] = table_name
    custom_db = get_vector_db(vector_config=vector_config)
    return custom_db
