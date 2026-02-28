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

""" result_db session manager"""
import contextlib
import logging

import psycopg2
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker

from GaussMaster import global_vars
from GaussMaster.common.metadatabase.utils import create_dsn, DbType
from GaussMaster.common.utils import write_to_terminal
from GaussMaster.common.utils.checking import prepare_ip

session_clz = dict()
CONSTANT_VECTOR = 'VECTOR'


def update_session_clz_from_configs(is_terminal):
    """update session"""
    database = global_vars.configs.get(CONSTANT_VECTOR, 'metadatabase')
    host = global_vars.configs.get(CONSTANT_VECTOR, 'host')
    port = global_vars.configs.get(CONSTANT_VECTOR, 'port')
    username = global_vars.configs.get(CONSTANT_VECTOR, 'user')
    password = global_vars.configs.get(CONSTANT_VECTOR, 'password')
    ssl_mode = global_vars.configs.get(CONSTANT_VECTOR, 'ssl_mode')
    if ssl_mode not in ['disable', 'prefer', 'verify-ca']:
        raise ValueError(f"ssl_mode only support disable or prefer or verify-ca mode."
                         f" Please make sure the parameters meet the requirements.")

    valid_port = port.strip() != '' and port is not None
    valid_host = host.strip() != '' and host is not None
    if not valid_port:
        raise ValueError('Invalid port for metadatabase: %s.' % port)
    if not valid_host:
        raise ValueError('Invalid host for metadatabase: %s.' % host)

    session_clz.clear()
    dsn = create_dsn(DbType.OPENGAUSS.value, database, host, port, username, password)
    postgres_dsn = create_dsn(DbType.OPENGAUSS.value, DbType.POSTGRESQL.value, host, port, username, password)
    engine = create_engine(dsn, pool_pre_ping=True, pool_size=10, max_overflow=10, pool_recycle=25,
                           connect_args={'connect_timeout': 5, 'application_name': 'DBMind-Service',
                                         'sslmode': ssl_mode})
    session_maker = sessionmaker(bind=engine)
    conn = None
    try:
        conn = engine.connect()
        session_clz.update(
            host=host,
            port=port,
            postgres_dsn=postgres_dsn,
            dsn=dsn,
            engine=engine,
            session_maker=session_maker,
            db_type=DbType.OPENGAUSS.value,
            db_name=database
        )
    except (Exception, psycopg2.Error):
        output_connection_info(msg=f'Can not create a valid connection to {prepare_ip(host)}:{port}',
                               is_terminal=is_terminal, level='error')
    try:
        if conn:
            conn.close()
    except Exception:
        output_connection_info(msg=f'Can not close the connection to {prepare_ip(host)}:{port}',
                               is_terminal=False, level='error')


log_level_map = {
    "level": {
        "INFO": "info",
        "WARN": "info",
        "ERROR": "error"
    },
    "color": {
        "INFO": "green",
        "WARN": "yellow",
        "ERROR": "red"
    }
}


def output_connection_info(msg, is_terminal, level):
    """
    - param msg: The tip context.
    - param is_terminal: Print message in terminal, or in log.
    - param level: The log level: ['info', 'warn', 'error']
    """
    if is_terminal:
        write_to_terminal(message=f'{level.upper()}: {msg}', level=log_level_map.get("level").get(level.upper()),
                          color=log_level_map.get("color").get(level.upper()))
    else:
        if level.upper() == 'WARN':
            logging.warning(msg)
        elif level.upper() == 'ERROR':
            logging.error(msg)
        else:
            logging.info(msg)


@contextlib.contextmanager
def get_session():
    """
    get session to execute DML operation
    """
    if not session_clz:
        update_session_clz_from_configs(is_terminal=False)

    if not session_clz:
        raise ConnectionError("Can not get a valid connection to metadatabase")

    session = session_clz['session_maker']()
    session.begin()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logging.error("rollback: %s", e)
    finally:
        session.close()
