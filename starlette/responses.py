import hashlib
import http.cookies
import inspect
import json
import os
import stat
import typing
from email.utils import formatdate
from mimetypes import guess_type
from urllib.parse import quote_plus

from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool
from starlette.datastructures import URL, MutableHeaders
from starlette.types import Receive, Scope, Send

try:
    import aiofiles
    from aiofiles.os import stat as aio_stat
except ImportError:  # pragma: nocover
    aiofiles = None  # type: ignore
    aio_stat = None  # type: ignore

try:
    import ujson
except ImportError:  # pragma: nocover
    ujson = None  # type: ignore


class Response:
    media_type = None
    charset = "utf-8"

    def __init__(
        self,
        content: typing.Any = None,
        status_code: int = 200,
        headers: dict = None,
        media_type: str = None,
        background: BackgroundTask = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = MutableHeaders(headers)
        if media_type is not None:
            self.media_type = media_type
        self.background = background

    def render(self, content: typing.Any) -> bytes:
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        return content.encode(self.charset)

    def build_headers(
        self, body: bytes = None
    ) -> typing.List[typing.Tuple[bytes, bytes]]:
        headers = []

        if "Content-Length" not in self.headers and body is not None:
            content_length = str(len(body))
            headers.append((b"content-length", content_length.encode("latin-1")))

        if "Content-Type" not in self.headers and self.media_type is not None:
            content_type = self.media_type
            if content_type.startswith("text/"):
                content_type += "; charset=" + self.charset
            headers.append((b"content-type", content_type.encode("latin-1")))

        return headers + self.headers.raw

    def set_cookie(
        self,
        key: str,
        value: str = "",
        max_age: int = None,
        expires: int = None,
        path: str = "/",
        domain: str = None,
        secure: bool = False,
        httponly: bool = False,
    ) -> None:
        cookie = http.cookies.SimpleCookie()  # type: http.cookies.BaseCookie
        cookie[key] = value
        if max_age is not None:
            cookie[key]["max-age"] = max_age  # type: ignore
        if expires is not None:
            cookie[key]["expires"] = expires  # type: ignore
        if path is not None:
            cookie[key]["path"] = path
        if domain is not None:
            cookie[key]["domain"] = domain
        if secure:
            cookie[key]["secure"] = True  # type: ignore
        if httponly:
            cookie[key]["httponly"] = True  # type: ignore
        cookie_val = cookie.output(header="").strip()
        self.headers.append("set-cookie", cookie_val)

    def delete_cookie(self, key: str, path: str = "/", domain: str = None) -> None:
        self.set_cookie(key, expires=0, max_age=0, path=path, domain=domain)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = self.render(self.content)
        headers = self.build_headers(body)

        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

        if self.background is not None:
            await self.background()


class HTMLResponse(Response):
    media_type = "text/html"


class PlainTextResponse(Response):
    media_type = "text/plain"


class JSONResponse(Response):
    media_type = "application/json"

    def render(self, content: typing.Any) -> bytes:
        return json.dumps(
            self.content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


class UJSONResponse(JSONResponse):
    media_type = "application/json"

    def render(self, content: typing.Any) -> bytes:
        assert ujson is not None, "ujson must be installed to use UJSONResponse"
        return ujson.dumps(content, ensure_ascii=False).encode("utf-8")


class RedirectResponse(Response):
    def __init__(
        self, url: typing.Union[str, URL], status_code: int = 307, headers: dict = None
    ) -> None:
        super().__init__(content=b"", status_code=status_code, headers=headers)
        self.headers["location"] = quote_plus(str(url), safe=":/%#?&=@[]!$&'()*+,;")


class TemplateResponse(Response):
    def __init__(
        self,
        template_name: str,
        context: dict,
        status_code: int = 200,
        headers: dict = None,
        media_type: str = "text/html",
        charset: str = "utf-8",
        background: BackgroundTask = None,
    ) -> None:
        self.template_name = template_name
        self.context = context

        self.status_code = status_code
        self.headers = MutableHeaders(headers)
        self.media_type = media_type
        self.charset = charset
        self.background = background

    def render_template(self, scope: Scope) -> bytes:
        app = scope["app"]
        text = app.render_template(
            template_name=self.template_name, context=self.context
        )
        return text.encode(self.charset)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = self.render_template(scope)
        headers = self.build_headers(body)

        if "http.response.template" in scope.get("extensions", {}):
            await send(
                {
                    "type": "http.response.template",
                    "template": self.template_name,
                    "context": self.context,
                }
            )

        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

        if self.background is not None:
            await self.background()  # pragma: nocover


class StreamingResponse(Response):
    def __init__(
        self,
        content: typing.Any,
        status_code: int = 200,
        headers: typing.Mapping[str, str] = None,
        raw_headers: typing.List[typing.Tuple[bytes, bytes]] = None,
        media_type: str = None,
        background: BackgroundTask = None,
    ) -> None:
        if inspect.isasyncgen(content):
            self.body_iterator = content
        else:
            self.body_iterator = iterate_in_threadpool(content)
        self.status_code = status_code
        self.headers = MutableHeaders(headers=headers, raw=raw_headers)
        self.media_type = self.media_type if media_type is None else media_type
        self.background = background

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.build_headers(),
            }
        )
        async for chunk in self.body_iterator:
            if not isinstance(chunk, bytes):
                chunk = chunk.encode(self.charset)
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

        if self.background is not None:
            await self.background()


class FileResponse(Response):
    chunk_size = 4096

    def __init__(
        self,
        path: str,
        status_code: int = 200,
        headers: dict = None,
        media_type: str = None,
        background: BackgroundTask = None,
        filename: str = None,
        stat_result: os.stat_result = None,
        method: str = None,
    ) -> None:
        assert aiofiles is not None, "'aiofiles' must be installed to use FileResponse"
        self.path = path
        self.status_code = status_code
        self.filename = filename
        self.send_header_only = method is not None and method.upper() == "HEAD"
        if media_type is None:
            media_type = guess_type(filename or path)[0] or "text/plain"
        self.media_type = media_type
        self.headers = MutableHeaders(headers)
        self.background = background
        if self.filename is not None:
            content_disposition = 'attachment; filename="{}"'.format(self.filename)
            self.headers.setdefault("content-disposition", content_disposition)
        self.stat_result = stat_result
        if stat_result is not None:
            self.set_stat_headers(stat_result)

    def set_stat_headers(self, stat_result: os.stat_result) -> None:
        content_length = str(stat_result.st_size)
        last_modified = formatdate(stat_result.st_mtime, usegmt=True)
        etag_base = str(stat_result.st_mtime) + "-" + str(stat_result.st_size)
        etag = hashlib.md5(etag_base.encode()).hexdigest()

        self.headers.setdefault("content-length", content_length)
        self.headers.setdefault("last-modified", last_modified)
        self.headers.setdefault("etag", etag)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.stat_result is None:
            try:
                stat_result = await aio_stat(self.path)
                self.set_stat_headers(stat_result)
            except FileNotFoundError:
                raise RuntimeError(f"File at path {self.path} does not exist.")
            else:
                mode = stat_result.st_mode
                if not stat.S_ISREG(mode):
                    raise RuntimeError(f"File at path {self.path} is not a file.")
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.build_headers(),
            }
        )
        if self.send_header_only:
            await send({"type": "http.response.body"})
        else:
            async with aiofiles.open(self.path, mode="rb") as file:
                more_body = True
                while more_body:
                    chunk = await file.read(self.chunk_size)
                    more_body = len(chunk) == self.chunk_size
                    await send(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": more_body,
                        }
                    )
        if self.background is not None:
            await self.background()
