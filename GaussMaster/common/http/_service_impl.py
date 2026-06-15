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

import contextlib
import inspect
import json
import logging
import ssl
import sys
import threading
import time
from functools import wraps

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import StarletteHTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from GaussMaster import global_vars
from GaussMaster.common.utils import dbmind_assert, parse_accept_language

_RequestMappingTable = dict()
dbmind_assert(Response)


class HttpService:
    """A Http service implementation.
    ~~~~~~~~~~~~~~~~~~

    To decouple web service framework and web service interface, DBMind implements the class.
    In this way, DBMind can change to another web framework easily.

    DBMind easily migrated from the flask to the fastapi through this class.
    """

    def __init__(self, name=__name__):
        self.app = FastAPI(title=name, openapi_url=None, docs_url=None, redoc_url=None)
        self._server = None
        self.need_to_exit = False

        @self.app.exception_handler(StarletteHTTPException)
        async def exception_handler(_, exc):
            return JSONResponse(
                content={'success': False, 'msg': str(exc.detail)},
                status_code=exc.status_code
            )

        self.rule_num = 0

        @self.app.middleware("http")
        async def set_language_middleware(request: Request, call_next):
            accept_language = request.headers.get('Accept-Language', global_vars.LANGUAGE)
            global_vars.LANGUAGE = parse_accept_language(accept_language)  # 设置全局语言类型
            response = await call_next(request)
            return response

    @property
    def started(self):
        if self._server:
            return self._server.started
        return False

    def attach(self, func, rule, method, **options):
        """Attach a rule to the backend app."""
        is_api = options.pop('api', False)
        rule = rule.replace('<', '{').replace('>', '}')
        if is_api:
            self.app.add_api_route(rule, func, methods=[method], **options)
        else:
            self.app.add_route(rule, func, methods=[method], **options)
        self.rule_num += 1

    def route(self, rule, **options):
        def decorator(f):
            self.attach(f, rule, **options)
            return f

        return decorator

    @staticmethod
    def register_controller_module(module_name):
        __import__(module_name)

    def start_listen(self, host, port,
                     ssl_keyfile=None, ssl_certfile=None,
                     ssl_keyfile_password=None, ssl_ca_file=None):
        this_service = self

        class Server(uvicorn.Server):
            def install_signal_handlers(self) -> None:
                pass

            @contextlib.contextmanager
            def run_in_thread(self):
                thread = threading.Thread(target=self.run, name='WebServiceThread')
                thread.daemon = True
                thread.start()

                try:
                    # Will block here.
                    while not this_service.need_to_exit:
                        time.sleep(0.001)
                    yield
                finally:
                    thread.join(5)
                    _RequestMappingTable.clear()

        # attach global request mapping table.
        for (rule, method), items in _RequestMappingTable.items():
            f, options = items
            self.attach(f, rule, method, **options)

        config = uvicorn.Config(self.app, host=host, port=port,
                                ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile,
                                ssl_keyfile_password=ssl_keyfile_password,
                                ssl_ca_certs=ssl_ca_file,
                                ssl_cert_reqs=ssl.CERT_REQUIRED,
                                log_config=None)
        config.load()
        if config.is_ssl:
            config.ssl.options |= (
                    ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
            )  # RFC 7540 Section 9.2: MUST be TLS >=1.2
            config.ssl.set_ciphers('DHE+AESGCM:ECDHE+AESGCM')
        self._server = Server(config)
        if sys.version_info >= (3, 7):
            with self._server.run_in_thread():
                pass
        else:
            try:
                self._server.run()
            except RuntimeError:
                # Ignore it due to force exit.
                pass

    def wait_for_shutting_down(self):
        lifespan = self._server.lifespan
        if hasattr(lifespan, 'shutdown_event'):
            lifespan.shutdown_event.wait()

    def shutdown(self):
        self.need_to_exit = True
        self._server.handle_exit(None, None)
        self._server.shutdown()
        if sys.version_info < (3, 7):
            import asyncio
            asyncio.get_event_loop().stop()


def request_mapping(rule, method, **kwargs):
    """To record to a static mapping dict.

    Notice: This mapper will be used for all HttpServices if you have many.
    """

    def decorator(f):
        _RequestMappingTable[(rule, method)] = (f, kwargs)
        return f

    return decorator


def standardized_api_output(f):
    class ToleratedEncoder(json.JSONEncoder):
        """Allow to encode more types and abnormal values."""

        def default(self, o):
            return str(o)

        def iterencode(self, o, _one_shot=False):
            try:
                return super().iterencode(o, _one_shot)
            except ValueError:
                if self.check_circular:
                    markers = {}
                else:
                    markers = None
                if self.ensure_ascii:
                    _encoder = json.encoder.encode_basestring_ascii
                else:
                    _encoder = json.encoder.encode_basestring

                def floatstr(o_):
                    if o_ != o_:
                        text = 'null'  # cast NaN to null.
                    elif o_ == float('inf'):
                        text = 'Infinity'
                    elif o_ == -float('inf'):
                        text = '-Infinity'
                    else:
                        return str(o_)

                    return text

                make_iterencode = getattr(json.encoder, '_make_iterencode')
                _iterencode = make_iterencode(
                    markers, self.default, _encoder, self.indent, floatstr,
                    self.key_separator, self.item_separator, self.sort_keys,
                    self.skipkeys, _one_shot)
                return _iterencode(o, 0)

    class ToleratedJSONResponse(JSONResponse):
        def render(self, content) -> bytes:
            return json.dumps(
                content,
                ensure_ascii=False,
                allow_nan=False,
                indent=None,
                sort_keys=True,
                separators=(",", ":"),
                cls=ToleratedEncoder
            ).encode("utf-8")

    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            data = f(*args, **kwargs)
            return ToleratedJSONResponse(content={'success': True, 'data': data})
        except HTTPException as e:
            raise e
        except Exception as e:
            logging.getLogger('uvicorn.error').exception(e)
            return ToleratedJSONResponse(content={'success': False, 'msg': f'Internal server error.'})

    @wraps(f)
    async def async_wrapper(*args, **kwargs):
        """async wrapper"""
        try:
            data = await f(*args, **kwargs)
            return ToleratedJSONResponse(content={'success': True, 'data': data})
        except HTTPException as e:
            raise e
        except Exception as e:
            logging.getLogger('uvicorn.error').exception(e)
            return ToleratedJSONResponse(content={'success': False, 'msg': f'Internal server error.'})

    if inspect.iscoroutinefunction(f):
        return async_wrapper
    return wrapper


def standardized_event_stream_output(f):
    @wraps(f)
    async def wrapper(*args, **kwargs):
        try:
            generator = f(*args, **kwargs)
            if isinstance(generator, str) or not generator:
                return generator

            async def standardize_generator():
                try:
                    async for item in generator:
                        yield f"data:{json.dumps({'success': True, 'data': item}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n\n"
                except Exception as e:
                    logging.getLogger('uvicorn.error').exception(e)
                    err_msg = str(e).strip() or 'Internal server error.'
                    yield f"data:" \
                          f"{json.dumps({'success': False, 'msg': err_msg}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n\n"

            return StreamingResponse(standardize_generator(), media_type="text/event-stream")
        except Exception as e:
            logging.getLogger('uvicorn.error').exception(e)
            error_message = f"data:" \
                            f"{json.dumps({'success': False, 'msg': 'Internal server error.'}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n\n"
            return StreamingResponse(iter([error_message]), media_type="text/event-stream")

    return wrapper
