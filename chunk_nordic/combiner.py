import asyncio
import logging
import uuid

from aiohttp import web
from .constants import SERVER, BUFSIZE, Way


class Joint:
    def __init__(self, dst_host, dst_port, loop=None):
        self._dst_host = dst_host
        self._dst_port = dst_port
        self._loop = loop if loop is not None else asyncio.get_event_loop()
        self._conn = asyncio.ensure_future(
            asyncio.open_connection(dst_host, dst_port, loop=loop), loop=loop)
        self._upstream = None
        self._downstream = None

    async def _patch_upstream(self, req):
        await self._conn
        try:
            _, writer = self._conn.result()
        except Exception as e:
            return web.Response(text=("Connect error: %s" % str(e)),
                                status=504,
                                headers={"Server": SERVER})
        try:
            while True:
                data = await req.content.read(BUFSIZE)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            return web.Response(status=204, headers={"Server": SERVER})

    async def _patch_downstream(self, req):
        await self._conn
        try:
            reader, _ = self._conn.result()
        except Exception as e:
            return web.Response(text=("Connect error: %s" % str(e)),
                                status=504,
                                headers={"Server": SERVER})

        resp = web.StreamResponse(
            headers={'Content-Type': 'application/octet-stream',
                     'Server': SERVER,})
        resp.enable_chunked_encoding()
        await resp.prepare(request)

        try:
            while True:
                data = reader.read(BUFSIZE)
                if not data:
                    break
                await resp.write(data)
        finally:
            return resp
            

    async def patch_in(self, req, way):
        if way is Way.upstream:
            self._patch_upstream(req)
        elif way is Way.downstream:
            self._patch_downstream(req)


class Combiner:
    SHUTDOWN_TIMEOUT = 1

    def __init__(self, *, address=None, port=8080, ssl_context=None,
                 uri="/chunk-nordic", dst_host, dst_port, loop=None):
        self._loop = loop if loop is not None else asyncio.get_event_loop()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._address = address
        self._port = port
        self._uri = uri
        self._dst_host = dst_host
        self._dst_port = dst_port
        self._ssl_context = ssl_context
        self._joints = {}

    async def stop(self):
        await self._server.shutdown()
        await self._site.stop()
        await self._runner.cleanup()

    async def _dispatch_req(self, req, sid, way):
        if sid not in self._joints:
            self._joints[sid] = Joint(self._dst_host,
                                      self._dst_port,
                                      self._loop)
        return await self._joints[sid].patch_in(req, way)

    async def handler(self, request):
        peer_addr = request.transport.get_extra_info('peername')
        if request.path != self._uri:
            return web.Response(status=404, text="NOT FOUND\n",
                                headers={"Server": SERVER})
        try:
            sid = uuid.UUID(hex=request.headers["X-Session-ID"])
            way = Way(int(request.headers["X-Session-Way"]))
            self._logger.info("Client connected: addr=%s, sid=%s, way=%s.", str(peer_addr), sid, way)
        except:
            return web.Response(status=400, text="INVALID REQUEST\n",
                                headers={"Server": SERVER})
        return await self._dispatch_req(request, sid, way)

    async def start(self):
        self._server = web.Server(self.handler)
        self._runner = web.ServerRunner(self._server)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._address, self._port,
                                 ssl_context=self._ssl_context,
                                 shutdown_timeout=self.SHUTDOWN_TIMEOUT)
        await self._site.start()
        self._logger.info("Server ready.")